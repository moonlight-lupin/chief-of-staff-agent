#!/usr/bin/env python3
"""Standalone helpers extracted from chief_of_staff.py.

These utilities have no command-routing or panel-collection logic; they
exist so the entrypoint can stay focused on orchestration.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
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
