#!/usr/bin/env python3
"""Weekly review workspace data collection via WorkspaceClient.

Read-only: gathers Gmail, Calendar, and Drive data for the Friday weekly review.

Commands:
    workspace_collect.py all --week-start 2026-07-06
    workspace_collect.py gmail --query "is:unread" --max 20
    workspace_collect.py calendar --start 2026-07-06 --end 2026-07-10
    workspace_collect.py drive --query "" --max 20
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


def collect_gmail(config: Any, query: str = "is:unread", max_results: int = 20) -> list[dict[str, Any]]:
    client = get_client(config)
    return client.gmail_search(query, max_results=max_results)


def collect_calendar(config: Any, start: str, end: str) -> list[dict[str, Any]]:
    client = get_client(config)
    return client.calendar_list(start, end)


def collect_drive(config: Any, query: str = "", max_results: int = 20) -> list[dict[str, Any]]:
    client = get_client(config)
    return client.drive_search(query, max_results=max_results)


def cmd_all(args: argparse.Namespace) -> int:
    """Gather all workspace data for the week."""
    cfg = load_config(args.config)
    if cfg is None:
        print("Could not load config", file=sys.stderr)
        return 1

    week_start = args.week_start or date.today().isoformat()
    start_date = date.fromisoformat(week_start)
    week_end = (start_date + timedelta(days=6)).isoformat()

    result: dict[str, Any] = {
        "week_start": week_start,
        "week_end": week_end,
        "gmail_unread": collect_gmail(cfg, max_results=args.max),
        "calendar_events": collect_calendar(cfg, week_start, week_end),
        "drive_recent": collect_drive(cfg, max_results=args.max),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_gmail(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    results = collect_gmail(cfg, query=args.query, max_results=args.max)
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_calendar(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    results = collect_calendar(cfg, args.start, args.end)
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_drive(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    results = collect_drive(cfg, query=args.query, max_results=args.max)
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Weekly review workspace collection via WorkspaceClient")
    parser.add_argument("--config", help="Path to company.yaml")
    parser.add_argument("--max", type=int, default=20, help="Max items per source")
    sub = parser.add_subparsers(dest="command", required=True)

    all_cmd = sub.add_parser("all", help="Gather all workspace data for the week")
    all_cmd.add_argument("--week-start", help="Week start date (YYYY-MM-DD)")

    sub.add_parser("gmail", help="Collect Gmail data")
    sub.add_parser("calendar", help="Collect Calendar data")
    sub.add_parser("drive", help="Collect Drive data")

    # Add per-command args
    for name in ("gmail", "calendar", "drive"):
        cmd_parser = sub.choices[name]
        if name in ("gmail", "drive"):
            cmd_parser.add_argument("--query", default="")
        if name == "calendar":
            cmd_parser.add_argument("--start", required=True)
            cmd_parser.add_argument("--end", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "all":
            return cmd_all(args)
        elif args.command == "gmail":
            return cmd_gmail(args)
        elif args.command == "calendar":
            return cmd_calendar(args)
        elif args.command == "drive":
            return cmd_drive(args)
        else:
            parser.error("unknown command")
            return 2
    except Exception as exc:
        print(f"workspace_collect.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())