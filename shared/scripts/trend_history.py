#!/usr/bin/env python3
"""Compact briefing-counter snapshots for week-over-week trend rendering.

Persists to StateDB kv store ``briefing_trends``. Never raises to callers.
Demo briefings are isolated and write nothing.
"""
from __future__ import annotations

import html as _html
import math
import os
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

TREND_KV_STORE = "briefing_trends"
TREND_ROOT_KEY = "trend_snapshots"
TREND_MAX_AGE_DAYS = 90
TREND_STALE_DAYS = 7

_TREND_METRICS = (
    "needs_attention",
    "pending_approvals",
    "suggestions",
    "classified_emails",
    "system_warnings",
    "pipeline.active_deals",
    "pipeline.stale_deals",
    "pipeline.oldest_stale_days",
    "pipeline.recently_moved",
    "pipeline.pending_crm_actions",
    "pipeline.contract_signed_no_invoice",
    "pipeline.invoiced_not_paid",
    "bookkeeper.candidates_found",
    "bookkeeper.candidates_needs_review",
    "bookkeeper.duplicate_warnings",
    "bookkeeper.pending_record_actions",
    "bookkeeper.outstanding_ar",
    "bookkeeper.outstanding_ap",
    "bookkeeper.overdue_count",
)

_COERCE_NUMERIC_KEY = re.compile(r"^outstanding_|^amount|_total$")


def _esc(text: Any) -> str:
    return _html.escape(str(text)) if text not in (None, "") else ""


def _is_numeric_scalar(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return True


def note_exception(where: str, exc: BaseException) -> None:
    """Breadcrumb for swallowed trend failures. Never raises."""
    message = f"{where}: {type(exc).__name__}: {exc}"
    debug = os.getenv("CHIEF_OF_STAFF_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    if debug:
        print(f"trend_history {message}", file=sys.stderr)
    try:
        from runtime_log import log_event
        log_event(
            "trend_history_error",
            level="debug",
            component="trend_history",
            message=message,
        )
    except Exception:
        pass


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _utc_date(value: Any):
    dt = _parse_ts(value)
    return dt.date() if dt is not None else None


def _coerce_numeric(key: str, value: Any) -> int | float | None:
    if _is_numeric_scalar(value):
        return value
    if not isinstance(value, str) or not _COERCE_NUMERIC_KEY.search(str(key)):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        decimal_value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not decimal_value.is_finite():
        return None
    as_float = float(decimal_value)
    if not math.isfinite(as_float):
        return None
    if as_float == int(as_float) and abs(as_float) < 2**53:
        return int(as_float)
    return as_float


def _flatten_currency_list(key: str, items: list, put) -> None:
    totals: dict[str, float] = {}
    int_only: dict[str, bool] = {}
    for item in items:
        if not isinstance(item, dict):
            return
        currency = item.get("currency")
        amount = item.get("amount")
        if not isinstance(currency, str) or not currency or not _is_numeric_scalar(amount):
            return
        totals[currency] = totals.get(currency, 0) + amount
        int_only[currency] = int_only.get(currency, True) and isinstance(amount, int)
    for currency, total in totals.items():
        value = int(total) if int_only.get(currency) and total == int(total) else total
        put(f"{key}::{currency}", value)


def _flatten_mapping(block: dict, counters: dict[str, int | float], prefix: str) -> None:
    dropped: set[str] = set()

    def _put(dest: str, value: int | float) -> None:
        if dest in dropped:
            return
        if dest in counters:
            counters.pop(dest, None)
            dropped.add(dest)
            note_exception(
                "_flatten_mapping",
                ValueError(f"duplicate destination key {dest!r}"),
            )
            return
        counters[dest] = value

    for key, value in block.items():
        if not isinstance(key, str):
            continue
        dest = f"{prefix}.{key}" if prefix else key
        coerced = _coerce_numeric(key, value)
        if coerced is not None:
            _put(dest, coerced)
            continue
        if isinstance(value, dict):
            if not value:
                continue
            if not all(isinstance(k, str) for k in value):
                continue
            nested_ok = True
            nested_pairs: list[tuple[str, int | float]] = []
            for nested_key, nested_val in value.items():
                nested_coerced = _coerce_numeric(nested_key, nested_val) if nested_val is not None else None
                if nested_val is None:
                    continue
                if nested_coerced is None:
                    nested_ok = False
                    break
                nested_pairs.append((nested_key, nested_coerced))
            if not nested_ok:
                continue
            nested_prefix = "stage" if key == "deals_by_stage" else key
            head = f"{prefix}.{nested_prefix}" if prefix else nested_prefix
            for nested_key, nested_val in nested_pairs:
                _put(f"{head}::{nested_key}", nested_val)
            continue
        if isinstance(value, list):
            if not value:
                continue
            if all(isinstance(item, dict) for item in value):
                _flatten_currency_list(dest, value, _put)


def _extract_counters(briefing: Mapping[str, Any]) -> dict[str, int | float] | None:
    summary = briefing.get("summary")
    if not isinstance(summary, dict):
        return None
    counters: dict[str, int | float] = {}
    for key, value in summary.items():
        if not isinstance(key, str):
            continue
        coerced = _coerce_numeric(key, value)
        if coerced is not None:
            counters[key] = coerced
    sections = briefing.get("sections")
    if isinstance(sections, dict):
        for name in ("pipeline", "bookkeeper", "system"):
            block = sections.get(name)
            if isinstance(block, dict):
                _flatten_mapping(block, counters, prefix=name)
    if not counters:
        return None
    return counters


def _load_snapshots(db) -> list:
    stored = db.get_kv(TREND_KV_STORE)
    if not stored or not isinstance(stored, dict):
        return []
    snaps = stored.get(TREND_ROOT_KEY)
    if not isinstance(snaps, list):
        return []
    return [s for s in snaps if isinstance(s, dict)]


def _prune(snaps: list, now: datetime) -> list:
    cutoff = now - timedelta(days=TREND_MAX_AGE_DAYS)
    kept = []
    for snap in snaps:
        dt = _parse_ts(snap.get("ts"))
        if dt is None:
            continue
        if dt >= cutoff:
            kept.append(snap)
    return kept


def capture_snapshot(briefing: dict, config: Mapping, kind: str = "daily") -> dict | None:
    """Persist a counters snapshot. Returns the stored dict, or None."""
    try:
        if not isinstance(briefing, dict):
            return None
        if briefing.get("demo"):
            return None
        counters = _extract_counters(briefing)
        if counters is None:
            return None
        from state_db import StateDB

        now = datetime.now(timezone.utc)
        snapshot = {
            "ts": now.isoformat(),
            "kind": kind,
            "counters": counters,
        }

        def _mutate(data: dict[str, Any]) -> dict:
            snaps = data.get(TREND_ROOT_KEY)
            if not isinstance(snaps, list):
                snaps = []
            snaps = [s for s in snaps if isinstance(s, dict)]
            snaps = _prune(snaps, now)
            day = now.date()
            replace_at = None
            for i, existing in enumerate(snaps):
                if existing.get("kind") != kind:
                    continue
                existing_day = _utc_date(existing.get("ts"))
                if existing_day is None:
                    continue
                if existing_day == day:
                    replace_at = i
            if replace_at is not None:
                snaps[replace_at] = snapshot
            else:
                snaps.append(snapshot)
            data[TREND_ROOT_KEY] = snaps
            return snapshot

        with StateDB(config) as db:
            result = db.mutate_kv(TREND_KV_STORE, _mutate)
        return result
    except Exception as exc:
        note_exception("capture_snapshot", exc)
        return None


def _points_from_snaps(
    snaps: list,
    metric: str,
    cutoff: datetime,
    kind: str | None,
) -> list[dict]:
    points: list[dict] = []
    for snap in snaps:
        if kind is not None and snap.get("kind") != kind:
            continue
        dt = _parse_ts(snap.get("ts"))
        if dt is None or dt < cutoff:
            continue
        counters = snap.get("counters")
        if not isinstance(counters, dict) or metric not in counters:
            continue
        value = counters[metric]
        if not _is_numeric_scalar(value):
            continue
        points.append({
            "ts": snap.get("ts"),
            "kind": snap.get("kind"),
            "value": value,
        })
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    points.sort(key=lambda p: _parse_ts(p.get("ts")) or epoch)
    return points


def get_series(
    config: Mapping,
    metric: str,
    days: int = 30,
    kind: str | None = None,
) -> list[dict]:
    """Return ``[{ts, kind, value}, ...]`` ascending by ts. Never raises."""
    try:
        from state_db import StateDB

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        with StateDB(config) as db:
            snaps = _load_snapshots(db)
        return _points_from_snaps(snaps, metric, cutoff, kind)
    except Exception as exc:
        note_exception("get_series", exc)
        return []


def _format_delta_number(delta: int | float) -> str:
    if isinstance(delta, int) or (isinstance(delta, float) and delta.is_integer()):
        return str(int(delta))
    text = f"{float(delta):.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _delta_label(series: list[dict]) -> str:
    same_kind = series
    if len(same_kind) < 2:
        return ""
    last = same_kind[-1].get("value")
    prev = same_kind[-2].get("value")
    if not _is_numeric_scalar(last) or not _is_numeric_scalar(prev):
        return ""
    delta = last - prev
    if isinstance(last, int) and isinstance(prev, int):
        delta = int(delta)
    if delta > 0:
        return f"vs prev: +{_format_delta_number(delta)}"
    if delta < 0:
        return f"vs prev: {_format_delta_number(delta)}"
    return "vs prev: 0"


_OUTSTANDING_METRIC_PREFIXES = (
    "bookkeeper.outstanding_ar",
    "bookkeeper.outstanding_ap",
)


def _trend_metrics_for(snaps: list) -> list[str]:
    """Static metric order, plus per-currency outstanding keys found in snaps."""
    extras: dict[str, list[str]] = {p: [] for p in _OUTSTANDING_METRIC_PREFIXES}
    seen: set[str] = set()
    for snap in snaps:
        if not isinstance(snap, dict):
            continue
        counters = snap.get("counters")
        if not isinstance(counters, dict):
            continue
        for key in counters:
            if not isinstance(key, str) or key in seen:
                continue
            for prefix in _OUTSTANDING_METRIC_PREFIXES:
                if key.startswith(prefix + "::"):
                    extras[prefix].append(key)
                    seen.add(key)
                    break
    for prefix in extras:
        extras[prefix].sort()
    out: list[str] = []
    for metric in _TREND_METRICS:
        out.append(metric)
        children = extras.get(metric)
        if children:
            out.extend(children)
    return out


def build_trends_section(briefing: dict, config: Mapping) -> dict:
    """Build the HTML trends section dict. Never raises; ``{}`` on error."""
    try:
        del briefing  # unused; series come from stored snapshots
        from state_db import StateDB

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=30)
        with StateDB(config) as db:
            snaps = _load_snapshots(db)
        metrics = []
        for metric in _trend_metrics_for(snaps):
            series = _points_from_snaps(snaps, metric, cutoff, kind="daily")
            if not series:
                continue
            metrics.append({
                "label": metric,
                "series": series,
                "delta_label": _delta_label(series),
            })
        if not metrics:
            return {}
        return {"metrics": metrics}
    except Exception as exc:
        note_exception("build_trends_section", exc)
        return {}


def snapshot_health(config: Mapping | None) -> dict[str, str]:
    """Doctor check: snapshot store presence and last-snapshot age."""
    try:
        from state_db import StateDB

        with StateDB(config) as db:
            snaps = _load_snapshots(db)
        if not snaps:
            return {"status": "warn", "detail": "no snapshots yet"}
        latest = None
        for snap in snaps:
            dt = _parse_ts(snap.get("ts"))
            if dt is not None and (latest is None or dt > latest):
                latest = dt
        if latest is None:
            return {"status": "warn", "detail": "snapshot store has no parseable timestamps"}
        age = datetime.now(timezone.utc) - latest
        days = age.days
        if age > timedelta(days=TREND_STALE_DAYS):
            return {
                "status": "warn",
                "detail": f"last snapshot {days}d ago (>{TREND_STALE_DAYS}d)",
            }
        return {"status": "ok", "detail": f"last snapshot {days}d ago"}
    except Exception as exc:
        note_exception("snapshot_health", exc)
        return {"status": "warn", "detail": f"unavailable: {exc}"}


def doctor_snapshot_health(config: Mapping | None) -> dict[str, str]:
    """Doctor view: a never-written store is not a permanent warn."""
    health = snapshot_health(config)
    if health.get("status") == "warn" and health.get("detail") == "no snapshots yet":
        return {"status": "ok", "detail": "trend snapshots not enabled yet"}
    return health


def render_trends_html(section: dict) -> str:
    """Render CSS bar rows. Malformed input → ``""``."""
    try:
        if not isinstance(section, dict):
            return ""
        metrics = section.get("metrics")
        if not isinstance(metrics, list):
            return ""
        parts: list[str] = []
        for item in metrics:
            if not isinstance(item, dict):
                continue
            label = item.get("label")
            series = item.get("series")
            if not isinstance(label, str) or not isinstance(series, list) or not series:
                continue
            values = [
                p.get("value")
                for p in series
                if isinstance(p, dict) and _is_numeric_scalar(p.get("value"))
            ]
            if not values:
                continue
            last = values[-1]
            peak = max(values)
            if peak <= 0:
                width = 0
            else:
                width = int(round((last / peak) * 100))
                if width < 0:
                    width = 0
            delta = item.get("delta_label") or ""
            if not isinstance(delta, str):
                delta = ""
            parts.append(
                f'<div class="trend-row">'
                f'<span class="trend-label">{_esc(label)}</span>'
                f'<span class="trend-value">{_esc(last)}</span>'
                f'<span class="trend-delta">{_esc(delta)}</span>'
                f'<div class="bar"><div class="bar-fill" style="width: {width}%"></div></div>'
                f"</div>"
            )
        return "".join(parts)
    except Exception as exc:
        note_exception("render_trends_html", exc)
        return ""
