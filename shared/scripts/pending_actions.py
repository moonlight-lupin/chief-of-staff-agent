#!/usr/bin/env python3
"""Pending action storage for gated operations (e.g. Gmail send).

State machine: requested → approved → executed | cancelled
               requested → cancelled (skip approval)

Concurrency: optimistic versioning via version counter in the JSON file.
Each save checks the version; if it changed since load, the write is rejected.
This prevents lost updates from concurrent channels without external locks.

Approval expiry: requested actions older than EXPIRY_HOURS are marked 'expired'.
Expired actions cannot be approved or executed — they must be re-prepared.

Pending actions are stored as JSON in project_root/.pending_actions.json.
All state transitions are audited via workspace_audit.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

# Ensure shared/scripts is importable
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

# Approval expiry: requested actions older than this are stale.
EXPIRY_HOURS = 72

# Approved actions must be executed within APPROVED_EXPIRY_HOURS,
# otherwise the approval lapses and the action must be re-approved.
APPROVED_EXPIRY_HOURS = 24

# Risk classification for email recipients.
# Internal = same domain as the company. External = different domain.
# High-risk = never-seen external domains (future: maintain a known-contacts list).

KNOWN_SAFE_DOMAINS: set[str] = set()  # populated from config if available


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
        return {"actions": {}, "_version": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "actions" not in data:
            return {"actions": {}, "_version": 0}
        if "_version" not in data:
            data["_version"] = 0
        return data
    except (json.JSONDecodeError, OSError):
        return {"actions": {}, "_version": 0}


class ConcurrencyError(Exception):
    """Raised when optimistic version check fails."""
    pass


def _save(config: Any, data: dict[str, Any], expected_version: int | None = None) -> int:
    """Atomically save pending actions to disk with optimistic versioning.

    If expected_version is provided, checks that the on-disk version matches.
    Returns the new version number.
    Raises ConcurrencyError if the version has changed since load.
    """
    path = _pending_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Optimistic version check
    if expected_version is not None:
        current = _load(config)
        current_version = current.get("_version", 0)
        if current_version != expected_version:
            raise ConcurrencyError(
                f"Pending actions changed since load (expected v{expected_version}, "
                f"found v{current_version}). Reload and retry."
            )

    new_version = (data.get("_version", 0) or 0) + 1
    data["_version"] = new_version

    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)
    return new_version


def _is_expired(action: dict[str, Any], expiry_hours: int = EXPIRY_HOURS) -> bool:
    """Check if a requested action is expired (older than expiry_hours)."""
    if action.get("state") != "requested":
        return False
    created = action.get("created_at", "")
    if not created:
        return False
    try:
        dt = datetime.fromisoformat(created)
        age = datetime.now(timezone.utc) - dt
        return age > timedelta(hours=expiry_hours)
    except (ValueError, TypeError):
        return False


def _is_approval_lapsed(action: dict[str, Any],
                        expiry_hours: int = APPROVED_EXPIRY_HOURS) -> bool:
    """Check if an approved action's approval has lapsed (not executed in time)."""
    if action.get("state") != "approved":
        return False
    approved_at = action.get("approved_at", "")
    if not approved_at:
        return False
    try:
        dt = datetime.fromisoformat(approved_at)
        age = datetime.now(timezone.utc) - dt
        return age > timedelta(hours=expiry_hours)
    except (ValueError, TypeError):
        return False


def classify_recipient_risk(
    recipient: str, config: Any | None = None
) -> dict[str, str]:
    """Classify the risk of an email recipient.

    Returns dict with:
    - level: 'internal', 'external', 'unknown'
    - domain: extracted domain
    - reason: human-readable explanation
    """
    domain = ""
    if "@" in recipient:
        domain = recipient.split("@", 1)[1].lower()

    if not domain:
        return {"level": "unknown", "domain": "", "reason": "Invalid or missing email domain"}

    # Check if internal (same domain as company)
    if config and isinstance(config, Mapping):
        company = config.get("company", {})
        if isinstance(company, Mapping):
            company_domain = str(company.get("website", "")).lower()
            if company_domain:
                # Extract domain from website URL
                if "://" in company_domain:
                    company_domain = company_domain.split("://", 1)[1]
                company_domain = company_domain.rstrip("/").split("/")[0]
                if company_domain in domain or domain in company_domain:
                    return {"level": "internal", "domain": domain,
                            "reason": f"Same domain as company ({company_domain})"}
        # Also check google.domain
        google = config.get("google", {})
        if isinstance(google, Mapping):
            google_domain = str(google.get("domain", "")).lower()
            if google_domain and google_domain == domain:
                return {"level": "internal", "domain": domain,
                        "reason": f"Same domain as Google workspace ({google_domain})"}

    # Check known safe domains
    if domain in KNOWN_SAFE_DOMAINS:
        return {"level": "external", "domain": domain,
                "reason": f"External but known domain ({domain})"}

    return {"level": "external", "domain": domain,
            "reason": f"External domain ({domain}) — verify recipient before approving"}


def create_pending_action(
    config: Any,
    action_type: str,
    provider: str,
    target: str,
    payload: dict[str, Any],
    summary: str | None = None,
    approver: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Create a pending action in 'requested' state.

    Returns the action dict with a unique ID.
    Audits the creation.
    """
    action_id = str(uuid.uuid4())[:12]

    # Classify recipient risk for email actions
    risk = None
    if action_type == "gmail.send":
        risk = classify_recipient_risk(target, config)

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
        "expired_at": None,
        "result": None,
        "approver": None,
        "approval_reason": None,
        "risk": risk,
    }

    data = _load(config)
    expected_version = data.get("_version", 0)
    data["actions"][action_id] = action
    _save(config, data, expected_version=expected_version)

    # Audit
    try:
        from workspace_audit import audit_workspace_action
        extra: dict[str, Any] = {"action_id": action_id}
        if risk:
            extra["risk_level"] = risk["level"]
        audit_workspace_action(config, provider, action_type, "pending",
                               target=target, status="requested",
                               extra=extra)
    except Exception:
        pass  # best-effort

    return action


def list_pending_actions(config: Any, state: str | None = None,
                         include_expired: bool = True) -> list[dict[str, Any]]:
    """List pending actions, optionally filtered by state.

    If include_expired is False, expired 'requested' actions are excluded
    from 'requested' results.
    """
    data = _load(config)
    actions = list(data["actions"].values())
    if state:
        actions = [a for a in actions if a.get("state") == state]
        if state == "requested" and not include_expired:
            actions = [a for a in actions if not _is_expired(a)]
    return sorted(actions, key=lambda a: a.get("created_at", ""))


def get_pending_action(config: Any, action_id: str) -> dict[str, Any] | None:
    """Get a single pending action by ID."""
    data = _load(config)
    return data["actions"].get(action_id)


def check_expired(config: Any, action_id: str) -> bool:
    """Check if a specific action is expired. Marks it if so."""
    data = _load(config)
    action = data["actions"].get(action_id)
    if not action:
        return False
    if _is_expired(action):
        expected_version = data.get("_version", 0)
        action["state"] = "expired"
        action["expired_at"] = _now()
        _save(config, data, expected_version=expected_version)
        try:
            from workspace_audit import audit_workspace_action
            audit_workspace_action(config, action["provider"], action["type"], "pending",
                                   target=action["target"], status="expired",
                                   extra={"action_id": action_id})
        except Exception:
            pass
        return True
    return False


def approve_pending_action(
    config: Any, action_id: str,
    approver: str | None = None,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Transition a pending action from 'requested' to 'approved'.

    Returns the updated action, or None if:
    - not found
    - not in 'requested' state
    - expired (stale)
    Audits the approval with approver/reason metadata.
    """
    data = _load(config)
    expected_version = data.get("_version", 0)
    action = data["actions"].get(action_id)
    if not action or action["state"] != "requested":
        return None

    # Check expiry
    if _is_expired(action):
        action["state"] = "expired"
        action["expired_at"] = _now()
        _save(config, data, expected_version=expected_version)
        return None

    action["state"] = "approved"
    action["approved_at"] = _now()
    action["approver"] = approver
    action["approval_reason"] = reason
    _save(config, data, expected_version=expected_version)

    try:
        from workspace_audit import audit_workspace_action
        audit_workspace_action(config, action["provider"], action["type"], "pending",
                               target=action["target"], status="approved",
                               extra={"action_id": action_id,
                                      "approver": approver or "",
                                      "approval_reason": reason or ""})
    except Exception:
        pass

    return action


def cancel_pending_action(
    config: Any, action_id: str,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Transition a pending action to 'cancelled'.

    Can cancel from 'requested', 'approved', or 'expired' state.
    Returns the updated action, or None if not found or already terminal.
    """
    data = _load(config)
    expected_version = data.get("_version", 0)
    action = data["actions"].get(action_id)
    if not action or action["state"] in ("executed", "cancelled"):
        return None

    action["state"] = "cancelled"
    action["cancelled_at"] = _now()
    if reason:
        action["cancel_reason"] = reason
    _save(config, data, expected_version=expected_version)

    try:
        from workspace_audit import audit_workspace_action
        audit_workspace_action(config, action["provider"], action["type"], "pending",
                               target=action["target"], status="cancelled",
                               extra={"action_id": action_id, "cancel_reason": reason or ""})
    except Exception:
        pass

    return action


def mark_executing(config: Any, action_id: str) -> dict[str, Any] | None:
    """Pre-execution eligibility check — transition approved → executing.

    This MUST be called before any provider method to prevent the race where
    a provider action succeeds but mark_executed() rejects a lapsed approval.

    Returns the action dict if eligible (state='executing'), or None if:
    - not found
    - not in 'approved' state
    - approval has lapsed (marks as expired)
    - concurrency conflict (version changed)
    """
    data = _load(config)
    expected_version = data.get("_version", 0)
    action = data["actions"].get(action_id)
    if not action or action["state"] != "approved":
        return None

    # Check if approval has lapsed BEFORE any provider call
    if _is_approval_lapsed(action):
        action["state"] = "expired"
        action["expired_at"] = _now()
        _save(config, data, expected_version=expected_version)
        try:
            from workspace_audit import audit_workspace_action
            audit_workspace_action(config, action["provider"], action["type"], "pending",
                                   target=action["target"], status="expired",
                                   extra={"action_id": action_id, "reason": "approval_lapsed"})
        except Exception:
            pass
        return None

    # Transition to executing
    action["state"] = "executing"
    action["executing_at"] = _now()
    _save(config, data, expected_version=expected_version)

    try:
        from workspace_audit import audit_workspace_action
        audit_workspace_action(config, action["provider"], action["type"], "pending",
                               target=action["target"], status="executing",
                               extra={"action_id": action_id})
    except Exception:
        pass

    return action


def mark_executed(config: Any, action_id: str, result: dict[str, Any]) -> dict[str, Any] | None:
    """Transition an executing action to 'executed' with the result.

    This is called AFTER the provider method completes. The action must be
    in 'executing' state (set by mark_executing() before the provider call).

    Returns the updated action, or None if:
    - not found
    - not in 'executing' state
    """
    data = _load(config)
    expected_version = data.get("_version", 0)
    action = data["actions"].get(action_id)
    if not action or action["state"] != "executing":
        return None

    action["state"] = "executed"
    action["executed_at"] = _now()
    action["result"] = result
    _save(config, data, expected_version=expected_version)

    try:
        from workspace_audit import audit_workspace_action
        audit_workspace_action(config, action["provider"], action["type"], "pending",
                               target=action["target"], status="executed",
                               extra={"action_id": action_id,
                                      "result_success": result.get("success", False),
                                      "approver": action.get("approver", "")})
    except Exception:
        pass

    return action


def mark_failed(config: Any, action_id: str, error: str) -> dict[str, Any] | None:
    """Transition an executing action to 'failed' with an error message.

    Called when the provider method raises an exception or returns failure.
    The action transitions back to 'approved' so it can be retried.

    Returns the updated action, or None if not in 'executing' state.
    """
    data = _load(config)
    expected_version = data.get("_version", 0)
    action = data["actions"].get(action_id)
    if not action or action["state"] != "executing":
        return None

    action["state"] = "approved"  # back to approved for retry
    action["last_error"] = error
    action["retry_count"] = action.get("retry_count", 0) + 1
    _save(config, data, expected_version=expected_version)

    try:
        from workspace_audit import audit_workspace_action
        audit_workspace_action(config, action["provider"], action["type"], "pending",
                               target=action["target"], status="failed",
                               extra={"action_id": action_id, "error": error})
    except Exception:
        pass

    return action


def assert_executable(config: Any, action_id: str) -> dict[str, Any] | None:
    """Check if an action is eligible for execution WITHOUT changing state.

    Use this for pre-execution checks where you want to verify eligibility
    but not commit to the executing transition yet.

    Returns the action dict if eligible, or None if:
    - not found
    - not in 'approved' state
    - approval has lapsed (marks as expired)
    """
    action = get_pending_action(config, action_id)
    if not action:
        return None
    if action["state"] != "approved":
        return None
    if _is_approval_lapsed(action):
        # Mark as expired
        data = _load(config)
        expected_version = data.get("_version", 0)
        data["actions"][action_id]["state"] = "expired"
        data["actions"][action_id]["expired_at"] = _now()
        _save(config, data, expected_version=expected_version)
        return None
    return action


def preview_pending_action(config: Any, action_id: str) -> dict[str, Any] | None:
    """Return a preview-safe view of a pending action (no payload execution)."""
    action = get_pending_action(config, action_id)
    if not action:
        return None

    # Check and mark expiry if needed
    is_exp = check_expired(config, action_id)
    if is_exp:
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
        "risk": action.get("risk"),
        "preview": {
            "to": action["payload"].get("to"),
            "subject": action["payload"].get("subject"),
            "body_preview": action["payload"].get("body", "")[:200],
        },
        "created_at": action["created_at"],
        "approved_at": action.get("approved_at"),
        "approver": action.get("approver"),
        "approval_reason": action.get("approval_reason"),
    }


def cleanup_old_actions(config: Any, days: int = 30) -> int:
    """Remove executed/cancelled/expired actions older than N days. Returns count removed."""
    data = _load(config)
    expected_version = data.get("_version", 0)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    removed = 0
    for aid in list(data["actions"].keys()):
        action = data["actions"][aid]
        if action["state"] in ("executed", "cancelled", "expired"):
            ts = action.get("executed_at") or action.get("cancelled_at") or action.get("expired_at") or ""
            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    if dt < cutoff:
                        del data["actions"][aid]
                        removed += 1
                except (ValueError, TypeError):
                    pass
    if removed:
        _save(config, data, expected_version=expected_version)
    return removed


def get_pending_summary(config: Any) -> dict[str, Any]:
    """Return a summary of pending actions by state for operator UX."""
    data = _load(config)
    actions = list(data["actions"].values())
    counts: dict[str, int] = {}
    expired_count = 0
    for a in actions:
        state = a.get("state", "unknown")
        if state == "requested" and _is_expired(a):
            expired_count += 1
            state = "expired"
        counts[state] = counts.get(state, 0) + 1

    high_risk = []
    for a in actions:
        if a.get("state") == "requested" and not _is_expired(a):
            risk = a.get("risk")
            if risk and risk.get("level") == "external":
                high_risk.append({
                    "id": a["id"],
                    "target": a["target"],
                    "summary": a.get("summary", ""),
                    "risk_reason": risk.get("reason", ""),
                })

    return {
        "total": len(actions),
        "by_state": counts,
        "expired_unmarked": expired_count,
        "high_risk_pending": high_risk,
    }


def format_preview_for_delivery(action_id: str, preview: dict[str, Any]) -> str:
    """Format a pending action preview as a text message for operator delivery.

    This is the delivery-channel hook — the output is a plain-text message
    suitable for Telegram, WhatsApp, or any text channel. The calling agent
    or cron job is responsible for actually sending it.
    """
    state = preview.get("state", "?")
    icon = {
        "requested": "📨", "approved": "✅", "executed": "📤",
        "cancelled": "❌", "expired": "⏰",
    }.get(state, "?")

    lines = [
        f"{icon} Pending Gmail Send — {action_id}",
        f"State: {state}",
        f"To: {preview.get('preview', {}).get('to', '?')}",
        f"Subject: {preview.get('preview', {}).get('subject', '?')}",
    ]

    risk = preview.get("risk")
    if risk:
        risk_icon = "⚠️" if risk["level"] == "external" else "✅"
        lines.append(f"Risk: {risk_icon} {risk['level']} — {risk['reason']}")

    body_preview = preview.get("preview", {}).get("body_preview", "")
    if body_preview:
        lines.append(f"Body: {body_preview[:100]}...")

    if state == "requested":
        lines.append("")
        lines.append(f"Approve: send_email.py approve --action-id {action_id}")
        lines.append(f"Cancel:  send_email.py cancel --action-id {action_id}")
    elif state == "approved":
        lines.append("")
        lines.append(f"Execute: send_email.py execute --action-id {action_id}")

    return "\n".join(lines)


def get_actions_for_delivery(config: Any) -> list[dict[str, Any]]:
    """Return pending 'requested' actions formatted for operator delivery.

    Used by cron jobs or the agent to surface pending actions to the operator.
    Each item has: id, formatted_message, risk_level, target.
    """
    actions = list_pending_actions(config, state="requested", include_expired=False)
    results = []
    for a in actions:
        preview = preview_pending_action(config, a["id"])
        if not preview:
            continue
        msg = format_preview_for_delivery(a["id"], preview)
        risk = a.get("risk", {})
        results.append({
            "id": a["id"],
            "formatted_message": msg,
            "risk_level": risk.get("level", "unknown"),
            "target": a.get("target", ""),
        })
    return results