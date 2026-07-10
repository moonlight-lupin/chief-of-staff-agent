#!/usr/bin/env python3
"""Email organisation onboarding CLI — inspect, propose, save, validate.

Read-only: discovers existing Gmail labels, infers categories, generates
a proposed label policy, and saves it locally for operator approval.

Never creates, applies, archives, trashes, or sends anything.

Commands:
    email_organisation.py inspect-labels [--summary | --json]
    email_organisation.py propose-policy [--summary | --json]
    email_organisation.py show-policy [--summary | --json]
    email_organisation.py save-policy --from <file> --approved-by <name>
    email_organisation.py validate-policy
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


def get_client(config: Any):
    from workspace_client import get_workspace_client
    return get_workspace_client(config)


def cmd_inspect_labels(args: argparse.Namespace) -> int:
    """Discover and display existing Gmail labels."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1

    client = get_client(cfg)

    from workspace_capabilities import require_capability
    unsupported = require_capability(client, "gmail.labels.list", target="labels")
    if unsupported:
        if args.summary:
            print("❌ gmail.labels.list not supported by current provider")
            print(f"   Provider: {client.provider_name}")
        else:
            print_json(unsupported)
        return 1

    from email_label_policy import parse_labels
    raw_labels = client.gmail_list_labels()
    parsed = parse_labels(raw_labels)

    if args.summary:
        print("📬 Email Organisation — Label Inspection")
        print()
        print(f"Total labels: {parsed['total']}")
        print(f"User labels: {len(parsed['user_labels'])}")
        print(f"System labels: {len(parsed['system_labels'])}")
        print(f"Nested labels: {len(parsed['nested_user_labels'])}")
        print()

        groups = parsed["groups"]
        if groups:
            print("Detected groups:")
            for parent, labels in sorted(groups.items()):
                print(f"- {parent}: {len(labels)} label(s)")
                for name in labels:
                    print(f"    └─ {name}")
            print()

        if parsed["user_labels"]:
            print("User labels with inferred categories:")
            for label in parsed["user_labels"]:
                cat = label.get("inferred_category") or "unmapped"
                conf = label.get("inferred_confidence", 0)
                conf_str = f" ({conf:.0%})" if conf > 0 else ""
                print(f"  {label['name']} → {cat}{conf_str}")
            print()

        unmapped = [l for l in parsed["user_labels"] if not l.get("inferred_category")]
        if unmapped:
            print("Unmapped labels:")
            for label in unmapped:
                print(f"  {label['name']}")
    else:
        print_json(parsed)

    return 0


def cmd_propose_policy(args: argparse.Namespace) -> int:
    """Generate a proposed label policy from existing Gmail labels."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1

    client = get_client(cfg)

    from workspace_capabilities import require_capability
    unsupported = require_capability(client, "gmail.labels.list", target="labels")
    if unsupported:
        if args.summary:
            print("❌ gmail.labels.list not supported by current provider")
        else:
            print_json(unsupported)
        return 1

    from email_label_policy import parse_labels, generate_policy, save_proposal
    raw_labels = client.gmail_list_labels()
    parsed = parse_labels(raw_labels)
    policy = generate_policy(parsed, provider=client.provider_name)
    path = save_proposal(cfg, policy)

    if args.summary:
        print("🧭 Proposed Email Organisation Policy")
        print()
        print(f"Mode: {policy['mode']}")
        print(f"Mapped categories: {len(policy['categories'])}")
        print(f"Unmapped labels: {len(policy['unmapped_labels'])}")
        print(f"New labels proposed: 0")
        print()

        high_conf = [(cat, data) for cat, data in policy["categories"].items()
                     if data.get("confidence", 0) >= 0.75]
        if high_conf:
            print("High-confidence mappings:")
            for cat, data in sorted(high_conf, key=lambda x: -x[1].get("confidence", 0)):
                print(f"  {cat} → {data['preferred_label']} ({data['confidence']:.0%})")
            print()

        if policy["unmapped_labels"]:
            print("Unmapped labels:")
            for item in policy["unmapped_labels"]:
                print(f"  {item['name']} — {item['reason']}")
            print()

        print("No Gmail changes were made.")
        print(f"Proposal saved to: {path}")
    else:
        policy["_proposal_path"] = str(path)
        print_json(policy)

    return 0


def cmd_show_policy(args: argparse.Namespace) -> int:
    """Show the current approved policy."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1

    from email_label_policy import load_policy
    policy = load_policy(cfg)
    if not policy:
        if args.summary:
            print("No approved policy found. Run 'propose-policy' first, then 'save-policy'.")
        else:
            print_json({"error": "no_approved_policy"})
        return 1

    if args.summary:
        print("📋 Approved Email Organisation Policy")
        print()
        print(f"Status: {policy.get('status', 'unknown')}")
        print(f"Approved by: {policy.get('approved_by', 'unknown')}")
        print(f"Mode: {policy.get('mode', 'unknown')}")
        print(f"Categories: {len(policy.get('categories', {}))}")
        print()
        for cat, data in sorted(policy.get("categories", {}).items()):
            print(f"  {cat} → {data.get('preferred_label', '?')} ({data.get('confidence', 0):.0%})")
        if policy.get("unmapped_labels"):
            print(f"\nUnmapped: {len(policy['unmapped_labels'])} label(s)")
    else:
        print_json(policy)

    return 0


def cmd_save_policy(args: argparse.Namespace) -> int:
    """Save a proposal as an approved policy."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1

    from email_label_policy import save_approved_policy, load_proposal, validate_policy
    from pathlib import Path as P

    # Load from --from file or default proposal path
    if args.from_file:
        from_path = P(args.from_file)
        if not from_path.exists():
            print(f"File not found: {from_path}", file=sys.stderr)
            return 1
        try:
            policy = json.loads(from_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"Invalid JSON: {exc}", file=sys.stderr)
            return 1
    else:
        policy = load_proposal(cfg)
        if not policy:
            print("No proposal found. Run 'propose-policy' first.", file=sys.stderr)
            return 1

    # Validate before saving
    errors = validate_policy(policy)
    if errors:
        print("Policy validation failed:", file=sys.stderr)
        for err in errors:
            print(f"  ❌ {err}", file=sys.stderr)
        return 1

    path = save_approved_policy(cfg, policy, approved_by=args.approved_by)

    if args.summary:
        print(f"✅ Policy approved and saved to: {path}")
        print(f"   Approved by: {args.approved_by}")
        print(f"   Categories: {len(policy.get('categories', {}))}")
    else:
        print_json({"saved": True, "path": str(path), "approved_by": args.approved_by})

    return 0


def cmd_validate_policy(args: argparse.Namespace) -> int:
    """Validate a policy file."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1

    from email_label_policy import load_policy, load_proposal, validate_policy

    policy = load_policy(cfg)
    source = "approved policy"
    if not policy:
        policy = load_proposal(cfg)
        source = "proposal"
    if not policy:
        print("No policy or proposal found.", file=sys.stderr)
        return 1

    errors = validate_policy(policy)
    if errors:
        print(f"❌ {source} has {len(errors)} error(s):")
        for err in errors:
            print(f"  ❌ {err}")
        return 1
    else:
        print(f"✅ {source} is valid")
        return 0


# ─── v0.1.27: Classification and Suggestion Commands ──────────

def cmd_classify_inbox(args: argparse.Namespace) -> int:
    """Classify recent inbox emails against the approved policy."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1

    from email_classifier import classify_inbox as do_classify
    from email_label_policy import load_policy

    policy = load_policy(cfg)
    if not policy:
        if args.summary:
            print("❌ No approved policy found. Run: propose-policy → save-policy first.")
        else:
            print_json({"error": "no_approved_policy"})
        return 1

    client = get_client(cfg)
    from workspace_capabilities import require_capability
    unsupported = require_capability(client, "gmail.search", target="inbox")
    if unsupported:
        if args.summary:
            print("❌ gmail.search not supported by current provider")
        else:
            print_json(unsupported)
        return 1

    emails = client.gmail_search(query="is:inbox", max_results=args.limit)
    result = do_classify(cfg, emails, limit=args.limit)

    if args.summary:
        if result.get("no_policy"):
            print(f"❌ {result.get('error', 'No policy')}")
        else:
            print(f"📧 Inbox Classification")
            print(f"  Classified: {result['classified']}")
            print(f"  With category: {result['with_category']}")
            print(f"  Unmapped: {result['unmapped']}")
            for cls in result.get("details", [])[:10]:
                cat = cls.get("category") or "unmapped"
                conf = f"{cls['confidence']:.0%}" if cls.get("confidence") else "?"
                label = cls.get("matched_policy_label", "")
                label_str = f" → {label}" if label else ""
                print(f"  {cat} ({conf}){label_str}: {cls.get('subject', '')[:50]}")
    else:
        print_json(result)
    return 0


def cmd_suggest(args: argparse.Namespace) -> int:
    """Generate email organisation suggestions from classifications."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1

    from email_classifier import generate_org_suggestions
    result = generate_org_suggestions(cfg, limit=args.limit, dry_run=args.dry_run)

    if args.summary:
        if result.get("error"):
            print(f"❌ {result['error']}")
            return 1
        mode = "dry-run" if args.dry_run else "saved"
        print(f"🧭 Email Organisation Suggestions ({mode})")
        print(f"  Generated: {result['generated']}")
        print(f"  Label suggestions: {result['label_suggestions']}")
        print(f"  Archive suggestions: {result['archive_suggestions']}")
        print(f"  Create-label suggestions: {result['create_label_suggestions']}")
        for sug in result.get("details", [])[:10]:
            risk_icon = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(sug.get("execution_risk", ""), "?")
            conf = f"{sug['confidence']:.0%}"
            print(f"  {risk_icon} {sug['action_type']} ({conf}): {sug['title']}")
        print()
        print("No Gmail changes were made.")
    else:
        print_json(result)
    return 0 if not result.get("error") else 1


def cmd_list_suggestions(args: argparse.Namespace) -> int:
    """List email organisation suggestions."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from email_classifier import list_org_suggestions
    suggestions = list_org_suggestions(cfg, state=args.state, action_type=args.action_type, limit=args.limit)
    if args.summary:
        if not suggestions:
            print("No suggestions")
        else:
            for s in suggestions:
                icon = {"suggested": "💡", "dismissed": "❌", "acted_on": "✅"}.get(s["state"], "?")
                risk_icon = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(s.get("execution_risk", ""), "?")
                print(f"{icon} {risk_icon} {s['id']}  {s['action_type']}  [conf={s['confidence']:.0%}]")
                print(f"   {s['title']}")
    else:
        print_json(suggestions)
    return 0


def cmd_preview_suggestion(args: argparse.Namespace) -> int:
    """Preview a single suggestion."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from email_classifier import get_org_suggestion
    sug = get_org_suggestion(cfg, args.suggestion_id)
    if not sug:
        print(f"Suggestion not found: {args.suggestion_id}", file=sys.stderr)
        return 1
    print_json(sug)
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    """Prepare a pending action from a suggestion."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from email_classifier import prepare_pending_from_suggestion
    result = prepare_pending_from_suggestion(cfg, args.suggestion_id)
    if args.summary:
        if result.get("success"):
            print(f"📋 Pending action created: {result.get('action_type', '?')}")
            print(f"   {result.get('message', '')}")
        else:
            print(f"❌ {result.get('error', 'unknown error')}")
    else:
        print_json(result)
    return 0 if result.get("success") else 1


def cmd_pending(args: argparse.Namespace) -> int:
    """List pending email organisation actions."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from email_classifier import list_pending_org
    actions = list_pending_org(cfg)
    if args.summary:
        if not actions:
            print("No pending email organisation actions")
        else:
            for a in actions:
                icon = {"requested": "📨", "approved": "✅", "executed": "✅",
                        "cancelled": "❌", "expired": "⏰", "executing": "⏳"}.get(a.get("state"), "?")
                print(f"{icon} {a['id']}  {a['type']}  [{a['state']}]")
                print(f"   {a.get('summary', '')}")
    else:
        print_json(actions)
    return 0


def cmd_dismiss_suggestion(args: argparse.Namespace) -> int:
    """Dismiss a suggestion."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from email_classifier import dismiss_org_suggestion
    result = dismiss_org_suggestion(cfg, args.suggestion_id, reason=args.reason)
    if not result:
        print(f"Suggestion not found or not in 'suggested' state: {args.suggestion_id}", file=sys.stderr)
        return 1
    print_json(result)
    return 0


# ─── v0.1.28: Digest and Notification Commands ──────────────

def cmd_digest(args: argparse.Namespace) -> int:
    """Render email organisation digest — read-only, no mutations."""
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    from email_classifier import render_email_org_digest
    digest = render_email_org_digest(cfg)
    if args.summary:
        print(digest["text"])
    else:
        print_json(digest)
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    """Deliver email organisation digest via a channel.

    CLI channel: prints digest to stdout.
    Email channel: creates a pending action (NOT auto-sent) for operator approval.

    Notification CANNOT approve, execute, or auto-send.
    """
    cfg = load_config(args.config)
    if cfg is None:
        return 1

    from email_classifier import render_email_org_digest
    digest = render_email_org_digest(cfg)

    if digest["total_classified"] == 0:
        if args.summary:
            print("No email classifications to digest. Run 'classify-inbox' first.")
        else:
            print_json({"delivered": False, "reason": "no_classifications"})
        return 0

    if args.channel == "cli":
        if args.summary:
            print(digest["text"])
        else:
            print_json(digest)
        return 0

    elif args.channel == "email":
        if not args.to:
            print("--to is required for email channel", file=sys.stderr)
            return 1
        from pending_actions import create_pending_action
        from workspace_client import get_workspace_client
        from workspace_capabilities import require_capability
        client = get_workspace_client(cfg)
        unsupported = require_capability(client, "gmail.send", target=args.to)
        if unsupported:
            if args.summary:
                print(f"❌ gmail.send not supported by {client.provider_name}")
            else:
                print_json({"delivered": False, "error": "gmail.send not supported"})
            return 1
        action = create_pending_action(
            config=cfg,
            action_type="gmail.send",
            provider=client.provider_name,
            target=args.to,
            payload={
                "to": args.to,
                "subject": args.subject,
                "body": digest["text"],
                "cc": None,
                "source": "email_org_digest",
            },
            summary=f"Email org digest to {args.to} ({digest['total_classified']} classified)",
        )
        if args.summary:
            print(f"📋 Digest email prepared ({digest['total_classified']} classified)")
            print(f"   Approve with: send_email.py approve --action-id {action['id'] if action else '?'}")
        else:
            print_json({
                "delivered": False,  # not yet delivered — pending approval
                "pending_action_id": action["id"] if action else None,
                "message": "Pending action created — approve to send",
            })
        return 0

    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Email organisation — onboarding, classification, suggestions")
    parser.add_argument("--config", help="Path to company.yaml")
    parser.add_argument("--summary", action="store_true", help="Print human-readable summary")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    sub = parser.add_subparsers(dest="command", required=True)

    # v0.1.26 commands
    sub.add_parser("inspect-labels", help="Discover and display existing Gmail labels")
    sub.add_parser("propose-policy", help="Generate a proposed label policy")
    sub.add_parser("show-policy", help="Show current approved policy")

    save = sub.add_parser("save-policy", help="Save a proposal as an approved policy")
    save.add_argument("--from", dest="from_file", help="Path to proposal JSON (default: standard proposal path)")
    save.add_argument("--approved-by", required=True, help="Name of approver")

    sub.add_parser("validate-policy", help="Validate a policy file")

    # v0.1.27 commands
    classify = sub.add_parser("classify-inbox", help="Classify recent inbox emails against policy")
    classify.add_argument("--limit", type=int, default=50, help="Max emails to classify")

    suggest = sub.add_parser("suggest", help="Generate email organisation suggestions")
    suggest.add_argument("--limit", type=int, default=50, help="Max suggestions")
    suggest.add_argument("--dry-run", action="store_true", help="Show plan without saving suggestions")

    list_sug = sub.add_parser("list-suggestions", help="List email organisation suggestions")
    list_sug.add_argument("--state", choices=["suggested", "dismissed", "acted_on"])
    list_sug.add_argument("--action-type", help="Filter by action type")
    list_sug.add_argument("--limit", type=int, default=50)

    get_sug = sub.add_parser("preview", help="Preview a suggestion")
    get_sug.add_argument("--suggestion-id", required=True)

    prepare = sub.add_parser("prepare", help="Prepare a pending action from a suggestion")
    prepare.add_argument("--suggestion-id", required=True)

    pending = sub.add_parser("pending", help="List pending email organisation actions")

    dismiss_sug = sub.add_parser("dismiss", help="Dismiss a suggestion")
    dismiss_sug.add_argument("--suggestion-id", required=True)
    dismiss_sug.add_argument("--reason", help="Dismissal reason")

    # v0.1.28 commands
    sub.add_parser("digest", help="Render email organisation digest")

    notify = sub.add_parser("notify", help="Deliver email organisation digest via channel")
    notify.add_argument("--channel", required=True, choices=["cli", "email"],
                        help="Delivery channel")
    notify.add_argument("--to", help="Email recipient (for email channel)")
    notify.add_argument("--subject", default="Chief-of-Staff: Email Organisation Digest")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect-labels":
            return cmd_inspect_labels(args)
        elif args.command == "propose-policy":
            return cmd_propose_policy(args)
        elif args.command == "show-policy":
            return cmd_show_policy(args)
        elif args.command == "save-policy":
            return cmd_save_policy(args)
        elif args.command == "validate-policy":
            return cmd_validate_policy(args)
        elif args.command == "classify-inbox":
            return cmd_classify_inbox(args)
        elif args.command == "suggest":
            return cmd_suggest(args)
        elif args.command == "list-suggestions":
            return cmd_list_suggestions(args)
        elif args.command == "preview":
            return cmd_preview_suggestion(args)
        elif args.command == "prepare":
            return cmd_prepare(args)
        elif args.command == "pending":
            return cmd_pending(args)
        elif args.command == "dismiss":
            return cmd_dismiss_suggestion(args)
        elif args.command == "digest":
            return cmd_digest(args)
        elif args.command == "notify":
            return cmd_notify(args)
        else:
            parser.error("unknown command")
            return 2
    except Exception as exc:
        print(f"email_organisation.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())