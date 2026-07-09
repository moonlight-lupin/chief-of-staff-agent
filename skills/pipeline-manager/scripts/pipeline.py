#!/usr/bin/env python3
"""Mutate and query the Chief-of-Staff sales pipeline store."""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

try:
    from config_loader import load_config  # type: ignore
    from schemas import SchemaError, generate_id, validate_deal  # type: ignore
    from state_store import load_store, save_store_atomic  # type: ignore
except Exception as exc:  # pragma: no cover
    print(
        f"Chief-of-Staff bootstrap incomplete: cannot import shared scripts from {SHARED_SCRIPTS}: {exc}. "
        "Run the plugin bootstrap/foundation setup first.",
        file=sys.stderr,
    )
    raise SystemExit(2)

VALID_STATUSES = {"active", "won", "lost", "cancelled"}


def today() -> str:
    return date.today().isoformat()


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def configure(path: str | None) -> Mapping[str, Any]:
    if path:
        os.environ["CHIEF_OF_STAFF_CONFIG"] = path
    config = load_config(path)
    if config is None:
        raise RuntimeError("Could not load company.yaml; pass --config or set CHIEF_OF_STAFF_CONFIG")
    return config


def sales_stages(config: Mapping[str, Any]) -> list[str]:
    stages = config.get("sales_stages") or []
    if not isinstance(stages, list) or not all(isinstance(s, str) for s in stages):
        raise RuntimeError("company.yaml sales_stages must be a list of stage names")
    return stages


def stale_threshold(config: Mapping[str, Any]) -> int:
    return int(config.get("stale_threshold_days") or 14)


def output(payload: Any, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return
    if isinstance(payload, list):
        for item in payload:
            print(f"{item.get('id')}: {item.get('client_name')} — {item.get('stage')} — {item.get('value')} {item.get('currency', '')}")
    elif isinstance(payload, dict):
        for key, value in payload.items():
            print(f"{key}: {value}")
    else:
        print(payload)


def normalize_deal(deal: dict[str, Any]) -> dict[str, Any]:
    deal.setdefault("documents", [])
    if not isinstance(deal.get("documents"), list):
        deal["documents"] = []
    if not isinstance(deal.get("notes"), list):
        existing = deal.get("notes")
        deal["notes"] = [] if existing in (None, "") else [{"at": deal.get("last_activity") or today(), "note": str(existing)}]
    if not isinstance(deal.get("stage_history"), list):
        hist = []
        if deal.get("stage"):
            hist.append({"stage": deal.get("stage"), "at": deal.get("created") or deal.get("last_activity") or today()})
        deal["stage_history"] = hist
    deal.setdefault("status", "active")
    return deal


def find_deal(data: dict[str, Any], deal_id: str) -> dict[str, Any]:
    for deal in data.setdefault("deals", []):
        if isinstance(deal, dict) and str(deal.get("id")) == deal_id:
            return normalize_deal(deal)
    raise KeyError(f"Deal not found: {deal_id}")


def cmd_add(args: argparse.Namespace) -> tuple[dict[str, Any], str, Any, Any]:
    config = configure(args.config)
    if args.stage not in sales_stages(config):
        raise ValueError(f"Invalid stage {args.stage!r}; valid stages: {', '.join(sales_stages(config))}")
    status = args.status or "active"
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status {status!r}; expected one of {sorted(VALID_STATUSES)}")
    data = load_store("pipeline")
    deals = data.setdefault("deals", [])
    if not isinstance(deals, list):
        raise ValueError("pipeline.yaml 'deals' must be a list")
    now = today()
    deal = {
        "id": generate_id("deal"),
        "client_name": args.client,
        "contact_name": args.contact,
        "contact_email": args.email,
        "stage": args.stage,
        "status": status,
        "value": args.value,
        "currency": args.currency,
        "created": now,
        "last_activity": now,
        "stage_history": [{"stage": args.stage, "at": now}],
        "documents": [],
        "notes": [],
    }
    validate_deal(deal)
    before = copy.deepcopy(data)
    deals.append(deal)
    save_store_atomic("pipeline", data, action="add_deal", before=before, after=data)
    return deal, "add_deal", before, data


def cmd_move(args: argparse.Namespace) -> tuple[dict[str, Any], str, Any, Any]:
    config = configure(args.config)
    stages = sales_stages(config)
    if args.stage not in stages:
        raise ValueError(f"Invalid stage {args.stage!r}; valid stages: {', '.join(stages)}")
    data = load_store("pipeline")
    before = copy.deepcopy(data)
    deal = find_deal(data, args.id)
    old_stage = deal.get("stage")
    now = today()
    deal["stage"] = args.stage
    deal["last_activity"] = now
    deal.setdefault("stage_history", []).append({"stage": args.stage, "at": now})
    if args.status:
        if args.status not in VALID_STATUSES:
            raise ValueError(f"Invalid status {args.status!r}; expected one of {sorted(VALID_STATUSES)}")
        deal["status"] = args.status
    validate_deal(deal)
    save_store_atomic("pipeline", data, action="move_stage", before={"id": args.id, "stage": old_stage}, after={"id": args.id, "stage": args.stage})
    return deal, "move_stage", before, data


def _parse_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def cmd_list(args: argparse.Namespace) -> list[dict[str, Any]]:
    config = configure(args.config)
    data = load_store("pipeline")
    records = [normalize_deal(dict(d)) for d in data.get("deals", []) if isinstance(d, dict)]
    if args.stage:
        records = [d for d in records if d.get("stage") == args.stage]
    if args.status:
        records = [d for d in records if d.get("status") == args.status]
    if args.stale:
        threshold = stale_threshold(config)
        today_date = date.today()
        filtered: list[dict[str, Any]] = []
        for deal in records:
            try:
                age = (today_date - _parse_date(deal.get("last_activity"))).days
            except Exception:
                age = threshold + 1
            if age > threshold:
                with_age = dict(deal)
                with_age["stale_days"] = age
                filtered.append(with_age)
        records = filtered
    return sorted(records, key=lambda d: (str(d.get("stage", "")), str(d.get("client_name", "")), str(d.get("id", ""))))


def cmd_add_note(args: argparse.Namespace) -> dict[str, Any]:
    configure(args.config)
    data = load_store("pipeline")
    before = copy.deepcopy(data)
    deal = find_deal(data, args.id)
    note = {"at": timestamp(), "note": args.note}
    deal.setdefault("notes", []).append(note)
    deal["last_activity"] = today()
    validate_deal(deal)
    save_store_atomic("pipeline", data, action="add_note", before=before, after=data)
    return deal


def cmd_link_doc(args: argparse.Namespace) -> dict[str, Any]:
    configure(args.config)
    data = load_store("pipeline")
    before = copy.deepcopy(data)
    deal = find_deal(data, args.id)
    doc = {"type": args.type, "path": args.path, "status": args.status, "linked_at": timestamp()}
    deal.setdefault("documents", []).append(doc)
    deal["last_activity"] = today()
    validate_deal(deal)
    save_store_atomic("pipeline", data, action="link_doc", before=before, after=data)
    return deal


def cmd_show(args: argparse.Namespace) -> dict[str, Any]:
    configure(args.config)
    data = load_store("pipeline")
    return dict(find_deal(data, args.id))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mutate/query Chief-of-Staff pipeline.yaml")
    parser.add_argument("--config", help="Path to company.yaml (or CHIEF_OF_STAFF_CONFIG)")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="Add a deal")
    add.add_argument("--client", required=True)
    add.add_argument("--contact", default="")
    add.add_argument("--email", default="")
    add.add_argument("--value", type=float, default=0)
    add.add_argument("--currency", default=None)
    add.add_argument("--stage", required=True)
    add.add_argument("--status", choices=sorted(VALID_STATUSES))

    move = sub.add_parser("move", help="Move a deal to a new stage")
    move.add_argument("--id", required=True)
    move.add_argument("--stage", required=True)
    move.add_argument("--status", choices=sorted(VALID_STATUSES))

    ls = sub.add_parser("list", help="List deals")
    ls.add_argument("--stage")
    ls.add_argument("--status", choices=sorted(VALID_STATUSES))
    ls.add_argument("--stale", action="store_true")

    note = sub.add_parser("add-note", help="Append a note to a deal")
    note.add_argument("--id", required=True)
    note.add_argument("--note", required=True)

    doc = sub.add_parser("link-doc", help="Link a document to a deal")
    doc.add_argument("--id", required=True)
    doc.add_argument("--type", required=True)
    doc.add_argument("--path", required=True)
    doc.add_argument("--status", required=True)

    show = sub.add_parser("show", help="Show a deal")
    show.add_argument("--id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "add":
            if args.currency is None:
                cfg = configure(args.config)
                args.currency = str(cfg.get("company", {}).get("currency", "")) or "UNSPECIFIED"
            result = cmd_add(args)[0]
        elif args.command == "move":
            result = cmd_move(args)[0]
        elif args.command == "list":
            result = cmd_list(args)
        elif args.command == "add-note":
            result = cmd_add_note(args)
        elif args.command == "link-doc":
            result = cmd_link_doc(args)
        elif args.command == "show":
            result = cmd_show(args)
        else:  # pragma: no cover
            parser.error("unknown command")
            return 2
    except KeyError as exc:
        print(str(exc).strip("'"), file=sys.stderr)
        return 1
    except (RuntimeError, ValueError, SchemaError) as exc:
        print(f"pipeline.py error: {exc}", file=sys.stderr)
        return 1
    output(result, as_json=args.json)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
