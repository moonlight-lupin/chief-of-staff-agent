#!/usr/bin/env python3
"""Mutate and query Chief-of-Staff todos store (SQLite KV)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

try:
    from config_loader import load_config  # type: ignore
    from schemas import SchemaError, generate_id, validate_todo  # type: ignore
    from state_db import load_store, mutate_kv  # type: ignore
except Exception as exc:  # pragma: no cover
    print(
        f"Chief-of-Staff bootstrap incomplete: cannot import shared scripts from {SHARED_SCRIPTS}: {exc}. "
        "Run the plugin bootstrap/foundation setup first.",
        file=sys.stderr,
    )
    raise SystemExit(2)

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def today() -> str:
    return date.today().isoformat()


def configure(path: str | None) -> None:
    if path:
        os.environ["CHIEF_OF_STAFF_CONFIG"] = path
    if load_config(path) is None:
        raise RuntimeError("Could not load company.yaml; pass --config or set CHIEF_OF_STAFF_CONFIG")


def emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    elif isinstance(payload, list):
        for todo in payload:
            due = todo.get("due") or "no due date"
            print(f"{todo.get('id')}: [{todo.get('priority')}] {todo.get('title')} ({todo.get('status')}, due {due})")
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def parse_tags(value: str | None) -> list[str]:
    if not value:
        return []
    return [tag.strip() for tag in value.split(",") if tag.strip()]


def normalize(todo: dict[str, Any]) -> dict[str, Any]:
    todo.setdefault("tags", [])
    if not isinstance(todo.get("tags"), list):
        todo["tags"] = parse_tags(str(todo.get("tags") or ""))
    todo.setdefault("source_skill", todo.pop("source", None))
    todo.setdefault("source_ref", None)
    todo.setdefault("completed", None)
    return todo


def find_todo(data: dict[str, Any], todo_id: str) -> dict[str, Any]:
    for todo in data.setdefault("todos", []):
        if isinstance(todo, dict) and str(todo.get("id")) == todo_id:
            return normalize(todo)
    raise KeyError(f"Todo not found: {todo_id}")


def cmd_add(args: argparse.Namespace) -> dict[str, Any]:
    configure(args.config)
    todo = {
        "id": generate_id("todo"),
        "title": args.title,
        "priority": args.priority,
        "due": args.due,
        "tags": parse_tags(args.tags),
        "status": "open",
        "created": today(),
        "completed": None,
        "source_skill": args.source_skill,
        "source_ref": args.source_ref,
    }
    validate_todo(todo)

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        todos = data.setdefault("todos", [])
        if not isinstance(todos, list):
            raise ValueError("todos.yaml 'todos' must be a list")
        todos.append(todo)
        return todo

    return mutate_kv("todos", _mutate, action="add_todo")


def cmd_complete(args: argparse.Namespace) -> dict[str, Any]:
    configure(args.config)

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        todo = find_todo(data, args.id)
        todo["status"] = "done"
        todo["completed"] = args.completed or today()
        validate_todo(todo)
        return todo

    return mutate_kv("todos", _mutate, action="complete_todo")


def cmd_defer(args: argparse.Namespace) -> dict[str, Any]:
    configure(args.config)

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        todo = find_todo(data, args.id)
        todo["due"] = args.to
        todo["status"] = "open"
        todo["completed"] = None
        validate_todo(todo)
        return todo

    return mutate_kv("todos", _mutate, action="defer_todo")


def cmd_cancel(args: argparse.Namespace) -> dict[str, Any]:
    configure(args.config)

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        todo = find_todo(data, args.id)
        todo["status"] = "cancelled"
        validate_todo(todo)
        return todo

    return mutate_kv("todos", _mutate, action="cancel_todo")


def sort_key(todo: dict[str, Any]) -> tuple[int, str, str]:
    due = str(todo.get("due") or "9999-12-31")
    return (PRIORITY_ORDER.get(str(todo.get("priority")), 99), due, str(todo.get("id", "")))


def cmd_list(args: argparse.Namespace) -> list[dict[str, Any]]:
    configure(args.config)
    data = load_store("todos")
    records = [normalize(dict(t)) for t in data.get("todos", []) if isinstance(t, dict)]
    if args.status:
        records = [t for t in records if t.get("status") == args.status]
    if args.priority:
        records = [t for t in records if t.get("priority") == args.priority]
    if args.tag:
        records = [t for t in records if args.tag in (t.get("tags") or [])]
    return sorted(records, key=sort_key)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mutate/query Chief-of-Staff todos.yaml")
    parser.add_argument("--config", help="Path to company.yaml (or CHIEF_OF_STAFF_CONFIG)")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add")
    add.add_argument("--title", required=True)
    add.add_argument("--priority", choices=["high", "medium", "low"], default="medium")
    add.add_argument("--due")
    add.add_argument("--tags")
    add.add_argument("--source-skill")
    add.add_argument("--source-ref")

    complete = sub.add_parser("complete")
    complete.add_argument("--id", required=True)
    complete.add_argument("--completed")

    ls = sub.add_parser("list")
    ls.add_argument("--status", choices=["open", "done", "cancelled"])
    ls.add_argument("--priority", choices=["high", "medium", "low"])
    ls.add_argument("--tag")

    defer = sub.add_parser("defer")
    defer.add_argument("--id", required=True)
    defer.add_argument("--to", required=True)

    cancel = sub.add_parser("cancel")
    cancel.add_argument("--id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "add":
            result = cmd_add(args)
        elif args.command == "complete":
            result = cmd_complete(args)
        elif args.command == "defer":
            result = cmd_defer(args)
        elif args.command == "cancel":
            result = cmd_cancel(args)
        elif args.command == "list":
            result = cmd_list(args)
        else:  # pragma: no cover
            parser.error("unknown command")
            return 2
    except KeyError as exc:
        print(str(exc).strip("'"), file=sys.stderr)
        return 1
    except (RuntimeError, ValueError, SchemaError) as exc:
        print(f"todo.py error: {exc}", file=sys.stderr)
        return 1
    emit(result, args.json)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
