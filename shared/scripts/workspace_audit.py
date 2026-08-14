#!/usr/bin/env python3
"""Audit trail for workspace write actions.

Records gmail_create_draft, calendar_create, calendar_update,
drive_upload, drive_download to project_root/.audit/workspace.log as JSONL.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    import file_lock
except Exception:  # pragma: no cover - lock is best-effort
    file_lock = None  # type: ignore[assignment]


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


_audit_log_path = _audit_path


def _hash_record(prev_hash: str, record: Mapping[str, Any]) -> str:
    """sha256(prev_hash + canonical JSON of the record without ``_hash``)."""
    payload = prev_hash + json.dumps(dict(record), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _last_hash(path: Path) -> str:
    """Return the ``_hash`` of the last JSONL record, or ``""`` if none."""
    if not path.exists() or path.stat().st_size == 0:
        return ""
    last_line = ""
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                last_line = line
    if not last_line:
        return ""
    try:
        rec = json.loads(last_line)
    except json.JSONDecodeError:
        return ""
    if isinstance(rec, dict):
        return str(rec.get("_hash") or "")
    return ""


def verify_audit_chain(config: Any) -> bool:
    """Return True iff every record's ``_hash`` matches the hash chain.

    The first record uses ``prev_hash = ""``. Any missing hash, malformed
    line, or content/hash mismatch returns False.
    """
    path = _audit_path(config)
    if not path.exists():
        return True
    prev_hash = ""
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return False
                if not isinstance(record, dict):
                    return False
                stored = record.get("_hash")
                if not stored:
                    return False
                content = {k: v for k, v in record.items() if k != "_hash"}
                expected = _hash_record(prev_hash, content)
                if stored != expected:
                    return False
                prev_hash = stored
    except OSError:
        return False
    return True


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

        def _append() -> None:
            prev_hash = _last_hash(path)
            to_write = dict(record)
            to_write["_hash"] = _hash_record(prev_hash, to_write)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(to_write) + "\n")

        if file_lock is not None:
            with file_lock.with_lock(str(path), timeout=10):
                _append()
        else:
            _append()
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