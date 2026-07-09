#!/usr/bin/env python3
"""Mutate and summarize Chief-of-Staff invoices.yaml."""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

try:
    from config_loader import load_config  # type: ignore
    from schemas import SchemaError, generate_id, validate_invoice  # type: ignore
    from state_store import load_store, save_store_atomic  # type: ignore
except Exception as exc:  # pragma: no cover
    print(
        f"Chief-of-Staff bootstrap incomplete: cannot import shared scripts from {SHARED_SCRIPTS}: {exc}. "
        "Run the plugin bootstrap/foundation setup first.",
        file=sys.stderr,
    )
    raise SystemExit(2)

UNPAID = {"draft", "sent", "received", "approved"}
CLOSED = {"paid", "cancelled"}
TRANSITIONS = {
    "draft": {"sent", "cancelled"},
    "sent": {"paid", "cancelled"},
    "received": {"approved", "cancelled"},
    "approved": {"paid", "cancelled"},
    "paid": set(),
    "cancelled": set(),
}


def today() -> str:
    return date.today().isoformat()


def configure(path: str | None) -> dict[str, Any]:
    if path:
        os.environ["CHIEF_OF_STAFF_CONFIG"] = path
    cfg = load_config(path)
    if cfg is None:
        raise RuntimeError("Could not load company.yaml; pass --config or set CHIEF_OF_STAFF_CONFIG")
    return cfg


def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid amount: {value!r}") from exc


def emit(payload: Any, as_json: bool) -> None:
    def default(obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)
        return str(obj)

    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=default))
    elif isinstance(payload, list):
        for inv in payload:
            print(f"{inv.get('id')}: {inv.get('direction')} {inv.get('counterparty')} {inv.get('amount')} {inv.get('currency')} [{inv.get('status')}]")
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=default))


def find_invoice(data: dict[str, Any], invoice_id: str) -> dict[str, Any]:
    for inv in data.setdefault("invoices", []):
        if isinstance(inv, dict) and str(inv.get("id")) == invoice_id:
            return inv
    raise KeyError(f"Invoice not found: {invoice_id}")


def check_transition(current: str, new: str) -> None:
    if new not in TRANSITIONS.get(current, set()):
        raise ValueError(f"Invalid invoice status transition: {current} -> {new}")


def initial_status(direction: str, requested: str | None) -> str:
    if requested:
        status = requested
    else:
        status = "sent" if direction == "sent" else "received"
    if direction == "sent" and status not in {"draft", "sent", "cancelled"}:
        raise ValueError("sent invoices must start as draft, sent, or cancelled")
    if direction == "received" and status not in {"received", "approved", "cancelled"}:
        raise ValueError("received invoices must start as received, approved, or cancelled")
    return status


def duplicate_of(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    return (
        str(existing.get("counterparty", "")).strip().lower() == str(candidate.get("counterparty", "")).strip().lower()
        and money(existing.get("amount")) == money(candidate.get("amount"))
        and str(existing.get("issue_date")) == str(candidate.get("issue_date"))
    )


def cmd_add(args: argparse.Namespace) -> dict[str, Any]:
    cfg = configure(args.config)
    data = load_store("invoices")
    invoices = data.setdefault("invoices", [])
    if not isinstance(invoices, list):
        raise ValueError("invoices.yaml 'invoices' must be a list")
    inv = {
        "id": generate_id("INV"),
        "direction": args.direction,
        "counterparty": args.counterparty,
        "amount": float(money(args.amount)),
        "currency": (args.currency or str(cfg.get("company", {}).get("currency", "")) or "UNSPECIFIED").upper(),
        "issue_date": args.issue_date,
        "due_date": args.due_date,
        "deal_id": args.deal_id,
        "status": initial_status(args.direction, args.status),
        "paid_date": None,
        "document_path": args.document_path,
        "notes": args.notes or "",
    }
    validate_invoice(inv)
    for existing in invoices:
        if isinstance(existing, dict) and duplicate_of(existing, inv):
            raise ValueError(
                "Duplicate invoice detected: same counterparty, amount, and issue_date "
                f"as {existing.get('id', '<unknown>')}"
            )
    before = copy.deepcopy(data)
    invoices.append(inv)
    save_store_atomic("invoices", data, action="add_invoice", before=before, after=data)
    return inv


def cmd_mark_paid(args: argparse.Namespace) -> dict[str, Any]:
    configure(args.config)
    data = load_store("invoices")
    before = copy.deepcopy(data)
    inv = find_invoice(data, args.id)
    current = str(inv.get("status"))
    check_transition(current, "paid")
    inv["status"] = "paid"
    inv["paid_date"] = args.paid_date or today()
    validate_invoice(inv)
    save_store_atomic("invoices", data, action="mark_paid", before=before, after=data)
    return inv


def parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def cmd_list_overdue(args: argparse.Namespace) -> list[dict[str, Any]]:
    configure(args.config)
    data = load_store("invoices")
    result = []
    now = date.today()
    for inv in data.get("invoices", []) or []:
        if not isinstance(inv, dict) or str(inv.get("status")) in CLOSED:
            continue
        due = parse_date(inv.get("due_date"))
        if due and due < now:
            result.append(dict(inv))
    return sorted(result, key=lambda i: (str(i.get("due_date")), str(i.get("id"))))


def summary(direction: str, args: argparse.Namespace) -> dict[str, Any]:
    configure(args.config)
    data = load_store("invoices")
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    count_by_currency: dict[str, int] = defaultdict(int)
    items = []
    for inv in data.get("invoices", []) or []:
        if not isinstance(inv, dict):
            continue
        if str(inv.get("direction")) != direction or str(inv.get("status")) in CLOSED:
            continue
        currency = str(inv.get("currency") or "UNSPECIFIED").upper()
        totals[currency] += money(inv.get("amount"))
        count_by_currency[currency] += 1
        items.append(inv)
    return {
        "direction": direction,
        "totals": {cur: str(amount.quantize(Decimal("0.01"))) for cur, amount in sorted(totals.items())},
        "counts": dict(sorted(count_by_currency.items())),
        "items": items,
    }


def cmd_list(args: argparse.Namespace) -> list[dict[str, Any]]:
    configure(args.config)
    data = load_store("invoices")
    records = [dict(i) for i in data.get("invoices", []) if isinstance(i, dict)]
    if args.direction:
        records = [i for i in records if i.get("direction") == args.direction]
    if args.status:
        records = [i for i in records if i.get("status") == args.status]
    return sorted(records, key=lambda i: (str(i.get("issue_date")), str(i.get("id"))))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mutate/query Chief-of-Staff invoices.yaml")
    parser.add_argument("--config", help="Path to company.yaml (or CHIEF_OF_STAFF_CONFIG)")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add")
    add.add_argument("--direction", choices=["sent", "received"], required=True)
    add.add_argument("--counterparty", required=True)
    add.add_argument("--amount", required=True)
    add.add_argument("--currency")
    add.add_argument("--issue-date", required=True)
    add.add_argument("--due-date", required=True)
    add.add_argument("--deal-id")
    add.add_argument("--status", choices=sorted(TRANSITIONS))
    add.add_argument("--document-path")
    add.add_argument("--notes")

    paid = sub.add_parser("mark-paid")
    paid.add_argument("--id", required=True)
    paid.add_argument("--paid-date")

    sub.add_parser("list-overdue")
    sub.add_parser("ar-summary")
    sub.add_parser("ap-summary")

    ls = sub.add_parser("list")
    ls.add_argument("--direction", choices=["sent", "received"])
    ls.add_argument("--status", choices=sorted(TRANSITIONS))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "add":
            result = cmd_add(args)
        elif args.command == "mark-paid":
            result = cmd_mark_paid(args)
        elif args.command == "list-overdue":
            result = cmd_list_overdue(args)
        elif args.command == "ar-summary":
            result = summary("sent", args)
        elif args.command == "ap-summary":
            result = summary("received", args)
        elif args.command == "list":
            result = cmd_list(args)
        else:  # pragma: no cover
            parser.error("unknown command")
            return 2
    except KeyError as exc:
        print(str(exc).strip("'"), file=sys.stderr)
        return 1
    except (RuntimeError, ValueError, SchemaError) as exc:
        print(f"invoices.py error: {exc}", file=sys.stderr)
        return 1
    emit(result, args.json)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
