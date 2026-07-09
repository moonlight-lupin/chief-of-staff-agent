#!/usr/bin/env python3
"""Meeting prep context gathering via WorkspaceClient.

Read-only: gathers Gmail threads + calendar events + Drive files for meeting context.

Commands:
    workspace_actions.py gather --event-id <id> --attendees a@x.com,b@y.com
    workspace_actions.py gmail-context --query "from:a@x.com" --max 5
    workspace_actions.py calendar-context --start 2026-07-09 --end 2026-07-16
    workspace_actions.py drive-context --query "meeting notes" --max 5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

try:
    from config_loader import load_config  # type: ignore
except Exception as exc:  # pragma: no cover
    print(f"Chief-of-Staff bootstrap incomplete: {exc}", file=sys.stderr)
    raise SystemExit(2)


def get_client(config: Any):
    from workspace_client import get_workspace_client
    return get_workspace_client(config)


def _find_event(events: list[dict[str, Any]], event_id: str) -> dict[str, Any] | None:
    """Find an event by ID from a list of calendar events."""
    for event in events:
        if event.get("id") == event_id:
            return event
    return None


def _extract_attendees(event: dict[str, Any]) -> list[str]:
    """Extract attendee emails from a calendar event."""
    attendees = []
    for attendee in event.get("attendees", []) or []:
        if isinstance(attendee, dict):
            email = attendee.get("email") or attendee.get("displayName")
            if email:
                attendees.append(email)
        elif isinstance(attendee, str):
            attendees.append(attendee)
    return attendees


def _get_event_dates(event: dict[str, Any]) -> tuple[str, str]:
    """Extract start/end dates from a calendar event."""
    start = event.get("start", {})
    end = event.get("end", {})
    if isinstance(start, dict):
        start = start.get("dateTime") or start.get("date") or ""
    if isinstance(end, dict):
        end = end.get("dateTime") or end.get("date") or ""
    return str(start), str(end)


def cmd_gather(args: argparse.Namespace) -> int:
    """Gather all context for a meeting in one call.

    Uses event_id to locate the matching calendar event, extract attendees,
    and define the context window. Falls back to manual attendees if provided.
    """
    cfg = load_config(args.config)
    if cfg is None:
        print("Could not load config", file=sys.stderr)
        return 1
    client = get_client(cfg)

    # Fetch calendar events for a 7-day window to find the event
    today = date.today()
    cal_start = today.isoformat()
    cal_end = (today + timedelta(days=7)).isoformat()
    events = client.calendar_list(cal_start, cal_end)

    # Find the specific event
    event = _find_event(events, args.event_id)
    if not event:
        result: dict[str, Any] = {
            "event": None,
            "error": f"Event {args.event_id} not found in {cal_start} to {cal_end}",
            "recent_related_events": events,
        }
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 1

    # Extract event details
    event_title = event.get("summary") or event.get("title") or "Untitled"
    event_start, event_end = _get_event_dates(event)

    # Determine attendees: use --attendees if passed, else extract from event
    if args.attendees:
        attendees = [a.strip() for a in args.attendees.split(",")]
    else:
        attendees = _extract_attendees(event)

    result = {
        "event": {
            "id": event.get("id"),
            "title": event_title,
            "start": event_start,
            "end": event_end,
            "attendees": attendees,
        },
    }

    # Gmail context per attendee
    gmail_items = []
    for email in attendees:
        messages = client.gmail_search(f"from:{email}", max_results=3)
        gmail_items.append({"attendee": email, "messages": messages})
    result["gmail_context"] = gmail_items

    # Recent related events (same window, excluding the target event)
    related = [e for e in events if e.get("id") != args.event_id]
    result["recent_related_events"] = related

    # Drive context: use event title or --drive-query
    drive_query = args.drive_query or event_title
    result["drive_files"] = client.drive_search(drive_query, max_results=5)

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_gmail_context(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    client = get_client(cfg)
    results = client.gmail_search(args.query, max_results=args.max)
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_calendar_context(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    client = get_client(cfg)
    results = client.calendar_list(args.start, args.end)
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_drive_context(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    client = get_client(cfg)
    results = client.drive_search(args.query, max_results=args.max)
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Meeting prep context via WorkspaceClient")
    parser.add_argument("--config", help="Path to company.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    gather = sub.add_parser("gather", help="Gather all meeting context")
    gather.add_argument("--event-id", required=True)
    gather.add_argument("--attendees", help="Comma-separated email list")
    gather.add_argument("--drive-query", help="Drive search query for meeting context")

    gmail = sub.add_parser("gmail-context", help="Search Gmail for attendee context")
    gmail.add_argument("--query", required=True)
    gmail.add_argument("--max", type=int, default=5)

    cal = sub.add_parser("calendar-context", help="List calendar events")
    cal.add_argument("--start", required=True)
    cal.add_argument("--end", required=True)

    drive = sub.add_parser("drive-context", help="Search Drive for meeting context")
    drive.add_argument("--query", required=True)
    drive.add_argument("--max", type=int, default=5)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "gather":
            return cmd_gather(args)
        elif args.command == "gmail-context":
            return cmd_gmail_context(args)
        elif args.command == "calendar-context":
            return cmd_calendar_context(args)
        elif args.command == "drive-context":
            return cmd_drive_context(args)
        else:
            parser.error("unknown command")
            return 2
    except Exception as exc:
        print(f"workspace_actions.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())