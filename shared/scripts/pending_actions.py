#!/usr/bin/env python3
"""Pending action storage for gated operations (e.g. Gmail send).

State machine: requested → approved → executing → executed | cancelled | dismissed | expired | failed
               requested → cancelled | dismissed (skip approval)
               executing → approved (retry, under MAX_RETRIES) | failed (retry cap)

Concurrency: exclusive file lock (file_lock.with_lock) around load-check-write,
plus a version counter as defense-in-depth. Concurrent writers serialize on
the lock; a stale expected_version still raises ConcurrencyError.

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
from collections.abc import Callable
from typing import Any, Mapping, TypeVar

import file_lock

# Ensure shared/scripts is importable
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

# Approval expiry: requested actions older than this are stale.
EXPIRY_HOURS = 72

# Approved actions must be executed within APPROVED_EXPIRY_HOURS,
# otherwise the approval lapses and the action must be re-approved.
APPROVED_EXPIRY_HOURS = 24

# Cap on mark_failed → approved retries. At MAX_RETRIES the action
# transitions to terminal 'failed' so a poison / ambiguous-504 send
# cannot be re-armed forever (and duplicate a message that may have
# already been delivered).
MAX_RETRIES = 3

# Risk classification for email recipients.
# Internal = same domain as the company. External = different domain.
# High-risk = never-seen external domains (future: maintain a known-contacts list).

KNOWN_SAFE_DOMAINS: set[str] = set()  # populated from config if available



def _get_default_project_root_fallback() -> Path:
    """Default project root for fallback paths (env-configurable).

    Returns <hermes_home>/projects/default, NOT <hermes_home> itself,
    so state files like .events.json go under projects/default/ not
    polluting the Hermes home root.
    """
    env = os.getenv("CHIEF_OF_STAFF_HERMES_HOME") or os.getenv("HERMES_HOME")
    home = Path(env).expanduser() if env else Path.home() / ".hermes"
    return home / "projects" / "default"

def _project_root(config: Any) -> Path:
    """Get project root from config or CHIEF_OF_STAFF_PROJECT_ROOT.

    Raises RuntimeError when neither is set — do not silently write to
    ~/.hermes/projects/default.
    """
    root = None
    if isinstance(config, Mapping):
        paths = config.get("paths", {})
        if isinstance(paths, Mapping):
            root = paths.get("project_root")
    if not root:
        root = os.getenv("CHIEF_OF_STAFF_PROJECT_ROOT")
    if not root:
        raise RuntimeError(
            "Missing project root: set paths.project_root in config or "
            "CHIEF_OF_STAFF_PROJECT_ROOT"
        )
    return Path(str(root)).expanduser()


def _pending_path(config: Any) -> Path:
    """Path to pending actions JSON file."""
    return _project_root(config) / ".pending_actions.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_event(event: str, **fields: Any) -> None:
    """Best-effort action-lifecycle runtime log. No-ops when runtime_log is
    unavailable or no run is active; never raises into the caller. Only ids /
    emails are passed — never payload contents."""
    try:
        from runtime_log import log_event
        log_event(event, **fields)
    except Exception:  # pragma: no cover - logging must never break the caller
        pass


class StateCorruptionError(Exception):
    """Raised when a state file exists but cannot be parsed.

    Distinguishes 'file absent' (safe to return empty) from 'file corrupt'
    (must not be silently overwritten). Callers should fail closed, preserve
    the bad file, and alert the operator.
    """
    pass


def _load(config: Any) -> dict[str, Any]:
    """Load pending actions from disk.

    Returns empty structure if the file is absent.
    Raises StateCorruptionError if the file exists but is unreadable or
    contains invalid JSON — this must NOT be silently treated as empty,
    because a subsequent _save would overwrite the only copy of the data.
    """
    path = _pending_path(config)
    if not path.exists():
        return {"actions": {}, "_version": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "actions" not in data:
            raise StateCorruptionError(
                f"Pending actions file {path} exists but has invalid structure"
            )
        if "_version" not in data:
            data["_version"] = 0
        return data
    except json.JSONDecodeError as exc:
        raise StateCorruptionError(
            f"Pending actions file {path} is corrupt: {exc}"
        ) from exc
    except OSError as exc:
        raise StateCorruptionError(
            f"Cannot read pending actions file {path}: {exc}"
        ) from exc


class ConcurrencyError(Exception):
    """Raised when optimistic version check fails."""
    pass


def _save(config: Any, data: dict[str, Any], expected_version: int | None = None) -> int:
    """Atomically save pending actions to disk with locking + versioning.

    If expected_version is provided, checks that the on-disk version matches.
    Returns the new version number.
    Raises ConcurrencyError if the version has changed since load.
    """
    path = _pending_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)

    with file_lock.with_lock(str(path), timeout=10):
        # Version check inside the lock: defense in depth against a stale
        # in-memory snapshot from before the lock was acquired.
        current = _load(config)
        current_version = current.get("_version", 0) or 0
        if expected_version is not None:
            if current_version != expected_version:
                raise ConcurrencyError(
                    f"Pending actions changed since load (expected v{expected_version}, "
                    f"found v{current_version}). Reload and retry."
                )
            new_version = current_version + 1
        else:
            # Derive from disk, not from stale in-memory data
            new_version = max(data.get("_version", 0) or 0, current_version) + 1
        data["_version"] = new_version

        tmp = path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, indent=2, ensure_ascii=False, default=str))
                fh.flush()
                os.fsync(fh.fileno())
            tmp.replace(path)
            dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return new_version


T = TypeVar("T")


def _with_retry(
    config: Any,
    mutate_fn: Callable[[Any], T],
    max_attempts: int = 3,
) -> T | None:
    """Run a load→mutate→save function, retrying on ConcurrencyError.

    ``mutate_fn(config)`` must perform the full load→mutate→save cycle so
    each attempt sees a fresh snapshot. Returns the mutate function's
    result, or None if every attempt raises ConcurrencyError.
    """
    for attempt in range(max_attempts):
        try:
            return mutate_fn(config)
        except ConcurrencyError:
            if attempt == max_attempts - 1:
                return None
    return None


def _parse_ts(value: str) -> datetime | None:
    """Parse an ISO timestamp, normalizing naive timestamps to UTC.

    Returns None if the value is empty or unparseable.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _is_expired(action: dict[str, Any], expiry_hours: int = EXPIRY_HOURS) -> bool:
    """Check if a requested action is expired (older than expiry_hours)."""
    if action.get("state") != "requested":
        return False
    dt = _parse_ts(action.get("created_at", ""))
    if dt is None:
        return False
    age = datetime.now(timezone.utc) - dt
    return age > timedelta(hours=expiry_hours)


def _is_approval_lapsed(action: dict[str, Any],
                        expiry_hours: int = APPROVED_EXPIRY_HOURS) -> bool:
    """Check if an approved action's approval has lapsed (not executed in time)."""
    if action.get("state") != "approved":
        return False
    dt = _parse_ts(action.get("approved_at", ""))
    if dt is None:
        return False
    age = datetime.now(timezone.utc) - dt
    return age > timedelta(hours=expiry_hours)


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
                if domain == company_domain or domain.endswith("." + company_domain):
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
) -> dict[str, Any] | None:
    """Create a pending action in 'requested' state.

    Returns the action dict with a unique ID, or None on persistent
    concurrency conflict.
    Audits the creation.
    """
    action_id = str(uuid.uuid4())[:12]

    # Classify recipient risk for email actions
    risk = None
    if action_type in ("gmail.send", "mail.send"):
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
        "dismissed_at": None,
        "expired_at": None,
        "result": None,
        "approver": None,
        "approval_reason": None,
        "risk": risk,
    }

    def _mutate(cfg: Any) -> dict[str, Any]:
        data = _load(cfg)
        expected_version = data.get("_version", 0)
        data["actions"][action_id] = action
        _save(cfg, data, expected_version=expected_version)
        return action

    result = _with_retry(config, _mutate)
    if result is None:
        return None

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

    _log_event(
        "action_requested", level="info", component="pending_actions",
        action_id=action_id, action_type=action_type, provider=provider,
        target=target,
    )

    return result


def list_pending_actions(config: Any, state: str | None = None,
                         include_expired: bool = True) -> list[dict[str, Any]]:
    """List pending actions, optionally filtered by state.

    If include_expired is False, expired 'requested' actions are excluded
    from 'requested' results.

    If the state file is corrupt, returns an empty list and logs a warning
    rather than raising — this is a read-only listing API. The underlying
    _load() raises StateCorruptionError to prevent silent overwrites; this
    caller catches it for display purposes.
    """
    try:
        data = _load(config)
    except StateCorruptionError as exc:
        _log_event("state_corruption", level="error", component="pending_actions",
                   error=str(exc))
        return []
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

    def _mutate(cfg: Any) -> tuple[bool, dict[str, Any] | None]:
        data = _load(cfg)
        action = data["actions"].get(action_id)
        if not action:
            return False, None
        if _is_expired(action):
            expected_version = data.get("_version", 0)
            action["state"] = "expired"
            action["expired_at"] = _now()
            _save(cfg, data, expected_version=expected_version)
            return True, action
        return False, None

    result = _with_retry(config, _mutate)
    if result is None:
        return False
    marked, action = result
    if marked and action is not None:
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
    - persistent concurrency conflict
    Audits the approval with approver/reason metadata.
    """

    def _mutate(cfg: Any) -> dict[str, Any] | None:
        data = _load(cfg)
        expected_version = data.get("_version", 0)
        action = data["actions"].get(action_id)
        if not action or action["state"] != "requested":
            return None

        # Check expiry
        if _is_expired(action):
            action["state"] = "expired"
            action["expired_at"] = _now()
            _save(cfg, data, expected_version=expected_version)
            return None

        action["state"] = "approved"
        action["approved_at"] = _now()
        action["approver"] = approver
        action["approval_reason"] = reason
        _save(cfg, data, expected_version=expected_version)
        return action

    action = _with_retry(config, _mutate)
    if action is None:
        return None

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
    Returns the updated action, or None if not found, already terminal,
    or a concurrency conflict persists.
    """

    def _mutate(cfg: Any) -> dict[str, Any] | None:
        data = _load(cfg)
        expected_version = data.get("_version", 0)
        action = data["actions"].get(action_id)
        if not action or action["state"] in ("executed", "cancelled", "dismissed"):
            return None

        action["state"] = "cancelled"
        action["cancelled_at"] = _now()
        if reason:
            action["cancel_reason"] = reason
        _save(cfg, data, expected_version=expected_version)
        return action

    action = _with_retry(config, _mutate)
    if action is None:
        return None

    try:
        from workspace_audit import audit_workspace_action
        audit_workspace_action(config, action["provider"], action["type"], "pending",
                               target=action["target"], status="cancelled",
                               extra={"action_id": action_id, "cancel_reason": reason or ""})
    except Exception:
        pass

    return action


def dismiss_pending_action(
    config: Any, action_id: str,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Transition a pending action from 'requested', 'approved', or 'expired' to 'dismissed'.

    Returns the updated action, or None if not found, not dismissible,
    or a concurrency conflict persists.
    """

    def _mutate(cfg: Any) -> dict[str, Any] | None:
        data = _load(cfg)
        expected_version = data.get("_version", 0)
        action = data["actions"].get(action_id)
        if not action or action["state"] not in ("requested", "approved", "expired"):
            return None

        dismiss_reason = reason if reason is not None else "No dismiss reason provided"
        action["state"] = "dismissed"
        action["dismissed_at"] = _now()
        action["dismiss_reason"] = dismiss_reason
        _save(cfg, data, expected_version=expected_version)
        return action

    action = _with_retry(config, _mutate)
    if action is None:
        return None

    dismiss_reason = action.get("dismiss_reason", "No dismiss reason provided")
    try:
        from workspace_audit import audit_workspace_action
        audit_workspace_action(config, action["provider"], action["type"], "pending",
                               target=action["target"], status="dismissed",
                               extra={"action_id": action_id,
                                      "dismiss_reason": dismiss_reason,
                                      "reason_missing": reason is None})
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

    def _mutate(cfg: Any) -> tuple[dict[str, Any], bool] | None:
        data = _load(cfg)
        expected_version = data.get("_version", 0)
        action = data["actions"].get(action_id)
        if not action or action["state"] != "approved":
            return None

        # Check if approval has lapsed BEFORE any provider call
        if _is_approval_lapsed(action):
            action["state"] = "expired"
            action["expired_at"] = _now()
            _save(cfg, data, expected_version=expected_version)
            return action, True

        # Transition to executing
        action["state"] = "executing"
        action["executing_at"] = _now()
        _save(cfg, data, expected_version=expected_version)
        return action, False

    result = _with_retry(config, _mutate)
    if result is None:
        return None

    action, lapsed = result
    if lapsed:
        try:
            from workspace_audit import audit_workspace_action
            audit_workspace_action(config, action["provider"], action["type"], "pending",
                                   target=action["target"], status="expired",
                                   extra={"action_id": action_id, "reason": "approval_lapsed"})
        except Exception:
            pass
        return None

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
    - ConcurrencyError persists after 3 attempts
    """

    def _mutate(cfg: Any) -> dict[str, Any] | None:
        data = _load(cfg)
        expected_version = data.get("_version", 0)
        action = data["actions"].get(action_id)
        if not action or action["state"] != "executing":
            return None
        action["state"] = "executed"
        action["executed_at"] = _now()
        action["result"] = result
        _save(cfg, data, expected_version=expected_version)
        return action

    action = _with_retry(config, _mutate)
    if action is None:
        return None

    try:
        from workspace_audit import audit_workspace_action
        audit_workspace_action(config, action["provider"], action["type"], "pending",
                               target=action["target"], status="executed",
                               extra={"action_id": action_id,
                                      "result_success": result.get("success", False),
                                      "approver": action.get("approver", "")})
    except Exception:
        pass

    _log_event(
        "action_executed", level="info", component="pending_actions",
        action_id=action_id, action_type=action["type"],
        provider=action["provider"],
    )

    return action


def mark_failed(config: Any, action_id: str, error: str) -> dict[str, Any] | None:
    """Record a provider failure on an executing action.

    Called when the provider method raises an exception or returns failure.
    Increments ``retry_count``. Under ``MAX_RETRIES`` the action returns to
    ``approved`` so it can be retried; at the cap it transitions to terminal
    ``failed`` and cannot be re-executed.

    Returns the updated action, or None if not in 'executing' state
    or ConcurrencyError persists after 3 attempts.
    """

    def _mutate(cfg: Any) -> dict[str, Any] | None:
        data = _load(cfg)
        expected_version = data.get("_version", 0)
        action = data["actions"].get(action_id)
        if not action or action["state"] != "executing":
            return None

        action["retry_count"] = action.get("retry_count", 0) + 1
        if action["retry_count"] >= MAX_RETRIES:
            action["state"] = "failed"
        else:
            action["state"] = "approved"  # back to approved for retry
        action["last_error"] = error
        _save(cfg, data, expected_version=expected_version)
        return action

    action = _with_retry(config, _mutate)
    if action is None:
        return None

    try:
        from workspace_audit import audit_workspace_action
        audit_workspace_action(config, action["provider"], action["type"], "pending",
                               target=action["target"], status="failed",
                               extra={"action_id": action_id, "error": error})
    except Exception:
        pass

    _log_event(
        "action_failed", level="warning", component="pending_actions",
        action_id=action_id, action_type=action["type"],
        provider=action["provider"], error=error,
    )

    return action


def find_stuck_actions(config: Any, max_minutes: int = 15) -> list[dict[str, Any]]:
    """Return actions stuck in 'executing' longer than ``max_minutes``.

    A worker that crashes after ``mark_executing`` but before
    ``mark_executed`` leaves the action in 'executing' forever. Callers
    can use this list plus ``revert_stuck_action`` to re-arm them.
    """
    data = _load(config)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_minutes)
    stuck: list[dict[str, Any]] = []
    for action in data["actions"].values():
        if action.get("state") != "executing":
            continue
        executing_at = action.get("executing_at") or ""
        if not executing_at:
            continue
        try:
            dt = datetime.fromisoformat(executing_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if dt < cutoff:
            stuck.append(action)
    return stuck


def revert_stuck_action(config: Any, action_id: str,
                        max_minutes: int = 15) -> dict[str, Any] | None:
    """Revert a stuck 'executing' action back to 'approved' for retry.

    Only reverts if the action has been in 'executing' state for longer
    than ``max_minutes``. Clears ``executing_at``. Returns the updated
    action, or None if the action is not stuck (still actively executing)
    or not in 'executing' state.
    """

    def _mutate(cfg: Any) -> dict[str, Any] | None:
        data = _load(cfg)
        expected_version = data.get("_version", 0)
        action = data["actions"].get(action_id)
        if not action or action["state"] != "executing":
            return None
        # Revalidate executing_at inside the locked mutation
        executing_at = action.get("executing_at") or ""
        if not executing_at:
            return None
        try:
            dt = datetime.fromisoformat(executing_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_minutes)
        if dt >= cutoff:
            return None  # Still actively executing — don't revert
        # Increment retry_count and share the MAX_RETRIES budget with
        # mark_failed, so a stuck action cannot be re-armed without bound.
        action["retry_count"] = action.get("retry_count", 0) + 1
        if action["retry_count"] >= MAX_RETRIES:
            action["state"] = "failed"
        else:
            action["state"] = "approved"
        action["executing_at"] = None
        _save(cfg, data, expected_version=expected_version)
        return action

    action = _with_retry(config, _mutate)
    if action is None:
        return None

    try:
        from workspace_audit import audit_workspace_action
        audit_workspace_action(config, action["provider"], action["type"], "pending",
                               target=action["target"], status="approved",
                               extra={"action_id": action_id, "reason": "stuck_reverted"})
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
        # Mark as expired — revalidate preconditions on each retry
        def _mutate(cfg: Any) -> None:
            data = _load(cfg)
            expected_version = data.get("_version", 0)
            stored = data["actions"].get(action_id)
            if stored and stored["state"] == "approved" and _is_approval_lapsed(stored):
                stored["state"] = "expired"
                stored["expired_at"] = _now()
                _save(cfg, data, expected_version=expected_version)

        _with_retry(config, _mutate)
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

    def _mutate(cfg: Any) -> int:
        data = _load(cfg)
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
            _save(cfg, data, expected_version=expected_version)
        return removed

    result = _with_retry(config, _mutate)
    return 0 if result is None else result


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
        "cancelled": "❌", "dismissed": "🚫", "expired": "⏰",
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