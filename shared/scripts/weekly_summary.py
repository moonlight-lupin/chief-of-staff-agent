#!/usr/bin/env python3
"""Structured weekly review summary from local project files.

Reads pipeline / invoices / todos / expenses YAML (or StateDB stores) and
returns a briefing-shaped dict. Missing or malformed sources become empty
sections. Never raises to callers.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None  # type: ignore

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

WEEKLY_TITLE = "Chief-of-Staff Weekly Review"

try:
    from schemas import TODO_STATUSES as _TODO_STATUSES
except Exception:  # pragma: no cover
    _TODO_STATUSES = {"open", "done", "deferred", "cancelled"}

_DONE_STATUSES = frozenset({"done"}) & frozenset(_TODO_STATUSES)
_OPEN_STATUSES = frozenset(_TODO_STATUSES) - _DONE_STATUSES - {"cancelled"}
_SENT_STATUSES = frozenset({"sent"})
_PAID_STATUSES = frozenset({"paid"})
_OVERDUE_STATUSES = frozenset({"overdue"})
_RECEIVED_STATUSES = frozenset({"received"})
_CLOSED_STATUSES = frozenset({"paid", "cancelled"})
_DRAFT_STATUS = "draft"


def _project_root(config: Mapping[str, Any] | None) -> Path | None:
    try:
        if not isinstance(config, Mapping):
            return None
        paths = config.get("paths") or {}
        if isinstance(paths, Mapping) and paths.get("project_root"):
            return Path(str(paths["project_root"])).expanduser()
    except Exception:
        return None
    return None


def _wiki_path(config: Mapping[str, Any] | None, root: Path | None) -> Path | None:
    try:
        if isinstance(config, Mapping):
            paths = config.get("paths") or {}
            if isinstance(paths, Mapping) and paths.get("wiki_path"):
                return Path(str(paths["wiki_path"])).expanduser()
        if root is not None:
            return root / "wiki"
    except Exception:
        return None
    return None


def _yaml_records(path: Path, key: str) -> list | None:
    """Load a YAML list under ``key``. None means the file is absent.

    Structural problems (no PyYAML, non-dict document, missing/non-list key)
    raise so ``_load_records`` can fall through to the store.
    """
    if not path.exists():
        return None
    if yaml is None:
        raise ValueError("PyYAML is not available")
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path.name} is not a mapping")
    recs = loaded.get(key)
    if not isinstance(recs, list):
        raise ValueError(f"{path.name} missing list {key!r}")
    return recs


def _store_records(config: Mapping[str, Any] | None, store_name: str, key: str) -> list:
    try:
        from state_db import load_store
        data = load_store(store_name, config, validate=False)
        recs = data.get(key) if isinstance(data, dict) else []
        return recs if isinstance(recs, list) else []
    except Exception:
        return []


def _fallback_store(config: Mapping[str, Any] | None, store_name: str, key: str) -> list:
    if store_name == "pipeline":
        try:
            from pipeline_actions import load_pipeline
            data = load_pipeline(config)
            recs = data.get("deals") if isinstance(data, dict) else []
            return recs if isinstance(recs, list) else []
        except Exception:
            pass
    return _store_records(config, store_name, key)


def _peek_store_records(config: Mapping[str, Any] | None, store_name: str, key: str) -> list:
    """Count store records without persisting an empty template."""
    try:
        from state_db import StateDB
        with StateDB(config) as db:
            data = db.get_kv(store_name)
        recs = data.get(key) if isinstance(data, dict) else []
        return recs if isinstance(recs, list) else []
    except Exception:
        return []


def _load_records(
    config: Mapping[str, Any] | None,
    yaml_name: str,
    store_name: str,
    key: str,
    sources: dict[str, Any] | None = None,
) -> list:
    root = _project_root(config)
    yaml_recs: list | None = None
    yaml_ok = False
    if root is not None:
        try:
            yaml_recs = _yaml_records(root / yaml_name, key)
            yaml_ok = yaml_recs is not None
        except Exception:
            # Malformed YAML: fall through to the store instead of returning [].
            yaml_recs = None
            yaml_ok = False
    if yaml_ok:
        store_recs = _peek_store_records(config, store_name, key)
        if sources is not None:
            yaml_n = len(yaml_recs or [])
            store_n = len(store_recs)
            sources[store_name] = {
                "yaml_records": yaml_n,
                "store_records": store_n,
                "divergence": yaml_n != store_n,
            }
        return yaml_recs if isinstance(yaml_recs, list) else []
    return _fallback_store(config, store_name, key)


def _status(record: Mapping[str, Any]) -> str:
    return str(record.get("status") or "").strip().lower()


def _direction(record: Mapping[str, Any]) -> str:
    raw = str(record.get("direction") or "").strip().lower()
    if raw in {"sent", "received"}:
        return raw
    status = _status(record)
    if status in _SENT_STATUSES:
        return "sent"
    if status in _RECEIVED_STATUSES:
        return "received"
    return ""


def _week_start(today: date | None = None) -> date:
    day = today or date.today()
    return day - timedelta(days=day.weekday())


def _parse_date(value: Any) -> date | None:
    """Parse a date. Aware ISO timestamps are converted to the local timezone
    before taking ``date()``, so week membership uses local calendar days."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            return dt.date()
        return dt.astimezone().date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        if "T" in text or " " in text:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return dt.date()
            return dt.astimezone().date()
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _in_week(value: Any, start: date, end: date) -> bool:
    parsed = _parse_date(value)
    if parsed is None:
        return False
    return start <= parsed <= end


def _in_week_or_undated(value: Any, start: date, end: date) -> bool:
    parsed = _parse_date(value)
    if parsed is None:
        return True
    return start <= parsed <= end


def _amount(record: Mapping[str, Any]) -> float | int | None:
    raw = record.get("amount")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if not math.isfinite(raw):
        return None
    return raw


def _fallback_currency(config: Mapping[str, Any] | None) -> str:
    try:
        if isinstance(config, Mapping):
            bookkeeping = config.get("bookkeeping") or {}
            if isinstance(bookkeeping, Mapping) and bookkeeping.get("base_currency"):
                return str(bookkeeping["base_currency"])
    except Exception:
        pass
    return "SGD"


def _add_amount(bucket: dict[str, float | int], currency: str, amt: float | int) -> None:
    prev = bucket.get(currency, 0)
    total = prev + amt
    bucket[currency] = int(total) if isinstance(total, (int, float)) and total == int(total) else total


def _pipeline_section(deals: list) -> dict[str, Any]:
    rows = [d for d in deals if isinstance(d, dict)]
    if not rows:
        return {}
    by_stage: dict[str, int] = {}
    for deal in rows:
        stage = str(deal.get("stage") or "unknown")
        by_stage[stage] = by_stage.get(stage, 0) + 1
    week_start = _week_start()
    today = date.today()
    moved = 0
    for deal in rows:
        history = deal.get("stage_history")
        if isinstance(history, list) and history:
            last = history[-1]
            if isinstance(last, dict) and _in_week(last.get("at"), week_start, today):
                moved += 1
            continue
        if _in_week(deal.get("updated_at"), week_start, today):
            moved += 1
    return {
        "deals_by_stage": by_stage,
        "total_deals": len(rows),
        "deals_moved": moved,
    }


def _bookkeeping_section(
    invoices: list,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rows = [i for i in invoices if isinstance(i, dict)]
    if not rows:
        return {}
    week_start = _week_start()
    today = date.today()
    fallback_ccy = _fallback_currency(config)
    sent_n = 0
    received_n = 0
    paid_n = 0
    overdue_n = 0
    outstanding_ar: dict[str, float | int] = {}
    outstanding_ap: dict[str, float | int] = {}
    outstanding_unknown: dict[str, float | int] = {}
    for inv in rows:
        status = _status(inv)
        direction = _direction(inv)
        if (
            direction == "sent"
            and status != _DRAFT_STATUS
            and _in_week_or_undated(inv.get("issue_date"), week_start, today)
        ):
            sent_n += 1
        if (
            direction == "received"
            and status != _DRAFT_STATUS
            and _in_week_or_undated(inv.get("issue_date"), week_start, today)
        ):
            received_n += 1
        if status in _PAID_STATUSES:
            if _in_week(inv.get("paid_date"), week_start, today):
                paid_n += 1
            elif (
                _parse_date(inv.get("paid_date")) is None
                and _parse_date(inv.get("issue_date")) is None
            ):
                paid_n += 1
        if status in _OVERDUE_STATUSES:
            overdue_n += 1
        if status in _CLOSED_STATUSES or status == _DRAFT_STATUS:
            continue
        amt = _amount(inv)
        if amt is None:
            continue
        ccy = str(inv.get("currency") or fallback_ccy)
        if direction == "received":
            _add_amount(outstanding_ap, ccy, amt)
        elif direction == "sent":
            _add_amount(outstanding_ar, ccy, amt)
        else:
            _add_amount(outstanding_unknown, ccy, amt)
    out: dict[str, Any] = {
        "invoices_sent": sent_n,
        "invoices_received": received_n,
        "invoices_paid": paid_n,
        "overdue_invoices": overdue_n,
        "outstanding_ar": outstanding_ar,
        "outstanding_ap": outstanding_ap,
    }
    if outstanding_unknown:
        out["outstanding_unknown"] = outstanding_unknown
    return out


def _tasks_section(todos: list) -> dict[str, Any]:
    rows = [t for t in todos if isinstance(t, dict)]
    if not rows:
        return {}
    completed = [t for t in rows if _status(t) in _DONE_STATUSES]
    carry = [t for t in rows if _status(t) in _OPEN_STATUSES]
    week_start = _week_start()
    today = date.today()
    overdue = []
    for todo in carry:
        due = _parse_date(todo.get("due") or todo.get("due_date"))
        if due is not None and due < today:
            overdue.append(todo)
    weekly_completed = 0
    for todo in completed:
        stamp = todo.get("completed_at")
        if stamp:
            if _in_week(stamp, week_start, today):
                weekly_completed += 1
        else:
            weekly_completed += 1
    return {
        "tasks_completed": weekly_completed,
        "tasks_done_total": len(completed),
        "tasks_carry_over": len(carry),
        "tasks_overdue_open": len(overdue),
    }


def _wiki_frontmatter_dates(text: str) -> tuple[date | None, date | None]:
    created = None
    updated = None
    if not text.startswith("---\n"):
        return None, None
    fm_end = text.find("\n---\n", 4)
    if fm_end <= 0:
        return None, None
    for line in text[4:fm_end].split("\n"):
        stripped = line.strip()
        if stripped.startswith("created:"):
            created = _parse_date(stripped.split(":", 1)[1].strip().strip('"').strip("'"))
        elif stripped.startswith("updated:"):
            updated = _parse_date(stripped.split(":", 1)[1].strip().strip('"').strip("'"))
    return created, updated


def _local_mtime_date(path: Path) -> date | None:
    try:
        st = path.stat()
    except OSError:
        return None
    # File mtimes are UTC instants; convert to local before date() so a
    # Monday 05:00 SGT edit is not treated as Sunday UTC.
    return datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).astimezone().date()


def _knowledge_section(config: Mapping[str, Any] | None) -> dict[str, Any]:
    root = _project_root(config)
    wiki = _wiki_path(config, root)
    if wiki is None or not wiki.exists():
        return {}
    week_start = _week_start()
    today = date.today()
    created = 0
    updated = 0
    try:
        pages = [p for p in wiki.rglob("*.md") if p.is_file()]
    except Exception:
        return {}
    if not pages:
        return {}
    for path in pages:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        created_d, updated_d = _wiki_frontmatter_dates(text)
        if created_d is not None and week_start <= created_d <= today:
            created += 1
            continue
        if updated_d is not None and week_start <= updated_d <= today:
            updated += 1
            continue
        if updated_d is None:
            mtime = _local_mtime_date(path)
            if mtime is not None and week_start <= mtime <= today:
                updated += 1
    return {
        "wiki_pages_created": created,
        "wiki_pages_updated": updated,
        "wiki_pages_changed": created + updated,
    }


def _empty_summary() -> dict[str, int]:
    return {
        "deals_moved": 0,
        "invoices_sent": 0,
        "invoices_paid": 0,
        "overdue_invoices": 0,
        "tasks_completed": 0,
        "tasks_carry_over": 0,
        "wiki_pages_changed": 0,
    }


def _week_window() -> dict[str, str]:
    today = date.today()
    return {"start": _week_start(today).isoformat(), "end": today.isoformat()}


def _envelope(
    *,
    operator: str,
    summary: dict[str, int],
    pipeline: dict[str, Any],
    bookkeeping: dict[str, Any],
    tasks: dict[str, Any],
    knowledge: dict[str, Any],
    expenses: dict[str, Any],
    sources: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": "weekly",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "week": _week_window(),
        "operator": operator,
        "summary": summary,
        "pipeline": pipeline,
        "bookkeeping": bookkeeping,
        "tasks": tasks,
        "knowledge": knowledge,
        "expenses": expenses,
        "sections": {
            "pipeline": pipeline,
            "bookkeeper": bookkeeping,
        },
    }
    if sources:
        out["sources"] = sources
    return out


def _isolated_section(builder):
    """Run a section builder; any failure yields ``{}`` for that section only."""
    try:
        result = builder()
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def build_weekly_summary_from_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build a structured weekly summary from ``paths.project_root`` files."""
    try:
        sources: dict[str, Any] = {}
        pipeline = _isolated_section(
            lambda: _pipeline_section(
                _load_records(config, "pipeline.yaml", "pipeline", "deals", sources)
            )
        )
        bookkeeping = _isolated_section(
            lambda: _bookkeeping_section(
                _load_records(config, "invoices.yaml", "invoices", "invoices", sources),
                config,
            )
        )
        tasks = _isolated_section(
            lambda: _tasks_section(
                _load_records(config, "todos.yaml", "todos", "todos", sources)
            )
        )
        def _expenses():
            rows = _load_records(config, "expenses.yaml", "expenses", "expenses", sources)
            return {"expenses": rows} if rows else {}

        expenses = _isolated_section(_expenses)
        knowledge = _isolated_section(lambda: _knowledge_section(config))
        summary = _empty_summary()
        if pipeline:
            summary["deals_moved"] = int(pipeline.get("deals_moved") or 0)
        if bookkeeping:
            summary["invoices_sent"] = int(bookkeeping.get("invoices_sent") or 0)
            summary["invoices_paid"] = int(bookkeeping.get("invoices_paid") or 0)
            summary["overdue_invoices"] = int(bookkeeping.get("overdue_invoices") or 0)
        if tasks:
            summary["tasks_completed"] = int(tasks.get("tasks_completed") or 0)
            summary["tasks_carry_over"] = int(tasks.get("tasks_carry_over") or 0)
        if knowledge:
            summary["wiki_pages_changed"] = int(knowledge.get("wiki_pages_changed") or 0)
        operator = "Operator"
        try:
            if isinstance(config, Mapping):
                company = config.get("company") or {}
                if isinstance(company, Mapping) and company.get("name"):
                    operator = str(company["name"])
                elif config.get("operator"):
                    operator = str(config["operator"])
        except Exception:
            operator = "Operator"
        return _envelope(
            operator=operator,
            summary=summary,
            pipeline=pipeline,
            bookkeeping=bookkeeping,
            tasks=tasks,
            knowledge=knowledge,
            expenses=expenses,
            sources=sources,
        )
    except Exception:
        return _envelope(
            operator="Operator",
            summary=_empty_summary(),
            pipeline={},
            bookkeeping={},
            tasks={},
            knowledge={},
            expenses={},
        )


def build_weekly_summary(config_path: str | Path | None) -> dict[str, Any]:
    """Load company.yaml at ``config_path`` (or default) and build a summary."""
    try:
        from config_loader import load_config
        config = load_config(config_path, quiet=True) if config_path else load_config(quiet=True)
    except Exception:
        config = None
    if config is None and config_path:
        try:
            path = Path(str(config_path)).expanduser()
            if path.is_dir():
                config = {"paths": {"project_root": str(path)}}
            elif yaml is not None and path.exists():
                loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    config = loaded
        except Exception:
            config = None
    return build_weekly_summary_from_config(config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chief-of-Staff weekly review summary")
    parser.add_argument("--config", default=None, help="Path to company.yaml")
    parser.add_argument("--html", action="store_true", help="Self-contained HTML document")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args(argv)
    summary = build_weekly_summary(args.config)
    if args.html:
        from briefing_renderer import render
        print(render(summary, "html", title=WEEKLY_TITLE))
        return 0
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
        return 0
    from briefing_renderer import render
    print(render(summary, "text"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
