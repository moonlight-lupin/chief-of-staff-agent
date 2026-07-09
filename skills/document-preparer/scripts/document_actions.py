#!/usr/bin/env python3
"""Document preparation + Drive upload + Gmail draft via WorkspaceClient.

Commands:
    document_actions.py upload --file /tmp/generated.docx --parent <folder_id>
    document_actions.py search --query "NDA" --max 5
    document_actions.py draft-email --to client@test.com --subject "NDA for review" --body "..."
    document_actions.py handoff --file /tmp/NDA.docx --parent <id> --to client@x.com --subject "NDA" --body "..."
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


def _print_result(result: dict[str, Any], summary: bool, action_label: str) -> None:
    """Print result as JSON or human-readable summary."""
    if summary:
        success = result.get("success", False)
        icon = "✅" if success else "❌"
        provider = result.get("provider", "?")
        audited = "yes" if result.get("audited") else "no"
        target = result.get("target", "")
        print(f"{icon} {action_label}" + (f": {target}" if target else ""))
        print(f"Provider: {provider}")
        print(f"Audited: {audited}")
        data = result.get("data", {})
        for key in ("id", "path", "display_url", "htmlLink", "webViewLink"):
            if key in data:
                print(f"{key}: {data[key]}")
        if result.get("error"):
            print(f"Error: {result['error']}")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


def cmd_upload(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        print("Could not load config", file=sys.stderr)
        return 1
    if not os.path.isfile(args.file):
        print(f"File not found: {args.file}", file=sys.stderr)
        return 1
    client = get_client(cfg)
    result = client.drive_upload(args.file, parent_id=args.parent)
    _print_result(result, args.summary, "Drive file uploaded")
    return 0 if result.get("success") else 1


def cmd_search(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    client = get_client(cfg)
    results = client.drive_search(args.query, max_results=args.max)
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_draft_email(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        return 1
    client = get_client(cfg)
    # Check capability before calling provider method
    from workspace_capabilities import require_capability
    unsupported = require_capability(client, "gmail.draft", target=args.to)
    if unsupported:
        _print_result(unsupported, args.summary, "Gmail draft created")
        return 1
    result = client.gmail_create_draft(args.to, args.subject, args.body, cc=args.cc)
    _print_result(result, args.summary, "Gmail draft created")
    return 0 if result.get("success") else 1


def cmd_handoff(args: argparse.Namespace) -> int:
    """Combined workflow: upload file to Drive, then create Gmail draft with link.

    Flow:
    1. Check drive.upload and gmail.draft capabilities
    2. If gmail.draft unsupported and --allow-partial not set, fail cleanly before side effects
    3. Upload file to Drive
    4. Extract share link from upload result
    5. Create Gmail draft with Drive link in body
    6. Return combined summary
    """
    cfg = load_config(args.config)
    if cfg is None:
        print("Could not load config", file=sys.stderr)
        return 1
    if not os.path.isfile(args.file):
        print(f"File not found: {args.file}", file=sys.stderr)
        return 1
    client = get_client(cfg)

    # Check capabilities before any side effects
    from workspace_capabilities import require_capability
    draft_unsupported = require_capability(client, "gmail.draft", target=args.to)

    if draft_unsupported and not args.allow_partial:
        # Fail cleanly without uploading (avoid partial side effects)
        combined = {
            "success": False,
            "action": "document.handoff",
            "provider": client.provider_name,
            "steps": {"drive_upload": None, "gmail_draft": None},
            "error": f"document.handoff requires gmail.draft, which is not supported by provider {client.provider_name}. "
                     f"Use provider=composio for full handoff, or pass --allow-partial to upload without drafting.",
            "audited": False,
        }
        _print_result(combined, args.summary, "Document handoff not supported")
        return 1

    # Step 1: Upload to Drive
    upload_result = client.drive_upload(args.file, parent_id=args.parent)
    if not upload_result.get("success"):
        combined = {
            "success": False,
            "action": "document.handoff",
            "provider": upload_result.get("provider", "?"),
            "steps": {"drive_upload": upload_result, "gmail_draft": None},
            "error": upload_result.get("error", "drive upload failed"),
        }
        _print_result(combined, args.summary, "Document handoff failed")
        return 1

    # If gmail.draft unsupported but --allow-partial, return partial result
    if draft_unsupported:
        combined = {
            "success": False,
            "action": "document.handoff",
            "provider": client.provider_name,
            "steps": {"drive_upload": upload_result, "gmail_draft": None},
            "error": draft_unsupported["error"],
            "audited": False,
        }
        _print_result(combined, args.summary, "Document handoff partial (draft unsupported)")
        return 1

    # Step 2: Extract share link from upload result
    upload_data = upload_result.get("data", {})
    drive_link = (
        upload_data.get("webViewLink")
        or upload_data.get("htmlLink")
        or upload_data.get("display_url")
        or upload_data.get("link")
        or ""
    )

    # Step 3: Create Gmail draft with Drive link in body
    body_with_link = args.body
    if drive_link:
        body_with_link = f"{args.body}\n\nDrive link: {drive_link}"
    draft_result = client.gmail_create_draft(args.to, args.subject, body_with_link, cc=args.cc)

    combined = {
        "success": draft_result.get("success", False),
        "action": "document.handoff",
        "provider": upload_result.get("provider", "?"),
        "steps": {
            "drive_upload": upload_result,
            "gmail_draft": draft_result,
        },
        "error": draft_result.get("error") if not draft_result.get("success") else None,
    }
    _print_result(combined, args.summary, "Document handoff completed")
    return 0 if combined["success"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Document + Drive operations via WorkspaceClient")
    parser.add_argument("--config", help="Path to company.yaml")
    parser.add_argument("--summary", action="store_true", help="Print human-readable summary instead of JSON")
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

    handoff = sub.add_parser("handoff", help="Upload to Drive + create Gmail draft in one command")
    handoff.add_argument("--file", required=True, help="Local file to upload")
    handoff.add_argument("--parent", help="Parent folder ID")
    handoff.add_argument("--to", required=True, help="Draft recipient email")
    handoff.add_argument("--subject", required=True)
    handoff.add_argument("--body", required=True, help="Email body (Drive link appended automatically)")
    handoff.add_argument("--cc")
    handoff.add_argument("--allow-partial", action="store_true",
                         help="Upload to Drive even if gmail.draft is unsupported by provider")

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
        elif args.command == "handoff":
            return cmd_handoff(args)
        else:
            parser.error("unknown command")
            return 2
    except Exception as exc:
        print(f"document_actions.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())