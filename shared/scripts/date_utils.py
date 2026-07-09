#!/usr/bin/env python3
"""Jurisdiction-aware date helpers for Chief-of-Staff deadline tracking.

The functions here deliberately keep legal-date logic transparent and
conservative. They cover the bundled SG/HK/US/UK jurisdiction packs and expose
small primitives that scripts can compose for statutory deadlines, reminders,
and briefing urgency buckets.
"""

from __future__ import annotations

import argparse
import calendar
import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Mapping

DateLike = date | datetime | str

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

# Simple known public holidays for statutory deadline roll-forward. These are
# intentionally limited to national/public holidays most likely to affect filing
# offices. If a filing portal publishes special closure days, add them here.
_PUBLIC_HOLIDAYS: dict[str, set[str]] = {
    "SG": {
        # 2025
        "2025-01-01", "2025-01-29", "2025-01-30", "2025-03-31", "2025-04-18",
        "2025-05-01", "2025-05-12", "2025-06-07", "2025-08-09", "2025-10-20",
        "2025-12-25",
        # 2026
        "2026-01-01", "2026-02-17", "2026-02-18", "2026-03-20", "2026-04-03",
        "2026-05-01", "2026-05-27", "2026-05-31", "2026-08-09", "2026-11-08",
        "2026-12-25",
    },
    "HK": {
        # 2025
        "2025-01-01", "2025-01-29", "2025-01-30", "2025-01-31", "2025-04-04",
        "2025-04-18", "2025-04-19", "2025-04-21", "2025-05-01", "2025-05-05",
        "2025-05-31", "2025-07-01", "2025-10-01", "2025-10-07", "2025-10-29",
        "2025-12-25", "2025-12-26",
        # 2026
        "2026-01-01", "2026-02-17", "2026-02-18", "2026-02-19", "2026-04-03",
        "2026-04-04", "2026-04-06", "2026-04-07", "2026-05-01", "2026-05-25",
        "2026-06-19", "2026-07-01", "2026-09-26", "2026-10-01", "2026-10-19",
        "2026-12-25", "2026-12-26",
    },
    "US": {
        # 2025 federal holidays
        "2025-01-01", "2025-01-20", "2025-02-17", "2025-05-26", "2025-06-19",
        "2025-07-04", "2025-09-01", "2025-10-13", "2025-11-11", "2025-11-27",
        "2025-12-25",
        # 2026 federal holidays
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-05-25", "2026-06-19",
        "2026-07-03", "2026-09-07", "2026-10-12", "2026-11-11", "2026-11-26",
        "2026-12-25",
    },
    "UK": {
        # England and Wales bank holidays
        "2025-01-01", "2025-04-18", "2025-04-21", "2025-05-05", "2025-05-26",
        "2025-08-25", "2025-12-25", "2025-12-26",
        "2026-01-01", "2026-04-03", "2026-04-06", "2026-05-04", "2026-05-25",
        "2026-08-31", "2026-12-25", "2026-12-28",
    },
}


def parse_date(value: DateLike) -> date:
    """Parse ISO, common numeric, or ``DD Mon YYYY`` dates into ``date``."""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Cannot parse date: {value!r}")


def days_until(date_str: DateLike) -> int:
    """Return calendar days from today to ``date_str``.

    Negative values mean the date is overdue.
    """

    return (parse_date(date_str) - date.today()).days


def categorize_deadline(deadline_date: DateLike) -> str:
    """Return ``overdue``, ``within_7``, ``within_30``, or ``future``."""

    delta = days_until(deadline_date)
    if delta < 0:
        return "overdue"
    if delta <= 7:
        return "within_7"
    if delta <= 30:
        return "within_30"
    return "future"


def _jurisdiction_code(jurisdiction: str | None) -> str:
    return (jurisdiction or "").upper().replace("GB", "UK")


def is_business_day(date_str: DateLike, jurisdiction: str) -> bool:
    """Return True if ``date_str`` is not a weekend or known public holiday."""

    d = parse_date(date_str)
    code = _jurisdiction_code(jurisdiction)
    if d.weekday() >= 5:
        return False
    return d.isoformat() not in _PUBLIC_HOLIDAYS.get(code, set())


def next_business_day(value: DateLike, jurisdiction: str) -> date:
    """Roll ``value`` forward to the next business day in ``jurisdiction``."""

    d = parse_date(value)
    while not is_business_day(d, jurisdiction):
        d += timedelta(days=1)
    return d


def add_months(value: DateLike, months: int) -> date:
    """Add calendar months while preserving end-of-month where possible."""

    d = parse_date(value)
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, last_day))


def _parse_fy_end(value: Any) -> tuple[int, int]:
    if value is None:
        return (12, 31)
    text = str(value).strip()
    try:
        d = parse_date(text)
        return d.month, d.day
    except ValueError:
        pass
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)", text)
    if m:
        day = int(m.group(1))
        month = _MONTHS[m.group(2).lower()]
        return month, day
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2})", text)
    if m:
        month = _MONTHS[m.group(1).lower()]
        day = int(m.group(2))
        return month, day
    raise ValueError(f"Cannot parse financial_year_end: {value!r}")


def _fy_end_for_year(company_info: Mapping[str, Any], year: int) -> date:
    month, day = _parse_fy_end(company_info.get("financial_year_end") or company_info.get("fy_end"))
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _next_fy_end(company_info: Mapping[str, Any], reference: date | None = None) -> date:
    ref = reference or date.today()
    candidate = _fy_end_for_year(company_info, ref.year)
    if candidate < ref:
        candidate = _fy_end_for_year(company_info, ref.year + 1)
    return candidate


def _next_anniversary(base: date, reference: date | None = None) -> date:
    ref = reference or date.today()
    day = min(base.day, calendar.monthrange(ref.year, base.month)[1])
    candidate = date(ref.year, base.month, day)
    if candidate < ref:
        day = min(base.day, calendar.monthrange(ref.year + 1, base.month)[1])
        candidate = date(ref.year + 1, base.month, day)
    return candidate


def _next_month_day(month: int, day: int, reference: date | None = None) -> date:
    ref = reference or date.today()
    day_this_year = min(day, calendar.monthrange(ref.year, month)[1])
    candidate = date(ref.year, month, day_this_year)
    if candidate < ref:
        day_next_year = min(day, calendar.monthrange(ref.year + 1, month)[1])
        candidate = date(ref.year + 1, month, day_next_year)
    return candidate


def _quarter_end_after(reference: date) -> date:
    quarter_ends = [(3, 31), (6, 30), (9, 30), (12, 31)]
    for month, day in quarter_ends:
        candidate = date(reference.year, month, day)
        if candidate >= reference:
            return candidate
    return date(reference.year + 1, 3, 31)


def _extract_month_day_dates(trigger: str) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    # "April 15" / "Sept 15"
    for month_name, day in re.findall(r"\b([A-Za-z]{3,9})\s+(\d{1,2})\b", trigger):
        month = _MONTHS.get(month_name.lower())
        if month:
            pairs.append((month, int(day)))
    # "30 Nov" / "1 May"
    for day, month_name in re.findall(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\b", trigger):
        month = _MONTHS.get(month_name.lower())
        if month:
            pair = (month, int(day))
            if pair not in pairs:
                pairs.append(pair)
    return pairs


def _company_value(company_info: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in company_info and company_info[name] not in (None, ""):
            return company_info[name]
        if "company" in company_info and isinstance(company_info["company"], Mapping):
            nested = company_info["company"]
            if name in nested and nested[name] not in (None, ""):
                return nested[name]
    return None


def compute_statutory_deadline(requirement: Mapping[str, Any], company_info: Mapping[str, Any]) -> date | None:
    """Compute the next actionable deadline for a statutory requirement.

    Args:
        requirement: A jurisdiction-pack requirement with at least ``trigger``.
        company_info: Company config mapping (or the ``company`` section). Uses
            ``incorporation_date``, ``financial_year_end``, ``jurisdiction``,
            and optional event dates such as ``last_rorc_change_date``.

    Returns:
        A ``date`` for the next concrete deadline, rolled forward to the next
        business day where relevant. Returns ``None`` when the trigger depends
        on an event date not supplied by config (for example, RORC updates after
        a register change).
    """

    trigger = str(requirement.get("trigger", "")).lower()
    name = str(requirement.get("name", "")).lower()
    jurisdiction = _jurisdiction_code(str(_company_value(company_info, "jurisdiction") or requirement.get("jurisdiction") or "SG"))
    today = date.today()

    incorporation_raw = _company_value(company_info, "incorporation_date")
    incorporation_date = parse_date(incorporation_raw) if incorporation_raw else None

    # Event/change-driven triggers: use explicit trigger/event date if present.
    if "register change" in trigger or "on change" in str(requirement.get("frequency", "")).lower():
        event_raw = (
            requirement.get("event_date")
            or _company_value(company_info, "last_register_change_date", "last_rorc_change_date", "trigger_date")
        )
        if not event_raw:
            return None
        base = parse_date(event_raw)
        days_match = re.search(r"within\s+(\d+)\s+days?", trigger)
        due = base + timedelta(days=int(days_match.group(1)) if days_match else 0)
        return next_business_day(due, jurisdiction)

    # First AGM edge cases should be based on incorporation when still relevant.
    if "agm" in name and incorporation_date and ("first" in trigger or "18 months" in trigger):
        first_due = add_months(incorporation_date, 18)
        if first_due >= today:
            return next_business_day(first_due, jurisdiction)

    # "within N months of FY end/accounting period end"
    m = re.search(r"within\s+(\d+)\s+months?\s+of\s+(?:fy|financial year|accounting period)\s+end", trigger)
    if m:
        months = int(m.group(1))
        fy = _next_fy_end(company_info, today - timedelta(days=months * 31))
        # Find the next FY-based due date that has not passed.
        for year in range(fy.year - 1, fy.year + 3):
            due = add_months(_fy_end_for_year(company_info, year), months)
            if due >= today:
                return next_business_day(due, jurisdiction)
        return next_business_day(add_months(fy, months), jurisdiction)

    # "within N days/months of incorporation anniversary"
    if "incorporation anniversary" in trigger and incorporation_date:
        base = _next_anniversary(incorporation_date, today - timedelta(days=60))
        days_match = re.search(r"within\s+(\d+)\s+days?", trigger)
        months_match = re.search(r"within\s+(\d+)\s+months?", trigger)
        for year_offset in range(0, 3):
            anniversary = date(base.year + year_offset, base.month, min(base.day, calendar.monthrange(base.year + year_offset, base.month)[1]))
            if days_match:
                due = anniversary + timedelta(days=int(days_match.group(1)))
            elif months_match:
                due = add_months(anniversary, int(months_match.group(1)))
            else:
                due = anniversary
            if due >= today:
                return next_business_day(due, jurisdiction)

    # "within N months of incorporation"
    if "of incorporation" in trigger and incorporation_date:
        months_match = re.search(r"within\s+(\d+)\s+months?\s+of\s+incorporation", trigger)
        days_match = re.search(r"within\s+(\d+)\s+days?\s+of\s+incorporation", trigger)
        if months_match:
            return next_business_day(add_months(incorporation_date, int(months_match.group(1))), jurisdiction)
        if days_match:
            return next_business_day(incorporation_date + timedelta(days=int(days_match.group(1))), jurisdiction)

    # Quarterly: "within N days of quarter end" or listed payment dates.
    if "quarter" in trigger or str(requirement.get("frequency", "")).lower() == "quarterly":
        listed = _extract_month_day_dates(trigger)
        if listed:
            candidates = [next_business_day(_next_month_day(month, day, today), jurisdiction) for month, day in listed]
            return min(d for d in candidates if d >= today)
        days_match = re.search(r"within\s+(\d+)\s+days?\s+of\s+quarter\s+end", trigger)
        days = int(days_match.group(1)) if days_match else 0
        ref = today - timedelta(days=days + 1)
        for _ in range(8):
            qe = _quarter_end_after(ref)
            due = qe + timedelta(days=days)
            if due >= today:
                return next_business_day(due, jurisdiction)
            ref = qe + timedelta(days=1)

    # State-level US annual reports. Keep common defaults, otherwise anniversary.
    if "varies by state" in trigger or "annual report" in name:
        state = str(_company_value(company_info, "state", "incorporation_state") or "").upper()
        if jurisdiction == "US" and state in {"DE", "DELAWARE"}:
            return next_business_day(_next_month_day(3, 1, today), jurisdiction)
        if jurisdiction == "US" and state in {"CA", "CALIFORNIA"} and incorporation_date:
            anniversary = _next_anniversary(incorporation_date, today - timedelta(days=45))
            last_day = calendar.monthrange(anniversary.year, anniversary.month)[1]
            candidate = date(anniversary.year, anniversary.month, last_day)
            if candidate < today:
                candidate = date(anniversary.year + 1, anniversary.month, calendar.monthrange(anniversary.year + 1, anniversary.month)[1])
            return next_business_day(candidate, jurisdiction)
        if incorporation_date:
            return next_business_day(_next_anniversary(incorporation_date), jurisdiction)

    # Notice pattern: "issued by IRD in April, due 1 month later". Use the
    # first day of the issue month as a conservative default until the actual
    # notice date is recorded as a custom deadline.
    notice_match = re.search(r"issued\b.*\bin\s+([A-Za-z]{3,9})\b.*\bdue\s+(\d+(?:\.\d+)?)\s+months?\s+later", trigger)
    if notice_match:
        issue_month = _MONTHS.get(notice_match.group(1).lower())
        months_later = int(float(notice_match.group(2)))
        if issue_month:
            issue_date = _next_month_day(issue_month, 1, today - timedelta(days=62))
            due = add_months(issue_date, months_later)
            if due < today:
                issue_date = date(issue_date.year + 1, issue_date.month, 1)
                due = add_months(issue_date, months_later)
            return next_business_day(due, jurisdiction)

    # Fixed annual dates: "by April 15", "by 30 Nov", "by May 1 each year".
    pairs = _extract_month_day_dates(trigger)
    if pairs:
        candidates = [next_business_day(_next_month_day(month, day, today), jurisdiction) for month, day in pairs]
        return min(d for d in candidates if d >= today)

    # Specific explicit ISO date in trigger/requirement.
    for key in ("due", "deadline", "deadline_date"):
        if requirement.get(key):
            return next_business_day(parse_date(requirement[key]), jurisdiction)

    return None


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chief-of-Staff date utility helpers")
    sub = parser.add_subparsers(dest="command")

    p_days = sub.add_parser("days-until", help="Print days from today until a date")
    p_days.add_argument("date")

    p_cat = sub.add_parser("categorize", help="Print deadline category for a date")
    p_cat.add_argument("date")

    p_biz = sub.add_parser("is-business-day", help="Check business day in SG/HK/US/UK")
    p_biz.add_argument("date")
    p_biz.add_argument("jurisdiction")

    p_compute = sub.add_parser("compute", help="Compute deadline from a JSON requirement and company JSON")
    p_compute.add_argument("--requirement", required=True, help="JSON object with name/frequency/trigger")
    p_compute.add_argument("--company", required=True, help="JSON object with company fields")

    args = parser.parse_args(argv)
    if args.command == "days-until":
        print(days_until(args.date))
    elif args.command == "categorize":
        print(categorize_deadline(args.date))
    elif args.command == "is-business-day":
        print("true" if is_business_day(args.date, args.jurisdiction) else "false")
    elif args.command == "compute":
        req = json.loads(args.requirement)
        company = json.loads(args.company)
        due = compute_statutory_deadline(req, company)
        print(due.isoformat() if due else "")
    else:
        parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
