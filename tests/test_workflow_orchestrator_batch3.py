#!/usr/bin/env python3
"""RED-phase tests: Workflow Orchestrator Batch 3 (pointer strip + advancement hooks).

Plan (spec v3.1 US-4/5/7/8/11; Implementation 142-153; Testing 154-161):
- Target: shared/scripts/workflow_hooks.py
- Hook 1 ``pointer_strip`` — pre_llm_call signature (context, **kwargs) → str | None
- Hook 2 ``advancement`` — post_tool_call signature (tool_name, args, result, context, **kwargs)
- Pure ``compute_verdict(run, facts, now=)`` and public ``observe_and_advance(run_id, config, now=)``
Every test is expected to FAIL until the GREEN-phase implementation lands.

Do NOT assert ALL_HOOKS registration, CLI wiring, cron, architect, or doctor (later batches).
Audit actor/metadata is SKIPPED: batch 2's mutate_kv seam does not pass actor through.
"""
from __future__ import annotations

import copy
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from workflows import validate_workflow  # noqa: E402

FROZEN = datetime(2026, 9, 20, 6, 17, tzinfo=timezone.utc)
STORE_RUNS = "workflow_runs"
STORE_FACTS = "workflow_facts"
# US-4 hard-asserts the bracketed phrase; the leading `next: step N` is asserted separately.
GATE_PHRASE = "[APPROVAL REQUIRED — propose, do not execute]"
APPROVE_CMD_PREFIX = "review_queue.py approve --action-id"


def _hooks():
    """Import the hooks module or fail with an explicit RED message."""
    try:
        import workflow_hooks
    except ImportError as e:
        pytest.fail(f"RED: shared/scripts/workflow_hooks.py not implemented yet ({e})")
    return workflow_hooks


def _runs():
    import workflow_runs

    return workflow_runs


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
    return {
        "name": name,
        "description": "Chases unpaid invoices until they are settled.",
        "steps": steps,
    }


def _validated(name="invoice-chase", steps=None):
    return validate_workflow(_raw_workflow(name=name, steps=steps))


def _ctx(session_id="sess-1"):
    """Fake Hermes ctx — same shape as tests/test_hooks.py, plus session_id for US-4 gating."""
    return {
        "session_id": session_id,
        "loaded_skills": ["daily-briefing", "pipeline-manager"],
    }


@pytest.fixture
def temp_project(tmp_path, monkeypatch):
    """StateDB + company.yaml on a temp project root; sets CHIEF_OF_STAFF_CONFIG like hook tests."""
    project = tmp_path / "project"
    project.mkdir()
    (project / ".audit").mkdir()
    (project / ".runs").mkdir()
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


def _start(
    mod,
    config,
    *,
    workflow=None,
    trigger="message",
    session_id="sess-1",
    now=FROZEN,
):
    wf = workflow if workflow is not None else _validated()
    return mod.start_run(
        wf["name"],
        trigger,
        session_id,
        workflow=wf,
        config=config,
        now=now,
    )


def _seed_facts(
    config,
    *,
    now=FROZEN,
    capabilities=None,
    state_files=None,
    refreshed_at=None,
    facts_max_age_hours=1,
):
    """Write the workflow_facts kv doc. Shape is the batch-3 contract (see report)."""
    from state_db import mutate_kv

    payload = {
        "refreshed_at": (refreshed_at if refreshed_at is not None else now).isoformat(),
        "facts_max_age_hours": facts_max_age_hours,
        "capabilities": capabilities
        if capabilities is not None
        else {"gmail.send": True, "gmail.draft": True, "gmail.search": True},
        "state_files": state_files
        if state_files is not None
        else {"pipeline.yaml": True, "invoices.yaml": True},
    }

    def _write(data):
        data.clear()
        data.update(copy.deepcopy(payload))
        return data

    mutate_kv(STORE_FACTS, _write, config=config)
    return payload


def _load_runs_doc(config):
    from state_db import load_store

    return load_store(STORE_RUNS, config=config, validate=False)


def _pointer(mod, ctx, config, now=FROZEN, **kwargs):
    return mod.pointer_strip(ctx, config=config, now=now, **kwargs)


def _advance_hook(mod, tool_name, args, result, ctx, config, now=FROZEN, **kwargs):
    return mod.advancement(
        tool_name,
        args,
        result,
        ctx,
        config=config,
        now=now,
        **kwargs,
    )


def _verdict_token(value):
    if isinstance(value, dict):
        return str(value.get("verdict") or "")
    return str(value)


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
    """Write terminal review-queue state via the store API (US-8 out-of-band record-execution)."""
    from state_db import approve_pending_action, mark_executed, mark_executing

    approve_pending_action(config, action_id, approver="MH", reason="Reviewed")
    mark_executing(config, action_id)
    return mark_executed(config, action_id, {"success": success})


def _touch_file(project: Path, relative: str, when: datetime) -> Path:
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("nudge body\n", encoding="utf-8")
    epoch = when.timestamp()
    os.utime(path, (epoch, epoch))
    return path


# ---------------------------------------------------------------------------
# US-4: pointer strip gate — zero injection unless this session owns an active run
# ---------------------------------------------------------------------------


def test_pointer_strip_no_active_run_returns_none(temp_project):
    """US-4: no active run → zero injection (None), including after a run completes."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    _seed_facts(config)
    assert _pointer(hooks, _ctx("sess-1"), config) is None

    run = _start(runs, config, session_id="sess-1", now=FROZEN)
    runs.complete_run(run["workflow_run_id"], config=config, now=FROZEN + timedelta(minutes=1))
    assert _pointer(hooks, _ctx("sess-1"), config, now=FROZEN + timedelta(minutes=2)) is None


def test_pointer_strip_no_workflow_runs_doc_returns_none(temp_project):
    """US-4: missing workflow_runs kv document → None (not an error)."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    _seed_facts(config)
    assert _pointer(hooks, _ctx("sess-1"), config) is None


def test_pointer_strip_unrelated_session_returns_none(temp_project):
    """US-4: a session that does not own the active run gets zero injection."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    _seed_facts(config)
    _start(runs, config, session_id="sess-owner", now=FROZEN)
    assert _pointer(hooks, _ctx("sess-other"), config) is None


def test_pointer_strip_owning_session_injects_name_step_state_verdict_next(temp_project):
    """US-4: owning session gets one strip: workflow name, step n/N, state, verdict, next action."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    workflow = _validated()
    _seed_facts(config)
    _start(runs, config, workflow=workflow, session_id="sess-1", now=FROZEN)

    strip = _pointer(hooks, _ctx("sess-1"), config)
    assert isinstance(strip, str)
    assert "invoice-chase" in strip
    assert "1/3" in strip
    assert "running" in strip.lower()
    assert "GO" in strip
    assert "list-overdue" in strip or "List overdue" in strip
    assert len(strip) <= 200


def test_pointer_strip_resumed_session_receives_strip(temp_project):
    """US-4/US-6: resume_run rebinds the owner; the new session gets the strip, the old one does not."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    _seed_facts(config)
    run = _start(runs, config, session_id="sess-old", now=FROZEN)
    runs.resume_run(
        run["workflow_run_id"],
        "sess-new",
        config=config,
        now=FROZEN + timedelta(minutes=1),
    )
    later = FROZEN + timedelta(minutes=1)
    assert _pointer(hooks, _ctx("sess-old"), config, now=later) is None
    strip = _pointer(hooks, _ctx("sess-new"), config, now=later)
    assert isinstance(strip, str)
    assert "invoice-chase" in strip


# ---------------------------------------------------------------------------
# US-4 / US-8: approval gate marker + awaiting-approval copy
# ---------------------------------------------------------------------------


def test_pointer_strip_approval_required_renders_gate_marker_literally(temp_project):
    """US-4: an approval-required current step is never a bare imperative — gate phrase is literal."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    workflow = _validated(
        steps=[
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ]
    )
    _seed_facts(config)
    _start(runs, config, workflow=workflow, session_id="sess-1", now=FROZEN)
    strip = _pointer(hooks, _ctx("sess-1"), config)
    assert isinstance(strip, str)
    assert GATE_PHRASE in strip
    assert "next:" in strip
    # Bare "execute" as the next action would violate the gate; the phrase must be present.
    assert "1/1" in strip
    assert len(strip) <= 200


def test_pointer_strip_awaiting_approval_bound_names_action_id_and_approve_command(temp_project):
    """US-8: parked + bound strip names the action id and the literal approve command."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    workflow = _validated(
        steps=[
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ]
    )
    _seed_facts(config)
    run = _start(runs, config, workflow=workflow, session_id="sess-1", now=FROZEN)
    action = _create_action(config)
    action_id = action["id"]
    runs.bind_action(run["workflow_run_id"], "propose-send", action_id, config=config)

    strip = _pointer(hooks, _ctx("sess-1"), config)
    assert isinstance(strip, str)
    assert action_id in strip
    assert APPROVE_CMD_PREFIX in strip
    assert "awaiting-approval" in strip.lower() or "awaiting" in strip.lower()
    assert len(strip) <= 200


def test_pointer_strip_awaiting_approval_unbound_says_propose_and_bind(temp_project):
    """US-8: approval step with no action_id tells the model to propose and bind via workflows bind-action."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    workflow = _validated(
        steps=[
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ]
    )
    _seed_facts(config)
    _start(runs, config, workflow=workflow, session_id="sess-1", now=FROZEN)
    strip = _pointer(hooks, _ctx("sess-1"), config)
    assert isinstance(strip, str)
    lowered = strip.lower()
    assert "propose" in lowered
    assert "bind-action" in lowered or "bind action" in lowered
    assert GATE_PHRASE in strip
    assert len(strip) <= 200


# ---------------------------------------------------------------------------
# US-4 / US-11: no mutation, fail-soft, 200-char cap, truncation
# ---------------------------------------------------------------------------


def test_pointer_strip_never_mutates_kv_on_command_step(temp_project):
    """US-4: pointer injection itself is a pure read. Command-signal steps leave kv byte-identical."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    _seed_facts(config)
    _start(runs, config, session_id="sess-1", now=FROZEN)
    before = copy.deepcopy(_load_runs_doc(config))
    strip = _pointer(hooks, _ctx("sess-1"), config)
    assert isinstance(strip, str)
    after = _load_runs_doc(config)
    assert after == before


def test_pointer_strip_fail_soft_corrupted_runs_doc_returns_none(temp_project):
    """US-11 / existing hook convention: any exception inside the hook → None (wiki_context_injection)."""
    from state_db import mutate_kv

    config, _project, _config_path = temp_project
    hooks = _hooks()
    _seed_facts(config)

    def _corrupt(data):
        data.clear()
        data["runs"] = "<<<not-a-mapping>>>"
        return data

    mutate_kv(STORE_RUNS, _corrupt, config=config)
    assert _pointer(hooks, _ctx("sess-1"), config) is None


def test_pointer_strip_worst_case_names_always_at_most_200_chars(temp_project):
    """US-11: 32-char workflow + 24-char step names still render a strip ≤200 chars."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    workflow = _validated(
        name="w" * 32,
        steps=[
            _step("s" * 24, "N" * 24, {"command": {"pattern": "show overdue invoices"}}),
            _step(
                "t" * 24,
                "M" * 24,
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ],
    )
    _seed_facts(config)
    run = _start(runs, config, workflow=workflow, session_id="sess-1", now=FROZEN)
    runs.advance_run(
        run["workflow_run_id"],
        0,
        {"kind": "command"},
        config=config,
        now=FROZEN + timedelta(minutes=5),
    )
    strip = _pointer(hooks, _ctx("sess-1"), config, now=FROZEN + timedelta(minutes=5))
    assert isinstance(strip, str)
    assert len(strip) <= 200
    assert "2/2" in strip
    assert "HALT" in strip or "DEGRADED" in strip or "GO" in strip or GATE_PHRASE in strip


def test_pointer_strip_truncation_keeps_step_verdict_and_next_action(temp_project):
    """US-11 truncation order: drop last-completed ts, then name; always keep n/N + verdict + next."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    workflow = _validated(
        name="w" * 32,
        steps=[
            _step("done-step-id-xxxxxxxx", "D" * 24, {"command": {"pattern": "show overdue invoices"}}),
            _step(
                "gate-step-id-xxxxxxxx",
                "G" * 24,
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ],
    )
    _seed_facts(config)
    run = _start(runs, config, workflow=workflow, session_id="sess-1", now=FROZEN)
    runs.advance_run(
        run["workflow_run_id"],
        0,
        {"kind": "command"},
        config=config,
        now=FROZEN + timedelta(minutes=5),
    )
    strip = _pointer(hooks, _ctx("sess-1"), config, now=FROZEN + timedelta(minutes=5))
    assert isinstance(strip, str)
    assert len(strip) <= 200
    assert "2/2" in strip
    assert "GO" in strip or "DEGRADED" in strip or "HALT" in strip
    assert GATE_PHRASE in strip or "next:" in strip.lower()


def test_pointer_strip_halt_names_missing_required_capability(temp_project):
    """US-5: required capability missing → strip is HALT and names the capability."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    workflow = _validated(
        steps=[
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ]
    )
    _seed_facts(config, capabilities={"gmail.send": False})
    _start(runs, config, workflow=workflow, session_id="sess-1", now=FROZEN)
    strip = _pointer(hooks, _ctx("sess-1"), config)
    assert isinstance(strip, str)
    assert "HALT" in strip
    assert "gmail.send" in strip
    assert len(strip) <= 200


# ---------------------------------------------------------------------------
# US-5: compute_verdict — pure, now= injected
# ---------------------------------------------------------------------------


def test_compute_verdict_clean_facts_is_go(temp_project):
    """US-5: fresh facts, all capabilities present, state files present → GO."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    facts = _seed_facts(config)
    run = _start(runs, config, session_id="sess-1", now=FROZEN)
    result = hooks.compute_verdict(run, facts, now=FROZEN)
    assert _verdict_token(result) == "GO"


def test_compute_verdict_required_capability_missing_is_halt_and_names_it(temp_project):
    """US-5: required capability missing → HALT and the capability name is in the result."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    workflow = _validated(
        steps=[
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ]
    )
    facts = _seed_facts(config, capabilities={"gmail.send": False})
    run = _start(runs, config, workflow=workflow, session_id="sess-1", now=FROZEN)
    result = hooks.compute_verdict(run, facts, now=FROZEN)
    assert _verdict_token(result) == "HALT"
    assert "gmail.send" in str(result)


def test_compute_verdict_optional_capability_missing_is_degraded_with_skip_list(temp_project):
    """US-5: optional-only capability missing → DEGRADED + skip-list of affected step ids."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    workflow = _validated(
        steps=[
            _step(
                "draft-nudge",
                "Draft nudge",
                {"review_queue": {"action_type": "gmail.draft"}},
                required=False,
            ),
            _step(
                "list-overdue",
                "List overdue",
                {"command": {"pattern": "show overdue invoices"}},
            ),
        ]
    )
    facts = _seed_facts(config, capabilities={"gmail.draft": False, "gmail.send": True})
    run = _start(runs, config, workflow=workflow, session_id="sess-1", now=FROZEN)
    result = hooks.compute_verdict(run, facts, now=FROZEN)
    assert _verdict_token(result) == "DEGRADED"
    if isinstance(result, dict):
        skip = result.get("skip") or result.get("skip_list") or []
        assert "draft-nudge" in list(skip)
    else:
        assert "draft-nudge" in str(result)


def test_compute_verdict_state_file_absent_is_degraded(temp_project):
    """US-5: a declared state file marked absent → DEGRADED."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    facts = _seed_facts(config, state_files={"pipeline.yaml": False, "invoices.yaml": True})
    run = _start(runs, config, session_id="sess-1", now=FROZEN)
    result = hooks.compute_verdict(run, facts, now=FROZEN)
    assert _verdict_token(result) == "DEGRADED"


def test_compute_verdict_stale_facts_is_degraded_never_go(temp_project):
    """US-5: facts older than facts_max_age_hours (default 1h) vs now= → DEGRADED, never GO."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    stale_at = FROZEN - timedelta(hours=2)
    facts = _seed_facts(config, now=FROZEN, refreshed_at=stale_at)
    run = _start(runs, config, session_id="sess-1", now=FROZEN)
    result = hooks.compute_verdict(run, facts, now=FROZEN)
    assert _verdict_token(result) == "DEGRADED"
    assert "GO" != _verdict_token(result)


def test_compute_verdict_missing_facts_doc_is_degraded(temp_project):
    """US-5: facts argument missing/empty (kv doc absent) → DEGRADED, never GO."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    run = _start(runs, config, session_id="sess-1", now=FROZEN)
    for facts in (None, {}):
        result = hooks.compute_verdict(run, facts, now=FROZEN)
        assert _verdict_token(result) == "DEGRADED"


def test_pointer_strip_missing_facts_doc_injects_degraded(temp_project):
    """US-5: no workflow_facts kv doc at all → strip still injects, verdict DEGRADED."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    _start(runs, config, session_id="sess-1", now=FROZEN)
    strip = _pointer(hooks, _ctx("sess-1"), config)
    assert isinstance(strip, str)
    assert "DEGRADED" in strip
    assert "HALT" not in strip
    assert len(strip) <= 200


# ---------------------------------------------------------------------------
# US-8: review_queue re-observation on pre_llm_call + observe_and_advance
# ---------------------------------------------------------------------------


def test_pointer_strip_reobserves_review_queue_and_advances_without_tool_event(temp_project):
    """US-8: record-execution landing out-of-band is caught on the next owning-session pre_llm_call."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    workflow = _validated(
        steps=[
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
            _step("wrap-up", "Wrap up", {"manual": True}),
        ]
    )
    _seed_facts(config)
    run = _start(runs, config, workflow=workflow, session_id="sess-1", now=FROZEN)
    action = _create_action(config)
    action_id = action["id"]
    runs.bind_action(run["workflow_run_id"], "propose-send", action_id, config=config)
    _land_executed(config, action_id, success=True)

    strip = _pointer(hooks, _ctx("sess-1"), config, now=FROZEN + timedelta(minutes=2))
    fetched = runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["step_status"][0] == "completed"
    assert fetched["current_step_index"] == 1
    assert isinstance(strip, str)
    assert "2/2" in strip


def test_observe_and_advance_is_public_and_advances_executed_review_queue_step(temp_project):
    """US-8: observe_and_advance(run_id, config, now=) is the CLI sync path; same re-observation."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    workflow = _validated(
        steps=[
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ]
    )
    _seed_facts(config)
    run = _start(runs, config, workflow=workflow, session_id="sess-1", now=FROZEN)
    action = _create_action(config)
    runs.bind_action(run["workflow_run_id"], "propose-send", action["id"], config=config)
    _land_executed(config, action["id"], success=True)

    hooks.observe_and_advance(
        run["workflow_run_id"],
        config,
        now=FROZEN + timedelta(minutes=3),
    )
    fetched = runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["step_status"][0] == "completed"
    assert fetched["state"] == "completed"


# ---------------------------------------------------------------------------
# US-7: advancement hook — command signal
# ---------------------------------------------------------------------------


def test_advancement_command_pattern_exit0_fresh_advances(temp_project):
    """US-7 signal 1: terminal call matching command.pattern, exit 0, event time ≥ started_at → advance."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    _seed_facts(config)
    run = _start(runs, config, session_id="sess-1", now=FROZEN)
    later = FROZEN + timedelta(minutes=2)
    _advance_hook(
        hooks,
        "terminal",
        {"command": "python invoices.py show overdue invoices --json"},
        "ok",
        _ctx("sess-1"),
        config,
        now=later,
        exit_code=0,
    )
    fetched = runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["step_status"][0] == "completed"
    assert fetched["current_step_index"] == 1


def test_advancement_pre_run_event_does_not_advance(temp_project):
    """US-7: delayed/pre-run event (now= < started_at) never advances."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    _seed_facts(config)
    run = _start(runs, config, session_id="sess-1", now=FROZEN)
    _advance_hook(
        hooks,
        "terminal",
        {"command": "show overdue invoices"},
        "ok",
        _ctx("sess-1"),
        config,
        now=FROZEN - timedelta(minutes=5),
        exit_code=0,
    )
    fetched = runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["current_step_index"] == 0
    assert fetched["step_status"][0] == "pending"


def test_advancement_exit_nonzero_does_not_advance(temp_project):
    """US-7: matching pattern with exit 1 never advances."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    _seed_facts(config)
    run = _start(runs, config, session_id="sess-1", now=FROZEN)
    _advance_hook(
        hooks,
        "terminal",
        {"command": "show overdue invoices"},
        "failed",
        _ctx("sess-1"),
        config,
        now=FROZEN + timedelta(minutes=1),
        exit_code=1,
    )
    fetched = runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["current_step_index"] == 0
    assert fetched["step_status"][0] == "pending"


def test_advancement_wrong_pattern_does_not_advance(temp_project):
    """US-7: terminal command that does not match the step pattern never advances."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    _seed_facts(config)
    run = _start(runs, config, session_id="sess-1", now=FROZEN)
    _advance_hook(
        hooks,
        "terminal",
        {"command": "ls -la /tmp"},
        "ok",
        _ctx("sess-1"),
        config,
        now=FROZEN + timedelta(minutes=1),
        exit_code=0,
    )
    fetched = runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["current_step_index"] == 0


def test_advancement_duplicate_completed_step_is_noop(temp_project):
    """US-7: re-reporting an already-completed step is a no-op (batch 2 advance_run idempotency)."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    _seed_facts(config)
    run = _start(runs, config, session_id="sess-1", now=FROZEN)
    t1 = FROZEN + timedelta(minutes=1)
    t2 = FROZEN + timedelta(minutes=4)
    args = {"command": "show overdue invoices"}
    _advance_hook(hooks, "terminal", args, "ok", _ctx("sess-1"), config, now=t1, exit_code=0)
    mid = copy.deepcopy(runs.get_run(run["workflow_run_id"], config=config))
    _advance_hook(hooks, "terminal", args, "ok", _ctx("sess-1"), config, now=t2, exit_code=0)
    after = runs.get_run(run["workflow_run_id"], config=config)
    assert after["current_step_index"] == mid["current_step_index"] == 1
    assert after["step_status"] == mid["step_status"]


def test_advancement_unrelated_session_does_not_advance(temp_project):
    """US-4/US-7: an unrelated session's tool event must not move another run's pointer."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    _seed_facts(config)
    run = _start(runs, config, session_id="sess-owner", now=FROZEN)
    _advance_hook(
        hooks,
        "terminal",
        {"command": "show overdue invoices"},
        "ok",
        _ctx("sess-intruder"),
        config,
        now=FROZEN + timedelta(minutes=1),
        exit_code=0,
    )
    fetched = runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["current_step_index"] == 0


def test_advancement_same_event_never_advances_two_steps(temp_project):
    """US-7: one completion event completes at most the current step, even if the next shares the pattern."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    workflow = _validated(
        steps=[
            _step("first", "First", {"command": {"pattern": "do-work"}}),
            _step("second", "Second", {"command": {"pattern": "do-work"}}),
        ]
    )
    _seed_facts(config)
    run = _start(runs, config, workflow=workflow, session_id="sess-1", now=FROZEN)
    _advance_hook(
        hooks,
        "terminal",
        {"command": "do-work --now"},
        "ok",
        _ctx("sess-1"),
        config,
        now=FROZEN + timedelta(minutes=1),
        exit_code=0,
    )
    fetched = runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["step_status"][0] == "completed"
    assert fetched["current_step_index"] == 1
    assert fetched["step_status"][1] == "pending"


# ---------------------------------------------------------------------------
# US-7: advancement hook — file signal (mtime via os.utime, no wall-clock)
# ---------------------------------------------------------------------------


def test_advancement_file_signal_fresh_mtime_advances(temp_project):
    """US-7 signal 2: declared file exists with mtime ≥ started_at after a write_file event → advance."""
    config, project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    workflow = _validated(
        steps=[
            _step("draft-nudge", "Draft nudge", {"file": {"path": "out/nudge.txt"}}),
        ]
    )
    _seed_facts(config)
    run = _start(runs, config, workflow=workflow, session_id="sess-1", now=FROZEN)
    later = FROZEN + timedelta(minutes=3)
    _touch_file(project, "out/nudge.txt", later)
    _advance_hook(
        hooks,
        "write_file",
        {"path": "out/nudge.txt"},
        "ok",
        _ctx("sess-1"),
        config,
        now=later,
    )
    fetched = runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["step_status"][0] == "completed"
    assert fetched["state"] == "completed"


def test_advancement_file_stale_mtime_does_not_advance(temp_project):
    """US-7: file whose mtime is before started_at is not fresh evidence."""
    config, project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    workflow = _validated(
        steps=[
            _step("draft-nudge", "Draft nudge", {"file": {"path": "out/nudge.txt"}}),
        ]
    )
    _seed_facts(config)
    _touch_file(project, "out/nudge.txt", FROZEN - timedelta(hours=1))
    run = _start(runs, config, workflow=workflow, session_id="sess-1", now=FROZEN)
    _advance_hook(
        hooks,
        "write_file",
        {"path": "out/nudge.txt"},
        "ok",
        _ctx("sess-1"),
        config,
        now=FROZEN + timedelta(minutes=1),
    )
    fetched = runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["current_step_index"] == 0
    assert fetched["step_status"][0] == "pending"


# ---------------------------------------------------------------------------
# US-7 / US-8: review_queue signal — proposed/approved/unbound do not advance
# ---------------------------------------------------------------------------


def test_advancement_review_queue_proposed_approved_unbound_do_not_advance(temp_project):
    """US-8: unbound, requested, and approved-but-not-executed never complete a review_queue step."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    workflow = _validated(
        steps=[
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ]
    )
    _seed_facts(config)
    later = FROZEN + timedelta(minutes=1)

    unbound = _start(runs, config, workflow=workflow, session_id="sess-1", now=FROZEN)
    hooks.observe_and_advance(unbound["workflow_run_id"], config, now=later)
    assert runs.get_run(unbound["workflow_run_id"], config=config)["current_step_index"] == 0
    runs.abort_run(unbound["workflow_run_id"], config=config, now=later)

    parked = _start(
        runs,
        config,
        workflow=workflow,
        session_id="sess-2",
        now=FROZEN + timedelta(minutes=2),
    )
    action = _create_action(config)
    runs.bind_action(parked["workflow_run_id"], "propose-send", action["id"], config=config)
    hooks.observe_and_advance(parked["workflow_run_id"], config, now=FROZEN + timedelta(minutes=3))
    assert runs.get_run(parked["workflow_run_id"], config=config)["step_status"][0] == "awaiting-approval"

    from state_db import approve_pending_action

    approve_pending_action(config, action["id"], approver="MH", reason="Reviewed")
    hooks.observe_and_advance(parked["workflow_run_id"], config, now=FROZEN + timedelta(minutes=4))
    fetched = runs.get_run(parked["workflow_run_id"], config=config)
    assert fetched["step_status"][0] == "awaiting-approval"
    assert fetched["state"] == "awaiting-approval"


# ---------------------------------------------------------------------------
# US-7: manual steps are never auto-advanced
# ---------------------------------------------------------------------------


def test_advancement_manual_step_ignores_matching_looking_event(temp_project):
    """US-7: manual steps have no observable signal; post_tool_call must not advance them."""
    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    workflow = _validated(
        steps=[
            _step("talk", "Talk it through", {"manual": True}),
        ]
    )
    _seed_facts(config)
    run = _start(runs, config, workflow=workflow, session_id="sess-1", now=FROZEN)
    _advance_hook(
        hooks,
        "terminal",
        {"command": "Talk it through"},
        "ok",
        _ctx("sess-1"),
        config,
        now=FROZEN + timedelta(minutes=1),
        exit_code=0,
    )
    fetched = runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["current_step_index"] == 0
    assert fetched["step_status"][0] == "pending"
    assert fetched["state"] == "running"


# ---------------------------------------------------------------------------
# US-7: bounded write — at most one mutate_kv; drop-don't-block on lock
# ---------------------------------------------------------------------------


def test_advancement_at_most_one_mutate_kv_write_per_event(temp_project, monkeypatch):
    """US-7: a matching command event performs at most one mutate_kv write on workflow_runs."""
    from state_db import StateDB

    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    _seed_facts(config)
    _start(runs, config, session_id="sess-1", now=FROZEN)

    orig = StateDB.mutate_kv
    calls: list[str] = []

    def spy(self, store_name, mutate_fn, **kwargs):
        calls.append(store_name)
        return orig(self, store_name, mutate_fn, **kwargs)

    monkeypatch.setattr(StateDB, "mutate_kv", spy)
    _advance_hook(
        hooks,
        "terminal",
        {"command": "show overdue invoices"},
        "ok",
        _ctx("sess-1"),
        config,
        now=FROZEN + timedelta(minutes=1),
        exit_code=0,
    )
    run_writes = [name for name in calls if name == STORE_RUNS]
    assert len(run_writes) <= 1
    assert run_writes, "successful command completion must write the run store once"


def test_advancement_lock_contention_drops_and_does_not_raise(temp_project, monkeypatch):
    """US-7: lock contention drops the advance (fail-soft None) rather than blocking the tool call.

    Seam: monkeypatch StateDB.mutate_kv to raise sqlite3.OperationalError('database is locked'),
    mirroring existing ConcurrencyError/lock tests. mutate_kv has no busy_timeout kwarg today,
    so we assert drop-don't-block behaviour rather than inventing a timeout parameter.
    """
    from state_db import StateDB

    config, _project, _config_path = temp_project
    hooks = _hooks()
    runs = _runs()
    _seed_facts(config)
    run = _start(runs, config, session_id="sess-1", now=FROZEN)

    def boom(self, store_name, mutate_fn, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(StateDB, "mutate_kv", boom)
    result = _advance_hook(
        hooks,
        "terminal",
        {"command": "show overdue invoices"},
        "ok",
        _ctx("sess-1"),
        config,
        now=FROZEN + timedelta(minutes=1),
        exit_code=0,
    )
    assert result is None
    fetched = runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["current_step_index"] == 0
    assert fetched["step_status"][0] == "pending"


def test_advancement_fail_soft_on_internal_error_returns_none(temp_project):
    """Existing hook convention: advancement swallows exceptions and returns None."""
    from state_db import mutate_kv

    config, _project, _config_path = temp_project
    hooks = _hooks()

    def _corrupt(data):
        data.clear()
        data["runs"] = {"broken": ["not", "a", "run"]}
        return data

    mutate_kv(STORE_RUNS, _corrupt, config=config)
    result = _advance_hook(
        hooks,
        "terminal",
        {"command": "show overdue invoices"},
        "ok",
        _ctx("sess-1"),
        config,
        now=FROZEN + timedelta(minutes=1),
        exit_code=0,
    )
    assert result is None
