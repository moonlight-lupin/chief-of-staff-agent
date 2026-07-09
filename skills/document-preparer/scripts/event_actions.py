#!/usr/bin/env python3
"""Event ingestion CLI — inspect, classify, and process inbound events.

Events are ingested via the event_store module (polling-based, not webhooks).
This CLI lets operators inspect received events, see suggested actions,
and mark them as processed.

No automatic write/destructive action is ever triggered by an event.

Commands:
    event_actions.py list [--state] [--source] [--category] [--limit N]
    event_actions.py get --event-id <id>
    event_actions.py summary
    event_actions.py mark-processed --event-id <id> [--by name] [--notes "..."]
    event_actions.py cleanup [--days N]
    event_actions.py ingest --source <src> --source-id <sid> --type <type> [--summary "..."]
                           --payload-json '{"key": "value"}'
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


def cmd_list(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from event_store import list_events
    events = list_events(cfg, state=args.state, source=args.source,
                         category=args.category, limit=args.limit)
    if args.summary:
        if not events:
            print("No events" + (f" with state={args.state}" if args.state else ""))
        else:
            for e in events:
                icon = {"received": "📨", "classified": "📨", "surfaced": "👀",
                        "processed": "✅"}.get(e["state"], "?")
                cat = e.get("classification", {}).get("label", "?")
                print(f"{icon} {e['id']}  {e['source']}:{e['source_id']}  [{e['state']}]")
                print(f"   {cat} — {e.get('summary', '')}")
                sa = e.get("classification", {}).get("suggested_actions", [])
                if sa:
                    print(f"   Suggested: {', '.join(sa)}")
    else:
        print_json(events)
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from event_store import get_event
    event = get_event(cfg, args.event_id)
    if not event:
        print(f"Event not found: {args.event_id}", file=sys.stderr)
        return 1
    print_json(event)
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from event_store import get_event_summary
    summary = get_event_summary(cfg)
    if args.summary:
        print(f"Events: {summary['total']} total, {summary['pending_count']} pending")
        for state, count in sorted(summary["by_state"].items()):
            print(f"  {state}: {count}")
        if summary["by_category"]:
            print("By category:")
            for cat, count in sorted(summary["by_category"].items()):
                print(f"  {cat}: {count}")
    else:
        print_json(summary)
    return 0


def cmd_mark_processed(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from event_store import mark_processed
    event = mark_processed(cfg, args.event_id, processed_by=args.by, notes=args.notes)
    if not event:
        print(f"Event not found or already processed: {args.event_id}", file=sys.stderr)
        return 1
    print_json(event)
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from event_store import cleanup_old_events
    removed = cleanup_old_events(cfg, days=args.days)
    if args.summary:
        print(f"Cleaned up {removed} processed event(s) older than {args.days} days")
    else:
        print_json({"removed": removed, "days": args.days})
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """Manually ingest an event (for testing or polling-based import)."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from event_store import ingest_event
    try:
        payload = json.loads(args.payload_json) if args.payload_json else {}
    except json.JSONDecodeError as exc:
        print(f"Invalid --payload-json: {exc}", file=sys.stderr)
        return 1
    event = ingest_event(
        config=cfg,
        source=args.source,
        source_id=args.source_id,
        event_type=args.type,
        payload=payload,
        summary=args.summary,
    )
    if event is None:
        print(f"Duplicate event ignored: {args.source}:{args.source_id}", file=sys.stderr)
        return 0  # idempotent — not an error
    print_json(event)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Event ingestion — inspect, classify, process")
    parser.add_argument("--config", help="Path to company.yaml")
    parser.add_argument("--summary", action="store_true", help="Print human-readable summary")
    sub = parser.add_subparsers(dest="command", required=True)

    list_cmd = sub.add_parser("list", help="List events")
    list_cmd.add_argument("--state", choices=["received", "classified", "surfaced", "processed"])
    list_cmd.add_argument("--source", help="Filter by source")
    list_cmd.add_argument("--category", help="Filter by category")
    list_cmd.add_argument("--limit", type=int, default=50)

    get_cmd = sub.add_parser("get", help="Get a single event")
    get_cmd.add_argument("--event-id", required=True)

    sub.add_parser("summary", help="Print event summary by state and category")

    proc = sub.add_parser("mark-processed", help="Mark an event as processed")
    proc.add_argument("--event-id", required=True)
    proc.add_argument("--by", help="Who processed it")
    proc.add_argument("--notes", help="Processing notes")

    cleanup = sub.add_parser("cleanup", help="Remove old processed events")
    cleanup.add_argument("--days", type=int, default=30)

    ingest = sub.add_parser("ingest", help="Manually ingest an event")
    ingest.add_argument("--source", required=True, help="Event source (e.g. 'gmail', 'calendar')")
    ingest.add_argument("--source-id", required=True, help="Unique ID from the source")
    ingest.add_argument("--type", required=True, help="Event type (e.g. 'email_received')")
    ingest.add_argument("--summary", help="Human-readable summary")
    ingest.add_argument("--payload-json", help="JSON payload string")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            return cmd_list(args)
        elif args.command == "get":
            return cmd_get(args)
        elif args.command == "summary":
            return cmd_summary(args)
        elif args.command == "mark-processed":
            return cmd_mark_processed(args)
        elif args.command == "cleanup":
            return cmd_cleanup(args)
        elif args.command == "ingest":
            return cmd_ingest(args)
        else:
            parser.error("unknown command")
            return 2
    except Exception as exc:
        print(f"event_actions.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())