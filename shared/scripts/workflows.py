#!/usr/bin/env python3
"""Pure workflow schema validator, SKILL.md generator, and name-conflict check.

No I/O beyond a local directory existence check in ``check_skill_name_free``.
No imports from other Chief-of-Staff modules. Callers pass already-loaded
dicts; this module never reads YAML from disk.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

# Single-char names are valid; consecutive hyphens (a--b) are permitted.
# \A...\Z so .match() cannot accept a trailing newline (fullmatch is also used).
NAME_PATTERN = re.compile(r"\A[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")

WORKFLOW_NAME_MAX = 32
STEP_NAME_MAX = 24
STEP_ID_MAX = 24
STEPS_MAX = 20

TOP_LEVEL_REQUIRED = ("name", "description", "steps")
TOP_LEVEL_OPTIONAL = ("triggers", "delivery", "failure_policy", "run_log_delivery")
TOP_LEVEL_ALLOWED = frozenset(TOP_LEVEL_REQUIRED + TOP_LEVEL_OPTIONAL)
TOP_LEVEL_KEY_ORDER = (
    "name",
    "description",
    "triggers",
    "steps",
    "delivery",
    "failure_policy",
    "run_log_delivery",
)

STEP_REQUIRED = ("id", "name", "description")
SIGNAL_KEYS = ("command", "file", "review_queue", "manual")
STEP_OPTIONAL = ("required", "requires_approval")
STEP_ALLOWED = frozenset(STEP_REQUIRED + SIGNAL_KEYS + STEP_OPTIONAL)
STEP_KEY_ORDER = (
    "id",
    "name",
    "description",
    "command",
    "file",
    "review_queue",
    "manual",
    "required",
    "requires_approval",
)

TRIGGER_ALLOWED = frozenset({"message", "schedule"})
TRIGGER_KEY_ORDER = ("message", "schedule")
SCHEDULE_ALLOWED = frozenset({"cron", "timezone"})
SCHEDULE_KEY_ORDER = ("cron", "timezone")

COMMAND_ALLOWED = frozenset({"pattern"})
FILE_ALLOWED = frozenset({"path"})
REVIEW_QUEUE_ALLOWED = frozenset({"action_type"})

_DELIVERY_BLOCKS = (
    ("delivery", ("channel", "target")),
    ("failure_policy", ("on_failure",)),
    ("run_log_delivery", ("channel", "target")),
)
_KNOWN_DELIVERY_LABELS = {
    "channel": "Channel",
    "target": "Target",
    "on_failure": "On failure",
}


class WorkflowError(Exception):
    """Base error for workflow schema, generation, and name checks."""


class WorkflowValidationError(WorkflowError):
    """Invalid workflow document. Message includes the offending field name."""


class WorkflowNameConflict(WorkflowError):
    """Workflow name collides with a bundled or local skill directory."""


def validate_workflow(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate ``data`` and return a normalized copy. Does not mutate input."""
    if not isinstance(data, Mapping):
        raise WorkflowValidationError("workflow: must be a mapping")

    unknown = [key for key in data if key not in TOP_LEVEL_ALLOWED]
    if unknown:
        raise WorkflowValidationError(f"{unknown[0]!r}: unknown top-level key")

    for field in TOP_LEVEL_REQUIRED:
        if field not in data:
            raise WorkflowValidationError(f"{field}: required")

    name = _require_bounded_kebab(data.get("name"), "name", WORKFLOW_NAME_MAX)
    description = _require_non_empty_str(data.get("description"), "description")
    steps_in = data.get("steps")
    if not isinstance(steps_in, list):
        raise WorkflowValidationError("steps: must be a non-empty list")
    if not steps_in:
        raise WorkflowValidationError("steps: must be a non-empty list")
    if len(steps_in) > STEPS_MAX:
        raise WorkflowValidationError(f"steps: at most {STEPS_MAX} steps allowed")

    seen_ids: set[str] = set()
    steps: list[dict[str, Any]] = []
    for index, raw_step in enumerate(steps_in):
        steps.append(_normalize_step(raw_step, index, seen_ids))
    if not any(step["required"] for step in steps):
        raise WorkflowValidationError("steps: at least one required step")

    normalized: dict[str, Any] = {
        "name": name,
        "description": description,
    }
    if "triggers" in data:
        normalized["triggers"] = _normalize_triggers(data.get("triggers"))
    normalized["steps"] = steps
    for field in ("delivery", "failure_policy", "run_log_delivery"):
        if field in data:
            raw_block = data[field]
            if not isinstance(raw_block, Mapping):
                raise WorkflowValidationError(f"{field}: must be a mapping")
            normalized[field] = _canonical_copy(raw_block, field)
    return _ordered(normalized, TOP_LEVEL_KEY_ORDER)


def generate_skill_md(workflow: Mapping[str, Any]) -> str:
    """Render deterministic SKILL.md markdown from a validated workflow dict."""
    if not isinstance(workflow, Mapping):
        raise WorkflowValidationError("workflow: must be a mapping")
    for field in ("name", "description", "steps"):
        if field not in workflow:
            raise WorkflowValidationError(f"{field}: required")
    steps = workflow["steps"]
    if not isinstance(steps, list):
        raise WorkflowValidationError("steps: must be a list")

    name = workflow["name"]
    description = workflow["description"]
    quoted = _quote_frontmatter_description(description)
    header = (
        f"<!-- Generated from workflows/{name}.yaml. "
        f"Regenerate with: chief_of_staff.py workflows generate-skill {name} -->"
    )
    lines = [
        "---",
        f"name: {name}",
        f"description: {quoted}",
        "---",
        header,
        "",
        f"# {name}",
        "",
        str(description),
        "",
        "## Steps",
        "",
    ]
    for index, step in enumerate(steps, start=1):
        lines.extend(_render_step(index, step))
        lines.append("")
    lines.extend(_render_delivery_section(workflow))
    if lines[-1] != "":
        lines.append("")
    return "\n".join(lines)


def check_skill_name_free(workflow_name: str, plugin_root: str | Path) -> None:
    """Refuse names that shadow ``skills/`` or ``skills.local/`` directories."""
    _require_bounded_kebab(workflow_name, "name", WORKFLOW_NAME_MAX)
    root = Path(plugin_root)
    bundled = root / "skills" / workflow_name
    local = root / "skills.local" / workflow_name
    if bundled.is_dir() or local.is_dir():
        raise WorkflowNameConflict(
            f"{workflow_name}: conflicts with an existing skill directory"
        )


def _normalize_step(raw_step: Any, index: int, seen_ids: set[str]) -> dict[str, Any]:
    prefix = f"steps[{index}]"
    if not isinstance(raw_step, Mapping):
        raise WorkflowValidationError(f"{prefix}: must be a mapping")

    unknown = [key for key in raw_step if key not in STEP_ALLOWED]
    if unknown:
        raise WorkflowValidationError(f"{prefix}.{unknown[0]!r}: unknown key")

    for field in STEP_REQUIRED:
        if field not in raw_step:
            raise WorkflowValidationError(f"{prefix}.{field}: required")

    step_id = _require_bounded_kebab(raw_step.get("id"), f"{prefix}.id", STEP_ID_MAX)
    if step_id in seen_ids:
        raise WorkflowValidationError(f"{prefix}.id: duplicate step id '{step_id}'")
    seen_ids.add(step_id)

    step_name = _require_bounded_str(raw_step.get("name"), f"{prefix}.name", STEP_NAME_MAX)
    description = _require_non_empty_str(raw_step.get("description"), f"{prefix}.description")

    present_signals = [key for key in SIGNAL_KEYS if key in raw_step]
    if len(present_signals) != 1:
        raise WorkflowValidationError(
            f"{prefix}: exactly one completion signal required "
            f"(command, file, review_queue, or manual)"
        )
    signal_key = present_signals[0]
    signal_payload = _normalize_signal(prefix, signal_key, raw_step[signal_key])

    required = _optional_bool(raw_step.get("required", True), f"{prefix}.required")
    default_approval = True if signal_key == "review_queue" else False
    requires_approval = _optional_bool(
        raw_step.get("requires_approval", default_approval), f"{prefix}.requires_approval"
    )
    if requires_approval and signal_key != "review_queue":
        raise WorkflowValidationError(
            f"{prefix}.requires_approval: requires a review_queue signal"
        )

    step: dict[str, Any] = {
        "id": step_id,
        "name": step_name,
        "description": description,
        signal_key: signal_payload,
        "required": required,
        "requires_approval": requires_approval,
    }
    return _ordered(step, STEP_KEY_ORDER)


def _normalize_signal(prefix: str, signal_key: str, payload: Any) -> Any:
    field = f"{prefix}.{signal_key}"
    if signal_key == "manual":
        if payload is True:
            return True
        if isinstance(payload, Mapping) and dict(payload) == {}:
            return {}
        raise WorkflowValidationError(f"{field}: must be true or an empty mapping")

    if not isinstance(payload, Mapping):
        raise WorkflowValidationError(f"{field}: must be a mapping")

    if signal_key == "command":
        _reject_unknown_keys(payload, COMMAND_ALLOWED, field)
        pattern = _require_code_span_str(payload.get("pattern"), f"{field}.pattern")
        return {"pattern": pattern}

    if signal_key == "file":
        _reject_unknown_keys(payload, FILE_ALLOWED, field)
        path = _require_code_span_str(payload.get("path"), f"{field}.path")
        return {"path": _require_project_relative_path(path, f"{field}.path")}

    _reject_unknown_keys(payload, REVIEW_QUEUE_ALLOWED, field)
    action_type = _require_code_span_str(payload.get("action_type"), f"{field}.action_type")
    return {"action_type": action_type}


def _normalize_triggers(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WorkflowValidationError("triggers: must be a mapping")
    unknown = [key for key in raw if key not in TRIGGER_ALLOWED]
    if unknown:
        raise WorkflowValidationError(f"triggers.{unknown[0]!r}: unknown key")

    triggers: dict[str, Any] = {}
    if "message" in raw:
        messages = raw.get("message")
        if not isinstance(messages, list) or not all(isinstance(item, str) for item in messages):
            raise WorkflowValidationError("triggers.message: must be a list of non-empty strings")
        stripped = [item.strip() for item in messages]
        if not all(stripped):
            raise WorkflowValidationError("triggers.message: must be a list of non-empty strings")
        triggers["message"] = stripped
    if "schedule" in raw:
        triggers["schedule"] = _normalize_schedule(raw.get("schedule"))
    return _ordered(triggers, TRIGGER_KEY_ORDER)


def _normalize_schedule(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WorkflowValidationError("triggers.schedule: must be a mapping")
    unknown = [key for key in raw if key not in SCHEDULE_ALLOWED]
    if unknown:
        raise WorkflowValidationError(f"triggers.schedule.{unknown[0]!r}: unknown key")
    if "cron" not in raw:
        raise WorkflowValidationError("triggers.schedule.cron: required")
    cron = _require_non_empty_str(raw.get("cron"), "triggers.schedule.cron")
    timezone = raw.get("timezone", None)
    if timezone is not None and not isinstance(timezone, str):
        raise WorkflowValidationError("triggers.schedule.timezone: must be a string or null")
    if isinstance(timezone, str):
        timezone = timezone.strip()
    return _ordered({"cron": cron, "timezone": timezone}, SCHEDULE_KEY_ORDER)


def _render_step(index: int, step: Mapping[str, Any]) -> list[str]:
    lines = [f"{index}. **{step['name']}** — {step['description']}"]
    if "command" in step:
        pattern = step["command"]["pattern"]
        lines.append(f"   Completion signal: command pattern `{pattern}`.")
    elif "file" in step:
        path = step["file"]["path"]
        lines.append(f"   Completion signal: file `{path}`.")
    elif "review_queue" in step:
        action_type = step["review_queue"]["action_type"]
        lines.append(
            f"   Completion signal: review_queue action `{action_type}`. "
            "Advancement follows the existing claim / record-execution lifecycle."
        )
    elif "manual" in step:
        lines.append(
            "   Completion signal: manual. Advance explicitly via `workflows advance`."
        )
    if step.get("requires_approval"):
        lines.append("   This step requires approval before the bound action may execute.")
    if step.get("required") is False:
        lines.append(
            "   This step is optional; if inputs are unavailable it may be skipped (degraded)."
        )
    return lines


def _render_delivery_section(workflow: Mapping[str, Any]) -> list[str]:
    if not any(key in workflow for key in ("delivery", "failure_policy", "run_log_delivery")):
        return []
    lines = ["## Delivery", ""]
    for block_key, preferred in _DELIVERY_BLOCKS:
        block = workflow.get(block_key)
        if not isinstance(block, Mapping):
            continue
        rendered: list[str] = [key for key in preferred if key in block]
        for key in sorted(k for k in block if k not in preferred):
            rendered.append(key)
        for key in rendered:
            label = _KNOWN_DELIVERY_LABELS.get(key, key)
            lines.append(f"{label}: {block[key]}")
    lines.append("")
    return lines


def _quote_frontmatter_description(description: Any) -> str:
    text = " ".join(str(description).split())
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _require_bounded_kebab(value: Any, field: str, max_len: int) -> str:
    text = _require_bounded_str(value, field, max_len)
    if NAME_PATTERN.fullmatch(text) is None:
        raise WorkflowValidationError(f"{field}: must be lowercase kebab-case")
    return text


def _require_bounded_str(value: Any, field: str, max_len: int) -> str:
    text = _require_non_empty_str(value, field)
    if len(text) > max_len:
        raise WorkflowValidationError(f"{field}: must be at most {max_len} characters")
    return text


def _require_non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise WorkflowValidationError(f"{field}: must be a non-empty string")
    if _has_disallowed_control_chars(value):
        raise WorkflowValidationError(f"{field}: must be a single line without control characters")
    text = value.strip()
    if text == "":
        raise WorkflowValidationError(f"{field}: must be a non-empty string")
    return text


def _require_code_span_str(value: Any, field: str) -> str:
    text = _require_non_empty_str(value, field)
    if "`" in text:
        raise WorkflowValidationError(f"{field}: must not contain backticks")
    return text


def _require_project_relative_path(path: str, field: str) -> str:
    if path.startswith("/"):
        raise WorkflowValidationError(f"{field}: must be a relative path")
    drive_letter = len(path) >= 2 and path[0].isalpha() and path[1] == ":"
    if path.startswith("~") or "\\" in path or drive_letter or ".." in PurePosixPath(path).parts:
        raise WorkflowValidationError(f"{field}: must stay within the project root")
    return path


def _has_disallowed_control_chars(text: str) -> bool:
    return any(ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F for ch in text)


def _optional_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise WorkflowValidationError(f"{field}: must be a boolean")
    return value


def _reject_unknown_keys(payload: Mapping[str, Any], allowed: frozenset[str], field: str) -> None:
    unknown = [key for key in payload if key not in allowed]
    if unknown:
        raise WorkflowValidationError(f"{field}.{unknown[0]!r}: unknown key")


def _canonical_copy(value: Any, field: str) -> Any:
    if isinstance(value, Mapping):
        keys = list(value)
        for key in keys:
            if not isinstance(key, str):
                raise WorkflowValidationError(f"{field}: keys must be strings")
        return {key: _canonical_copy(value[key], field) for key in sorted(keys)}
    if isinstance(value, list):
        return [_canonical_copy(item, field) for item in value]
    return value


def _ordered(data: Mapping[str, Any], key_order: tuple[str, ...]) -> dict[str, Any]:
    return {key: data[key] for key in key_order if key in data}
