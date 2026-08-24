#!/usr/bin/env python3
"""Core pipeline operations and execution of approved pipeline pending actions.

Handles:
  - load / save / find / validate deals in the pipeline KV store (SQLite)
  - stale deal detection for briefing / review
  - execution of approved ``pipeline.*`` pending actions

Safety:
  - Must only write the pipeline store after an approved pending action
  - Must use state_db.mutate_kv for the audit trail and CAS
  - Must NOT call any provider (Gmail/Drive/Calendar)
  - Must NOT delete deals
  - Must NOT write the invoices store
  - Stdlib only (shared modules live on sys.path)
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from state_db import (  # noqa: E402
    get_pending_action,
    mark_executed,
    mark_executing,
    mark_failed,
    mutate_kv,
)
from schemas import generate_id  # noqa: E402
from state_db import load_store  # noqa: E402

TERMINAL_STAGES = frozenset({"Paid", "Lost", "Cancelled"})
DEFAULT_STALE_THRESHOLD_DAYS = 14
SUPPORTED_ACTION_TYPES = frozenset(
    {
        "pipeline.deal.add",
        "pipeline.deal.move_stage",
        "pipeline.deal.add_note",
        "pipeline.deal.link_document",
    }
)
UNSUPPORTED_ACTION_TYPES = frozenset({"pipeline.deal.delete"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today() -> str:
    return date.today().isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sales_stages(config: Any) -> list[str]:
    if isinstance(config, Mapping):
        stages = config.get("sales_stages") or []
        if isinstance(stages, list) and all(isinstance(s, str) for s in stages):
            return list(stages)
    return []


def _stale_threshold(config: Any) -> int:
    if isinstance(config, Mapping):
        raw = config.get("stale_threshold_days")
        if raw is not None:
            try:
                return max(1, int(raw))
            except (TypeError, ValueError):
                pass
    return DEFAULT_STALE_THRESHOLD_DAYS


def _default_currency(config: Any) -> str:
    if isinstance(config, Mapping):
        company = config.get("company") or {}
        if isinstance(company, Mapping):
            ccy = str(company.get("currency") or "").strip().upper()
            if ccy:
                return ccy
    return "SGD"


def _norm_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _parse_iso_date(value: Any) -> date | None:
    """Parse ISO date or datetime; return date or None if unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # Prefer date-only YYYY-MM-DD; allow full ISO datetime prefix.
    try:
        if "T" in text:
            # Handle trailing Z
            clean = text.replace("Z", "+00:00")
            return datetime.fromisoformat(clean).date()
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _is_iso_date_like(value: Any) -> bool:
    return _parse_iso_date(value) is not None


def _parse_numeric(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError, AttributeError):
        return None


def _normalize_deal_lists(deal: dict[str, Any]) -> dict[str, Any]:
    """Ensure notes/documents/stage_history are list-shaped (in place)."""
    if not isinstance(deal.get("documents"), list):
        deal["documents"] = []
    notes = deal.get("notes")
    if not isinstance(notes, list):
        if notes in (None, ""):
            deal["notes"] = []
        else:
            deal["notes"] = [
                {"at": deal.get("last_activity") or _today(), "note": str(notes)}
            ]
    if not isinstance(deal.get("stage_history"), list):
        hist: list[dict[str, Any]] = []
        if deal.get("stage"):
            hist.append(
                {
                    "stage": deal.get("stage"),
                    "at": deal.get("created") or deal.get("last_activity") or _today(),
                }
            )
        deal["stage_history"] = hist
    return deal


def _recommended_action(stage: str, days_inactive: int) -> str:
    stage_l = (stage or "").strip().lower()
    if stage_l in {"lead"}:
        return "Follow up to qualify opportunity and schedule discovery"
    if "proposal" in stage_l:
        return "Follow up on outstanding proposal"
    if "nda" in stage_l:
        return "Check NDA status and nudge for signature if pending"
    if "contract" in stage_l:
        return "Follow up on contract review / signature"
    if "invoice" in stage_l:
        return "Confirm invoice delivery and payment timeline"
    if days_inactive > 30:
        return "Re-engage or mark Lost/Cancelled if no longer viable"
    return "Reach out to re-engage; update last_activity after contact"


# ---------------------------------------------------------------------------
# Public: load / save / find
# ---------------------------------------------------------------------------


def load_pipeline(config: Any) -> dict[str, Any]:
    """Load the pipeline KV store via state_db. Returns dict with ``deals`` list."""
    data = load_store("pipeline", config)
    if not isinstance(data, dict):
        return {"deals": []}
    if not isinstance(data.get("deals"), list):
        data["deals"] = []
    return data


def save_pipeline(
    config: Any,
    data: Mapping[str, Any],
    action: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> Path:
    """Atomically replace the pipeline KV store with audit trail (actor=agent).

    The write runs inside ``mutate_kv()`` so concurrent updates cannot
    last-writer-win outside a CAS transaction.
    """
    incoming = dict(data)

    def _mutate(current: dict[str, Any]) -> None:
        current.clear()
        current.update(incoming)
        if not isinstance(current.get("deals"), list):
            current["deals"] = []

    mutate_kv(
        "pipeline",
        _mutate,
        action=action,
        before=before,
        after=after,
        actor="agent",
        config=config,
    )
    from state_db import get_store_path
    return get_store_path("pipeline", config=config)


def find_deal_by_id(data: Mapping[str, Any], deal_id: str) -> dict[str, Any] | None:
    """Exact match on deal id. Returns the deal dict or None."""
    target = str(deal_id or "").strip()
    if not target:
        return None
    deals = data.get("deals", []) if isinstance(data, Mapping) else []
    if not isinstance(deals, list):
        return None
    for deal in deals:
        if isinstance(deal, dict) and str(deal.get("id") or "") == target:
            return deal
    return None


def find_deal_by_name(data: Mapping[str, Any], client_name: str) -> dict[str, Any] | None:
    """Find a deal by client_name.

    1. Case-insensitive exact match (unique preferred; first of multiples → None if >1)
    2. Else safe case-insensitive substring (partial) match
    3. Return None if no match or if match is ambiguous (multiple hits)
    """
    needle = _norm_name(client_name)
    if not needle:
        return None
    deals = data.get("deals", []) if isinstance(data, Mapping) else []
    if not isinstance(deals, list):
        return None

    candidates = [d for d in deals if isinstance(d, dict)]

    exact = [d for d in candidates if _norm_name(d.get("client_name")) == needle]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None  # ambiguous exact

    partial = [
        d
        for d in candidates
        if needle in _norm_name(d.get("client_name"))
        or _norm_name(d.get("client_name")) in needle
    ]
    if len(partial) == 1:
        return partial[0]
    return None  # none or ambiguous


# ---------------------------------------------------------------------------
# Public: validate_deal (soft report, does not raise)
# ---------------------------------------------------------------------------


def validate_deal(deal: Any, config: Any = None) -> dict[str, Any]:
    """Soft-validate a deal; returns validity report.

    Checks:
      id (required), client_name (required), stage (in sales_stages when configured),
      value (numeric, non-negative), currency (3-letter code), created / last_activity
      (ISO dates), and uniqueness of id when ``config`` holds enough context via
      a ``_pipeline_ids`` helper set or we skip uniqueness if no peers provided.

    Returns
    -------
    {"valid": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(deal, Mapping):
        return {"valid": False, "errors": ["deal must be a mapping"], "warnings": []}

    # id
    deal_id = deal.get("id")
    if deal_id is None or str(deal_id).strip() == "":
        errors.append("id is required")
    else:
        deal_id_s = str(deal_id).strip()
        # Optional uniqueness when peers provided as deal["_existing_ids"] or
        # config is a mapping with "_check_existing_ids".
        existing = None
        if isinstance(deal.get("_existing_ids"), (list, set, frozenset, tuple)):
            existing = {str(x) for x in deal["_existing_ids"]}
        elif isinstance(config, Mapping) and isinstance(
            config.get("_existing_ids"), (list, set, frozenset, tuple)
        ):
            existing = {str(x) for x in config["_existing_ids"]}
        if existing is not None and deal_id_s in existing:
            errors.append(f"id is not unique: {deal_id_s!r}")

    # client_name
    client_name = deal.get("client_name")
    if client_name is None or str(client_name).strip() == "":
        errors.append("client_name is required")

    # stage
    stage = deal.get("stage")
    if stage is None or str(stage).strip() == "":
        errors.append("stage is required")
    else:
        stages = _sales_stages(config)
        if stages and str(stage) not in stages:
            errors.append(
                f"stage {stage!r} not in configured sales_stages {stages!r}"
            )
        elif not stages:
            warnings.append("sales_stages not configured; stage membership not checked")

    # value
    if "value" in deal and deal.get("value") is not None and deal.get("value") != "":
        amount = _parse_numeric(deal.get("value"))
        if amount is None:
            errors.append(f"value is not numeric: {deal.get('value')!r}")
        elif amount < 0:
            errors.append(f"value must be non-negative: {deal.get('value')!r}")
    else:
        warnings.append("value is missing")

    # currency
    currency = deal.get("currency")
    if currency is None or str(currency).strip() == "":
        warnings.append("currency is missing")
    else:
        ccy = str(currency).strip()
        if len(ccy) != 3 or not ccy.isalpha():
            errors.append(f"currency must be a 3-letter code: {ccy!r}")
        elif ccy != ccy.upper():
            warnings.append(f"currency should be uppercase: {ccy!r}")

    # created
    created = deal.get("created")
    if created is None or str(created).strip() == "":
        warnings.append("created is missing")
    elif not _is_iso_date_like(created):
        errors.append(f"created is not an ISO date: {created!r}")

    # last_activity
    last_activity = deal.get("last_activity")
    if last_activity is None or str(last_activity).strip() == "":
        warnings.append("last_activity is missing")
    elif not _is_iso_date_like(last_activity):
        errors.append(f"last_activity is not an ISO date: {last_activity!r}")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Public: detect_stale_deals
# ---------------------------------------------------------------------------


def detect_stale_deals(config: Any, data: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return stale non-terminal deals.

    A deal is stale when today - last_activity > stale_threshold_days (default 14).
    Terminal stages Paid / Lost / Cancelled are excluded.

    Each entry:
      deal_id, client_name, stage, value, currency, days_inactive,
      last_activity, recommended_action
    """
    if data is None:
        data = load_pipeline(config)
    deals = data.get("deals", []) if isinstance(data, Mapping) else []
    if not isinstance(deals, list):
        deals = []

    threshold = _stale_threshold(config)
    today = date.today()
    stale: list[dict[str, Any]] = []

    for deal in deals:
        if not isinstance(deal, dict):
            continue
        stage = str(deal.get("stage") or "")
        if stage in TERMINAL_STAGES:
            continue
        # Also treat status fields won/lost/cancelled as terminal when present
        status = str(deal.get("status") or "").strip().lower()
        if status in {"lost", "cancelled", "won"} and stage in TERMINAL_STAGES | {
            "Paid"
        }:
            # Already handled by stage; keep multi-signal exclusion mild.
            pass
        if status in {"lost", "cancelled"}:
            continue

        last_raw = deal.get("last_activity")
        last_d = _parse_iso_date(last_raw)
        if last_d is None:
            # Unparseable activity date → treat as stale with unknown age
            days_inactive = threshold + 1
            last_activity_out = str(last_raw) if last_raw is not None else ""
        else:
            days_inactive = (today - last_d).days
            last_activity_out = last_d.isoformat()

        if days_inactive <= threshold:
            continue

        stale.append(
            {
                "deal_id": str(deal.get("id") or ""),
                "client_name": str(deal.get("client_name") or ""),
                "stage": stage,
                "value": deal.get("value"),
                "currency": deal.get("currency") or _default_currency(config),
                "days_inactive": days_inactive,
                "last_activity": last_activity_out,
                "recommended_action": _recommended_action(stage, days_inactive),
            }
        )

    stale.sort(
        key=lambda x: (-int(x.get("days_inactive") or 0), str(x.get("client_name") or ""))
    )
    return stale


# ---------------------------------------------------------------------------
# Execution helpers for individual action types
# ---------------------------------------------------------------------------


def _build_deal_from_payload(payload: Mapping[str, Any], config: Any) -> dict[str, Any]:
    """Construct a new deal record from a pending-action payload."""
    now = _today()
    stages = _sales_stages(config)
    stage = str(payload.get("stage") or (stages[0] if stages else "Lead")).strip()
    currency = str(
        payload.get("currency") or _default_currency(config)
    ).strip().upper()

    deal_id = str(payload.get("id") or payload.get("deal_id") or "").strip()
    if not deal_id:
        deal_id = generate_id("deal")

    value_raw = payload.get("value", 0)
    amount = _parse_numeric(value_raw)
    if amount is None:
        raise ValueError(f"Invalid value: {value_raw!r}")
    if amount < 0:
        raise ValueError(f"value must be non-negative: {value_raw!r}")
    # Prefer int when whole; else float for YAML friendliness
    if amount == amount.to_integral_value():
        value: Any = int(amount)
    else:
        value = float(amount)

    deal: dict[str, Any] = {
        "id": deal_id,
        "client_name": str(payload.get("client_name") or "").strip(),
        "contact_name": str(payload.get("contact_name") or "").strip(),
        "contact_email": str(payload.get("contact_email") or "").strip(),
        "stage": stage,
        "status": str(payload.get("status") or "active").strip() or "active",
        "value": value,
        "currency": currency,
        "created": str(payload.get("created") or now),
        "last_activity": str(payload.get("last_activity") or now),
        "stage_history": [{"stage": stage, "at": str(payload.get("created") or now)}],
        "documents": list(payload.get("documents") or [])
        if isinstance(payload.get("documents"), list)
        else [],
        "notes": list(payload.get("notes") or [])
        if isinstance(payload.get("notes"), list)
        else [],
    }

    # Optional extra fields pass-through (non-authoritative)
    for key in ("source", "tags", "owner"):
        if key in payload and payload[key] is not None:
            deal[key] = payload[key]

    report = validate_deal(deal, config)
    if not report["valid"]:
        raise ValueError("Deal validation failed: " + "; ".join(report["errors"]))
    return deal


def _exec_deal_add(config: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        deals = data.setdefault("deals", [])
        if not isinstance(deals, list):
            raise ValueError("pipeline store 'deals' must be a list")

        deal = _build_deal_from_payload(payload, config)
        existing_ids = {str(d.get("id")) for d in deals if isinstance(d, dict)}
        if deal["id"] in existing_ids:
            raise ValueError(f"Deal id already exists: {deal['id']}")

        deals.append(deal)
        return {
            "success": True,
            "action_type": "pipeline.deal.add",
            "deal_id": deal["id"],
            "client_name": deal.get("client_name"),
            "stage": deal.get("stage"),
        }

    return mutate_kv("pipeline", _mutate, config=config, action="add_deal")


def _exec_deal_move_stage(config: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    deal_id = str(payload.get("deal_id") or payload.get("id") or "").strip()
    new_stage = str(payload.get("stage") or payload.get("new_stage") or "").strip()
    if not deal_id:
        raise ValueError("payload.deal_id is required")
    if not new_stage:
        raise ValueError("payload.stage is required")

    stages = _sales_stages(config)
    if stages and new_stage not in stages:
        raise ValueError(
            f"Invalid stage {new_stage!r}; valid stages: {', '.join(stages)}"
        )

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        deal = find_deal_by_id(data, deal_id)
        if deal is None:
            raise ValueError(f"Deal not found: {deal_id}")
        _normalize_deal_lists(deal)

        old_stage = deal.get("stage")
        now = _today()
        note_text = str(payload.get("note") or payload.get("reason") or "").strip()
        if not note_text:
            note_text = f"Stage moved from {old_stage} to {new_stage}"

        deal["stage"] = new_stage
        deal["last_activity"] = now
        deal.setdefault("stage_history", []).append({"stage": new_stage, "at": now})
        deal.setdefault("notes", []).append(
            {
                "at": _now_iso(),
                "note": note_text,
                "kind": "stage_move",
                "from_stage": old_stage,
                "to_stage": new_stage,
            }
        )
        if payload.get("status"):
            deal["status"] = str(payload.get("status")).strip()

        report = validate_deal(deal, config)
        if not report["valid"]:
            raise ValueError("Deal validation failed: " + "; ".join(report["errors"]))

        return {
            "success": True,
            "action_type": "pipeline.deal.move_stage",
            "deal_id": deal_id,
            "from_stage": old_stage,
            "to_stage": new_stage,
        }

    return mutate_kv(
        "pipeline",
        _mutate,
        config=config,
        action="move_stage",
    )


def _exec_deal_add_note(config: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    deal_id = str(payload.get("deal_id") or payload.get("id") or "").strip()
    note_text = payload.get("note") or payload.get("text") or payload.get("content")
    if not deal_id:
        raise ValueError("payload.deal_id is required")
    if note_text is None or str(note_text).strip() == "":
        raise ValueError("payload.note is required")

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        deal = find_deal_by_id(data, deal_id)
        if deal is None:
            raise ValueError(f"Deal not found: {deal_id}")
        _normalize_deal_lists(deal)

        entry = {
            "at": _now_iso(),
            "note": str(note_text).strip(),
        }
        if payload.get("author"):
            entry["author"] = str(payload["author"])
        if payload.get("kind"):
            entry["kind"] = str(payload["kind"])

        deal.setdefault("notes", []).append(entry)
        if payload.get("touch_activity", True):
            deal["last_activity"] = _today()

        report = validate_deal(deal, config)
        if not report["valid"]:
            raise ValueError("Deal validation failed: " + "; ".join(report["errors"]))

        return {
            "success": True,
            "action_type": "pipeline.deal.add_note",
            "deal_id": deal_id,
            "note": entry,
        }

    return mutate_kv("pipeline", _mutate, config=config, action="add_note")


def _exec_deal_link_document(config: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    deal_id = str(payload.get("deal_id") or payload.get("id") or "").strip()
    if not deal_id:
        raise ValueError("payload.deal_id is required")

    doc_path = str(
        payload.get("path")
        or payload.get("document_path")
        or payload.get("file_path")
        or ""
    ).strip()
    doc_type = str(payload.get("type") or payload.get("document_type") or "document").strip()
    doc_status = str(payload.get("status") or "linked").strip()

    if not doc_path:
        # Allow nested document object
        nested = payload.get("document")
        if isinstance(nested, Mapping):
            doc_path = str(
                nested.get("path") or nested.get("document_path") or ""
            ).strip()
            doc_type = str(nested.get("type") or doc_type).strip()
            doc_status = str(nested.get("status") or doc_status).strip()
    if not doc_path:
        raise ValueError("payload.path (document path) is required")

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        deal = find_deal_by_id(data, deal_id)
        if deal is None:
            raise ValueError(f"Deal not found: {deal_id}")
        _normalize_deal_lists(deal)

        doc: dict[str, Any] = {
            "type": doc_type,
            "path": doc_path,
            "status": doc_status,
            "linked_at": _now_iso(),
        }
        if payload.get("name"):
            doc["name"] = str(payload["name"])

        deal.setdefault("documents", []).append(doc)
        if payload.get("touch_activity", True):
            deal["last_activity"] = _today()

        report = validate_deal(deal, config)
        if not report["valid"]:
            raise ValueError("Deal validation failed: " + "; ".join(report["errors"]))

        return {
            "success": True,
            "action_type": "pipeline.deal.link_document",
            "deal_id": deal_id,
            "document": doc,
        }

    return mutate_kv("pipeline", _mutate, config=config, action="link_doc")


# ---------------------------------------------------------------------------
# Public: execute_pipeline_action
# ---------------------------------------------------------------------------


def execute_pipeline_action(config: Any, action_id: str) -> dict[str, Any]:
    """Execute an approved ``pipeline.*`` pending action.

    Steps:
      1. Load pending action; require type starts with ``pipeline.`` and state approved
      2. mark_executing
      3. Route to add / move_stage / add_note / link_document handlers
      4. mark_executed or mark_failed

    ``pipeline.deal.delete`` is intentionally unsupported.

    Returns
    -------
    {"success": True, "action_type": ..., "deal_id": ...}
    or {"success": False, "error": ...}
    """
    action = get_pending_action(config, action_id)
    if not action:
        return {"success": False, "error": f"Action not found: {action_id}"}

    action_type = str(action.get("type") or "")
    if not action_type.startswith("pipeline."):
        return {
            "success": False,
            "error": (
                f"Wrong action type: expected pipeline.* prefix, got {action_type!r}"
            ),
        }

    if action_type in UNSUPPORTED_ACTION_TYPES or action_type == "pipeline.deal.delete":
        err = "Deal deletion is not supported"
        # Only mark_failed if currently approved/executing so we leave a trail
        state_preview = str(action.get("state") or "")
        if state_preview in {"approved", "executing"}:
            try:
                if state_preview == "approved":
                    mark_executing(config, action_id)
                mark_failed(config, action_id, err)
            except Exception:
                pass
        return {"success": False, "error": err, "action_type": action_type}

    state = str(action.get("state") or "")
    if state == "approved":
        executing = mark_executing(config, action_id)
        if not executing:
            return {
                "success": False,
                "error": "Cannot execute (approval may have lapsed or concurrent update)",
                "action_type": action_type,
            }
        action = executing
    elif state != "executing":
        return {
            "success": False,
            "error": f"Action not approved (state={state})",
            "action_type": action_type,
        }

    raw_payload = action.get("payload") if isinstance(action, dict) else None
    payload: dict[str, Any] = dict(raw_payload) if isinstance(raw_payload, dict) else {}

    try:
        if action_type == "pipeline.deal.add":
            result = _exec_deal_add(config, payload)
        elif action_type == "pipeline.deal.move_stage":
            result = _exec_deal_move_stage(config, payload)
        elif action_type == "pipeline.deal.add_note":
            result = _exec_deal_add_note(config, payload)
        elif action_type == "pipeline.deal.link_document":
            result = _exec_deal_link_document(config, payload)
        else:
            raise ValueError(f"Unsupported pipeline action type: {action_type!r}")

        mark_executed(config, action_id, result)
        return result
    except Exception as exc:
        err = str(exc)
        mark_failed(config, action_id, err)
        return {
            "success": False,
            "error": err,
            "action_type": action_type,
        }


__all__ = [
    "TERMINAL_STAGES",
    "detect_stale_deals",
    "execute_pipeline_action",
    "find_deal_by_id",
    "find_deal_by_name",
    "load_pipeline",
    "save_pipeline",
    "validate_deal",
]
