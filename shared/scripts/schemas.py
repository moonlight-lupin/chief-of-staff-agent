#!/usr/bin/env python3
"""Schema validation for chief-of-staff data stores.

Usage:
    from schemas import validate_deal, validate_invoice, validate_todo
    validate_deal(deal_dict)  # raises ValueError if invalid
"""

from __future__ import annotations

import argparse
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    raise RuntimeError("PyYAML is required for schemas.py") from exc

try:
    from config_loader import load_config
except Exception:  # pragma: no cover
    load_config = None  # type: ignore


class SchemaError(ValueError):
    """Validation error with a field name in the message."""


DEFAULT_SALES_STAGES = ["Lead", "Proposal Sent", "NDA Signed", "Contract Signed", "Invoiced", "Paid"]
INVOICE_DIRECTIONS = {"sent", "received"}
INVOICE_STATUSES = {"draft", "sent", "paid", "overdue", "cancelled", "received", "approved"}
TODO_STATUSES = {"open", "done", "deferred", "cancelled"}
TODO_PRIORITIES = {"high", "medium", "low"}
STORE_KEYS = {
    "pipeline": "deals",
    "invoices": "invoices",
    "expenses": "expenses",
    "todos": "todos",
}


def _sales_stages(config: Mapping[str, Any] | None = None) -> list[str]:
    if config and isinstance(config.get("sales_stages"), list):
        return [str(s) for s in config["sales_stages"]]
    if load_config is not None:
        cfg = load_config()
        if cfg and isinstance(cfg.get("sales_stages"), list):
            return [str(s) for s in cfg["sales_stages"]]
    return DEFAULT_SALES_STAGES


def _require_mapping(obj: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(obj, Mapping):
        raise SchemaError(f"{label}: expected mapping")
    return obj


def _require_fields(obj: Mapping[str, Any], fields: list[str], label: str) -> None:
    for field in fields:
        value = obj.get(field)
        if value is None or value == "":
            raise SchemaError(f"{label}.{field}: required")


def _require_non_empty_string(obj: Mapping[str, Any], field: str, label: str) -> None:
    value = obj.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{label}.{field}: must be a non-empty string")


def _require_non_negative_number(obj: Mapping[str, Any], field: str, label: str) -> None:
    value = obj.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(f"{label}.{field}: must be a non-negative number")
    if value < 0:
        raise SchemaError(f"{label}.{field}: must be non-negative")


def validate_deal(deal: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> None:
    """Validate a pipeline deal."""

    deal = _require_mapping(deal, "deal")
    _require_fields(deal, ["id", "client_name", "stage"], "deal")
    _require_non_empty_string(deal, "id", "deal")
    stage = str(deal.get("stage"))
    stages = _sales_stages(config)
    if stage not in stages:
        raise SchemaError(f"deal.stage: {stage!r} not in configured sales_stages {stages!r}")


def validate_invoice(inv: Mapping[str, Any]) -> None:
    """Validate an invoice or bill record."""

    inv = _require_mapping(inv, "invoice")
    _require_fields(inv, ["id", "direction", "counterparty", "amount", "issue_date", "due_date", "status"], "invoice")
    _require_non_empty_string(inv, "id", "invoice")
    if str(inv.get("direction")) not in INVOICE_DIRECTIONS:
        raise SchemaError("invoice.direction: must be one of sent, received")
    if str(inv.get("status")) not in INVOICE_STATUSES:
        raise SchemaError(f"invoice.status: must be one of {sorted(INVOICE_STATUSES)}")
    _require_non_negative_number(inv, "amount", "invoice")


def validate_expense(exp: Mapping[str, Any]) -> None:
    """Validate an expense record."""

    exp = _require_mapping(exp, "expense")
    _require_fields(exp, ["id", "category", "vendor", "amount", "date", "status"], "expense")
    _require_non_empty_string(exp, "id", "expense")
    _require_non_negative_number(exp, "amount", "expense")


def validate_todo(todo: Mapping[str, Any]) -> None:
    """Validate a todo record."""

    todo = _require_mapping(todo, "todo")
    _require_fields(todo, ["id", "title", "status"], "todo")
    _require_non_empty_string(todo, "id", "todo")
    if str(todo.get("status")) not in TODO_STATUSES:
        raise SchemaError(f"todo.status: must be one of {sorted(TODO_STATUSES)}")
    priority = todo.get("priority")
    if priority is not None and priority != "" and str(priority) not in TODO_PRIORITIES:
        raise SchemaError(f"todo.priority: must be one of {sorted(TODO_PRIORITIES)}")


def validate_store(store_name: str, data: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> None:
    """Validate every record in a supported YAML store."""

    data = _require_mapping(data, store_name)
    key = STORE_KEYS.get(store_name)
    if key is None:
        return
    records = data.get(key, [])
    if records is None:
        records = []
    if not isinstance(records, list):
        raise SchemaError(f"{key}: must be a list")
    for idx, record in enumerate(records):
        try:
            if store_name == "pipeline":
                validate_deal(record, config=config)
            elif store_name == "invoices":
                validate_invoice(record)
            elif store_name == "expenses":
                validate_expense(record)
            elif store_name == "todos":
                validate_todo(record)
        except SchemaError as exc:
            raise SchemaError(f"{key}[{idx}].{exc}") from exc


def generate_id(prefix: str = "deal") -> str:
    """Generate an ID like ``deal-20260709T120000Z-a1b2``."""

    clean = "".join(ch for ch in str(prefix or "id").lower() if ch.isalnum() or ch in "-_").strip("-_") or "id"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{clean}-{ts}-{secrets.token_hex(2)}"


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Chief-of-Staff YAML data stores")
    parser.add_argument("--validate", choices=sorted(STORE_KEYS), required=True, help="Store type to validate")
    parser.add_argument("--file", required=True, help="YAML file to validate")
    args = parser.parse_args(argv)
    path = Path(args.file).expanduser()
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    validate_store(args.validate, data)
    print(f"OK: {args.validate} valid ({path})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
