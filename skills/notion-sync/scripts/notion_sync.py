#!/usr/bin/env python3
"""Plan (and optionally queue) a sync between the plugin store and Notion.

The agent reads the Notion database with its own connector, writes the rows as
an envelope, and passes it in. Planning is read-only; ``--queue`` creates
pending actions that still need approval. Nothing here calls Notion.

Envelope::

    {"domain": "pipeline" | "todos",
     "rows": [{"page_id": "...", "last_edited_time": "...",
               "properties": {"CoS ID": "deal-001", "Name": "...", ...}}]}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

from company_registry import RegistryError, apply_company_flag  # noqa: E402
from config_loader import load_config  # noqa: E402
from notion_mirror import (  # noqa: E402
    PROPERTY_MAP, NotionConfigError, check_schema, plan_link, plan_sync, queue_add_columns, queue_link,
    queue_plan, queue_resolve,
)


def _summary(plan: dict, queued: dict | None) -> str:
    lines = [f"Notion sync plan — {plan['domain']}"]
    for key, label in (("push", "push to Notion"), ("pull", "Notion edits to review"),
                       ("archive", "archive in Notion"), ("conflicts", "conflicts (no action)"),
                       ("rejected", "rejected Notion values"), ("ignored", "ignored Notion edits"),
                       ("missing_in_notion", "missing in Notion"), ("in_sync", "in sync")):
        lines.append(f"  {label:<26} {len(plan.get(key) or [])}")
    for item in plan.get("conflicts") or []:
        lines.append(f"    ! {item['record_id']}: {item['reason']}")
    for item in plan.get("rejected") or []:
        lines.append(f"    ! {item.get('record_id') or item.get('page_id')}: {item['reason']}")
    if queued is not None:
        lines.append(f"  queued {len(queued['created'])} action(s) for approval; "
                     f"{len(queued['skipped_open'])} already open")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    try:
        argv = apply_company_flag(argv if argv is not None else sys.argv[1:])
    except RegistryError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", help="Path to company.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    plan_p = sub.add_parser("plan", help="Compare the store with Notion rows")
    plan_p.add_argument("--input", required=True, help="Envelope JSON with the Notion rows")
    plan_p.add_argument("--domain", choices=sorted(PROPERTY_MAP), help="Overrides the envelope's domain")
    plan_p.add_argument("--queue", action="store_true", help="Create pending actions for approval")
    plan_p.add_argument("--summary", action="store_true", help="Human-readable output")

    check_p = sub.add_parser("check", help="Check a database's columns against what sync needs")
    check_p.add_argument("--input", required=True,
                         help='JSON: {"domain": ..., "properties": {"<column name>": "<notion type>"}}')
    check_p.add_argument("--domain", choices=sorted(PROPERTY_MAP))
    check_p.add_argument("--add", action="append", default=[], metavar="COLUMN",
                         help="Queue adding this missing column (e.g. 'CoS ID'); needs approval")

    link_p = sub.add_parser("link", help="Match an existing database's rows to plugin records (once)")
    link_p.add_argument("--input", required=True, help="Rows envelope, as for plan")
    link_p.add_argument("--domain", choices=sorted(PROPERTY_MAP))
    link_p.add_argument("--queue", action="store_true", help="Queue the link for approval")

    res_p = sub.add_parser("resolve", help="Resolve a conflict by choosing the side to keep")
    res_p.add_argument("--input", required=True, help="Rows envelope, as for plan")
    res_p.add_argument("--domain", choices=sorted(PROPERTY_MAP))
    res_p.add_argument("--record", required=True, help="Record id in conflict")
    res_p.add_argument("--keep", required=True, choices=["store", "notion"])
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if config is None:
        return 1
    try:
        envelope = json.loads(Path(args.input).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Cannot read envelope {args.input}: {exc}", file=sys.stderr)
        return 1
    domain = args.domain or envelope.get("domain")
    if domain not in PROPERTY_MAP:
        print("Envelope needs 'domain' (pipeline|todos)", file=sys.stderr)
        return 1

    def emit(payload: dict) -> int:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return 0

    try:
        if args.command == "check":
            report = check_schema(config, domain, envelope.get("properties") or {})
            queued = queue_add_columns(config, report, args.add) if args.add else []
            return emit({"report": report,
                         "queued": [{"id": a["id"], "type": a["type"], "summary": a["summary"]} for a in queued]})
        rows = envelope.get("rows")
        if not isinstance(rows, list):
            print("Envelope needs a 'rows' list", file=sys.stderr)
            return 1
        if args.command == "link":
            plan = plan_link(config, domain, rows)
            action = queue_link(config, plan) if args.queue else None
            return emit({"plan": plan, "queued": {"id": action["id"], "summary": action["summary"]} if action else None})
        if args.command == "resolve":
            action = queue_resolve(config, domain, args.record, args.keep, rows)
            return emit({"queued": {"id": action["id"], "summary": action["summary"]}})
        plan = plan_sync(config, domain, rows)
    except NotionConfigError as exc:
        print(f"Notion sync refused: {exc}", file=sys.stderr)
        return 1
    queued = queue_plan(config, plan) if args.queue else None
    if args.summary:
        print(_summary(plan, queued))
    else:
        print(json.dumps({"plan": plan, "queued": queued}, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
