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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Email organisation onboarding — read-only")
    parser.add_argument("--config", help="Path to company.yaml")
    parser.add_argument("--summary", action="store_true", help="Print human-readable summary")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("inspect-labels", help="Discover and display existing Gmail labels")
    sub.add_parser("propose-policy", help="Generate a proposed label policy")
    sub.add_parser("show-policy", help="Show current approved policy")

    save = sub.add_parser("save-policy", help="Save a proposal as an approved policy")
    save.add_argument("--from", dest="from_file", help="Path to proposal JSON (default: standard proposal path)")
    save.add_argument("--approved-by", required=True, help="Name of approver")

    sub.add_parser("validate-policy", help="Validate a policy file")

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
        else:
            parser.error("unknown command")
            return 2
    except Exception as exc:
        print(f"email_organisation.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())