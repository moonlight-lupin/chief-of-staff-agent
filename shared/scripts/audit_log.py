#!/usr/bin/env python3
"""Append-only audit log for state mutations.

Usage:
    from audit_log import append_audit
    append_audit("pipeline", action="move_stage", before={"stage":"Lead"}, after={"stage":"Proposal Sent"}, actor="agent")
"""

from __future__ import annotations

import argparse
import json
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from config_loader import get_project_root, load_config
except Exception:  # pragma: no cover - only when imported outside plugin path
    get_project_root = None  # type: ignore
    load_config = None  # type: ignore


class AuditLogError(RuntimeError):
    """Raised when audit logging cannot proceed safely."""


def _plain(value: Any) -> Any:
    if hasattr(value, "to_plain_dict"):
        return value.to_plain_dict()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    return value


def _project_root(config: Any | None = None) -> Path:
    env_root = os.getenv("CHIEF_OF_STAFF_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    cfg = config
    if cfg is None and load_config is not None:
        cfg = load_config()
    if cfg is not None and get_project_root is not None:
        root = get_project_root(cfg)
        if root is not None:
            return root
    if cfg is not None:
        try:
            return Path(str(cfg["paths"]["project_root"])).expanduser().resolve()  # type: ignore[index]
        except Exception as exc:
            raise AuditLogError(f"Cannot resolve paths.project_root: {exc}") from exc
    return Path.cwd().resolve()


def _audit_path(store_name: str, config: Any | None = None) -> Path:
    root = _project_root(config)
    return root / ".audit" / f"{store_name}.log"


def append_audit(
    store_name: str,
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    actor: str = "agent",
    config: Any | None = None,
) -> dict[str, Any]:
    """Append one JSONL audit entry and fsync it to disk."""

    if not store_name or not str(store_name).strip():
        raise AuditLogError("store_name is required")
    if not action or not str(action).strip():
        raise AuditLogError("action is required")
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "store": str(store_name),
        "action": str(action),
        "before": _plain(before or {}),
        "after": _plain(after or {}),
        "actor": str(actor or "agent"),
    }
    # ── Audit → operational-log linkage (v0.3.4, additive) ────────────────
    # Stamp the active run id so this audit (what changed) record can be
    # correlated with the SEPARATE operational (runtime) log. Entries written
    # outside a run omit the field, so existing consumers are unaffected.
    try:
        from runtime_log import current_run_id
        run_id = current_run_id()
    except Exception:
        run_id = None
    if run_id is not None:
        entry["run_id"] = run_id

    path = _audit_path(store_name, config=config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        try:
            from runtime_log import log_event
            log_event("audit_write_failed", level="error", component="audit",
                      reason=type(exc).__name__)
        except Exception:  # pragma: no cover - logging must never break the caller
            pass
        raise AuditLogError(f"Failed to append audit entry to {path}: {exc}") from exc
    return entry


def read_audit(store_name: str, limit: int = 50, config: Any | None = None) -> list[dict[str, Any]]:
    """Read the last ``limit`` audit entries for ``store_name``."""

    if limit < 1:
        return []
    path = _audit_path(store_name, config=config)
    if not path.exists():
        return []
    rows: deque[str] = deque(maxlen=limit)
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(line)
    entries: list[dict[str, Any]] = []
    for line in rows:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise AuditLogError(f"Corrupt audit log line in {path}: {exc}") from exc
    return entries


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read Chief-of-Staff audit logs")
    parser.add_argument("store", help="Store name, e.g. pipeline")
    parser.add_argument("--limit", type=int, default=50, help="Number of entries to print")
    parser.add_argument("--json", action="store_true", help="Print JSON array instead of text lines")
    args = parser.parse_args(argv)
    entries = read_audit(args.store, args.limit)
    if args.json:
        print(json.dumps(entries, indent=2, default=str))
    else:
        for entry in entries:
            print(f"{entry.get('timestamp')} {entry.get('actor')} {entry.get('action')}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
