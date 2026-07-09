#!/usr/bin/env python3
"""Suggested action generation from classified events.

Converts event classifications into structured, reviewable suggestions.
Suggestions NEVER execute anything — they are advisory only. An operator
must explicitly choose to act on a suggestion through the approval queue.

State machine: suggested → dismissed | acted_on

Suggestion shape:
{
  "id": "sug_abc123",
  "event_id": "evt_123",
  "action_type": "gmail.draft",
  "title": "Draft a reply to client email",
  "reason": "Client asked for follow-up documents",
  "confidence": 0.82,
  "risk": "low",
  "provider": "composio",
  "requires_approval": true,
  "auto_execute": false,  # always false
  "state": "suggested"
}
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))


# ─── Suggestion Templates ─────────────────────────────────────

# Maps event categories to suggestion templates.
# Each template produces a structured suggestion with confidence,
# risk, provider recommendation, and approval requirement.

SUGGESTION_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "email_received": [
        {
            "action_type": "gmail.draft",
            "title": "Draft a reply to this email",
            "reason": "New email received — consider drafting a response",
            "confidence": 0.60,
            "risk": "low",
            "requires_approval": True,
        },
    ],
    "email_urgent": [
        {
            "action_type": "gmail.draft",
            "title": "Draft an urgent reply",
            "reason": "Urgent email received — prioritize response",
            "confidence": 0.85,
            "risk": "medium",
            "requires_approval": True,
        },
        {
            "action_type": "gmail.send",
            "title": "Send an immediate response",
            "reason": "Urgent email may require immediate action",
            "confidence": 0.70,
            "risk": "high",
            "requires_approval": True,
        },
    ],
    "calendar_changed": [
        {
            "action_type": "calendar.list",
            "title": "Review updated calendar",
            "reason": "Calendar event was modified — review changes",
            "confidence": 0.50,
            "risk": "low",
            "requires_approval": False,
        },
    ],
    "calendar_cancelled": [
        {
            "action_type": "calendar.list",
            "title": "Review cancelled events",
            "reason": "A calendar event was cancelled — check for conflicts",
            "confidence": 0.65,
            "risk": "low",
            "requires_approval": False,
        },
    ],
    "deadline_approaching": [
        {
            "action_type": "drive.search",
            "title": "Gather documents for upcoming deadline",
            "reason": "Deadline approaching — collect relevant files",
            "confidence": 0.75,
            "risk": "low",
            "requires_approval": False,
        },
        {
            "action_type": "gmail.draft",
            "title": "Draft deadline reminder email",
            "reason": "Deadline approaching — notify stakeholders",
            "confidence": 0.65,
            "risk": "medium",
            "requires_approval": True,
        },
    ],
    "document_shared": [
        {
            "action_type": "drive.search",
            "title": "Review shared documents",
            "reason": "New document shared — review contents",
            "confidence": 0.55,
            "risk": "low",
            "requires_approval": False,
        },
        {
            "action_type": "drive.download",
            "title": "Download shared document",
            "reason": "Document shared — download for offline review",
            "confidence": 0.50,
            "risk": "low",
            "requires_approval": False,
        },
    ],
    "unknown": [],
}


def _risk_for_action(action_type: str) -> str:
    """Determine risk level for an action type."""
    high_risk = {"gmail.send", "gmail.trash", "drive.trash", "calendar.cancel"}
    medium_risk = {"gmail.draft", "calendar.create", "calendar.update", "drive.upload"}
    if action_type in high_risk:
        return "high"
    if action_type in medium_risk:
        return "medium"
    return "low"


def _provider_for_action(action_type: str) -> str:
    """Recommend provider for an action type."""
    from workspace_capabilities import recommend_provider_for, supports
    rec = recommend_provider_for(action_type)
    return rec or "unknown"


# ─── Storage ──────────────────────────────────────────────────

def _project_root(config: Any) -> Path:
    root = None
    if isinstance(config, Mapping):
        paths = config.get("paths", {})
        if isinstance(paths, Mapping):
            root = paths.get("project_root")
    if not root:
        root = os.getenv("CHIEF_OF_STAFF_PROJECT_ROOT",
                         str(Path.home() / ".hermes" / "projects" / "default"))
    return Path(str(root)).expanduser()


def _suggestions_path(config: Any) -> Path:
    return _project_root(config) / ".suggestions.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(config: Any) -> dict[str, Any]:
    path = _suggestions_path(config)
    if not path.exists():
        return {"suggestions": {}, "_version": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "suggestions" not in data:
            return {"suggestions": {}, "_version": 0}
        return data
    except (json.JSONDecodeError, OSError):
        return {"suggestions": {}, "_version": 0}


def _save(config: Any, data: dict[str, Any], expected_version: int | None = None) -> int:
    path = _suggestions_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    if expected_version is not None:
        current = _load(config)
        if current.get("_version", 0) != expected_version:
            from pending_actions import ConcurrencyError
            raise ConcurrencyError("Suggestions store changed since load")
    new_version = (data.get("_version", 0) or 0) + 1
    data["_version"] = new_version
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)
    return new_version


# ─── Suggestion Generation ────────────────────────────────────

def generate_suggestions(config: Any, event: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate structured suggestions from a classified event.

    Returns a list of suggestion dicts. Does NOT execute anything.
    Does NOT create pending actions. Does NOT call provider methods.
    """
    classification = event.get("classification", {})
    category = classification.get("category", "unknown")
    templates = SUGGESTION_TEMPLATES.get(category, [])

    suggestions = []
    for template in templates:
        action_type = template["action_type"]
        sug_id = f"sug_{uuid.uuid4().hex[:10]}"
        suggestion = {
            "id": sug_id,
            "event_id": event.get("id", ""),
            "event_key": event.get("key", ""),
            "action_type": action_type,
            "title": template["title"],
            "reason": template["reason"],
            "confidence": template["confidence"],
            "risk": template.get("risk", _risk_for_action(action_type)),
            "provider": _provider_for_action(action_type),
            "requires_approval": template["requires_approval"],
            "auto_execute": False,  # ALWAYS false
            "state": "suggested",
            "created_at": _now(),
            "dismissed_at": None,
            "acted_on_at": None,
            "event_summary": event.get("summary", ""),
            "event_source": event.get("source", ""),
        }
        suggestions.append(suggestion)

    return suggestions


def generate_for_events(config: Any, event_ids: list[str] | None = None) -> dict[str, Any]:
    """Generate suggestions for events that don't have suggestions yet.

    If event_ids is None, generates for all classified/surfaced events.
    Returns summary: {generated, skipped, events_processed}
    """
    from event_store import list_events, get_event

    if event_ids is None:
        events = list_events(config, state="classified") + list_events(config, state="surfaced")
    else:
        events = []
        for eid in event_ids:
            ev = get_event(config, eid)
            if ev:
                events.append(ev)

    data = _load(config)
    expected_version = data.get("_version", 0)

    # Track which events already have suggestions
    existing_event_ids = {s.get("event_id") for s in data["suggestions"].values()}

    generated = 0
    skipped = 0
    for event in events:
        if event["id"] in existing_event_ids:
            skipped += 1
            continue
        suggestions = generate_suggestions(config, event)
        for sug in suggestions:
            data["suggestions"][sug["id"]] = sug
            generated += 1

    if generated:
        _save(config, data, expected_version=expected_version)

    return {"generated": generated, "skipped": skipped, "events_processed": len(events)}


# ─── Suggestion CRUD ──────────────────────────────────────────

def list_suggestions(
    config: Any,
    state: str | None = None,
    event_id: str | None = None,
    action_type: str | None = None,
    min_confidence: float | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List suggestions with optional filters."""
    data = _load(config)
    suggestions = list(data["suggestions"].values())
    if state:
        suggestions = [s for s in suggestions if s.get("state") == state]
    if event_id:
        suggestions = [s for s in suggestions if s.get("event_id") == event_id]
    if action_type:
        suggestions = [s for s in suggestions if s.get("action_type") == action_type]
    if min_confidence is not None:
        suggestions = [s for s in suggestions if s.get("confidence", 0) >= min_confidence]
    suggestions = sorted(suggestions, key=lambda s: s.get("confidence", 0), reverse=True)
    return suggestions[:limit]


def get_suggestion(config: Any, suggestion_id: str) -> dict[str, Any] | None:
    """Get a single suggestion by ID."""
    data = _load(config)
    return data["suggestions"].get(suggestion_id)


def dismiss_suggestion(config: Any, suggestion_id: str, reason: str | None = None) -> dict[str, Any] | None:
    """Dismiss a suggestion — marks it as dismissed."""
    data = _load(config)
    expected_version = data.get("_version", 0)
    sug = data["suggestions"].get(suggestion_id)
    if not sug or sug["state"] != "suggested":
        return None
    sug["state"] = "dismissed"
    sug["dismissed_at"] = _now()
    if reason:
        sug["dismiss_reason"] = reason
    _save(config, data, expected_version=expected_version)
    return sug


def mark_acted_on(config: Any, suggestion_id: str, notes: str | None = None) -> dict[str, Any] | None:
    """Mark a suggestion as acted on — operator took explicit action.

    This does NOT execute anything. It simply records that the operator
    handled this suggestion (e.g., via the approval queue separately).
    """
    data = _load(config)
    expected_version = data.get("_version", 0)
    sug = data["suggestions"].get(suggestion_id)
    if not sug or sug["state"] != "suggested":
        return None
    sug["state"] = "acted_on"
    sug["acted_on_at"] = _now()
    if notes:
        sug["action_notes"] = notes
    _save(config, data, expected_version=expected_version)
    return sug


def get_suggestion_summary(config: Any) -> dict[str, Any]:
    """Return summary of suggestions by state and risk."""
    data = _load(config)
    suggestions = list(data["suggestions"].values())
    by_state: dict[str, int] = {}
    by_risk: dict[str, int] = {}
    by_action: dict[str, int] = {}
    for s in suggestions:
        state = s.get("state", "unknown")
        by_state[state] = by_state.get(state, 0) + 1
        risk = s.get("risk", "unknown")
        by_risk[risk] = by_risk.get(risk, 0) + 1
        action = s.get("action_type", "unknown")
        by_action[action] = by_action.get(action, 0) + 1
    active = [s for s in suggestions if s["state"] == "suggested"]
    return {
        "total": len(suggestions),
        "by_state": by_state,
        "by_risk": by_risk,
        "by_action": by_action,
        "active_count": len(active),
    }


def cleanup_old_suggestions(config: Any, days: int = 30) -> int:
    """Remove dismissed/acted_on suggestions older than N days."""
    from datetime import datetime, timedelta
    data = _load(config)
    expected_version = data.get("_version", 0)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    removed = 0
    for sid in list(data["suggestions"].keys()):
        sug = data["suggestions"][sid]
        if sug["state"] in ("dismissed", "acted_on"):
            ts = sug.get("dismissed_at") or sug.get("acted_on_at") or sug.get("created_at") or ""
            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    if dt < cutoff:
                        del data["suggestions"][sid]
                        removed += 1
                except (ValueError, TypeError):
                    pass
    if removed:
        _save(config, data, expected_version=expected_version)
    return removed