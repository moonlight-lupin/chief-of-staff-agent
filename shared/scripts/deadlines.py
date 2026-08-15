#!/usr/bin/env python3
"""Unified deadline engine for chief-of-staff.

Computes both statutory (from jurisdiction pack) and custom (from company.yaml) deadlines,
categorizes by urgency, and returns structured results.

Usage:
    from deadlines import compute_all_deadlines
    result = compute_all_deadlines(config)
    # Returns: [{"name":..., "due_date":..., "days_until":..., "category":..., "source":...}]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    raise RuntimeError("PyYAML is required for deadlines.py") from exc

from date_utils import categorize_deadline, compute_statutory_deadline, days_until, parse_date

try:
    from config_loader import Config, load_config
except Exception:  # pragma: no cover
    Config = dict  # type: ignore
    load_config = None  # type: ignore


PLUGIN_ROOT = Path(__file__).resolve().parents[2]


class DeadlineError(RuntimeError):
    """Raised when deadline inputs cannot be computed."""


def _plain(value: Any) -> Any:
    if hasattr(value, "to_plain_dict"):
        return value.to_plain_dict()
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _company_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    return config.get("company", config) if isinstance(config.get("company", config), Mapping) else {}


def _jurisdiction_code(config_or_company: Mapping[str, Any]) -> str:
    company = _company_section(config_or_company)
    return str(company.get("jurisdiction") or config_or_company.get("jurisdiction") or "").lower()


def load_jurisdiction_pack(jurisdiction: str) -> dict[str, Any]:
    code = str(jurisdiction or "").lower()
    if not code:
        raise DeadlineError("company.jurisdiction is required")
    path = PLUGIN_ROOT / "shared" / "config" / "jurisdictions" / f"{code}.yaml"
    if not path.exists():
        raise DeadlineError(f"Jurisdiction pack not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise DeadlineError(f"Cannot parse jurisdiction pack {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("statutory"), list):
        raise DeadlineError(f"Invalid jurisdiction pack {path}: missing statutory list")
    return data


def _entry(name: str, due: Any, source: str, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    due_date = parse_date(due)
    row = {
        "name": name,
        "due_date": due_date.isoformat(),
        "days_until": days_until(due_date),
        "category": categorize(due_date),
        "source": source,
    }
    if extra:
        for key in ("id", "authority", "frequency", "notes", "penalty", "conditional", "status"):
            if key in extra:
                row[key] = extra[key]
    return row


def categorize(deadline_date: Any) -> str:
    """Categorize a deadline date using shared date_utils logic."""

    return categorize_deadline(deadline_date)


def compute_statutory(jurisdiction_pack: Mapping[str, Any], company_info: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Compute concrete dates for all statutory requirements that can be resolved."""

    results: list[dict[str, Any]] = []
    for req in jurisdiction_pack.get("statutory", []) or []:
        if not isinstance(req, Mapping):
            continue
        due = compute_statutory_deadline(req, company_info)
        if due is None:
            continue
        results.append(_entry(str(req.get("name") or req.get("id") or "Unnamed statutory deadline"), due, "statutory", req))
    return results


def _custom_items(custom_deadlines: Any) -> list[Mapping[str, Any]]:
    if custom_deadlines is None:
        return []
    if isinstance(custom_deadlines, Mapping):
        if isinstance(custom_deadlines.get("custom"), list):
            return [x for x in custom_deadlines["custom"] if isinstance(x, Mapping)]
        return [custom_deadlines]
    if isinstance(custom_deadlines, list):
        return [x for x in custom_deadlines if isinstance(x, Mapping)]
    return []


def compute_custom(custom_deadlines: Any) -> list[dict[str, Any]]:
    """Normalize custom deadline entries from company.yaml."""

    results: list[dict[str, Any]] = []
    for item in _custom_items(custom_deadlines):
        due = item.get("due_date") or item.get("due") or item.get("deadline")
        if not due:
            continue
        try:
            results.append(_entry(str(item.get("name") or item.get("id") or "Custom deadline"), due, "custom", item))
        except ValueError as exc:
            raise DeadlineError(f"Invalid custom deadline {item.get('name', item)}: {exc}") from exc
    return results


def compute_all_deadlines(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Combine statutory and custom deadlines and sort by due date."""

    plain = _plain(config)
    if not isinstance(plain, Mapping):
        raise DeadlineError("config must be a mapping")
    company = _company_section(plain)
    pack = load_jurisdiction_pack(_jurisdiction_code(plain))
    deadlines = compute_statutory(pack, {**dict(company), "company": dict(company)})
    deadlines.extend(compute_custom(plain.get("deadlines", {}).get("custom") if isinstance(plain.get("deadlines"), Mapping) else plain.get("deadlines")))
    deadlines.sort(key=lambda row: (row["due_date"], row["name"]))
    return deadlines


def _is_done(deadline: dict[str, Any]) -> bool:
    """True if a deadline entry is marked status: done (case-insensitive)."""

    return str(deadline.get("status", "")).strip().lower() == "done"


def filter_actionable(deadlines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return overdue and within-30-days deadlines, excluding any marked status: done."""

    return [
        d
        for d in deadlines
        if not _is_done(d) and d.get("category") in {"overdue", "within_7", "within_30"}
    ]


def filter_overdue(deadlines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only overdue deadlines, excluding any marked status: done."""

    return [
        d
        for d in deadlines
        if not _is_done(d)
        and (d.get("category") == "overdue" or int(d.get("days_until", 999999)) < 0)
    ]


def _within(deadlines: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
    return [d for d in deadlines if 0 <= int(d.get("days_until", 999999)) <= days]


def _load_config_arg(path: str | None) -> Mapping[str, Any]:
    if load_config is not None:
        cfg = load_config(path)
        if cfg is None:
            raise DeadlineError(f"Could not load config: {path or 'default company.yaml'}")
        return cfg
    if not path:
        raise DeadlineError("--config is required when config_loader is unavailable")
    data = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise DeadlineError("Config must be a mapping")
    return data


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute Chief-of-Staff statutory and custom deadlines")
    parser.add_argument("--config", default=None, help="Path to company.yaml")
    parser.add_argument("--within", type=int, help="Only include deadlines due within N days (excluding overdue)")
    parser.add_argument("--overdue", action="store_true", help="Only include overdue deadlines")
    parser.add_argument("--json", action="store_true", help="Print JSON")
    args = parser.parse_args(argv)
    try:
        result = compute_all_deadlines(_load_config_arg(args.config))
        if args.overdue:
            result = filter_overdue(result)
        if args.within is not None:
            result = _within(result, args.within)
    except DeadlineError as exc:
        print(f"ERROR: {exc}")
        return 1
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        for item in result:
            print(f"{item['due_date']} ({item['category']}, {item['days_until']}d) {item['name']} [{item['source']}]")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
