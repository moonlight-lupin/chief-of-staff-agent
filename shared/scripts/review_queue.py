#!/usr/bin/env python3
"""Unified operator review queue for pending actions and suggestions.

Commands:
    python shared/scripts/review_queue.py list [--state requested] [--risk high] [--type gmail.send]
    python shared/scripts/review_queue.py preview --action-id <id>
    python shared/scripts/review_queue.py approve --action-id <id> --approver "MH" --reason "Reviewed"
    python shared/scripts/review_queue.py approve --all --risk low --type gmail.label --reason "..." --confirm-low-risk-bulk
    python shared/scripts/review_queue.py dismiss --action-id <id> --reason "Not needed"
    python shared/scripts/review_queue.py execute --action-id <id>
    python shared/scripts/review_queue.py summary
    python shared/scripts/review_queue.py audit --limit 20

This CLI does not bypass provider guardrails. Execution is delegated to the
same approved-action execution path used by webhook_events.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

_SCRIPT_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _SCRIPT_DIR.parents[1]
_DOC_PREPARER_SCRIPTS = _PLUGIN_ROOT / "skills" / "document-preparer" / "scripts"

for _path in (_SCRIPT_DIR, _DOC_PREPARER_SCRIPTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from action_risk import ACTION_RISK_MAP, get_risk_explanation, get_risk_icon  # noqa: E402
from config_loader import get_project_root, load_config  # noqa: E402
from pending_actions import (  # noqa: E402
    approve_pending_action,
    dismiss_pending_action,
    get_pending_action,
    list_pending_actions,
    preview_pending_action,
)
from suggested_actions import dismiss_suggestion, get_suggestion, list_suggestions  # noqa: E402

Risk = str

_UNKNOWN_WRITE_TOKENS = (
    "send",
    "trash",
    "cancel",
    "delete",
    "upload",
    "create",
    "update",
    "archive",
    "label",
)
_UNKNOWN_READ_TOKENS = ("search", "download", "get", "list", "read")
_RISK_ORDER = {"high": 0, "medium": 1, "low": 2, "unknown": 3}
_STATE_ORDER = {
    "requested": 0,
    "approved": 1,
    "executing": 2,
    "suggested": 3,
    "failed": 4,
    "dismissed": 5,
    "executed": 6,
    "cancelled": 7,
    "expired": 8,
    "acted_on": 9,
}


def _safe_action_risk(action_type: str | None) -> Risk:
    """Classify action risk without silently defaulting unknown writes to low."""
    action_type = str(action_type or "").strip()
    if action_type in ACTION_RISK_MAP:
        return ACTION_RISK_MAP[action_type]

    lowered = action_type.lower()
    if any(token in lowered for token in _UNKNOWN_WRITE_TOKENS):
        return "high"
    if any(token in lowered for token in _UNKNOWN_READ_TOKENS):
        return "low"
    return "medium"


def _json_dump(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _truncate(value: Any, width: int) -> str:
    text = "" if value is None else str(value).replace("\n", " ")
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def _command(command: str, action_id: str | None = None) -> str:
    base = f"python shared/scripts/review_queue.py {command}"
    if action_id:
        base += f" --action-id {action_id}"
    return base


def _approval_command(action_id: str, kind: str, state: str) -> str:
    if kind != "pending_action":
        return "N/A — suggestion only; create a pending action through the appropriate workflow"
    if state == "requested":
        return f"{_command('approve', action_id)} --approver 'MH' --reason 'Reviewed'"
    return "N/A"


def _execute_command(action_id: str, kind: str, state: str) -> str:
    if kind != "pending_action":
        return "N/A — suggestion only"
    if state == "approved":
        return _command("execute", action_id)
    return "N/A until approved"


def _pending_action_to_item(action: Mapping[str, Any]) -> dict[str, Any]:
    action_id = str(action.get("id", ""))
    action_type = str(action.get("type") or action.get("action_type") or "")
    state = str(action.get("state", "unknown"))
    risk = _safe_action_risk(action_type)
    summary = str(action.get("summary") or f"{action_type} to {action.get('target', '')}".strip())
    return {
        "id": action_id,
        "kind": "pending_action",
        "action_type": action_type,
        "state": state,
        "risk": risk,
        "title": summary,
        "summary": summary,
        "why": action.get("reason") or action.get("approval_reason") or get_risk_explanation(action_type, risk),
        "source": action.get("provider") or "pending_actions",
        "created_at": action.get("created_at") or "",
        "approval_command": _approval_command(action_id, "pending_action", state),
        "execute_command": _execute_command(action_id, "pending_action", state),
    }


def _suggestion_to_item(suggestion: Mapping[str, Any]) -> dict[str, Any]:
    suggestion_id = str(suggestion.get("id", ""))
    action_type = str(suggestion.get("action_type") or "")
    state = str(suggestion.get("state", "unknown"))
    risk = _safe_action_risk(action_type)
    title = str(suggestion.get("title") or f"Suggested {action_type}")
    summary = str(suggestion.get("event_summary") or suggestion.get("summary") or title)
    return {
        "id": suggestion_id,
        "kind": "suggestion",
        "action_type": action_type,
        "state": state,
        "risk": risk,
        "title": title,
        "summary": summary,
        "why": suggestion.get("reason") or get_risk_explanation(action_type, risk),
        "source": suggestion.get("event_source") or suggestion.get("provider") or "suggested_actions",
        "created_at": suggestion.get("created_at") or "",
        "approval_command": _approval_command(suggestion_id, "suggestion", state),
        "execute_command": _execute_command(suggestion_id, "suggestion", state),
    }


def _load_review_items(config: Any) -> list[dict[str, Any]]:
    actions = list_pending_actions(config, include_expired=True)
    suggestions = list_suggestions(config, limit=1000)
    items = [_pending_action_to_item(action) for action in actions]
    items.extend(_suggestion_to_item(suggestion) for suggestion in suggestions)
    return sorted(
        items,
        key=lambda item: (
            _STATE_ORDER.get(str(item.get("state")), 99),
            _RISK_ORDER.get(str(item.get("risk")), 99),
            str(item.get("created_at") or ""),
            str(item.get("id") or ""),
        ),
    )


def _filter_items(
    items: Sequence[dict[str, Any]],
    *,
    state: str | None = None,
    risk: str | None = None,
    action_type: str | None = None,
) -> list[dict[str, Any]]:
    filtered = list(items)
    if state:
        filtered = [item for item in filtered if item.get("state") == state]
    if risk:
        filtered = [item for item in filtered if item.get("risk") == risk]
    if action_type:
        filtered = [item for item in filtered if item.get("action_type") == action_type]
    return filtered


def _print_table(items: Sequence[Mapping[str, Any]]) -> None:
    columns: list[tuple[str, str, int]] = [
        ("id", "ID", 14),
        ("kind", "KIND", 15),
        ("action_type", "TYPE", 20),
        ("state", "STATE", 12),
        ("risk", "RISK", 9),
        ("title", "TITLE", 56),
        ("created_at", "CREATED", 20),
    ]
    if not items:
        print("No review items found")
        return

    header = "  ".join(label.ljust(width) for _, label, width in columns)
    print(header)
    print("  ".join("-" * width for _, _, width in columns))
    for item in items:
        cells: list[str] = []
        for key, _, width in columns:
            value = item.get(key, "")
            if key == "risk":
                risk = str(value or "unknown")
                value = f"{get_risk_icon(risk)} {risk}"
            elif key == "created_at":
                value = str(value or "")[:19]
            cells.append(_truncate(value, width).ljust(width))
        print("  ".join(cells))


def _expected_effect(action_type: str, target: str, payload: Mapping[str, Any]) -> str:
    if action_type == "gmail.send":
        return f"Send an email to {payload.get('to') or target} with subject {payload.get('subject', '')!r}."
    if action_type == "gmail.draft":
        return f"Create a Gmail draft for {payload.get('to') or target}."
    if action_type == "gmail.label":
        return f"Apply label {payload.get('label_id') or payload.get('label') or target!r} to message {payload.get('message_id', '')!r}."
    if action_type == "gmail.create_label":
        return f"Create Gmail label {payload.get('label') or payload.get('label_name') or target!r}."
    if action_type == "gmail.archive":
        return f"Archive Gmail message {payload.get('message_id') or target}."
    if action_type == "gmail.trash":
        return f"Move Gmail message {payload.get('message_id') or target} to trash."
    if action_type == "calendar.create":
        return f"Create calendar event {payload.get('summary') or payload.get('title') or target!r}."
    if action_type == "calendar.update":
        return f"Update calendar event {payload.get('event_id') or target}."
    if action_type == "calendar.cancel":
        return f"Cancel calendar event {payload.get('event_id') or target}."
    if action_type == "drive.upload":
        return f"Upload file {payload.get('file_path') or payload.get('path') or target}."
    if action_type == "drive.download":
        return f"Download Drive file {payload.get('file_id') or target} to {payload.get('output_path') or payload.get('path') or 'configured output path'}."
    if action_type == "drive.trash":
        return f"Move Drive file {payload.get('file_id') or target} to trash."
    return f"Run {action_type or 'unknown action'} against target {target or '(none)'} with the stored payload."


def _reversal_hint(action_type: str) -> str:
    hints = {
        "gmail.send": "No full reversal; send a correction/follow-up if needed.",
        "gmail.draft": "Delete the created draft if it is not needed.",
        "gmail.label": "Remove the label from the message.",
        "gmail.create_label": "Delete the label if created in error.",
        "gmail.archive": "Move the message back to the inbox.",
        "gmail.trash": "Restore the message from trash while retention allows.",
        "calendar.create": "Delete/cancel the created event.",
        "calendar.update": "Apply another update restoring prior event fields.",
        "calendar.cancel": "Recreate the event and notify attendees if needed.",
        "drive.upload": "Trash/delete the uploaded file if inappropriate.",
        "drive.download": "Delete the downloaded local copy if inappropriate.",
        "drive.trash": "Restore the file from Drive trash while retention allows.",
    }
    return hints.get(action_type, "Review provider audit logs and undo manually if supported.")


def _load_config_or_exit(config_path: str | None) -> Any | None:
    config = load_config(config_path)
    if config is None:
        return None
    return config


def cmd_list(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args.config)
    if config is None:
        return 1
    items = _filter_items(
        _load_review_items(config),
        state=args.state,
        risk=args.risk,
        action_type=args.action_type,
    )
    _print_table(items)
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args.config)
    if config is None:
        return 1

    action = get_pending_action(config, args.action_id)
    if action:
        preview = preview_pending_action(config, args.action_id) or {}
        item = _pending_action_to_item(action)
        action_type = item["action_type"]
        raw_payload = action.get("payload")
        payload: Mapping[str, Any] = raw_payload if isinstance(raw_payload, Mapping) else {}
        target = str(action.get("target") or "")
        print(f"Pending action: {args.action_id}")
        print(f"Type: {action_type}")
        print(f"State: {item['state']}")
        print(f"Risk: {get_risk_icon(item['risk'])} {item['risk']} — {get_risk_explanation(action_type, item['risk'])}")
        print(f"Source: {item['source']}")
        print(f"Created: {item['created_at']}")
        print(f"Summary: {item['summary']}")
        print(f"Target: {target}")
        print(f"Expected effect: {_expected_effect(action_type, target, payload)}")
        print(f"Reversal hint: {_reversal_hint(action_type)}")
        print("Payload:")
        print(_json_dump(action.get("payload", {})))
        if preview:
            print("Preview:")
            print(_json_dump(preview))
        print(f"Approval command: {item['approval_command']}")
        print(f"Execute command: {item['execute_command']}")
        return 0

    suggestion = get_suggestion(config, args.action_id)
    if suggestion:
        item = _suggestion_to_item(suggestion)
        action_type = item["action_type"]
        print(f"Suggestion: {args.action_id}")
        print(f"Type: {action_type}")
        print(f"State: {item['state']}")
        print(f"Risk: {get_risk_icon(item['risk'])} {item['risk']} — {get_risk_explanation(action_type, item['risk'])}")
        print(f"Source: {item['source']}")
        print(f"Created: {item['created_at']}")
        print(f"Title: {item['title']}")
        print(f"Summary: {item['summary']}")
        print(f"Why: {item['why']}")
        print(f"Suggested action: {action_type}")
        print(f"What it would do: {_expected_effect(action_type, str(suggestion.get('event_id') or ''), {})}")
        print(f"Reversal hint: {_reversal_hint(action_type)}")
        print(f"Approval command: {item['approval_command']}")
        print(f"Execute command: {item['execute_command']}")
        print("Suggestion record:")
        print(_json_dump(suggestion))
        return 0

    print(f"Review item not found: {args.action_id}", file=sys.stderr)
    return 1


def _approve_one(config: Any, action_id: str, approver: str | None, reason: str | None) -> dict[str, Any] | None:
    action = get_pending_action(config, action_id)
    if not action:
        return None
    return approve_pending_action(config, action_id, approver=approver, reason=reason)


def cmd_approve(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args.config)
    if config is None:
        return 1

    if args.approve_all:
        if args.risk != "low":
            print("Bulk approve is only allowed with --risk low", file=sys.stderr)
            return 2
        if not args.confirm_low_risk_bulk:
            print("Bulk approve requires --confirm-low-risk-bulk", file=sys.stderr)
            return 2
        if not args.action_type:
            print("Bulk approve requires --type <action_type>", file=sys.stderr)
            return 2
        if not args.reason:
            print("Bulk approve requires --reason", file=sys.stderr)
            return 2

        candidates = list_pending_actions(config, state="requested", include_expired=False)
        candidates = [
            action
            for action in candidates
            if str(action.get("type") or action.get("action_type") or "") == args.action_type
            and _safe_action_risk(str(action.get("type") or action.get("action_type") or "")) == "low"
        ]
        if not candidates:
            print("No low-risk requested pending actions matched the bulk approval filters")
            return 0

        approved: list[str] = []
        failed: list[str] = []
        for action in candidates:
            action_id = str(action.get("id") or "")
            result = _approve_one(config, action_id, args.approver, args.reason)
            if result:
                approved.append(action_id)
            else:
                failed.append(action_id)
        print(f"✅ Bulk approved {len(approved)} low-risk {args.action_type} action(s)")
        for action_id in approved:
            print(f"   {action_id} — execute with: {_command('execute', action_id)}")
        if failed:
            print(f"⚠️  Failed to approve {len(failed)} action(s): {', '.join(failed)}", file=sys.stderr)
            return 1
        return 0

    if not args.action_id:
        print("approve requires --action-id or --all", file=sys.stderr)
        return 2

    if get_suggestion(config, args.action_id):
        print("Suggestions cannot be approved directly; approve a pending action instead.", file=sys.stderr)
        return 1

    before = get_pending_action(config, args.action_id)
    if not before:
        print(f"Action not found: {args.action_id}", file=sys.stderr)
        return 1
    result = approve_pending_action(config, args.action_id, approver=args.approver, reason=args.reason)
    if not result:
        print(f"Approval failed for {args.action_id} (state={before.get('state')})", file=sys.stderr)
        return 1
    action_type = str(result.get("type") or result.get("action_type") or "")
    print(f"✅ Approved: {action_type} ({args.action_id})")
    if args.approver:
        print(f"   Approver: {args.approver}")
    if args.reason:
        print(f"   Reason: {args.reason}")
    print(f"   Execute with: {_command('execute', args.action_id)}")
    return 0


def cmd_dismiss(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args.config)
    if config is None:
        return 1
    if not args.reason:
        print("dismiss requires --reason", file=sys.stderr)
        return 2

    action = get_pending_action(config, args.action_id)
    if action:
        result = dismiss_pending_action(config, args.action_id, reason=args.reason)
        if not result:
            print(f"Dismiss failed for {args.action_id} (state={action.get('state')})", file=sys.stderr)
            return 1
        print(f"✅ Dismissed pending action: {args.action_id}")
        print(f"   Reason: {args.reason}")
        return 0

    suggestion = get_suggestion(config, args.action_id)
    if suggestion:
        result = dismiss_suggestion(config, args.action_id, reason=args.reason)
        if not result:
            print(f"Dismiss failed for suggestion {args.action_id} (state={suggestion.get('state')})", file=sys.stderr)
            return 1
        print(f"✅ Dismissed suggestion: {args.action_id}")
        print(f"   Reason: {args.reason}")
        return 0

    print(f"Review item not found: {args.action_id}", file=sys.stderr)
    return 1


def cmd_execute(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args.config)
    if config is None:
        return 1
    action = get_pending_action(config, args.action_id)
    if not action:
        if get_suggestion(config, args.action_id):
            print("Suggestions cannot be executed directly; execute an approved pending action instead.", file=sys.stderr)
        else:
            print(f"Action not found: {args.action_id}", file=sys.stderr)
        return 1
    state = str(action.get("state") or "")
    if state != "approved":
        print(f"Refusing to execute {args.action_id}: action is not approved (state={state})", file=sys.stderr)
        return 1

    # Reuse the existing execution router. It performs mark_executing, provider
    # capability checks, workspace_client dispatch, and mark_executed/mark_failed.
    from webhook_events import cmd_execute as webhook_cmd_execute  # type: ignore  # noqa: E402

    delegated_args = argparse.Namespace(config=args.config, action_id=args.action_id, summary=True)
    return int(webhook_cmd_execute(delegated_args))


def cmd_summary(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args.config)
    if config is None:
        return 1
    items = _load_review_items(config)
    by_state: dict[str, int] = {}
    by_risk: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
    for item in items:
        state = str(item.get("state") or "unknown")
        risk = str(item.get("risk") or "unknown")
        by_state[state] = by_state.get(state, 0) + 1
        by_risk[risk] = by_risk.get(risk, 0) + 1

    print("Review queue summary")
    print(f"Total items: {len(items)}")
    print("By state:")
    for state, count in sorted(by_state.items(), key=lambda pair: (_STATE_ORDER.get(pair[0], 99), pair[0])):
        print(f"  {state}: {count}")
    print("By risk:")
    for risk, count in sorted(by_risk.items(), key=lambda pair: (_RISK_ORDER.get(pair[0], 99), pair[0])):
        print(f"  {get_risk_icon(risk)} {risk}: {count}")

    actionable = [item for item in items if item.get("state") in {"requested", "approved", "suggested"}]
    if actionable:
        actionable.sort(key=lambda item: (_RISK_ORDER.get(str(item.get("risk")), 99), _STATE_ORDER.get(str(item.get("state")), 99), str(item.get("created_at") or "")))
        next_item = actionable[0]
        if next_item.get("state") == "approved":
            verb = "Execute approved item"
            command = next_item.get("execute_command")
        else:
            verb = "Review"
            command = _command("preview", str(next_item.get("id")))
        print(f"Next step: {verb} {next_item.get('risk')} risk item {next_item.get('id')} ({next_item.get('action_type')})")
        print(f"Command: {command}")
    else:
        print("Next step: No active review items")
    return 0


def _audit_paths(config: Any) -> list[Path]:
    root = get_project_root(config)
    if root is None:
        return []
    audit_dir = root / ".audit"
    if not audit_dir.exists():
        return []
    paths: list[Path] = []
    for pattern in ("*.log", "*.jsonl"):
        paths.extend(sorted(audit_dir.glob(pattern)))
    return sorted(set(paths))


def _load_audit_records(config: Any, limit: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in _audit_paths(config):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                record = {"timestamp": "", "status": "raw", "message": line}
            record["_file"] = str(path)
            records.append(record)
    records.sort(key=lambda record: str(record.get("timestamp") or ""), reverse=True)
    return records[:limit]


def cmd_audit(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args.config)
    if config is None:
        return 1
    records = _load_audit_records(config, args.limit)
    if not records:
        print("No audit records found under project .audit/")
        return 0
    print(f"Recent audit records (limit {args.limit})")
    for record in records:
        timestamp = str(record.get("timestamp") or "")[:19]
        status = record.get("status", "?")
        provider = record.get("provider", "?")
        operation = record.get("operation", record.get("message", "?"))
        tool = record.get("tool", "?")
        target = record.get("target", "")
        extra = record.get("extra") if isinstance(record.get("extra"), Mapping) else {}
        action_id = extra.get("action_id", "") if isinstance(extra, Mapping) else ""
        suffix = f" action={action_id}" if action_id else ""
        print(f"{timestamp}  {status:<10}  {provider:<12}  {operation:<24}  {tool:<16}  {target}{suffix}")
    return 0


def _add_common_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        dest="sub_config",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified Chief-of-Staff operator review queue")
    parser.add_argument("--config", help="Path to company.yaml (default: CHIEF_OF_STAFF_CONFIG or shared/config/company.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="List pending actions and suggestions")
    _add_common_config_arg(list_parser)
    list_parser.add_argument("--state", help="Filter by state (requested, approved, suggested, dismissed, etc.)")
    list_parser.add_argument("--risk", choices=["high", "medium", "low"], help="Filter by risk")
    list_parser.add_argument("--type", dest="action_type", help="Filter by action type (e.g. gmail.send)")

    preview_parser = sub.add_parser("preview", help="Show full details for a review item")
    _add_common_config_arg(preview_parser)
    preview_parser.add_argument("--action-id", required=True)

    approve_parser = sub.add_parser("approve", help="Approve a pending action")
    _add_common_config_arg(approve_parser)
    approve_parser.add_argument("--action-id")
    approve_parser.add_argument("--all", dest="approve_all", action="store_true", help="Bulk approve matching low-risk pending actions")
    approve_parser.add_argument("--approver", default=None)
    approve_parser.add_argument("--reason", default=None)
    approve_parser.add_argument("--risk", choices=["high", "medium", "low"], help="Bulk approve risk filter; must be low")
    approve_parser.add_argument("--type", dest="action_type", help="Bulk approve action type filter")
    approve_parser.add_argument("--confirm-low-risk-bulk", action="store_true", help="Required confirmation for low-risk bulk approve")

    dismiss_parser = sub.add_parser("dismiss", help="Dismiss a pending action or suggestion")
    _add_common_config_arg(dismiss_parser)
    dismiss_parser.add_argument("--action-id", required=True)
    dismiss_parser.add_argument("--reason", required=True)

    execute_parser = sub.add_parser("execute", help="Execute an approved pending action")
    _add_common_config_arg(execute_parser)
    execute_parser.add_argument("--action-id", required=True)

    summary_parser = sub.add_parser("summary", help="Show grouped queue counts and recommended next step")
    _add_common_config_arg(summary_parser)

    audit_parser = sub.add_parser("audit", help="Show recent pending-action/workspace audit records")
    _add_common_config_arg(audit_parser)
    audit_parser.add_argument("--limit", type=int, default=20)

    return parser


def _main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "sub_config"):
        args.config = args.sub_config

    if args.command == "list":
        return cmd_list(args)
    if args.command == "preview":
        return cmd_preview(args)
    if args.command == "approve":
        return cmd_approve(args)
    if args.command == "dismiss":
        return cmd_dismiss(args)
    if args.command == "execute":
        return cmd_execute(args)
    if args.command == "summary":
        return cmd_summary(args)
    if args.command == "audit":
        return cmd_audit(args)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
