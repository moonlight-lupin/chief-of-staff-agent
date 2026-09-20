#!/usr/bin/env python3
"""Workflow run store and lifecycle CLI.

One kv document ``workflow_runs`` at ``__root__``. Every write goes through
``mutate_kv`` so the active-run check-and-insert is race-free.

Commands:
    python shared/scripts/workflow_runs.py runs
    python shared/scripts/workflow_runs.py runs --summary
    python shared/scripts/workflow_runs.py start --file <workflow.yaml> --session-id <id>
    python shared/scripts/workflow_runs.py abort --run-id <id>
    python shared/scripts/workflow_runs.py resume --run-id <id> --session-id <id>
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from config_loader import load_config  # noqa: E402
from state_db import get_pending_action, load_store, mutate_kv  # noqa: E402
from workflows import validate_workflow  # noqa: E402

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

STORE_NAME = "workflow_runs"
STORE_FACTS = "workflow_facts"
HOSTED_SESSION_ENV = "CLAUDE_CODE_REMOTE_SESSION_ID"
ACTIVE_STATES = frozenset({"running", "awaiting-approval"})
TERMINAL_STATES = frozenset({"completed", "aborted", "failed"})
COMPLETED_RETENTION = 10
DEFAULT_STALE_HOURS = 48
DEFAULT_FACTS_MAX_AGE_HOURS = 1
STATE_FILE_NAMES = ("pipeline.yaml", "invoices.yaml", "expenses.yaml", "todos.yaml")
HOOK_ACTOR = "hook:workflow-orchestrator"
OPERATOR_ACTOR = "operator"

RUN_KEY_ORDER = (
    "workflow_run_id",
    "workflow_name",
    "definition",
    "current_step_index",
    "step_status",
    "trigger_source",
    "started_at",
    "last_progress_at",
    "session_id",
    "state",
)


class WorkflowRunError(Exception):
    """Refused run-store mutation (dedup, out-of-order, skip, bind, terminal, hosted)."""


def _in_hosted_session() -> bool:
    return bool(os.getenv(HOSTED_SESSION_ENV, "").strip())


def _hosted_start_refusal() -> str:
    return (
        "Cannot start a workflow run in a hosted cloud session: "
        f"{HOSTED_SESSION_ENV} is set, so project state does not survive "
        "session teardown. View existing runs with get_run/list_runs; start "
        "runs on a local machine or Remote Control session."
    )


def _require_non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowRunError(f"{field}: must be a non-empty string")
    return value.strip()


def _aware(now: datetime | None) -> datetime:
    dt = now if now is not None else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(now: datetime | None) -> str:
    return _aware(now).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _ordered_run(run: Mapping[str, Any]) -> dict[str, Any]:
    ordered: dict[str, Any] = {}
    for key in RUN_KEY_ORDER:
        if key in run:
            ordered[key] = run[key]
    for key, value in run.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def _copy_run(run: Mapping[str, Any]) -> dict[str, Any]:
    return _ordered_run(copy.deepcopy(dict(run)))


def _runs_map(data: dict[str, Any]) -> dict[str, Any]:
    runs = data.get("runs")
    if not isinstance(runs, dict):
        runs = {}
        data["runs"] = runs
    return runs


def _mutate(
    config: Mapping[str, Any] | None,
    mutate_fn: Callable[[dict[str, Any]], Any],
    *,
    action: str,
    actor: str = OPERATOR_ACTOR,
    workflow_run_id: str | None = None,
    open_db: Any = None,
) -> Any:
    """mutate_kv wrapper that always appends an audit row with workflow_run_id."""
    audit_after: dict[str, Any] = {}

    def _wrapped(data: dict[str, Any]) -> Any:
        result = mutate_fn(data)
        audit_after.clear()
        if isinstance(data, dict):
            audit_after.update(copy.deepcopy(data))
        rid = workflow_run_id
        if not rid and isinstance(result, Mapping):
            rid = result.get("workflow_run_id")
        if rid:
            audit_after["workflow_run_id"] = rid
        return result

    return mutate_kv(
        STORE_NAME,
        _wrapped,
        config=config,
        action=action,
        actor=actor,
        after=audit_after,
        open_db=open_db,
    )


def _load_runs(config: Mapping[str, Any] | None, *, open_db: Any = None) -> dict[str, Any]:
    data = load_store(STORE_NAME, config=config, validate=False, open_db=open_db)
    runs = data.get("runs") if isinstance(data, dict) else None
    if isinstance(runs, dict):
        return runs
    return {}


def _get_existing(runs: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    run = runs.get(run_id)
    if not isinstance(run, dict):
        raise WorkflowRunError(f"unknown workflow_run_id {run_id}")
    return run


def _action_executed_success(
    action_id: str,
    config: Mapping[str, Any] | None,
    *,
    open_db: Any = None,
) -> bool:
    if not action_id:
        return False
    action = get_pending_action(config, action_id, open_db=open_db)
    if not isinstance(action, dict) or action.get("state") != "executed":
        return False
    result = action.get("result")
    if not isinstance(result, Mapping):
        return False
    return bool(result.get("success"))


def _current_step(run: Mapping[str, Any]) -> dict[str, Any] | None:
    steps = _steps(run)
    try:
        index = int(run.get("current_step_index") or 0)
    except (TypeError, ValueError):
        index = 0
    if 0 <= index < len(steps) and isinstance(steps[index], Mapping):
        return dict(steps[index])
    return None


def _signal_key(step: Mapping[str, Any] | None) -> str | None:
    if not isinstance(step, Mapping):
        return None
    for key in ("command", "file", "review_queue", "manual"):
        if key in step:
            return key
    return None


def _require_active(run: Mapping[str, Any], action: str) -> None:
    state = str(run.get("state") or "")
    if state in TERMINAL_STATES:
        raise WorkflowRunError(
            f"cannot {action} run {run.get('workflow_run_id')} in terminal state {state}"
        )


def _active_for_workflow(runs: Mapping[str, Any], workflow_name: str) -> dict[str, Any] | None:
    for run in runs.values():
        if not isinstance(run, dict):
            continue
        if run.get("workflow_name") == workflow_name and run.get("state") in ACTIVE_STATES:
            return run
    return None


def _new_run_id(workflow_name: str, runs: Mapping[str, Any]) -> str:
    prefix = f"wf-{workflow_name}-"
    for _ in range(8):
        candidate = prefix + uuid.uuid4().hex[:12]
        if candidate not in runs:
            return candidate
    raise WorkflowRunError(f"could not allocate a unique workflow_run_id for {workflow_name}")


def _steps(run: Mapping[str, Any]) -> list[Any]:
    definition = run.get("definition")
    if not isinstance(definition, Mapping):
        return []
    steps = definition.get("steps")
    return list(steps) if isinstance(steps, list) else []


def _step_status_list(run: dict[str, Any]) -> list[Any]:
    status = run.get("step_status")
    if not isinstance(status, list):
        status = ["pending"] * len(_steps(run))
        run["step_status"] = status
    return status


def _prune_completed(runs: dict[str, Any], workflow_name: str) -> None:
    completed: list[tuple[str, dict[str, Any], datetime]] = []
    for run_id, run in list(runs.items()):
        if not isinstance(run, dict):
            continue
        if run.get("workflow_name") != workflow_name or run.get("state") != "completed":
            continue
        stamp = _parse_dt(run.get("last_progress_at")) or _parse_dt(run.get("started_at"))
        if stamp is None:
            stamp = datetime.min.replace(tzinfo=timezone.utc)
        completed.append((run_id, run, stamp))
    completed.sort(key=lambda item: (item[2], item[0]))
    while len(completed) > COMPLETED_RETENTION:
        run_id, _run, _stamp = completed.pop(0)
        runs.pop(run_id, None)


def _mark_completed(run: dict[str, Any], runs: dict[str, Any], stamp: str) -> None:
    status = _step_status_list(run)
    degraded = any(token == "skipped" for token in status)
    run["state"] = "completed"
    run["degraded"] = degraded
    run["last_progress_at"] = stamp
    _prune_completed(runs, str(run.get("workflow_name") or ""))


def _advance_pointer(run: dict[str, Any], runs: dict[str, Any], step_index: int, stamp: str) -> None:
    status = _step_status_list(run)
    status[step_index] = "completed" if status[step_index] != "skipped" else "skipped"
    run["last_progress_at"] = stamp
    last_index = max(len(_steps(run)) - 1, 0)
    if step_index >= last_index:
        _mark_completed(run, runs, stamp)
        run["current_step_index"] = last_index
        return
    run["current_step_index"] = step_index + 1
    run["state"] = "running"


def start_run(
    workflow_name: str,
    trigger_source: str,
    session_id: str,
    definition: Mapping[str, Any] | None = None,
    *,
    workflow: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    actor: str = OPERATOR_ACTOR,
) -> dict[str, Any]:
    """Create a running workflow run. Dedup of an active run is inside mutate_kv."""
    if _in_hosted_session():
        raise WorkflowRunError(_hosted_start_refusal())
    name = _require_non_empty_str(workflow_name, "workflow_name")
    trigger = _require_non_empty_str(trigger_source, "trigger_source")
    owner = _require_non_empty_str(session_id, "session_id")
    snapshot_src = workflow if workflow is not None else definition
    if not isinstance(snapshot_src, Mapping):
        raise WorkflowRunError("workflow: required")
    snapshot = copy.deepcopy(dict(snapshot_src))
    steps = snapshot.get("steps")
    if not isinstance(steps, list) or not steps:
        raise WorkflowRunError("workflow.steps: must be a non-empty list")
    stamp = _iso(now)

    def _insert(data: dict[str, Any]) -> dict[str, Any]:
        runs = _runs_map(data)
        active = _active_for_workflow(runs, name)
        if active is not None:
            active_id = active.get("workflow_run_id") or ""
            raise WorkflowRunError(
                f"active run already exists for workflow {name}: {active_id}"
            )
        run_id = _new_run_id(name, runs)
        record = _ordered_run(
            {
                "workflow_run_id": run_id,
                "workflow_name": name,
                "definition": snapshot,
                "current_step_index": 0,
                "step_status": ["pending"] * len(steps),
                "trigger_source": trigger,
                "started_at": stamp,
                "last_progress_at": stamp,
                "session_id": owner,
                "state": "running",
            }
        )
        runs[run_id] = record
        return _copy_run(record)

    return _mutate(config, _insert, action="workflow.start", actor=actor or OPERATOR_ACTOR)


def get_run(
    run_id: str,
    *,
    config: Mapping[str, Any] | None = None,
    open_db: Any = None,
) -> dict[str, Any] | None:
    run_id = _require_non_empty_str(run_id, "workflow_run_id")
    run = _load_runs(config, open_db=open_db).get(run_id)
    if not isinstance(run, dict):
        return None
    return _copy_run(run)


def list_runs(*, config: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """All runs: active first, then remaining newest-first by last progress."""
    items: list[dict[str, Any]] = []
    for run in _load_runs(config).values():
        if isinstance(run, dict):
            items.append(_copy_run(run))

    def _sort_key(run: Mapping[str, Any]) -> tuple[int, float, str]:
        active = 0 if run.get("state") in ACTIVE_STATES else 1
        stamp = _parse_dt(run.get("last_progress_at")) or _parse_dt(run.get("started_at"))
        ts = -(stamp.timestamp()) if stamp is not None else 0.0
        return (active, ts, str(run.get("workflow_run_id") or ""))

    items.sort(key=_sort_key)
    return items


def advance_run(
    run_id: str,
    step_index: int,
    evidence: Any = None,
    *,
    config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    actor: str = OPERATOR_ACTOR,
    open_db: Any = None,
) -> dict[str, Any]:
    """Complete the current step. Future indexes error; the prior completed step is a no-op."""
    del evidence
    run_id = _require_non_empty_str(run_id, "workflow_run_id")
    if not isinstance(step_index, int) or isinstance(step_index, bool) or step_index < 0:
        raise WorkflowRunError(f"step_index: must be a non-negative integer, got {step_index!r}")
    stamp = _iso(now)
    writer = actor if actor else OPERATOR_ACTOR
    existing = _load_runs(config, open_db=open_db).get(run_id)
    approval_ok = True
    if isinstance(existing, dict) and str(existing.get("state") or "") == "awaiting-approval":
        try:
            current_pre = int(existing.get("current_step_index") or 0)
        except (TypeError, ValueError):
            current_pre = 0
        if step_index == current_pre:
            step = _current_step(existing)
            bound_id = str((step or {}).get("action_id") or "").strip()
            approval_ok = _action_executed_success(bound_id, config, open_db=open_db)
            if not approval_ok:
                raise WorkflowRunError(
                    "cannot advance past an unsatisfied approval gate: "
                    "bound action is not executed with success"
                )

    def _advance(data: dict[str, Any]) -> dict[str, Any]:
        runs = _runs_map(data)
        run = _get_existing(runs, run_id)
        _require_active(run, "advance")
        current = int(run.get("current_step_index") or 0)
        if step_index > current:
            raise WorkflowRunError(
                f"out-of-order advance: step {step_index} while current is {current}"
            )
        if step_index < current:
            return _copy_run(run)
        if str(run.get("state") or "") == "awaiting-approval" and not approval_ok:
            raise WorkflowRunError(
                "cannot advance past an unsatisfied approval gate: "
                "bound action is not executed with success"
            )
        status = _step_status_list(run)
        if step_index >= len(status):
            raise WorkflowRunError(f"step_index {step_index} is past the end of the run")
        status[step_index] = "completed"
        _advance_pointer(run, runs, step_index, stamp)
        return _copy_run(run)

    return _mutate(
        config,
        _advance,
        action="workflow.advance",
        actor=writer,
        workflow_run_id=run_id,
        open_db=open_db,
    )


def skip_step(
    run_id: str,
    step_index: int,
    reason: str,
    *,
    config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    actor: str = OPERATOR_ACTOR,
    open_db: Any = None,
) -> dict[str, Any]:
    """Skip the current optional step. Required steps are refused."""
    run_id = _require_non_empty_str(run_id, "workflow_run_id")
    _require_non_empty_str(reason, "reason")
    if not isinstance(step_index, int) or isinstance(step_index, bool) or step_index < 0:
        raise WorkflowRunError(f"step_index: must be a non-negative integer, got {step_index!r}")
    stamp = _iso(now)

    def _skip(data: dict[str, Any]) -> dict[str, Any]:
        runs = _runs_map(data)
        run = _get_existing(runs, run_id)
        _require_active(run, "skip")
        current = int(run.get("current_step_index") or 0)
        if step_index != current:
            raise WorkflowRunError(
                f"can only skip the current step (index {current}), not {step_index}"
            )
        steps = _steps(run)
        if step_index >= len(steps) or not isinstance(steps[step_index], Mapping):
            raise WorkflowRunError(f"step_index {step_index} is past the end of the run")
        step = steps[step_index]
        if step.get("required", True) is not False:
            raise WorkflowRunError(
                f"step {step.get('id')} is required and cannot be skipped"
            )
        status = _step_status_list(run)
        status[step_index] = "skipped"
        last_index = max(len(steps) - 1, 0)
        run["last_progress_at"] = stamp
        if step_index >= last_index:
            _mark_completed(run, runs, stamp)
            run["current_step_index"] = last_index
        else:
            run["current_step_index"] = step_index + 1
            run["state"] = "running"
        return _copy_run(run)

    return _mutate(
        config,
        _skip,
        action="workflow.skip",
        actor=actor or OPERATOR_ACTOR,
        workflow_run_id=run_id,
        open_db=open_db,
    )


def bind_action(
    run_id: str,
    step_id: str,
    action_id: str,
    *,
    config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    actor: str = OPERATOR_ACTOR,
) -> dict[str, Any]:
    """Write action_id onto the snapshot step once. First bind of an approval step parks."""
    del now
    run_id = _require_non_empty_str(run_id, "workflow_run_id")
    step_id = _require_non_empty_str(step_id, "step_id")
    action_id = _require_non_empty_str(action_id, "action_id")

    def _bind(data: dict[str, Any]) -> dict[str, Any]:
        runs = _runs_map(data)
        run = _get_existing(runs, run_id)
        _require_active(run, "bind")
        steps = _steps(run)
        found_index: int | None = None
        step: dict[str, Any] | None = None
        for index, raw in enumerate(steps):
            if isinstance(raw, dict) and raw.get("id") == step_id:
                found_index = index
                step = raw
                break
        if found_index is None or step is None:
            raise WorkflowRunError(f"step {step_id!r} is not in run {run_id}")
        existing = step.get("action_id")
        if existing and existing != action_id:
            raise WorkflowRunError(
                f"step {step_id} already bound to {existing}; overwrite refused"
            )
        step["action_id"] = action_id
        approval_required = bool(step.get("requires_approval")) or "review_queue" in step
        if found_index == int(run.get("current_step_index") or 0) and approval_required:
            run["state"] = "awaiting-approval"
            status = _step_status_list(run)
            if found_index < len(status):
                status[found_index] = "awaiting-approval"
        return _copy_run(run)

    return _mutate(
        config,
        _bind,
        action="workflow.bind",
        actor=actor or OPERATOR_ACTOR,
        workflow_run_id=run_id,
    )


def complete_run(
    run_id: str,
    *,
    config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    actor: str = OPERATOR_ACTOR,
) -> dict[str, Any]:
    run_id = _require_non_empty_str(run_id, "workflow_run_id")
    stamp = _iso(now)

    def _complete(data: dict[str, Any]) -> dict[str, Any]:
        runs = _runs_map(data)
        run = _get_existing(runs, run_id)
        _require_active(run, "complete")
        _mark_completed(run, runs, stamp)
        return _copy_run(run)

    return _mutate(
        config,
        _complete,
        action="workflow.complete",
        actor=actor or OPERATOR_ACTOR,
        workflow_run_id=run_id,
    )


def abort_run(
    run_id: str,
    *,
    config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    actor: str = OPERATOR_ACTOR,
) -> dict[str, Any]:
    run_id = _require_non_empty_str(run_id, "workflow_run_id")
    stamp = _iso(now)

    def _abort(data: dict[str, Any]) -> dict[str, Any]:
        runs = _runs_map(data)
        run = _get_existing(runs, run_id)
        _require_active(run, "abort")
        run["state"] = "aborted"
        run["last_progress_at"] = stamp
        return _copy_run(run)

    return _mutate(
        config,
        _abort,
        action="workflow.abort",
        actor=actor or OPERATOR_ACTOR,
        workflow_run_id=run_id,
    )


def resume_run(
    run_id: str,
    new_session_id: str,
    *,
    config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    actor: str = OPERATOR_ACTOR,
) -> dict[str, Any]:
    run_id = _require_non_empty_str(run_id, "workflow_run_id")
    owner = _require_non_empty_str(new_session_id, "session_id")
    stamp = _iso(now)

    def _resume(data: dict[str, Any]) -> dict[str, Any]:
        runs = _runs_map(data)
        run = _get_existing(runs, run_id)
        _require_active(run, "resume")
        run["session_id"] = owner
        run["state"] = "running"
        run["last_progress_at"] = stamp
        return _copy_run(run)

    return _mutate(
        config,
        _resume,
        action="workflow.resume",
        actor=actor or OPERATOR_ACTOR,
        workflow_run_id=run_id,
    )


def is_stale(
    run: Mapping[str, Any],
    threshold_hours: int = DEFAULT_STALE_HOURS,
    now: datetime | None = None,
) -> tuple[bool, str, timedelta]:
    """Return (stale, current step name, age since last_progress_at)."""
    if not isinstance(run, Mapping):
        raise WorkflowRunError("run: must be a mapping")
    last = _parse_dt(run.get("last_progress_at")) or _parse_dt(run.get("started_at"))
    moment = _aware(now)
    if last is None:
        age = timedelta(0)
    else:
        age = moment - last
    try:
        hours = float(threshold_hours)
    except (TypeError, ValueError):
        hours = float(DEFAULT_STALE_HOURS)
    stale = age >= timedelta(hours=hours)
    steps = _steps(run)
    index = int(run.get("current_step_index") or 0)
    step_name = ""
    if 0 <= index < len(steps) and isinstance(steps[index], Mapping):
        step_name = str(steps[index].get("name") or steps[index].get("id") or "")
    return stale, step_name, age


def _facts_project_root(config: Mapping[str, Any] | None) -> Path | None:
    if not isinstance(config, Mapping):
        return None
    paths = config.get("paths")
    if not isinstance(paths, Mapping) or not paths.get("project_root"):
        return None
    return Path(str(paths["project_root"])).expanduser()


def get_facts(*, config: Mapping[str, Any] | None = None, open_db: Any = None) -> dict[str, Any] | None:
    """Load the workflow_facts kv doc. Missing or empty docs return None."""
    data = load_store(STORE_FACTS, config=config, validate=False, open_db=open_db)
    if not isinstance(data, dict) or not data:
        return None
    return copy.deepcopy(data)


def build_facts(
    config: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    workflow_name: str | None = None,
) -> dict[str, Any]:
    """Rebuild a facts payload from capabilities + state-file presence. Does not write."""
    capabilities: dict[str, Any] = {}
    try:
        from capability_report import build_capability_report

        report = build_capability_report(config or {})
        supported = report.get("supported") if isinstance(report, Mapping) else None
        if isinstance(supported, list):
            capabilities = {str(name): True for name in supported if str(name).strip()}
        unsupported = report.get("unsupported") if isinstance(report, Mapping) else None
        if isinstance(unsupported, list):
            for name in unsupported:
                key = str(name).strip()
                if key:
                    capabilities.setdefault(key, False)
    except Exception:
        capabilities = {}
    root = _facts_project_root(config)
    state_files: dict[str, bool] = {}
    for name in STATE_FILE_NAMES:
        state_files[name] = bool(root is not None and (root / name).is_file())
    payload: dict[str, Any] = {
        "refreshed_at": _iso(now),
        "facts_max_age_hours": DEFAULT_FACTS_MAX_AGE_HOURS,
        "capabilities": capabilities,
        "state_files": state_files,
    }
    if workflow_name:
        payload["workflow_name"] = workflow_name
    return payload


def refresh_facts(
    facts: Mapping[str, Any] | None = None,
    *,
    config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    actor: str = OPERATOR_ACTOR,
    workflow_name: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Write the workflow_facts kv doc via mutate_kv with audit fields."""
    stamp = _iso(now)
    if isinstance(facts, Mapping) and facts:
        payload = copy.deepcopy(dict(facts))
    else:
        payload = build_facts(config, now=now, workflow_name=workflow_name)
    payload["refreshed_at"] = stamp
    try:
        payload["facts_max_age_hours"] = float(
            payload.get("facts_max_age_hours", DEFAULT_FACTS_MAX_AGE_HOURS)
        )
    except (TypeError, ValueError):
        payload["facts_max_age_hours"] = float(DEFAULT_FACTS_MAX_AGE_HOURS)
    if not isinstance(payload.get("capabilities"), dict):
        payload["capabilities"] = {}
    if not isinstance(payload.get("state_files"), dict):
        payload["state_files"] = {}
    if workflow_name:
        payload["workflow_name"] = workflow_name
    if run_id:
        payload["workflow_run_id"] = run_id

    def _write(data: dict[str, Any]) -> dict[str, Any]:
        data.clear()
        data.update(copy.deepcopy(payload))
        return copy.deepcopy(data)

    return mutate_kv(
        STORE_FACTS,
        _write,
        config=config,
        action="workflow.refresh-facts",
        actor=actor or OPERATOR_ACTOR,
        after=payload,
    )


def _json_dump(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _truncate(value: Any, width: int) -> str:
    text = "" if value is None else str(value).replace("\n", " ")
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def _print_table(runs: Sequence[Mapping[str, Any]]) -> None:
    columns: list[tuple[str, str, int]] = [
        ("workflow_run_id", "RUN ID", 28),
        ("workflow_name", "WORKFLOW", 20),
        ("state", "STATE", 18),
        ("session_id", "SESSION", 16),
        ("current_step_index", "STEP", 6),
    ]
    if not runs:
        print("No workflow runs")
        return
    header = "  ".join(label.ljust(width) for _, label, width in columns)
    print(header)
    print("  ".join("-" * width for _, _, width in columns))
    for run in runs:
        cells = [
            _truncate(run.get(key, ""), width).ljust(width) for key, _, width in columns
        ]
        print("  ".join(cells))


def _load_config_or_exit(config_path: str | None) -> Any | None:
    config = load_config(config_path)
    if config is None:
        return None
    return config


def _cli_error(exc: BaseException) -> int:
    print(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False))
    return 1


def cmd_runs(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args.config)
    if config is None:
        return 1
    runs = list_runs(config=config)
    if getattr(args, "summary", False):
        _print_table(runs)
        return 0
    print(_json_dump({"runs": runs}))
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args.config)
    if config is None:
        return 1
    if yaml is None:
        print("PyYAML is required to start a run from a workflow file", file=sys.stderr)
        return 1
    path = Path(str(args.file)).expanduser()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        workflow = validate_workflow(raw)
        run = start_run(
            workflow["name"],
            args.trigger,
            args.session_id,
            workflow=workflow,
            config=config,
        )
    except (OSError, WorkflowRunError, Exception) as exc:
        return _cli_error(exc)
    if getattr(args, "summary", False):
        _print_table([run])
    else:
        print(_json_dump(run))
    return 0


def cmd_abort(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args.config)
    if config is None:
        return 1
    try:
        run = abort_run(args.run_id, config=config)
    except Exception as exc:
        return _cli_error(exc)
    if getattr(args, "summary", False):
        _print_table([run])
    else:
        print(_json_dump(run))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args.config)
    if config is None:
        return 1
    try:
        run = resume_run(args.run_id, args.session_id, config=config)
    except Exception as exc:
        return _cli_error(exc)
    if getattr(args, "summary", False):
        _print_table([run])
    else:
        print(_json_dump(run))
    return 0


def _add_common_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        dest="sub_config",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )


def _add_summary_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--summary", action="store_true", help="Human-readable table instead of JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chief-of-Staff workflow run store")
    parser.add_argument(
        "--config",
        help="Path to company.yaml (default: CHIEF_OF_STAFF_CONFIG or shared/config/company.yaml)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    runs_parser = sub.add_parser("runs", help="List workflow runs (JSON default)")
    _add_common_config_arg(runs_parser)
    _add_summary_arg(runs_parser)

    start_parser = sub.add_parser("start", help="Start a run from a validated workflow YAML file")
    _add_common_config_arg(start_parser)
    _add_summary_arg(start_parser)
    start_parser.add_argument("--file", required=True, help="Path to workflow YAML")
    start_parser.add_argument("--trigger", default="message", help="Trigger source (message or cron)")
    start_parser.add_argument("--session-id", required=True)

    abort_parser = sub.add_parser("abort", help="Abort an active run")
    _add_common_config_arg(abort_parser)
    _add_summary_arg(abort_parser)
    abort_parser.add_argument("--run-id", required=True)

    resume_parser = sub.add_parser("resume", help="Rebind the owning session and resume a run")
    _add_common_config_arg(resume_parser)
    _add_summary_arg(resume_parser)
    resume_parser.add_argument("--run-id", required=True)
    resume_parser.add_argument("--session-id", required=True)

    return parser


def _main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "sub_config"):
        args.config = args.sub_config

    if args.command == "runs":
        return cmd_runs(args)
    if args.command == "start":
        return cmd_start(args)
    if args.command == "abort":
        return cmd_abort(args)
    if args.command == "resume":
        return cmd_resume(args)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
