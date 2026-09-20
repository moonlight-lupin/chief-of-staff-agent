#!/usr/bin/env python3
"""RED-phase tests: Workflow Orchestrator Batch 4 (cron lifecycle + doctor checks).

Plan (spec v3.1 US-9/10/12; Implementation 142-161; Testing 154-161):
- Target: shared/scripts/workflow_cron.py
- Part 1: cron lifecycle -- install/uninstall/occurrence fire (US-9)
- Part 2: doctor workflow checks -- extend the existing doctor check registry (US-12)

Mirrors batch 2/3 fixtures (temp_project, FROZEN, now= injection) and the
existing cron prior art (shared/scripts/install_cron.py: build job dict ->
shell out to ``hermes cron create`` via subprocess) and the existing doctor
pattern (doctor_base.CheckResult dataclass + check signature
``(fix, data, config_path) -> CheckResult``).

Every test is expected to FAIL until the GREEN-phase implementation lands.
The target module ``shared/scripts/workflow_cron.py`` does not exist yet.

Do NOT assert ALL_HOOKS registration, CLI wiring, or chief_of_staff.py
integration (later batch). Tests only -- no implementation.
"""
from __future__ import annotations

import re
import subprocess
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
STORE_CRONS = "workflow_crons"
STORE_RUNS = "workflow_runs"
ACTIVE_STATES = frozenset({"running", "awaiting-approval"})
DEFAULT_STALE_RUN_HOURS = 24
OCCURRENCES_BOUND = 20
NO_PROGRESS_THRESHOLD = 3


def _cron():
    """Import the workflow_cron module or fail with an explicit RED message."""
    try:
        import workflow_cron
    except ImportError as e:
        pytest.fail(f"RED: shared/scripts/workflow_cron.py not implemented yet ({e})")
    return workflow_cron


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


def _raw_workflow(name="invoice-chase", steps=None, cron="*/5 * * * *", timezone_name="UTC"):
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
        "triggers": {"schedule": {"cron": cron, "timezone": timezone_name}},
        "steps": steps,
    }


def _validated(name="invoice-chase", steps=None, cron="*/5 * * * *", timezone_name="UTC"):
    return validate_workflow(
        _raw_workflow(name=name, steps=steps, cron=cron, timezone_name=timezone_name)
    )


def _as_dt(value):
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@pytest.fixture
def temp_project(tmp_path, monkeypatch):
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
    monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))
    return config, project, config_path


def _install(mod, config, *, name="invoice-chase", workflow=None, session_id="sess-1", now=FROZEN, cron=None):
    wf = workflow
    if wf is None:
        kwargs = {"name": name}
        if cron is not None:
            kwargs["cron"] = cron
        wf = _validated(**kwargs)
    return mod.install_workflow_cron(name, wf, config, now=now, session_id=session_id)


def _fake_subprocess_ok(monkeypatch, captured=None):
    """Mirror install_cron.py: monkeypatch subprocess.run to capture + succeed."""

    class _FakeProc:
        stdout = "ok"
        stderr = ""
        returncode = 0

    def _fake_run(argv, capture_output=True, text=True, timeout=60, check=False, **kwargs):
        if captured is not None:
            captured.append(list(argv))
        return _FakeProc()

    monkeypatch.setattr(subprocess, "run", _fake_run)


def _load_crons_doc(config):
    from state_db import load_store

    return load_store(STORE_CRONS, config=config, validate=False)


def _binding(config, name="invoice-chase"):
    doc = _load_crons_doc(config)
    bindings = doc.get("bindings", {}) if isinstance(doc, dict) else {}
    return bindings.get(name, {})


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


def _minute(base, minutes):
    """Return base + minutes as a tz-aware datetime (minute-precision for cron tests)."""
    return base + timedelta(minutes=minutes)


# ===========================================================================
# Part 1: Cron lifecycle -- install (US-9)
# ===========================================================================


def test_install_workflow_cron_validates_via_batch1_validator(temp_project):
    """US-9: install_workflow_cron validates the workflow via the batch 1 validator."""
    config, _project, _config_path = temp_project
    mod = _cron()
    raw = _raw_workflow()
    del raw["name"]
    with pytest.raises((mod.WorkflowRunError, Exception)):
        mod.install_workflow_cron(
            "invoice-chase", raw, config, now=FROZEN, session_id="sess-1"
        )


def test_install_workflow_cron_registers_via_hermes_cron_create_subprocess(
    temp_project, monkeypatch
):
    """US-9: registration mirrors install_cron.py -- shell out to ``hermes cron create``."""
    config, _project, _config_path = temp_project
    mod = _cron()
    captured: list[list[str]] = []
    _fake_subprocess_ok(monkeypatch, captured)
    mod.install_workflow_cron(
        "invoice-chase", _validated(), config, now=FROZEN, session_id="sess-1"
    )
    assert captured, "install must shell out to hermes cron"
    cmd = captured[0]
    assert cmd[0] == "hermes"
    assert "cron" in cmd
    assert "*/5 * * * *" in cmd or any("*/5" in part for part in cmd)


def test_install_workflow_cron_schedule_id_is_deterministic_from_workflow_name(
    temp_project, monkeypatch
):
    """US-9: same workflow name -> same schedule_id (idempotent reinstall = update-in-place)."""
    config, _project, _config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    first = mod.install_workflow_cron(
        "invoice-chase", _validated(), config, now=FROZEN, session_id="sess-1"
    )
    second = mod.install_workflow_cron(
        "invoice-chase",
        _validated(),
        config,
        now=FROZEN + timedelta(minutes=1),
        session_id="sess-1",
    )
    assert first["schedule_id"] == second["schedule_id"]


def test_install_workflow_cron_schedule_id_le_32_chars_and_filesystem_safe(
    temp_project, monkeypatch
):
    """US-9: schedule_id is <=32 chars and filesystem-safe kebab-case."""
    config, _project, _config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    binding = mod.install_workflow_cron(
        "invoice-chase", _validated(), config, now=FROZEN, session_id="sess-1"
    )
    sid = binding["schedule_id"]
    assert isinstance(sid, str)
    assert len(sid) <= 32
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]*[a-z0-9]|[a-z0-9]", sid), (
        f"schedule_id {sid!r} must be filesystem-safe kebab-case"
    )


def test_install_workflow_cron_stores_binding_in_workflow_crons_kv_doc(
    temp_project, monkeypatch
):
    """US-9: binding is stored in the ``workflow_crons`` kv doc (one doc, all workflows)."""
    config, _project, _config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    mod.install_workflow_cron(
        "invoice-chase", _validated(), config, now=FROZEN, session_id="sess-1"
    )
    doc = _load_crons_doc(config)
    assert isinstance(doc, dict)
    bindings = doc.get("bindings") if isinstance(doc, dict) else None
    assert isinstance(bindings, dict)
    assert "invoice-chase" in bindings


def test_install_workflow_cron_binding_has_required_fields_and_defaults(
    temp_project, monkeypatch
):
    """US-9: binding carries workflow name, cron expr, owning session, created_at,
    last_occurrence_at=None, missed/parked counters default 0."""
    config, _project, _config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    binding = mod.install_workflow_cron(
        "invoice-chase",
        _validated(cron="*/5 * * * *"),
        config,
        now=FROZEN,
        session_id="sess-owner",
    )
    assert binding["workflow_name"] == "invoice-chase"
    assert binding["cron"] == "*/5 * * * *"
    assert binding["session_id"] == "sess-owner"
    assert _as_dt(binding["created_at"]) == FROZEN
    assert binding.get("last_occurrence_at") is None
    assert binding.get("missed_count", 0) == 0
    assert binding.get("parked_count", 0) == 0


def test_install_workflow_cron_idempotent_reinstall_returns_same_id_and_preserves_state(
    temp_project, monkeypatch
):
    """US-9: reinstall = update-in-place, same schedule_id; preserves last_occurrence_at
    and missed/parked counters (reinstall resets only schedule fields it owns:
    cron expr, owning session, created_at stays)."""
    config, _project, _config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    first = mod.install_workflow_cron(
        "invoice-chase",
        _validated(cron="*/5 * * * *"),
        config,
        now=FROZEN,
        session_id="sess-1",
    )
    from state_db import mutate_kv

    def _seed_state(data):
        bindings = data.setdefault("bindings", {})
        record = bindings["invoice-chase"]
        record["last_occurrence_at"] = (FROZEN + timedelta(minutes=5)).isoformat()
        record["missed_count"] = 2
        record["parked_count"] = 1
        return record

    mutate_kv(STORE_CRONS, _seed_state, config=config)

    reinstalled = mod.install_workflow_cron(
        "invoice-chase",
        _validated(cron="0 * * * *"),
        config,
        now=FROZEN + timedelta(minutes=10),
        session_id="sess-2",
    )
    assert reinstalled["schedule_id"] == first["schedule_id"]
    assert reinstalled["cron"] == "0 * * * *"
    assert reinstalled["session_id"] == "sess-2"
    assert reinstalled.get("last_occurrence_at") is not None
    assert reinstalled.get("missed_count") == 2
    assert reinstalled.get("parked_count") == 1


def test_install_workflow_cron_writes_through_mutate_kv(temp_project, monkeypatch):
    """US-9: install writes via StateDB.mutate_kv('workflow_crons', ...)."""
    from state_db import StateDB

    config, _project, _config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    calls: list[str] = []
    orig = StateDB.mutate_kv

    def spy(self, store_name, mutate_fn, **kwargs):
        calls.append(store_name)
        return orig(self, store_name, mutate_fn, **kwargs)

    monkeypatch.setattr(StateDB, "mutate_kv", spy)
    mod.install_workflow_cron(
        "invoice-chase", _validated(), config, now=FROZEN, session_id="sess-1"
    )
    assert STORE_CRONS in calls


# ===========================================================================
# Part 1: Cron lifecycle -- uninstall (US-9)
# ===========================================================================


def test_uninstall_workflow_cron_removes_binding(temp_project, monkeypatch):
    """US-9: uninstall_workflow_cron removes the binding from the kv doc."""
    config, _project, _config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    _install(mod, config)
    mod.uninstall_workflow_cron("invoice-chase", config)
    doc = _load_crons_doc(config)
    bindings = doc.get("bindings", {}) if isinstance(doc, dict) else {}
    assert "invoice-chase" not in bindings


def test_uninstall_workflow_cron_calls_hermes_cron_remove(temp_project, monkeypatch):
    """US-9: uninstall shells out to ``hermes cron`` remove/delete (mirror install_cron.py)."""
    config, _project, _config_path = temp_project
    mod = _cron()
    captured: list[list[str]] = []
    _fake_subprocess_ok(monkeypatch, captured)
    binding = _install(mod, config)
    captured.clear()
    mod.uninstall_workflow_cron("invoice-chase", config)
    assert captured, "uninstall must shell out to hermes cron"
    cmd = captured[0]
    assert cmd[0] == "hermes"
    assert "cron" in cmd
    assert binding["schedule_id"] in cmd or "invoice-chase" in cmd


def test_uninstall_workflow_cron_nonexistent_raises_workflow_run_error(
    temp_project, monkeypatch
):
    """US-9: uninstall of a nonexistent binding -> WorkflowRunError."""
    config, _project, _config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    with pytest.raises(mod.WorkflowRunError):
        mod.uninstall_workflow_cron("never-installed", config)


def test_uninstall_workflow_cron_double_uninstall_raises_second_time(
    temp_project, monkeypatch
):
    """US-9: idempotent double-uninstall -> error the second time (binding is gone)."""
    config, _project, _config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    _install(mod, config)
    mod.uninstall_workflow_cron("invoice-chase", config)
    with pytest.raises(mod.WorkflowRunError):
        mod.uninstall_workflow_cron("invoice-chase", config)


# ===========================================================================
# Part 1: Cron lifecycle -- occurrence fire / tick handler (US-9)
# ===========================================================================


def test_fire_occurrence_not_due_does_not_fire(temp_project, monkeypatch):
    """US-9: tick handler only fires when due per cron expr (not due -> no occurrence)."""
    config, _project, _config_path = temp_project
    mod = _cron()
    runs = _runs()
    _fake_subprocess_ok(monkeypatch)
    _install(mod, config, cron="*/5 * * * *")
    result = mod.fire_occurrence("invoice-chase", config, now=_minute(FROZEN, 1))
    assert result is None
    binding = _binding(config)
    assert binding.get("last_occurrence_at") is None
    assert runs.list_runs(config=config) == []


def test_fire_occurrence_due_starts_new_run_wakeup(temp_project, monkeypatch):
    """US-9: no active run + due occurrence -> start_run (wakeup), trigger_source=cron."""
    config, _project, _config_path = temp_project
    mod = _cron()
    runs = _runs()
    _fake_subprocess_ok(monkeypatch)
    _install(mod, config, cron="*/5 * * * *")
    fire_at = _minute(FROZEN, 0)  # minute 0 is a */5 boundary
    result = mod.fire_occurrence("invoice-chase", config, now=fire_at)
    assert result is not None
    active = [r for r in runs.list_runs(config=config) if r["state"] in ACTIVE_STATES]
    assert len(active) == 1
    assert active[0]["trigger_source"] == "cron"
    binding = _binding(config)
    assert _as_dt(binding["last_occurrence_at"]) == fire_at


def test_fire_occurrence_dedup_same_minute_fires_once(temp_project, monkeypatch):
    """US-9: dedup via last_occurrence_at -- same minute fires once even if hook runs twice."""
    config, _project, _config_path = temp_project
    mod = _cron()
    runs = _runs()
    _fake_subprocess_ok(monkeypatch)
    _install(mod, config, cron="*/5 * * * *")
    fire_at = _minute(FROZEN, 0)
    first = mod.fire_occurrence("invoice-chase", config, now=fire_at)
    second = mod.fire_occurrence("invoice-chase", config, now=fire_at)
    assert first is not None
    assert second is None
    active = [r for r in runs.list_runs(config=config) if r["state"] in ACTIVE_STATES]
    assert len(active) == 1
    binding = _binding(config)
    assert _as_dt(binding["last_occurrence_at"]) == fire_at


def test_fire_occurrence_active_run_with_progress_records_occurrence_no_new_run(
    temp_project, monkeypatch
):
    """US-9: active run that advanced since last occurrence -> record occurrence, no new run."""
    config, _project, _config_path = temp_project
    mod = _cron()
    runs = _runs()
    _fake_subprocess_ok(monkeypatch)
    _install(mod, config, cron="*/5 * * * *")
    t0 = _minute(FROZEN, 0)
    mod.fire_occurrence("invoice-chase", config, now=t0)
    first_active = [r for r in runs.list_runs(config=config) if r["state"] in ACTIVE_STATES]
    assert len(first_active) == 1
    run_id = first_active[0]["workflow_run_id"]
    # Advance the run (progress) before the next occurrence.
    runs.advance_run(run_id, 0, {"kind": "command"}, config=config, now=_minute(FROZEN, 2))
    t5 = _minute(FROZEN, 5)
    result = mod.fire_occurrence("invoice-chase", config, now=t5)
    assert result is not None
    active = [r for r in runs.list_runs(config=config) if r["state"] in ACTIVE_STATES]
    assert len(active) == 1
    assert active[0]["workflow_run_id"] == run_id
    binding = _binding(config)
    assert _as_dt(binding["last_occurrence_at"]) == t5


def test_fire_occurrence_active_run_no_progress_3_consecutive_creates_stalled_action(
    temp_project, monkeypatch
):
    """US-9: active run with NO progress for 3 consecutive occurrences -> create
    review_queue 'workflow-stalled' action naming run_id + counts. NO auto-abort."""
    from state_db import list_pending_actions

    config, _project, _config_path = temp_project
    mod = _cron()
    runs = _runs()
    _fake_subprocess_ok(monkeypatch)
    _install(mod, config, cron="*/5 * * * *")
    t0 = _minute(FROZEN, 0)
    mod.fire_occurrence("invoice-chase", config, now=t0)
    run = [r for r in runs.list_runs(config=config) if r["state"] in ACTIVE_STATES][0]
    run_id = run["workflow_run_id"]
    # 3 consecutive occurrences with no progress (no advance_run between them).
    for minute in (5, 10, 15):
        mod.fire_occurrence("invoice-chase", config, now=_minute(FROZEN, minute))
    actions = list_pending_actions(config=config)
    stalled = [a for a in actions if a.get("type") == "workflow-stalled"]
    assert len(stalled) >= 1
    detail = str(stalled[0].get("summary", "")) + str(stalled[0].get("payload", ""))
    assert run_id in detail
    # NO auto-abort: the run is still active.
    fetched = runs.get_run(run_id, config=config)
    assert fetched["state"] in ACTIVE_STATES


def test_fire_occurrence_parked_run_records_occurrence_no_second_run(
    temp_project, monkeypatch
):
    """US-9: a run parked at awaiting-approval does NOT block occurrence recording,
    but does NOT start a second concurrent run (batch 2 dedup)."""
    config, _project, _config_path = temp_project
    mod = _cron()
    runs = _runs()
    _fake_subprocess_ok(monkeypatch)
    workflow = _validated(
        steps=[
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ]
    )
    _install(mod, config, workflow=workflow, cron="*/5 * * * *")
    t0 = _minute(FROZEN, 0)
    mod.fire_occurrence("invoice-chase", config, now=t0)
    run = [r for r in runs.list_runs(config=config) if r["state"] in ACTIVE_STATES][0]
    run_id = run["workflow_run_id"]
    action = _create_action(config)
    runs.bind_action(run_id, "propose-send", action["id"], config=config)
    assert runs.get_run(run_id, config=config)["state"] == "awaiting-approval"
    t5 = _minute(FROZEN, 5)
    result = mod.fire_occurrence("invoice-chase", config, now=t5)
    assert result is not None
    active = [r for r in runs.list_runs(config=config) if r["state"] in ACTIVE_STATES]
    assert len(active) == 1
    assert active[0]["workflow_run_id"] == run_id
    binding = _binding(config)
    assert _as_dt(binding["last_occurrence_at"]) == t5


def test_fire_occurrence_parked_run_exactly_once_wakeup_note(temp_project, monkeypatch):
    """US-9: N occurrences while parked -> 1 pending wakeup note max, tracked in cron doc."""
    config, _project, _config_path = temp_project
    mod = _cron()
    runs = _runs()
    _fake_subprocess_ok(monkeypatch)
    workflow = _validated(
        steps=[
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ]
    )
    _install(mod, config, workflow=workflow, cron="*/5 * * * *")
    t0 = _minute(FROZEN, 0)
    mod.fire_occurrence("invoice-chase", config, now=t0)
    run = [r for r in runs.list_runs(config=config) if r["state"] in ACTIVE_STATES][0]
    runs.bind_action(run["workflow_run_id"], "propose-send", _create_action(config)["id"], config=config)
    # Fire 3 more occurrences while parked.
    for minute in (5, 10, 15):
        mod.fire_occurrence("invoice-chase", config, now=_minute(FROZEN, minute))
    binding = _binding(config)
    parked_count = binding.get("parked_count", 0)
    assert parked_count >= 1
    # US-9 (review round B): wakeup notes are per-occurrence deduped, not
    # globally capped at 1 — each distinct occurrence while parked reports.
    wakeup_notes = binding.get("wakeup_notes", [])
    assert isinstance(wakeup_notes, list)
    assert len(wakeup_notes) >= 1


def test_fire_occurrence_records_occurrences_bounded_last_20(temp_project, monkeypatch):
    """US-9: cron doc gains occurrences list (bounded -- keep last 20)."""
    config, _project, _config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    _install(mod, config, cron="* * * * *")  # every minute
    for minute in range(25):
        mod.fire_occurrence("invoice-chase", config, now=_minute(FROZEN, minute))
    binding = _binding(config)
    occurrences = binding.get("occurrences", [])
    assert isinstance(occurrences, list)
    assert len(occurrences) <= OCCURRENCES_BOUND


def test_fire_occurrence_nonexistent_binding_returns_none(temp_project, monkeypatch):
    """US-9: firing an occurrence for a workflow with no cron binding -> None (no error)."""
    config, _project, _config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    result = mod.fire_occurrence("never-installed", config, now=FROZEN)
    assert result is None


# ===========================================================================
# Part 1: Cron lifecycle -- list / get bindings (US-11 visibility)
# ===========================================================================


def test_list_cron_bindings_returns_all_bindings(temp_project, monkeypatch):
    """US-11: list_cron_bindings returns all installed bindings."""
    config, _project, _config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    _install(mod, config, name="invoice-chase")
    _install(mod, config, name="weekly-review")
    bindings = mod.list_cron_bindings(config=config)
    names = {b["workflow_name"] for b in bindings}
    assert names == {"invoice-chase", "weekly-review"}


def test_get_cron_binding_returns_binding_or_none(temp_project, monkeypatch):
    """US-11: get_cron_binding returns the binding dict, or None if not installed."""
    config, _project, _config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    _install(mod, config)
    binding = mod.get_cron_binding("invoice-chase", config=config)
    assert binding is not None
    assert binding["workflow_name"] == "invoice-chase"
    assert mod.get_cron_binding("never-installed", config=config) is None


# ===========================================================================
# Part 2: Doctor workflow checks (US-12) -- extend the existing doctor pattern
# ===========================================================================
# Mirror doctor_base.CheckResult dataclass + check signature
# ``(fix: bool, data: dict | None, config_path: Path) -> CheckResult``.
# Tests assert the check functions directly; registration in CHECKS is a later batch.


def _check_result_class():
    from doctor_base import CheckResult

    return CheckResult


def test_check_cron_skill_files_missing_skill_warns_naming_id(temp_project, monkeypatch):
    """US-12: cron binding without matching skill file installed -> warn naming the id."""
    config, _project, _config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    _install(mod, config, name="invoice-chase")
    # No skills.local/invoice-chase/SKILL.md exists.
    result = mod.check_cron_skill_files(False, config, _config_path)
    CheckResult = _check_result_class()
    assert isinstance(result, CheckResult)
    assert result.status == "warn"
    assert "invoice-chase" in result.detail


def test_check_cron_skill_files_present_skill_passes(temp_project, monkeypatch):
    """US-12: cron binding with a matching skill file installed -> pass."""
    config, project, config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    _install(mod, config, name="invoice-chase")
    skill_dir = PLUGIN_ROOT / "skills.local" / "invoice-chase"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: invoice-chase\ndescription: ok\n---\n# Invoice Chase\n",
        encoding="utf-8",
    )
    try:
        result = mod.check_cron_skill_files(False, config, config_path)
        CheckResult = _check_result_class()
        assert isinstance(result, CheckResult)
        assert result.status == "pass"
    finally:
        if skill_dir.exists():
            import shutil

            shutil.rmtree(skill_dir, ignore_errors=True)


def test_check_cron_skill_files_no_bindings_passes(temp_project, monkeypatch):
    """US-12: no cron bindings installed -> pass (nothing to check)."""
    config, _project, config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    result = mod.check_cron_skill_files(False, config, config_path)
    CheckResult = _check_result_class()
    assert isinstance(result, CheckResult)
    assert result.status == "pass"


def test_check_stale_run_warns_with_run_id_and_last_progress(temp_project, monkeypatch):
    """US-12 (review round B): active run stale per 48h-from-last-progress
    threshold -> warn 'stale run' with run_id + last_progress_at + step."""
    config, _project, config_path = temp_project
    mod = _cron()
    runs = _runs()
    _fake_subprocess_ok(monkeypatch)
    run = runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=_validated(),
        config=config,
        now=FROZEN,
    )
    # Inject now= 49h after started_at (US-12 review round B: threshold is
    # 48h from last_progress_at — started_at-only 24h rule was replaced).
    later = FROZEN + timedelta(hours=49)
    result = mod.check_stale_run(False, config, config_path, now=later)
    CheckResult = _check_result_class()
    assert isinstance(result, CheckResult)
    assert result.status == "warn"
    assert run["workflow_run_id"] in result.detail
    assert "stale" in result.detail.lower()


def test_check_stale_run_fresh_run_passes(temp_project, monkeypatch):
    """US-12 (review round B): active run within the 48h threshold -> pass."""
    config, _project, config_path = temp_project
    mod = _cron()
    runs = _runs()
    _fake_subprocess_ok(monkeypatch)
    runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=_validated(),
        config=config,
        now=FROZEN,
    )
    later = FROZEN + timedelta(hours=1)
    result = mod.check_stale_run(False, config, config_path, now=later)
    CheckResult = _check_result_class()
    assert isinstance(result, CheckResult)
    assert result.status == "pass"


def test_check_stale_run_no_active_runs_passes(temp_project, monkeypatch):
    """US-12: no active runs -> pass."""
    config, _project, config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    result = mod.check_stale_run(False, config, config_path, now=FROZEN)
    CheckResult = _check_result_class()
    assert isinstance(result, CheckResult)
    assert result.status == "pass"


def test_check_unhonored_advancement_warns_when_executed_not_advanced(
    temp_project, monkeypatch
):
    """US-12: run in awaiting-approval with a bound action that is executed+success
    in review_queue but not advanced -> warn 'unhonored advancement'.
    Doctor only REPORTS, does not fix (the reconcile path batch 3's
    observe_and_advance covers -- doctor does not advance)."""
    config, _project, config_path = temp_project
    mod = _cron()
    runs = _runs()
    _fake_subprocess_ok(monkeypatch)
    workflow = _validated(
        steps=[
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ]
    )
    run = runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=workflow,
        config=config,
        now=FROZEN,
    )
    action = _create_action(config)
    runs.bind_action(run["workflow_run_id"], "propose-send", action["id"], config=config)
    _land_executed(config, action["id"], success=True)
    # The run is still awaiting-approval (not advanced).
    assert runs.get_run(run["workflow_run_id"], config=config)["state"] == "awaiting-approval"
    result = mod.check_unhonored_advancement(False, config, config_path)
    CheckResult = _check_result_class()
    assert isinstance(result, CheckResult)
    assert result.status == "warn"
    assert "unhonored" in result.detail.lower() or "advancement" in result.detail.lower()
    # Doctor did NOT advance the run.
    assert runs.get_run(run["workflow_run_id"], config=config)["state"] == "awaiting-approval"


def test_check_unhonored_advancement_passes_when_advanced(temp_project, monkeypatch):
    """US-12: run whose bound action is executed+success AND already advanced -> pass."""
    config, _project, config_path = temp_project
    mod = _cron()
    runs = _runs()
    _fake_subprocess_ok(monkeypatch)
    workflow = _validated(
        steps=[
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ]
    )
    run = runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=workflow,
        config=config,
        now=FROZEN,
    )
    action = _create_action(config)
    runs.bind_action(run["workflow_run_id"], "propose-send", action["id"], config=config)
    _land_executed(config, action["id"], success=True)
    # Advance the run (honored).
    runs.advance_run(
        run["workflow_run_id"],
        0,
        {"kind": "review_queue"},
        config=config,
        now=FROZEN + timedelta(minutes=2),
    )
    result = mod.check_unhonored_advancement(False, config, config_path)
    CheckResult = _check_result_class()
    assert isinstance(result, CheckResult)
    assert result.status == "pass"


def test_check_unhonored_advancement_no_awaiting_approval_runs_passes(temp_project, monkeypatch):
    """US-12: no runs in awaiting-approval -> pass."""
    config, _project, config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    result = mod.check_unhonored_advancement(False, config, config_path)
    CheckResult = _check_result_class()
    assert isinstance(result, CheckResult)
    assert result.status == "pass"


def test_check_workflow_crons_doc_corrupt_returns_corruption_finding_not_raise(
    temp_project, monkeypatch
):
    """US-12: workflow_crons doc corrupt/unreadable -> existing corruption finding
    shape (fail/warn), does not raise."""
    from state_db import mutate_kv

    config, _project, config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)

    def _corrupt(data):
        data.clear()
        data["bindings"] = "<<<not-a-mapping>>>"
        return data

    mutate_kv(STORE_CRONS, _corrupt, config=config)
    # The check must not raise; it returns a CheckResult.
    result = mod.check_workflow_crons_doc(False, config, config_path)
    CheckResult = _check_result_class()
    assert isinstance(result, CheckResult)
    assert result.status in ("fail", "warn")
    assert "corrupt" in result.detail.lower() or "unreadable" in result.detail.lower()


def test_check_workflow_crons_doc_clean_passes(temp_project, monkeypatch):
    """US-12: a clean workflow_crons doc (or none) -> pass."""
    config, _project, config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    result = mod.check_workflow_crons_doc(False, config, config_path)
    CheckResult = _check_result_class()
    assert isinstance(result, CheckResult)
    assert result.status == "pass"


def test_doctor_check_signature_matches_existing_pattern(temp_project, monkeypatch):
    """US-12: each new check follows the existing doctor signature
    ``(fix: bool, data: dict | None, config_path: Path) -> CheckResult``."""
    import inspect

    config, _project, config_path = temp_project
    mod = _cron()
    _fake_subprocess_ok(monkeypatch)
    _install(mod, config)
    for check_name in (
        "check_cron_skill_files",
        "check_stale_run",
        "check_unhonored_advancement",
        "check_workflow_crons_doc",
    ):
        check_fn = getattr(mod, check_name)
        sig = inspect.signature(check_fn)
        params = list(sig.parameters)
        # The existing doctor checks accept (fix, data, config_path); we allow
        # an optional now= kwarg for the staleness check (now= injection convention).
        assert "fix" in params or len(params) >= 1
        assert "config_path" in params or len(params) >= 3
