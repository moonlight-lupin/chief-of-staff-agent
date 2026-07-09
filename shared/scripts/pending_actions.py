#!/usr/bin/env python3
"""Pending action storage for gated operations (e.g. Gmail send).

State machine: requested → approved → executed | cancelled

Pending actions are stored as JSON in project_root/.pending_actions.json.
All state transitions are audited via workspace_audit.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

# Ensure shared/scripts is importable
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))


def _project_root(config: Any) -> Path:
    """Get project root from config, env, or default."""
    root = None
    if isinstance(config, Mapping):
        paths = config.get("paths", {})
        if isinstance(paths, Mapping):
            root = paths.get("project_root")
    if not root:
        root = os.getenv("CHIEF_OF_STAFF_PROJECT_ROOT",
                         str(Path.home() / ".hermes" / "projects" / "default"))
    return Path(str(root)).expanduser()


def _pending_path(config: Any) -> Path:
    """Path to pending actions JSON file."""
    return _project_root(config) / ".pending_actions.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(config: Any) -> dict[str, Any]:
    """Load pending actions from disk. Returns empty structure if missing."""
    path = _pending_path(config)
    if not path.exists():
        return {"actions": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "actions" not in data:
            return {"actions": {}}
        return data
    except (json.JSONDecodeError, OSError):
        return {"actions": {}}


def _save(config: Any, data: dict[str, Any]) -> None:
    """Atomically save pending actions to disk."""
    path = _pending_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def create_pending_action(
    config: Any,
    action_type: str,
    provider: str,
    target: str,
    payload: dict[str, Any],
    summary: str | None = None,
) -> dict[str, Any]:
    """Create a pending action in 'requested' state.

    Returns the action dict with a unique ID.
    Audits the creation.
    """
    action_id = str(uuid.uuid4())[:12]
    action = {
        "id": action_id,
        "type": action_type,
        "provider": provider,
        "target": target,
        "payload": payload,
        "summary": summary or f"{action_type} to {target}",
        "state": "requested",
        "created_at": _now(),
        "approved_at": None,
        "executed_at": None,
        "cancelled_at": None,
        "result": None,
    }

    data = _load(config)
    data["actions"][action_id] = action
    _save(config, data)

    # Audit
    try:
        from workspace_audit import audit_workspace_action
        audit_workspace_action(config, provider, action_type, "pending",
                               target=target, status="requested",
                               extra={"action_id": action_id})
    except Exception:
        pass  # best-effort

    return action


def list_pending_actions(config: Any, state: str | None = None) -> list[dict[str, Any]]:
    """List pending actions, optionally filtered by state."""
    data = _load(config)
    actions = list(data["actions"].values())
    if state:
        actions = [a for a in actions if a.get("state") == state]
    return sorted(actions, key=lambda a: a.get("created_at", ""))


def get_pending_action(config: Any, action_id: str) -> dict[str, Any] | None:
    """Get a single pending action by ID."""
    data = _load(config)
    return data["actions"].get(action_id)


def approve_pending_action(config: Any, action_id: str) -> dict[str, Any] | None:
    """Transition a pending action from 'requested' to 'approved'.

    Returns the updated action, or None if not found or not in 'requested' state.
    Audits the approval.
    """
    data = _load(config)
    action = data["actions"].get(action_id)
    if not action or action["state"] != "requested":
        return None

    action["state"] = "approved"
    action["approved_at"] = _now()
    _save(config, data)

    try:
        from workspace_audit import audit_workspace_action
        audit_workspace_action(config, action["provider"], action["type"], "pending",
                               target=action["target"], status="approved",
                               extra={"action_id": action_id})
    except Exception:
        pass

    return action


def cancel_pending_action(config: Any, action_id: str) -> dict[str, Any] | None:
    """Transition a pending action to 'cancelled'.

    Can cancel from 'requested' or 'approved' state.
    Returns the updated action, or None if not found or already terminal.
    """
    data = _load(config)
    action = data["actions"].get(action_id)
    if not action or action["state"] in ("executed", "cancelled"):
        return None

    action["state"] = "cancelled"
    action["cancelled_at"] = _now()
    _save(config, data)

    try:
        from workspace_audit import audit_workspace_action
        audit_workspace_action(config, action["provider"], action["type"], "pending",
                               target=action["target"], status="cancelled",
                               extra={"action_id": action_id})
    except Exception:
        pass

    return action


def mark_executed(config: Any, action_id: str, result: dict[str, Any]) -> dict[str, Any] | None:
    """Transition an approved action to 'executed' with the result.

    Returns the updated action, or None if not found or not 'approved'.
    """
    data = _load(config)
    action = data["actions"].get(action_id)
    if not action or action["state"] != "approved":
        return None

    action["state"] = "executed"
    action["executed_at"] = _now()
    action["result"] = result
    _save(config, data)

    try:
        from workspace_audit import audit_workspace_action
        audit_workspace_action(config, action["provider"], action["type"], "pending",
                               target=action["target"], status="executed",
                               extra={"action_id": action_id, "result_success": result.get("success", False)})
    except Exception:
        pass

    return action


def preview_pending_action(config: Any, action_id: str) -> dict[str, Any] | None:
    """Return a preview-safe view of a pending action (no payload execution)."""
    action = get_pending_action(config, action_id)
    if not action:
        return None
    return {
        "id": action["id"],
        "type": action["type"],
        "provider": action["provider"],
        "target": action["target"],
        "summary": action["summary"],
        "state": action["state"],
        "preview": {
            "to": action["payload"].get("to"),
            "subject": action["payload"].get("subject"),
            "body_preview": action["payload"].get("body", "")[:200],
        },
        "created_at": action["created_at"],
    }


def cleanup_old_actions(config: Any, days: int = 30) -> int:
    """Remove executed/cancelled actions older than N days. Returns count removed."""
    from datetime import datetime, timedelta
    data = _load(config)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    removed = 0
    for aid in list(data["actions"].keys()):
        action = data["actions"][aid]
        if action["state"] in ("executed", "cancelled"):
            ts = action.get("executed_at") or action.get("cancelled_at") or ""
            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    if dt < cutoff:
                        del data["actions"][aid]
                        removed += 1
                except (ValueError, TypeError):
                    pass
    if removed:
        _save(config, data)
    return removed