#!/usr/bin/env python3
"""Document preparation + Drive upload via WorkspaceClient.

Commands:
    document_actions.py upload --file /tmp/generated.docx --parent <folder_id>
    document_actions.py search --query "NDA" --max 5
    document_actions.py draft-email --to client@test.com --subject "NDA for review" --body "Please find attached..."
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


def get_client(config: Any):
    from workspace_client import get_workspace_client
    return get_workspace_client(config)


def cmd_upload(args: argparse.Namespace) -> int:
    """Upload a generated document to Drive."""
    cfg = load_config(args.config)
    if cfg is None:
        print("Could not load config", file=sys.stderr)
        return 1
    if not os.path.isfile(args.file):
        print(f"File not found: {args.file}", file=sys.stderr)
        return 1
    client = get_client(cfg)
    result = client.drive_upload(args.file, parent_id=args.parent)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("success") else 1


def cmd_search(args: argparse.Namespace) -> int:
    """Search Drive for existing documents."""
    cfg = load_config(args.config)
    if cfg is None:
        print("Could not load config", file=sys.stderr)
        return 1
    client = get_client(cfg)
    results = client.drive_search(args.query, max_results=args.max)
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_draft_email(args: argparse.Namespace) -> int:
    """Create a Gmail draft with document context (e.g. 'Please find attached NDA')."""
    cfg = load_config(args.config)
    if cfg is None:
        print("Could not load config", file=sys.stderr)
        return 1
    client = get_client(cfg)
    result = client.gmail_create_draft(args.to, args.subject, args.body, cc=args.cc)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("success") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Document + Drive operations via WorkspaceClient")
    parser.add_argument("--config", help="Path to company.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    upload = sub.add_parser("upload", help="Upload a document to Drive")
    upload.add_argument("--file", required=True, help="Local file path")
    upload.add_argument("--parent", help="Parent folder ID")

    search = sub.add_parser("search", help="Search Drive for documents")
    search.add_argument("--query", required=True)
    search.add_argument("--max", type=int, default=10)

    draft = sub.add_parser("draft-email", help="Create a Gmail draft")
    draft.add_argument("--to", required=True)
    draft.add_argument("--subject", required=True)
    draft.add_argument("--body", required=True)
    draft.add_argument("--cc")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "upload":
            return cmd_upload(args)
        elif args.command == "search":
            return cmd_search(args)
        elif args.command == "draft-email":
            return cmd_draft_email(args)
        else:
            parser.error("unknown command")
            return 2
    except Exception as exc:
        print(f"document_actions.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())