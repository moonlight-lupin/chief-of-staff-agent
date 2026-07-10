#!/usr/bin/env python3
"""Webhook payload adapters — convert native provider payloads into event_store events.

Supports:
- Gmail Pub/Sub push envelopes (base64url-decoded message.data)
- Google Calendar push (X-Goog-* headers, empty body)
- Google Drive push (X-Goog-* headers, empty body)
- Generic signed webhooks

Each adapter extracts a provider-native delivery ID for deduplication.
Adapters NEVER execute, approve, or mutate anything.
"""
from __future__ import annotations

import base64
import json
import hashlib
from typing import Any


def _pad_b64(s: str) -> str:
    """Add padding to a base64url string."""
    return s + "=" * (-len(s) % 4)


def adapt_gmail_pubsub(payload: dict[str, Any]) -> dict[str, Any]:
    """Adapt a Gmail Pub/Sub push notification.

    Native Gmail push arrives as a Cloud Pub/Sub envelope:
    {
      "message": {
        "data": "<base64url-encoded JSON with emailAddress, historyId>",
        "messageId": "1234567890",
        "publishTime": "2026-07-10T..."
      },
      "subscription": "projects/x/subscriptions/y"
    }
    """
    msg = payload.get("message", {})
    encoded = msg.get("data", "")
    message_id = str(msg.get("messageId", ""))
    publish_time = msg.get("publishTime", "")

    gmail_data: dict[str, Any] = {}
    if encoded:
        try:
            decoded = base64.urlsafe_b64decode(_pad_b64(encoded))
            gmail_data = json.loads(decoded)
        except Exception:
            gmail_data = {}

    email = gmail_data.get("emailAddress", "")
    history_id = str(gmail_data.get("historyId", ""))

    # Use Pub/Sub messageId for dedup — unique per delivery
    if message_id:
        source_id = f"gmail-pubsub-{message_id}"
    elif history_id:
        source_id = f"gmail-history-{history_id}"
    else:
        source_id = f"gmail-{email}-{message_id or 'unknown'}"

    return {
        "source": "webhook.gmail",
        "source_id": source_id,
        "event_type": "email_received",
        "payload": {
            "provider": "gmail",
            "email_address": email,
            "history_id": history_id,
            "message_id": "",
            "thread_id": gmail_data.get("threadId", ""),
            "pubsub_message_id": message_id,
            "publish_time": publish_time,
        },
        "summary": f"Gmail Pub/Sub: {email} (history {history_id}, msg {message_id})",
        "delivery_id": message_id or source_id,
    }


def adapt_gmail(payload: dict[str, Any]) -> dict[str, Any]:
    """Adapt a direct (non-Pub/Sub) Gmail payload."""
    email = payload.get("emailAddress", "")
    history_id = str(payload.get("historyId", ""))
    message_id = payload.get("messageId", "")

    source_id = f"gmail-history-{history_id}" if history_id else f"gmail-{email}"

    return {
        "source": "webhook.gmail",
        "source_id": source_id,
        "event_type": "email_received",
        "payload": {
            "provider": "gmail",
            "email_address": email,
            "history_id": history_id,
            "message_id": message_id,
            "thread_id": payload.get("threadId", ""),
        },
        "summary": f"Gmail webhook: {email} (history {history_id})",
        "delivery_id": history_id or source_id,
    }


def adapt_calendar_headers(headers: dict[str, str]) -> dict[str, Any]:
    """Adapt a Google Calendar push notification from X-Goog-* headers.

    Calendar notifications have empty bodies. All data is in headers:
    X-Goog-Channel-ID, X-Goog-Message-Number, X-Goog-Resource-ID,
    X-Goog-Resource-State, X-Goog-Resource-URI, X-Goog-Channel-Token
    """
    channel_id = headers.get("X-Goog-Channel-ID", "")
    message_number = headers.get("X-Goog-Message-Number", "")
    resource_id = headers.get("X-Goog-Resource-ID", "")
    resource_state = headers.get("X-Goog-Resource-State", "unknown")
    resource_uri = headers.get("X-Goog-Resource-URI", "")
    channel_token = headers.get("X-Goog-Channel-Token", "")

    # Dedup by channel + message number — unique per delivery
    if channel_id and message_number:
        source_id = f"calendar-{channel_id}-{message_number}"
    else:
        source_id = f"calendar-{resource_id}-{resource_state}"

    event_type = "calendar_changed"
    if resource_state == "not_exists":
        event_type = "calendar_cancelled"
    elif resource_state == "sync":
        event_type = "calendar_sync"

    return {
        "source": "webhook.calendar",
        "source_id": source_id,
        "event_type": event_type,
        "payload": {
            "provider": "googlecalendar",
            "channel_id": channel_id,
            "message_number": message_number,
            "resource_id": resource_id,
            "resource_state": resource_state,
            "resource_uri": resource_uri,
        },
        "summary": f"Calendar push: {resource_state} (channel {channel_id}, msg {message_number})",
        "delivery_id": f"{channel_id}:{message_number}" if channel_id and message_number else source_id,
    }


def adapt_drive_headers(headers: dict[str, str]) -> dict[str, Any]:
    """Adapt a Google Drive push notification from X-Goog-* headers."""
    channel_id = headers.get("X-Goog-Channel-ID", "")
    message_number = headers.get("X-Goog-Message-Number", "")
    resource_id = headers.get("X-Goog-Resource-ID", "")
    resource_state = headers.get("X-Goog-Resource-State", "unknown")
    resource_uri = headers.get("X-Goog-Resource-URI", "")

    # Dedup by channel + message number
    if channel_id and message_number:
        source_id = f"drive-{channel_id}-{message_number}"
    else:
        source_id = f"drive-{resource_id}-{resource_state}"

    event_type = "document_shared"
    if resource_state == "not_exists":
        event_type = "document_deleted"

    return {
        "source": "webhook.drive",
        "source_id": source_id,
        "event_type": event_type,
        "payload": {
            "provider": "googledrive",
            "channel_id": channel_id,
            "message_number": message_number,
            "resource_id": resource_id,
            "resource_state": resource_state,
            "resource_uri": resource_uri,
        },
        "summary": f"Drive push: {resource_state} (channel {channel_id}, msg {message_number})",
        "delivery_id": f"{channel_id}:{message_number}" if channel_id and message_number else source_id,
    }


def adapt_generic(payload: dict[str, Any], delivery_id: str | None = None) -> dict[str, Any]:
    """Adapt a generic webhook payload."""
    source = payload.get("source", "webhook.generic")
    source_id = str(payload.get("source_id") or payload.get("id") or payload.get("event_id") or "")
    event_type = payload.get("event_type") or payload.get("type") or "generic_event"
    summary = payload.get("summary") or payload.get("message") or f"Webhook event: {event_type}"

    if not source_id:
        if delivery_id:
            source_id = f"generic-{delivery_id}"
        else:
            raw = json.dumps(payload, sort_keys=True).encode()
            source_id = f"generic-{hashlib.sha256(raw).hexdigest()[:12]}"

    return {
        "source": source,
        "source_id": source_id,
        "event_type": event_type,
        "payload": payload,
        "summary": summary,
        "delivery_id": delivery_id or source_id,
    }


def detect_provider_from_body(payload: dict[str, Any]) -> str:
    """Auto-detect provider from body content."""
    # Pub/Sub envelope for Gmail
    if "message" in payload and isinstance(payload.get("message"), dict):
        msg = payload["message"]
        if "data" in msg:
            return "gmail_pubsub"
    # Direct Gmail
    if "emailAddress" in payload or "historyId" in payload:
        return "gmail"
    return "generic"


def detect_provider_from_headers(headers: dict[str, str]) -> str | None:
    """Detect provider from HTTP headers (Calendar/Drive X-Goog-*)."""
    if headers.get("X-Goog-Resource-ID") or headers.get("X-Goog-Channel-ID"):
        # Distinguish Calendar from Drive — check resource URI
        uri = headers.get("X-Goog-Resource-URI", "")
        if "calendar" in uri.lower():
            return "calendar"
        if "drive" in uri.lower():
            return "drive"
        # Fallback: if we can't tell, treat as calendar
        return "calendar"
    return None


def adapt_for_endpoint(
    endpoint: str,
    body: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    """Route to the correct adapter based on endpoint path.

    Endpoints:
    /webhooks/gmail    → Gmail (Pub/Sub or direct)
    /webhooks/calendar → Calendar (X-Goog headers)
    /webhooks/drive    → Drive (X-Goog headers)
    /webhooks/generic  → Generic
    """
    if endpoint == "/webhooks/gmail":
        # Check if it's a Pub/Sub envelope
        if "message" in body and isinstance(body.get("message"), dict) and "data" in body.get("message", {}):
            return adapt_gmail_pubsub(body)
        return adapt_gmail(body)

    elif endpoint == "/webhooks/calendar":
        return adapt_calendar_headers(headers)

    elif endpoint == "/webhooks/drive":
        return adapt_drive_headers(headers)

    else:
        # Generic endpoint — try auto-detection
        provider = detect_provider_from_headers(headers)
        if provider == "calendar":
            return adapt_calendar_headers(headers)
        elif provider == "drive":
            return adapt_drive_headers(headers)
        provider = detect_provider_from_body(body)
        if provider == "gmail_pubsub":
            return adapt_gmail_pubsub(body)
        elif provider == "gmail":
            return adapt_gmail(body)
        return adapt_generic(body)