#!/usr/bin/env python3
"""Suggested actions CLI — generate, list, dismiss suggestions.

Suggestions are advisory only. No suggestion can execute a write/destructive
action or create an approval item without an explicit operator command.

Commands:
    suggest_actions.py generate [--event-id <id>]
    suggest_actions.py list [--state] [--action-type] [--min-confidence] [--limit N]
    suggest_actions.py get --suggestion-id <id>
    suggest_actions.py dismiss --suggestion-id <id> [--reason "..."]
    suggest_actions.py acted-on --suggestion-id <id> [--notes "..."]
    suggest_actions.py summary
    suggest_actions.py cleanup [--days N]
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


def cmd_generate(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from suggested_actions import generate_for_events
    event_ids = [args.event_id] if args.event_id else None
    result = generate_for_events(cfg, event_ids=event_ids)
    if args.summary:
        print(f"Generated {result['generated']} suggestion(s) "
              f"({result['skipped']} events already had suggestions, "
              f"{result['events_processed']} events processed)")
    else:
        print_json(result)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from suggested_actions import list_suggestions
    suggestions = list_suggestions(
        cfg, state=args.state, action_type=args.action_type,
        min_confidence=args.min_confidence, limit=args.limit,
    )
    if args.summary:
        if not suggestions:
            print("No suggestions" + (f" with state={args.state}" if args.state else ""))
        else:
            for s in suggestions:
                icon = {"suggested": "💡", "dismissed": "❌", "acted_on": "✅"}.get(s["state"], "?")
                risk_icon = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(s.get("risk", ""), "?")
                conf = f"{s['confidence']:.0%}" if isinstance(s.get("confidence"), (int, float)) else "?"
                print(f"{icon} {risk_icon} {s['id']}  {s['action_type']}  [conf={conf}]")
                print(f"   {s['title']}")
                print(f"   Reason: {s['reason']}")
                print(f"   Provider: {s.get('provider', '?')}  Approval: {s.get('requires_approval', '?')}")
    else:
        print_json(suggestions)
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from suggested_actions import get_suggestion
    sug = get_suggestion(cfg, args.suggestion_id)
    if not sug:
        print(f"Suggestion not found: {args.suggestion_id}", file=sys.stderr)
        return 1
    print_json(sug)
    return 0


def cmd_dismiss(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from suggested_actions import dismiss_suggestion
    sug = dismiss_suggestion(cfg, args.suggestion_id, reason=args.reason)
    if not sug:
        print(f"Suggestion not found or not in 'suggested' state: {args.suggestion_id}", file=sys.stderr)
        return 1
    print_json(sug)
    return 0


def cmd_acted_on(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from suggested_actions import mark_acted_on
    sug = mark_acted_on(cfg, args.suggestion_id, notes=args.notes)
    if not sug:
        print(f"Suggestion not found or not in 'suggested' state: {args.suggestion_id}", file=sys.stderr)
        return 1
    print_json(sug)
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from suggested_actions import get_suggestion_summary
    summary = get_suggestion_summary(cfg)
    if args.summary:
        print(f"Suggestions: {summary['total']} total, {summary['active_count']} active")
        for state, count in sorted(summary["by_state"].items()):
            print(f"  {state}: {count}")
        if summary["by_risk"]:
            print("By risk:")
            for risk, count in sorted(summary["by_risk"].items()):
                print(f"  {risk}: {count}")
    else:
        print_json(summary)
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from suggested_actions import cleanup_old_suggestions
    removed = cleanup_old_suggestions(cfg, days=args.days)
    if args.summary:
        print(f"Cleaned up {removed} old suggestion(s) older than {args.days} days")
    else:
        print_json({"removed": removed, "days": args.days})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Suggested actions — generate, list, dismiss")
    parser.add_argument("--config", help="Path to company.yaml")
    parser.add_argument("--summary", action="store_true", help="Print human-readable summary")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="Generate suggestions from events")
    gen.add_argument("--event-id", help="Generate for specific event (default: all classified/surfaced)")

    list_cmd = sub.add_parser("list", help="List suggestions")
    list_cmd.add_argument("--state", choices=["suggested", "dismissed", "acted_on"])
    list_cmd.add_argument("--action-type", help="Filter by action type")
    list_cmd.add_argument("--min-confidence", type=float, help="Minimum confidence (0.0-1.0)")
    list_cmd.add_argument("--limit", type=int, default=50)

    get_cmd = sub.add_parser("get", help="Get a single suggestion")
    get_cmd.add_argument("--suggestion-id", required=True)

    dismiss = sub.add_parser("dismiss", help="Dismiss a suggestion")
    dismiss.add_argument("--suggestion-id", required=True)
    dismiss.add_argument("--reason", help="Dismissal reason")

    acted = sub.add_parser("acted-on", help="Mark suggestion as acted on")
    acted.add_argument("--suggestion-id", required=True)
    acted.add_argument("--notes", help="Action notes")

    sub.add_parser("summary", help="Print suggestion summary")

    cleanup = sub.add_parser("cleanup", help="Remove old dismissed/acted_on suggestions")
    cleanup.add_argument("--days", type=int, default=30)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            return cmd_generate(args)
        elif args.command == "list":
            return cmd_list(args)
        elif args.command == "get":
            return cmd_get(args)
        elif args.command == "dismiss":
            return cmd_dismiss(args)
        elif args.command == "acted-on":
            return cmd_acted_on(args)
        elif args.command == "summary":
            return cmd_summary(args)
        elif args.command == "cleanup":
            return cmd_cleanup(args)
        else:
            parser.error("unknown command")
            return 2
    except Exception as exc:
        print(f"suggest_actions.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())