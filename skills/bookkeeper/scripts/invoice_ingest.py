#!/usr/bin/env python3
"""Invoice ingestion for Chief-of-Staff bookkeeper.

Detects invoice-like material from local events / email classifications /
suggested actions, extracts structured candidates, validates them, checks
duplicates, and routes ready candidates through the pending-action review
queue.

This module does NOT write invoices.yaml. Recording happens only after an
operator approves a pending action of type bookkeeper.invoice.record.

CLI:
  invoice_ingest.py scan --since 24h --summary
  invoice_ingest.py scan --dry-run
  invoice_ingest.py extract --source-id <id>
  invoice_ingest.py candidates --summary
  invoice_ingest.py preview --candidate-id <id>
  invoice_ingest.py prepare --candidate-id <id>
  invoice_ingest.py validate
  invoice_ingest.py dismiss --candidate-id <id> --reason "..."
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

try:
    from config_loader import load_config  # type: ignore
    from event_store import get_event, list_events  # type: ignore
    from pending_actions import create_pending_action  # type: ignore
    from schemas import generate_id  # type: ignore
    from state_store import load_store  # type: ignore
except Exception as exc:  # pragma: no cover
    print(
        f"Chief-of-Staff bootstrap incomplete: cannot import shared scripts from {SHARED_SCRIPTS}: {exc}. "
        "Run the plugin bootstrap/foundation setup first.",
        file=sys.stderr,
    )
    raise SystemExit(2)

# Optional imports — degrade gracefully if modules missing.
try:
    from suggested_actions import list_suggestions  # type: ignore
except Exception:  # pragma: no cover
    def list_suggestions(config: Any, **kwargs: Any) -> list[dict[str, Any]]:  # type: ignore
        return []

CANDIDATE_STORE_NAME = ".bookkeeper_invoice_candidates.json"
ID_PREFIX = "bic_"

SUPPORTED_CURRENCIES = {
    "SGD", "USD", "EUR", "GBP", "AUD", "NZD", "CAD", "JPY", "CNY", "HKD",
    "MYR", "THB", "IDR", "INR", "PHP", "CHF", "KRW", "TWD", "VND",
}

INVOICE_KEYWORDS = (
    "invoice", "bill", "receipt", "payment", "amount", "sgd", "usd",
    "due date", "due_date", "counterparty", "tax invoice", "purchase order",
    "accounts payable", "accounts receivable", "a/p", "a/r", "invoice #",
    "inv-", "bill-",
)

# Weight contributions for duplicate scoring (max ~1.0)
DUP_WEIGHTS = {
    "invoice_number": 0.40,
    "counterparty": 0.25,
    "amount_currency": 0.25,
    "issue_date": 0.10,
}

REQUIRED_FIELDS = ("direction", "counterparty", "amount", "currency", "issue_date", "due_date")

# ---------------------------------------------------------------------------
# Project root / paths
# ---------------------------------------------------------------------------


def _get_default_project_root_fallback() -> Path:
    env = os.getenv("CHIEF_OF_STAFF_HERMES_HOME") or os.getenv("HERMES_HOME")
    home = Path(env).expanduser() if env else Path.home() / ".hermes"
    return home / "projects" / "default"


def _project_root(config: Any) -> Path:
    root = None
    if isinstance(config, Mapping):
        paths = config.get("paths", {})
        if isinstance(paths, Mapping):
            root = paths.get("project_root")
    if not root:
        root = os.getenv("CHIEF_OF_STAFF_PROJECT_ROOT",
                         str(_get_default_project_root_fallback()))
    return Path(str(root)).expanduser()


def _candidates_path(config: Any) -> Path:
    return _project_root(config) / CANDIDATE_STORE_NAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def configure(path: str | None) -> dict[str, Any]:
    if path:
        os.environ["CHIEF_OF_STAFF_CONFIG"] = path
    cfg = load_config(path)
    if cfg is None:
        raise RuntimeError(
            "Could not load company.yaml; pass --config or set CHIEF_OF_STAFF_CONFIG"
        )
    return cfg  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Money / dates
# ---------------------------------------------------------------------------


def money(value: Any) -> Decimal:
    """Parse a money value to Decimal(2dp). Raises ValueError if invalid."""
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid amount: {value!r}") from exc


def money_str(value: Any) -> str | None:
    """Return money as a fixed 2dp string, or None if unparseable."""
    if value in (None, ""):
        return None
    try:
        return str(money(value))
    except ValueError:
        return None


def parse_date(value: Any) -> str | None:
    """Normalize a date-ish value to ISO YYYY-MM-DD or None."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = str(value).strip()
    if not text:
        return None
    # Already ISO date
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    if m:
        try:
            datetime.strptime(m.group(1), "%Y-%m-%d")
            return m.group(1)
        except ValueError:
            return None
    # ISO datetime
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    # Common alt formats
    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Candidate store
# ---------------------------------------------------------------------------


def _load_candidates(config: Any) -> dict[str, Any]:
    path = _candidates_path(config)
    if not path.exists():
        return {"candidates": {}, "_version": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "candidates" not in data:
            return {"candidates": {}, "_version": 0}
        if not isinstance(data["candidates"], dict):
            data["candidates"] = {}
        if "_version" not in data:
            data["_version"] = 0
        return data
    except (json.JSONDecodeError, OSError):
        return {"candidates": {}, "_version": 0}


def _save_candidates(config: Any, data: dict[str, Any]) -> int:
    path = _candidates_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_version = (data.get("_version", 0) or 0) + 1
    data["_version"] = new_version
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    tmp.replace(path)
    return new_version


def _next_candidate_id(store: dict[str, Any]) -> str:
    """Generate next sequential bic_NNN id."""
    max_n = 0
    for cid in store.get("candidates", {}):
        m = re.match(r"^bic_(\d+)$", str(cid))
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"{ID_PREFIX}{max_n + 1:03d}"


def _find_by_source(
    store: dict[str, Any],
    source_id: str,
    source_type: str | None = None,
) -> dict[str, Any] | None:
    for cand in store.get("candidates", {}).values():
        if not isinstance(cand, dict):
            continue
        if str(cand.get("source_id")) == str(source_id):
            if source_type is None or str(cand.get("source_type")) == source_type:
                # Prefer active (non-dismissed) candidates
                if cand.get("state") != "dismissed":
                    return cand
    return None


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


_AMOUNT_RE = re.compile(
    r"(?:"
    r"(?:(?:USD|SGD|EUR|GBP|AUD|MYR|HKD|JPY|CNY)\s*)?"
    r"[\$€£]?\s*"
    r"(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"(?:\s*(?:USD|SGD|EUR|GBP|AUD|MYR|HKD|JPY|CNY))?"
    r")",
    re.IGNORECASE,
)

_CURRENCY_RE = re.compile(
    r"\b(SGD|USD|EUR|GBP|AUD|NZD|CAD|JPY|CNY|HKD|MYR|THB|IDR|INR|PHP|CHF|KRW|TWD|VND)\b",
    re.IGNORECASE,
)

_INVOICE_NUM_RE = re.compile(
    r"(?:"
    r"(?:invoice|inv|bill|receipt)\s*(?:#|no\.?|number|num)?\s*[:\s]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9\-/_.]{2,30})"
    r"|"
    r"\b((?:INV|BILL|RCP|INVOICE)[-_]?\d{3,})\b"
    r")",
    re.IGNORECASE,
)

_DATE_LABEL_RE = re.compile(
    r"(?P<label>issue(?:\s*date)?|invoice\s*date|dated|due(?:\s*date)?|"
    r"payment\s*due|due\s*by)\s*[:\s]+\s*"
    r"(?P<date>\d{4}-\d{2}-\d{2}|\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|"
    r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})",
    re.IGNORECASE,
)

_TA_X_RE = re.compile(
    r"(?:tax|gst|vat)\s*(?:amount)?\s*[:\s]+\s*"
    r"[\$€£]?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)

_TOTAL_RE = re.compile(
    r"(?:total|amount\s*due|grand\s*total|balance\s*due)\s*[:\s]+\s*"
    r"[\$€£]?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)

_COUNTERPARTY_RE = re.compile(
    r"(?:"
    r"(?:from|to|vendor|supplier|client|customer|bill\s*to|sold\s*to|pay\s*to)\s*[:\s]+"
    r"([A-Z][A-Za-z0-9 &.,'\-]{2,60})"
    r")",
)


def _flatten_text(payload: Any) -> str:
    """Flatten a payload (dict/list/str) into searchable text."""
    parts: list[str] = []

    def walk(obj: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if obj is None:
            return
        if isinstance(obj, str):
            parts.append(obj)
        elif isinstance(obj, (int, float, Decimal)):
            parts.append(str(obj))
        elif isinstance(obj, Mapping):
            for k, v in obj.items():
                parts.append(str(k))
                walk(v, depth + 1)
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                walk(item, depth + 1)
        else:
            parts.append(str(obj))

    walk(payload)
    return "\n".join(parts)


def _looks_invoice_like(text: str, payload: Mapping[str, Any] | None = None) -> bool:
    lower = text.lower()
    score = 0
    for kw in INVOICE_KEYWORDS:
        if kw in lower:
            score += 1
    if payload:
        # Structured clues
        for key in ("invoice_number", "invoice_id", "amount", "due_date",
                    "counterparty", "currency", "direction", "tax_amount"):
            if payload.get(key) not in (None, ""):
                score += 2
        cat = str(payload.get("category") or payload.get("classification") or "").lower()
        if "invoice" in cat or cat == "finance_invoice":
            score += 3
    return score >= 2


def _extract_from_text(text: str, structured: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Extract invoice fields from free text + optional structured dict."""
    structured = dict(structured or {})
    extracted: dict[str, Any] = {
        "direction": "unknown",
        "counterparty": None,
        "invoice_number": None,
        "amount": None,
        "currency": None,
        "issue_date": None,
        "due_date": None,
        "description": None,
        "tax_amount": None,
        "total_amount": None,
    }

    # Prefer structured fields when present
    for key in extracted:
        if structured.get(key) not in (None, ""):
            extracted[key] = structured.get(key)

    # Direction from structured
    direction = str(structured.get("direction") or "").strip().lower()
    if direction in ("sent", "received"):
        extracted["direction"] = direction
    else:
        # Infer from text cues
        lower = text.lower()
        if any(p in lower for p in ("we invoice", "invoice to", "invoice sent",
                                    "please find our invoice", "our invoice",
                                    "bill to client", "accounts receivable")):
            extracted["direction"] = "sent"
        elif any(p in lower for p in ("invoice from", "bill from", "vendor invoice",
                                      "your invoice", "accounts payable",
                                      "payment request", "please pay", "amount due")):
            extracted["direction"] = "received"

    # Counterparty
    if not extracted["counterparty"]:
        for key in ("counterparty", "vendor", "supplier", "client", "from",
                    "sender", "company", "payee", "payer"):
            val = structured.get(key)
            if isinstance(val, str) and val.strip():
                extracted["counterparty"] = val.strip()
                break
        if not extracted["counterparty"]:
            m = _COUNTERPARTY_RE.search(text)
            if m:
                extracted["counterparty"] = m.group(1).strip().rstrip(".,;")

    # Invoice number
    if not extracted["invoice_number"]:
        for key in ("invoice_number", "invoice_id", "invoice_no", "number", "inv_number"):
            val = structured.get(key)
            if val not in (None, ""):
                extracted["invoice_number"] = str(val).strip()
                break
        if not extracted["invoice_number"]:
            m = _INVOICE_NUM_RE.search(text)
            if m:
                extracted["invoice_number"] = (m.group(1) or m.group(2) or "").strip()

    # Currency
    if not extracted["currency"]:
        cur = structured.get("currency")
        if cur:
            extracted["currency"] = str(cur).upper().strip()
        else:
            m = _CURRENCY_RE.search(text)
            if m:
                extracted["currency"] = m.group(1).upper()

    # Amounts
    amount_ambiguous = False
    for key in ("amount", "total", "total_amount", "subtotal"):
        if structured.get(key) not in (None, ""):
            ms = money_str(structured.get(key))
            if ms is not None:
                if key in ("total", "total_amount"):
                    extracted["total_amount"] = ms
                    if not extracted["amount"]:
                        extracted["amount"] = ms
                else:
                    extracted["amount"] = ms
    if not extracted["amount"]:
        amounts: list[str] = []
        for m in _AMOUNT_RE.finditer(text):
            raw = m.group(1).replace(",", "")
            ms = money_str(raw)
            if ms is not None:
                try:
                    if money(ms) > 0:
                        amounts.append(ms)
                except ValueError:
                    pass
        # Prefer total-pattern match
        tm = _TOTAL_RE.search(text)
        if tm:
            ms = money_str(tm.group(1).replace(",", ""))
            if ms is not None:
                extracted["total_amount"] = ms
                extracted["amount"] = ms
        if not extracted["amount"] and amounts:
            # Take the largest plausible amount (common for invoice totals)
            best = max(amounts, key=lambda a: money(a))
            extracted["amount"] = best
            if len(set(amounts)) > 1:
                amount_ambiguous = True

    # Tax
    if not extracted["tax_amount"]:
        if structured.get("tax_amount") not in (None, ""):
            extracted["tax_amount"] = money_str(structured.get("tax_amount"))
        else:
            tm = _TA_X_RE.search(text)
            if tm:
                extracted["tax_amount"] = money_str(tm.group(1).replace(",", ""))

    # Dates
    issue_date = parse_date(structured.get("issue_date") or structured.get("date")
                            or structured.get("invoice_date"))
    due_date = parse_date(structured.get("due_date") or structured.get("payment_due"))
    if not issue_date or not due_date:
        for m in _DATE_LABEL_RE.finditer(text):
            label = m.group("label").lower()
            d = parse_date(m.group("date"))
            if not d:
                continue
            if "due" in label:
                if not due_date:
                    due_date = d
            else:
                if not issue_date:
                    issue_date = d
    # Bare ISO dates as fallback for issue date
    if not issue_date:
        bare = re.findall(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
        if bare:
            issue_date = parse_date(bare[0])
    extracted["issue_date"] = issue_date
    extracted["due_date"] = due_date

    # Description
    if not extracted["description"]:
        desc = structured.get("description") or structured.get("subject") or structured.get("summary")
        if isinstance(desc, str) and desc.strip():
            extracted["description"] = desc.strip()[:500]
        else:
            # First non-empty line containing invoice-ish words
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                if any(k in line.lower() for k in ("invoice", "bill", "receipt", "payment")):
                    extracted["description"] = line[:500]
                    break

    # Normalize money fields to strings
    for mkey in ("amount", "tax_amount", "total_amount"):
        if extracted[mkey] is not None:
            extracted[mkey] = money_str(extracted[mkey])

    # Normalize currency
    if extracted["currency"]:
        extracted["currency"] = str(extracted["currency"]).upper().strip()

    # Counterparty as string
    if extracted["counterparty"] is not None:
        extracted["counterparty"] = str(extracted["counterparty"]).strip() or None

    # Default currency from config hearts — left for caller
    extracted["_amount_ambiguous"] = amount_ambiguous
    return extracted


def _default_currency(config: Any) -> str | None:
    if isinstance(config, Mapping):
        company = config.get("company", {})
        if isinstance(company, Mapping):
            cur = company.get("currency")
            if cur:
                return str(cur).upper().strip()
    return None


# ---------------------------------------------------------------------------
# Validation & duplicates
# ---------------------------------------------------------------------------


def validate_candidate_extracted(extracted: Mapping[str, Any],
                                 document_path: str | None = None,
                                 amount_ambiguous: bool = False) -> tuple[str, list[str]]:
    """Return (validation_status, warnings)."""
    warnings: list[str] = []
    missing_required = False

    direction = str(extracted.get("direction") or "unknown")
    if direction not in ("sent", "received"):
        missing_required = True
        warnings.append("direction_uncertain")

    if not extracted.get("counterparty"):
        missing_required = True
        warnings.append("missing_counterparty")

    amount = extracted.get("amount")
    if amount in (None, ""):
        missing_required = True
    else:
        try:
            money(amount)
        except ValueError:
            missing_required = True
            warnings.append("amount_ambiguous")

    currency = extracted.get("currency")
    if not currency:
        missing_required = True
        warnings.append("missing_currency")
    else:
        cur = str(currency).upper()
        if cur not in SUPPORTED_CURRENCIES:
            warnings.append("unsupported_currency")

    if not extracted.get("issue_date"):
        missing_required = True
        warnings.append("missing_issue_date")

    if not extracted.get("due_date"):
        missing_required = True
        warnings.append("missing_due_date")

    if not extracted.get("invoice_number"):
        warnings.append("missing_invoice_number")

    if amount_ambiguous or extracted.get("_amount_ambiguous"):
        if "amount_ambiguous" not in warnings:
            warnings.append("amount_ambiguous")

    # Tax / total consistency
    try:
        base = extracted.get("amount")
        tax = extracted.get("tax_amount")
        total = extracted.get("total_amount")
        if base not in (None, "") and tax not in (None, "") and total not in (None, ""):
            if money(base) + money(tax) != money(total):
                # Also accept total == base (tax included style)
                if money(total) != money(base):
                    warnings.append("tax_total_mismatch")
    except ValueError:
        warnings.append("amount_ambiguous")

    if not document_path:
        # optional soft warning only — not always available for event sources
        pass

    # Deduplicate warnings while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            ordered.append(w)

    if missing_required:
        status = "invalid"
    elif ordered:
        status = "needs_review"
    else:
        status = "valid"
    return status, ordered


def _norm_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _score_duplicate(candidate_extracted: Mapping[str, Any],
                     other: Mapping[str, Any]) -> tuple[float, list[str]]:
    """Score similarity of candidate vs an existing invoice or candidate."""
    score = 0.0
    reasons: list[str] = []

    inv_a = str(candidate_extracted.get("invoice_number") or "").strip().lower()
    # Other may be an invoice dict or another candidate's extracted/proposed
    inv_b = str(
        other.get("invoice_number")
        or (other.get("extracted") or {}).get("invoice_number")
        or (other.get("proposed_invoice") or {}).get("invoice_number")
        or (other.get("notes") and "")
        or ""
    ).strip().lower()
    # invoices.yaml doesn't have invoice_number field — use id tip if INV-ish notes
    if not inv_b and other.get("id"):
        # Don't match on generated ids alone
        inv_b = ""
    if inv_a and inv_b and inv_a == inv_b:
        score += DUP_WEIGHTS["invoice_number"]
        reasons.append("invoice_number_match")

    name_a = _norm_name(candidate_extracted.get("counterparty"))
    name_b = _norm_name(
        other.get("counterparty")
        or (other.get("extracted") or {}).get("counterparty")
        or (other.get("proposed_invoice") or {}).get("counterparty")
    )
    if name_a and name_b and name_a == name_b:
        score += DUP_WEIGHTS["counterparty"]
        reasons.append("counterparty_match")

    try:
        amt_a = money(candidate_extracted.get("amount")) if candidate_extracted.get("amount") not in (None, "") else None
    except ValueError:
        amt_a = None
    other_amount = (
        other.get("amount")
        if other.get("amount") not in (None, "")
        else (other.get("extracted") or {}).get("amount")
        if isinstance(other.get("extracted"), Mapping)
        else (other.get("proposed_invoice") or {}).get("amount")
        if isinstance(other.get("proposed_invoice"), Mapping)
        else None
    )
    try:
        amt_b = money(other_amount) if other_amount not in (None, "") else None
    except ValueError:
        amt_b = None
    cur_a = str(candidate_extracted.get("currency") or "").upper()
    cur_b = str(
        other.get("currency")
        or (other.get("extracted") or {}).get("currency")
        or (other.get("proposed_invoice") or {}).get("currency")
        or ""
    ).upper()
    if amt_a is not None and amt_b is not None and amt_a == amt_b and cur_a and cur_a == cur_b:
        score += DUP_WEIGHTS["amount_currency"]
        reasons.append("amount_currency_match")

    date_a = parse_date(candidate_extracted.get("issue_date"))
    date_b = parse_date(
        other.get("issue_date")
        or (other.get("extracted") or {}).get("issue_date")
        or (other.get("proposed_invoice") or {}).get("issue_date")
    )
    if date_a and date_b and date_a == date_b:
        score += DUP_WEIGHTS["issue_date"]
        reasons.append("issue_date_match")

    return round(score, 2), reasons


def find_duplicates(
    config: Any,
    extracted: Mapping[str, Any],
    store: dict[str, Any],
    exclude_id: str | None = None,
) -> list[dict[str, Any]]:
    """Compare against invoices.yaml + candidate store. Return scored matches."""
    hits: list[dict[str, Any]] = []

    # Existing invoices
    try:
        inv_data = load_store("invoices", config)
        for inv in inv_data.get("invoices", []) or []:
            if not isinstance(inv, dict):
                continue
            score, reasons = _score_duplicate(extracted, inv)
            if score >= 0.50 and reasons:
                hits.append({
                    "invoice_id": inv.get("id"),
                    "score": score,
                    "reasons": reasons,
                    "source": "invoices.yaml",
                })
    except Exception:
        pass  # store may be unavailable in bare environments

    # Other candidates
    for cid, cand in (store.get("candidates") or {}).items():
        if exclude_id and cid == exclude_id:
            continue
        if not isinstance(cand, dict):
            continue
        if cand.get("state") == "dismissed":
            continue
        # Build a comparison object from the candidate
        other = {
            "id": cid,
            "counterparty": (cand.get("extracted") or {}).get("counterparty"),
            "amount": (cand.get("extracted") or {}).get("amount"),
            "currency": (cand.get("extracted") or {}).get("currency"),
            "issue_date": (cand.get("extracted") or {}).get("issue_date"),
            "invoice_number": (cand.get("extracted") or {}).get("invoice_number"),
            "extracted": cand.get("extracted"),
            "proposed_invoice": cand.get("proposed_invoice"),
        }
        score, reasons = _score_duplicate(extracted, other)
        if score >= 0.50 and reasons:
            hits.append({
                "invoice_id": f"candidate:{cid}",
                "score": score,
                "reasons": reasons,
                "source": "candidates",
            })

    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits


def _apply_duplicate_warnings(
    warnings: list[str],
    duplicates: list[dict[str, Any]],
) -> list[str]:
    if not duplicates:
        return warnings
    top = duplicates[0]["score"]
    out = list(warnings)
    if top >= 0.95:
        if "duplicate_likely" not in out:
            out.append("duplicate_likely")
        if "duplicate_possible" not in out:
            out.append("duplicate_possible")
    elif top >= 0.85:
        if "duplicate_possible" not in out:
            out.append("duplicate_possible")
    return out


def build_proposed_invoice(
    config: Any,
    extracted: Mapping[str, Any],
    document_path: str | None = None,
) -> dict[str, Any]:
    """Map extracted fields to invoices.yaml schema shape (status draft/received)."""
    direction = extracted.get("direction")
    if direction not in ("sent", "received"):
        direction = "received"  # safer default for proposed — still fails validation if unknown upstream

    amount_s = money_str(extracted.get("amount")) or "0.00"
    # invoices.yaml historically stores amount as number; proposed keeps string-safe
    # conversion for schema validate_invoice which expects a number.
    try:
        amount_num: Any = float(money(amount_s))
    except ValueError:
        amount_num = 0.0

    currency = str(extracted.get("currency") or _default_currency(config) or "SGD").upper()
    status = "sent" if direction == "sent" else "received"
    notes_parts = []
    if extracted.get("invoice_number"):
        notes_parts.append(f"invoice_number={extracted['invoice_number']}")
    if extracted.get("description"):
        notes_parts.append(str(extracted["description"])[:200])

    proposed = {
        "id": generate_id("INV"),
        "direction": direction,
        "counterparty": extracted.get("counterparty") or "",
        "deal_id": None,
        "amount": amount_num,
        "currency": currency,
        "issue_date": extracted.get("issue_date"),
        "due_date": extracted.get("due_date"),
        "status": status,
        "paid_date": None,
        "document_path": document_path,
        "notes": "; ".join(notes_parts),
    }
    return proposed


def _confidence_from(extracted: Mapping[str, Any], warnings: list[str], keyword_hits: int) -> float:
    base = 0.35
    if extracted.get("direction") in ("sent", "received"):
        base += 0.15
    if extracted.get("counterparty"):
        base += 0.10
    if extracted.get("amount"):
        base += 0.15
    if extracted.get("currency"):
        base += 0.05
    if extracted.get("issue_date"):
        base += 0.05
    if extracted.get("due_date"):
        base += 0.05
    if extracted.get("invoice_number"):
        base += 0.10
    base += min(0.10, keyword_hits * 0.02)
    base -= min(0.30, 0.05 * len(warnings))
    return max(0.0, min(1.0, round(base, 2)))


# ---------------------------------------------------------------------------
# Candidate factory
# ---------------------------------------------------------------------------


def _build_candidate(
    config: Any,
    store: dict[str, Any],
    *,
    source_type: str,
    source_id: str,
    source_path: str | None,
    document_path: str | None,
    text: str,
    structured: Mapping[str, Any] | None,
) -> dict[str, Any]:
    extracted = _extract_from_text(text, structured)
    if not extracted.get("currency"):
        extracted["currency"] = _default_currency(config)

    amount_ambiguous = bool(extracted.pop("_amount_ambiguous", False))
    clean_extracted = {k: v for k, v in extracted.items() if not k.startswith("_")}

    status, warnings = validate_candidate_extracted(
        clean_extracted,
        document_path=document_path,
        amount_ambiguous=amount_ambiguous,
    )
    duplicates = find_duplicates(config, clean_extracted, store)
    warnings = _apply_duplicate_warnings(warnings, duplicates)
    # Recompute status after duplicate warnings are applied
    required_missing = (
        clean_extracted.get("direction") not in ("sent", "received")
        or not clean_extracted.get("counterparty")
        or not clean_extracted.get("amount")
        or not clean_extracted.get("currency")
        or not clean_extracted.get("issue_date")
        or not clean_extracted.get("due_date")
    )
    if required_missing:
        status = "invalid"
    elif warnings:
        status = "needs_review"
    else:
        status = "valid"

    proposed = build_proposed_invoice(config, clean_extracted, document_path)
    keyword_hits = sum(1 for kw in INVOICE_KEYWORDS if kw in text.lower())
    conf = _confidence_from(clean_extracted, warnings, keyword_hits)
    now = _now()
    cid = _next_candidate_id(store)

    return {
        "id": cid,
        "state": "candidate",
        "source_type": source_type,
        "source_id": source_id,
        "source_path": source_path,
        "document_path": document_path,
        "extracted": clean_extracted,
        "proposed_invoice": proposed,
        "confidence": conf,
        "warnings": warnings,
        "validation_status": status,
        "duplicate_candidates": duplicates,
        "created_at": now,
        "updated_at": now,
        "dismiss_reason": None,
        "pending_action_id": None,
    }


# ---------------------------------------------------------------------------
# Source collectors
# ---------------------------------------------------------------------------


def _parse_since(since: str | None) -> datetime | None:
    if not since:
        return None
    text = since.strip().lower()
    m = re.match(r"^(\d+)\s*h(ours?)?$", text)
    if m:
        return datetime.now(timezone.utc) - timedelta(hours=int(m.group(1)))
    m = re.match(r"^(\d+)\s*d(ays?)?$", text)
    if m:
        return datetime.now(timezone.utc) - timedelta(days=int(m.group(1)))
    m = re.match(r"^(\d+)\s*m(in(utes?)?)?$", text)
    if m:
        return datetime.now(timezone.utc) - timedelta(minutes=int(m.group(1)))
    # ISO
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"Invalid --since value: {since!r} (use e.g. 24h, 7d, or ISO datetime)")


def _event_timestamp(event: Mapping[str, Any]) -> datetime | None:
    for key in ("created_at", "received_at", "classified_at"):
        raw = event.get(key)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


def _load_email_classifications(config: Any) -> list[dict[str, Any]]:
    path = _project_root(config) / ".email_organisation_classifications.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get("items", {}) if isinstance(data, dict) else {}
        if isinstance(items, dict):
            return [v for v in items.values() if isinstance(v, dict)]
        if isinstance(items, list):
            return [v for v in items if isinstance(v, dict)]
    except (json.JSONDecodeError, OSError):
        pass
    return []


def collect_sources(config: Any, since: str | None = None) -> list[dict[str, Any]]:
    """Collect invoice-like sources from events, email classifications, suggestions."""
    cutoff = _parse_since(since)
    sources: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # Events
    try:
        events = list_events(config, limit=200)
    except Exception:
        events = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if cutoff:
            ts = _event_timestamp(event)
            if ts is not None and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts is not None and ts < cutoff:
                continue
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        payload_m = dict(payload) if payload else {}
        # Merge classification category if present
        classification = event.get("classification")
        if isinstance(classification, Mapping):
            payload_m.setdefault("category", classification.get("category"))
        text = _flatten_text({"event_type": event.get("event_type"), "summary": event.get("summary"),
                              "payload": payload_m})
        if not _looks_invoice_like(text, payload_m):
            continue
        sid = str(event.get("id") or event.get("source_id") or "")
        if not sid or sid in seen_ids:
            continue
        seen_ids.add(sid)
        doc_path = (
            payload_m.get("document_path")
            or payload_m.get("file_path")
            or payload_m.get("attachment_path")
        )
        sources.append({
            "source_type": "event",
            "source_id": sid,
            "source_path": None,
            "document_path": str(doc_path) if doc_path else None,
            "text": text,
            "structured": payload_m,
        })

    # Email classifications (finance_invoice)
    for cls in _load_email_classifications(config):
        cat = str(cls.get("category") or "").lower()
        conf = float(cls.get("confidence") or 0)
        text = _flatten_text({
            "subject": cls.get("subject"),
            "snippet": cls.get("snippet"),
            "from": cls.get("from"),
            "category": cat,
            "reason": cls.get("classification_reason"),
        })
        is_finance = cat in ("finance_invoice", "finance", "finance_receipt") or "invoice" in cat
        if not is_finance and not _looks_invoice_like(text, cls):
            continue
        if conf < 0.4 and not is_finance:
            continue
        if cutoff and cls.get("created_at"):
            try:
                ts = datetime.fromisoformat(str(cls["created_at"]).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    continue
            except ValueError:
                pass
        sid = str(cls.get("message_id") or cls.get("id") or "")
        if not sid or sid in seen_ids:
            continue
        seen_ids.add(sid)
        structured = {
            "counterparty": cls.get("from"),
            "description": cls.get("subject") or cls.get("snippet"),
            "category": cat,
            "direction": "received",  # inbound email invoice often AP
        }
        sources.append({
            "source_type": "email",
            "source_id": sid,
            "source_path": None,
            "document_path": None,
            "text": text,
            "structured": structured,
        })

    # Suggested actions that mention invoices
    try:
        suggestions = list_suggestions(config, limit=100)
    except Exception:
        suggestions = []
    for sug in suggestions:
        if not isinstance(sug, dict):
            continue
        if sug.get("state") not in (None, "suggested", "acted_on"):
            continue
        text = _flatten_text({
            "title": sug.get("title"),
            "reason": sug.get("reason"),
            "action_type": sug.get("action_type"),
            "payload": sug.get("payload"),
        })
        if not _looks_invoice_like(text, sug.get("payload") if isinstance(sug.get("payload"), Mapping) else None):
            continue
        sid = str(sug.get("id") or "")
        if not sid or sid in seen_ids:
            continue
        seen_ids.add(sid)
        payload = sug.get("payload") if isinstance(sug.get("payload"), Mapping) else {}
        sources.append({
            "source_type": "event",
            "source_id": sid,
            "source_path": None,
            "document_path": payload.get("document_path") if isinstance(payload, Mapping) else None,
            "text": text,
            "structured": dict(payload) if isinstance(payload, Mapping) else {},
        })

    return sources


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_scan(args: argparse.Namespace) -> dict[str, Any]:
    cfg = configure(args.config)
    store = _load_candidates(cfg)
    sources = collect_sources(cfg, since=args.since)
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for src in sources:
        existing = _find_by_source(store, src["source_id"], src.get("source_type"))
        if existing:
            skipped.append({
                "source_id": src["source_id"],
                "reason": "already_have_candidate",
                "candidate_id": existing.get("id"),
            })
            continue
        cand = _build_candidate(
            cfg,
            store,
            source_type=src["source_type"],
            source_id=src["source_id"],
            source_path=src.get("source_path"),
            document_path=src.get("document_path"),
            text=src["text"],
            structured=src.get("structured"),
        )
        # Reserve id in store so sequential ids stay unique when multiple created
        store.setdefault("candidates", {})[cand["id"]] = cand
        created.append(cand)

    if not args.dry_run and created:
        _save_candidates(cfg, store)

    summary = {
        "scanned_sources": len(sources),
        "created": len(created),
        "skipped": len(skipped),
        "dry_run": bool(args.dry_run),
        "since": args.since,
        "by_validation": {},
        "by_state": {},
        "candidates": [{"id": c["id"], "source_id": c["source_id"],
                        "validation_status": c["validation_status"],
                        "confidence": c["confidence"],
                        "counterparty": (c.get("extracted") or {}).get("counterparty"),
                        "amount": (c.get("extracted") or {}).get("amount"),
                        "currency": (c.get("extracted") or {}).get("currency")}
                       for c in created],
        "skipped_detail": skipped if args.dry_run or args.summary else skipped[:20],
    }
    for c in created:
        vs = c.get("validation_status", "unknown")
        summary["by_validation"][vs] = summary["by_validation"].get(vs, 0) + 1
        st = c.get("state", "unknown")
        summary["by_state"][st] = summary["by_state"].get(st, 0) + 1

    if args.summary:
        print("📥 Invoice ingest — scan")
        print()
        print(f"Sources scanned: {summary['scanned_sources']}")
        print(f"Candidates created: {summary['created']}"
              + (" (dry-run, not written)" if args.dry_run else ""))
        print(f"Skipped (already present): {summary['skipped']}")
        if summary["by_validation"]:
            print("By validation:")
            for k, v in sorted(summary["by_validation"].items()):
                print(f"  {k}: {v}")
        if created:
            print()
            print("New candidates:")
            for c in created:
                ext = c.get("extracted") or {}
                print(
                    f"  {c['id']}: {ext.get('counterparty') or '?'} "
                    f"{ext.get('amount') or '?'} {ext.get('currency') or ''} "
                    f"[{c.get('validation_status')}] conf={c.get('confidence')}"
                )
        return summary

    _emit(summary)
    return summary


def cmd_extract(args: argparse.Namespace) -> dict[str, Any]:
    cfg = configure(args.config)
    store = _load_candidates(cfg)
    source_id = args.source_id

    # Try event first
    event = None
    try:
        event = get_event(cfg, source_id)
    except Exception:
        event = None

    structured: dict[str, Any] = {}
    text = ""
    source_type = "event"
    document_path = None

    if event:
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        structured = dict(payload or {})
        classification = event.get("classification")
        if isinstance(classification, Mapping):
            structured.setdefault("category", classification.get("category"))
        text = _flatten_text({
            "event_type": event.get("event_type"),
            "summary": event.get("summary"),
            "payload": structured,
        })
        document_path = (structured.get("document_path") or structured.get("file_path")
                         or structured.get("attachment_path"))
        if document_path:
            document_path = str(document_path)
    else:
        # Email classifications
        for cls in _load_email_classifications(cfg):
            if str(cls.get("message_id")) == source_id or str(cls.get("id")) == source_id:
                source_type = "email"
                text = _flatten_text({
                    "subject": cls.get("subject"),
                    "snippet": cls.get("snippet"),
                    "from": cls.get("from"),
                    "category": cls.get("category"),
                })
                structured = {
                    "counterparty": cls.get("from"),
                    "description": cls.get("subject") or cls.get("snippet"),
                    "category": cls.get("category"),
                    "direction": "received",
                }
                break
        if not text:
            # Suggestions
            try:
                for sug in list_suggestions(cfg, limit=200):
                    if str(sug.get("id")) == source_id:
                        text = _flatten_text({
                            "title": sug.get("title"),
                            "reason": sug.get("reason"),
                            "payload": sug.get("payload"),
                        })
                        structured = dict(sug.get("payload") or {}) if isinstance(sug.get("payload"), Mapping) else {}
                        source_type = "event"
                        break
            except Exception:
                pass

    if not text and not structured:
        raise KeyError(f"Source not found: {source_id}")

    existing = _find_by_source(store, source_id)
    if existing and not getattr(args, "force", False):
        # Refresh extraction on the existing candidate
        cand = existing
        new_cand = _build_candidate(
            cfg, store,
            source_type=source_type,
            source_id=source_id,
            source_path=None,
            document_path=document_path,
            text=text,
            structured=structured,
        )
        # Keep original id/created_at/state if not dismissed
        new_cand["id"] = cand["id"]
        new_cand["created_at"] = cand.get("created_at") or new_cand["created_at"]
        if cand.get("state") in ("prepared", "recorded"):
            new_cand["state"] = cand["state"]
            new_cand["pending_action_id"] = cand.get("pending_action_id")
        store["candidates"][new_cand["id"]] = new_cand
        _save_candidates(cfg, store)
        _emit(new_cand)
        return new_cand

    cand = _build_candidate(
        cfg, store,
        source_type=source_type,
        source_id=source_id,
        source_path=None,
        document_path=document_path,
        text=text,
        structured=structured,
    )
    store.setdefault("candidates", {})[cand["id"]] = cand
    _save_candidates(cfg, store)
    _emit(cand)
    return cand


def cmd_candidates(args: argparse.Namespace) -> dict[str, Any]:
    cfg = configure(args.config)
    store = _load_candidates(cfg)
    cands = list(store.get("candidates", {}).values())
    by_state: dict[str, int] = {}
    by_validation: dict[str, int] = {}
    for c in cands:
        if not isinstance(c, dict):
            continue
        st = str(c.get("state") or "unknown")
        by_state[st] = by_state.get(st, 0) + 1
        vs = str(c.get("validation_status") or "unknown")
        by_validation[vs] = by_validation.get(vs, 0) + 1

    summary = {
        "total": len(cands),
        "by_state": by_state,
        "by_validation": by_validation,
        "store_version": store.get("_version", 0),
        "candidates": [
            {
                "id": c.get("id"),
                "state": c.get("state"),
                "validation_status": c.get("validation_status"),
                "source_type": c.get("source_type"),
                "source_id": c.get("source_id"),
                "confidence": c.get("confidence"),
                "counterparty": (c.get("extracted") or {}).get("counterparty"),
                "amount": (c.get("extracted") or {}).get("amount"),
                "currency": (c.get("extracted") or {}).get("currency"),
                "warnings": c.get("warnings") or [],
            }
            for c in sorted(cands, key=lambda x: str(x.get("created_at") or ""), reverse=True)
            if isinstance(c, dict)
        ],
    }

    if args.summary:
        print("📋 Invoice candidates")
        print()
        print(f"Total: {summary['total']}  (store v{summary['store_version']})")
        if by_state:
            print("By state:")
            for k, v in sorted(by_state.items()):
                print(f"  {k}: {v}")
        if by_validation:
            print("By validation:")
            for k, v in sorted(by_validation.items()):
                print(f"  {k}: {v}")
        print()
        active = [c for c in summary["candidates"] if c.get("state") not in ("dismissed", "recorded")]
        if not active:
            print("No active candidates.")
        else:
            for c in active:
                print(
                    f"  {c['id']} [{c.get('state')}/{c.get('validation_status')}] "
                    f"{c.get('counterparty') or '?'} "
                    f"{c.get('amount') or '?'} {c.get('currency') or ''} "
                    f"conf={c.get('confidence')} src={c.get('source_type')}:{c.get('source_id')}"
                )
                if c.get("warnings"):
                    print(f"    warnings: {', '.join(c['warnings'])}")
        return summary

    _emit(summary)
    return summary


def cmd_preview(args: argparse.Namespace) -> dict[str, Any]:
    cfg = configure(args.config)
    store = _load_candidates(cfg)
    cand = store.get("candidates", {}).get(args.candidate_id)
    if not cand:
        raise KeyError(f"Candidate not found: {args.candidate_id}")
    _emit(cand)
    return cand


def cmd_prepare(args: argparse.Namespace) -> dict[str, Any]:
    """Validate, duplicate-check, create pending action. Does NOT write invoices.yaml."""
    cfg = configure(args.config)
    store = _load_candidates(cfg)
    cand = store.get("candidates", {}).get(args.candidate_id)
    if not cand:
        raise KeyError(f"Candidate not found: {args.candidate_id}")

    if cand.get("state") == "dismissed":
        raise ValueError(f"Candidate {args.candidate_id} is dismissed")
    if cand.get("state") == "recorded":
        raise ValueError(f"Candidate {args.candidate_id} is already recorded")

    extracted = dict(cand.get("extracted") or {})
    # Refresh validation + duplicates
    status, warnings = validate_candidate_extracted(
        extracted,
        document_path=cand.get("document_path"),
        amount_ambiguous=False,
    )
    duplicates = find_duplicates(cfg, extracted, store, exclude_id=cand["id"])
    warnings = _apply_duplicate_warnings(warnings, duplicates)

    required_missing = (
        extracted.get("direction") not in ("sent", "received")
        or not extracted.get("counterparty")
        or not extracted.get("amount")
        or not extracted.get("currency")
        or not extracted.get("issue_date")
        or not extracted.get("due_date")
    )
    if required_missing:
        status = "invalid"
    elif warnings:
        status = "needs_review"
    else:
        status = "valid"

    cand["warnings"] = warnings
    cand["duplicate_candidates"] = duplicates
    cand["validation_status"] = status
    cand["proposed_invoice"] = build_proposed_invoice(
        cfg, extracted, document_path=cand.get("document_path")
    )
    cand["updated_at"] = _now()

    # Block prepare on invalid
    if status == "invalid" and not getattr(args, "force", False):
        store["candidates"][cand["id"]] = cand
        _save_candidates(cfg, store)
        raise ValueError(
            f"Candidate {cand['id']} is invalid (missing required fields): "
            f"{', '.join(warnings)}. Fix fields or re-extract before prepare."
        )

    # Block on likely duplicate unless override
    top_dup = duplicates[0]["score"] if duplicates else 0.0
    if top_dup >= 0.95 and not getattr(args, "override_duplicate", False):
        store["candidates"][cand["id"]] = cand
        _save_candidates(cfg, store)
        raise ValueError(
            f"Candidate {cand['id']} has likely duplicate score={top_dup} "
            f"({duplicates[0].get('invoice_id')}). "
            "Pass --override-duplicate to prepare anyway."
        )

    proposed = cand["proposed_invoice"]
    summary = (
        f"Record invoice {extracted.get('direction')} "
        f"{extracted.get('counterparty')} "
        f"{extracted.get('amount')} {extracted.get('currency')} "
        f"(candidate {cand['id']})"
    )
    payload = {
        "candidate_id": cand["id"],
        "proposed_invoice": proposed,
        "extracted": extracted,
        "warnings": warnings,
        "duplicate_candidates": duplicates,
        "source_type": cand.get("source_type"),
        "source_id": cand.get("source_id"),
        "document_path": cand.get("document_path"),
        "risk": "medium",
    }
    action = create_pending_action(
        cfg,
        action_type="bookkeeper.invoice.record",
        provider="bookkeeper",
        target=str(extracted.get("counterparty") or cand["id"]),
        payload=payload,
        summary=summary,
        approver=None,
        reason=f"Prepared from invoice candidate {cand['id']}",
    )

    # Annotate risk on the action if create didn't set it (email path only sets risk)
    # Risk is informational in payload already.

    cand["state"] = "prepared"
    cand["pending_action_id"] = action.get("id")
    cand["updated_at"] = _now()
    store["candidates"][cand["id"]] = cand
    _save_candidates(cfg, store)

    result = {
        "candidate": cand,
        "pending_action": action,
        "note": "invoices.yaml was NOT written. Approve pending action to record.",
    }
    _emit(result)
    return result


def cmd_validate(args: argparse.Namespace) -> dict[str, Any]:
    cfg = configure(args.config)
    store = _load_candidates(cfg)
    results = []
    changed = False

    for cid, cand in list(store.get("candidates", {}).items()):
        if not isinstance(cand, dict):
            continue
        if cand.get("state") in ("dismissed", "recorded"):
            results.append({
                "id": cid,
                "state": cand.get("state"),
                "skipped": True,
            })
            continue
        extracted = dict(cand.get("extracted") or {})
        status, warnings = validate_candidate_extracted(
            extracted,
            document_path=cand.get("document_path"),
        )
        duplicates = find_duplicates(cfg, extracted, store, exclude_id=cid)
        warnings = _apply_duplicate_warnings(warnings, duplicates)
        required_missing = (
            extracted.get("direction") not in ("sent", "received")
            or not extracted.get("counterparty")
            or not extracted.get("amount")
            or not extracted.get("currency")
            or not extracted.get("issue_date")
            or not extracted.get("due_date")
        )
        if required_missing:
            status = "invalid"
        elif warnings:
            status = "needs_review"
        else:
            status = "valid"

        if (cand.get("validation_status") != status
                or cand.get("warnings") != warnings
                or cand.get("duplicate_candidates") != duplicates):
            cand["validation_status"] = status
            cand["warnings"] = warnings
            cand["duplicate_candidates"] = duplicates
            cand["updated_at"] = _now()
            store["candidates"][cid] = cand
            changed = True

        results.append({
            "id": cid,
            "state": cand.get("state"),
            "validation_status": status,
            "warnings": warnings,
            "duplicate_candidates": duplicates,
            "counterparty": extracted.get("counterparty"),
            "amount": extracted.get("amount"),
            "currency": extracted.get("currency"),
        })

    if changed:
        _save_candidates(cfg, store)

    by_status: dict[str, int] = {}
    for r in results:
        if r.get("skipped"):
            continue
        vs = r.get("validation_status", "unknown")
        by_status[vs] = by_status.get(vs, 0) + 1

    out = {
        "validated": len([r for r in results if not r.get("skipped")]),
        "by_status": by_status,
        "results": results,
    }

    if getattr(args, "summary", False):
        print("✅ Invoice candidate validation")
        print()
        print(f"Validated: {out['validated']}")
        for k, v in sorted(by_status.items()):
            print(f"  {k}: {v}")
        issues = [r for r in results if r.get("validation_status") in ("invalid", "needs_review")]
        if issues:
            print()
            print("Issues:")
            for r in issues:
                print(f"  {r['id']}: {r.get('validation_status')} — {', '.join(r.get('warnings') or [])}")
        return out

    _emit(out)
    return out


def cmd_dismiss(args: argparse.Namespace) -> dict[str, Any]:
    cfg = configure(args.config)
    store = _load_candidates(cfg)
    cand = store.get("candidates", {}).get(args.candidate_id)
    if not cand:
        raise KeyError(f"Candidate not found: {args.candidate_id}")
    if cand.get("state") == "recorded":
        raise ValueError("Cannot dismiss a recorded candidate")
    cand["state"] = "dismissed"
    cand["dismiss_reason"] = args.reason or "dismissed"
    cand["updated_at"] = _now()
    store["candidates"][cand["id"]] = cand
    _save_candidates(cfg, store)
    _emit(cand)
    return cand


# ---------------------------------------------------------------------------
# Output / CLI
# ---------------------------------------------------------------------------


def _emit(payload: Any) -> None:
    def default(obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)
        return str(obj)

    print(json.dumps(payload, indent=2, ensure_ascii=False, default=default))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Invoice ingestion: detect, extract, validate, prepare (no invoices.yaml writes)."
    )
    parser.add_argument("--config", help="Path to company.yaml (or CHIEF_OF_STAFF_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Scan local events/classifications for invoice candidates")
    scan.add_argument("--since", default=None, help="Only sources newer than e.g. 24h, 7d")
    scan.add_argument("--dry-run", action="store_true", help="Report without writing candidates")
    scan.add_argument("--summary", action="store_true", help="Human-readable summary")

    extract = sub.add_parser("extract", help="Extract a single source into a candidate")
    extract.add_argument("--source-id", required=True, help="Event/email/suggestion id")
    extract.add_argument("--force", action="store_true", help="Create new even if one exists")

    cands = sub.add_parser("candidates", help="List invoice candidates")
    cands.add_argument("--summary", action="store_true", help="Human-readable summary")

    preview = sub.add_parser("preview", help="Show full candidate details")
    preview.add_argument("--candidate-id", required=True)

    prepare = sub.add_parser(
        "prepare",
        help="Validate + duplicate-check + create pending action (does not write invoices.yaml)",
    )
    prepare.add_argument("--candidate-id", required=True)
    prepare.add_argument("--force", action="store_true",
                         help="Allow prepare even if validation is invalid")
    prepare.add_argument("--override-duplicate", action="store_true",
                         help="Allow prepare even if likely duplicate (score >= 0.95)")

    validate = sub.add_parser("validate", help="Re-validate all active candidates")
    validate.add_argument("--summary", action="store_true")

    dismiss = sub.add_parser("dismiss", help="Mark a candidate as dismissed")
    dismiss.add_argument("--candidate-id", required=True)
    dismiss.add_argument("--reason", required=True, help="Why this is not an invoice / not wanted")

    return parser


def _main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            cmd_scan(args)
        elif args.command == "extract":
            cmd_extract(args)
        elif args.command == "candidates":
            cmd_candidates(args)
        elif args.command == "preview":
            cmd_preview(args)
        elif args.command == "prepare":
            cmd_prepare(args)
        elif args.command == "validate":
            cmd_validate(args)
        elif args.command == "dismiss":
            cmd_dismiss(args)
        else:  # pragma: no cover
            parser.error("unknown command")
            return 2
    except KeyError as exc:
        print(str(exc).strip("'"), file=sys.stderr)
        return 1
    except (RuntimeError, ValueError) as exc:
        print(f"invoice_ingest.py error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
