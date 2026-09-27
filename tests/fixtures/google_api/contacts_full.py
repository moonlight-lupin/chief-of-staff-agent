#!/usr/bin/env python3
"""Stand-in for a google_api.py that implements contacts create/update/delete."""
import argparse
import json
import os
import sys

log = os.environ.get("FAKE_GOOGLE_API_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\n")

parser = argparse.ArgumentParser(prog="google_api.py")
parser.add_argument("--account")
parser.add_argument("--as", dest="as_user")
sub = parser.add_subparsers(dest="service", required=True)
contacts = sub.add_parser("contacts")
contacts_sub = contacts.add_subparsers(dest="action", required=True)
contacts_sub.add_parser("list").add_argument("--max", type=int, default=50)
for name in ("create", "update"):
    p = contacts_sub.add_parser(name)
    for flag in ("--person-id", "--given-name", "--family-name", "--email", "--phone",
                 "--organization", "--note"):
        p.add_argument(flag)
contacts_sub.add_parser("delete").add_argument("--person-id")
args = parser.parse_args()
if args.action == "delete":
    print(json.dumps({"status": "deleted"}))
else:
    print(json.dumps({"resourceName": getattr(args, "person_id", None) or "people/c1"}))
