#!/usr/bin/env python3
"""Fix-round A+B: reachability (I4/H5/C4/skip/H1/H2) and fail-soft (J1/K1/D1/W6/C3/C5/C6/C7/W4)."""
from __future__ import annotations

import importlib
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from workflows import validate_workflow  # noqa: E402

FROZEN = datetime(2026, 9, 20, 6, 17, tzinfo=timezone.utc)
US8_BOUND = "bound — await operator approval"


def _step(step_id, name, signal, **extra):
    step = {
        "id": step_id,
        "name": name,
        "description": f"{name} end to end.",
    }
    step.update(signal)
    step.update(extra)
    return step


def _raw_workflow(name="invoice-chase", steps=None):
    if steps is None:
        steps = [
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ]
    return {
        "name": name,
        "description": "Chases unpaid invoices until they are settled.",
        "triggers": {"schedule": {"cron": "*/5 * * * *", "timezone": "UTC"}},
        "steps": steps,
    }


def _validated(name="invoice-chase", steps=None):
    return validate_workflow(_raw_workflow(name=name, steps=steps))


@pytest.fixture
def temp_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".audit").mkdir()
    (project / ".runs").mkdir()
    (project / "workflows").mkdir()
    config = {
        "company": {
            "name": "Test Co",
            "jurisdiction": "SG",
            "currency": "SGD",
            "incorporation_date": "2026-01-01",
            "financial_year_end": "31 Dec",
            "business_type": "professional_services",
        },
        "google": {
            "delegate_email": "test@test.com",
            "account_alias": "test",
            "domain": "test.com",
            "service_account_path": "/tmp/sa.json",
        },
        "paths": {
            "project_root": str(project),
            "wiki_path": str(project / "wiki"),
            "templates": str(PLUGIN_ROOT / "shared" / "templates"),
        },
        "delivery": {
            "channel": "telegram",
            "briefing_time": "08:00",
            "weekly_review_day": "friday",
            "weekly_review_time": "17:00",
            "timezone": "Asia/Singapore",
        },
        "integrations": {"workspace": {"provider": "google_api"}},
        "sales_stages": ["Lead", "Proposal Sent", "Paid"],
    }
    config_path = project / "company.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))
    return config, project, config_path


def _cos(config_path, *argv):
    import chief_of_staff

    buf = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        rc = chief_of_staff.main(["--config", str(config_path), *argv])
    return rc, buf.getvalue(), err.getvalue()


def _create_action(config, action_type="gmail.send"):
    from state_db import create_pending_action

    return create_pending_action(
        config=config,
        action_type=action_type,
        provider="google_api",
        target="billing@example.com",
        payload={"to": "billing@example.com", "subject": "nudge", "body": "please pay"},
        summary=f"Test {action_type}",
    )


def _land_executed(config, action_id, *, success=True):
    from state_db import approve_pending_action, mark_executed, mark_executing

    approve_pending_action(config, action_id, approver="MH", reason="Reviewed")
    mark_executing(config, action_id)
    return mark_executed(config, action_id, {"success": success})


def test_advance_run_refuses_awaiting_approval_until_bound_action_executed(temp_project):
    """W2: advance_run refuses awaiting-approval unless bound action is executed+success."""
    import workflow_runs

    config, _project, _config_path = temp_project
    run = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=_validated(),
        config=config,
        now=FROZEN,
    )
    action = _create_action(config)
    workflow_runs.bind_action(
        run["workflow_run_id"], "propose-send", action["id"], config=config
    )
    parked = workflow_runs.get_run(run["workflow_run_id"], config=config)
    assert parked["state"] == "awaiting-approval"
    with pytest.raises(workflow_runs.WorkflowRunError, match="approval"):
        workflow_runs.advance_run(run["workflow_run_id"], 0, config=config)
    still = workflow_runs.get_run(run["workflow_run_id"], config=config)
    assert still["state"] == "awaiting-approval"
    assert still["current_step_index"] == 0

    _land_executed(config, action["id"], success=True)
    advanced = workflow_runs.advance_run(run["workflow_run_id"], 0, config=config)
    assert advanced["step_status"][0] == "completed"


def test_cmd_advance_refuses_non_manual_current_step(temp_project):
    """W2: workflows advance refuses when the current step's signal is not manual."""
    import workflow_runs

    config, _project, config_path = temp_project
    run = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=_validated(
            steps=[
                _step(
                    "list-overdue",
                    "List overdue",
                    {"command": {"pattern": "show overdue invoices"}},
                ),
            ]
        ),
        config=config,
        now=FROZEN,
    )
    rc, out, err = _cos(
        config_path,
        "workflows",
        "advance",
        "--run-id",
        run["workflow_run_id"],
    )
    assert rc != 0
    blob = (out.strip() or err.strip())
    payload = json.loads(blob)
    assert payload.get("error")
    assert "manual" in str(payload.get("error")).lower()
    fetched = workflow_runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["current_step_index"] == 0
    assert fetched["step_status"][0] == "pending"


def test_uninstall_refuses_path_traversal_bookkeeper_intact(temp_project):
    """BLOCK-2: workflows uninstall ../skills/bookkeeper is refused; bundled skill intact."""
    import workflow_install

    _config, _project, config_path = temp_project
    bookkeeper = PLUGIN_ROOT / "skills" / "bookkeeper"
    skill = bookkeeper / "SKILL.md"
    assert bookkeeper.is_dir()
    sentinel = skill.read_text(encoding="utf-8")
    rc, out, err = _cos(config_path, "workflows", "uninstall", "../skills/bookkeeper")
    assert rc != 0
    blob = (out.strip() or err.strip())
    payload = json.loads(blob)
    assert payload.get("error")
    assert bookkeeper.is_dir()
    assert skill.read_text(encoding="utf-8") == sentinel
    with pytest.raises(workflow_install.WorkflowInstallError):
        workflow_install.uninstall_workflow("../skills/bookkeeper", config=_config)


def test_pointer_strip_bound_approval_uses_us8_wording(temp_project):
    """H4: bound approval strip renders US-8 wording, not a bare self-approve imperative."""
    import workflow_hooks
    import workflow_runs

    config, _project, _config_path = temp_project
    run = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=_validated(),
        config=config,
        now=FROZEN,
    )
    action = _create_action(config)
    action_id = action["id"]
    workflow_runs.bind_action(
        run["workflow_run_id"], "propose-send", action_id, config=config
    )
    strip = workflow_hooks.pointer_strip(
        {"session_id": "sess-1"}, config=config, now=FROZEN
    )
    assert isinstance(strip, str)
    assert f"action {action_id} {US8_BOUND}" in strip
    assert "review_queue.py approve --action-id" in strip
    approve_at = strip.lower().find("review_queue.py approve")
    operator_at = strip.lower().find("operator:")
    assert operator_at != -1
    assert operator_at < approve_at
    assert len(strip) <= 200


def test_start_run_audit_carries_action_actor_and_run_id(temp_project):
    """W3: mutate_kv audit for start includes workflow.start, operator, workflow_run_id."""
    from audit_log import read_audit
    import workflow_runs

    config, _project, _config_path = temp_project
    run = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=_validated(),
        config=config,
        now=FROZEN,
    )
    entries = read_audit("workflow_runs", limit=20, config=config)
    assert entries
    last = entries[-1]
    assert last.get("action") == "workflow.start"
    assert last.get("actor") == "operator"
    after = last.get("after") or {}
    assert after.get("workflow_run_id") == run["workflow_run_id"]


def _write_workflow_yaml(project, workflow):
    wf_dir = project / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    path = wf_dir / f"{workflow['name']}.yaml"
    path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")
    return path


def _fake_hermes_ok(monkeypatch, captured=None, returncode=0, stderr=""):
    class _Proc:
        stdout = "ok"
        def __init__(self):
            self.returncode = returncode
            self.stderr = stderr

    def _run(argv, capture_output=True, text=True, timeout=60, check=False, **kwargs):
        if captured is not None:
            captured.append(list(argv))
        return _Proc()

    monkeypatch.setattr(subprocess, "run", _run)


def _facts_payload(now=FROZEN, capabilities=None, state_files=None):
    return {
        "refreshed_at": now.isoformat(),
        "facts_max_age_hours": 1,
        "capabilities": capabilities
        if capabilities is not None
        else {"gmail.send": True, "gmail.draft": True, "gmail.search": True},
        "state_files": state_files
        if state_files is not None
        else {"pipeline.yaml": True, "invoices.yaml": True},
    }


def test_bind_action_cli_binds_approval_step(temp_project):
    """I4: workflows bind-action delegates to bind_action for an approval-gated step."""
    import workflow_runs

    config, project, config_path = temp_project
    run = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=_validated(),
        config=config,
        now=FROZEN,
    )
    action = _create_action(config)
    rc, out, err = _cos(
        config_path,
        "workflows",
        "bind-action",
        "--workflow",
        "invoice-chase",
        "--step",
        "propose-send",
        "--action-id",
        action["id"],
    )
    assert rc == 0, err or out
    fetched = workflow_runs.get_run(run["workflow_run_id"], config=config)
    bound = next(s for s in fetched["definition"]["steps"] if s["id"] == "propose-send")
    assert bound["action_id"] == action["id"]
    assert fetched["state"] == "awaiting-approval"


def test_bind_action_cli_refuses_non_approval_step(temp_project):
    """I4: bind-action refuses when the named step is not approval-gated."""
    import workflow_runs

    config, _project, config_path = temp_project
    workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=_validated(
            steps=[
                _step(
                    "list-overdue",
                    "List overdue",
                    {"command": {"pattern": "show overdue invoices"}},
                ),
            ]
        ),
        config=config,
        now=FROZEN,
    )
    rc, out, err = _cos(
        config_path,
        "workflows",
        "bind-action",
        "--workflow",
        "invoice-chase",
        "--step",
        "list-overdue",
        "--action-id",
        "act-1",
    )
    assert rc != 0
    payload = json.loads((out.strip() or err.strip()))
    assert payload.get("error")
    assert "approval" in str(payload.get("error")).lower() or "gated" in str(payload.get("error")).lower()


def test_refresh_facts_then_pointer_strip_is_go(temp_project):
    """H5: facts written via refresh-facts → next pointer strip computes GO."""
    import workflow_hooks
    import workflow_runs

    config, project, config_path = temp_project
    run = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=_validated(),
        config=config,
        now=FROZEN,
    )
    facts_path = project / "facts.json"
    facts_path.write_text(json.dumps(_facts_payload()), encoding="utf-8")
    rc, out, err = _cos(
        config_path,
        "workflows",
        "refresh-facts",
        "--workflow",
        "invoice-chase",
        "--run-id",
        run["workflow_run_id"],
        "--facts",
        str(facts_path),
    )
    assert rc == 0, err or out
    stored = workflow_runs.get_facts(config=config)
    assert isinstance(stored, dict)
    assert stored.get("capabilities", {}).get("gmail.send") is True
    verdict = workflow_hooks.compute_verdict(run, stored, now=FROZEN)
    assert verdict.get("verdict") == "GO"
    strip = workflow_hooks.pointer_strip({"session_id": "sess-1"}, config=config, now=FROZEN)
    assert isinstance(strip, str)
    assert "GO" in strip
    assert "DEGRADED" not in strip


def test_install_cron_uninstall_cron_verbs_exist_and_work(temp_project, monkeypatch):
    """I4 / US-9: install-cron and uninstall-cron verbs exist and re-install."""
    _fake_hermes_ok(monkeypatch)
    _config, project, config_path = temp_project
    _write_workflow_yaml(
        project,
        _raw_workflow(
            steps=[
                _step(
                    "list-overdue",
                    "List overdue",
                    {"command": {"pattern": "show overdue invoices"}},
                )
            ]
        ),
    )
    from state_db import approve_pending_action, mark_executing

    rc, out, err = _cos(config_path, "workflows", "install-cron", "invoice-chase", "--session-id", "sess-1")
    assert rc == 0, err or out
    payload = json.loads(out)
    if payload.get("pending_action_id"):
        action_id = payload["pending_action_id"]
        approve_pending_action(_config, action_id, approver="MH", reason="ok")
        mark_executing(_config, action_id)
        rc, out, err = _cos(config_path, "workflows", "install-cron", "invoice-chase", "--session-id", "sess-1")
        assert rc == 0, err or out
        payload = json.loads(out)
    assert payload.get("cron_installed") is True
    rc, out, err = _cos(config_path, "workflows", "uninstall-cron", "invoice-chase")
    assert rc == 0, err or out
    removed = json.loads(out)
    assert removed.get("cron_removed") is True


def test_fire_verb_wired_and_wakeup_delivers(temp_project, monkeypatch):
    """C4: fire verb is wired; wakeup delivers; distinct occurrences are not capped at 1."""
    import workflow_cron
    import workflow_runs

    config, _project, config_path = temp_project
    captured: list[list[str]] = []
    _fake_hermes_ok(monkeypatch, captured)
    workflow = _validated(
        steps=[
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ],
    )
    binding = workflow_cron.install_workflow_cron(
        "invoice-chase", workflow, config, now=FROZEN, session_id="sess-1"
    )
    prompt_cmds = [c for c in captured if "create" in c]
    assert prompt_cmds
    assert any("workflows fire --schedule-id" in " ".join(c) for c in prompt_cmds)
    t0 = FROZEN.replace(second=0, microsecond=0)
    first = workflow_cron.fire_occurrence("invoice-chase", config, now=t0)
    assert first is not None
    run = [r for r in workflow_runs.list_runs(config=config) if r["state"] in workflow_runs.ACTIVE_STATES][0]
    action = _create_action(config)
    workflow_runs.bind_action(run["workflow_run_id"], "propose-send", action["id"], config=config)
    t5 = t0 + timedelta(minutes=5)
    t10 = t0 + timedelta(minutes=10)
    workflow_cron.fire_occurrence("invoice-chase", config, now=t5)
    workflow_cron.fire_occurrence("invoice-chase", config, now=t10)
    notes = workflow_cron.get_cron_binding("invoice-chase", config=config).get("wakeup_notes") or []
    assert len(notes) == 2
    assert all(n.get("delivery_target") for n in notes)
    assert notes[0].get("occurrence_count") != notes[1].get("occurrence_count") or notes[0].get("at") != notes[1].get("at")
    same = workflow_cron.fire_occurrence("invoice-chase", config, now=t10)
    assert same is None
    notes_again = workflow_cron.get_cron_binding("invoice-chase", config=config).get("wakeup_notes") or []
    assert len(notes_again) == 2
    rc, out, err = _cos(config_path, "workflows", "fire", "--schedule-id", binding["schedule_id"])
    assert rc == 0, err or out


def test_skip_step_wired_completed_degraded_reachable(temp_project):
    """Skip marking: DEGRADED skip list on the current step → skipped; completed (degraded) reachable."""
    import workflow_hooks
    import workflow_runs

    config, _project, _config_path = temp_project
    workflow = _validated(
        steps=[
            _step(
                "list-overdue",
                "List overdue",
                {"command": {"pattern": "show overdue invoices"}},
            ),
            _step(
                "optional-nudge",
                "Optional nudge",
                {"review_queue": {"action_type": "drive.upload"}},
                required=False,
            ),
        ]
    )
    run = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=workflow,
        config=config,
        now=FROZEN,
    )
    facts = _facts_payload(
        capabilities={"gmail.send": True, "drive.upload": False},
        state_files={"pipeline.yaml": True, "invoices.yaml": True},
    )
    workflow_runs.refresh_facts(facts, config=config, workflow_name="invoice-chase", run_id=run["workflow_run_id"])
    later = FROZEN + timedelta(minutes=2)
    workflow_hooks.advancement(
        "terminal",
        {"command": "python show overdue invoices"},
        json.dumps({"exit_code": 0}),
        {"session_id": "sess-1"},
        config=config,
        now=later,
    )
    fetched = workflow_runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["step_status"][0] == "completed"
    assert fetched["step_status"][1] == "skipped"
    assert fetched["state"] == "completed"
    assert fetched.get("degraded") is True


def test_command_signal_four_arg_post_tool_call(temp_project):
    """H1: documented 4-arg post_tool_call (no exit_code/config kwargs) advances a command step."""
    import workflow_hooks
    import workflow_runs

    config, _project, _config_path = temp_project
    workflow = _validated(
        steps=[
            _step(
                "list-overdue",
                "List overdue",
                {"command": {"pattern": "show overdue invoices"}},
            ),
        ]
    )
    run = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=workflow,
        config=config,
        now=FROZEN,
    )
    result = workflow_hooks.advancement(
        "terminal",
        {"command": "python invoices.py show overdue invoices --json"},
        json.dumps({"exit_code": 0}),
        {"session_id": "sess-1"},
    )
    assert result is None
    fetched = workflow_runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["step_status"][0] == "completed"


def test_file_signal_four_arg_post_tool_call(temp_project):
    """H2: documented 4-arg post_tool_call (no config kwarg) advances a file step via load_config()."""
    import workflow_hooks
    import workflow_runs

    config, project, _config_path = temp_project
    workflow = _validated(
        steps=[
            _step(
                "draft-nudge",
                "Draft nudge",
                {"file": {"path": "out/nudge.txt"}},
            ),
        ]
    )
    run = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=workflow,
        config=config,
        now=FROZEN,
    )
    later = FROZEN + timedelta(minutes=2)
    path = project / "out" / "nudge.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("nudge body\n", encoding="utf-8")
    epoch = later.timestamp()
    os.utime(path, (epoch, epoch))
    result = workflow_hooks.advancement(
        "write_file",
        {"path": "out/nudge.txt"},
        "ok",
        {"session_id": "sess-1"},
    )
    assert result is None
    fetched = workflow_runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["step_status"][0] == "completed"


def test_build_parser_survives_poisoned_workflow_install():
    """J1: chief_of_staff.build_parser() does not raise when workflow_install is poisoned."""
    import chief_of_staff

    saved = sys.modules.get("workflow_install")
    sys.modules["workflow_install"] = None
    try:
        parser = chief_of_staff.build_parser()
        assert parser is not None
    finally:
        if saved is not None:
            sys.modules["workflow_install"] = saved
        else:
            sys.modules.pop("workflow_install", None)


def test_hooks_import_survives_poisoned_workflow_hooks():
    """K1: poisoning workflow_hooks still imports hooks; ALL_HOOKS keeps the original 10."""
    saved_wh = sys.modules.get("workflow_hooks")
    sys.modules["workflow_hooks"] = None
    sys.modules.pop("hooks", None)
    try:
        hooks_mod = importlib.import_module("hooks")
        total = sum(len(items) for items in hooks_mod.ALL_HOOKS.values())
        assert total == 10
        names = [name for items in hooks_mod.ALL_HOOKS.values() for name, _cb in items]
        assert "company_context_primer" in names
        assert "workflow_pointer_strip" not in names
        assert "workflow_advancement" not in names
    finally:
        if saved_wh is not None:
            sys.modules["workflow_hooks"] = saved_wh
        else:
            sys.modules.pop("workflow_hooks", None)
        sys.modules.pop("hooks", None)
        importlib.import_module("hooks")


def test_doctor_run_checks_survives_poisoned_workflow_cron(temp_project):
    """D1: poison workflow_cron → run_checks completes with 4 warns, no raise."""
    from doctor_base import run_checks

    _config, _project, config_path = temp_project
    saved = sys.modules.get("workflow_cron")
    sys.modules["workflow_cron"] = None
    try:
        results = run_checks(fix=False, config=str(config_path))
    finally:
        if saved is not None:
            sys.modules["workflow_cron"] = saved
        else:
            sys.modules.pop("workflow_cron", None)
    names = {
        "cron_skill_files",
        "stale_run",
        "unhonored_advancement",
        "workflow_crons_doc",
    }
    warns = [r for r in results if getattr(r, "name", "") in names]
    assert len(warns) == 4
    assert all(getattr(r, "status", "") == "warn" for r in warns)


def test_cli_error_json_and_abort_catches_exception(temp_project):
    """W6: abort of unknown run emits JSON error (not a traceback)."""
    _config, _project, config_path = temp_project
    rc, out, err = _cos(config_path, "workflows", "abort", "--run-id", "wf-missing-nope")
    assert rc != 0
    blob = out.strip() or err.strip()
    payload = json.loads(blob)
    assert payload.get("error")


def test_failed_hermes_create_does_not_write_binding(temp_project, monkeypatch):
    """C3: failed hermes cron create raises and does not write the binding."""
    import workflow_cron

    config, _project, _config_path = temp_project
    _fake_hermes_ok(monkeypatch, returncode=1, stderr="boom from hermes")
    with pytest.raises(workflow_cron.WorkflowRunError, match="hermes"):
        workflow_cron.install_workflow_cron(
            "invoice-chase",
            _validated(),
            config,
            now=FROZEN,
            session_id="sess-1",
        )
    assert workflow_cron.get_cron_binding("invoice-chase", config=config) is None


def test_check_stale_run_uses_is_stale_48h_and_step_name(temp_project, monkeypatch):
    """C5: stale check uses last_progress_at / 48h and names the stuck step."""
    import workflow_cron
    import workflow_runs

    config, _project, config_path = temp_project
    _fake_hermes_ok(monkeypatch)
    run = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=_validated(),
        config=config,
        now=FROZEN,
    )
    later = FROZEN + timedelta(hours=49)
    result = workflow_cron.check_stale_run(False, config, config_path, now=later)
    assert result.status == "warn"
    assert run["workflow_run_id"] in result.detail
    assert "Propose send" in result.detail or "propose-send" in result.detail
    fresh = workflow_cron.check_stale_run(
        False, config, config_path, now=FROZEN + timedelta(hours=25)
    )
    assert fresh.status == "pass"


def test_cron_reconciliation_and_facts_doctor_warns(temp_project, monkeypatch):
    """C6 + H5 doctor: scheduled YAML without binding; binding without YAML; missing facts."""
    import workflow_cron
    import workflow_runs

    config, project, config_path = temp_project
    _fake_hermes_ok(monkeypatch)
    _write_workflow_yaml(project, _raw_workflow())
    result = workflow_cron.check_workflow_crons_doc(False, config, config_path)
    assert result.status == "warn"
    assert "scheduled workflow without cron binding" in result.detail
    assert "workflow_facts missing" in result.detail
    workflow_cron.install_workflow_cron(
        "ghost-flow",
        _validated(name="ghost-flow"),
        config,
        now=FROZEN,
        session_id="sess-1",
    )
    result = workflow_cron.check_workflow_crons_doc(False, config, config_path)
    assert "cron binding whose workflow YAML was deleted" in result.detail
    workflow_runs.start_run(
        "vanished",
        "message",
        "sess-1",
        workflow=_validated(name="vanished"),
        config=config,
        now=FROZEN,
    )
    result = workflow_cron.check_workflow_crons_doc(False, config, config_path)
    assert "active run whose workflow disappeared" in result.detail


def test_cron_sunday_seven_matches_sunday_not_minute_zero():
    """C7: Sunday=7 alias compares field index; `7 * * * 7` does not match at :00."""
    import workflow_cron

    sunday = datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc)
    assert sunday.weekday() == 6
    assert workflow_cron._cron_matches("7 * * * 7", sunday) is False
    sunday_seven = sunday.replace(minute=7)
    assert workflow_cron._cron_matches("7 * * * 7", sunday_seven) is True
    assert workflow_cron._cron_matches("0 * * * 7", sunday) is True
    monday = datetime(2026, 9, 21, 6, 7, tzinfo=timezone.utc)
    assert monday.weekday() == 0
    assert workflow_cron._cron_matches("7 * * * 7", monday) is False


def test_failed_is_terminal(temp_project):
    """W4: failed is a terminal state; a failed run is neither active nor advanceable."""
    import workflow_runs
    from state_db import mutate_kv

    config, _project, _config_path = temp_project
    assert "failed" in workflow_runs.TERMINAL_STATES
    run = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=_validated(),
        config=config,
        now=FROZEN,
    )
    run_id = run["workflow_run_id"]

    def _fail(data):
        runs = data.setdefault("runs", {})
        runs[run_id]["state"] = "failed"
        return runs[run_id]

    mutate_kv("workflow_runs", _fail, config=config)
    fetched = workflow_runs.get_run(run_id, config=config)
    assert fetched["state"] == "failed"
    with pytest.raises(workflow_runs.WorkflowRunError, match="terminal"):
        workflow_runs.advance_run(run_id, 0, config=config)
    second = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-2",
        workflow=_validated(),
        config=config,
        now=FROZEN + timedelta(minutes=1),
    )
    assert second["workflow_run_id"] != run_id
