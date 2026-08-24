#!/usr/bin/env python3
"""Schema validation for chief-of-staff data stores.

Usage:
    from schemas import validate_deal, validate_invoice, validate_todo
    validate_deal(deal_dict)  # raises ValueError if invalid

WORKSPACE PAYLOAD CONTRACT (fetch/compute split)
------------------------------------------------
An AI agent may fetch workspace data (mail, calendar, files) using its own
native connector tools (Google Workspace or Microsoft 365) and hand that data
to the Chief-of-Staff compute pipeline (classify / dedupe / render) as a single
JSON "envelope". The compute scripts (daily_briefing.py, weekly-review
workspace_collect.py, meeting-prep workspace_actions.py) accept this envelope
via ``--input PATH`` (or ``--input -`` for stdin) instead of constructing a
Python workspace client.

The envelope is a JSON object with this shape (all fields optional except the
per-record required fields listed below)::

    {
      "generated_at": "2026-07-10T08:00:00Z",   # ISO 8601, optional
      "source": "agent",                          # str, optional (e.g. "gmail"|"outlook"|"agent")
      "messages": [ <message>, ... ],             # optional, defaults to []
      "events":   [ <event>, ... ],               # optional, defaults to []
      "files":    [ <file>, ... ]                 # optional, defaults to []
    }

Record shapes (required fields, then optional fields):

  message:
    required: id (str), sender (str), subject (str), date (ISO 8601 str)
    optional: thread_id (str), snippet (str), tags (list[str], default []),
              has_attachments (bool), link (str), source (str)

  event:
    required: id (str), title (str), start (ISO 8601 str), end (ISO 8601 str)
    optional: attendees (list[str], default []), organizer (str),
              location (str), conference_link (str), event_link (str),
              status (str), source (str)

  file:
    required: id (str), name (str)
    optional: mime_type (str), modified (ISO 8601 str), link (str),
              parents (list[str], default []), source (str)

Validate an envelope with ``validate_workspace_payload(payload)`` (raises
``SchemaError`` on any violation) and get a defaults-filled copy with
``normalize_workspace_payload(payload)``. Individual records can be validated
with ``validate_message`` / ``validate_event`` / ``validate_file``.

Minimal conforming example an agent can emit::

    {"messages": [{"id": "m1", "sender": "a@x.com", "subject": "Hi",
                   "date": "2026-07-10T08:00:00Z"}],
     "events": [{"id": "e1", "title": "Standup", "start": "2026-07-10T09:00:00Z",
                 "end": "2026-07-10T09:15:00Z"}]}
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


# ── Workspace record schemas (fetch/compute split contract) ────────────────
# These validate agent-fetched JSON (mail / calendar / files) before it enters
# the compute pipeline. See the module docstring for the full contract.

def _require_iso8601(obj: Mapping[str, Any], field: str, label: str, required: bool = True) -> None:
    """Validate that ``field`` is an ISO 8601 datetime/date string."""
    value = obj.get(field)
    if value is None or value == "":
        if required:
            raise SchemaError(f"{label}.{field}: required")
        return
    if not isinstance(value, str):
        raise SchemaError(f"{label}.{field}: must be an ISO 8601 string")
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        datetime.fromisoformat(text)
    except ValueError:
        raise SchemaError(f"{label}.{field}: {value!r} is not a valid ISO 8601 datetime")


def _validate_optional_str_list(obj: Mapping[str, Any], field: str, label: str) -> None:
    value = obj.get(field)
    if value is None:
        return
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SchemaError(f"{label}.{field}: must be a list of strings")


def _validate_optional_bool(obj: Mapping[str, Any], field: str, label: str) -> None:
    value = obj.get(field)
    if value is None:
        return
    if not isinstance(value, bool):
        raise SchemaError(f"{label}.{field}: must be a boolean")


def validate_message(message: Mapping[str, Any]) -> None:
    """Validate an agent-fetched mail message record."""

    message = _require_mapping(message, "message")
    for field in ("id", "sender", "subject"):
        _require_non_empty_string(message, field, "message")
    _require_iso8601(message, "date", "message")
    _validate_optional_str_list(message, "tags", "message")
    _validate_optional_bool(message, "has_attachments", "message")


def validate_event(event: Mapping[str, Any]) -> None:
    """Validate an agent-fetched calendar event record."""

    event = _require_mapping(event, "event")
    for field in ("id", "title"):
        _require_non_empty_string(event, field, "event")
    _require_iso8601(event, "start", "event")
    _require_iso8601(event, "end", "event")
    _validate_optional_str_list(event, "attendees", "event")


def validate_file(file_rec: Mapping[str, Any]) -> None:
    """Validate an agent-fetched file record."""

    file_rec = _require_mapping(file_rec, "file")
    for field in ("id", "name"):
        _require_non_empty_string(file_rec, field, "file")
    _require_iso8601(file_rec, "modified", "file", required=False)
    _validate_optional_str_list(file_rec, "parents", "file")


_WORKSPACE_RECORD_VALIDATORS = {
    "messages": validate_message,
    "events": validate_event,
    "files": validate_file,
}


def validate_workspace_payload(payload: Mapping[str, Any]) -> None:
    """Validate an agent-fetched workspace envelope.

    Raises SchemaError on the first violation. See the module docstring for the
    envelope/record contract.
    """

    payload = _require_mapping(payload, "workspace_payload")
    _require_iso8601(payload, "generated_at", "workspace_payload", required=False)
    source = payload.get("source")
    if source is not None and not isinstance(source, str):
        raise SchemaError("workspace_payload.source: must be a string")
    for key, validator in _WORKSPACE_RECORD_VALIDATORS.items():
        records = payload.get(key)
        if records is None:
            continue
        if not isinstance(records, list):
            raise SchemaError(f"workspace_payload.{key}: must be a list")
        for idx, record in enumerate(records):
            try:
                validator(record)
            except SchemaError as exc:
                raise SchemaError(f"{key}[{idx}].{exc}") from exc


def normalize_workspace_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate ``payload`` and return a cleaned copy with defaults filled in.

    Guarantees the returned dict has ``messages``/``events``/``files`` lists and
    that each record carries its list-typed optional defaults (``tags``,
    ``attendees``, ``parents``). Does not mutate the input.
    """

    validate_workspace_payload(payload)
    cleaned: dict[str, Any] = {
        "generated_at": payload.get("generated_at"),
        "source": payload.get("source"),
        "messages": [],
        "events": [],
        "files": [],
    }
    for message in payload.get("messages") or []:
        rec = dict(message)
        rec.setdefault("tags", [])
        cleaned["messages"].append(rec)
    for event in payload.get("events") or []:
        rec = dict(event)
        rec.setdefault("attendees", [])
        cleaned["events"].append(rec)
    for file_rec in payload.get("files") or []:
        rec = dict(file_rec)
        rec.setdefault("parents", [])
        cleaned["files"].append(rec)
    return cleaned


# ── Individual record normalizers ─────────────────────────────────────────

def normalize_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a normalized message dict with defaults filled."""
    validate_message(message)
    rec = dict(message)
    rec.setdefault("tags", [])
    return rec


def normalize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a normalized event dict with defaults filled.

    Optional fields (attendees, organizer, location, conference_link,
    event_link, status, source) are preserved if present; ``attendees``
    defaults to ``[]``.
    """
    validate_event(event)
    rec = dict(event)
    rec.setdefault("attendees", [])
    return rec


def normalize_file(file_rec: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a normalized file dict with defaults filled."""
    validate_file(file_rec)
    rec = dict(file_rec)
    rec.setdefault("parents", [])
    return rec


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
