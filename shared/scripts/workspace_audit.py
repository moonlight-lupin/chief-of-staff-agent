#!/usr/bin/env python3
"""Audit trail for workspace write actions.

Records gmail_create_draft, calendar_create, calendar_update,
drive_upload, drive_download to project_root/.audit/workspace.log as JSONL.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import sys
import time
from collections.abc import Iterator
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


_audit_log_path = _audit_path


def _hash_record(prev_hash: str, record: Mapping[str, Any]) -> str:
    """sha256(prev_hash + canonical JSON of the record without ``_hash``)."""
    payload = prev_hash + json.dumps(dict(record), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _last_hash(path: Path) -> str:
    """Return the ``_hash`` of the last JSONL record, or ``""`` if none.

    Raises ``RuntimeError`` if the last line exists but is unparseable
    or lacks a ``_hash`` key — this distinguishes "empty log" (genesis)
    from "corrupt tail" (must not silently restart the chain).
    """
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
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Audit log tail is corrupt (unparseable JSON): {exc}"
        ) from exc
    if not isinstance(rec, dict):
        raise RuntimeError(
            f"Audit log tail is not a JSON object: {last_line[:80]}"
        )
    if not rec.get("_hash"):
        # Legacy record without _hash — write a genesis marker to
        # restart the chain legally, rather than silently chaining
        # from "".
        raise RuntimeError(
            "Audit log has legacy records without _hash — migration needed"
        )
    return str(rec["_hash"])


@contextlib.contextmanager
def _chain_lock(path: Path) -> Iterator[None]:
    """Serialize hash-chain appends with BEGIN EXCLUSIVE on a sidecar SQLite DB."""
    lock_path = path.parent / ".workspace.lock.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(lock_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS audit_lock (id INTEGER PRIMARY KEY CHECK (id = 1))"
        )
        conn.execute("INSERT OR IGNORE INTO audit_lock (id) VALUES (1)")
        conn.commit()
        delay = 0.02
        acquired = False
        last_exc: BaseException | None = None
        for attempt in range(3):
            try:
                conn.execute("BEGIN EXCLUSIVE")
                acquired = True
                break
            except sqlite3.OperationalError as exc:
                last_exc = exc
                msg = str(exc).lower()
                if ("busy" in msg or "locked" in msg) and attempt < 2:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
        if not acquired:
            raise sqlite3.OperationalError(f"Could not acquire audit lock: {last_exc}")
        try:
            yield
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
    finally:
        conn.close()


def verify_audit_chain(config: Any) -> bool:
    """Return True iff every record's ``_hash`` matches the hash chain.

    The first record uses ``prev_hash = ""``. A record with
    ``_chain_restart=True`` is treated as a legal chain restart
    (genesis marker for legacy/corrupt migration). Any missing hash,
    malformed line, or content/hash mismatch returns False.
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
                # Legal chain restart (migration marker)
                if record.get("_chain_restart"):
                    prev_hash = ""
                    stored = record.get("_hash")
                    if not stored:
                        return False
                    content = {k: v for k, v in record.items() if k != "_hash"}
                    expected = _hash_record("", content)
                    if stored != expected:
                        return False
                    prev_hash = stored
                    continue
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
            try:
                prev_hash = _last_hash(path)
            except RuntimeError as exc:
                # Corrupt or legacy tail — log and chain from genesis
                # with a migration marker so the break is visible.
                import sys
                print(f"[workspace_audit] WARNING: {exc}", file=sys.stderr)
                prev_hash = ""
                to_write = dict(record)
                to_write["_chain_restart"] = True
                to_write["_restart_reason"] = str(exc)
                to_write["_hash"] = _hash_record(prev_hash, to_write)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(to_write) + "\n")
                return
            to_write = dict(record)
            to_write["_hash"] = _hash_record(prev_hash, to_write)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(to_write) + "\n")

        with _chain_lock(path):
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