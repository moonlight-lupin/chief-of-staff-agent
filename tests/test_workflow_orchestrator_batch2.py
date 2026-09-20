#!/usr/bin/env python3
"""RED-phase tests: Workflow Orchestrator Batch 2 (kv run store + lifecycle CLI).

Plan (spec v3.1 US-4/6/7/8/10/11; Testing Decisions 154-161):
- Target: shared/scripts/workflow_runs.py (Python API + review_queue-style CLI).
- Store: one kv document ``workflow_runs`` at ``__root__``; every write via mutate_kv.
- Dedup check-and-insert lives inside the mutate_kv callback (US-6).
- No hooks, cron install, or architect skill (later batches).
Every test is expected to FAIL until the GREEN-phase implementation lands.
"""
from __future__ import annotations

import copy
import io
import json
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
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
ACTIVE_STATES = frozenset({"running", "awaiting-approval"})
STORE_NAME = "workflow_runs"


def _runs():
    """Import the run-store module or fail with an explicit RED message."""
    try:
        import workflow_runs
    except ImportError as e:
        pytest.fail(f"RED: shared/scripts/workflow_runs.py not implemented yet ({e})")
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


def _as_dt(value):
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@pytest.fixture
def temp_project(tmp_path):
    """StateDB + CLI config on a temp project root (review_queue / state_db pattern)."""
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


def _cli(mod, config_path, *argv):
    buf = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        rc = mod._main(["--config", str(config_path), *argv])
    return rc, buf.getvalue(), err.getvalue()


def _definition_without_bindings(definition):
    """Execution-relevant snapshot fields; action_id bindings are bind-time state."""
    copied = copy.deepcopy(definition)
    for step in copied.get("steps") or []:
        step.pop("action_id", None)
    return copied


# ---------------------------------------------------------------------------
# US-4 / US-6: start_run + concurrent-run dedup
# ---------------------------------------------------------------------------


def test_start_run_creates_record_with_snapshot_pending_steps_and_running_state(temp_project):
    """US-4/US-6: start writes the run record, snapshot, pointer 0, pending statuses."""
    config, _project, _config_path = temp_project
    mod = _runs()
    workflow = _validated()
    run = _start(mod, config, workflow=workflow, trigger="message", session_id="sess-alpha", now=FROZEN)

    assert run["workflow_name"] == "invoice-chase"
    assert run["state"] == "running"
    assert run["trigger_source"] == "message"
    assert run["session_id"] == "sess-alpha"
    assert run["current_step_index"] == 0
    assert run["step_status"] == ["pending"] * len(workflow["steps"])
    assert _as_dt(run["started_at"]) == FROZEN
    assert _as_dt(run["last_progress_at"]) == FROZEN
    assert _definition_without_bindings(run["definition"])["name"] == workflow["name"]
    assert [s["id"] for s in run["definition"]["steps"]] == [s["id"] for s in workflow["steps"]]


def test_start_run_id_matches_wf_name_suffix_format(temp_project):
    """US-4: workflow_run_id is wf-<workflow-name>-<unique suffix> (review_queue-style id)."""
    config, _project, _config_path = temp_project
    mod = _runs()
    workflow = _validated(name="invoice-chase")
    run = _start(mod, config, workflow=workflow, now=FROZEN)
    run_id = run["workflow_run_id"]
    assert run_id.startswith("wf-invoice-chase-")
    assert len(run_id) > len("wf-invoice-chase-")


def test_start_run_second_active_rejected_with_pointer_to_existing_id(temp_project):
    """US-6: a second start for the same workflow is refused and names the active id."""
    config, _project, _config_path = temp_project
    mod = _runs()
    first = _start(mod, config, session_id="sess-a", now=FROZEN)
    with pytest.raises(mod.WorkflowRunError) as excinfo:
        _start(mod, config, session_id="sess-b", now=FROZEN + timedelta(minutes=1))
    assert first["workflow_run_id"] in str(excinfo.value)
    listed = [r for r in mod.list_runs(config=config) if r["state"] in ACTIVE_STATES]
    assert [r["workflow_run_id"] for r in listed] == [first["workflow_run_id"]]


def test_concurrent_starts_exactly_one_wins(temp_project):
    """US-6: two start_run threads; check-and-insert inside mutate_kv means one wins."""
    config, _project, _config_path = temp_project
    mod = _runs()
    workflow = _validated()
    results: list[dict] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker(session_id: str) -> None:
        barrier.wait()
        try:
            results.append(
                mod.start_run(
                    workflow["name"],
                    "message",
                    session_id,
                    workflow=workflow,
                    config=config,
                    now=FROZEN,
                )
            )
        except BaseException as exc:  # noqa: BLE001 — collect worker failures
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("sess-t1",)),
        threading.Thread(target=worker, args=("sess-t2",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 1, f"expected one winner, got {results!r} errors={errors!r}"
    assert len(errors) == 1
    assert isinstance(errors[0], mod.WorkflowRunError)
    assert results[0]["workflow_run_id"] in str(errors[0])
    active = [r for r in mod.list_runs(config=config) if r["state"] in ACTIVE_STATES]
    assert len(active) == 1
    assert active[0]["workflow_run_id"] == results[0]["workflow_run_id"]


def test_start_run_allowed_after_prior_run_completed(temp_project):
    """US-6: completed is not active — a new start for the same workflow is allowed."""
    config, _project, _config_path = temp_project
    mod = _runs()
    first = _start(mod, config, session_id="sess-1", now=FROZEN)
    mod.complete_run(first["workflow_run_id"], config=config, now=FROZEN + timedelta(minutes=5))
    second = _start(mod, config, session_id="sess-2", now=FROZEN + timedelta(minutes=6))
    assert second["workflow_run_id"] != first["workflow_run_id"]
    assert second["state"] == "running"
    fetched = mod.get_run(first["workflow_run_id"], config=config)
    assert fetched is not None
    assert fetched["state"] == "completed"


def test_start_run_refused_when_hosted_cloud_session_set(temp_project, monkeypatch):
    """US-6: hosted cloud sessions may view but not start (state does not survive teardown)."""
    config, _project, _config_path = temp_project
    mod = _runs()
    existing = _start(mod, config, now=FROZEN)
    mod.complete_run(
        existing["workflow_run_id"], config=config, now=FROZEN + timedelta(minutes=1)
    )
    monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "remote-session-1")
    viewed = mod.get_run(existing["workflow_run_id"], config=config)
    assert viewed is not None
    assert viewed["workflow_run_id"] == existing["workflow_run_id"]
    with pytest.raises(mod.WorkflowRunError):
        _start(mod, config, session_id="sess-cloud", now=FROZEN + timedelta(minutes=2))


def test_start_run_records_cron_trigger_source(temp_project):
    """US-4: trigger source is message|cron; cron is recorded on the run."""
    config, _project, _config_path = temp_project
    mod = _runs()
    run = _start(mod, config, trigger="cron", session_id="cron-1", now=FROZEN)
    assert run["trigger_source"] == "cron"


# ---------------------------------------------------------------------------
# US-11: get_run / list_runs
# ---------------------------------------------------------------------------


def test_get_run_roundtrip_and_unknown_id_is_none(temp_project):
    """US-11: get_run returns the stored record; unknown ids are None."""
    config, _project, _config_path = temp_project
    mod = _runs()
    created = _start(mod, config, now=FROZEN)
    fetched = mod.get_run(created["workflow_run_id"], config=config)
    assert fetched is not None
    assert fetched["workflow_run_id"] == created["workflow_run_id"]
    assert fetched["workflow_name"] == created["workflow_name"]
    assert mod.get_run("wf-missing-does-not-exist", config=config) is None


def test_list_runs_active_first_then_recent(temp_project):
    """US-11: list_runs returns active runs first, then recent terminal runs."""
    config, _project, _config_path = temp_project
    mod = _runs()
    chase = _validated(name="invoice-chase")
    other = _validated(name="weekly-review")
    older_done = _start(mod, config, workflow=chase, session_id="s0", now=FROZEN)
    mod.complete_run(older_done["workflow_run_id"], config=config, now=FROZEN + timedelta(minutes=1))
    active = _start(
        mod, config, workflow=other, session_id="s1", now=FROZEN + timedelta(minutes=2)
    )
    newer_done = _start(
        mod, config, workflow=chase, session_id="s2", now=FROZEN + timedelta(minutes=3)
    )
    mod.complete_run(newer_done["workflow_run_id"], config=config, now=FROZEN + timedelta(minutes=4))

    listed = mod.list_runs(config=config)
    ids = [r["workflow_run_id"] for r in listed]
    assert ids[0] == active["workflow_run_id"]
    assert listed[0]["state"] in ACTIVE_STATES
    done_ids = [r["workflow_run_id"] for r in listed if r["state"] == "completed"]
    assert done_ids == [newer_done["workflow_run_id"], older_done["workflow_run_id"]]


# ---------------------------------------------------------------------------
# US-7: advance_run — pointer, out-of-order, idempotent duplicate
# ---------------------------------------------------------------------------


def test_advance_run_completes_step_moves_pointer_updates_progress(temp_project):
    """US-7: completing the current step advances the pointer and last_progress_at."""
    config, _project, _config_path = temp_project
    mod = _runs()
    t0 = FROZEN
    t1 = FROZEN + timedelta(minutes=10)
    run = _start(mod, config, now=t0)
    mod.advance_run(
        run["workflow_run_id"],
        0,
        {"kind": "manual", "note": "listed overdue"},
        config=config,
        now=t1,
    )
    updated = mod.get_run(run["workflow_run_id"], config=config)
    assert updated["step_status"][0] == "completed"
    assert updated["current_step_index"] == 1
    assert updated["state"] == "running"
    assert _as_dt(updated["last_progress_at"]) == t1
    assert _as_dt(updated["started_at"]) == t0


def test_advance_run_out_of_order_rejected(temp_project):
    """US-7: evidence for a future step never advances the pointer."""
    config, _project, _config_path = temp_project
    mod = _runs()
    run = _start(mod, config, now=FROZEN)
    with pytest.raises(mod.WorkflowRunError):
        mod.advance_run(
            run["workflow_run_id"],
            2,
            {"kind": "manual"},
            config=config,
            now=FROZEN + timedelta(minutes=1),
        )
    fetched = mod.get_run(run["workflow_run_id"], config=config)
    assert fetched["current_step_index"] == 0
    assert fetched["step_status"][0] == "pending"


def test_advance_run_duplicate_completed_step_is_noop(temp_project):
    """US-7: re-reporting the same completed step is a no-op, not an error."""
    config, _project, _config_path = temp_project
    mod = _runs()
    t1 = FROZEN + timedelta(minutes=5)
    t2 = FROZEN + timedelta(minutes=9)
    run = _start(mod, config, now=FROZEN)
    mod.advance_run(run["workflow_run_id"], 0, {"kind": "manual"}, config=config, now=t1)
    mid = copy.deepcopy(mod.get_run(run["workflow_run_id"], config=config))
    mod.advance_run(run["workflow_run_id"], 0, {"kind": "manual"}, config=config, now=t2)
    after = mod.get_run(run["workflow_run_id"], config=config)
    assert after["current_step_index"] == mid["current_step_index"] == 1
    assert after["step_status"] == mid["step_status"]
    assert _as_dt(after["last_progress_at"]) == _as_dt(mid["last_progress_at"]) == t1


def test_advance_final_step_marks_run_completed(temp_project):
    """US-10: reaching the final step completes the run."""
    config, _project, _config_path = temp_project
    mod = _runs()
    workflow = _validated(
        steps=[
            _step("only", "Only step", {"command": {"pattern": "do it"}}),
        ]
    )
    run = _start(mod, config, workflow=workflow, now=FROZEN)
    mod.advance_run(
        run["workflow_run_id"],
        0,
        {"kind": "command"},
        config=config,
        now=FROZEN + timedelta(minutes=2),
    )
    fetched = mod.get_run(run["workflow_run_id"], config=config)
    assert fetched["step_status"] == ["completed"]
    assert fetched["state"] == "completed"
    assert fetched["current_step_index"] == 0


def test_advance_run_refused_on_terminal_run(temp_project):
    """US-10: advance after complete/abort is refused."""
    config, _project, _config_path = temp_project
    mod = _runs()
    run = _start(mod, config, now=FROZEN)
    mod.complete_run(run["workflow_run_id"], config=config, now=FROZEN + timedelta(minutes=1))
    with pytest.raises(mod.WorkflowRunError):
        mod.advance_run(
            run["workflow_run_id"],
            0,
            {"kind": "manual"},
            config=config,
            now=FROZEN + timedelta(minutes=2),
        )


# ---------------------------------------------------------------------------
# US-5 / US-10: skip_step — optional only
# ---------------------------------------------------------------------------


def test_skip_step_optional_marks_skipped_and_advances(temp_project):
    """US-5/US-10: skip_step is allowed on required:false and moves the pointer."""
    config, _project, _config_path = temp_project
    mod = _runs()
    workflow = _validated(
        steps=[
            _step(
                "nudge",
                "Nudge",
                {"file": {"path": "out/nudge.txt"}},
                required=False,
            ),
            _step("list-overdue", "List overdue", {"command": {"pattern": "show overdue"}}),
        ]
    )
    t1 = FROZEN + timedelta(minutes=3)
    run = _start(mod, config, workflow=workflow, now=FROZEN)
    mod.skip_step(run["workflow_run_id"], 0, "inputs unavailable", config=config, now=t1)
    fetched = mod.get_run(run["workflow_run_id"], config=config)
    assert fetched["step_status"][0] == "skipped"
    assert fetched["current_step_index"] == 1
    assert fetched["state"] == "running"
    assert _as_dt(fetched["last_progress_at"]) == t1


def test_skip_step_required_refused(temp_project):
    """US-5: required steps are never skippable."""
    config, _project, _config_path = temp_project
    mod = _runs()
    run = _start(mod, config, now=FROZEN)
    with pytest.raises(mod.WorkflowRunError):
        mod.skip_step(run["workflow_run_id"], 0, "operator skip", config=config, now=FROZEN)
    fetched = mod.get_run(run["workflow_run_id"], config=config)
    assert fetched["step_status"][0] == "pending"
    assert fetched["current_step_index"] == 0


# ---------------------------------------------------------------------------
# US-8: bind_action — write once, overwrite refused
# ---------------------------------------------------------------------------


def test_bind_action_writes_action_id_and_parks_awaiting_approval(temp_project):
    """US-8: bind_action stores action_id on the snapshot step; parks awaiting-approval."""
    config, _project, _config_path = temp_project
    mod = _runs()
    workflow = _validated(
        steps=[
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ]
    )
    run = _start(mod, config, workflow=workflow, now=FROZEN)
    mod.bind_action(run["workflow_run_id"], "propose-send", "act-pending-1", config=config)
    fetched = mod.get_run(run["workflow_run_id"], config=config)
    bound = next(s for s in fetched["definition"]["steps"] if s["id"] == "propose-send")
    assert bound["action_id"] == "act-pending-1"
    assert fetched["state"] == "awaiting-approval"
    assert fetched["step_status"][0] == "awaiting-approval"


def test_bind_action_refuses_overwrite(temp_project):
    """US-8: a second bind with a different action_id is refused; the first id stays."""
    config, _project, _config_path = temp_project
    mod = _runs()
    workflow = _validated(
        steps=[
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ]
    )
    run = _start(mod, config, workflow=workflow, now=FROZEN)
    mod.bind_action(run["workflow_run_id"], "propose-send", "act-pending-1", config=config)
    with pytest.raises(mod.WorkflowRunError):
        mod.bind_action(run["workflow_run_id"], "propose-send", "act-pending-2", config=config)
    fetched = mod.get_run(run["workflow_run_id"], config=config)
    bound = next(s for s in fetched["definition"]["steps"] if s["id"] == "propose-send")
    assert bound["action_id"] == "act-pending-1"


# ---------------------------------------------------------------------------
# US-10: complete / abort / resume / staleness / retention
# ---------------------------------------------------------------------------


def test_complete_run_is_terminal(temp_project):
    """US-10: complete_run marks completed; abort of a completed run is refused."""
    config, _project, _config_path = temp_project
    mod = _runs()
    t1 = FROZEN + timedelta(minutes=8)
    run = _start(mod, config, now=FROZEN)
    mod.complete_run(run["workflow_run_id"], config=config, now=t1)
    fetched = mod.get_run(run["workflow_run_id"], config=config)
    assert fetched["state"] == "completed"
    assert _as_dt(fetched["last_progress_at"]) == t1
    with pytest.raises(mod.WorkflowRunError):
        mod.abort_run(run["workflow_run_id"], config=config, now=t1 + timedelta(minutes=1))
    assert mod.get_run(run["workflow_run_id"], config=config)["state"] == "completed"


def test_complete_run_retention_keeps_last_10_completed_per_workflow(temp_project):
    """US-10: archive keeps the last 10 completed runs per workflow; others are untouched."""
    config, _project, _config_path = temp_project
    mod = _runs()
    chase = _validated(name="invoice-chase")
    other = _validated(name="weekly-review")
    kept_other = _start(mod, config, workflow=other, session_id="other-1", now=FROZEN)
    mod.complete_run(kept_other["workflow_run_id"], config=config, now=FROZEN + timedelta(minutes=1))

    completed_ids = []
    for index in range(11):
        now = FROZEN + timedelta(hours=index + 1)
        run = _start(mod, config, workflow=chase, session_id=f"sess-{index}", now=now)
        mod.complete_run(
            run["workflow_run_id"],
            config=config,
            now=now + timedelta(minutes=1),
        )
        completed_ids.append(run["workflow_run_id"])

    listed = [
        r
        for r in mod.list_runs(config=config)
        if r["workflow_name"] == "invoice-chase" and r["state"] == "completed"
    ]
    assert len(listed) == 10
    remaining = {r["workflow_run_id"] for r in listed}
    assert completed_ids[0] not in remaining
    assert set(completed_ids[1:]) == remaining
    assert mod.get_run(completed_ids[0], config=config) is None
    other_fetched = mod.get_run(kept_other["workflow_run_id"], config=config)
    assert other_fetched is not None
    assert other_fetched["state"] == "completed"


def test_abort_run_from_running_and_awaiting_approval(temp_project):
    """US-10: abort_run terminals an active run from running or awaiting-approval."""
    config, _project, _config_path = temp_project
    mod = _runs()
    running = _start(mod, config, session_id="sess-run", now=FROZEN)
    mod.abort_run(running["workflow_run_id"], config=config, now=FROZEN + timedelta(minutes=1))
    assert mod.get_run(running["workflow_run_id"], config=config)["state"] == "aborted"

    approval_wf = _validated(
        name="send-chase",
        steps=[
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ],
    )
    parked = _start(
        mod,
        config,
        workflow=approval_wf,
        session_id="sess-park",
        now=FROZEN + timedelta(minutes=2),
    )
    mod.bind_action(parked["workflow_run_id"], "propose-send", "act-1", config=config)
    assert mod.get_run(parked["workflow_run_id"], config=config)["state"] == "awaiting-approval"
    mod.abort_run(parked["workflow_run_id"], config=config, now=FROZEN + timedelta(minutes=3))
    assert mod.get_run(parked["workflow_run_id"], config=config)["state"] == "aborted"


def test_resume_run_rebinds_session_and_returns_to_running(temp_project):
    """US-6/US-10: resume_run rebinds the owning session and restores running from stale."""
    config, _project, _config_path = temp_project
    mod = _runs()
    t0 = FROZEN
    t_stale = FROZEN + timedelta(hours=49)
    run = _start(mod, config, session_id="sess-old", now=t0)
    stale, _step_name, _age = mod.is_stale(mod.get_run(run["workflow_run_id"], config=config), now=t_stale)
    assert stale is True
    mod.resume_run(run["workflow_run_id"], "sess-new", config=config, now=t_stale)
    fetched = mod.get_run(run["workflow_run_id"], config=config)
    assert fetched["session_id"] == "sess-new"
    assert fetched["state"] == "running"
    assert _as_dt(fetched["last_progress_at"]) == t_stale
    stale_after, _, _ = mod.is_stale(fetched, now=t_stale)
    assert stale_after is False


def test_is_stale_fresh_run_is_not_stale(temp_project):
    """US-10: a run whose last_progress_at is inside the 48h window is not stale."""
    config, _project, _config_path = temp_project
    mod = _runs()
    run = _start(mod, config, now=FROZEN)
    stale, step_name, age = mod.is_stale(run, threshold_hours=48, now=FROZEN + timedelta(hours=1))
    assert stale is False
    assert isinstance(step_name, str)
    assert age is not None


def test_is_stale_past_threshold_returns_step_name_and_age(temp_project):
    """US-10: past 48h, is_stale is true and names the current step plus last-event age."""
    config, _project, _config_path = temp_project
    mod = _runs()
    run = _start(mod, config, now=FROZEN)
    later = FROZEN + timedelta(hours=49)
    stale, step_name, age = mod.is_stale(run, threshold_hours=48, now=later)
    assert stale is True
    assert "List overdue" in step_name
    if isinstance(age, timedelta):
        assert age >= timedelta(hours=48)
    else:
        assert float(age) >= 48


def test_advance_abort_resume_never_mutate_definition_snapshot(temp_project):
    """US-4: advance/abort/resume leave the run-definition snapshot byte-equal."""
    config, _project, _config_path = temp_project
    mod = _runs()
    workflow = _validated()
    run = _start(mod, config, workflow=workflow, session_id="sess-snap", now=FROZEN)
    snapshot = copy.deepcopy(run["definition"])

    mod.advance_run(
        run["workflow_run_id"],
        0,
        {"kind": "manual"},
        config=config,
        now=FROZEN + timedelta(minutes=1),
    )
    after_advance = mod.get_run(run["workflow_run_id"], config=config)
    assert after_advance["definition"] == snapshot

    mod.resume_run(
        run["workflow_run_id"],
        "sess-snap-2",
        config=config,
        now=FROZEN + timedelta(minutes=2),
    )
    after_resume = mod.get_run(run["workflow_run_id"], config=config)
    assert after_resume["definition"] == snapshot

    sibling = _start(
        mod,
        config,
        workflow=_validated(name="weekly-review"),
        session_id="sess-abort",
        now=FROZEN + timedelta(minutes=3),
    )
    sibling_snapshot = copy.deepcopy(sibling["definition"])
    mod.abort_run(sibling["workflow_run_id"], config=config, now=FROZEN + timedelta(minutes=4))
    after_abort = mod.get_run(sibling["workflow_run_id"], config=config)
    assert after_abort["definition"] == sibling_snapshot


# ---------------------------------------------------------------------------
# mutate_kv seam
# ---------------------------------------------------------------------------


def test_writes_go_through_mutate_kv_on_workflow_runs_store(temp_project, monkeypatch):
    """US-4: start/advance/abort all write via StateDB.mutate_kv('workflow_runs', ...)."""
    from state_db import StateDB

    config, _project, _config_path = temp_project
    mod = _runs()
    calls: list[str] = []
    orig = StateDB.mutate_kv

    def spy(self, store_name, mutate_fn, **kwargs):
        calls.append(store_name)
        return orig(self, store_name, mutate_fn, **kwargs)

    monkeypatch.setattr(StateDB, "mutate_kv", spy)
    run = _start(mod, config, now=FROZEN)
    mod.advance_run(
        run["workflow_run_id"],
        0,
        {"kind": "manual"},
        config=config,
        now=FROZEN + timedelta(minutes=1),
    )
    sibling = _start(
        mod,
        config,
        workflow=_validated(name="weekly-review"),
        session_id="sess-b",
        now=FROZEN + timedelta(minutes=2),
    )
    mod.abort_run(sibling["workflow_run_id"], config=config, now=FROZEN + timedelta(minutes=3))
    assert calls
    assert all(name == STORE_NAME for name in calls)


# ---------------------------------------------------------------------------
# US-11: lifecycle CLI (JSON default, --summary table) — review_queue _main pattern
# ---------------------------------------------------------------------------


def test_cli_runs_json_default_lists_active(temp_project):
    """US-11: workflow_runs.py runs prints JSON by default with the active run."""
    config, _project, config_path = temp_project
    mod = _runs()
    run = _start(mod, config, now=FROZEN)
    rc, out, _err = _cli(mod, config_path, "runs")
    assert rc == 0
    payload = json.loads(out)
    listed = payload["runs"]
    assert any(item["workflow_run_id"] == run["workflow_run_id"] for item in listed)


def test_cli_runs_summary_is_human_table(temp_project):
    """US-11: --summary is a human table, not JSON."""
    config, _project, config_path = temp_project
    mod = _runs()
    _start(mod, config, now=FROZEN)
    rc, out, _err = _cli(mod, config_path, "runs", "--summary")
    assert rc == 0
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
    assert "invoice-chase" in out


def test_cli_abort_marks_run_aborted(temp_project):
    """US-10/US-11: workflow_runs.py abort --run-id terminals the active run."""
    config, _project, config_path = temp_project
    mod = _runs()
    run = _start(mod, config, now=FROZEN)
    rc, _out, _err = _cli(mod, config_path, "abort", "--run-id", run["workflow_run_id"])
    assert rc == 0
    assert mod.get_run(run["workflow_run_id"], config=config)["state"] == "aborted"


def test_cli_resume_rebinds_session(temp_project):
    """US-6/US-11: workflow_runs.py resume --run-id --session-id rebinds the owner."""
    config, _project, config_path = temp_project
    mod = _runs()
    run = _start(mod, config, session_id="sess-old", now=FROZEN)
    rc, _out, _err = _cli(
        mod,
        config_path,
        "resume",
        "--run-id",
        run["workflow_run_id"],
        "--session-id",
        "sess-new",
    )
    assert rc == 0
    fetched = mod.get_run(run["workflow_run_id"], config=config)
    assert fetched["session_id"] == "sess-new"
    assert fetched["state"] == "running"
