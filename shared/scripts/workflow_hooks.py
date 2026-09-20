#!/usr/bin/env python3
"""Workflow orchestrator hooks: pointer strip (pre_llm_call) and advancement (post_tool_call).

Fail-soft: any exception inside a hook returns None. Command/file advancement is
event-driven; review_queue steps are re-observed via observe_and_advance.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from state_db import ConcurrencyError, get_pending_action, load_store
from workflow_runs import (
    ACTIVE_STATES,
    HOOK_ACTOR,
    STORE_NAME as STORE_RUNS,
    advance_run,
    get_facts,
    get_run,
    skip_step,
)

STORE_FACTS = "workflow_facts"
DEFAULT_FACTS_MAX_AGE_HOURS = 1
STRIP_MAX = 200
GATE_PHRASE = "[APPROVAL REQUIRED — propose, do not execute]"
APPROVE_CMD_PREFIX = "review_queue.py approve --action-id"
VERDICTS = frozenset({"GO", "DEGRADED", "HALT"})


def pointer_strip(context: dict | None = None, **kwargs: Any) -> str | None:
    """pre_llm_call: inject the owning session's pointer + verdict, or None."""
    try:
        return _pointer_strip(context, **kwargs)
    except Exception:
        return None


def advancement(
    tool_name: str = "",
    args: dict | None = None,
    result: str = "",
    context: dict | None = None,
    **kwargs: Any,
) -> str | None:
    """post_tool_call: advance the current step when its completion signal is met."""
    try:
        return _advancement(tool_name, args, result, context, **kwargs)
    except Exception:
        return None


def compute_verdict(
    run: Mapping[str, Any],
    facts: Mapping[str, Any] | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Bounded local preflight. Missing/empty/stale facts never yield GO."""
    if not isinstance(facts, Mapping) or not facts:
        return {"verdict": "DEGRADED"}

    missing_required: list[str] = []
    skip: list[str] = []
    capabilities = facts.get("capabilities")
    if not isinstance(capabilities, Mapping):
        capabilities = {}
    for step in _steps(run):
        if not isinstance(step, Mapping):
            continue
        review = step.get("review_queue")
        if not isinstance(review, Mapping):
            continue
        action_type = str(review.get("action_type") or "").strip()
        if not action_type:
            continue
        present = bool(capabilities.get(action_type))
        if present:
            continue
        if step.get("required", True) is not False:
            missing_required.append(action_type)
        else:
            step_id = str(step.get("id") or "").strip()
            if step_id:
                skip.append(step_id)

    if missing_required:
        named = missing_required[0]
        return {"verdict": "HALT", "missing": named, "reason": named}

    state_files = facts.get("state_files")
    state_absent = isinstance(state_files, Mapping) and any(
        value is False for value in state_files.values()
    )
    stale = _facts_are_stale(facts, now=now)
    if skip or state_absent or stale:
        result: dict[str, Any] = {"verdict": "DEGRADED"}
        if skip:
            result["skip"] = skip
            result["skip_list"] = list(skip)
        return result
    return {"verdict": "GO"}


def observe_and_advance(
    run_id: str,
    config: Mapping[str, Any] | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Re-observe the current review_queue step and advance if executed+success."""
    run = get_run(run_id, config=config)
    if not isinstance(run, dict) or run.get("state") not in ACTIVE_STATES:
        return run
    index = _current_index(run)
    step = _step_at(run, index)
    if step is None or "review_queue" not in step:
        return run
    if not _review_queue_succeeded(step, config):
        return run
    return advance_run(
        run_id,
        index,
        {"kind": "review_queue"},
        config=config,
        now=now,
        actor=HOOK_ACTOR,
    )


def _pointer_strip(context: dict | None, **kwargs: Any) -> str | None:
    session_id = _session_id(context)
    if not session_id:
        return None
    config = _resolve_config(kwargs.get("config"))
    now = kwargs.get("now")
    run = _owned_active_run(config, session_id)
    if run is None:
        return None
    step = _step_at(run, _current_index(run))
    if step is not None and "review_queue" in step:
        observe_and_advance(str(run.get("workflow_run_id") or ""), config, now=now)
        run = _owned_active_run(config, session_id)
        if run is None:
            return None
    facts = _load_facts(config)
    verdict = compute_verdict(run, facts, now=now)
    return _render_strip(run, verdict)


def _advancement(
    tool_name: str,
    args: dict | None,
    result: Any,
    context: dict | None,
    **kwargs: Any,
) -> str | None:
    session_id = _session_id(context)
    if not session_id:
        return None
    config = _resolve_config(kwargs.get("config"))
    now = kwargs.get("now")
    run = _owned_active_run(config, session_id)
    if run is None:
        return None
    index = _current_index(run)
    step = _step_at(run, index)
    if step is None:
        return None
    payload = args if isinstance(args, dict) else {}
    if not _event_matches_step(
        step,
        tool_name,
        payload,
        run,
        config,
        now=now,
        exit_code=kwargs.get("exit_code"),
        result=result,
    ):
        return None
    run_id = str(run.get("workflow_run_id") or "")
    try:
        advanced = advance_run(
            run_id,
            index,
            {"kind": _signal_key(step) or "event"},
            config=config,
            now=now,
            actor=HOOK_ACTOR,
        )
    except ConcurrencyError:
        return None
    if isinstance(advanced, Mapping):
        _apply_degraded_skips(advanced, config, now=now)
    return None


def _event_matches_step(
    step: Mapping[str, Any],
    tool_name: str,
    args: Mapping[str, Any],
    run: Mapping[str, Any],
    config: Mapping[str, Any] | None,
    *,
    now: datetime | None,
    exit_code: Any,
    result: Any = None,
) -> bool:
    if "manual" in step:
        return False
    if "command" in step:
        return _command_matches(
            step, tool_name, args, run, now=now, exit_code=exit_code, result=result
        )
    if "file" in step:
        return _file_matches(step, tool_name, args, run, config)
    if "review_queue" in step:
        return _review_queue_succeeded(step, config)
    return False


def _command_matches(
    step: Mapping[str, Any],
    tool_name: str,
    args: Mapping[str, Any],
    run: Mapping[str, Any],
    *,
    now: datetime | None,
    exit_code: Any,
    result: Any = None,
) -> bool:
    if tool_name != "terminal" or not _command_succeeded(result, exit_code):
        return False
    command = str(args.get("command") or "")
    pattern = ""
    raw = step.get("command")
    if isinstance(raw, Mapping):
        pattern = str(raw.get("pattern") or "")
    if not pattern or pattern not in command:
        return False
    started = _parse_dt(run.get("started_at"))
    event_time = _aware(now)
    if started is not None and event_time < started:
        return False
    return True


def _command_succeeded(result: Any, exit_code: Any) -> bool:
    """exit_code kwarg wins when present; otherwise parse result (HOOKS.md post_tool_call)."""
    if exit_code is not None:
        try:
            return int(exit_code) == 0
        except (TypeError, ValueError):
            return False
    if result is None:
        return False
    if isinstance(result, Mapping):
        if "exit_code" in result:
            return _command_succeeded(None, result.get("exit_code"))
        if "returncode" in result:
            return _command_succeeded(None, result.get("returncode"))
        if "success" in result:
            return bool(result.get("success"))
        return False
    code = getattr(result, "exit_code", None)
    if code is None:
        code = getattr(result, "returncode", None)
    if code is not None:
        return _command_succeeded(None, code)
    if isinstance(result, str):
        text = result.strip()
        if not text:
            return False
        if text[:1] in "{[":
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if parsed is not None:
                return _command_succeeded(parsed, None)
        lowered = text.lower()
        if lowered in {"failed", "error", "failure"}:
            return False
        if "traceback" in lowered or "exit_code=1" in lowered or "returncode=1" in lowered:
            return False
        return True
    return False


def _apply_degraded_skips(
    run: Mapping[str, Any],
    config: Mapping[str, Any] | None,
    *,
    now: datetime | None,
) -> None:
    """After advancement, skip the current step when a DEGRADED verdict names it."""
    facts = _load_facts(config)
    current = dict(run)
    seen: set[str] = set()
    while isinstance(current, Mapping) and current.get("state") in ACTIVE_STATES:
        verdict = compute_verdict(current, facts, now=now)
        skip_ids = [
            str(item).strip()
            for item in (verdict.get("skip") or verdict.get("skip_list") or [])
            if str(item).strip()
        ]
        if not skip_ids:
            return
        step = _step_at(current, _current_index(current))
        if step is None:
            return
        step_id = str(step.get("id") or "").strip()
        if not step_id or step_id not in skip_ids or step_id in seen:
            return
        seen.add(step_id)
        try:
            current = skip_step(
                str(current.get("workflow_run_id") or ""),
                _current_index(current),
                "preflight degraded",
                config=config,
                now=now,
                actor=HOOK_ACTOR,
            )
        except ConcurrencyError:
            return
        except Exception:
            return


def _file_matches(
    step: Mapping[str, Any],
    tool_name: str,
    args: Mapping[str, Any],
    run: Mapping[str, Any],
    config: Mapping[str, Any] | None,
) -> bool:
    if tool_name != "write_file":
        return False
    declared = ""
    raw = step.get("file")
    if isinstance(raw, Mapping):
        declared = str(raw.get("path") or "")
    if not declared:
        return False
    root = _project_root(config)
    if root is None:
        return False
    target = _resolve_under_root(root, declared)
    if target is None:
        return False
    event_path = str(args.get("path") or "")
    if not event_path:
        return False
    event_target = _resolve_under_root(root, event_path)
    if event_target is None or event_target != target:
        return False
    if not target.is_file():
        return False
    started = _parse_dt(run.get("started_at"))
    mtime = datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc)
    if started is not None and mtime < started:
        return False
    return True


def _review_queue_succeeded(step: Mapping[str, Any], config: Mapping[str, Any] | None) -> bool:
    action_id = str(step.get("action_id") or "").strip()
    if not action_id:
        return False
    action = get_pending_action(config, action_id)
    if not isinstance(action, dict) or action.get("state") != "executed":
        return False
    result = action.get("result")
    if not isinstance(result, Mapping):
        return False
    return bool(result.get("success"))


def _owned_active_run(config: Mapping[str, Any] | None, session_id: str) -> dict[str, Any] | None:
    data = load_store(STORE_RUNS, config=config, validate=False)
    if not isinstance(data, dict):
        return None
    runs = data.get("runs")
    if not isinstance(runs, dict):
        return None
    for run in runs.values():
        if not isinstance(run, dict):
            continue
        if run.get("session_id") == session_id and run.get("state") in ACTIVE_STATES:
            return run
    return None


def _load_facts(config: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return get_facts(config=config)


def _facts_are_stale(facts: Mapping[str, Any], now: datetime | None) -> bool:
    refreshed = _parse_dt(facts.get("refreshed_at"))
    if refreshed is None:
        return True
    raw_age = facts.get("facts_max_age_hours", DEFAULT_FACTS_MAX_AGE_HOURS)
    try:
        hours = float(raw_age)
    except (TypeError, ValueError):
        hours = float(DEFAULT_FACTS_MAX_AGE_HOURS)
    return _aware(now) - refreshed >= timedelta(hours=hours)


def _render_strip(run: Mapping[str, Any], verdict_info: Mapping[str, Any]) -> str:
    name = str(run.get("workflow_name") or "").strip()
    steps = _steps(run)
    index = _current_index(run)
    n_of_n = f"{index + 1}/{max(len(steps), 1)}"
    state = str(run.get("state") or "running")
    verdict = str(verdict_info.get("verdict") or "DEGRADED")
    if verdict not in VERDICTS:
        verdict = "DEGRADED"
    extra = _verdict_extra(verdict_info)
    last_name, last_ts = _last_completed(run, steps, index)
    next_action = _next_action_text(_step_at(run, index))
    return _fit_strip(name, n_of_n, state, verdict, extra, last_name, last_ts, next_action)


def _verdict_extra(verdict_info: Mapping[str, Any]) -> str:
    verdict = str(verdict_info.get("verdict") or "")
    if verdict == "HALT":
        named = str(verdict_info.get("missing") or verdict_info.get("reason") or "").strip()
        return named
    if verdict == "DEGRADED":
        skip = verdict_info.get("skip") or verdict_info.get("skip_list") or []
        ids = [str(item) for item in skip if str(item).strip()]
        if ids:
            return "skip:" + ",".join(ids)
    return ""


def _last_completed(
    run: Mapping[str, Any],
    steps: list[Any],
    current_index: int,
) -> tuple[str, str]:
    status = run.get("step_status")
    if not isinstance(status, list):
        return "", ""
    last_i: int | None = None
    for i, token in enumerate(status):
        if i >= current_index:
            break
        if token in ("completed", "skipped"):
            last_i = i
    if last_i is None or last_i >= len(steps) or not isinstance(steps[last_i], Mapping):
        return "", ""
    step = steps[last_i]
    last_name = str(step.get("name") or step.get("id") or "").strip()
    last_ts = str(run.get("last_progress_at") or "").strip()
    return last_name, last_ts


def _next_action_text(step: Mapping[str, Any] | None) -> str:
    if step is None:
        return "complete"
    label = str(step.get("id") or step.get("name") or "").strip() or "step"
    if "review_queue" in step or step.get("requires_approval"):
        action_id = str(step.get("action_id") or "").strip()
        if action_id:
            return (
                f"action {action_id} bound — await operator approval; "
                f"operator: {APPROVE_CMD_PREFIX} {action_id}"
            )
        return f"{label} {GATE_PHRASE} propose bind-action"
    return label


def _fit_strip(
    name: str,
    n_of_n: str,
    state: str,
    verdict: str,
    extra: str,
    last_name: str,
    last_ts: str,
    next_action: str,
) -> str:
    next_part = f"next: {next_action}"
    last_with_ts = f"last:{last_name}@{last_ts}" if last_name and last_ts else ""
    last_only = f"last:{last_name}" if last_name else ""
    candidates = (
        _join(name, n_of_n, state, verdict, extra, last_with_ts, next_part),
        _join(name, n_of_n, state, verdict, extra, last_only, next_part),
        _join(name, n_of_n, state, verdict, extra, next_part),
        _join(n_of_n, state, verdict, extra, next_part),
        _join(n_of_n, verdict, extra, next_part),
        _join(n_of_n, verdict, next_part),
    )
    for strip in candidates:
        if len(strip) <= STRIP_MAX:
            return strip
    return candidates[-1][:STRIP_MAX]


def _join(*parts: str) -> str:
    return " ".join(part for part in parts if part)


def _steps(run: Mapping[str, Any]) -> list[Any]:
    definition = run.get("definition")
    if not isinstance(definition, Mapping):
        return []
    steps = definition.get("steps")
    return list(steps) if isinstance(steps, list) else []


def _current_index(run: Mapping[str, Any]) -> int:
    try:
        return int(run.get("current_step_index") or 0)
    except (TypeError, ValueError):
        return 0


def _step_at(run: Mapping[str, Any], index: int) -> dict[str, Any] | None:
    steps = _steps(run)
    if index < 0 or index >= len(steps) or not isinstance(steps[index], dict):
        return None
    return steps[index]


def _signal_key(step: Mapping[str, Any]) -> str | None:
    for key in ("command", "file", "review_queue", "manual"):
        if key in step:
            return key
    return None


def _session_id(context: dict | None) -> str:
    if not isinstance(context, dict):
        return ""
    value = context.get("session_id")
    if not isinstance(value, str):
        return ""
    return value.strip()


def _resolve_config(config: Any) -> Mapping[str, Any] | None:
    if isinstance(config, Mapping):
        return config
    try:
        from config_loader import load_config

        loaded = load_config()
    except Exception:
        return None
    return loaded if isinstance(loaded, Mapping) else None


def _project_root(config: Mapping[str, Any] | None) -> Path | None:
    resolved = config if isinstance(config, Mapping) else _resolve_config(config)
    if not isinstance(resolved, Mapping):
        return None
    paths = resolved.get("paths")
    if not isinstance(paths, Mapping):
        return None
    raw = paths.get("project_root")
    if not raw:
        return None
    return Path(str(raw)).expanduser()


def _resolve_under_root(root: Path, relative: str) -> Path | None:
    raw = str(relative).strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _aware(now: datetime | None) -> datetime:
    dt = now if now is not None else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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
