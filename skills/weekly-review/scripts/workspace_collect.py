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


def load_workspace_input(path: str) -> dict[str, Any]:
    """Load and validate an agent-fetched workspace envelope from PATH or stdin.

    ``path`` of ``"-"`` reads from stdin. Returns a normalized payload (defaults
    filled). Raises FileNotFoundError / ValueError / SchemaError on a missing
    file, malformed JSON, or a schema violation.
    """
    from schemas import normalize_workspace_payload  # SchemaError is a ValueError

    if path == "-":
        raw = sys.stdin.read()
    else:
        file_path = Path(path).expanduser()
        if not file_path.exists():
            raise FileNotFoundError(f"--input file not found: {file_path}")
        raw = file_path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--input is not valid JSON: {exc}")
    return normalize_workspace_payload(payload)


def collect_gmail(config: Any, query: str = "is:unread", max_results: int = 20) -> list[dict[str, Any]]:
    client = get_client(config)
    return client.mail_search(query, max_results=max_results)


def collect_calendar(config: Any, start: str, end: str) -> list[dict[str, Any]]:
    client = get_client(config)
    return client.calendar_list(start, end)


def collect_drive(config: Any, query: str = "", max_results: int = 20) -> list[dict[str, Any]]:
    client = get_client(config)
    return client.files_search(query, max_results=max_results)


def cmd_all(args: argparse.Namespace) -> int:
    """Gather all workspace data for the week."""
    workspace_input = _maybe_load_input(args)

    week_start = args.week_start or date.today().isoformat()
    start_date = date.fromisoformat(week_start)
    week_end = (start_date + timedelta(days=6)).isoformat()

    if workspace_input is not None:
        # Fetch/compute split: agent already fetched the workspace records.
        result: dict[str, Any] = {
            "week_start": week_start,
            "week_end": week_end,
            "gmail_unread": workspace_input.get("messages", []),
            "calendar_events": workspace_input.get("events", []),
            "drive_recent": workspace_input.get("files", []),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0

    cfg = load_config(args.config)
    if cfg is None:
        print("Could not load config", file=sys.stderr)
        return 1

    result = {
        "week_start": week_start,
        "week_end": week_end,
        "gmail_unread": collect_gmail(cfg, max_results=args.max),
        "calendar_events": collect_calendar(cfg, week_start, week_end),
        "drive_recent": collect_drive(cfg, max_results=args.max),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


def _maybe_load_input(args: argparse.Namespace) -> dict[str, Any] | None:
    """Return a normalized envelope if --input was passed, else None."""
    if getattr(args, "input", None):
        return load_workspace_input(args.input)
    return None


def cmd_gmail(args: argparse.Namespace) -> int:
    workspace_input = _maybe_load_input(args)
    if workspace_input is not None:
        print(json.dumps(workspace_input.get("messages", []), indent=2, ensure_ascii=False, default=str))
        return 0
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    results = collect_gmail(cfg, query=args.query, max_results=args.max)
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_calendar(args: argparse.Namespace) -> int:
    workspace_input = _maybe_load_input(args)
    if workspace_input is not None:
        print(json.dumps(workspace_input.get("events", []), indent=2, ensure_ascii=False, default=str))
        return 0
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    results = collect_calendar(cfg, args.start, args.end)
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_drive(args: argparse.Namespace) -> int:
    workspace_input = _maybe_load_input(args)
    if workspace_input is not None:
        print(json.dumps(workspace_input.get("files", []), indent=2, ensure_ascii=False, default=str))
        return 0
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
    parser.add_argument("--input", dest="input", help=(
        "Path to an agent-fetched workspace JSON envelope (or '-' for stdin) "
        "conforming to shared/scripts/schemas.py workspace payload schema. "
        "When set, records are read from this file instead of a workspace client."))
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