#!/usr/bin/env python3
"""Compact briefing-counter snapshots for week-over-week trend rendering.

Persists to StateDB kv store ``briefing_trends``. Never raises to callers.
Demo briefings are isolated and write nothing.
"""
from __future__ import annotations

import html as _html
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

TREND_KV_STORE = "briefing_trends"
TREND_ROOT_KEY = "trend_snapshots"
TREND_MAX_AGE_DAYS = 90

_TREND_METRICS = (
    "needs_attention",
    "pending_approvals",
    "suggestions",
    "classified_emails",
    "system_warnings",
)


def _esc(text: Any) -> str:
    return _html.escape(str(text)) if text not in (None, "") else ""


def _is_numeric_scalar(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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


def _flatten_currency_list(key: str, items: list, counters: dict[str, int | float]) -> None:
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
        counters[f"{key}::{currency}"] = int(total) if int_only.get(currency) and total == int(total) else total


def _flatten_mapping(block: dict, counters: dict[str, int | float]) -> None:
    for key, value in block.items():
        if not isinstance(key, str):
            continue
        if _is_numeric_scalar(value):
            counters[key] = value
            continue
        if isinstance(value, dict):
            if not value:
                continue
            if not all(isinstance(k, str) for k in value):
                continue
            if not all(v is None or _is_numeric_scalar(v) for v in value.values()):
                continue
            prefix = "stage" if key == "deals_by_stage" else key
            for nested_key, nested_val in value.items():
                if _is_numeric_scalar(nested_val):
                    counters[f"{prefix}::{nested_key}"] = nested_val
            continue
        if isinstance(value, list):
            if not value:
                continue
            if all(isinstance(item, dict) for item in value):
                _flatten_currency_list(key, value, counters)


def _extract_counters(briefing: Mapping[str, Any]) -> dict[str, int | float] | None:
    summary = briefing.get("summary")
    if not isinstance(summary, dict):
        return None
    counters: dict[str, int | float] = {}
    for key, value in summary.items():
        if isinstance(key, str) and _is_numeric_scalar(value):
            counters[key] = value
    sections = briefing.get("sections")
    if isinstance(sections, dict):
        for name in ("pipeline", "bookkeeper"):
            block = sections.get(name)
            if isinstance(block, dict):
                _flatten_mapping(block, counters)
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
        if dt is None or dt >= cutoff:
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
        with StateDB(config) as db:
            snaps = _load_snapshots(db)
            snaps = _prune(snaps, now)
            day = now.date()
            replace_at = None
            for i, existing in enumerate(snaps):
                if existing.get("kind") != kind:
                    continue
                if _utc_date(existing.get("ts")) == day:
                    replace_at = i
            if replace_at is not None:
                snaps[replace_at] = snapshot
            else:
                snaps.append(snapshot)
            db.set_kv(TREND_KV_STORE, {TREND_ROOT_KEY: snaps})
        return snapshot
    except Exception:
        return None


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
        points.sort(key=lambda p: str(p.get("ts") or ""))
        return points
    except Exception:
        return []


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
        return f"vs prev: +{delta}"
    if delta < 0:
        return f"vs prev: {delta}"
    return "vs prev: 0"


def build_trends_section(briefing: dict, config: Mapping) -> dict:
    """Build the HTML trends section dict. Never raises; ``{}`` on error."""
    try:
        del briefing  # unused; series come from stored snapshots
        metrics = []
        for metric in _TREND_METRICS:
            series = get_series(config, metric, days=30, kind="daily")
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
    except Exception:
        return {}


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
    except Exception:
        return ""
