#!/usr/bin/env python3
"""Audit trail for workspace write actions.

Records gmail_create_draft, calendar_create, calendar_update,
drive_upload, drive_download to project_root/.audit/workspace.log as JSONL.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def _audit_path(config: Any) -> Path:
    """Return path to .audit/workspace.log under project root."""
    project_root = None
    if isinstance(config, Mapping):
        paths = config.get("paths", {})
        if isinstance(paths, Mapping):
            project_root = paths.get("project_root")
    if not project_root:
        project_root = os.getenv("CHIEF_OF_STAFF_PROJECT_ROOT",
                                  str(Path.home() / ".hermes" / "projects" / "default"))
    return Path(str(project_root)).expanduser() / ".audit" / "workspace.log"


def audit_workspace_action(
    config: Any,
    provider: str,
    operation: str,
    tool: str,
    target: str = "",
    status: str = "success",
    extra: dict[str, Any] | None = None,
) -> None:
    """Append a workspace audit record as JSONL.

    Best-effort: if the audit directory doesn't exist or write fails,
    log to stderr but don't crash the operation.
    """
    record: dict[str, Any] = {
        "provider": provider,
        "operation": operation,
        "tool": tool,
        "target": target,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        record["extra"] = extra

    # ── Audit → operational-log linkage (v0.3.4, additive) ────────────────
    # Attach the active run id so an operational (runtime) log can be correlated
    # with this audit (what changed) record. Records written outside a run omit
    # the field, so existing consumers are unaffected. Surface action_id from
    # ``extra`` to the top level when present (a stable cross-log identifier).
    # The audit log and the operational log remain SEPARATE files.
    try:
        from runtime_log import current_run_id
        run_id = current_run_id()
    except Exception:
        run_id = None
    if run_id is not None:
        record["run_id"] = run_id
    if extra and isinstance(extra, dict) and extra.get("action_id") is not None:
        record["action_id"] = extra["action_id"]

    try:
        path = _audit_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as exc:
        print(f"Warning: workspace audit write failed: {exc}", file=sys.stderr)
        _audit_write_failed(exc)


def _audit_write_failed(exc: Exception) -> None:
    """Emit an ``audit_write_failed`` operational event (no record contents, no
    path) when an audit write fails. Best-effort; never raises."""
    try:
        from runtime_log import log_event
        log_event(
            "audit_write_failed", level="error", component="audit",
            reason=type(exc).__name__,
        )
    except Exception:  # pragma: no cover - logging must never break the caller
        pass