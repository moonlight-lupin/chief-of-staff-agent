#!/usr/bin/env python3
"""RED-phase tests: Workflow Orchestrator Batch 5 (hooks + CLI + skill-install wiring).

Plan (spec v3.1 US-2/3/6/11; Implementation Decisions; Testing 154-161):
- Wire batches 1-4 into live entry points: hooks.py ALL_HOOKS, chief_of_staff.py
  ``workflows`` subcommand (logs-style nested parsers + set_defaults(func=...)),
  thin ``workflow_install.py``, and doctor_base._get_registered_skills overlay pass.
- Dispatch tests call ``chief_of_staff.main(["--config", ..., "workflows", ...])``
  the same way tests/test_logs_cli_v034.py and tests/test_daily_loop_beta_v030.py do.
Every test is expected to FAIL until the GREEN-phase wiring lands.

Do not re-test batch 1-4 internals (validator, run store, hook bodies, cron math).
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from workflows import generate_skill_md, validate_workflow  # noqa: E402

FROZEN = datetime(2026, 9, 20, 6, 17, tzinfo=timezone.utc)
WORKFLOW_NAME_MAX = 32
EXISTING_HOOK_COUNT = 10
WIRED_HOOK_COUNT = EXISTING_HOOK_COUNT + 2
REQUIRED_VERBS = frozenset(
    {
        "list",
        "runs",
        "start",
        "advance",
        "sync",
        "resume",
        "abort",
        "install",
        "uninstall",
    }
)
PRE_LLM_CALL_NAMES = [
    "company_context_primer",
    "deadline_urgency_injection",
    "wiki_context_injection",
    "workflow_pointer_strip",
]
POST_TOOL_CALL_NAMES = [
    "yaml_integrity_checker",
    "self_sign_guard",
    "workflow_advancement",
]
ON_SESSION_START_NAMES = ["stale_briefing_detector"]
PRE_TOOL_CALL_NAMES = ["pipeline_stage_validator"]
POST_LLM_CALL_NAMES = [
    "format_enforcer",
    "note_capture_reminder",
    "attachment_drive_suggestion",
]
OVERLAY_NAMES = ("invoice-chase", "b5red-chase", "b5red-other")


def _overlay(name: str) -> Path:
    return PLUGIN_ROOT / "skills.local" / name


def _overlay_skill(name: str) -> Path:
    return _overlay(name) / "SKILL.md"


@pytest.fixture(autouse=True)
def _clean_workflow_overlays():
    """Install writes the gitignored skills.local overlay; never the shipped skills/ tree."""
    for name in OVERLAY_NAMES:
        shutil.rmtree(_overlay(name), ignore_errors=True)
    yield
    for name in OVERLAY_NAMES:
        shutil.rmtree(_overlay(name), ignore_errors=True)


def _install_mod():
    """Import the thin installer or fail with an explicit RED message (batch 2 pattern)."""
    try:
        import workflow_install
    except ImportError as e:
        pytest.fail(f"RED: shared/scripts/workflow_install.py not implemented yet ({e})")
    return workflow_install


def _step(step_id, name, signal, **extra):
    step = {
        "id": step_id,
        "name": name,
        "description": f"{name} end to end.",
    }
    step.update(signal)
    step.update(extra)
    return step


def _raw_workflow(name="invoice-chase", steps=None, schedule=None):
    if steps is None:
        steps = [
            _step(
                "list-overdue",
                "List overdue",
                {"command": {"pattern": "show overdue invoices"}},
            ),
            _step(
                "draft-nudge",
                "Draft nudge",
                {"file": {"path": "out/nudge.txt"}},
                required=False,
            ),
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ]
    raw = {
        "name": name,
        "description": "Chases unpaid invoices until they are settled.",
        "steps": steps,
    }
    if schedule is not None:
        cron, timezone_name = schedule
        raw["triggers"] = {"schedule": {"cron": cron, "timezone": timezone_name}}
    return raw


def _validated(name="invoice-chase", steps=None, schedule=None):
    return validate_workflow(_raw_workflow(name=name, steps=steps, schedule=schedule))


def _write_workflow_yaml(project: Path, raw: dict) -> Path:
    path = project / "workflows" / f"{raw['name']}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def temp_project(tmp_path, monkeypatch):
    """StateDB + CLI config on a temp project root (batch 2 fixture + CHIEF_OF_STAFF_CONFIG)."""
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


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _parse(*argv: str):
    """Parse chief_of_staff argv; missing workflows wiring is an explicit RED fail."""
    import chief_of_staff

    try:
        return chief_of_staff.build_parser().parse_args(list(argv))
    except SystemExit as exc:
        pytest.fail(f"RED: workflows parser missing or rejected {list(argv)!r}: {exc}")


def _cos(config_path, *argv):
    """Dispatch via chief_of_staff.main — same helper shape as tests/test_logs_cli_v034.py."""
    import chief_of_staff

    buf = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(err):
            rc = chief_of_staff.main(["--config", str(config_path), *argv])
    except SystemExit as exc:
        pytest.fail(
            f"RED: chief_of_staff.main rejected workflows argv {list(argv)!r}: {exc}"
        )
    return rc, buf.getvalue(), err.getvalue()


def _json_out(out: str):
    text = out.strip()
    assert text, "expected JSON on stdout"
    return json.loads(text)


def _fake_subprocess_ok(monkeypatch, captured=None):
    """Mirror tests/test_workflow_orchestrator_batch4.py hermes-cron stub."""

    class _FakeProc:
        stdout = "ok"
        stderr = ""
        returncode = 0

    def _fake_run(argv, capture_output=True, text=True, timeout=60, check=False, **kwargs):
        if captured is not None:
            captured.append(list(argv))
        return _FakeProc()

    monkeypatch.setattr(subprocess, "run", _fake_run)


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


def _approve_and_claim(config, action_id):
    from state_db import approve_pending_action, mark_executing

    approve_pending_action(config, action_id, approver="MH", reason="Reviewed")
    claimed = mark_executing(config, action_id)
    assert claimed is not None
    return claimed


def _cron_create_actions(config):
    from state_db import list_pending_actions

    return [
        item
        for item in (list_pending_actions(config, include_expired=True) or [])
        if item.get("type") == "cron.create"
    ]


def _pending_ids(config):
    from state_db import list_pending_actions

    return [item.get("id") for item in (list_pending_actions(config, include_expired=True) or [])]


def _hook_names(event: str) -> list[str]:
    from hooks import ALL_HOOKS

    return [name for name, _callback in ALL_HOOKS[event]]


# ---------------------------------------------------------------------------
# Part 1: hooks.py ALL_HOOKS registration contract
# ---------------------------------------------------------------------------


def test_hooks_py_imports_cleanly_with_workflow_hooks_present():
    """Wiring: hooks.py imports while workflow_hooks is in-repo (no absent-module pin)."""
    import hooks
    import workflow_hooks

    assert hooks.ALL_HOOKS
    assert callable(workflow_hooks.pointer_strip)
    assert callable(workflow_hooks.advancement)
    pre_callbacks = [cb for _name, cb in hooks.ALL_HOOKS["pre_llm_call"]]
    if workflow_hooks.pointer_strip not in pre_callbacks:
        pytest.fail("RED: hooks.ALL_HOOKS['pre_llm_call'] does not reference workflow_hooks.pointer_strip")


def test_hooks_py_imports_workflow_hooks_at_module_level():
    """Module-level import required; lazy import inside ALL_HOOKS construction is forbidden.

    Review round B (K1): the import may sit inside a module-level try/except
    (guarded import with None fallbacks) — it must still execute at module
    load, not lazily inside a function or ALL_HOOKS construction.
    """
    tree = ast.parse((PLUGIN_ROOT / "hooks.py").read_text(encoding="utf-8"))
    found = False
    for node in tree.body:
        candidates = [node]
        if isinstance(node, ast.Try):
            candidates = node.body + node.orelse + node.finalbody
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue  # function-level lazy import is forbidden here
        for cand in candidates:
            if isinstance(cand, ast.ImportFrom) and cand.module == "workflow_hooks":
                found = True
                break
            if isinstance(cand, ast.Import) and any(alias.name == "workflow_hooks" for alias in cand.names):
                found = True
                break
        if found:
            break
    if not found:
        pytest.fail("RED: hooks.py must import workflow_hooks at module level")


def test_all_hooks_pre_llm_call_appends_workflow_pointer_strip_last():
    """pre_llm_call keeps existing first-call order; workflow_pointer_strip is appended last."""
    names = _hook_names("pre_llm_call")
    if "workflow_pointer_strip" not in names:
        pytest.fail("RED: workflow_pointer_strip not in ALL_HOOKS['pre_llm_call']")
    assert names == PRE_LLM_CALL_NAMES
    assert names[-1] == "workflow_pointer_strip"


def test_all_hooks_post_tool_call_appends_workflow_advancement_last():
    """post_tool_call keeps existing first-call order; workflow_advancement is appended last."""
    names = _hook_names("post_tool_call")
    if "workflow_advancement" not in names:
        pytest.fail("RED: workflow_advancement not in ALL_HOOKS['post_tool_call']")
    assert names == POST_TOOL_CALL_NAMES
    assert names[-1] == "workflow_advancement"


def test_workflow_hook_tuples_are_name_callback_identity():
    """ALL_HOOKS entries are (name, callback) tuples; callbacks are the workflow_hooks functions."""
    import workflow_hooks
    from hooks import ALL_HOOKS

    pre = ALL_HOOKS["pre_llm_call"]
    post = ALL_HOOKS["post_tool_call"]
    pointer = pre[-1]
    advance = post[-1]
    assert isinstance(pointer, tuple) and len(pointer) == 2
    assert isinstance(advance, tuple) and len(advance) == 2
    assert pointer[0] == "workflow_pointer_strip"
    assert advance[0] == "workflow_advancement"
    assert pointer[1] is workflow_hooks.pointer_strip
    assert advance[1] is workflow_hooks.advancement
    assert callable(pointer[1]) and callable(advance[1])


def test_register_all_hooks_registers_existing_plus_two():
    """register_all_hooks still registers every ALL_HOOKS entry (existing 10 + 2 workflow hooks)."""
    from hooks import ALL_HOOKS, register_all_hooks

    class _RecordingCtx:
        def __init__(self):
            self.calls: list[tuple[str, object]] = []

        def register_hook(self, event, callback):
            self.calls.append((event, callback))

    ctx = _RecordingCtx()
    register_all_hooks(ctx)
    assert _hook_names("on_session_start") == ON_SESSION_START_NAMES
    assert _hook_names("pre_tool_call") == PRE_TOOL_CALL_NAMES
    assert _hook_names("post_llm_call") == POST_LLM_CALL_NAMES
    expected = sum(len(hooks) for hooks in ALL_HOOKS.values())
    assert expected == WIRED_HOOK_COUNT
    assert len(ctx.calls) == WIRED_HOOK_COUNT
    registered_fns = {cb for _event, cb in ctx.calls}
    import workflow_hooks

    assert workflow_hooks.pointer_strip in registered_fns
    assert workflow_hooks.advancement in registered_fns


def test_register_all_hooks_fail_soft_continues_after_raise():
    """A register_hook that raises does not break registration of the rest (hooks.py fail-soft)."""
    from hooks import register_all_hooks

    import workflow_hooks

    class _RecordingCtx:
        def __init__(self):
            self.calls: list[tuple[str, object]] = []

        def register_hook(self, event, callback):
            if callback is workflow_hooks.pointer_strip:
                raise RuntimeError("boom")
            self.calls.append((event, callback))

    ctx = _RecordingCtx()
    register_all_hooks(ctx)
    assert workflow_hooks.pointer_strip not in {cb for _event, cb in ctx.calls}
    assert workflow_hooks.advancement in {cb for _event, cb in ctx.calls}
    assert len(ctx.calls) == WIRED_HOOK_COUNT - 1


# ---------------------------------------------------------------------------
# Part 2: chief_of_staff.py workflows subcommand — parser shape
# ---------------------------------------------------------------------------


def test_workflow_install_module_exposes_install_and_uninstall():
    """Thin shared/scripts/workflow_install.py is the install/uninstall implementation target."""
    mod = _install_mod()
    assert callable(getattr(mod, "install_workflow", None)), (
        "RED: workflow_install.install_workflow is missing"
    )
    assert callable(getattr(mod, "uninstall_workflow", None)), (
        "RED: workflow_install.uninstall_workflow is missing"
    )


def test_workflows_subcommand_exists_with_required_verbs():
    """US-6/US-11: nested ``workflows`` parser mirrors logs (add_parser + nested subparsers)."""
    import chief_of_staff

    root = _subparsers(chief_of_staff.build_parser())
    if root is None or "workflows" not in root.choices:
        pytest.fail("RED: workflows subcommand missing from chief_of_staff.build_parser")
    nested = _subparsers(root.choices["workflows"])
    if nested is None:
        pytest.fail("RED: workflows has no sub-subcommands")
    verbs = set(nested.choices)
    missing = sorted(REQUIRED_VERBS - verbs)
    if missing:
        pytest.fail(f"RED: workflows verbs missing {missing}; have {sorted(verbs)}")
    assert REQUIRED_VERBS <= verbs


def test_workflows_verbs_set_defaults_func():
    """Each workflows verb wires set_defaults(func=...) like daily/logs/capabilities."""
    import chief_of_staff

    root = _subparsers(chief_of_staff.build_parser())
    if root is None or "workflows" not in root.choices:
        pytest.fail("RED: workflows subcommand missing from chief_of_staff.build_parser")
    nested = _subparsers(root.choices["workflows"])
    if nested is None:
        pytest.fail("RED: workflows has no sub-subcommands")
    for verb in sorted(REQUIRED_VERBS):
        if verb not in nested.choices:
            pytest.fail(f"RED: workflows {verb} parser missing")
        func = nested.choices[verb].get_default("func")
        assert callable(func), f"workflows {verb} must set_defaults(func=...)"


def test_workflows_summary_flag_parses_on_runs():
    """US-11: --summary table flag is on the workflows verb (JSON remains the default)."""
    args = _parse("workflows", "runs", "--summary")
    assert getattr(args, "summary", False) is True
    args_default = _parse("workflows", "runs")
    assert getattr(args_default, "summary", False) is False


# ---------------------------------------------------------------------------
# Part 2: dispatch via chief_of_staff.main (logs-cli pattern)
# ---------------------------------------------------------------------------


def test_workflows_runs_json_default_lists_active(temp_project):
    """US-11: ``workflows runs`` prints JSON by default and includes the active run."""
    import workflow_runs

    config, _project, config_path = temp_project
    run = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=_validated(),
        config=config,
        now=FROZEN,
    )
    rc, out, _err = _cos(config_path, "workflows", "runs")
    assert rc == 0
    payload = _json_out(out)
    listed = payload["runs"] if isinstance(payload, dict) else payload
    assert any(item.get("workflow_run_id") == run["workflow_run_id"] for item in listed)


def test_workflows_runs_summary_is_human_table(temp_project):
    """US-11: ``workflows runs --summary`` is a human table, not JSON (batch 2 CLI convention)."""
    import workflow_runs

    config, _project, config_path = temp_project
    workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=_validated(),
        config=config,
        now=FROZEN,
    )
    rc, out, _err = _cos(config_path, "workflows", "runs", "--summary")
    assert rc == 0
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.strip())
    assert "invoice-chase" in out


def test_workflows_list_json_reports_validation_status(temp_project):
    """US-11: ``workflows list`` is JSON default with name + validation status per YAML."""
    _config, project, config_path = temp_project
    _write_workflow_yaml(project, _raw_workflow(name="invoice-chase"))
    bad = project / "workflows" / "b5red-other.yaml"
    bad.write_text("name: b5red-other\n", encoding="utf-8")
    rc, out, _err = _cos(config_path, "workflows", "list")
    assert rc == 0
    payload = _json_out(out)
    rows = payload["workflows"]
    by_name = {row["name"]: row for row in rows}
    assert by_name["invoice-chase"]["valid"] is True
    assert by_name["b5red-other"]["valid"] is False
    assert by_name["b5red-other"].get("error")


def test_workflows_start_dispatches_to_start_run(temp_project):
    """US-6: ``workflows start`` maps to workflow_runs.start_run / cmd_start."""
    import workflow_runs

    config, project, config_path = temp_project
    yaml_path = _write_workflow_yaml(project, _raw_workflow())
    rc, out, _err = _cos(
        config_path,
        "workflows",
        "start",
        "--file",
        str(yaml_path),
        "--session-id",
        "sess-1",
    )
    assert rc == 0
    payload = _json_out(out)
    run_id = payload.get("workflow_run_id") or payload.get("run_id")
    assert run_id
    fetched = workflow_runs.get_run(run_id, config=config)
    assert fetched["workflow_name"] == "invoice-chase"
    assert fetched["state"] == "running"
    assert fetched["session_id"] == "sess-1"


def test_workflows_advance_dispatches_to_advance_run(temp_project):
    """US-7: ``workflows advance --run-id`` maps to workflow_runs.advance_run (manual current step)."""
    import workflow_runs

    config, _project, config_path = temp_project
    run = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=_validated(
            steps=[
                _step("talk", "Talk it through", {"manual": True}),
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
    rc, _out, _err = _cos(
        config_path,
        "workflows",
        "advance",
        "--run-id",
        run["workflow_run_id"],
    )
    assert rc == 0
    fetched = workflow_runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["step_status"][0] == "completed"
    assert fetched["current_step_index"] == 1


def test_workflows_abort_dispatches_to_abort_run(temp_project):
    """US-10: ``workflows abort --run-id`` maps to workflow_runs.abort_run / cmd_abort."""
    import workflow_runs

    config, _project, config_path = temp_project
    run = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=_validated(),
        config=config,
        now=FROZEN,
    )
    rc, _out, _err = _cos(
        config_path,
        "workflows",
        "abort",
        "--run-id",
        run["workflow_run_id"],
    )
    assert rc == 0
    assert workflow_runs.get_run(run["workflow_run_id"], config=config)["state"] == "aborted"


def test_workflows_resume_dispatches_to_resume_run(temp_project):
    """US-6: ``workflows resume --run-id --session-id`` maps to workflow_runs.resume_run."""
    import workflow_runs

    config, _project, config_path = temp_project
    run = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-old",
        workflow=_validated(),
        config=config,
        now=FROZEN,
    )
    rc, _out, _err = _cos(
        config_path,
        "workflows",
        "resume",
        "--run-id",
        run["workflow_run_id"],
        "--session-id",
        "sess-new",
    )
    assert rc == 0
    fetched = workflow_runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["session_id"] == "sess-new"
    assert fetched["state"] == "running"


def test_workflows_sync_active_run_json_summary(temp_project, monkeypatch):
    """US-8: ``workflows sync --workflow`` calls observe_and_advance; JSON {run_id, advanced, observed_at}."""
    import workflow_hooks
    import workflow_runs

    config, _project, config_path = temp_project
    workflow = _validated(
        steps=[
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ]
    )
    run = workflow_runs.start_run(
        workflow["name"],
        "message",
        "sess-1",
        workflow=workflow,
        config=config,
        now=FROZEN,
    )
    action = _create_action(config)
    workflow_runs.bind_action(run["workflow_run_id"], "propose-send", action["id"], config=config)
    _land_executed(config, action["id"], success=True)

    observed: list[tuple] = []
    original = workflow_hooks.observe_and_advance

    def _spy(run_id, config=None, now=None):
        observed.append((run_id, now))
        return original(run_id, config, now=now)

    monkeypatch.setattr(workflow_hooks, "observe_and_advance", _spy)
    rc, out, _err = _cos(
        config_path,
        "workflows",
        "sync",
        "--workflow",
        "invoice-chase",
    )

    assert rc == 0
    payload = _json_out(out)
    assert payload["run_id"] == run["workflow_run_id"]
    assert isinstance(payload["advanced"], list)
    assert "propose-send" in payload["advanced"]
    observed_at = datetime.fromisoformat(str(payload["observed_at"]).replace("Z", "+00:00"))
    assert observed_at.tzinfo is not None
    assert observed
    assert observed[0][0] == run["workflow_run_id"]
    fetched = workflow_runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["step_status"][0] == "completed"


def test_workflows_sync_no_active_run_is_not_an_error(temp_project):
    """``workflows sync --workflow`` with no active run → {run_id: null} and exit 0."""
    _config, _project, config_path = temp_project
    rc, out, _err = _cos(
        config_path,
        "workflows",
        "sync",
        "--workflow",
        "invoice-chase",
    )
    assert rc == 0
    payload = _json_out(out)
    assert payload["run_id"] is None


def test_workflows_install_writes_overlay_skill_md(temp_project, monkeypatch):
    """US-2/US-3: ``workflows install <name>`` validates YAML and writes skills.local/<name>/SKILL.md."""
    _config, project, config_path = temp_project
    raw = _raw_workflow()
    _write_workflow_yaml(project, raw)
    _fake_subprocess_ok(monkeypatch)
    rc, _out, _err = _cos(
        config_path,
        "workflows",
        "install",
        "invoice-chase",
        "--session-id",
        "sess-1",
    )
    assert rc == 0
    skill = _overlay_skill("invoice-chase")
    assert skill.is_file()
    assert skill.read_text(encoding="utf-8") == generate_skill_md(_validated())
    shipped = PLUGIN_ROOT / "skills" / "invoice-chase"
    assert not shipped.exists()
    assert skill.is_relative_to(PLUGIN_ROOT / "skills.local")


def test_workflows_install_scheduled_calls_cron_idempotent(temp_project, monkeypatch):
    """Install with a schedule proposes cron.create; after approve+claim it registers.
    Reinstall rewrites SKILL.md and keeps the same cron id."""
    import workflow_cron

    config, project, config_path = temp_project
    raw = _raw_workflow(schedule=("*/5 * * * *", "UTC"))
    _write_workflow_yaml(project, raw)
    captured: list[list[str]] = []
    _fake_subprocess_ok(monkeypatch, captured)
    args = (
        config_path,
        "workflows",
        "install",
        "invoice-chase",
        "--session-id",
        "sess-1",
    )
    rc1, _out1, _err1 = _cos(*args)
    assert rc1 == 0
    assert workflow_cron.get_cron_binding("invoice-chase", config=config) is None
    assert not any("cron" in cmd and "create" in cmd for cmd in captured)
    proposed = _cron_create_actions(config)
    assert len(proposed) == 1
    _approve_and_claim(config, proposed[0]["id"])
    rc_exec, _out_exec, _err_exec = _cos(*args)
    assert rc_exec == 0
    first = workflow_cron.get_cron_binding("invoice-chase", config=config)
    assert first is not None
    schedule_id = first["schedule_id"]
    skill = _overlay_skill("invoice-chase")
    skill.write_text("stale overlay\n", encoding="utf-8")
    rc2, _out2, _err2 = _cos(*args)
    assert rc2 == 0
    assert skill.read_text(encoding="utf-8") == generate_skill_md(
        _validated(schedule=("*/5 * * * *", "UTC"))
    )
    second = workflow_cron.get_cron_binding("invoice-chase", config=config)
    assert second["schedule_id"] == schedule_id
    assert any("cron" in cmd and "create" in cmd for cmd in captured)


def test_workflows_install_without_schedule_skips_cron(temp_project, monkeypatch):
    """Install of a workflow with no schedule must not call install_workflow_cron."""
    import workflow_cron

    config, project, config_path = temp_project
    _write_workflow_yaml(project, _raw_workflow())
    captured: list[list[str]] = []
    _fake_subprocess_ok(monkeypatch, captured)
    rc, _out, _err = _cos(
        config_path,
        "workflows",
        "install",
        "invoice-chase",
        "--session-id",
        "sess-1",
    )
    assert rc == 0
    assert workflow_cron.get_cron_binding("invoice-chase", config=config) is None
    assert not any("cron" in cmd and "create" in cmd for cmd in captured)
    assert _overlay_skill("invoice-chase").is_file()


def test_workflows_uninstall_removes_overlay_and_cron(temp_project, monkeypatch):
    """``workflows uninstall <name>`` removes skills.local/<name>/ and uninstall_workflow_cron."""
    import workflow_cron

    config, project, config_path = temp_project
    _write_workflow_yaml(project, _raw_workflow(schedule=("*/5 * * * *", "UTC")))
    _fake_subprocess_ok(monkeypatch)
    rc_install, _out_i, _err_i = _cos(
        config_path,
        "workflows",
        "install",
        "invoice-chase",
        "--session-id",
        "sess-1",
    )
    assert rc_install == 0
    proposed = _cron_create_actions(config)
    assert proposed
    _approve_and_claim(config, proposed[0]["id"])
    rc_exec, _out_e, _err_e = _cos(
        config_path,
        "workflows",
        "install",
        "invoice-chase",
        "--session-id",
        "sess-1",
    )
    assert rc_exec == 0
    assert _overlay("invoice-chase").is_dir()
    assert workflow_cron.get_cron_binding("invoice-chase", config=config) is not None
    rc, _out, _err = _cos(config_path, "workflows", "uninstall", "invoice-chase")
    assert rc == 0
    assert not _overlay("invoice-chase").exists()
    assert workflow_cron.get_cron_binding("invoice-chase", config=config) is None
    assert not (PLUGIN_ROOT / "skills" / "invoice-chase").exists()


def test_workflows_uninstall_missing_is_error_json_not_traceback(temp_project):
    """Nonexistent uninstall → nonzero exit + error JSON; never a traceback."""
    _config, _project, config_path = temp_project
    rc, out, err = _cos(config_path, "workflows", "uninstall", "b5red-chase")
    assert rc != 0
    blob = (out or err).strip()
    assert "Traceback" not in out
    assert "Traceback" not in err
    payload = json.loads(blob if blob.startswith("{") else out.strip() or err.strip())
    assert payload.get("error")


def test_workflows_install_invalid_yaml_error_json_no_skill_written(temp_project):
    """Invalid YAML is refused by the batch-1 validator; no SKILL.md, error JSON, no traceback."""
    _config, project, config_path = temp_project
    (project / "workflows" / "b5red-chase.yaml").write_text("name: b5red-chase\n", encoding="utf-8")
    rc, out, err = _cos(
        config_path,
        "workflows",
        "install",
        "b5red-chase",
        "--session-id",
        "sess-1",
    )
    assert rc != 0
    assert not _overlay("b5red-chase").exists()
    assert "Traceback" not in out
    assert "Traceback" not in err
    blob = (out.strip() or err.strip())
    payload = json.loads(blob)
    assert payload.get("error")


def test_workflows_install_uninstall_never_mutate_review_queue(temp_project, monkeypatch):
    """No unapproved mutation: install proposes one cron.create; shells out only after approve+claim.
    Uninstall still never creates review-queue actions."""
    import state_db
    import workflow_cron

    config, project, config_path = temp_project
    _write_workflow_yaml(project, _raw_workflow(schedule=("*/5 * * * *", "UTC")))
    captured: list[list[str]] = []
    _fake_subprocess_ok(monkeypatch, captured)
    mutations: list[str] = []
    original_create = state_db.create_pending_action
    original_approve = state_db.approve_pending_action

    def _spy_create(*args, **kwargs):
        mutations.append("create_pending_action")
        return original_create(*args, **kwargs)

    def _spy_approve(*args, **kwargs):
        mutations.append("approve_pending_action")
        return original_approve(*args, **kwargs)

    monkeypatch.setattr(state_db, "create_pending_action", _spy_create)
    monkeypatch.setattr(state_db, "approve_pending_action", _spy_approve)
    # workflow_cron imported create_pending_action at module load — patch there too.
    monkeypatch.setattr(workflow_cron, "create_pending_action", _spy_create)
    before_ids = set(_pending_ids(config))
    rc_install, _out_i, _err_i = _cos(
        config_path,
        "workflows",
        "install",
        "invoice-chase",
        "--session-id",
        "sess-1",
    )
    assert rc_install == 0
    assert mutations.count("create_pending_action") == 1
    assert "approve_pending_action" not in mutations
    assert not any("cron" in cmd and "create" in cmd for cmd in captured)
    proposed = _cron_create_actions(config)
    assert len(proposed) == 1
    assert proposed[0]["id"] not in before_ids
    _approve_and_claim(config, proposed[0]["id"])
    rc_exec, _out_e, _err_e = _cos(
        config_path,
        "workflows",
        "install",
        "invoice-chase",
        "--session-id",
        "sess-1",
    )
    assert rc_exec == 0
    assert any("cron" in cmd and "create" in cmd for cmd in captured)
    mutations_after_install = list(mutations)
    rc_un, _out_u, _err_u = _cos(config_path, "workflows", "uninstall", "invoice-chase")
    assert rc_un == 0
    assert mutations.count("create_pending_action") == mutations_after_install.count(
        "create_pending_action"
    )


# ---------------------------------------------------------------------------
# Part 3: doctor_base._get_registered_skills — skills.local discovery pass
# ---------------------------------------------------------------------------


def test_get_registered_skills_includes_installed_workflow_name():
    """US-2: _get_registered_skills discovers skills.local/<workflow>/SKILL.md; name ≤32 chars."""
    from doctor_base import _get_registered_skills

    name = "b5red-chase"
    assert len(name) <= WORKFLOW_NAME_MAX
    path = _overlay_skill(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generate_skill_md(_validated(name=name)), encoding="utf-8")
    skills = _get_registered_skills()
    assert name in skills
    assert all(len(skill) <= WORKFLOW_NAME_MAX for skill in skills if skill == name)


def test_install_wiring_reconciles_doctor_cron_skill_files(temp_project, monkeypatch):
    """Install via wiring → cron_skill_files pass; removing the overlay skill while cron remains → warn."""
    import workflow_cron

    config, project, config_path = temp_project
    _write_workflow_yaml(project, _raw_workflow(schedule=("*/5 * * * *", "UTC")))
    _fake_subprocess_ok(monkeypatch)
    rc, _out, _err = _cos(
        config_path,
        "workflows",
        "install",
        "invoice-chase",
        "--session-id",
        "sess-1",
    )
    assert rc == 0
    proposed = _cron_create_actions(config)
    assert proposed
    _approve_and_claim(config, proposed[0]["id"])
    rc_exec, _out_e, _err_e = _cos(
        config_path,
        "workflows",
        "install",
        "invoice-chase",
        "--session-id",
        "sess-1",
    )
    assert rc_exec == 0
    passed = workflow_cron.check_cron_skill_files(False, config, config_path)
    assert passed.status == "pass"
    shutil.rmtree(_overlay("invoice-chase"), ignore_errors=True)
    warned = workflow_cron.check_cron_skill_files(False, config, config_path)
    assert warned.status == "warn"
    assert "invoice-chase" in warned.detail
