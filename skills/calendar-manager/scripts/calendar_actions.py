#!/usr/bin/env python3
"""Calendar scan and create using WorkspaceClient — replaces direct google_api.py calls.

Commands:
    calendar_actions.py scan --today/--tomorrow
    calendar_actions.py create --title "Team Sync" --start 2026-07-10 --end 2026-07-10
    calendar_actions.py update --event-id <id> --title "New Title"
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

from action_result_cli import print_result

try:
    from config_loader import load_config  # type: ignore
except Exception as exc:  # pragma: no cover
    print(f"Chief-of-Staff bootstrap incomplete: {exc}", file=sys.stderr)
    raise SystemExit(2)


def get_client(config: Any):
    from workspace_client import get_workspace_client
    return get_workspace_client(config)


def extract_meet_link(event: dict[str, Any]) -> str | None:
    for key in ("hangoutLink", "meet_link", "meetLink"):
        if event.get(key):
            return str(event[key])
    conf = event.get("conferenceData") or {}
    if isinstance(conf, dict):
        for entry in conf.get("entryPoints", []) or []:
            if isinstance(entry, dict) and entry.get("entryPointType") == "video" and entry.get("uri"):
                return str(entry["uri"])
    import re
    text = json.dumps(event, default=str)
    m = re.search(r"https://meet\.google\.com/[a-z0-9-]+", text, re.IGNORECASE)
    return m.group(0) if m else None


def normalize_event(event: dict[str, Any]) -> dict[str, Any] | None:
    link = extract_meet_link(event)
    if not link:
        return None
    attendees = []
    for attendee in event.get("attendees", []) or []:
        if isinstance(attendee, dict):
            attendees.append(attendee.get("email") or attendee.get("displayName") or attendee)
        else:
            attendees.append(attendee)
    start = event.get("start")
    end = event.get("end")
    if isinstance(start, dict):
        start = start.get("dateTime") or start.get("date")
    if isinstance(end, dict):
        end = end.get("dateTime") or end.get("date")
    return {
        "id": event.get("id"),
        "title": event.get("summary") or event.get("title") or "Untitled event",
        "start": start,
        "end": end,
        "meet_link": link,
        "attendees": attendees,
    }


def cmd_scan(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        print("Could not load config", file=sys.stderr)
        return 1

    client = get_client(cfg)
    day = date.today() + (timedelta(days=1) if args.tomorrow else timedelta(days=0))
    start = day.isoformat()
    end = (day + timedelta(days=1)).isoformat()

    events = client.calendar_list(start, end)
    result = []
    for event in events:
        if isinstance(event, dict):
            normalized = normalize_event(event)
            if normalized:
                result.append(normalized)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        print("Could not load config", file=sys.stderr)
        return 1

    client = get_client(cfg)

    # Preflight: check capability
    from workspace_capabilities import require_capability
    unsupported = require_capability(client, "calendar.create", target=args.title)
    if unsupported:
        print_result(unsupported, args.summary, "Calendar event created")
        return 1

    # Dry-run: show plan without executing
    if args.dry_run:
        plan = {
            "success": True,
            "action": "calendar.create (dry-run)",
            "provider": client.provider_name,
            "target": args.title,
            "data": {"start": args.start, "end": args.end,
                     "attendees": args.attendees or "", "description": args.description or ""},
            "error": None,
            "audited": False,
        }
        print_result(plan, args.summary, "Calendar event would be created")
        return 0

    attendees = [a.strip() for a in args.attendees.split(",")] if args.attendees else None
    result = client.calendar_create(
        title=args.title,
        start=args.start,
        end=args.end,
        attendees=attendees,
        description=args.description,
    )
    print_result(result, args.summary, "Calendar event created")
    return 0 if result.get("success") else 1


def cmd_update(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        print("Could not load config", file=sys.stderr)
        return 1

    client = get_client(cfg)

    # Preflight: check capability
    from workspace_capabilities import require_capability
    unsupported = require_capability(client, "calendar.update", target=args.event_id)
    if unsupported:
        print_result(unsupported, args.summary, "Calendar event updated")
        return 1

    fields: dict[str, Any] = {}
    if args.title:
        fields["summary"] = args.title
    if args.start:
        fields["start_datetime"] = args.start
    if args.end:
        fields["end_datetime"] = args.end
    if not fields:
        print("No fields to update", file=sys.stderr)
        return 1

    # Dry-run: show plan without executing
    if args.dry_run:
        plan = {
            "success": True,
            "action": "calendar.update (dry-run)",
            "provider": client.provider_name,
            "target": args.event_id,
            "data": fields,
            "error": None,
            "audited": False,
        }
        print_result(plan, args.summary, "Calendar event would be updated")
        return 0

    result = client.calendar_update(args.event_id, **fields)
    print_result(result, args.summary, "Calendar event updated")
    return 0 if result.get("success") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Calendar operations via WorkspaceClient")
    parser.add_argument("--config", help="Path to company.yaml")
    parser.add_argument("--summary", action="store_true", help="Print human-readable summary")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="List Meet-enabled calendar events")
    day = scan.add_mutually_exclusive_group(required=True)
    day.add_argument("--today", action="store_true")
    day.add_argument("--tomorrow", action="store_true")

    create = sub.add_parser("create", help="Create a calendar event")
    create.add_argument("--title", required=True)
    create.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    create.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    create.add_argument("--attendees", help="Comma-separated email list")
    create.add_argument("--description")
    create.add_argument("--dry-run", action="store_true", help="Show plan without executing")

    update = sub.add_parser("update", help="Update a calendar event")
    update.add_argument("--event-id", required=True)
    update.add_argument("--title")
    update.add_argument("--start")
    update.add_argument("--end")
    update.add_argument("--dry-run", action="store_true", help="Show plan without executing")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            return cmd_scan(args)
        elif args.command == "create":
            return cmd_create(args)
        elif args.command == "update":
            return cmd_update(args)
        else:
            parser.error("unknown command")
            return 2
    except Exception as exc:
        print(f"calendar_actions.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())