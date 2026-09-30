#!/usr/bin/env python3
"""Stand-in for google-workspace google_api.py — drive surface used by backup.py.

Logs argv (sans script path) to $FAKE_GOOGLE_API_LOG. Rejects legacy
``--account`` / ``--as`` flags so contract tests prove backup does not use them.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

if "--account" in sys.argv or "--as" in sys.argv:
    print("legacy identity flags are not supported", file=sys.stderr)
    sys.exit(2)

log = os.environ.get("FAKE_GOOGLE_API_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\n")

parser = argparse.ArgumentParser(prog="google_api.py")
sub = parser.add_subparsers(dest="service", required=True)
drv = sub.add_parser("drive")
drv_sub = drv.add_subparsers(dest="action", required=True)

search = drv_sub.add_parser("search")
search.add_argument("query")
search.add_argument("--max", type=int, default=10)
search.add_argument("--raw-query", action="store_true")

upload = drv_sub.add_parser("upload")
upload.add_argument("path")
upload.add_argument("--parent", default="")
upload.add_argument("--name", default="")

delete = drv_sub.add_parser("delete")
delete.add_argument("file_id")
delete.add_argument("--permanent", action="store_true")

args = parser.parse_args()
if args.service == "drive" and args.action == "search":
    print(json.dumps([]))
elif args.service == "drive" and args.action == "upload":
    print(json.dumps({"status": "uploaded", "id": "fake-file-id", "name": "backup.tar.gz"}))
elif args.service == "drive" and args.action == "delete":
    print(json.dumps({"status": "deleted", "file_id": args.file_id}))
else:
    print(json.dumps({"ok": True}))
