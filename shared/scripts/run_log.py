#!/usr/bin/env python3
"""Run logging for idempotent scheduled operations.

Usage:
    from run_log import record_run, last_run, was_run_today
    record_run("daily-briefing", status="delivered", sources={"gmail":"ok","calendar":"ok"}, errors=[])
    if was_run_today("daily-briefing"):
        print("Already ran today, skipping")
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    from config_loader import get_project_root, load_config
except Exception:  # pragma: no cover
    get_project_root = None  # type: ignore
    load_config = None  # type: ignore


class RunLogError(RuntimeError):
    """Raised when run logs cannot be written or parsed."""


def _project_root(config: Mapping[str, Any] | None = None) -> Path:
    env_root = os.getenv("CHIEF_OF_STAFF_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    cfg: Any = config
    if cfg is None and load_config is not None:
        cfg = load_config()
    if cfg is not None and get_project_root is not None:
        root = get_project_root(cfg)
        if root is not None:
            return root
    if cfg is not None:
        try:
            return Path(str(cfg["paths"]["project_root"])).expanduser().resolve()  # type: ignore[index]
        except Exception as exc:
            raise RunLogError(f"Cannot resolve paths.project_root: {exc}") from exc
    return Path.cwd().resolve()


def _safe_skill_name(skill_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(skill_name).strip()).strip("-.")
    if not safe:
        raise RunLogError("skill_name is required")
    return safe


def _run_dir(skill_name: str, config: Mapping[str, Any] | None = None) -> Path:
    return _project_root(config) / ".runs" / _safe_skill_name(skill_name)


def _hash_sources(sources: Mapping[str, Any]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for key, value in sources.items():
        payload = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
        hashes[str(key)] = hashlib.sha256(payload).hexdigest()
    return hashes


def record_run(
    skill_name: str,
    status: str,
    sources: Mapping[str, Any] | None = None,
    errors: list[Any] | None = None,
    actions: list[Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one run record under ``.runs/{skill_name}/``."""

    now = datetime.now(timezone.utc)
    sources = dict(sources or {})
    record = {
        "run_id": str(uuid.uuid4()),
        "skill": str(skill_name),
        "started_at": now.isoformat(),
        "completed_at": now.isoformat(),
        "status": str(status),
        "input_sources": sources,
        "source_hashes": _hash_sources(sources),
        "actions_taken": list(actions or []),
        "mutations": [],
        "delivery_status": str(status),
        "errors": list(errors or []),
    }
    run_dir = _run_dir(skill_name, config=config)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}.json"
    tmp = path.with_suffix(".json.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, sort_keys=True, default=str)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        raise RunLogError(f"Failed to record run {path}: {exc}") from exc
    return record


def _load_record(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise RunLogError(f"Run record {path} is not a JSON object")
    return data


def last_run(skill_name: str, config: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """Return the most recent run record for ``skill_name`` or None."""

    run_dir = _run_dir(skill_name, config=config)
    if not run_dir.exists():
        return None
    files = sorted(run_dir.glob("*.json"))
    if not files:
        return None
    return _load_record(files[-1])


def _completed_at(record: Mapping[str, Any]) -> datetime | None:
    raw = record.get("completed_at") or record.get("started_at")
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def was_run_today(skill_name: str, config: Mapping[str, Any] | None = None) -> bool:
    rec = last_run(skill_name, config=config)
    dt = _completed_at(rec or {})
    return bool(dt and dt.date() == datetime.now(timezone.utc).date())


def was_run_this_week(skill_name: str, config: Mapping[str, Any] | None = None) -> bool:
    rec = last_run(skill_name, config=config)
    dt = _completed_at(rec or {})
    if not dt:
        return False
    return dt.isocalendar()[:2] == datetime.now(timezone.utc).isocalendar()[:2]


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read or write Chief-of-Staff run logs")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="Record a run")
    rec.add_argument("skill")
    rec.add_argument("--status", required=True)
    last = sub.add_parser("last", help="Print last run")
    last.add_argument("skill")
    args = parser.parse_args(argv)
    if args.command == "record":
        print(json.dumps(record_run(args.skill, args.status), indent=2))
    else:
        print(json.dumps(last_run(args.skill), indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
