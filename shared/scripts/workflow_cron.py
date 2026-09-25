#!/usr/bin/env python3
"""Workflow cron lifecycle: hermes-cron install/uninstall, occurrence fire, doctor checks.

One kv document ``workflow_crons`` at ``__root__``. Every write goes through
``mutate_kv``. Registration shells out to ``hermes cron create`` / ``remove``
the same way ``install_cron.py`` does.
"""
from __future__ import annotations

import copy
import hashlib
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from doctor_base import CheckResult  # noqa: E402
from state_db import (  # noqa: E402
    create_pending_action,
    get_pending_action,
    list_pending_actions,
    load_store,
    mark_executed,
    mark_executing,
    mutate_kv,
)
from workflow_runs import (  # noqa: E402
    ACTIVE_STATES,
    DEFAULT_STALE_HOURS,
    WorkflowRunError,
    get_facts,
    is_stale,
    list_runs,
    start_run,
)
from workflows import validate_workflow  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
STORE_CRONS = "workflow_crons"
CRON_CREATE_TYPE = "cron.create"
OCCURRENCES_BOUND = 20
NO_PROGRESS_THRESHOLD = 3
SCHEDULE_ID_MAX = 32
HOOK_ACTOR = "hook:workflow-orchestrator"
OPERATOR_ACTOR = "operator"
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9-]*[a-z0-9]|[a-z0-9]")
_CRON_FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


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


def _floor_minute(dt: datetime) -> datetime:
    return _aware(dt).replace(second=0, microsecond=0)


def _same_minute(left: Any, right: datetime) -> bool:
    parsed = _parse_dt(left)
    if parsed is None:
        return False
    return _floor_minute(parsed) == _floor_minute(right)


def _copy_binding(record: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(record))


def _bindings_map(data: dict[str, Any]) -> dict[str, Any]:
    bindings = data.get("bindings")
    if not isinstance(bindings, dict):
        bindings = {}
        data["bindings"] = bindings
    return bindings


def _mutate(
    config: Mapping[str, Any] | None,
    mutate_fn: Any,
    *,
    action: str,
    actor: str = OPERATOR_ACTOR,
    workflow_run_id: str | None = None,
) -> Any:
    audit_after: dict[str, Any] = {}

    def _wrapped(data: dict[str, Any]) -> Any:
        result = mutate_fn(data)
        audit_after.clear()
        if isinstance(data, dict):
            audit_after.update(copy.deepcopy(data))
        rid = workflow_run_id
        if not rid and isinstance(result, Mapping):
            rid = result.get("workflow_run_id") or result.get("run_id")
        if rid:
            audit_after["workflow_run_id"] = rid
        return result

    return mutate_kv(
        STORE_CRONS,
        _wrapped,
        config=config,
        action=action,
        actor=actor,
        after=audit_after,
    )


def _load_doc(config: Mapping[str, Any] | None) -> dict[str, Any]:
    data = load_store(STORE_CRONS, config=config, validate=False)
    if isinstance(data, dict):
        return data
    return {}


def _get_binding(config: Mapping[str, Any] | None, name: str) -> dict[str, Any] | None:
    doc = _load_doc(config)
    bindings = doc.get("bindings")
    if not isinstance(bindings, dict):
        return None
    record = bindings.get(name)
    if isinstance(record, dict):
        return _copy_binding(record)
    return None


def _schedule_id(workflow_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(workflow_name).strip().lower()).strip("-")
    if not slug:
        slug = "wf"
    if len(slug) <= SCHEDULE_ID_MAX and _SAFE_ID.fullmatch(slug):
        return slug
    digest = hashlib.sha256(str(workflow_name).encode("utf-8")).hexdigest()[:8]
    base = slug[: max(SCHEDULE_ID_MAX - 9, 1)].rstrip("-") or "wf"
    candidate = f"{base}-{digest}"[:SCHEDULE_ID_MAX].rstrip("-")
    if _SAFE_ID.fullmatch(candidate):
        return candidate
    return digest


def _cron_expr(workflow: Mapping[str, Any]) -> str:
    triggers = workflow.get("triggers")
    if not isinstance(triggers, Mapping):
        raise WorkflowRunError("triggers.schedule.cron: required")
    schedule = triggers.get("schedule")
    if not isinstance(schedule, Mapping):
        raise WorkflowRunError("triggers.schedule.cron: required")
    cron = str(schedule.get("cron") or "").strip()
    if not cron:
        raise WorkflowRunError("triggers.schedule.cron: required")
    return cron


def _deliver_target(config: Mapping[str, Any] | None) -> str:
    delivery = config.get("delivery", {}) if isinstance(config, Mapping) else {}
    if not isinstance(delivery, Mapping):
        return "local"
    channel = str(delivery.get("channel") or "local")
    chat_id = delivery.get("chat_id") or delivery.get("home_chat_id")
    if channel == "telegram" and chat_id:
        return f"telegram:{chat_id}"
    return channel or "local"


def _hermes_cron(cmd: list[str]) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except Exception as exc:
        raise WorkflowRunError(f"hermes cron failed: {exc}") from exc
    if int(getattr(proc, "returncode", 1) or 0) != 0:
        err = str(getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "").strip()
        if len(err) > 400:
            err = err[:400]
        verb = cmd[2] if len(cmd) > 2 else "command"
        detail = f": {err}" if err else f" (exit {proc.returncode})"
        raise WorkflowRunError(f"hermes cron {verb} failed{detail}")


def _schedule_timezone(workflow: Mapping[str, Any]) -> str | None:
    triggers = workflow.get("triggers")
    if not isinstance(triggers, Mapping):
        return None
    schedule = triggers.get("schedule")
    if not isinstance(schedule, Mapping):
        return None
    timezone_name = schedule.get("timezone")
    if timezone_name is None or timezone_name == "":
        return None
    return str(timezone_name)


def _cron_create_payload_name(action: Mapping[str, Any], name: str) -> bool:
    payload = action.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    target = str(action.get("target") or "").strip()
    payload_name = str(payload.get("workflow_name") or "").strip()
    return target == name or payload_name == name


def find_cron_create_action(
    name: str,
    *,
    config: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Latest cron.create action for ``name``, preferring an open (non-terminal) one."""
    try:
        pending = list_pending_actions(config, include_expired=True) or []
    except Exception:
        pending = []
    open_states = {"requested", "approved", "executing"}
    open_match: dict[str, Any] | None = None
    any_match: dict[str, Any] | None = None
    for action in pending:
        if not isinstance(action, dict) or action.get("type") != CRON_CREATE_TYPE:
            continue
        if not _cron_create_payload_name(action, name):
            continue
        any_match = action
        if action.get("state") in open_states:
            open_match = action
    return open_match or any_match


def propose_cron_create(
    name: str,
    workflow: Mapping[str, Any],
    config: Mapping[str, Any] | None,
    *,
    session_id: str,
) -> dict[str, Any]:
    """Create (or reuse) a pending cron.create action. Does not shell out."""
    existing = find_cron_create_action(name, config=config)
    if existing is not None and existing.get("state") in {"requested", "approved", "executing"}:
        return existing
    cron = _cron_expr(workflow)
    timezone_name = _schedule_timezone(workflow)
    created = create_pending_action(
        config=config,
        action_type=CRON_CREATE_TYPE,
        provider="local",
        target=name,
        payload={
            "workflow_name": name,
            "cron": cron,
            "timezone": timezone_name,
            "session_id": session_id,
            "workflow": copy.deepcopy(dict(workflow)),
        },
        summary=f"cron.create: schedule workflow {name} ({cron})",
    )
    if not isinstance(created, dict):
        raise WorkflowRunError("failed to propose cron.create action")
    return created


def execute_cron_create(
    config: Mapping[str, Any] | None,
    action_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Claim (if still approved) then register the hermes cron job and write the binding."""
    action_id = str(action_id or "").strip()
    if not action_id:
        raise WorkflowRunError("action_id: must be a non-empty string")
    action = get_pending_action(config, action_id)
    if not isinstance(action, dict) or action.get("type") != CRON_CREATE_TYPE:
        raise WorkflowRunError(f"cron.create action not found: {action_id}")
    state = str(action.get("state") or "")
    if state == "executing":
        # Claimed by another process: only the claim winner may register the
        # cron, or it is installed twice.
        raise WorkflowRunError(
            f"cron.create {action_id} is already claimed by another execution; not running it again")
    if state == "approved":
        claimed = mark_executing(config, action_id)
        if not isinstance(claimed, dict):
            raise WorkflowRunError(f"could not claim cron.create action {action_id}")
        action = claimed
        state = str(action.get("state") or "")
    if state != "executing":
        raise WorkflowRunError(
            f"refusing to register cron until cron.create is approved and claimed "
            f"(state={state})"
        )
    payload = action.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    name = str(payload.get("workflow_name") or action.get("target") or "").strip()
    workflow = payload.get("workflow")
    if not name or not isinstance(workflow, Mapping):
        raise WorkflowRunError("cron.create payload missing workflow_name/workflow")
    session_id = str(payload.get("session_id") or "operator").strip() or "operator"
    binding = install_workflow_cron(
        name,
        workflow,
        config,
        now=now,
        session_id=session_id,
    )
    mark_executed(config, action_id, {"success": True, "schedule_id": binding.get("schedule_id")})
    return binding


def _field_matches(spec: str, value: int, minimum: int, maximum: int) -> bool:
    spec = spec.strip()
    if not spec:
        return False
    if spec == "*":
        return True
    for raw_part in spec.split(","):
        part = raw_part.strip()
        if not part:
            continue
        try:
            if "/" in part:
                range_part, step_s = part.split("/", 1)
                step = int(step_s)
                if step <= 0:
                    continue
                if range_part in ("", "*"):
                    start, end = minimum, maximum
                elif "-" in range_part:
                    start_s, end_s = range_part.split("-", 1)
                    start, end = int(start_s), int(end_s)
                else:
                    start, end = int(range_part), maximum
                if start <= value <= end and (value - start) % step == 0:
                    return True
            elif "-" in part:
                start_s, end_s = part.split("-", 1)
                if int(start_s) <= value <= int(end_s):
                    return True
            elif int(part) == value:
                return True
        except ValueError:
            return False
    return False


def _step_sizes(spec: str) -> list[int]:
    steps: list[int] = []
    for raw_part in spec.split(","):
        part = raw_part.strip()
        if "/" not in part:
            continue
        range_part, step_s = part.split("/", 1)
        try:
            step = int(step_s)
        except ValueError:
            continue
        if step > 0 and range_part in ("", "*"):
            steps.append(step)
    return steps


def _cron_matches(expr: str, now: datetime, origin: datetime | None = None) -> bool:
    fields = expr.split()
    if len(fields) == 6:
        fields = fields[1:]
    if len(fields) != 5:
        return False
    moment = _floor_minute(now)
    values = (
        moment.minute,
        moment.hour,
        moment.day,
        moment.month,
        (moment.weekday() + 1) % 7,
    )
    standard = True
    for index, (spec, value, bounds) in enumerate(
        zip(fields, values, _CRON_FIELD_BOUNDS, strict=True)
    ):
        if index == 4 and value == 0 and _field_matches(spec, 7, *bounds):
            continue
        if not _field_matches(spec, value, *bounds):
            standard = False
            break
    if standard:
        return True
    if origin is None:
        return False
    origin_m = _floor_minute(origin)
    minute_steps = _step_sizes(fields[0])
    if not minute_steps:
        return False
    delta_min = int((moment - origin_m).total_seconds() // 60)
    if delta_min < 0:
        return False
    if not any(delta_min % step == 0 for step in minute_steps):
        return False
    for index, (spec, value, bounds) in enumerate(
        zip(fields[1:], values[1:], _CRON_FIELD_BOUNDS[1:], strict=True),
        start=1,
    ):
        if index == 4 and value == 0 and _field_matches(spec, 7, *bounds):
            continue
        if not _field_matches(spec, value, *bounds):
            return False
    return True


def _config_or_none(data: dict[str, Any] | None) -> dict[str, Any] | None:
    return data if isinstance(data, dict) else None


def _plugin_skill_path(workflow_name: str) -> Path:
    return PLUGIN_ROOT / "skills.local" / workflow_name / "SKILL.md"


def _current_step(run: Mapping[str, Any]) -> dict[str, Any] | None:
    definition = run.get("definition")
    if not isinstance(definition, Mapping):
        return None
    steps = definition.get("steps")
    if not isinstance(steps, list):
        return None
    try:
        index = int(run.get("current_step_index") or 0)
    except (TypeError, ValueError):
        index = 0
    if 0 <= index < len(steps) and isinstance(steps[index], Mapping):
        return dict(steps[index])
    return None


def _action_succeeded(action_id: str, config: Mapping[str, Any] | None) -> bool:
    if not action_id:
        return False
    action = get_pending_action(config, action_id)
    if not isinstance(action, dict) or action.get("state") != "executed":
        return False
    result = action.get("result")
    if not isinstance(result, Mapping):
        return False
    return bool(result.get("success"))


def _active_for_workflow(config: Mapping[str, Any] | None, workflow_name: str) -> dict[str, Any] | None:
    for run in list_runs(config=config):
        if run.get("workflow_name") == workflow_name and run.get("state") in ACTIVE_STATES:
            return run
    return None


def _record_occurrence(record: dict[str, Any], now: datetime, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    stamp = _iso(now)
    occurrence: dict[str, Any] = {"at": stamp, "workflow_name": record.get("workflow_name")}
    if extra:
        occurrence.update(dict(extra))
    occurrences = record.get("occurrences")
    if not isinstance(occurrences, list):
        occurrences = []
    occurrences.append(occurrence)
    record["occurrences"] = occurrences[-OCCURRENCES_BOUND:]
    record["last_occurrence_at"] = stamp
    return occurrence


def install_workflow_cron(
    name: str,
    workflow: Mapping[str, Any],
    config: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    session_id: str,
) -> dict[str, Any]:
    """Validate, register via ``hermes cron create``, and upsert the kv binding.

    This is the post-claim executor. ``workflows install`` proposes a
    ``cron.create`` review-queue action and refuses to call this until that
    action is approved and claimed. Direct callers (tests of the hermes-cron
    primitive, idempotent reinstall of an existing binding) still register.
    """
    if not isinstance(name, str) or not name.strip():
        raise WorkflowRunError("workflow_name: must be a non-empty string")
    name = name.strip()
    try:
        snapshot = validate_workflow(workflow)
    except Exception as exc:
        raise WorkflowRunError(str(exc)) from exc
    cron = _cron_expr(snapshot)
    owner = str(session_id or "").strip()
    if not owner:
        raise WorkflowRunError("session_id: must be a non-empty string")
    schedule_id = _schedule_id(name)
    stamp = _iso(now)
    prompt = (
        f"Run the Chief-of-Staff workflow {name}. Load the generated skill and execute the workflow. "
        "This is a scheduled run; do not rely on conversation history. "
        f"Then run: chief_of_staff.py workflows fire --schedule-id {schedule_id}"
    )
    cmd = [
        "hermes",
        "cron",
        "create",
        cron,
        prompt,
        "--name",
        schedule_id,
        "--deliver",
        _deliver_target(config),
    ]
    _hermes_cron(cmd)

    def _upsert(data: dict[str, Any]) -> dict[str, Any]:
        bindings = _bindings_map(data)
        existing = bindings.get(name)
        if isinstance(existing, dict):
            record = existing
            record["cron"] = cron
            record["session_id"] = owner
            record["schedule_id"] = schedule_id
            record["workflow_name"] = name
            record["workflow"] = copy.deepcopy(snapshot)
            record.setdefault("last_occurrence_at", None)
            record.setdefault("missed_count", 0)
            record.setdefault("parked_count", 0)
            record.setdefault("occurrences", [])
            record.setdefault("wakeup_notes", [])
            record.setdefault("no_progress_count", 0)
        else:
            record = {
                "schedule_id": schedule_id,
                "workflow_name": name,
                "cron": cron,
                "session_id": owner,
                "created_at": stamp,
                "last_occurrence_at": None,
                "missed_count": 0,
                "parked_count": 0,
                "occurrences": [],
                "wakeup_notes": [],
                "no_progress_count": 0,
                "workflow": copy.deepcopy(snapshot),
            }
            bindings[name] = record
        return _copy_binding(record)

    return _mutate(config, _upsert, action="workflow.cron.install", actor=OPERATOR_ACTOR)


def uninstall_workflow_cron(name: str, config: Mapping[str, Any] | None) -> None:
    """Remove the kv binding and deregister the hermes cron job."""
    if not isinstance(name, str) or not name.strip():
        raise WorkflowRunError("workflow_name: must be a non-empty string")
    name = name.strip()
    existing = _get_binding(config, name)
    if existing is None:
        raise WorkflowRunError(f"no cron binding installed for workflow {name}")
    schedule_id = str(existing.get("schedule_id") or name)
    _hermes_cron(["hermes", "cron", "remove", schedule_id])

    def _remove(data: dict[str, Any]) -> None:
        bindings = _bindings_map(data)
        if name not in bindings:
            raise WorkflowRunError(f"no cron binding installed for workflow {name}")
        bindings.pop(name, None)

    _mutate(config, _remove, action="workflow.cron.uninstall", actor=OPERATOR_ACTOR)


def _record_wakeup_note(
    name: str,
    config: Mapping[str, Any] | None,
    run: Mapping[str, Any],
    moment: datetime,
    *,
    occurrence_count: int,
) -> dict[str, Any] | None:
    target = _deliver_target(config)
    run_id = str(run.get("workflow_run_id") or "")
    count = max(int(occurrence_count or 0), 1)

    def _note(data: dict[str, Any]) -> dict[str, Any] | None:
        bindings = _bindings_map(data)
        record = bindings.get(name)
        if not isinstance(record, dict):
            return None
        notes = record.get("wakeup_notes")
        if not isinstance(notes, list):
            notes = []
        for existing in notes:
            if isinstance(existing, Mapping) and _same_minute(existing.get("at"), moment):
                return dict(existing)
        note = {
            "at": _iso(moment),
            "run_id": run_id,
            "occurrence_count": count,
            "delivery_target": target,
            "message": (
                f"parked at awaiting-approval (occurrence {count}); "
                f"wait reported to {target}"
            ),
        }
        notes.append(note)
        record["wakeup_notes"] = notes[-OCCURRENCES_BOUND:]
        return dict(note)

    return _mutate(
        config,
        _note,
        action="workflow.wakeup",
        actor=HOOK_ACTOR,
        workflow_run_id=run_id or None,
    )


def fire_occurrence(
    workflow_name: str,
    config: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Tick handler: record a due occurrence and start/wakeup/stall as needed."""
    if not isinstance(workflow_name, str) or not workflow_name.strip():
        return None
    name = workflow_name.strip()
    binding = _get_binding(config, name)
    if binding is None:
        return None
    moment = _aware(now)
    if _same_minute(binding.get("last_occurrence_at"), moment):
        return None
    cron = str(binding.get("cron") or "").strip()
    origin = _parse_dt(binding.get("created_at"))
    if not cron or not _cron_matches(cron, moment, origin=origin):
        return None

    previous_last = _parse_dt(binding.get("last_occurrence_at"))
    active = _active_for_workflow(config, name)
    occurrence_box: dict[str, Any] = {}

    def _tick(data: dict[str, Any]) -> dict[str, Any]:
        bindings = _bindings_map(data)
        record = bindings.get(name)
        if not isinstance(record, dict):
            return {}
        if _same_minute(record.get("last_occurrence_at"), moment):
            return {}
        extra: dict[str, Any] = {}
        if active is not None:
            extra["run_id"] = active.get("workflow_run_id")
            extra["state"] = active.get("state")
        occurrence = _record_occurrence(record, moment, extra)
        occurrence_box.update(occurrence)
        if active is None:
            record["no_progress_count"] = 0
            return occurrence
        last_progress = _parse_dt(active.get("last_progress_at"))
        progressed = (
            last_progress is not None
            and previous_last is not None
            and last_progress > previous_last
        )
        if str(active.get("state") or "") == "awaiting-approval":
            record["parked_count"] = int(record.get("parked_count") or 0) + 1
        if progressed:
            record["no_progress_count"] = 0
        else:
            record["no_progress_count"] = int(record.get("no_progress_count") or 0) + 1
        occurrence["no_progress_count"] = record["no_progress_count"]
        occurrence["parked_count"] = record.get("parked_count")
        return occurrence

    fired = _mutate(
        config,
        _tick,
        action="workflow.occurrence",
        actor=HOOK_ACTOR,
        workflow_run_id=str((active or {}).get("workflow_run_id") or "") or None,
    )
    if not fired:
        return None

    def _observe(run_id: str) -> None:
        if not run_id:
            return
        try:
            from workflow_hooks import observe_and_advance

            observe_and_advance(run_id, config, now=moment)
        except Exception:
            return

    if active is None:
        snapshot = binding.get("workflow")
        owner = str(binding.get("session_id") or "cron").strip() or "cron"
        if isinstance(snapshot, Mapping):
            try:
                run = start_run(
                    name,
                    "cron",
                    owner,
                    workflow=snapshot,
                    config=config,
                    now=moment,
                    actor=HOOK_ACTOR,
                )
                fired["run_id"] = run.get("workflow_run_id")
                _observe(str(run.get("workflow_run_id") or ""))
            except WorkflowRunError:
                pass
        return fired

    run_id = str(active.get("workflow_run_id") or "")
    _observe(run_id)
    parked_now = None
    try:
        parked_now = _active_for_workflow(config, name)
    except Exception:
        parked_now = active
    if parked_now is not None and str(parked_now.get("state") or "") == "awaiting-approval":
        _record_wakeup_note(
            name,
            config,
            parked_now,
            moment,
            occurrence_count=int(fired.get("parked_count") or 0),
        )
        fired["wakeup_delivered"] = True
        fired["delivery_target"] = _deliver_target(config)

    no_progress = int(fired.get("no_progress_count") or 0)
    if no_progress >= NO_PROGRESS_THRESHOLD:
        run_id = str(active.get("workflow_run_id") or "")
        pending = []
        try:
            pending = list_pending_actions(config=config)
        except Exception:
            pending = []
        already = False
        for action in pending:
            if action.get("type") != "workflow-stalled":
                continue
            blob = str(action.get("summary") or "") + str(action.get("payload") or "")
            if run_id and run_id in blob:
                already = True
                break
        if not already:
            create_pending_action(
                config=config,
                action_type="workflow-stalled",
                provider="local",
                target=run_id or name,
                payload={
                    "run_id": run_id,
                    "count": no_progress,
                    "workflow_name": name,
                },
                summary=(
                    f"workflow-stalled: {run_id} has {no_progress} consecutive "
                    "no-progress occurrences"
                ),
            )
    return fired


def list_cron_bindings(*, config: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    doc = _load_doc(config)
    bindings = doc.get("bindings")
    if not isinstance(bindings, dict):
        return []
    items: list[dict[str, Any]] = []
    for name, record in bindings.items():
        if not isinstance(record, dict):
            continue
        copied = _copy_binding(record)
        copied.setdefault("workflow_name", name)
        items.append(copied)
    items.sort(key=lambda item: str(item.get("workflow_name") or ""))
    return items


def get_cron_binding(name: str, *, config: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    if not isinstance(name, str) or not name.strip():
        return None
    return _get_binding(config, name.strip())


def fire_by_schedule_id(
    schedule_id: str,
    config: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    sid = str(schedule_id or "").strip()
    if not sid:
        return None
    for binding in list_cron_bindings(config=config):
        if str(binding.get("schedule_id") or "") == sid:
            return fire_occurrence(str(binding.get("workflow_name") or ""), config, now=now)
        if str(binding.get("workflow_name") or "") == sid:
            return fire_occurrence(sid, config, now=now)
    return None


def check_cron_skill_files(
    fix: bool,
    data: dict[str, Any] | None,
    config_path: Path,
) -> CheckResult:
    del fix, config_path
    try:
        bindings = list_cron_bindings(config=_config_or_none(data))
    except Exception as exc:
        return CheckResult("cron_skill_files", "warn", f"cannot inspect cron bindings: {exc}")
    if not bindings:
        return CheckResult("cron_skill_files", "pass", "no cron bindings installed")
    missing: list[str] = []
    for binding in bindings:
        name = str(binding.get("workflow_name") or "").strip()
        schedule_id = str(binding.get("schedule_id") or name).strip()
        if not name:
            continue
        if not _plugin_skill_path(name).is_file():
            missing.append(schedule_id or name)
    if missing:
        named = ", ".join(missing)
        return CheckResult(
            "cron_skill_files",
            "warn",
            f"cron binding without installed skill file: {named}",
        )
    return CheckResult("cron_skill_files", "pass", "all cron bindings have skill files")


def check_stale_run(
    fix: bool,
    data: dict[str, Any] | None,
    config_path: Path,
    *,
    now: datetime | None = None,
) -> CheckResult:
    del fix, config_path
    try:
        runs = list_runs(config=_config_or_none(data))
    except Exception as exc:
        return CheckResult("stale_run", "warn", f"cannot inspect workflow runs: {exc}")
    moment = _aware(now)
    stale: list[str] = []
    for run in runs:
        if run.get("state") not in ACTIVE_STATES:
            continue
        try:
            flagged, step_name, age = is_stale(run, DEFAULT_STALE_HOURS, now=moment)
        except Exception:
            continue
        if not flagged:
            continue
        run_id = str(run.get("workflow_run_id") or "")
        hours = age.total_seconds() / 3600.0
        stale.append(
            f"stale run {run_id} stuck on {step_name or 'unknown step'} "
            f"for {hours:.1f}h (last progress)"
        )
    if not stale:
        return CheckResult("stale_run", "pass", "no stale active runs")
    return CheckResult("stale_run", "warn", "; ".join(stale))


def check_unhonored_advancement(
    fix: bool,
    data: dict[str, Any] | None,
    config_path: Path,
) -> CheckResult:
    del fix, config_path
    cfg = _config_or_none(data)
    try:
        runs = list_runs(config=cfg)
    except Exception as exc:
        return CheckResult("unhonored_advancement", "warn", f"cannot inspect workflow runs: {exc}")
    flagged: list[str] = []
    for run in runs:
        if run.get("state") != "awaiting-approval":
            continue
        step = _current_step(run)
        action_id = str((step or {}).get("action_id") or "").strip()
        try:
            succeeded = _action_succeeded(action_id, cfg)
        except Exception as exc:
            return CheckResult(
                "unhonored_advancement",
                "warn",
                f"cannot read review-queue action {action_id}: {exc}",
            )
        if not succeeded:
            continue
        run_id = str(run.get("workflow_run_id") or "")
        flagged.append(run_id)
    if not flagged:
        return CheckResult("unhonored_advancement", "pass", "no unhonored advancement")
    return CheckResult(
        "unhonored_advancement",
        "warn",
        f"unhonored advancement: executed success not advanced for {', '.join(flagged)}",
    )


def check_workflow_crons_doc(
    fix: bool,
    data: dict[str, Any] | None,
    config_path: Path,
) -> CheckResult:
    del fix
    cfg = _config_or_none(data)
    try:
        doc = load_store(STORE_CRONS, config=cfg, validate=False)
    except Exception as exc:
        return CheckResult("workflow_crons_doc", "fail", f"workflow_crons doc unreadable: {exc}")
    if not isinstance(doc, dict):
        return CheckResult("workflow_crons_doc", "fail", "workflow_crons doc corrupt")
    if "bindings" in doc and not isinstance(doc.get("bindings"), dict):
        return CheckResult("workflow_crons_doc", "fail", "workflow_crons doc corrupt: bindings is not a mapping")
    warnings = _cron_reconciliation_warnings(cfg, config_path)
    warnings.extend(_facts_doctor_warnings(cfg))
    if warnings:
        return CheckResult("workflow_crons_doc", "warn", "; ".join(warnings))
    return CheckResult("workflow_crons_doc", "pass", "workflow_crons doc ok")


def _workflows_dir(config: Mapping[str, Any] | None, config_path: Path | None = None) -> Path | None:
    if isinstance(config, Mapping):
        paths = config.get("paths")
        if isinstance(paths, Mapping) and paths.get("project_root"):
            return Path(str(paths["project_root"])).expanduser() / "workflows"
    if config_path is not None:
        try:
            from config_loader import get_project_root, load_config

            loaded = load_config(str(config_path), quiet=True)
            root = get_project_root(loaded) if loaded is not None else None
            if root is not None:
                return Path(root) / "workflows"
        except Exception:
            return None
    return None


def _scheduled_workflow_names(workflows_dir: Path | None) -> tuple[set[str], set[str]]:
    """Return (all yaml names, names that declare a cron schedule)."""
    names: set[str] = set()
    scheduled: set[str] = set()
    if workflows_dir is None or not workflows_dir.is_dir():
        return names, scheduled
    try:
        import yaml as _yaml
    except Exception:
        _yaml = None
    if _yaml is None:
        return names, scheduled
    for path in workflows_dir.glob("*.yaml"):
        names.add(path.stem)
        try:
            raw = _yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(raw, Mapping) and raw.get("name"):
            names.add(str(raw["name"]))
        try:
            workflow = validate_workflow(raw)
        except Exception:
            continue
        name = str(workflow.get("name") or path.stem)
        names.add(name)
        triggers = workflow.get("triggers")
        schedule = triggers.get("schedule") if isinstance(triggers, Mapping) else None
        if isinstance(schedule, Mapping) and str(schedule.get("cron") or "").strip():
            scheduled.add(name)
    return names, scheduled


def _cron_reconciliation_warnings(
    config: Mapping[str, Any] | None,
    config_path: Path,
) -> list[str]:
    warnings: list[str] = []
    try:
        bindings = list_cron_bindings(config=config)
    except Exception as exc:
        return [f"cannot inspect cron bindings: {exc}"]
    yaml_names, scheduled = _scheduled_workflow_names(_workflows_dir(config, config_path))
    bound_names = {
        str(item.get("workflow_name") or "").strip()
        for item in bindings
        if str(item.get("workflow_name") or "").strip()
    }
    for name in sorted(scheduled - bound_names):
        warnings.append(f"scheduled workflow without cron binding: {name}")
    for name in sorted(bound_names - yaml_names):
        warnings.append(f"cron binding whose workflow YAML was deleted: {name}")
    try:
        runs = list_runs(config=config)
    except Exception:
        runs = []
    for run in runs:
        if run.get("state") not in ACTIVE_STATES:
            continue
        name = str(run.get("workflow_name") or "").strip()
        if name and name not in yaml_names:
            run_id = str(run.get("workflow_run_id") or "")
            warnings.append(
                f"active run whose workflow disappeared: {run_id or name}"
            )
    return warnings


def _facts_doctor_warnings(config: Mapping[str, Any] | None) -> list[str]:
    """Report-only: missing/corrupt facts. Rebuilds a preview from YAML, does not write."""
    facts = None
    try:
        facts = get_facts(config=config)
    except Exception as exc:
        return [f"workflow_facts unreadable: {exc}"]
    yaml_names, _scheduled = _scheduled_workflow_names(_workflows_dir(config, None))
    if facts is None:
        if not yaml_names:
            return []
        try:
            from workflow_runs import build_facts

            build_facts(config)
        except Exception:
            pass
        return ["workflow_facts missing or corrupt; rebuilt preview from workflow YAML (not written)"]
    if not isinstance(facts, dict):
        return ["workflow_facts missing or corrupt; rebuilt preview from workflow YAML (not written)"]
    return []
