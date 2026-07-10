#!/usr/bin/env python3
"""Webhook events CLI — serve, inspect, replay, validate-secret, approve, execute.

Commands:
    webhook_events.py serve [--host 0.0.0.0] [--port 8787] [--generate-suggestions]
    webhook_events.py inspect [--limit 20]
    webhook_events.py replay --event-id <id> [--dry-run]
    webhook_events.py validate-secret
    webhook_events.py sign --body <json-string>
    webhook_events.py approve --action-id <id> --approver "MH" --reason "Reviewed"
    webhook_events.py execute --action-id <id>
    webhook_events.py pending [--summary]

Safety:
- serve: starts HTTP receiver (verify → ingest → optionally suggest)
- inspect: shows webhook-originated events from event_store
- replay: re-runs suggestion generation for an event (dry-run supported)
- validate-secret: checks webhook secret is configured
- sign: generates HMAC signature for testing
- approve/execute: generic pending-action routing for any action type

No command executes without explicit approval.
"""
from __future__ import annotations

import argparse
import json
import os
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


# ─── Action routing for approve/execute ───────────────────────

ACTION_APPROVAL_CLI = {
    "gmail.send": "send_email.py",
    "gmail.draft": "send_email.py",
    "gmail.archive": "delete_actions.py",
    "gmail.trash": "delete_actions.py",
    "gmail.label": "email_organisation.py",
    "gmail.create_label": "email_organisation.py",
    "calendar.cancel": "delete_actions.py",
    "calendar.create": "calendar_actions.py",
    "calendar.update": "calendar_actions.py",
    "drive.trash": "delete_actions.py",
    "drive.upload": "drive_file.py",
    "drive.download": "drive_file.py",
}


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
    # Use source filter — list_events supports exact match,
    # but we need prefix match for "webhook.*"
    # Fetch a larger set and filter, but use limit properly
    events = list_events(cfg, limit=args.limit * 10)
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
                print(f"   Type: {ev.get('event_type', ev.get('type', '?'))}")
                print(f"   Source ID: {ev.get('source_id', '?')}")
                print(f"   Summary: {ev.get('summary', '')}")
    else:
        print_json(webhook_events)
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Re-run suggestion generation for a webhook event."""
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
        print(f"  Type: {ev.get('event_type', ev.get('type', '?'))}")
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
    """Validate webhook security configuration."""
    from webhook_security import validate_secret_config
    result = validate_secret_config()
    if result["valid"]:
        print("✅ All webhook endpoints configured")
    else:
        print("⚠️  Configuration warnings:")
    for issue in result.get("issues", []):
        print(f"   {issue}")
    print()
    print("Endpoint status:")
    for ep, status in result.get("endpoints", {}).items():
        icon = "✅" if status == "enabled" else "❌"
        print(f"  {icon} /webhooks/{ep} — {status}")
    return 0 if result["valid"] else 1


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


def cmd_approve(args: argparse.Namespace) -> int:
    """Generic approve — works for any pending action type."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from pending_actions import approve_pending_action, get_pending_action
    action = get_pending_action(cfg, args.action_id)
    if not action:
        print(f"Action not found: {args.action_id}", file=sys.stderr)
        return 1
    result = approve_pending_action(cfg, args.action_id, approver=args.approver, reason=args.reason)
    if args.summary:
        if result:
            action_type = action.get("type", "?")
            cli = ACTION_APPROVAL_CLI.get(action_type, "pending_actions.py")
            print(f"✅ Approved: {action_type} ({args.action_id})")
            print(f"   Approver: {args.approver}")
            print(f"   Execute with: webhook_events.py execute --action-id {args.action_id}")
        else:
            print(f"❌ Approval failed")
    else:
        print_json({"approved": bool(result), "action_id": args.action_id})
    return 0 if result else 1


def cmd_execute(args: argparse.Namespace) -> int:
    """Generic execute — routes to provider based on action type."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from pending_actions import get_pending_action, mark_executing, mark_executed, mark_failed
    action = get_pending_action(cfg, args.action_id)
    if not action:
        print(f"Action not found: {args.action_id}", file=sys.stderr)
        return 1

    state = action.get("state", "")
    if state != "approved":
        print(f"Action not approved (state={state}). Run approve first.", file=sys.stderr)
        return 1

    action_type = action.get("type", "")
    payload = action.get("payload", {})

    # Reject unsupported action types BEFORE mark_executing
    if action_type == "gmail.draft":
        print(f"❌ gmail.draft execution not supported via generic executor. Use send_email.py or Composio MCP.", file=sys.stderr)
        return 1

    # Pre-execution gate
    executing = mark_executing(cfg, args.action_id)
    if not executing:
        print(f"Cannot execute (state={state}). Approval may have expired.", file=sys.stderr)
        return 1

    from workspace_client import get_workspace_client
    from workspace_capabilities import require_capability
    client = get_workspace_client(cfg)

    unsupported = require_capability(client, action_type, target=action.get("target", ""))
    if unsupported:
        mark_failed(cfg, args.action_id, f"{action_type} not supported by {client.provider_name}")
        print(f"❌ {action_type} not supported by {client.provider_name}", file=sys.stderr)
        return 1

    # Establish approved execution context — the explicit approval IS the confirmation.
    # Set env vars for guardrails, matching send_email.py and delete_actions.py behavior.
    # Restore previous values after execution (safe for tests/workers/long-running processes).
    old_auto = os.environ.get("CHIEF_OF_STAFF_AUTO_APPROVE")
    old_destructive = os.environ.get("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE")

    try:
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        os.environ["CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"] = "1"

        try:
            if action_type == "gmail.send":
                result = client.gmail_send(
                    to=payload.get("to", ""),
                    subject=payload.get("subject", ""),
                    body=payload.get("body", ""),
                    cc=payload.get("cc"),
                )
            elif action_type == "gmail.label":
                result = client.gmail_label(
                    message_id=payload.get("message_id", ""),
                    label_id=payload.get("label_id", ""),
                )
            elif action_type == "gmail.archive":
                result = client.gmail_archive(message_id=payload.get("message_id", ""))
            elif action_type == "gmail.create_label":
                result = client.gmail_create_label(label_name=payload.get("label", ""))
            elif action_type == "gmail.trash":
                result = client.gmail_trash(message_id=payload.get("message_id", ""))
            elif action_type == "calendar.create":
                result = client.calendar_create(
                    title=payload.get("summary", payload.get("title", "")),
                    start=payload.get("start", ""),
                    end=payload.get("end", ""),
                )
            elif action_type == "calendar.update":
                result = client.calendar_update(
                    event_id=payload.get("event_id", ""),
                    summary=payload.get("summary"),
                    start=payload.get("start"),
                    end=payload.get("end"),
                )
            elif action_type == "calendar.cancel":
                result = client.calendar_cancel(event_id=payload.get("event_id", ""))
            elif action_type == "drive.upload":
                result = client.drive_upload(
                    file_path=payload.get("file_path", payload.get("path", "")),
                    parent_id=payload.get("parent_id"),
                )
            elif action_type == "drive.download":
                result = client.drive_download(
                    file_id=payload.get("file_id", ""),
                    output_path=payload.get("output_path", payload.get("path", "")),
                )
            elif action_type == "drive.trash":
                result = client.drive_trash(file_id=payload.get("file_id", ""))
            else:
                mark_failed(cfg, args.action_id, f"Unknown action type: {action_type}")
                print(f"❌ Unknown action type: {action_type}", file=sys.stderr)
                return 1
        except Exception as exc:
            mark_failed(cfg, args.action_id, str(exc))
            if args.summary:
                print(f"❌ Execution failed: {exc}")
            else:
                print_json({"success": False, "error": str(exc)})
            return 1

        # Check provider result BEFORE marking executed
        success = result.get("success", False) if isinstance(result, dict) else True
        if not success:
            error = result.get("error", "provider returned failure") if isinstance(result, dict) else "unknown error"
            mark_failed(cfg, args.action_id, error)
            if args.summary:
                print(f"❌ Provider returned error: {error}")
            else:
                print_json({"success": False, "error": error, "result": result})
            return 1

        mark_executed(cfg, args.action_id, result if isinstance(result, dict) else {"raw": str(result)})

        if args.summary:
            print(f"✅ Executed: {action_type} ({args.action_id})")
        else:
            print_json({"success": True, "action_id": args.action_id, "action_type": action_type,
                         "result": result if isinstance(result, dict) else str(result)})
        return 0

    finally:
        if old_auto is None:
            os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)
        else:
            os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = old_auto
        if old_destructive is None:
            os.environ.pop("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", None)
        else:
            os.environ["CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"] = old_destructive


def cmd_pending(args: argparse.Namespace) -> int:
    """List all pending actions."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from pending_actions import list_pending_actions
    actions = list_pending_actions(cfg)
    if args.summary:
        if not actions:
            print("No pending actions")
        else:
            for a in actions:
                icon = {"requested": "📨", "approved": "✅", "executed": "✅",
                        "cancelled": "❌", "expired": "⏰", "executing": "⏳"}.get(a.get("state"), "?")
                cli = ACTION_APPROVAL_CLI.get(a.get("type", ""), "?")
                print(f"{icon} {a['id']}  {a.get('type', '?')}  [{a.get('state', '?')}]")
                print(f"   {a.get('summary', '')}")
                if a.get("state") == "requested":
                    print(f"   Approve: webhook_events.py approve --action-id {a['id']} --approver 'MH' --reason 'ok'")
    else:
        print_json(actions)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Webhook receiver and pending-action CLI")
    parser.add_argument("--config", help="Path to company.yaml")
    parser.add_argument("--summary", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Start webhook receiver server")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--generate-suggestions", action="store_true")

    inspect = sub.add_parser("inspect", help="Inspect webhook-originated events")
    inspect.add_argument("--limit", type=int, default=20)

    replay = sub.add_parser("replay", help="Re-run suggestion generation for an event")
    replay.add_argument("--event-id", required=True)
    replay.add_argument("--dry-run", action="store_true")

    sub.add_parser("validate-secret", help="Validate webhook secret configuration")

    sign = sub.add_parser("sign", help="Generate HMAC signature for testing")
    sign.add_argument("--body", required=True)

    approve = sub.add_parser("approve", help="Approve a pending action")
    approve.add_argument("--action-id", required=True)
    approve.add_argument("--approver", required=True)
    approve.add_argument("--reason", required=True)

    execute = sub.add_parser("execute", help="Execute an approved pending action")
    execute.add_argument("--action-id", required=True)

    pending = sub.add_parser("pending", help="List all pending actions")

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
        elif args.command == "approve":
            return cmd_approve(args)
        elif args.command == "execute":
            return cmd_execute(args)
        elif args.command == "pending":
            return cmd_pending(args)
        else:
            parser.error("unknown command")
            return 2
    except Exception as exc:
        print(f"webhook_events.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())