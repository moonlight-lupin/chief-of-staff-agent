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
        "restore_hint": "Restore via files_untrash / drive_untrash (Google Drive trash; OneDrive Personal Graph or Business SharePoint recycle-bin GUID)",
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
    """Execute an approved soft-delete action.

    Uses mark_executing() BEFORE the provider call to prevent the race where
    a provider action succeeds but the approval has lapsed.
    State machine: approved → executing → executed | failed (back to approved).
    """
    cfg = load_config(args.config)
    if cfg is None:
        return 1

    from pending_actions import get_pending_action, mark_executing, mark_executed, mark_failed
    action = get_pending_action(cfg, args.action_id)
    if not action:
        print(f"Action not found: {args.action_id}", file=sys.stderr)
        return 1
    if action["state"] != "approved":
        print(f"Action {args.action_id} is not approved (state={action['state']}).", file=sys.stderr)
        return 1

    # Pre-execution eligibility check — prevents race with lapsed approval
    executing = mark_executing(cfg, args.action_id)
    if not executing:
        print(f"Action {args.action_id} cannot be executed (approval may have lapsed).", file=sys.stderr)
        return 1

    action_type = action["type"]
    if action_type not in SOFT_DELETE_ACTIONS:
        print(f"Action type {action_type} is not a soft-delete action.", file=sys.stderr)
        return 1

    meta = SOFT_DELETE_ACTIONS[action_type]
    method_name = meta["provider_method"]
    client = get_client(cfg)

    # Capability gate — refuse an approved action the provider cannot support
    # (e.g. m365 calendar.cancel, whose capability is False) BEFORE invoking any
    # provider method. Mirrors webhook_events.cmd_execute exactly: the action is
    # already in 'executing' state (mark_executing above), so mark_failed
    # transitions it back to 'approved' with last_error recorded for retry.
    from workspace_capabilities import require_capability
    unsupported = require_capability(client, action_type, target=action.get("target", ""))
    if unsupported:
        mark_failed(cfg, args.action_id, f"{action_type} not supported by {client.provider_name}")
        print(f"❌ {action_type} not supported by {client.provider_name}", file=sys.stderr)
        return 1

    # Establish the approved-execution context — the explicit approval IS the
    # confirmation. Set AUTO_APPROVE (so reversible SAFE_WRITE actions such as
    # calendar.cancel pass the non-interactive gate) and ALLOW_DESTRUCTIVE, then
    # restore the previous values so we don't leak state into other processes.
    old_auto = os.environ.get("CHIEF_OF_STAFF_AUTO_APPROVE")
    old_destructive = os.environ.get("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE")
    os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
    os.environ["CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"] = "1"

    try:
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
    except Exception as exc:
        mark_failed(cfg, args.action_id, str(exc))
        print(f"Execute failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if old_auto is None:
            os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)
        else:
            os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = old_auto
        if old_destructive is None:
            os.environ.pop("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", None)
        else:
            os.environ["CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"] = old_destructive

    # Check the provider result BEFORE marking executed — a provider failure
    # (success=False) must transition the action to failed (back to 'approved'
    # for retry), NOT be recorded as executed. Mirrors webhook_events.cmd_execute.
    success = result.get("success", False) if isinstance(result, dict) else True
    if not success:
        error = result.get("error", "provider returned failure") if isinstance(result, dict) else "unknown error"
        mark_failed(cfg, args.action_id, error)
        print_result(result, args.summary, f"{meta['label']}: {action['target']}")
        return 1

    mark_executed(cfg, args.action_id, result)
    print_result(result, args.summary, f"{meta['label']}: {action['target']}")
    return 0


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


RESTORE_ACTIONS = {
    "gmail.archive": {"label": "Unarchive Gmail message", "method": "gmail_unarchive"},
    "gmail.trash": {"label": "Restore trashed Gmail message", "method": "gmail_untrash"},
    "calendar.cancel": {"label": "Restore cancelled calendar event", "method": "calendar_uncancel"},
    "drive.trash": {"label": "Restore trashed Drive/OneDrive file", "method": "drive_untrash"},
}


def _restore_target(action: dict) -> str:
    """Resolve the id to restore.

    Some providers (m365/Graph) change an object's id when it moves folders
    (archive/trash). The executed provider result persists the post-move id as
    ``restore_target`` (falling back to ``id``); prefer that over the original
    action ``target`` so restore addresses the object where it actually landed.
    Falls back to the original target when no executed result is recorded (the
    common case for providers whose ids are stable, e.g. Google).
    """
    result = action.get("result")
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, dict):
            restore_id = data.get("restore_target") or data.get("id")
            if restore_id:
                return str(restore_id)
    return action.get("target", "")


def cmd_restore(args: argparse.Namespace) -> int:
    """Restore a previously executed soft-delete action.

    Looks up the executed action, determines the original action type,
    and calls the corresponding restore provider method.
    Does NOT require approval queue — restore is always safe (non-destructive).
    """
    cfg = load_config(args.config)
    if cfg is None:
        return 1

    from pending_actions import get_pending_action
    action = get_pending_action(cfg, args.action_id)
    if not action:
        print(f"Action not found: {args.action_id}", file=sys.stderr)
        return 1
    if action["state"] != "executed":
        print(f"Action {args.action_id} is not executed (state={action['state']}). "
              f"Restore only works on executed actions.", file=sys.stderr)
        return 1

    action_type = action["type"]
    if action_type not in RESTORE_ACTIONS:
        print(f"No restore path for action type: {action_type}", file=sys.stderr)
        return 1

    meta = RESTORE_ACTIONS[action_type]
    client = get_client(cfg)

    # Restore is safe — no approval queue needed
    method = getattr(client, meta["method"], None)
    if method is None:
        print(f"Provider does not implement {meta['method']}", file=sys.stderr)
        return 1

    restore_target = _restore_target(action)
    result = method(restore_target)
    print_result(result, args.summary, f"{meta['label']}: {restore_target}")
    return 0 if result.get("success") else 1


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Clean up old executed/cancelled/expired actions."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from pending_actions import cleanup_old_actions
    removed = cleanup_old_actions(cfg, days=args.days)
    if args.summary:
        print(f"Cleaned up {removed} old action(s) older than {args.days} days")
    else:
        print_json({"removed": removed, "days": args.days})
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

    restore = sub.add_parser("restore", help="Restore a previously executed soft-delete action")
    restore.add_argument("--action-id", required=True)

    cleanup = sub.add_parser("cleanup", help="Remove old executed/cancelled/expired actions")
    cleanup.add_argument("--days", type=int, default=30, help="Remove actions older than N days")

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
        elif args.command == "restore":
            return cmd_restore(args)
        elif args.command == "cleanup":
            return cmd_cleanup(args)
        else:
            parser.error("unknown command")
            return 2
    except Exception as exc:
        print(f"delete_actions.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())