#!/usr/bin/env python3
"""Event ingestion foundation — local event store with idempotency.

Events are inbound signals (emails, calendar changes, deadlines, etc.)
that the system receives, deduplicates, classifies, and surfaces as
suggested actions — but never automatically executes.

State machine: received → classified → surfaced → processed
                             ↓
                        ignored (duplicate or irrelevant)

Events are stored as JSON in project_root/.events.json.
Idempotency: each event has a source + source_id pair that uniquely
identifies it. Duplicate events (same source + source_id) are ignored.
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


def _events_path(config: Any) -> Path:
    return _project_root(config) / ".events.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(config: Any) -> dict[str, Any]:
    path = _events_path(config)
    if not path.exists():
        return {"events": {}, "_version": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "events" not in data:
            return {"events": {}, "_version": 0}
        if "_version" not in data:
            data["_version"] = 0
        return data
    except (json.JSONDecodeError, OSError):
        return {"events": {}, "_version": 0}


def _save(config: Any, data: dict[str, Any], expected_version: int | None = None) -> int:
    path = _events_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    if expected_version is not None:
        current = _load(config)
        if current.get("_version", 0) != expected_version:
            from pending_actions import ConcurrencyError
            raise ConcurrencyError(
                f"Events store changed since load (expected v{expected_version}, "
                f"found v{current.get('_version', 0)})."
            )
    new_version = (data.get("_version", 0) or 0) + 1
    data["_version"] = new_version
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)
    return new_version


# ─── Idempotency Key ──────────────────────────────────────────

def _idempotency_key(source: str, source_id: str) -> str:
    """Generate a deterministic key from source + source_id."""
    return f"{source}:{source_id}"


# ─── Event Classification ─────────────────────────────────────

# Classification categories for inbound events.
EVENT_CATEGORIES = {
    "email_received": {
        "label": "Email received",
        "suggested_actions": ["gmail.search", "gmail.draft"],
        "destructive": False,
    },
    "email_urgent": {
        "label": "Urgent email",
        "suggested_actions": ["gmail.search", "gmail.draft", "gmail.send"],
        "destructive": False,  # suggested, not auto-executed
    },
    "calendar_changed": {
        "label": "Calendar event changed",
        "suggested_actions": ["calendar.list"],
        "destructive": False,
    },
    "calendar_cancelled": {
        "label": "Calendar event cancelled",
        "suggested_actions": ["calendar.list"],
        "destructive": False,
    },
    "deadline_approaching": {
        "label": "Deadline approaching",
        "suggested_actions": ["calendar.list", "drive.search"],
        "destructive": False,
    },
    "document_shared": {
        "label": "Document shared",
        "suggested_actions": ["drive.search", "drive.download"],
        "destructive": False,
    },
    "unknown": {
        "label": "Unclassified event",
        "suggested_actions": [],
        "destructive": False,
    },
}


def classify_event(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Classify an inbound event and determine suggested actions.

    Returns classification dict with:
    - category: one of EVENT_CATEGORIES keys
    - suggested_actions: list of action types the operator might want
    - auto_execute: always False — no automatic destructive action
    """
    category = EVENT_CATEGORIES.get(event_type, EVENT_CATEGORIES["unknown"])
    return {
        "category": event_type if event_type in EVENT_CATEGORIES else "unknown",
        "label": category["label"],
        "suggested_actions": category["suggested_actions"],
        "auto_execute": False,  # NEVER auto-execute
        "destructive": category["destructive"],
    }


# ─── Event CRUD ───────────────────────────────────────────────

def ingest_event(
    config: Any,
    source: str,
    source_id: str,
    event_type: str,
    payload: dict[str, Any],
    summary: str | None = None,
) -> dict[str, Any] | None:
    """Ingest an inbound event with idempotency.

    If an event with the same source + source_id already exists, it is
    ignored (idempotent) and None is returned.

    Returns the new event dict if created, or None if duplicate.
    """
    key = _idempotency_key(source, source_id)
    data = _load(config)
    expected_version = data.get("_version", 0)

    # Idempotency check — duplicate event
    if key in data["events"]:
        return None

    classification = classify_event(event_type, payload)
    event_id = str(uuid.uuid4())[:12]

    event = {
        "id": event_id,
        "key": key,
        "source": source,
        "source_id": source_id,
        "event_type": event_type,
        "payload": payload,
        "summary": summary or f"{event_type} from {source}",
        "state": "received",
        "classification": classification,
        "received_at": _now(),
        "classified_at": _now(),  # classification happens at ingestion
        "surfaced_at": None,
        "processed_at": None,
        "processed_by": None,
        "processing_notes": None,
    }

    data["events"][key] = event
    _save(config, data, expected_version=expected_version)
    return event


def list_events(
    config: Any,
    state: str | None = None,
    source: str | None = None,
    category: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List events with optional filters."""
    data = _load(config)
    events = list(data["events"].values())
    if state:
        events = [e for e in events if e.get("state") == state]
    if source:
        events = [e for e in events if e.get("source") == source]
    if category:
        events = [e for e in events if e.get("classification", {}).get("category") == category]
    events = sorted(events, key=lambda e: e.get("received_at", ""), reverse=True)
    return events[:limit]


def get_event(config: Any, event_id: str) -> dict[str, Any] | None:
    """Get a single event by ID."""
    data = _load(config)
    # Search by event_id (not key)
    for event in data["events"].values():
        if event.get("id") == event_id:
            return event
    return None


def mark_surfaced(config: Any, event_id: str) -> dict[str, Any] | None:
    """Mark an event as surfaced (shown to operator)."""
    data = _load(config)
    expected_version = data.get("_version", 0)
    for event in data["events"].values():
        if event.get("id") == event_id and event["state"] == "classified":
            event["state"] = "surfaced"
            event["surfaced_at"] = _now()
            _save(config, data, expected_version=expected_version)
            return event
    return None


def mark_processed(
    config: Any,
    event_id: str,
    processed_by: str | None = None,
    notes: str | None = None,
) -> dict[str, Any] | None:
    """Mark an event as processed (operator has taken action or dismissed)."""
    data = _load(config)
    expected_version = data.get("_version", 0)
    for event in data["events"].values():
        if event.get("id") == event_id and event["state"] in ("received", "classified", "surfaced"):
            event["state"] = "processed"
            event["processed_at"] = _now()
            event["processed_by"] = processed_by
            event["processing_notes"] = notes
            _save(config, data, expected_version=expected_version)
            return event
    return None


def get_event_summary(config: Any) -> dict[str, Any]:
    """Return summary of events by state and category."""
    data = _load(config)
    events = list(data["events"].values())
    by_state: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for e in events:
        state = e.get("state", "unknown")
        by_state[state] = by_state.get(state, 0) + 1
        cat = e.get("classification", {}).get("category", "unknown")
        by_category[cat] = by_category.get(cat, 0) + 1
    pending = [e for e in events if e["state"] in ("received", "classified", "surfaced")]
    return {
        "total": len(events),
        "by_state": by_state,
        "by_category": by_category,
        "pending_count": len(pending),
    }


def cleanup_old_events(config: Any, days: int = 30) -> int:
    """Remove processed events older than N days. Returns count removed."""
    from datetime import datetime, timedelta
    data = _load(config)
    expected_version = data.get("_version", 0)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    removed = 0
    for key in list(data["events"].keys()):
        event = data["events"][key]
        if event["state"] == "processed":
            ts = event.get("processed_at") or event.get("received_at") or ""
            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    if dt < cutoff:
                        del data["events"][key]
                        removed += 1
                except (ValueError, TypeError):
                    pass
    if removed:
        _save(config, data, expected_version=expected_version)
    return removed