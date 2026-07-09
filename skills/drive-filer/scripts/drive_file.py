#!/usr/bin/env python3
"""Drive file operations via WorkspaceClient — search, upload, download.

Commands:
    drive_file.py search --query "NDA" --max 10
    drive_file.py upload --file /tmp/report.pdf --parent <folder_id>
    drive_file.py download --file-id <id> --output /tmp/downloaded.pdf
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

def _print_result(result: dict, summary: bool, label: str) -> None:
    if summary:
        success = result.get("success", False)
        icon = "✅" if success else "❌"
        provider = result.get("provider", "?")
        audited = "yes" if result.get("audited") else "no"
        target = result.get("target", "")
        print(f"{icon} {label}" + (f": {target}" if target else ""))
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


def cmd_search(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        print("Could not load config", file=sys.stderr)
        return 1
    client = get_client(cfg)
    results = client.drive_search(args.query or "", max_results=args.max)
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    return 0


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


def cmd_download(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg is None:
        print("Could not load config", file=sys.stderr)
        return 1
    client = get_client(cfg)
    result = client.drive_download(args.file_id, args.output)
    _print_result(result, args.summary, "Drive file downloaded")
    return 0 if result.get("success") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Drive file operations via WorkspaceClient")
    parser.add_argument("--config", help="Path to company.yaml")
    parser.add_argument("--summary", action="store_true", help="Print human-readable summary")
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search", help="Search Google Drive files")
    search.add_argument("--query", default="")
    search.add_argument("--max", type=int, default=10)

    upload = sub.add_parser("upload", help="Upload a file to Drive")
    upload.add_argument("--file", required=True, help="Local file path")
    upload.add_argument("--parent", help="Parent folder ID")

    download = sub.add_parser("download", help="Download a file from Drive")
    download.add_argument("--file-id", required=True)
    download.add_argument("--output", required=True, help="Local output path")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "search":
            return cmd_search(args)
        elif args.command == "upload":
            return cmd_upload(args)
        elif args.command == "download":
            return cmd_download(args)
        else:
            parser.error("unknown command")
            return 2
    except Exception as exc:
        print(f"drive_file.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())