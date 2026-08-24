#!/usr/bin/env python3
"""Execute approved bookkeeper.invoice.record pending actions.

Called by the execution router (webhook_events.py) after an operator
approves a proposed invoice candidate. Re-validates and re-checks
duplicates before any write to the invoices KV store (SQLite).

Safety:
  - Only appends invoices after re-validation and re-duplicate-check
  - Refuses when a near-duplicate is found (score >= 0.95) unless
    payload has override_duplicate=true
  - Uses state_db.mutate_kv for the audit trail and CAS
  - Never calls providers, never deletes/updates existing invoices,
    never marks invoices paid
  - Money compared with Decimal (amounts stored as strings in payload /
    candidates; cast to number only for the invoices store schema)

Only stdlib imports (shared modules live on sys.path).
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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
from schemas import generate_id, validate_invoice  # noqa: E402

CANDIDATES_FILENAME = ".bookkeeper_invoice_candidates.json"
DUPLICATE_REFUSE_THRESHOLD = 0.95
ACTION_TYPE = "bookkeeper.invoice.record"

REQUIRED_FIELDS = (
    "direction",
    "counterparty",
    "amount",
    "currency",
    "issue_date",
    "due_date",
)

# Weighted contributions used by check_duplicate (sum to 1.0).
_DUP_WEIGHTS = {
    "invoice_number": 0.35,
    "counterparty": 0.25,
    "amount": 0.20,
    "currency": 0.10,
    "issue_date": 0.10,
}


# ---------------------------------------------------------------------------
# Path / money helpers
# ---------------------------------------------------------------------------


def _project_root(config: Any) -> Path:
    if isinstance(config, Mapping):
        paths = config.get("paths", {})
        if isinstance(paths, Mapping) and paths.get("project_root"):
            return Path(str(paths["project_root"])).expanduser().resolve()
    env = os.getenv("CHIEF_OF_STAFF_PROJECT_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    raise RuntimeError("Cannot resolve project_root from config or CHIEF_OF_STAFF_PROJECT_ROOT")


def _candidates_path(config: Any) -> Path:
    return _project_root(config) / CANDIDATES_FILENAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _money(value: Any) -> Decimal | None:
    """Parse a money value to Decimal(0.01). Returns None if unparseable."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).strip().replace(",", "")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, ValueError, AttributeError):
        return None


def _money_str(value: Any) -> str | None:
    m = _money(value)
    return str(m) if m is not None else None


def _norm_str(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _norm_invoice_number(value: Any) -> str:
    raw = str(value or "").strip().upper()
    return re.sub(r"[^A-Z0-9]", "", raw)


# ---------------------------------------------------------------------------
# Candidate store (JSON, optimistic version)
# ---------------------------------------------------------------------------


def _load_candidates(config: Any) -> dict[str, Any]:
    path = _candidates_path(config)
    if not path.exists():
        return {"candidates": {}, "_version": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"candidates": {}, "_version": 0}
    if not isinstance(data, dict):
        return {"candidates": {}, "_version": 0}
    if not isinstance(data.get("candidates"), dict):
        data["candidates"] = {}
    if "_version" not in data:
        data["_version"] = 0
    return data


def _save_candidates(config: Any, data: dict[str, Any], expected_version: int | None = None) -> int:
    path = _candidates_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    if expected_version is not None:
        current = _load_candidates(config)
        if current.get("_version", 0) != expected_version:
            raise RuntimeError(
                f"Candidates store changed since load (expected v{expected_version}, "
                f"found v{current.get('_version', 0)}). Reload and retry."
            )
    new_version = (data.get("_version", 0) or 0) + 1
    data["_version"] = new_version
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)
    return new_version


def _get_candidate(config: Any, candidate_id: str) -> dict[str, Any] | None:
    data = _load_candidates(config)
    cand = data.get("candidates", {}).get(candidate_id)
    return dict(cand) if isinstance(cand, dict) else None


def _mark_candidate_recorded(
    config: Any,
    candidate_id: str,
    invoice_id: str,
) -> dict[str, Any] | None:
    data = _load_candidates(config)
    expected = data.get("_version", 0)
    cand = data.get("candidates", {}).get(candidate_id)
    if not isinstance(cand, dict):
        return None
    cand["state"] = "recorded"
    cand["recorded_at"] = _now()
    cand["recorded_invoice_id"] = invoice_id
    data["candidates"][candidate_id] = cand
    _save_candidates(config, data, expected_version=expected)
    return cand


# ---------------------------------------------------------------------------
# Public: validate_candidate / check_duplicate
# ---------------------------------------------------------------------------


def validate_candidate(candidate: Mapping[str, Any], invoices_data: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate a bookkeeper invoice candidate (required fields).

    Returns
    -------
    dict with keys:
      valid: bool
      warnings: list[str]
      validation_status: "valid" | "needs_review" | "invalid"
    """
    warnings: list[str] = []
    # Accept candidate itself or its proposed_invoice for field source.
    source: Mapping[str, Any]
    proposed = candidate.get("proposed_invoice") if isinstance(candidate, Mapping) else None
    if isinstance(proposed, Mapping) and proposed:
        source = proposed
    elif isinstance(candidate, Mapping):
        source = candidate
    else:
        return {
            "valid": False,
            "warnings": ["candidate is not a mapping"],
            "validation_status": "invalid",
        }

    missing = [f for f in REQUIRED_FIELDS if source.get(f) in (None, "")]
    if missing:
        return {
            "valid": False,
            "warnings": [f"missing required field: {f}" for f in missing],
            "validation_status": "invalid",
        }

    direction = str(source.get("direction", "")).strip().lower()
    if direction not in {"sent", "received"}:
        warnings.append(f"direction must be 'sent' or 'received' (got {direction!r})")
        return {"valid": False, "warnings": warnings, "validation_status": "invalid"}

    if _money(source.get("amount")) is None:
        warnings.append(f"amount is not a valid money value: {source.get('amount')!r}")
        return {"valid": False, "warnings": warnings, "validation_status": "invalid"}

    currency = str(source.get("currency") or "").strip()
    if not currency:
        warnings.append("currency is empty")
        return {"valid": False, "warnings": warnings, "validation_status": "invalid"}
    if len(currency) != 3 or not currency.isalpha():
        warnings.append(f"currency looks non-standard: {currency!r}")

    # Soft date format checks (YYYY-MM-DD preferred)
    for field in ("issue_date", "due_date"):
        val = str(source.get(field) or "")
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", val):
            warnings.append(f"{field} is not YYYY-MM-DD: {val!r}")

    # Optional: surface pre-existing candidate warnings
    existing_warnings = candidate.get("warnings") if isinstance(candidate, Mapping) else None
    if isinstance(existing_warnings, list):
        for w in existing_warnings:
            if w and str(w) not in warnings:
                warnings.append(str(w))

    # Optional duplicate hints from prior scoring using live invoices
    if invoices_data is not None:
        dups = check_duplicate(dict(source), invoices_data)
        high = [d for d in dups if float(d.get("score", 0)) >= 0.80]
        if high:
            top = high[0]
            warnings.append(
                f"possible duplicate of {top.get('invoice_id')} (score={top.get('score')})"
            )

    confidence = candidate.get("confidence") if isinstance(candidate, Mapping) else None
    try:
        conf = float(confidence) if confidence is not None else 1.0
    except (TypeError, ValueError):
        conf = 0.0
        warnings.append(f"unparseable confidence: {confidence!r}")

    if conf < 0.7:
        warnings.append(f"low extraction confidence: {conf}")

    if warnings:
        return {"valid": True, "warnings": warnings, "validation_status": "needs_review"}
    return {"valid": True, "warnings": [], "validation_status": "valid"}


def check_duplicate(proposed_invoice: Mapping[str, Any], invoices_data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Score proposed invoice against existing invoices.yaml records.

    Scoring dimensions (weighted):
      invoice_number, counterparty, amount, currency, issue_date

    Returns a list of matches sorted by score desc:
      [{"invoice_id": ..., "score": 0.0-1.0, "reasons": [...]}, ...]
    Entries with score == 0 are omitted.
    """
    invoices = invoices_data.get("invoices", []) if isinstance(invoices_data, Mapping) else []
    if not isinstance(invoices, list):
        invoices = []

    prop_number = _norm_invoice_number(
        proposed_invoice.get("invoice_number") or proposed_invoice.get("number")
    )
    prop_counterparty = _norm_str(proposed_invoice.get("counterparty"))
    prop_amount = _money(proposed_invoice.get("amount"))
    prop_currency = _norm_str(proposed_invoice.get("currency")).upper()
    prop_issue = str(proposed_invoice.get("issue_date") or "").strip()

    matches: list[dict[str, Any]] = []
    for inv in invoices:
        if not isinstance(inv, dict):
            continue
        score = 0.0
        reasons: list[str] = []

        inv_number = _norm_invoice_number(inv.get("invoice_number") or inv.get("number"))
        if prop_number and inv_number and prop_number == inv_number:
            score += _DUP_WEIGHTS["invoice_number"]
            reasons.append("invoice_number match")

        inv_cp = _norm_str(inv.get("counterparty"))
        if prop_counterparty and inv_cp and prop_counterparty == inv_cp:
            score += _DUP_WEIGHTS["counterparty"]
            reasons.append("counterparty match")
        elif prop_counterparty and inv_cp and (
            prop_counterparty in inv_cp or inv_cp in prop_counterparty
        ):
            partial = _DUP_WEIGHTS["counterparty"] * 0.5
            score += partial
            reasons.append("counterparty partial match")

        inv_amount = _money(inv.get("amount"))
        if prop_amount is not None and inv_amount is not None and prop_amount == inv_amount:
            score += _DUP_WEIGHTS["amount"]
            reasons.append("amount match")

        inv_currency = _norm_str(inv.get("currency")).upper()
        if prop_currency and inv_currency and prop_currency == inv_currency:
            score += _DUP_WEIGHTS["currency"]
            reasons.append("currency match")

        inv_issue = str(inv.get("issue_date") or "").strip()
        if prop_issue and inv_issue and prop_issue == inv_issue:
            score += _DUP_WEIGHTS["issue_date"]
            reasons.append("issue_date match")

        # Cap and normalise floating point noise
        score = min(1.0, round(score, 4))
        if score <= 0:
            continue

        matches.append(
            {
                "invoice_id": str(inv.get("id") or ""),
                "score": score,
                "reasons": reasons,
            }
        )

    matches.sort(key=lambda m: (-float(m["score"]), str(m.get("invoice_id") or "")))
    return matches


# ---------------------------------------------------------------------------
# Public: execute_invoice_record
# ---------------------------------------------------------------------------


def _normalize_proposed_invoice(proposed: Mapping[str, Any], config: Any) -> dict[str, Any]:
    """Build a schema-valid invoice dict ready for invoices.yaml append."""
    inv = dict(proposed)

    if not inv.get("id"):
        inv["id"] = generate_id("INV")

    direction = str(inv.get("direction") or "").strip().lower()
    inv["direction"] = direction

    # Schema requires a non-negative number; keep display precision via quantize.
    amount = _money(inv.get("amount"))
    if amount is None:
        raise ValueError(f"Invalid amount: {inv.get('amount')!r}")
    # Prefer float for YAML friendliness (matches invoices.py convention).
    inv["amount"] = float(amount)

    currency = str(inv.get("currency") or "").strip().upper()
    if not currency:
        if isinstance(config, Mapping):
            currency = str(
                (config.get("company") or {}).get("currency") or "UNSPECIFIED"
            ).upper()
        else:
            currency = "UNSPECIFIED"
    inv["currency"] = currency

    inv["counterparty"] = str(inv.get("counterparty") or "").strip()
    inv["issue_date"] = str(inv.get("issue_date") or "").strip()
    inv["due_date"] = str(inv.get("due_date") or "").strip()

    if not inv.get("status"):
        inv["status"] = "sent" if direction == "sent" else "received"

    if "paid_date" not in inv:
        inv["paid_date"] = None
    if "notes" not in inv:
        inv["notes"] = ""

    # Keep amount also as string for callers that prefer string money, but
    # invoices store remains numeric for schema validation.
    return inv


class _DuplicateInvoiceRefused(Exception):
    """Raised inside mutate_kv when a likely duplicate should refuse the write."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(result.get("error") or "duplicate_likely")
        self.result = result


def execute_invoice_record(config: Any, action_id: str) -> dict[str, Any]:
    """Execute an approved bookkeeper.invoice.record pending action.

    Steps:
      1. Load pending action; require type + approved (or already executing)
      2. mark_executing if still approved
      3. Re-load candidate, re-validate, re-check duplicates
      4. Refuse on likely duplicate unless override_duplicate
      5. Append proposed_invoice via mutate_kv(action=add_invoice)
      6. Mark candidate recorded; mark_executed with result

    Returns a result dict with at least:
      success, invoice_id, candidate_id  (and error/refused on failure)
    """
    action = get_pending_action(config, action_id)
    if not action:
        return {
            "success": False,
            "error": f"Action not found: {action_id}",
            "invoice_id": None,
            "candidate_id": None,
        }

    action_type = str(action.get("type") or "")
    if action_type != ACTION_TYPE:
        return {
            "success": False,
            "error": f"Wrong action type: expected {ACTION_TYPE!r}, got {action_type!r}",
            "invoice_id": None,
            "candidate_id": None,
        }

    state = str(action.get("state") or "")
    if state == "approved":
        executing = mark_executing(config, action_id)
        if not executing:
            return {
                "success": False,
                "error": "Cannot execute (approval may have lapsed or concurrent update)",
                "invoice_id": None,
                "candidate_id": None,
            }
        action = executing
    elif state != "executing":
        return {
            "success": False,
            "error": f"Action not approved (state={state})",
            "invoice_id": None,
            "candidate_id": None,
        }

    raw_payload = action.get("payload") if isinstance(action, dict) else None
    payload: dict[str, Any] = dict(raw_payload) if isinstance(raw_payload, dict) else {}
    candidate_id = str(
        payload.get("candidate_id")
        or payload.get("id")
        or ""
    ).strip()
    override_duplicate = bool(payload.get("override_duplicate", False))

    if not candidate_id:
        err = "payload.candidate_id is required"
        mark_failed(config, action_id, err)
        return {
            "success": False,
            "error": err,
            "invoice_id": None,
            "candidate_id": None,
        }

    try:
        candidate = _get_candidate(config, candidate_id)
        if not candidate:
            raise ValueError(f"Candidate not found: {candidate_id}")

        cand_state = str(candidate.get("state") or "")
        if cand_state == "recorded":
            raise ValueError(f"Candidate already recorded: {candidate_id}")

        proposed_raw = candidate.get("proposed_invoice")
        if not isinstance(proposed_raw, dict) or not proposed_raw:
            # Fall back to payload.proposed_invoice when candidate is sparse
            proposed_raw = payload.get("proposed_invoice") if isinstance(payload.get("proposed_invoice"), dict) else None
        if not isinstance(proposed_raw, dict) or not proposed_raw:
            raise ValueError(f"Candidate {candidate_id} has no proposed_invoice")

        inv = _normalize_proposed_invoice(proposed_raw, config)
        # Final schema gate before write
        validate_invoice(inv)

        def _mutate(invoices_data: dict[str, Any]) -> dict[str, Any]:
            validation = validate_candidate(candidate, invoices_data)
            if not validation.get("valid") or validation.get("validation_status") == "invalid":
                raise ValueError(
                    "Re-validation failed: "
                    + "; ".join(validation.get("warnings") or ["invalid candidate"])
                )

            dups = check_duplicate(proposed_raw, invoices_data)
            likely = [d for d in dups if float(d.get("score", 0)) >= DUPLICATE_REFUSE_THRESHOLD]
            if likely and not override_duplicate:
                top = likely[0]
                err = (
                    f"duplicate_likely: matches {top.get('invoice_id')} "
                    f"(score={top.get('score')}, reasons={top.get('reasons')}). "
                    "Refuse unless payload.override_duplicate=true"
                )
                raise _DuplicateInvoiceRefused({
                    "success": False,
                    "refused": True,
                    "reason": "duplicate_likely",
                    "error": err,
                    "invoice_id": None,
                    "candidate_id": candidate_id,
                    "duplicate_candidates": likely,
                })

            invoices_list = invoices_data.setdefault("invoices", [])
            if not isinstance(invoices_list, list):
                raise ValueError("invoices store 'invoices' must be a list")

            # Guard again with Domain true-duplicate (cp + amount + issue_date)
            # even if score-based path was overridden/missed intermediate scores.
            invoices_list.append(inv)
            return {
                "success": True,
                "invoice_id": str(inv["id"]),
                "candidate_id": candidate_id,
                "validation_status": validation.get("validation_status"),
                "warnings": validation.get("warnings") or [],
                "duplicate_candidates": dups[:5],
                "override_duplicate": override_duplicate,
                "amount": _money_str(inv.get("amount")),
                "currency": inv.get("currency"),
                "counterparty": inv.get("counterparty"),
                "direction": inv.get("direction"),
            }

        try:
            result = mutate_kv(
                "invoices",
                _mutate,
                config=config,
                action="add_invoice",
            )
        except _DuplicateInvoiceRefused as refused:
            mark_failed(config, action_id, refused.result.get("error") or "duplicate_likely")
            return refused.result

        _mark_candidate_recorded(config, candidate_id, str(inv["id"]))
        mark_executed(config, action_id, result)
        return result

    except Exception as exc:
        err = str(exc)
        mark_failed(config, action_id, err)
        return {
            "success": False,
            "error": err,
            "invoice_id": None,
            "candidate_id": candidate_id or None,
        }


__all__ = [
    "ACTION_TYPE",
    "check_duplicate",
    "execute_invoice_record",
    "validate_candidate",
]
