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
    from state_db import (  # type: ignore
        StateStoreError,
        load_store,
        mutate_kv,
    )
except Exception as exc:  # pragma: no cover
    print(
        f"Chief-of-Staff bootstrap incomplete: cannot import shared scripts from {SHARED_SCRIPTS}: {exc}. "
        "Run the plugin bootstrap/foundation setup first.",
        file=sys.stderr,
    )
    raise SystemExit(2)

VALID_STATUSES = {"active", "won", "lost", "cancelled"}
DEFAULT_TERMINAL_STAGES = {"Paid", "Lost", "Cancelled"}

RECOMMENDED_NEXT_ACTION: dict[str, str] = {
    "Lead": "qualify or close",
    "Qualified": "send proposal",
    "Proposal Sent": "follow up",
    "NDA Signed": "prepare diligence / next meeting",
    "Contract Signed": "create invoice candidate",
    "Invoiced": "check payment status",
}

STAGE_MOVE_SUGGESTIONS: dict[str, str] = {
    "Proposal Sent": "Suggestion: prepare proposal follow-up",
    "NDA Signed": "Suggestion: prepare entity research / due diligence",
    "Contract Signed": "Suggestion: prepare invoice (Bookkeeper)",
    "Invoiced": "Suggestion: check linked invoice exists",
    "Paid": "Suggestion: verify invoice marked paid",
    "Lost": "Note: reason required (use --note)",
}

ISO_DATE_FIELDS = ("created", "last_activity")


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


def terminal_stages(config: Mapping[str, Any]) -> set[str]:
    configured = config.get("terminal_stages")
    if isinstance(configured, list) and configured:
        return {str(s) for s in configured}
    return set(DEFAULT_TERMINAL_STAGES)


def recommended_next_action(stage: Any) -> str:
    return RECOMMENDED_NEXT_ACTION.get(str(stage or ""), "review deal status")


def stage_move_suggestion(stage: Any) -> str | None:
    return STAGE_MOVE_SUGGESTIONS.get(str(stage or ""))


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


def _parse_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _is_iso_date(value: Any) -> bool:
    if value in (None, ""):
        return False
    try:
        parsed = _parse_date(value)
    except Exception:
        return False
    return str(value) == parsed.isoformat() or (isinstance(value, date) and not isinstance(value, datetime))


def load_pipeline_store(config: Mapping[str, Any] | None = None, *, strict: bool = True) -> dict[str, Any]:
    """Load the pipeline KV store from SQLite.

    Missing store initializes as ``deals: []`` via ``state_db.load_store``.
    Corrupt or unreadable stores raise ``StateStoreError`` (caller should print and return 1).

    When ``strict=False`` (read/validate paths), skip per-deal schema validation so
    ``validate`` / ``stale`` / ``list`` can still inspect imperfect data.
    """
    data = load_store("pipeline", config, validate=strict)
    if not isinstance(data, dict):
        return {"deals": []}
    if "deals" not in data:
        data = dict(data)
        data.setdefault("deals", [])
    return data


def days_inactive(deal: Mapping[str, Any], today_date: date | None = None) -> int | None:
    today_date = today_date or date.today()
    raw = deal.get("last_activity")
    if raw in (None, ""):
        return None
    try:
        return (today_date - _parse_date(raw)).days
    except Exception:
        return None


def is_stale_deal(
    deal: Mapping[str, Any],
    config: Mapping[str, Any],
    today_date: date | None = None,
) -> tuple[bool, int | None]:
    stage = str(deal.get("stage") or "")
    if stage in terminal_stages(config):
        return False, days_inactive(deal, today_date)
    threshold = stale_threshold(config)
    age = days_inactive(deal, today_date)
    if age is None:
        # Missing/bad last_activity counts as stale for active deals.
        return True, age
    return age > threshold, age


def document_path_is_unfiled(path_value: str, client_name: str | None = None) -> bool:
    """Absolute paths or paths outside expected client folder structure are unfiled."""
    raw = str(path_value or "").strip()
    if not raw:
        return True
    p = Path(raw)
    if p.is_absolute():
        return True
    # Expected: relative under a clients folder structure, e.g. 02_Clients/{client}/...
    parts = [part for part in Path(raw).parts if part not in (".",)]
    if not parts:
        return True
    joined = "/".join(parts)
    lower = joined.lower()
    under_clients = (
        lower.startswith("02_clients/")
        or lower.startswith("clients/")
        or "/02_clients/" in lower
        or "/clients/" in lower
    )
    if not under_clients:
        return True
    if client_name:
        # Prefer path segment after clients folder matches client name (soft check)
        client_slug = str(client_name).strip().lower()
        if client_slug and client_slug not in lower:
            # Still filed under clients tree; not unfiled, just maybe misnamed — no warning required
            pass
    return False


def cmd_add(args: argparse.Namespace) -> tuple[dict[str, Any], str, Any, Any]:
    config = configure(args.config)
    if args.stage not in sales_stages(config):
        raise ValueError(f"Invalid stage {args.stage!r}; valid stages: {', '.join(sales_stages(config))}")
    status = args.status or "active"
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status {status!r}; expected one of {sorted(VALID_STATUSES)}")
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
    validate_deal(deal, config)
    holder: dict[str, Any] = {}

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        deals = data.setdefault("deals", [])
        if not isinstance(deals, list):
            raise ValueError("pipeline.yaml 'deals' must be a list")
        holder["before"] = copy.deepcopy(data)
        deals.append(deal)
        holder["after"] = data
        return deal

    mutate_kv("pipeline", _mutate, action="add_deal", config=config)
    return deal, "add_deal", holder.get("before"), holder.get("after")


def cmd_move(args: argparse.Namespace) -> tuple[dict[str, Any], str, Any, Any]:
    config = configure(args.config)
    stages = sales_stages(config)
    if args.stage not in stages:
        raise ValueError(f"Invalid stage {args.stage!r}; valid stages: {', '.join(stages)}")
    holder: dict[str, Any] = {}

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        holder["before"] = copy.deepcopy(data)
        deal = find_deal(data, args.id)
        old_stage = deal.get("stage")
        now = today()
        note_reason = (args.note or "").strip() if getattr(args, "note", None) else ""
        reason_text = note_reason if note_reason else "(none)"
        audit_text = f"Moved from {old_stage} to {args.stage}. Reason: {reason_text}"

        deal["stage"] = args.stage
        deal["last_activity"] = now
        deal.setdefault("stage_history", []).append(
            {"stage": args.stage, "at": now, "from": old_stage, "reason": note_reason or None}
        )
        deal.setdefault("notes", []).append({"at": timestamp(), "note": audit_text})
        if args.status:
            if args.status not in VALID_STATUSES:
                raise ValueError(f"Invalid status {args.status!r}; expected one of {sorted(VALID_STATUSES)}")
            deal["status"] = args.status
        validate_deal(deal, config)
        holder["after"] = data
        holder["deal"] = deal
        return deal

    deal = mutate_kv("pipeline", _mutate, action="move_stage", config=config)
    suggestion = stage_move_suggestion(args.stage)
    if suggestion:
        print(suggestion, file=sys.stderr)
    return deal, "move_stage", holder.get("before"), holder.get("after")


def _list_summary(records: list[dict[str, Any]], config: Mapping[str, Any]) -> dict[str, Any]:
    terminals = terminal_stages(config)
    today_date = date.today()
    by_stage: dict[str, int] = {}
    active = 0
    stale_count = 0
    for deal in records:
        stage = str(deal.get("stage") or "(unknown)")
        by_stage[stage] = by_stage.get(stage, 0) + 1
        if stage not in terminals and str(deal.get("status") or "active") == "active":
            active += 1
        is_stale, _age = is_stale_deal(deal, config, today_date)
        if is_stale:
            stale_count += 1
    return {
        "total": len(records),
        "active": active,
        "by_stage": by_stage,
        "stale_count": stale_count,
    }


def cmd_list(args: argparse.Namespace) -> Any:
    config = configure(args.config)
    data = load_pipeline_store(config, strict=False)
    records = [normalize_deal(dict(d)) for d in data.get("deals", []) if isinstance(d, dict)]
    if args.stage:
        records = [d for d in records if d.get("stage") == args.stage]
    if args.status:
        records = [d for d in records if d.get("status") == args.status]
    if getattr(args, "stale", False):
        threshold = stale_threshold(config)
        today_date = date.today()
        filtered: list[dict[str, Any]] = []
        for deal in records:
            is_stale, age = is_stale_deal(deal, config, today_date)
            if is_stale:
                with_age = dict(deal)
                with_age["stale_days"] = age if age is not None else threshold + 1
                filtered.append(with_age)
        records = filtered
    records = sorted(records, key=lambda d: (str(d.get("stage", "")), str(d.get("client_name", "")), str(d.get("id", ""))))
    if getattr(args, "summary", False):
        return _list_summary(records, config)
    return records


def print_list_summary(summary: Mapping[str, Any], as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(dict(summary), indent=2, ensure_ascii=False, default=str))
        return
    print(f"total: {summary.get('total', 0)}")
    print(f"active: {summary.get('active', 0)}")
    print(f"stale_count: {summary.get('stale_count', 0)}")
    by_stage = summary.get("by_stage") or {}
    if isinstance(by_stage, dict) and by_stage:
        print("by_stage:")
        for stage, count in sorted(by_stage.items(), key=lambda kv: str(kv[0])):
            print(f"  {stage}: {count}")


def cmd_stale(args: argparse.Namespace) -> Any:
    config = configure(args.config)
    data = load_pipeline_store(config, strict=False)
    today_date = date.today()
    threshold = stale_threshold(config)
    rows: list[dict[str, Any]] = []
    for raw in data.get("deals", []) or []:
        if not isinstance(raw, dict):
            continue
        deal = normalize_deal(dict(raw))
        is_stale, age = is_stale_deal(deal, config, today_date)
        if not is_stale:
            continue
        days = age if age is not None else threshold + 1
        rows.append(
            {
                "id": deal.get("id"),
                "client_name": deal.get("client_name"),
                "stage": deal.get("stage"),
                "value": deal.get("value"),
                "currency": deal.get("currency"),
                "days_inactive": days,
                "last_activity": deal.get("last_activity"),
                "recommended_next_action": recommended_next_action(deal.get("stage")),
            }
        )
    rows = sorted(rows, key=lambda r: (-int(r.get("days_inactive") or 0), str(r.get("client_name") or "")))
    if getattr(args, "summary", False):
        return {"stale_count": len(rows), "threshold_days": threshold}
    return rows


def print_stale_rows(rows: list[dict[str, Any]], as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
        return
    if not rows:
        print("No stale deals.")
        return
    for row in rows:
        print(
            f"{row.get('id')}: {row.get('client_name')} — {row.get('stage')} — "
            f"{row.get('value')} {row.get('currency', '')} — "
            f"{row.get('days_inactive')}d inactive (last {row.get('last_activity')}) — "
            f"next: {row.get('recommended_next_action')}"
        )


def print_stale_summary(summary: Mapping[str, Any], as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(dict(summary), indent=2, ensure_ascii=False, default=str))
        return
    print(f"stale_count: {summary.get('stale_count', 0)}")
    if "threshold_days" in summary:
        print(f"threshold_days: {summary.get('threshold_days')}")


def cmd_validate(args: argparse.Namespace) -> int:
    config = configure(args.config)
    try:
        data = load_pipeline_store(config, strict=False)
    except StateStoreError as exc:
        print(f"ERROR: cannot load pipeline.yaml: {exc}")
        return 1

    findings: list[tuple[str, str]] = []
    stages = set(sales_stages(config))
    deals_raw = data.get("deals")
    if deals_raw is None:
        findings.append(("WARN", "pipeline.yaml has no 'deals' key"))
        deals = []
    elif not isinstance(deals_raw, list):
        findings.append(("ERROR", "pipeline.yaml 'deals' must be a list"))
        deals = []
    else:
        deals = deals_raw

    seen_ids: dict[str, int] = {}
    for idx, raw in enumerate(deals):
        label = f"deals[{idx}]"
        if not isinstance(raw, dict):
            findings.append(("ERROR", f"{label}: deal must be a mapping"))
            continue
        deal_id = raw.get("id")
        id_key = str(deal_id) if deal_id not in (None, "") else ""
        if not id_key:
            findings.append(("ERROR", f"{label}: missing required field id"))
        else:
            if id_key in seen_ids:
                findings.append(
                    ("ERROR", f"{label}: duplicate deal id {id_key!r} (first at deals[{seen_ids[id_key]}])")
                )
            else:
                seen_ids[id_key] = idx
            label = f"deal {id_key}"

        if raw.get("client_name") in (None, ""):
            findings.append(("ERROR", f"{label}: missing required field client_name"))
        if raw.get("stage") in (None, ""):
            findings.append(("ERROR", f"{label}: missing required field stage"))
        else:
            stage = str(raw.get("stage"))
            if stage not in stages:
                findings.append(("ERROR", f"{label}: invalid stage {stage!r}; expected one of {sorted(stages)}"))

        for field in ISO_DATE_FIELDS:
            if field not in raw or raw.get(field) in (None, ""):
                findings.append(("WARN", f"{label}: missing date field {field}"))
            elif not _is_iso_date(raw.get(field)):
                findings.append(("ERROR", f"{label}: bad date for {field}={raw.get(field)!r}; expected YYYY-MM-DD"))

        value = raw.get("value")
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                findings.append(("WARN", f"{label}: value is not a number ({value!r})"))
            elif value < 0:
                findings.append(("ERROR", f"{label}: negative value {value}"))

        status = raw.get("status")
        if status not in (None, "") and str(status) not in VALID_STATUSES:
            findings.append(("WARN", f"{label}: unexpected status {status!r}"))

        if not isinstance(raw.get("documents", []), list):
            findings.append(("WARN", f"{label}: documents should be a list"))
        if "notes" in raw and not isinstance(raw.get("notes"), (list, str)):
            findings.append(("WARN", f"{label}: notes should be a list (or legacy string)"))

    if not findings:
        findings.append(("INFO", f"pipeline OK: {len(seen_ids)} deal(s) checked"))

    has_error = False
    for level, message in findings:
        print(f"{level}: {message}")
        if level == "ERROR":
            has_error = True
    return 1 if has_error else 0


def cmd_add_note(args: argparse.Namespace) -> dict[str, Any]:
    config = configure(args.config)

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        deal = find_deal(data, args.id)
        note = {"at": timestamp(), "note": args.note}
        if getattr(args, "archival", False):
            note["archival"] = True
        deal.setdefault("notes", []).append(note)
        if not getattr(args, "archival", False):
            deal["last_activity"] = today()
        validate_deal(deal, config)
        return deal

    return mutate_kv("pipeline", _mutate, action="add_note", config=config)


def cmd_link_doc(args: argparse.Namespace) -> dict[str, Any]:
    config = configure(args.config)

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        deal = find_deal(data, args.id)
        path_value = str(args.path)
        if document_path_is_unfiled(path_value, str(deal.get("client_name") or "")):
            print("WARNING: unfiled_document_path", file=sys.stderr)
            print(
                f"WARNING: document path {path_value!r} is absolute or outside expected client folder structure",
                file=sys.stderr,
            )
        doc = {"type": args.type, "path": path_value, "status": args.status, "linked_at": timestamp()}
        deal.setdefault("documents", []).append(doc)
        deal["last_activity"] = today()
        validate_deal(deal, config)
        return deal

    return mutate_kv("pipeline", _mutate, action="link_doc", config=config)


def cmd_show(args: argparse.Namespace) -> dict[str, Any]:
    config = configure(args.config)
    data = load_pipeline_store(config, strict=False)
    return dict(find_deal(data, args.id))


def cmd_delete(_args: argparse.Namespace) -> int:
    print("Deal deletion is not supported. Move to 'Lost' or 'Cancelled' stage instead.")
    return 1


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
    move.add_argument("--note", default="", help="Reason / audit note for the stage move")

    ls = sub.add_parser("list", help="List deals")
    ls.add_argument("--stage")
    ls.add_argument("--status", choices=sorted(VALID_STATUSES))
    ls.add_argument("--stale", action="store_true")
    ls.add_argument("--summary", action="store_true", help="Print counts only (total, active, by stage, stale)")

    note = sub.add_parser("add-note", help="Append a note to a deal")
    note.add_argument("--id", required=True)
    note.add_argument("--note", required=True)
    note.add_argument(
        "--archival",
        action="store_true",
        help="Append note without updating last_activity",
    )
    # Alias name used in task description
    note_alias = sub.add_parser("note", help="Alias for add-note")
    note_alias.add_argument("--id", required=True)
    note_alias.add_argument("--note", required=True)
    note_alias.add_argument(
        "--archival",
        action="store_true",
        help="Append note without updating last_activity",
    )

    doc = sub.add_parser("link-doc", help="Link a document to a deal")
    doc.add_argument("--id", required=True)
    doc.add_argument("--type", required=True)
    doc.add_argument("--path", required=True)
    doc.add_argument("--status", required=True)

    show = sub.add_parser("show", help="Show a deal")
    show.add_argument("--id", required=True)

    stale = sub.add_parser("stale", help="List stale non-terminal deals")
    stale.add_argument("--summary", action="store_true", help="Print stale counts only")

    sub.add_parser("validate", help="Validate pipeline.yaml data quality")

    sub.add_parser("delete", help="Rejected: deal deletion is not supported")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "delete":
            return cmd_delete(args)
        if args.command == "validate":
            return cmd_validate(args)
        if args.command == "add":
            if args.currency is None:
                cfg = configure(args.config)
                args.currency = str(cfg.get("company", {}).get("currency", "")) or "UNSPECIFIED"
            result = cmd_add(args)[0]
            output(result, as_json=args.json)
            return 0
        if args.command == "move":
            result = cmd_move(args)[0]
            output(result, as_json=args.json)
            return 0
        if args.command == "list":
            result = cmd_list(args)
            if getattr(args, "summary", False):
                print_list_summary(result, as_json=args.json)
            else:
                output(result, as_json=args.json)
            return 0
        if args.command in ("add-note", "note"):
            result = cmd_add_note(args)
            output(result, as_json=args.json)
            return 0
        if args.command == "link-doc":
            result = cmd_link_doc(args)
            output(result, as_json=args.json)
            return 0
        if args.command == "show":
            result = cmd_show(args)
            output(result, as_json=args.json)
            return 0
        if args.command == "stale":
            result = cmd_stale(args)
            if getattr(args, "summary", False):
                print_stale_summary(result, as_json=args.json)
            else:
                print_stale_rows(result, as_json=args.json)
            return 0
        parser.error("unknown command")
        return 2
    except StateStoreError as exc:
        print(f"pipeline.py error: cannot parse or load pipeline store: {exc}", file=sys.stderr)
        return 1
    except KeyError as exc:
        print(str(exc).strip("'"), file=sys.stderr)
        return 1
    except (RuntimeError, ValueError, SchemaError) as exc:
        print(f"pipeline.py error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover — defensive last resort for corrupt data
        # Avoid crash on unexpected malformed content
        msg = str(exc)
        if "yaml" in msg.lower() or "parse" in msg.lower() or "mapping" in msg.lower():
            print(f"pipeline.py error: cannot parse pipeline.yaml: {exc}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
