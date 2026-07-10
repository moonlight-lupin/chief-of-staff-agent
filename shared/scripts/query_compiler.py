#!/usr/bin/env python3
"""Neutral query model -> provider dialect compiler.

A "query model" is either:

* a plain ``str`` — treated as RAW Gmail search syntax for back-compat with the
  existing ``queries.yaml`` templates (e.g. ``"is:unread newer_than:3d"``).  For
  the ``gmail`` dialect the string is returned unchanged.  For the ``m365``
  dialect a string cannot be reliably translated from Gmail syntax, so it is
  passed through best-effort as a ``$search`` KQL string.

* a ``dict`` with any of the neutral keys:
    unread(bool), from_sender(str), domain(str), newer_than_days(int),
    older_than_days(int), has_attachment(bool), subject_contains(str),
    tag(str), folder(str), text(str),
    raw({"gmail": str, "m365": {"filter":.., "search":..} | str})

``compile_query(model, dialect, now=None)`` returns:

* dialect ``"gmail"`` -> a query string
  (``"is:unread from:x newer_than:2d has:attachment"``).
* dialect ``"m365"``  -> ``{"filter": str | None, "search": str | None}``.

The ``raw`` override wins per-dialect: if ``raw["gmail"]`` / ``raw["m365"]`` is
present it is returned directly for that dialect.

Microsoft Graph rule (messages): ``$filter`` and ``$search`` may NOT be combined
on ``/messages``.  If both would be produced we FOLD everything into a single
``$search`` KQL string (``isread:false``, ``from:x``, ``hasattachments:true``,
``received>=YYYY-MM-DD`` and ``category:...`` are valid KQL) and set
``filter=None``.  See :func:`_m365_fold_to_kql`.

Pure functions, no I/O.  ``now`` is injectable so date math is deterministic in
tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


# ── OData / KQL helpers ────────────────────────────────────────────────

def _odata_escape(value: str) -> str:
    """Escape a string literal for an OData filter (single quote doubled)."""
    return str(value).replace("'", "''")


def _needs_quote(value: str) -> bool:
    return any(c.isspace() for c in value)


def _kql_term(prefix: str, value: str) -> str:
    """Build a KQL term, quoting the value if it contains whitespace."""
    val = str(value)
    if _needs_quote(val):
        return f'{prefix}:"{val}"'
    return f"{prefix}:{val}"


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _date_only(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


# ── Gmail dialect ──────────────────────────────────────────────────────

def _compile_gmail(model: dict[str, Any]) -> str:
    raw = model.get("raw")
    if isinstance(raw, dict) and raw.get("gmail"):
        return str(raw["gmail"])

    parts: list[str] = []
    if model.get("unread"):
        parts.append("is:unread")
    if model.get("from_sender"):
        parts.append(f"from:{model['from_sender']}")
    if model.get("domain"):
        parts.append(f"from:{model['domain']}")
    if model.get("newer_than_days") is not None:
        parts.append(f"newer_than:{int(model['newer_than_days'])}d")
    if model.get("older_than_days") is not None:
        parts.append(f"older_than:{int(model['older_than_days'])}d")
    if model.get("has_attachment"):
        parts.append("has:attachment")
    if model.get("subject_contains"):
        subj = str(model["subject_contains"])
        parts.append(f'subject:"{subj}"' if _needs_quote(subj) else f"subject:{subj}")
    if model.get("tag"):
        parts.append(f"label:{model['tag']}")
    if model.get("folder"):
        parts.append(f"in:{model['folder']}")
    if model.get("text"):
        parts.append(str(model["text"]))
    return " ".join(parts)


# ── Microsoft 365 (Graph) dialect ──────────────────────────────────────

def _m365_fold_to_kql(model: dict[str, Any], now: datetime) -> str:
    """Fold the entire model into a single Graph ``$search`` KQL string.

    Used when both a ``$filter`` and a ``$search`` would otherwise be produced,
    which Graph forbids on ``/messages``.
    """
    tokens: list[str] = []
    if model.get("unread"):
        tokens.append("isread:false")
    if model.get("from_sender"):
        tokens.append(_kql_term("from", model["from_sender"]))
    if model.get("domain"):
        tokens.append(_kql_term("from", model["domain"]))
    if model.get("newer_than_days") is not None:
        threshold = now - timedelta(days=int(model["newer_than_days"]))
        tokens.append(f"received>={_date_only(threshold)}")
    if model.get("older_than_days") is not None:
        threshold = now - timedelta(days=int(model["older_than_days"]))
        tokens.append(f"received<={_date_only(threshold)}")
    if model.get("has_attachment"):
        tokens.append("hasattachments:true")
    if model.get("subject_contains"):
        tokens.append(_kql_term("subject", model["subject_contains"]))
    if model.get("tag"):
        tokens.append(_kql_term("category", model["tag"]))
    if model.get("text"):
        text = str(model["text"])
        tokens.append(f'"{text}"' if _needs_quote(text) else text)
    return " ".join(tokens)


def _compile_m365(model: dict[str, Any], now: datetime) -> dict[str, str | None]:
    raw = model.get("raw")
    if isinstance(raw, dict) and "m365" in raw:
        m = raw["m365"]
        if isinstance(m, dict):
            return {"filter": m.get("filter"), "search": m.get("search")}
        # A bare string raw override is treated as a $search KQL string.
        return {"filter": None, "search": str(m)}

    filter_parts: list[str] = []
    search_parts: list[str] = []

    # $filter-eligible fields
    if model.get("unread"):
        filter_parts.append("isRead eq false")
    if model.get("from_sender"):
        filter_parts.append(
            f"from/emailAddress/address eq '{_odata_escape(model['from_sender'])}'"
        )
    if model.get("newer_than_days") is not None:
        threshold = now - timedelta(days=int(model["newer_than_days"]))
        filter_parts.append(f"receivedDateTime ge {_rfc3339(threshold)}")
    if model.get("older_than_days") is not None:
        threshold = now - timedelta(days=int(model["older_than_days"]))
        filter_parts.append(f"receivedDateTime le {_rfc3339(threshold)}")
    if model.get("has_attachment"):
        filter_parts.append("hasAttachments eq true")
    if model.get("tag"):
        filter_parts.append(
            f"categories/any(c:c eq '{_odata_escape(model['tag'])}')"
        )

    # $search-eligible fields (KQL)
    if model.get("subject_contains"):
        search_parts.append(_kql_term("subject", model["subject_contains"]))
    if model.get("domain"):
        search_parts.append(_kql_term("from", model["domain"]))
    if model.get("text"):
        text = str(model["text"])
        search_parts.append(f'"{text}"' if _needs_quote(text) else text)

    # Graph forbids combining $filter + $search on /messages -> fold to KQL.
    if filter_parts and search_parts:
        return {"filter": None, "search": _m365_fold_to_kql(model, now)}

    return {
        "filter": " and ".join(filter_parts) if filter_parts else None,
        "search": " ".join(search_parts) if search_parts else None,
    }


# ── Public API ─────────────────────────────────────────────────────────

def compile_query(
    model: str | dict[str, Any],
    dialect: str,
    now: datetime | None = None,
) -> Any:
    """Compile a neutral query model into a provider dialect.

    Args:
        model: a raw Gmail-syntax string or a neutral query dict.
        dialect: ``"gmail"`` or ``"m365"``.
        now: reference time for relative-date math (defaults to UTC now).

    Returns:
        ``str`` for the gmail dialect; ``{"filter": .., "search": ..}`` for m365.
    """
    now = now or datetime.now(timezone.utc)

    if dialect == "gmail":
        if isinstance(model, str):
            return model
        if isinstance(model, dict):
            return _compile_gmail(model)
        raise TypeError(f"query model must be str or dict, got {type(model).__name__}")

    if dialect == "m365":
        if isinstance(model, str):
            # Gmail-syntax strings can't be reliably translated; pass through as
            # a best-effort $search KQL string.
            return {"filter": None, "search": model or None}
        if isinstance(model, dict):
            return _compile_m365(model, now)
        raise TypeError(f"query model must be str or dict, got {type(model).__name__}")

    raise ValueError(f"unknown dialect: {dialect!r} (expected 'gmail' or 'm365')")
