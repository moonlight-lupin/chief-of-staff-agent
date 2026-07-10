#!/usr/bin/env python3
"""Webhook events CLI — serve, inspect, replay, validate-secret.

Commands:
    webhook_events.py serve [--host 0.0.0.0] [--port 8787] [--generate-suggestions]
    webhook_events.py inspect [--limit 20]
    webhook_events.py replay --event-id <id> [--dry-run]
    webhook_events.py validate-secret
    webhook_events.py sign --body <json-string>

Safety:
- serve: starts HTTP receiver (verify → ingest → optionally suggest)
- inspect: shows webhook-originated events from event_store
- replay: re-runs suggestion generation for an event (dry-run supported)
- validate-secret: checks webhook secret is configured
- sign: generates HMAC signature for testing

No command executes, approves, or mutates external systems.
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

try:
    from config_loader import load_config  # type: ignore
except Exception as exc:  # pragma: no cover
    print(f"Chief-of-Staff bootstrap incomplete: {exc}", file=sys.stderr)
    raise SystemExit(2)

from action_result_cli import print_json


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the webhook receiver server."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from webhook_receiver import start_server
    start_server(
        cfg,
        host=args.host,
        port=args.port,
        generate_suggestions=args.generate_suggestions,
    )
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Inspect webhook-originated events from event_store."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from event_store import list_events
    events = list_events(cfg, limit=args.limit * 2)  # over-fetch to filter
    webhook_events = [e for e in events if e.get("source", "").startswith("webhook.")]
    webhook_events = webhook_events[:args.limit]

    if args.summary:
        if not webhook_events:
            print("No webhook events found")
        else:
            print(f"🌐 Webhook Events — {len(webhook_events)} shown")
            for ev in webhook_events:
                icon = {"classified": "📨", "surfaced": "👀", "processed": "✅"}.get(ev.get("state"), "?")
                print(f"{icon} {ev['id']}  {ev['source']}  [{ev.get('state', '?')}]")
                print(f"   Type: {ev.get('type', '?')}")
                print(f"   Source ID: {ev.get('source_id', '?')}")
                print(f"   Summary: {ev.get('summary', '')}")
    else:
        print_json(webhook_events)
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Re-run suggestion generation for a webhook event.

    Does NOT execute anything — only generates suggestions.
    """
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from event_store import get_event
    ev = get_event(cfg, args.event_id)
    if not ev:
        print(f"Event not found: {args.event_id}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"DRY-RUN: Would generate suggestions for event {ev['id']}")
        print(f"  Source: {ev.get('source')}")
        print(f"  Type: {ev.get('type')}")
        print(f"  No suggestions would be executed")
        return 0

    from suggested_actions import generate_for_events
    sugs = generate_for_events(cfg, event_ids=[ev["id"]])
    if args.summary:
        print(f"Generated {len(sugs) if sugs else 0} suggestion(s) for event {ev['id']}")
    else:
        print_json({"event_id": ev["id"], "suggestions": sugs or []})
    return 0


def cmd_validate_secret(args: argparse.Namespace) -> int:
    """Validate that webhook secret is configured."""
    from webhook_security import validate_secret_config
    result = validate_secret_config()
    if result["valid"]:
        print(f"✅ Webhook secret configured (length: {result['length']})")
        print(f"   Algorithm: {result['algorithm']}")
        print(f"   Header: {result['header']}")
        return 0
    else:
        print(f"❌ {result['error']}", file=sys.stderr)
        if "hint" in result:
            print(f"   {result['hint']}", file=sys.stderr)
        return 1


def cmd_sign(args: argparse.Namespace) -> int:
    """Generate HMAC signature for testing."""
    from webhook_security import get_webhook_secret, sign_payload
    secret = get_webhook_secret()
    if not secret:
        print("❌ CHIEF_OF_STAFF_WEBHOOK_SECRET not set", file=sys.stderr)
        return 1
    body = args.body.encode("utf-8")
    signature = sign_payload(body, secret)
    print(signature)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Webhook receiver CLI — serve, inspect, replay")
    parser.add_argument("--config", help="Path to company.yaml")
    parser.add_argument("--summary", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Start webhook receiver server")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--generate-suggestions", action="store_true",
                        help="Generate suggestions after ingestion (read-only)")

    inspect = sub.add_parser("inspect", help="Inspect webhook-originated events")
    inspect.add_argument("--limit", type=int, default=20)

    replay = sub.add_parser("replay", help="Re-run suggestion generation for an event")
    replay.add_argument("--event-id", required=True)
    replay.add_argument("--dry-run", action="store_true")

    sub.add_parser("validate-secret", help="Validate webhook secret configuration")

    sign = sub.add_parser("sign", help="Generate HMAC signature for testing")
    sign.add_argument("--body", required=True, help="Request body (JSON string)")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            return cmd_serve(args)
        elif args.command == "inspect":
            return cmd_inspect(args)
        elif args.command == "replay":
            return cmd_replay(args)
        elif args.command == "validate-secret":
            return cmd_validate_secret(args)
        elif args.command == "sign":
            return cmd_sign(args)
        else:
            parser.error("unknown command")
            return 2
    except Exception as exc:
        print(f"webhook_events.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())