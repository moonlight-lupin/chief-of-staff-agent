#!/usr/bin/env python3
"""Generate a monthly cash-basis P&L report for Chief-of-Staff Bookkeeper.

Reads ``invoices.yaml`` and ``expenses.yaml`` from the configured project root.
Revenue is paid sent invoices with paid_date in the report month. Expenses are
paid expenses with date in the report month. Outstanding AR/AP are unpaid
invoices regardless of month.
"""

from __future__ import annotations

import argparse
import calendar
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    print(f"PyYAML is required for pl_report.py: {exc}", file=sys.stderr)
    raise SystemExit(2)

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

try:
    from config_loader import get_project_root, load_config  # type: ignore
except Exception as exc:  # pragma: no cover
    print(f"Cannot import shared config_loader.py from {SHARED_SCRIPTS}: {exc}", file=sys.stderr)
    raise SystemExit(2)

UNPAID_STATUSES = {"draft", "sent", "received", "approved", "submitted", "overdue", "partially_paid"}
CANCELLED_STATUSES = {"cancelled", "void", "written_off"}


@dataclass(frozen=True)
class MonthWindow:
    month: str
    start: date
    end: date
    label: str


def parse_month(value: str) -> MonthWindow:
    try:
        year_s, month_s = value.split("-", 1)
        year = int(year_s)
        month = int(month_s)
        if not 1 <= month <= 12:
            raise ValueError
    except ValueError as exc:
        raise argparse.ArgumentTypeError("month must be in YYYY-MM format") from exc
    last = calendar.monthrange(year, month)[1]
    start = date(year, month, 1)
    end = date(year, month, last)
    label = start.strftime("%B %Y")
    return MonthWindow(value, start, end, label)


def parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def money(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid money amount: {value!r}") from exc


def load_yaml_list(path: Path, top_key: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, Mapping):
        raise ValueError(f"{path} must contain a mapping with key {top_key!r}")
    records = loaded.get(top_key, []) or []
    if not isinstance(records, list):
        raise ValueError(f"{path}:{top_key} must be a list")
    normalized: list[dict[str, Any]] = []
    for idx, item in enumerate(records, start=1):
        if not isinstance(item, Mapping):
            raise ValueError(f"{path}:{top_key}[{idx}] must be a mapping")
        normalized.append(dict(item))
    return normalized


def in_month(value: Any, window: MonthWindow) -> bool:
    parsed = parse_date(value)
    return parsed is not None and window.start <= parsed <= window.end


def status(record: Mapping[str, Any]) -> str:
    return str(record.get("status", "")).strip().lower()


def currency_of(record: Mapping[str, Any], default: str) -> str:
    return str(record.get("currency") or default or "").upper() or "UNSPECIFIED"


def add_amount(bucket: dict[str, Decimal], currency: str, amount: Decimal) -> None:
    bucket[currency] = bucket.get(currency, Decimal("0.00")) + amount


def format_amount(amount: Decimal, currency: str) -> str:
    return f"{currency} {amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


def format_bucket(bucket: Mapping[str, Decimal]) -> str:
    if not bucket:
        return "0.00"
    return " / ".join(format_amount(amount, currency) for currency, amount in sorted(bucket.items()))


def bucket_subtract(a: Mapping[str, Decimal], b: Mapping[str, Decimal]) -> dict[str, Decimal]:
    currencies = set(a) | set(b)
    return {currency: a.get(currency, Decimal("0.00")) - b.get(currency, Decimal("0.00")) for currency in currencies}


def sorted_records(records: Iterable[Mapping[str, Any]], date_field: str) -> list[Mapping[str, Any]]:
    return sorted(records, key=lambda r: (parse_date(r.get(date_field)) or date.max, str(r.get("id", ""))))


def generate_report(project_root: Path, window: MonthWindow, default_currency: str) -> str:
    invoices_path = project_root / "invoices.yaml"
    expenses_path = project_root / "expenses.yaml"
    invoices = load_yaml_list(invoices_path, "invoices")
    expenses = load_yaml_list(expenses_path, "expenses")

    revenue_by_client: dict[str, dict[str, Decimal]] = defaultdict(dict)
    revenue_total: dict[str, Decimal] = {}
    paid_revenue_count = 0

    for inv in invoices:
        if str(inv.get("direction", "")).lower() != "sent":
            continue
        if status(inv) != "paid":
            continue
        if not in_month(inv.get("paid_date"), window):
            continue
        currency = currency_of(inv, default_currency)
        amount = money(inv.get("amount"))
        client = str(inv.get("counterparty") or "Unknown counterparty")
        add_amount(revenue_total, currency, amount)
        add_amount(revenue_by_client[client], currency, amount)
        paid_revenue_count += 1

    expense_by_category: dict[str, dict[str, Decimal]] = defaultdict(dict)
    expense_total: dict[str, Decimal] = {}
    paid_expense_count = 0

    for exp in expenses:
        if status(exp) != "paid":
            continue
        if not in_month(exp.get("date"), window):
            continue
        currency = currency_of(exp, default_currency)
        amount = money(exp.get("amount"))
        category = str(exp.get("category") or "uncategorized").strip().lower()
        add_amount(expense_total, currency, amount)
        add_amount(expense_by_category[category], currency, amount)
        paid_expense_count += 1

    outstanding_ar: dict[str, Decimal] = {}
    outstanding_ap: dict[str, Decimal] = {}
    overdue_ar: list[Mapping[str, Any]] = []
    overdue_ap: list[Mapping[str, Any]] = []
    today = date.today()

    for inv in invoices:
        inv_status = status(inv)
        if inv_status == "paid" or inv_status in CANCELLED_STATUSES:
            continue
        direction = str(inv.get("direction", "")).lower()
        currency = currency_of(inv, default_currency)
        amount = money(inv.get("amount"))
        due = parse_date(inv.get("due_date"))
        if direction == "sent":
            add_amount(outstanding_ar, currency, amount)
            if due and due < today:
                overdue_ar.append(inv)
        elif direction == "received":
            add_amount(outstanding_ap, currency, amount)
            if due and due < today:
                overdue_ap.append(inv)

    net = bucket_subtract(revenue_total, expense_total)

    lines: list[str] = []
    lines.append(f"📊 P&L Summary — {window.label}")
    lines.append("")
    lines.append(f"Project root: {project_root}")
    lines.append("")
    lines.append("Revenue (Invoices Paid)")
    lines.append(f"  Total: {format_bucket(revenue_total)}")
    lines.append(f"  Paid invoice count: {paid_revenue_count}")
    if revenue_by_client:
        lines.append("  By client:")
        for client in sorted(revenue_by_client):
            lines.append(f"    {client}: {format_bucket(revenue_by_client[client])}")
    else:
        lines.append("  By client: none")
    lines.append("")
    lines.append("Expenses")
    lines.append(f"  Total: {format_bucket(expense_total)}")
    lines.append(f"  Paid expense count: {paid_expense_count}")
    if expense_by_category:
        lines.append("  By category:")
        for category in sorted(expense_by_category):
            label = category.replace("_", " ").title()
            lines.append(f"    {label}: {format_bucket(expense_by_category[category])}")
    else:
        lines.append("  By category: none")
    lines.append("")
    lines.append(f"Net P&L: {format_bucket(net)}")
    lines.append("")
    lines.append(f"Outstanding AR: {format_bucket(outstanding_ar)} ({len(overdue_ar)} invoices overdue)")
    for inv in sorted_records(overdue_ar, "due_date")[:10]:
        lines.append(
            f"  - {inv.get('id', 'unknown')} {inv.get('counterparty', 'Unknown')}: "
            f"{format_amount(money(inv.get('amount')), currency_of(inv, default_currency))}, due {inv.get('due_date')}"
        )
    if len(overdue_ar) > 10:
        lines.append(f"  - ... {len(overdue_ar) - 10} more overdue AR invoices")

    lines.append(f"Outstanding AP: {format_bucket(outstanding_ap)} ({len(overdue_ap)} bills overdue)")
    for inv in sorted_records(overdue_ap, "due_date")[:10]:
        lines.append(
            f"  - {inv.get('id', 'unknown')} {inv.get('counterparty', 'Unknown')}: "
            f"{format_amount(money(inv.get('amount')), currency_of(inv, default_currency))}, due {inv.get('due_date')}"
        )
    if len(overdue_ap) > 10:
        lines.append(f"  - ... {len(overdue_ap) - 10} more overdue AP bills")

    if not invoices_path.exists():
        lines.append("")
        lines.append(f"Note: {invoices_path} not found; treated invoices as empty.")
    if not expenses_path.exists():
        lines.append(f"Note: {expenses_path} not found; treated expenses as empty.")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate Chief-of-Staff monthly P&L report")
    parser.add_argument("--config", required=True, help="Path to company.yaml")
    parser.add_argument("--month", required=True, type=parse_month, help="Report month in YYYY-MM format, e.g. 2026-07")
    parser.add_argument("--project-root", help="Override project root (otherwise read paths.project_root from config)")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if config is None:
        return 1

    if args.project_root:
        project_root = Path(args.project_root).expanduser().resolve()
    else:
        project_root = get_project_root(config)
        if project_root is None:
            return 1

    default_currency = str(config.get("company", {}).get("currency", "")) or "UNSPECIFIED"
    try:
        print(generate_report(project_root, args.month, default_currency))
    except Exception as exc:
        print(f"Failed to generate P&L report: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
