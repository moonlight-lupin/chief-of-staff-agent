#!/usr/bin/env python3
"""Structured weekly review summary from local project files.

Reads pipeline / invoices / todos / expenses YAML (or StateDB stores) and
returns a briefing-shaped dict. Missing or malformed sources become empty
sections. Never raises to callers.
"""
from __future__ import annotations

import argparse
import json
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
_DONE_STATUSES = frozenset({"done", "completed"})
_OPEN_STATUSES = frozenset({"open", "pending", "deferred"})
_SENT_STATUSES = frozenset({"sent"})
_PAID_STATUSES = frozenset({"paid"})
_OVERDUE_STATUSES = frozenset({"overdue"})
_RECEIVED_STATUSES = frozenset({"received"})


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
    """Load a YAML list under ``key``. None means the file is absent."""
    if not path.exists():
        return None
    if yaml is None:
        return []
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if loaded is None:
        return []
    if not isinstance(loaded, dict):
        return []
    recs = loaded.get(key)
    return recs if isinstance(recs, list) else []


def _store_records(config: Mapping[str, Any] | None, store_name: str, key: str) -> list:
    try:
        from state_db import load_store
        data = load_store(store_name, config, validate=False)
        recs = data.get(key) if isinstance(data, dict) else []
        return recs if isinstance(recs, list) else []
    except Exception:
        return []


def _load_records(config: Mapping[str, Any] | None, yaml_name: str, store_name: str, key: str) -> list:
    root = _project_root(config)
    if root is not None:
        try:
            recs = _yaml_records(root / yaml_name, key)
            if recs is not None:
                return recs
        except Exception:
            return []
    if store_name == "pipeline":
        try:
            from pipeline_actions import load_pipeline
            data = load_pipeline(config)
            recs = data.get("deals") if isinstance(data, dict) else []
            return recs if isinstance(recs, list) else []
        except Exception:
            pass
    return _store_records(config, store_name, key)


def _status(record: Mapping[str, Any]) -> str:
    return str(record.get("status") or "").strip().lower()


def _week_start(today: date | None = None) -> date:
    day = today or date.today()
    return day - timedelta(days=day.weekday())


def _parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        if "T" in text:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _in_week(value: Any, start: date, end: date) -> bool:
    parsed = _parse_date(value)
    if parsed is None:
        return False
    return start <= parsed <= end


def _amount(record: Mapping[str, Any]) -> float | int | None:
    raw = record.get("amount")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return raw


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
        for field in ("last_moved_at", "moved_at", "stage_changed_at", "updated_at"):
            if _in_week(deal.get(field), week_start, today):
                moved += 1
                break
    return {
        "deals_by_stage": by_stage,
        "total_deals": len(rows),
        "deals_moved": moved,
    }


def _bookkeeping_section(invoices: list) -> dict[str, Any]:
    rows = [i for i in invoices if isinstance(i, dict)]
    if not rows:
        return {}
    sent = [i for i in rows if _status(i) in _SENT_STATUSES]
    paid = [i for i in rows if _status(i) in _PAID_STATUSES]
    overdue = [i for i in rows if _status(i) in _OVERDUE_STATUSES]
    received = [i for i in rows if _status(i) in _RECEIVED_STATUSES]
    outstanding: dict[str, float | int] = {}
    for inv in rows:
        if _status(inv) in _PAID_STATUSES | {"cancelled"}:
            continue
        amt = _amount(inv)
        if amt is None:
            continue
        ccy = str(inv.get("currency") or "SGD")
        prev = outstanding.get(ccy, 0)
        total = prev + amt
        outstanding[ccy] = int(total) if isinstance(total, (int, float)) and total == int(total) else total
    return {
        "invoices_sent": len(sent),
        "invoices_received": len(received),
        "invoices_paid": len(paid),
        "overdue_invoices": len(overdue),
        "outstanding_totals": outstanding,
    }


def _tasks_section(todos: list) -> dict[str, Any]:
    rows = [t for t in todos if isinstance(t, dict)]
    if not rows:
        return {}
    completed = [t for t in rows if _status(t) in _DONE_STATUSES]
    carry = [t for t in rows if _status(t) in _OPEN_STATUSES]
    today = date.today()
    overdue = []
    for todo in carry:
        due = _parse_date(todo.get("due") or todo.get("due_date"))
        if due is not None and due < today:
            overdue.append(todo)
    return {
        "tasks_completed": len(completed),
        "tasks_carry_over": len(carry),
        "tasks_overdue_open": len(overdue),
    }


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
            st = path.stat()
        except OSError:
            continue
        ctime = datetime.fromtimestamp(st.st_ctime, tz=timezone.utc).date()
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).date()
        if week_start <= ctime <= today:
            created += 1
        elif week_start <= mtime <= today:
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


def build_weekly_summary_from_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build a structured weekly summary from ``paths.project_root`` files."""
    try:
        pipeline = _pipeline_section(_load_records(config, "pipeline.yaml", "pipeline", "deals"))
        bookkeeping = _bookkeeping_section(_load_records(config, "invoices.yaml", "invoices", "invoices"))
        tasks = _tasks_section(_load_records(config, "todos.yaml", "todos", "todos"))
        expenses = _load_records(config, "expenses.yaml", "expenses", "expenses")
        knowledge = _knowledge_section(config)
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
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "operator": operator,
            "summary": summary,
            "pipeline": pipeline,
            "bookkeeping": bookkeeping,
            "tasks": tasks,
            "knowledge": knowledge,
            "expenses": {"expenses": expenses} if expenses else {},
            "sections": {
                "pipeline": pipeline,
                "bookkeeper": bookkeeping,
            },
        }
    except Exception:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "operator": "Operator",
            "summary": _empty_summary(),
            "pipeline": {},
            "bookkeeping": {},
            "tasks": {},
            "knowledge": {},
            "expenses": {},
            "sections": {},
        }


def build_weekly_summary(config_path: str | Path | None) -> dict[str, Any]:
    """Load company.yaml at ``config_path`` (or default) and build a summary."""
    try:
        from config_loader import load_config
        config = load_config(config_path) if config_path else load_config()
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
