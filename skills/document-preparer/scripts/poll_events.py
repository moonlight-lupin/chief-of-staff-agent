#!/usr/bin/env python3
"""Polling-based event ingestion connectors.

Polls Gmail and Calendar for new items and ingests them as events
via the event_store. Uses idempotency keys so re-polling is safe.

No automatic destructive action is triggered. Events are classified
and surfaced for operator action through the approval queue.

Usage:
    python poll_events.py --config <CONFIG> gmail [--max 10]
    python poll_events.py --config <CONFIG> calendar [--days 1]
    python poll_events.py --config <CONFIG> all [--max 10] [--days 1]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

from action_result_cli import print_json

try:
    from config_loader import load_config  # type: ignore
except Exception as exc:  # pragma: no cover
    print(f"Chief-of-Staff bootstrap incomplete: {exc}", file=sys.stderr)
    raise SystemExit(2)


def get_client(config: Any):
    from workspace_client import get_workspace_client
    return get_workspace_client(config)


def poll_gmail(config: Any, max_results: int = 10) -> dict[str, Any]:
    """Poll Gmail for recent emails and ingest as events.

    Returns summary: {polled, ingested, duplicates, errors}
    """
    from event_store import ingest_event

    client = get_client(config)
    result = {"polled": 0, "ingested": 0, "duplicates": 0, "errors": 0, "details": []}

    try:
        emails = client.gmail_search(query="is:inbox", max_results=max_results)
    except Exception as exc:
        result["errors"] = 1
        result["details"].append(f"gmail_search failed: {exc}")
        return result

    for email in emails:
        if not isinstance(email, dict):
            continue
        result["polled"] += 1
        msg_id = str(email.get("id") or email.get("messageId") or "")
        if not msg_id:
            continue

        # Determine if urgent (has urgent/important in subject or labels)
        subject = str(email.get("subject") or email.get("snippet") or "")
        labels = email.get("labelIds", [])
        is_urgent = "IMPORTANT" in labels or "urgent" in subject.lower()

        event_type = "email_urgent" if is_urgent else "email_received"
        payload = {
            "from": email.get("from") or email.get("sender") or "",
            "subject": subject,
            "snippet": email.get("snippet") or "",
            "labelIds": labels,
            "date": email.get("date") or email.get("internalDate") or "",
        }

        event = ingest_event(
            config=config,
            source="gmail",
            source_id=msg_id,
            event_type=event_type,
            payload=payload,
            summary=f"Email: {subject[:60]}" if subject else f"Email {msg_id}",
        )
        if event:
            result["ingested"] += 1
            result["details"].append(f"ingested: gmail:{msg_id} ({event_type})")
        else:
            result["duplicates"] += 1

    return result


def poll_calendar(config: Any, days: int = 1) -> dict[str, Any]:
    """Poll Calendar for events in the next N days and ingest as events.

    Returns summary: {polled, ingested, duplicates, errors}
    """
    from event_store import ingest_event
    from datetime import date, timedelta

    client = get_client(config)
    result = {"polled": 0, "ingested": 0, "duplicates": 0, "errors": 0, "details": []}

    today = date.today()
    start = today.isoformat()
    end = (today + timedelta(days=days)).isoformat()

    try:
        events = client.calendar_list(start, end)
    except Exception as exc:
        result["errors"] = 1
        result["details"].append(f"calendar_list failed: {exc}")
        return result

    for event in events:
        if not isinstance(event, dict):
            continue
        result["polled"] += 1
        event_id = str(event.get("id") or "")
        if not event_id:
            continue

        # Check if event was recently changed or cancelled
        status = str(event.get("status") or "").lower()
        if status == "cancelled":
            event_type = "calendar_cancelled"
        else:
            event_type = "calendar_changed"

        summary = str(event.get("summary") or event.get("title") or "Untitled")
        payload = {
            "title": summary,
            "start": event.get("start"),
            "end": event.get("end"),
            "status": status,
            "attendees": event.get("attendees", []),
        }

        ev = ingest_event(
            config=config,
            source="calendar",
            source_id=event_id,
            event_type=event_type,
            payload=payload,
            summary=f"Calendar: {summary[:60]}",
        )
        if ev:
            result["ingested"] += 1
            result["details"].append(f"ingested: calendar:{event_id} ({event_type})")
        else:
            result["duplicates"] += 1

    return result


def poll_drive(config: Any, max_results: int = 10) -> dict[str, Any]:
    """Poll Drive for recent files and ingest as events.

    Returns summary: {polled, ingested, duplicates, errors}
    """
    from event_store import ingest_event

    client = get_client(config)
    result = {"polled": 0, "ingested": 0, "duplicates": 0, "errors": 0, "details": []}

    try:
        files = client.drive_search(query="", max_results=max_results)
    except Exception as exc:
        result["errors"] = 1
        result["details"].append(f"drive_search failed: {exc}")
        return result

    for file_item in files:
        if not isinstance(file_item, dict):
            continue
        result["polled"] += 1
        file_id = str(file_item.get("id") or file_item.get("fileId") or "")
        if not file_id:
            continue

        name = str(file_item.get("name") or file_item.get("title") or "Untitled")
        mime = str(file_item.get("mimeType") or "")
        shared = bool(file_item.get("sharedWithMeTime") or file_item.get("shared"))

        event_type = "document_shared" if shared else "document_shared"
        payload = {
            "name": name,
            "mimeType": mime,
            "shared": shared,
            "modifiedTime": file_item.get("modifiedTime") or file_item.get("modified") or "",
            "webViewLink": file_item.get("webViewLink") or "",
        }

        ev = ingest_event(
            config=config,
            source="drive",
            source_id=file_id,
            event_type=event_type,
            payload=payload,
            summary=f"Document: {name[:60]}",
        )
        if ev:
            result["ingested"] += 1
            result["details"].append(f"ingested: drive:{file_id}")
        else:
            result["duplicates"] += 1

    return result


def cmd_poll(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        print("Could not load config", file=sys.stderr)
        return 1

    results = {}
    if args.source in ("gmail", "all"):
        results["gmail"] = poll_gmail(cfg, max_results=args.max)
    if args.source in ("calendar", "all"):
        results["calendar"] = poll_calendar(cfg, days=args.days)
    if args.source in ("drive", "all"):
        results["drive"] = poll_drive(cfg, max_results=args.max)

    if args.summary:
        for source, res in results.items():
            print(f"{source}: polled={res['polled']} ingested={res['ingested']} "
                  f"duplicates={res['duplicates']} errors={res['errors']}")
            for detail in res["details"]:
                print(f"  {detail}")
    else:
        print_json(results)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Poll-based event ingestion")
    parser.add_argument("--config", help="Path to company.yaml")
    parser.add_argument("--summary", action="store_true", help="Print human-readable summary")
    parser.add_argument("source", choices=["gmail", "calendar", "drive", "all"],
                        help="What to poll")
    parser.add_argument("--max", type=int, default=10, help="Max emails to poll")
    parser.add_argument("--days", type=int, default=1, help="Calendar lookahead in days")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return cmd_poll(args)
    except Exception as exc:
        print(f"poll_events.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())