#!/usr/bin/env python3
"""Stand-in for the shipped google-workspace skill's google_api.py.

Its ``contacts`` subcommand offers only ``list`` — the surface that broke
v0.7.1's contacts writes in the field. Every invocation is appended to
$FAKE_GOOGLE_API_LOG so tests can prove what was (not) run.
"""
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
lst = contacts_sub.add_parser("list")
lst.add_argument("--max", type=int, default=50)
cal = sub.add_parser("calendar")
cal.add_subparsers(dest="action", required=True).add_parser("list")
args = parser.parse_args()
print(json.dumps([]))
