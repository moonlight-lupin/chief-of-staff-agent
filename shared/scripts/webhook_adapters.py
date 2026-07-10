#!/usr/bin/env python3
"""Webhook payload adapters — convert raw webhook payloads into event_store events.

Each adapter:
- Parses a provider-specific webhook payload
- Extracts source, source_id, event_type, and summary
- Returns a normalized event dict suitable for ingest_event()

Adapters NEVER execute, approve, or mutate anything.
"""
from __future__ import annotations

import json
from typing import Any


def adapt_gmail(payload: dict[str, Any]) -> dict[str, Any]:
    """Adapt a Gmail push notification payload.

    Gmail push notifications contain:
    - emailAddress: the Gmail address
    - historyId: the history ID for changes

    Returns normalized event dict.
    """
    email = payload.get("emailAddress", "")
    history_id = str(payload.get("historyId", ""))
    message_id = payload.get("messageId", "")

    source_id = f"gmail-history-{history_id}" if history_id else f"gmail-{email}"
    event_type = "email_received"
    summary = f"Gmail webhook: {email} (history {history_id})"

    return {
        "source": "webhook.gmail",
        "source_id": source_id,
        "event_type": event_type,
        "payload": {
            "provider": "gmail",
            "email_address": email,
            "history_id": history_id,
            "message_id": message_id,
            "thread_id": payload.get("threadId", ""),
        },
        "summary": summary,
    }


def adapt_calendar(payload: dict[str, Any]) -> dict[str, Any]:
    """Adapt a Google Calendar push notification payload.

    Calendar push notifications contain:
    - resource: resource URI
    - resourceId: the calendar resource ID
    - resourceState: exists|not_exists|sync
    - changed: type of change (created/updated/deleted)

    Returns normalized event dict.
    """
    resource_state = payload.get("resourceState", "unknown")
    resource_id = payload.get("resourceId", "")
    event_id = payload.get("eventId", resource_id)

    event_type = "calendar_changed"
    if resource_state == "not_exists":
        event_type = "calendar_cancelled"
    elif payload.get("changed") == "created":
        event_type = "calendar_created"

    source_id = f"calendar-{resource_id}-{resource_state}"
    summary = f"Calendar webhook: {resource_state} (resource {resource_id})"

    return {
        "source": "webhook.calendar",
        "source_id": source_id,
        "event_type": event_type,
        "payload": {
            "provider": "googlecalendar",
            "resource_id": resource_id,
            "resource_state": resource_state,
            "event_id": event_id,
        },
        "summary": summary,
    }


def adapt_drive(payload: dict[str, Any]) -> dict[str, Any]:
    """Adapt a Google Drive push notification payload."""
    resource_id = payload.get("resourceId", "")
    resource_state = payload.get("resourceState", "unknown")

    event_type = "document_shared"
    if resource_state == "not_exists":
        event_type = "document_deleted"

    source_id = f"drive-{resource_id}-{resource_state}"
    summary = f"Drive webhook: {resource_state} (resource {resource_id})"

    return {
        "source": "webhook.drive",
        "source_id": source_id,
        "event_type": event_type,
        "payload": {
            "provider": "googledrive",
            "resource_id": resource_id,
            "resource_state": resource_state,
        },
        "summary": summary,
    }


def adapt_generic(payload: dict[str, Any]) -> dict[str, Any]:
    """Adapt a generic webhook payload.

    Tries to extract common fields, falls back to sensible defaults.
    """
    source = payload.get("source", "webhook.generic")
    source_id = str(payload.get("source_id") or payload.get("id") or payload.get("event_id") or "")
    event_type = payload.get("event_type") or payload.get("type") or "generic_event"
    summary = payload.get("summary") or payload.get("message") or f"Webhook event: {event_type}"

    if not source_id:
        # Generate a source_id from the payload hash
        import hashlib
        source_id = f"generic-{hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]}"

    return {
        "source": source,
        "source_id": source_id,
        "event_type": event_type,
        "payload": payload,
        "summary": summary,
    }


# Adapter registry
ADAPTERS = {
    "gmail": adapt_gmail,
    "calendar": adapt_calendar,
    "drive": adapt_drive,
    "generic": adapt_generic,
}


def adapt_payload(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Route to the correct adapter based on provider name.

    Falls back to generic adapter for unknown providers.
    """
    adapter = ADAPTERS.get(provider, adapt_generic)
    return adapter(payload)


def detect_provider(payload: dict[str, Any]) -> str:
    """Auto-detect the provider from payload shape."""
    if "emailAddress" in payload or "historyId" in payload:
        return "gmail"
    if "resourceState" in payload and "resourceId" in payload:
        # Could be calendar or drive — check for calendar-specific fields
        if "eventId" in payload or "calendarId" in payload:
            return "calendar"
        return "drive"
    return "generic"