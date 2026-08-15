#!/usr/bin/env python3
"""Standalone helpers extracted from chief_of_staff.py.

These utilities have no command-routing or panel-collection logic; they
exist so the entrypoint can stay focused on orchestration.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safety_block() -> dict[str, Any]:
    return {
        "read_only": True,
        "approved_actions_executed": 0,
        "provider_writes": 0,
        "pipeline_mutations": 0,
        "invoice_writes": 0,
    }


def _safe_load_config(config_path: str | None) -> Any:
    """Load config via config_loader; never raise."""
    try:
        from config_loader import load_config
    except Exception:
        return None
    try:
        return load_config(config_path)
    except Exception:
        return None


def _resolve_project_root(config: Any) -> Path | None:
    if config is None:
        return None
    try:
        from config_loader import get_project_root
        root = get_project_root(config)
        if root is not None:
            return Path(root)
    except Exception:
        pass
    try:
        if isinstance(config, Mapping):
            paths = config.get("paths", {})
            if isinstance(paths, Mapping) and paths.get("project_root"):
                return Path(str(paths["project_root"])).expanduser()
    except Exception:
        return None
    return None


def _resolve_hermes_home() -> Path | None:
    try:
        from config_loader import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        pass
    env = os.getenv("CHIEF_OF_STAFF_HERMES_HOME") or os.getenv("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def _get_action_risk(action_type: str) -> str:
    try:
        from action_risk import get_action_risk
        return str(get_action_risk(action_type or ""))
    except Exception:
        return "unknown"


def _call_collector(fn_name: str, config: Any, *args: Any, default: Any = None) -> Any:
    """Call a briefing_sources collector, returning default on any failure."""
    if default is None and fn_name.endswith("stats"):
        default = {}
    if default is None:
        default = [] if "actions" in fn_name or "events" in fn_name else {}
    try:
        import briefing_sources
    except Exception:
        return default
    fn = getattr(briefing_sources, fn_name, None)
    if fn is None:
        return default
    try:
        return fn(config, *args)
    except Exception:
        return default


def _indent_lines(text: str, prefix: str = "  ") -> str:
    return "\n".join(f"{prefix}{line}" if line else prefix.rstrip() for line in text.splitlines())


def _json_dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)

# ---------------------------------------------------------------------------
# Demo fixture re-anchoring
# ---------------------------------------------------------------------------

_DEMO_TIMESTAMP_KEYS = ("date", "start", "end", "generated_at")


def _reanchor_demo_envelope(envelope: Any) -> Any:
    """Shift the bundled sample envelope so its anchor day becomes today.

    ``examples/sample-workspace.json`` is a hand-written fixture anchored on a
    fixed day. Rendered verbatim, the demo prints months-old timestamps under
    headings like "next 48h", which is the first thing a new user sees. Shift
    every timestamp by whole days so time-of-day and the relative spacing
    between records are preserved while "today" is genuinely today.

    Best-effort: an envelope without a parseable ``generated_at``, or an
    individual value that will not parse, is left untouched.
    """
    if not isinstance(envelope, dict):
        return envelope

    anchor_raw = envelope.get("generated_at")
    anchor = _parse_demo_ts(anchor_raw)
    if anchor is None:
        return envelope

    shift = timedelta(days=(datetime.now(timezone.utc).date() - anchor.date()).days)
    if not shift:
        return envelope

    def _shift(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: (_shift_ts(v, shift) if k in _DEMO_TIMESTAMP_KEYS else _shift(v))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [_shift(v) for v in value]
        return value

    return _shift(envelope)


def _parse_demo_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _shift_ts(raw: Any, shift: timedelta) -> Any:
    parsed = _parse_demo_ts(raw)
    if parsed is None:
        return raw
    shifted = parsed + shift
    # Preserve the fixture's trailing-Z spelling rather than +00:00.
    if isinstance(raw, str) and raw.endswith("Z"):
        return shifted.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return shifted.isoformat()
