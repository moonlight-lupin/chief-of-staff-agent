#!/usr/bin/env python3
"""Gated mail send — prepare, preview, approve, execute.

No user-facing workflow should call mail_send() directly.
Everything goes through: prepare → preview → pending action → explicit confirm → send.

Commands:
    send_email.py prepare --to client@x.com --subject "NDA" --body "Please sign..."
    send_email.py list [--state requested|approved|executed|cancelled|expired]
    send_email.py preview --action-id <id>
    send_email.py approve --action-id <id> [--approver name] [--reason "..."]
    send_email.py cancel --action-id <id> [--reason "..."]
    send_email.py execute --action-id <id>
    send_email.py summary

Core rule: execute requires an approved action ID. No direct send.
Works with any provider that supports ``mail.send`` (google_api, m365,
composio_microsoft). The approve step is the clear user approval gate;
execute then sets the destructive guardrail envs for that call only.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

from action_result_cli import print_result, print_json

try:
    from config_loader import load_config  # type: ignore
except Exception as exc:  # pragma: no cover
    print(f"Chief-of-Staff bootstrap incomplete: {exc}", file=sys.stderr)
    raise SystemExit(2)


def get_client(config: Any):
    from workspace_client import get_workspace_client
    return get_workspace_client(config)


def cmd_prepare(args: argparse.Namespace) -> int:
    """Prepare a Gmail send action — creates pending action in 'requested' state."""
    cfg = load_config(args.config)
    if cfg is None:
        print("Could not load config", file=sys.stderr)
        return 1
    client = get_client(cfg)

    # Neutral mail.send (gmail.send is a legacy alias in the capability matrix).
    from workspace_capabilities import require_capability
    unsupported = require_capability(client, "mail.send", target=args.to)
    if unsupported:
        print_result(unsupported, args.summary, "Mail send")
        return 1

    from state_db import create_pending_action
    action = create_pending_action(
        config=cfg,
        action_type="mail.send",
        provider=client.provider_name,
        target=args.to,
        payload={
            "to": args.to,
            "subject": args.subject,
            "body": args.body,
            "cc": args.cc or "",
        },
        summary=f"Send email to {args.to}: {args.subject}",
    )
    print_result(action, args.summary, "Mail send prepared")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """List pending actions, optionally filtered by state."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from state_db import list_pending_actions
    actions = list_pending_actions(cfg, state=args.state)
    if args.summary:
        if not actions:
            print("No pending actions" + (f" with state={args.state}" if args.state else ""))
        else:
            for a in actions:
                icon = {
                    "requested": "📨", "approved": "✅", "executed": "📤",
                    "cancelled": "❌", "expired": "⏰",
                }.get(a["state"], "?")
                risk_tag = ""
                risk = a.get("risk")
                if risk and risk.get("level") == "external":
                    risk_tag = " ⚠️external"
                print(f"{icon} {a['id']}  {a['type']}  → {a['target']}  [{a['state']}]{risk_tag}")
                print(f"   {a.get('summary', '')}")
                if a.get("approver"):
                    print(f"   Approved by: {a['approver']}")
    else:
        print_json(actions)
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    """Preview a pending action without executing."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from state_db import preview_pending_action
    preview = preview_pending_action(cfg, args.action_id)
    if not preview:
        print(f"Action not found: {args.action_id}", file=sys.stderr)
        return 1
    if args.summary:
        state = preview.get("state", "?")
        icon = {"requested": "📨", "approved": "✅", "executed": "📤",
                "cancelled": "❌", "expired": "⏰"}.get(state, "?")
        print(f"{icon} Pending action: {preview['id']}")
        print(f"State: {state}")
        print(f"Type: {preview['type']}")
        print(f"Target: {preview['target']}")
        print(f"Provider: {preview['provider']}")
        risk = preview.get("risk")
        if risk:
            print(f"Risk: {risk['level']} — {risk['reason']}")
        p = preview.get("preview", {})
        print(f"To: {p.get('to', '?')}")
        print(f"Subject: {p.get('subject', '?')}")
        print(f"Body preview: {p.get('body_preview', '')[:100]}")
        if preview.get("approver"):
            print(f"Approved by: {preview['approver']}")
            if preview.get("approval_reason"):
                print(f"Approval reason: {preview['approval_reason']}")
    else:
        print_json(preview)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """Approve a pending action (requested → approved) with metadata."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from state_db import approve_pending_action, check_expired
    # Check expiry first
    if check_expired(cfg, args.action_id):
        print(f"Action {args.action_id} has expired. Re-prepare with 'send_email.py prepare'.", file=sys.stderr)
        return 1
    action = approve_pending_action(cfg, args.action_id,
                                    approver=args.approver, reason=args.reason)
    if not action:
        print(f"Action not found, not in 'requested' state, or expired: {args.action_id}", file=sys.stderr)
        return 1
    print_result(action, args.summary, "Mail send approved")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    """Cancel a pending action with optional reason."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from state_db import cancel_pending_action
    action = cancel_pending_action(cfg, args.action_id, reason=args.reason)
    if not action:
        print(f"Action not found or already terminal: {args.action_id}", file=sys.stderr)
        return 1
    print_result(action, args.summary, "Mail send cancelled")
    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    """Execute an approved mail send — requires approved action ID.

    Uses mark_executing() BEFORE the provider call to prevent the race where
    a provider action succeeds but the approval has lapsed. The state machine
    is: approved → executing → executed | failed (back to approved for retry).

    The explicit approve step IS the user confirmation. For the provider
    guardrail we set both CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1 and
    CHIEF_OF_STAFF_AUTO_APPROVE=1 for this call only (same dual-gate as
    webhook_events execute).
    """
    cfg = load_config(args.config)
    if cfg is None:
        return 1

    from state_db import get_pending_action, mark_executing, mark_executed, mark_failed
    action = get_pending_action(cfg, args.action_id)
    if not action:
        print(f"Action not found: {args.action_id}", file=sys.stderr)
        return 1
    if action["state"] != "approved":
        print(f"Action {args.action_id} is not approved (state={action['state']}). "
              f"Run: send_email.py approve --action-id {args.action_id}", file=sys.stderr)
        return 1

    # Pre-execution eligibility check — prevents race with lapsed approval
    executing = mark_executing(cfg, args.action_id)
    if not executing:
        print(f"Action {args.action_id} cannot be executed (approval may have lapsed). "
              f"Re-approve with: send_email.py approve --action-id {args.action_id}", file=sys.stderr)
        return 1

    client = get_client(cfg)
    payload = action["payload"]

    # Approval queue already recorded human consent — unlock the destructive gate
    # for this call only, then restore prior env.
    prev_destr = os.environ.get("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE")
    prev_auto = os.environ.get("CHIEF_OF_STAFF_AUTO_APPROVE")
    os.environ["CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"] = "1"
    os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"

    # Execute the send
    try:
        result = client.mail_send(
            to=payload["to"],
            subject=payload["subject"],
            body=payload["body"],
            cc=payload.get("cc") or None,
        )
    except Exception as exc:
        mark_failed(cfg, args.action_id, str(exc))
        print(f"Send failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if prev_destr is None:
            os.environ.pop("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", None)
        else:
            os.environ["CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"] = prev_destr
        if prev_auto is None:
            os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)
        else:
            os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = prev_auto

    # Mark as executed with result
    mark_executed(cfg, args.action_id, result)
    print_result(result, args.summary, f"Mail sent to {payload['to']}")
    return 0 if result.get("success") else 1


def cmd_summary(args: argparse.Namespace) -> int:
    """Print a summary of pending actions by state."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from state_db import get_pending_summary
    summary = get_pending_summary(cfg)
    if args.summary:
        print(f"Pending actions: {summary['total']} total")
        for state, count in sorted(summary["by_state"].items()):
            icon = {"requested": "📨", "approved": "✅", "executed": "📤",
                    "cancelled": "❌", "expired": "⏰"}.get(state, "?")
            print(f"  {icon} {state}: {count}")
        if summary["expired_unmarked"]:
            print(f"  ⚠️ {summary['expired_unmarked']} expired but not yet marked")
        if summary["high_risk_pending"]:
            print(f"\n⚠️ High-risk pending ({len(summary['high_risk_pending'])}):")
            for item in summary["high_risk_pending"]:
                print(f"  📨 {item['id']} → {item['target']}")
                print(f"     {item['risk_reason']}")
    else:
        print_json(summary)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gated Gmail send — prepare, preview, approve, execute")
    parser.add_argument("--config", help="Path to company.yaml")
    parser.add_argument("--summary", action="store_true", help="Print human-readable summary")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="Prepare a Gmail send (creates pending action)")
    prepare.add_argument("--to", required=True, help="Recipient email")
    prepare.add_argument("--subject", required=True)
    prepare.add_argument("--body", required=True)
    prepare.add_argument("--cc")

    list_cmd = sub.add_parser("list", help="List pending actions")
    list_cmd.add_argument("--state", choices=["requested", "approved", "executed", "cancelled", "expired"])

    preview = sub.add_parser("preview", help="Preview a pending action")
    preview.add_argument("--action-id", required=True)

    approve = sub.add_parser("approve", help="Approve a pending action")
    approve.add_argument("--action-id", required=True)
    approve.add_argument("--approver", help="Name of the person approving")
    approve.add_argument("--reason", help="Approval reason (for audit trail)")

    cancel = sub.add_parser("cancel", help="Cancel a pending action")
    cancel.add_argument("--action-id", required=True)
    cancel.add_argument("--reason", help="Cancel reason (for audit trail)")

    execute = sub.add_parser("execute", help="Execute an approved Gmail send")
    execute.add_argument("--action-id", required=True)

    sub.add_parser("summary", help="Print pending action summary by state")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            return cmd_prepare(args)
        elif args.command == "list":
            return cmd_list(args)
        elif args.command == "preview":
            return cmd_preview(args)
        elif args.command == "approve":
            return cmd_approve(args)
        elif args.command == "cancel":
            return cmd_cancel(args)
        elif args.command == "execute":
            return cmd_execute(args)
        elif args.command == "summary":
            return cmd_summary(args)
        else:
            parser.error("unknown command")
            return 2
    except Exception as exc:
        print(f"send_email.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())