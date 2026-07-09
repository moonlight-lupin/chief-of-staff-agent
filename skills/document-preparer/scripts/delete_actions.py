#!/usr/bin/env python3
"""Gated soft-delete and archive actions — prepare, preview, approve, execute.

All destructive actions go through the pending_actions approval queue.
Only soft-delete paths exist: archive, trash, cancel — never permanent delete.
A reason is required for every delete/archive action.

Commands:
    delete_actions.py prepare --action-type gmail.archive --target <msg_id> --reason "..."
    delete_actions.py prepare --action-type gmail.trash --target <msg_id> --reason "..."
    delete_actions.py prepare --action-type drive.trash --target <file_id> --reason "..."
    delete_actions.py prepare --action-type calendar.cancel --target <event_id> --reason "..."
    delete_actions.py list [--state requested|approved|executed|cancelled|expired]
    delete_actions.py preview --action-id <id>
    delete_actions.py approve --action-id <id> [--approver name] [--reason "..."]
    delete_actions.py cancel --action-id <id> [--reason "..."]
    delete_actions.py execute --action-id <id>
    delete_actions.py summary
    delete_actions.py --dry-run prepare ...
    delete_actions.py --preflight prepare ...

Core rules:
- No permanent delete path exists in this CLI.
- All actions require a reason.
- All actions go through prepare → approve → execute.
- All actions are soft (reversible): archive, trash, cancel.
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

from action_result_cli import print_result, print_json

try:
    from config_loader import load_config  # type: ignore
except Exception as exc:  # pragma: no cover
    print(f"Chief-of-Staff bootstrap incomplete: {exc}", file=sys.stderr)
    raise SystemExit(2)


SOFT_DELETE_ACTIONS = {
    "gmail.archive": {
        "label": "Archive Gmail message",
        "reversible": True,
        "restore_hint": "Remove archive label or add INBOX back via gmail modify",
        "provider_method": "gmail_archive",
    },
    "gmail.trash": {
        "label": "Move Gmail to trash",
        "reversible": True,
        "restore_hint": "Remove TRASH label via gmail modify (30-day auto-delete by Google)",
        "provider_method": "gmail_trash",
    },
    "drive.trash": {
        "label": "Move Drive file to trash",
        "reversible": True,
        "restore_hint": "Restore from trash in Google Drive UI (30-day auto-delete by Google)",
        "provider_method": "drive_trash",
    },
    "calendar.cancel": {
        "label": "Cancel calendar event",
        "reversible": True,
        "restore_hint": "Update event status back to confirmed via calendar update",
        "provider_method": "calendar_cancel",
    },
}


def get_client(config: Any):
    from workspace_client import get_workspace_client
    return get_workspace_client(config)


def cmd_prepare(args: argparse.Namespace) -> int:
    """Prepare a soft-delete action — creates pending action in 'requested' state."""
    cfg = load_config(args.config)
    if cfg is None:
        print("Could not load config", file=sys.stderr)
        return 1

    action_type = args.action_type
    if action_type not in SOFT_DELETE_ACTIONS:
        print(f"Unknown action type: {action_type}. Supported: {', '.join(SOFT_DELETE_ACTIONS.keys())}", file=sys.stderr)
        return 1

    if not args.reason:
        print("A --reason is required for all delete/archive actions.", file=sys.stderr)
        return 1

    meta = SOFT_DELETE_ACTIONS[action_type]
    client = get_client(cfg)

    # Check capability
    from workspace_capabilities import require_capability
    unsupported = require_capability(client, action_type, target=args.target)
    if unsupported:
        print_result(unsupported, args.summary, meta["label"])
        return 1

    # Dry-run: show plan without creating pending action
    if args.dry_run:
        plan = {
            "success": True,
            "action": f"{action_type} (dry-run)",
            "provider": client.provider_name,
            "target": args.target,
            "data": {
                "reason": args.reason,
                "reversible": meta["reversible"],
                "restore_hint": meta["restore_hint"],
            },
            "error": None,
            "audited": False,
        }
        print_result(plan, args.summary, f"{meta['label']} would be prepared")
        return 0

    # Preflight: show execution plan without creating pending action
    if args.preflight:
        plan = {
            "success": True,
            "action": f"{action_type} (preflight)",
            "provider": client.provider_name,
            "target": args.target,
            "data": {
                "reason": args.reason,
                "reversible": meta["reversible"],
                "restore_hint": meta["restore_hint"],
                "capability_ok": True,
            },
            "error": None,
            "audited": False,
        }
        print_result(plan, args.summary, f"{meta['label']} preflight")
        return 0

    from pending_actions import create_pending_action
    action = create_pending_action(
        config=cfg,
        action_type=action_type,
        provider=client.provider_name,
        target=args.target,
        payload={
            "reason": args.reason,
            "reversible": meta["reversible"],
            "restore_hint": meta["restore_hint"],
            "provider_method": meta["provider_method"],
        },
        summary=f"{meta['label']}: {args.target} — {args.reason}",
    )
    print_result(action, args.summary, f"{meta['label']} prepared")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """List pending delete/archive actions."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from pending_actions import list_pending_actions
    actions = list_pending_actions(cfg, state=args.state)
    # Filter to only soft-delete actions
    actions = [a for a in actions if a.get("type") in SOFT_DELETE_ACTIONS]
    if args.summary:
        if not actions:
            print("No pending delete/archive actions" + (f" with state={args.state}" if args.state else ""))
        else:
            for a in actions:
                icon = {
                    "requested": "📨", "approved": "✅", "executed": "🗑️",
                    "cancelled": "❌", "expired": "⏰",
                }.get(a["state"], "?")
                print(f"{icon} {a['id']}  {a['type']}  → {a['target']}  [{a['state']}]")
                print(f"   {a.get('summary', '')}")
    else:
        print_json(actions)
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    """Preview a pending delete/archive action."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from pending_actions import preview_pending_action
    preview = preview_pending_action(cfg, args.action_id)
    if not preview:
        print(f"Action not found: {args.action_id}", file=sys.stderr)
        return 1
    if args.summary:
        state = preview.get("state", "?")
        icon = {"requested": "📨", "approved": "✅", "executed": "🗑️",
                "cancelled": "❌", "expired": "⏰"}.get(state, "?")
        print(f"{icon} Pending action: {preview['id']}")
        print(f"State: {state}")
        print(f"Type: {preview['type']}")
        print(f"Target: {preview['target']}")
        print(f"Provider: {preview['provider']}")
        # Show restore info from the action payload
        from pending_actions import get_pending_action
        action = get_pending_action(cfg, args.action_id)
        if action and action.get("payload"):
            p = action["payload"]
            print(f"Reversible: {p.get('reversible', '?')}")
            print(f"Restore: {p.get('restore_hint', 'N/A')}")
            print(f"Reason: {p.get('reason', 'N/A')}")
        if preview.get("approver"):
            print(f"Approved by: {preview['approver']}")
    else:
        print_json(preview)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """Approve a pending delete/archive action with metadata."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from pending_actions import approve_pending_action, check_expired
    if check_expired(cfg, args.action_id):
        print(f"Action {args.action_id} has expired.", file=sys.stderr)
        return 1
    action = approve_pending_action(cfg, args.action_id,
                                    approver=args.approver, reason=args.reason)
    if not action:
        print(f"Action not found, not in 'requested' state, or expired: {args.action_id}", file=sys.stderr)
        return 1
    print_result(action, args.summary, "Delete/archive approved")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    """Cancel a pending delete/archive action."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from pending_actions import cancel_pending_action
    action = cancel_pending_action(cfg, args.action_id, reason=args.reason)
    if not action:
        print(f"Action not found or already terminal: {args.action_id}", file=sys.stderr)
        return 1
    print_result(action, args.summary, "Delete/archive cancelled")
    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    """Execute an approved soft-delete action."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1

    from pending_actions import get_pending_action, mark_executed
    action = get_pending_action(cfg, args.action_id)
    if not action:
        print(f"Action not found: {args.action_id}", file=sys.stderr)
        return 1
    if action["state"] != "approved":
        print(f"Action {args.action_id} is not approved (state={action['state']}).", file=sys.stderr)
        return 1

    action_type = action["type"]
    if action_type not in SOFT_DELETE_ACTIONS:
        print(f"Action type {action_type} is not a soft-delete action.", file=sys.stderr)
        return 1

    meta = SOFT_DELETE_ACTIONS[action_type]
    method_name = meta["provider_method"]
    client = get_client(cfg)

    # Soft-delete actions are destructive — set the flag
    os.environ["CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"] = "1"

    # Call the provider method
    method = getattr(client, method_name, None)
    if method is None:
        result = {
            "success": False,
            "action": action_type,
            "provider": client.provider_name,
            "target": action["target"],
            "error": f"Provider {client.provider_name} does not implement {method_name}",
            "audited": False,
        }
    else:
        result = method(action["target"])

    mark_executed(cfg, args.action_id, result)
    print_result(result, args.summary, f"{meta['label']}: {action['target']}")
    return 0 if result.get("success") else 1


def cmd_summary(args: argparse.Namespace) -> int:
    """Print summary of pending delete/archive actions."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from pending_actions import list_pending_actions
    actions = list_pending_actions(cfg)
    actions = [a for a in actions if a.get("type") in SOFT_DELETE_ACTIONS]
    counts: dict[str, int] = {}
    for a in actions:
        state = a.get("state", "unknown")
        counts[state] = counts.get(state, 0) + 1
    if args.summary:
        print(f"Pending delete/archive actions: {len(actions)} total")
        for state, count in sorted(counts.items()):
            print(f"  {state}: {count}")
    else:
        print_json({"total": len(actions), "by_state": counts})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Gated soft-delete and archive actions — prepare, preview, approve, execute"
    )
    parser.add_argument("--config", help="Path to company.yaml")
    parser.add_argument("--summary", action="store_true", help="Print human-readable summary")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="Prepare a soft-delete action")
    prepare.add_argument("--action-type", required=True,
                         choices=list(SOFT_DELETE_ACTIONS.keys()),
                         help="Type of soft-delete action")
    prepare.add_argument("--target", required=True, help="Target ID (message/file/event ID)")
    prepare.add_argument("--reason", required=True, help="Reason for this action (required)")
    prepare.add_argument("--dry-run", action="store_true", help="Show plan without creating action")
    prepare.add_argument("--preflight", action="store_true", help="Show execution plan, then exit")

    list_cmd = sub.add_parser("list", help="List pending delete/archive actions")
    list_cmd.add_argument("--state", choices=["requested", "approved", "executed", "cancelled", "expired"])

    preview = sub.add_parser("preview", help="Preview a pending action")
    preview.add_argument("--action-id", required=True)

    approve = sub.add_parser("approve", help="Approve a pending action")
    approve.add_argument("--action-id", required=True)
    approve.add_argument("--approver", help="Name of the person approving")
    approve.add_argument("--reason", help="Approval reason")

    cancel = sub.add_parser("cancel", help="Cancel a pending action")
    cancel.add_argument("--action-id", required=True)
    cancel.add_argument("--reason", help="Cancel reason")

    execute = sub.add_parser("execute", help="Execute an approved soft-delete action")
    execute.add_argument("--action-id", required=True)

    summary_cmd = sub.add_parser("summary", help="Print summary by state")

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
        print(f"delete_actions.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())