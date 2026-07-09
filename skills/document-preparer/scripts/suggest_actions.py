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


def cmd_digest(args: argparse.Namespace) -> int:
    """Render a suggestion digest — no execution, no delivery."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from suggested_actions import render_digest
    digest = render_digest(
        cfg, state=args.state,
        min_confidence=args.min_confidence, limit=args.limit,
    )
    if args.summary:
        print(digest["text"])
    else:
        print_json(digest)
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    """Deliver suggestion digest via a channel.

    CLI channel: prints digest to stdout.
    Email channel: creates a pending action (NOT auto-sent) for operator approval.

    Notification CANNOT approve, execute, or create pending actions
    (except the email-to-self delivery which goes through approval queue).
    """
    cfg = load_config(args.config)
    if cfg is None:
        return 1

    from suggested_actions import render_digest, deliver_cli_digest, deliver_email_digest, mark_notified, list_suggestions

    digest = render_digest(
        cfg, state=args.state,
        min_confidence=args.min_confidence, limit=args.limit,
    )

    if digest["total"] == 0:
        if args.summary:
            print("No suggestions to notify")
        else:
            print_json({"delivered": False, "reason": "no_suggestions"})
        return 0

    if args.channel == "cli":
        deliver_cli_digest(cfg, digest)
        # Mark suggestions as notified — use digest items so filtering aligns
        sug_ids = [item["id"] for item in digest["items"]]
        marked = mark_notified(cfg, sug_ids)
        if not args.summary:
            print_json({"delivered": True, "channel": "cli", "marked_notified": marked})
        return 0

    elif args.channel == "email":
        if not args.to:
            print("--to is required for email channel", file=sys.stderr)
            return 1
        result = deliver_email_digest(cfg, digest, to=args.to, subject=args.subject)
        if args.summary:
            if result.get("success"):
                print(f"Digest email prepared ({digest['total']} items) — "
                      f"approve with: send_email.py approve --action-id {result['action_id']}")
            else:
                print(f"Email delivery failed: {result.get('error', 'unknown')}")
        else:
            print_json(result)
        return 0 if result.get("success") else 1

    return 1


def cmd_act(args: argparse.Namespace) -> int:
    """Act on a suggestion — safe reads execute, writes create pending actions."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from suggested_actions import act_on_suggestion
    result = act_on_suggestion(cfg, args.suggestion_id, dry_run=args.dry_run)
    if args.summary:
        mode = result.get("mode", "error")
        if mode == "dry_run":
            print(f"Dry-run for: {result.get('title', '?')}")
            print(f"Action: {result.get('action_type', '?')}")
            print(f"Would execute directly: {result.get('would_execute_directly', False)}")
            print(f"Would create pending: {result.get('would_create_pending', False)}")
            print(f"Requires approval: {result.get('requires_approval', False)}")
            print(f"Execution risk: {result.get('execution_risk', '?')}")
        elif mode == "read_executed":
            print(f"✅ Read executed: {result.get('action_type', '?')}")
        elif mode == "pending_created":
            print(f"📋 Pending action created: {result.get('action_type', '?')}")
            print(f"   {result.get('message', '')}")
        else:
            print(f"❌ {result.get('error', 'unknown error')}")
    else:
        print_json(result)
    return 0 if result.get("success") else 1


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

    digest = sub.add_parser("digest", help="Render a suggestion digest")
    digest.add_argument("--state", default="suggested", choices=["suggested", "dismissed", "acted_on"])
    digest.add_argument("--min-confidence", type=float, help="Minimum confidence (0.0-1.0)")
    digest.add_argument("--limit", type=int, default=20)

    notify = sub.add_parser("notify", help="Deliver suggestion digest via channel")
    notify.add_argument("--channel", required=True, choices=["cli", "email"],
                        help="Delivery channel")
    notify.add_argument("--to", help="Email recipient (for email channel)")
    notify.add_argument("--subject", default="Chief-of-Staff: Suggestion Digest")
    notify.add_argument("--state", default="suggested", choices=["suggested", "dismissed", "acted_on"])
    notify.add_argument("--min-confidence", type=float)
    notify.add_argument("--limit", type=int, default=20)

    act = sub.add_parser("act", help="Act on a suggestion (safe read or create pending action)")
    act.add_argument("--suggestion-id", required=True)
    act.add_argument("--dry-run", action="store_true", help="Show what would happen without executing")

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
        elif args.command == "digest":
            return cmd_digest(args)
        elif args.command == "notify":
            return cmd_notify(args)
        elif args.command == "act":
            return cmd_act(args)
        else:
            parser.error("unknown command")
            return 2
    except Exception as exc:
        print(f"suggest_actions.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())