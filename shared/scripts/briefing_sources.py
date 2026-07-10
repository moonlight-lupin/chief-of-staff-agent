#!/usr/bin/env python3
"""Read-only data collectors for Chief-of-Staff daily briefings.

This module only reads local Chief-of-Staff state files or read-only
workspace data. It must never approve, execute, create, send, mutate, or call
provider write methods.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

# Ensure shared/scripts is importable when run as a standalone script.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import email_classifier
import email_label_policy
import event_store
import pending_actions
import suggested_actions


_PENDING_STATES = ("requested", "approved", "executing", "executed", "failed")
_ACTIVE_PENDING_STATES = {"requested", "approved", "executing"}


def _get_default_project_root_fallback() -> Path:
    """Default project root for fallback paths (env-configurable)."""
    env = os.getenv("CHIEF_OF_STAFF_HERMES_HOME") or os.getenv("HERMES_HOME")
    home = Path(env).expanduser() if env else Path.home() / ".hermes"
    return home / "projects" / "default"


def _project_root(config: Any) -> Path:
    """Get project root from config, env, or the standard fallback."""
    root: object | None = None
    if isinstance(config, Mapping):
        paths = config.get("paths", {})
        if isinstance(paths, Mapping):
            root = paths.get("project_root")
    if not root:
        root = os.getenv("CHIEF_OF_STAFF_PROJECT_ROOT", str(_get_default_project_root_fallback()))
    return Path(str(root)).expanduser()


def _load_json(path: Path) -> Any:
    """Read JSON from disk. Returns None for missing or malformed data."""
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _items_from_store(data: Any, key: str = "items") -> list[dict[str, Any]]:
    """Extract list-like items from common Chief-of-Staff JSON store shapes."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, Mapping):
        return []

    container = data.get(key)
    if isinstance(container, Mapping):
        return [item for item in container.values() if isinstance(item, dict)]
    if isinstance(container, list):
        return [item for item in container if isinstance(item, dict)]

    # Graceful fallback for plain {id: item} stores.
    if key not in data:
        return [item for item in data.values() if isinstance(item, dict)]
    return []


def _parse_datetime(value: Any) -> datetime | None:
    """Parse a timestamp into an aware UTC datetime when possible."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _event_time(event: Mapping[str, Any]) -> datetime | None:
    """Return the best available event timestamp."""
    for key in ("created_at", "received_at", "classified_at", "updated_at"):
        dt = _parse_datetime(event.get(key))
        if dt is not None:
            return dt
    return None


def _calendar_event_time(event: Mapping[str, Any]) -> datetime | None:
    """Extract the start time from common calendar event shapes."""
    start = event.get("start")
    if isinstance(start, Mapping):
        for key in ("dateTime", "datetime", "date", "time"):
            dt = _parse_datetime(start.get(key))
            if dt is not None:
                return dt
    for key in ("start_time", "startTime", "start", "when", "dateTime", "date"):
        dt = _parse_datetime(event.get(key))
        if dt is not None:
            return dt
    return None


def _calendar_summary(event: Mapping[str, Any], when: datetime) -> dict[str, Any]:
    """Normalize a provider calendar event for briefing rendering."""
    summary = (
        event.get("summary")
        or event.get("title")
        or event.get("name")
        or event.get("subject")
        or "Calendar event"
    )
    return {
        "when": when.isoformat(),
        "summary": str(summary),
        "event_id": str(event.get("id") or event.get("event_id") or ""),
        "location": str(event.get("location") or ""),
    }


def collect_pending_actions(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Collect pending actions across all states for the briefing."""
    try:
        actions = pending_actions.list_pending_actions(config or {})
    except Exception:
        return []

    results: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, Mapping):
            continue
        results.append({
            "action_id": action.get("action_id") or action.get("id") or "",
            "type": action.get("type") or action.get("action_type") or "",
            "summary": action.get("summary") or action.get("title") or "",
            "state": action.get("state") or "",
            "created_at": action.get("created_at") or "",
            "target": action.get("target") or "",
        })
    return results


def collect_suggestions(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Collect currently suggested actions only."""
    try:
        suggestions = suggested_actions.list_suggestions(config or {})
    except Exception:
        return []
    return [
        dict(suggestion)
        for suggestion in suggestions
        if isinstance(suggestion, Mapping) and suggestion.get("state") == "suggested"
    ]


def collect_recent_events(
    config: dict[str, Any] | None,
    since_hours: int = 24,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Collect events created/received within the last ``since_hours`` hours."""
    try:
        hours = max(0, int(since_hours))
        max_items = max(0, int(limit))
    except (TypeError, ValueError):
        hours = 24
        max_items = 50

    try:
        events = event_store.list_events(config or {}, limit=max_items)
    except Exception:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        dt = _event_time(event)
        if dt is not None and dt >= cutoff:
            recent.append(dict(event))
    return recent


def collect_email_org_stats(config: dict[str, Any] | None) -> dict[str, int]:
    """Collect read-only email organisation counts for briefing display."""
    stats = {
        "classified": 0,
        "unmapped": 0,
        "archive_candidates": 0,
        "label_suggestions": 0,
        "pending_actions": 0,
    }

    try:
        root = _project_root(config or {})
        classifications = _items_from_store(
            _load_json(root / ".email_organisation_classifications.json"),
            key="items",
        )
        suggestions = _items_from_store(
            _load_json(root / ".email_organisation_suggestions.json"),
            key="items",
        )

        stats["classified"] = len(classifications)
        stats["unmapped"] = sum(1 for item in classifications if not item.get("category"))

        active_suggestions = [item for item in suggestions if item.get("state", "suggested") == "suggested"]
        stats["archive_candidates"] = sum(
            1 for item in active_suggestions if item.get("action_type") == "gmail.archive"
        )
        stats["label_suggestions"] = sum(
            1 for item in active_suggestions if item.get("action_type") == "gmail.label"
        )

        try:
            org_pending = email_classifier.list_pending_org(config or {})
        except Exception:
            org_pending = []
        stats["pending_actions"] = sum(
            1
            for action in org_pending
            if isinstance(action, Mapping) and action.get("state") in _ACTIVE_PENDING_STATES
        )
    except Exception:
        return stats

    # Touch imported policy module in a harmless way so this module explicitly
    # depends on the email organisation taxonomy without duplicating it.
    _ = getattr(email_label_policy, "CATEGORY_KEYWORDS", {})
    return stats


def collect_system_health(config: dict[str, Any] | None) -> dict[str, Any]:
    """Collect local state-file health without repairing or mutating anything."""
    pending_summary = {state: 0 for state in _PENDING_STATES}
    try:
        root = _project_root(config or {})
        pending_path = root / ".pending_actions.json"
        events_path = root / ".events.json"

        pending_data = _load_json(pending_path)
        for action in _items_from_store(pending_data, key="actions"):
            state = str(action.get("state") or "")
            if state in pending_summary:
                pending_summary[state] += 1

        state_files = "ok" if pending_path.exists() and events_path.exists() else "missing"
        return {
            "state_files": state_files,
            "pending_summary": pending_summary,
            "audit_dir": (root / ".audit").is_dir(),
            "runs_dir": (root / ".runs").is_dir(),
        }
    except Exception:
        return {
            "state_files": "missing",
            "pending_summary": pending_summary,
            "audit_dir": False,
            "runs_dir": False,
        }


def collect_calendar_summary(
    config: dict[str, Any] | None,
    hours_ahead: int = 48,
) -> list[dict[str, Any]]:
    """Collect read-only calendar events in the next ``hours_ahead`` hours.

    Calendar access is optional. If the google/workspace client is unavailable,
    misconfigured, or returns malformed data, this function returns an empty
    list instead of raising.
    """
    try:
        hours = max(0, int(hours_ahead))
    except (TypeError, ValueError):
        hours = 48

    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=hours)

    try:
        # Optional same-directory import: may instantiate google_workspace or
        # another read-only workspace provider. Only calendar_list is called.
        from workspace_client import get_workspace_client

        client = get_workspace_client(config or {})
        events = client.calendar_list(now.isoformat(), end.isoformat())
    except Exception:
        return []

    summaries: list[dict[str, Any]] = []
    try:
        for event in events:
            if not isinstance(event, Mapping):
                continue
            when = _calendar_event_time(event)
            if when is None or not (now <= when <= end):
                continue
            summaries.append(_calendar_summary(event, when))
    except Exception:
        return []

    return sorted(summaries, key=lambda item: item.get("when", ""))


def collect_knowledge_stats(config: object) -> dict[str, object]:
    """Read .knowledge/memory.json and .knowledge/memory_changes.json.

    Returns counts for the daily briefing knowledge maintenance section.
    Distinguishes memory records from wiki pages.
    Degrades gracefully if files don't exist.
    """
    try:
        root = _project_root(config)
    except Exception:
        return {}

    knowledge_dir = root / ".knowledge"
    memory_path = knowledge_dir / "memory.json"
    changes_path = knowledge_dir / "memory_changes.json"

    stats: dict[str, object] = {
        "total_records": 0,
        "memory_records_created": 0,
        "memory_records_updated": 0,
        "wiki_pages_created": 0,
        "wiki_pages_updated": 0,
        "duplicates_flagged": 0,
        "conflicts_flagged": 0,
        "observations_added": 0,
        "backlinks_added": 0,
        "open_questions_added": 0,
    }

    # Read memory records count
    try:
        if memory_path.exists():
            data = json.loads(memory_path.read_text(encoding="utf-8"))
            records = data.get("records", {})
            if isinstance(records, dict):
                stats["total_records"] = len(records)
    except Exception:
        pass

    # Read recent changes — distinguish memory vs wiki
    try:
        if changes_path.exists():
            data = json.loads(changes_path.read_text(encoding="utf-8"))
            changes = data.get("changes", [])
            if isinstance(changes, list):
                for ch in changes:
                    if not isinstance(ch, dict):
                        continue
                    ct = ch.get("change_type", "")
                    if ct == "memory_create":
                        stats["memory_records_created"] = stats["memory_records_created"] + 1 if isinstance(stats["memory_records_created"], int) else 1
                    elif ct == "memory_update":
                        stats["memory_records_updated"] = stats["memory_records_updated"] + 1 if isinstance(stats["memory_records_updated"], int) else 1
                    elif ct == "wiki_create":
                        stats["wiki_pages_created"] = stats["wiki_pages_created"] + 1 if isinstance(stats["wiki_pages_created"], int) else 1
                    elif ct == "wiki_update":
                        stats["wiki_pages_updated"] = stats["wiki_pages_updated"] + 1 if isinstance(stats["wiki_pages_updated"], int) else 1
                    elif ct == "duplicate_detected":
                        stats["duplicates_flagged"] = stats["duplicates_flagged"] + 1 if isinstance(stats["duplicates_flagged"], int) else 1
                    elif ct == "conflict_detected":
                        stats["conflicts_flagged"] = stats["conflicts_flagged"] + 1 if isinstance(stats["conflicts_flagged"], int) else 1
    except Exception:
        pass

    return stats


def collect_bookkeeper_stats(config: object) -> dict[str, object]:
    """Read .bookkeeper_invoice_candidates.json and invoices.yaml for
    daily briefing bookkeeper section.

    Degrades gracefully if files don't exist.
    """
    try:
        root = _project_root(config)
    except Exception:
        return {}

    candidates_path = root / ".bookkeeper_invoice_candidates.json"
    stats: dict[str, object] = {
        "candidates_found": 0,
        "candidates_needs_review": 0,
        "duplicate_warnings": 0,
        "pending_record_actions": 0,
        "outstanding_ap": "0",
        "outstanding_ar": "0",
        "overdue_count": 0,
    }

    # Read candidate store
    try:
        if candidates_path.exists():
            data = json.loads(candidates_path.read_text(encoding="utf-8"))
            candidates = data.get("candidates", {})
            if isinstance(candidates, dict):
                active = [c for c in candidates.values()
                          if isinstance(c, dict) and c.get("state") == "candidate"]
                stats["candidates_found"] = len(active)
                stats["candidates_needs_review"] = sum(
                    1 for c in active if c.get("validation_status") == "needs_review"
                )
                stats["duplicate_warnings"] = sum(
                    1 for c in active
                    if any(d.get("score", 0) >= 0.85 for d in c.get("duplicate_candidates", []))
                )
    except Exception:
        pass

    # Read pending actions for bookkeeper.invoice.record
    try:
        from pending_actions import list_pending_actions
        pending = list_pending_actions(config)
        stats["pending_record_actions"] = sum(
            1 for a in pending
            if a.get("type") == "bookkeeper.invoice.record" and a.get("state") == "requested"
        )
    except Exception:
        pass

    return stats


__all__ = [
    "collect_pending_actions",
    "collect_suggestions",
    "collect_recent_events",
    "collect_email_org_stats",
    "collect_system_health",
    "collect_calendar_summary",
    "collect_knowledge_stats",
    "collect_bookkeeper_stats",
]
