#!/usr/bin/env python3
"""Structured operational (runtime) logging for Chief-of-Staff, v0.3.4.

This module records *what the agent attempted* — provider calls, retries,
throttling, timings, outcomes — as newline-delimited JSON ("JSONL") under a
per-run directory. It is deliberately distinct from the **audit** log
(``audit_log.py`` / ``workspace_audit.py``):

    * audit log      = what *changed* (before/after state mutations)
    * operational log = what was *attempted* (this module)

The operational log is designed to be **safe to attach to a bug report by
default**: every string value is scrubbed for credentials, and known
sensitive keys are dropped or replaced before anything touches disk.

Layout / coexistence
---------------------
Runtime artifacts live under ``<project_root>/.runs/``. The existing
``run_log.py`` also uses ``.runs/`` but namespaces its files under
``.runs/<skill_name>/``. This module creates *per-run* directories named by a
distinctive run id::

    <project_root>/.runs/<YYYYMMDDTHHMMSSZ>-<6 hex>/
        events.jsonl     # one JSON object per line
        summary.json     # written on finish_run()

Because the run-id pattern (``YYYYMMDDTHHMMSSZ-<6hex>``) never collides with a
skill name, ``run_log.py`` and ``state_tools.py`` inspection of ``.runs/`` are
unaffected. ``prune_runs`` only ever removes directories matching that exact
pattern and never touches other entries in ``.runs/``.

Config
------
An optional ``logging`` block in ``company.yaml`` tunes behaviour::

    logging:
      level: INFO           # default file/console level (DEBUG/INFO/WARNING/ERROR)
      retention_days: 30    # prune_runs deletes run dirs older than this
      max_runs: 200         # prune_runs keeps at most this many run dirs

Cross-process propagation
--------------------------
Run context (run id, command, level, quiet) is held in ``contextvars`` so
nested/threaded calls resolve correctly within a process. The environment
variable ``CHIEF_OF_STAFF_RUN_ID`` is used *only* to propagate the active run
id to child processes so their events append to the same run.
"""

from __future__ import annotations

import argparse
import contextvars
import json
import os
import re
import secrets
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

try:  # Resolve project_root the same way the rest of the plugin does.
    from config_loader import get_project_root
except Exception:  # pragma: no cover - only when imported outside plugin path
    get_project_root = None  # type: ignore

# ─── Public constants ────────────────────────────────────────────────────────

REDACTED = "[redacted]"

RUN_ID_ENV = "CHIEF_OF_STAFF_RUN_ID"
LOG_LEVEL_ENV = "CHIEF_OF_STAFF_LOG_LEVEL"
PROJECT_ROOT_ENV = "CHIEF_OF_STAFF_PROJECT_ROOT"

DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_RUNS = 200

# Run-id: "YYYYMMDDTHHMMSSZ-<6 hex>". Used to name per-run dirs and to guard
# prune so it never deletes non-run entries in .runs/.
_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
_RUN_ID_TS_FMT = "%Y%m%dT%H%M%SZ"

# ─── Levels ──────────────────────────────────────────────────────────────────

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
_LEVEL_ALIASES = {"warn": "warning", "err": "error", "information": "info"}


def _normalize_level(level: str | None, default: str = "info") -> str:
    if not level:
        return default
    name = str(level).strip().lower()
    name = _LEVEL_ALIASES.get(name, name)
    return name if name in _LEVELS else default


def _level_num(level: str | None) -> int:
    return _LEVELS[_normalize_level(level)]


# ─── Run context (contextvars) ───────────────────────────────────────────────


class _RunContext:
    __slots__ = (
        "run_id",
        "command",
        "level",
        "quiet",
        "run_dir",
        "started_at",
        "counts",
        "first_error",
        "warnings",
        "owns_env",
    )

    def __init__(
        self,
        run_id: str,
        command: str,
        level: str,
        quiet: bool,
        run_dir: Path | None,
        owns_env: bool,
    ) -> None:
        self.run_id = run_id
        self.command = command
        self.level = level
        self.quiet = quiet
        self.run_dir = run_dir
        self.started_at = _now_iso()
        self.counts: dict[str, int] = {"error": 0, "warning": 0, "info": 0, "debug": 0}
        self.first_error: str | None = None
        self.warnings: list[str] = []
        self.owns_env = owns_env


_CURRENT: contextvars.ContextVar[_RunContext | None] = contextvars.ContextVar(
    "chief_of_staff_run", default=None
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _new_run_id() -> str:
    return f"{_now().strftime(_RUN_ID_TS_FMT)}-{secrets.token_hex(3)}"


# ─── Project root resolution ─────────────────────────────────────────────────


def _resolve_project_root(config: Mapping[str, Any] | None) -> Path | None:
    """Best-effort project-root resolution; returns None for console-only mode."""

    try:
        if config is not None and get_project_root is not None:
            root = get_project_root(config)
            if root is not None:
                return Path(root)
        env_root = os.getenv(PROJECT_ROOT_ENV)
        if env_root:
            return Path(env_root).expanduser().resolve()
        if config is not None:
            raw = config["paths"]["project_root"]  # type: ignore[index]
            return Path(str(raw)).expanduser().resolve()
    except Exception:
        return None
    return None


def _config_logging(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if config is None:
        return {}
    try:
        block = config.get("logging")  # type: ignore[union-attr]
    except Exception:
        return {}
    return block if isinstance(block, Mapping) else {}


# ─── Redaction (security-critical) ───────────────────────────────────────────

# Key substrings whose values become REDACTED (case-insensitive substring match).
_REDACT_KEY_SUBSTRINGS = (
    "authorization",
    "bearer",
    "token",
    "secret",
    "password",
    "client_secret",
    "api_key",
    "cookie",
    "set-cookie",
)

# Keys whose values are dropped entirely (case-insensitive exact match).
_DROP_KEYS = {"body", "payload", "content", "snippet", "email_body"}

# Reserved schema keys callers may not override via **fields.
_RESERVED_KEYS = {"timestamp", "level", "run_id", "command", "component", "event"}

_BEARER_RE = re.compile(r"(?i)\bbearer\s+\S+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_KV_SECRET_RE = re.compile(r"(?i)\b(client_secret|password|api_key)=\S+")


def _scrub_string(value: str) -> str:
    scrubbed = _BEARER_RE.sub("Bearer " + REDACTED, value)
    scrubbed = _JWT_RE.sub(REDACTED, scrubbed)
    scrubbed = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", scrubbed)
    return scrubbed


def _should_drop(key: str) -> bool:
    return key.strip().lower() in _DROP_KEYS


def _should_replace(key: str) -> bool:
    low = key.lower()
    return any(sub in low for sub in _REDACT_KEY_SUBSTRINGS)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, Mapping):
        return _redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


def _redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        skey = str(key)
        if _should_drop(skey):
            continue
        if _should_replace(skey):
            out[skey] = REDACTED
            continue
        out[skey] = _redact_value(value)
    return out


def _redact_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Redact caller-supplied structured fields, dropping reserved/forbidden keys."""

    out: dict[str, Any] = {}
    for key, value in fields.items():
        skey = str(key)
        if skey in _RESERVED_KEYS or _should_drop(skey):
            continue
        if _should_replace(skey):
            out[skey] = REDACTED
            continue
        out[skey] = _redact_value(value)
    return out


# ─── File / console output ───────────────────────────────────────────────────


def _write_event_line(run_dir: Path, record: Mapping[str, Any]) -> None:
    """Append one JSON line to events.jsonl. Never raises into the caller."""

    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "events.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    except Exception:
        # Operational logging must never break the caller; swallow IO errors.
        pass


def _console_line(record: Mapping[str, Any]) -> str:
    parts = [str(record.get("level", "info"))]
    component = record.get("component")
    if component:
        parts.append(str(component))
    parts.append(str(record.get("event", "")))
    message = None
    for key, value in record.items():
        if key in _RESERVED_KEYS:
            continue
        if key == "message":
            message = value
            continue
        parts.append(f"{key}={value}")
    if message is not None:
        parts.append(str(message))
    return " ".join(p for p in parts if p)


def _emit_console(record: Mapping[str, Any]) -> None:
    try:
        print(_console_line(record), file=sys.stderr)
    except Exception:
        pass


# ─── Public API ──────────────────────────────────────────────────────────────


def init_run(
    command: str,
    config: Mapping[str, Any] | None = None,
    *,
    level: str | None = None,
    quiet: bool = False,
) -> str:
    """Start (or join) a run and return its run id.

    If ``CHIEF_OF_STAFF_RUN_ID`` is set in the environment, the existing run is
    joined (events append to its ``events.jsonl``; no new directory is created).
    Otherwise a new run id is generated, ``<project_root>/.runs/<run_id>/`` is
    created, the env var is set for child processes, and ``run_started`` is
    emitted. If the project root cannot be resolved, the run operates in
    console-only mode and never raises.
    """

    resolved_level = _normalize_level(
        level
        or os.getenv(LOG_LEVEL_ENV)
        or _config_logging(config).get("level"),
        default="info",
    )

    project_root = _resolve_project_root(config)
    joined_id = os.getenv(RUN_ID_ENV)

    if joined_id:
        run_dir = (project_root / ".runs" / joined_id) if project_root else None
        ctx = _RunContext(
            run_id=joined_id,
            command=str(command),
            level=resolved_level,
            quiet=bool(quiet),
            run_dir=run_dir,
            owns_env=False,
        )
        _CURRENT.set(ctx)
        return joined_id

    run_id = _new_run_id()
    run_dir = None
    owns_env = False
    if project_root is not None:
        run_dir = project_root / ".runs" / run_id
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            run_dir = None
    # Propagate to child processes regardless (they may resolve their own root).
    try:
        os.environ[RUN_ID_ENV] = run_id
        owns_env = True
    except Exception:
        owns_env = False

    ctx = _RunContext(
        run_id=run_id,
        command=str(command),
        level=resolved_level,
        quiet=bool(quiet),
        run_dir=run_dir,
        owns_env=owns_env,
    )
    _CURRENT.set(ctx)

    log_event("run_started", level="info", component="runtime", command_line=str(command))
    return run_id


def log_event(
    event: str,
    *,
    level: str = "info",
    component: str | None = None,
    **fields: Any,
) -> None:
    """Append one structured event. No-ops (aside from console) when no run is active.

    Every string value is redacted and forbidden keys are dropped. This function
    never raises into the caller.
    """

    ctx = _CURRENT.get()
    norm_level = _normalize_level(level, default="info")
    event_num = _LEVELS[norm_level]

    # Determine threshold: active run's level, else default INFO.
    threshold = _level_num(ctx.level) if ctx is not None else _LEVELS["info"]
    if event_num < threshold:
        return

    record: dict[str, Any] = {
        "timestamp": _now_iso(),
        "level": norm_level,
        "run_id": ctx.run_id if ctx is not None else None,
        "command": ctx.command if ctx is not None else None,
        "component": component,
        "event": str(event),
    }
    record.update(_redact_fields(fields))
    # Drop keys whose value is None for a compact schema.
    record = {k: v for k, v in record.items() if v is not None}

    if ctx is not None:
        ctx.counts[norm_level] = ctx.counts.get(norm_level, 0) + 1
        msg = record.get("message")
        msg_text = str(msg) if msg is not None else str(event)
        if norm_level == "error" and ctx.first_error is None:
            ctx.first_error = msg_text
        if norm_level == "warning" and len(ctx.warnings) < 10:
            ctx.warnings.append(msg_text)
        if ctx.run_dir is not None:
            _write_event_line(ctx.run_dir, record)
        if not ctx.quiet:
            _emit_console(record)
    else:
        # No active run: still print to console at the default threshold.
        _emit_console(record)


def finish_run(outcome: str = "success", **summary_fields: Any) -> None:
    """Finish the active run: emit completion event and write ``summary.json``.

    ``outcome`` is one of ``"success"``, ``"degraded"``, ``"failed"``. When it is
    ``"failed"`` a ``run_failed`` event is emitted, otherwise ``run_completed``.
    Clears the run context (and unsets ``CHIEF_OF_STAFF_RUN_ID`` if this process
    set it). Never raises into the caller.
    """

    ctx = _CURRENT.get()
    if ctx is None:
        return

    outcome_str = str(outcome)
    if outcome_str not in {"success", "degraded", "failed"}:
        outcome_str = "success"

    event_name = "run_failed" if outcome_str == "failed" else "run_completed"
    log_event(event_name, level="info", component="runtime", outcome=outcome_str)

    finished_at = _now_iso()
    summary = {
        "run_id": ctx.run_id,
        "command": ctx.command,
        "started_at": ctx.started_at,
        "finished_at": finished_at,
        "outcome": outcome_str,
        "counts": dict(ctx.counts),
        "first_error": ctx.first_error,
        "warnings": list(ctx.warnings[:10]),
    }
    for key, value in _redact_fields(summary_fields).items():
        summary[key] = value

    if ctx.run_dir is not None:
        try:
            ctx.run_dir.mkdir(parents=True, exist_ok=True)
            path = ctx.run_dir / "summary.json"
            tmp = path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(summary, fh, indent=2, sort_keys=True, default=str)
                fh.write("\n")
            os.replace(tmp, path)
        except Exception:
            pass

    if ctx.owns_env and os.getenv(RUN_ID_ENV) == ctx.run_id:
        try:
            del os.environ[RUN_ID_ENV]
        except Exception:
            pass

    _CURRENT.set(None)


def current_run_id() -> str | None:
    """Return the active run id for this context, or None."""

    ctx = _CURRENT.get()
    return ctx.run_id if ctx is not None else None


def _run_dirs(runs_dir: Path) -> list[tuple[datetime, str, Path]]:
    """Return (timestamp, run_id, path) for run-id-shaped dirs under ``runs_dir``."""

    out: list[tuple[datetime, str, Path]] = []
    if not runs_dir.is_dir():
        return out
    for child in runs_dir.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if not _RUN_ID_RE.match(name):
            continue  # never touch non-run entries (e.g. run_log skill dirs)
        try:
            ts = datetime.strptime(name[:16], _RUN_ID_TS_FMT).replace(tzinfo=timezone.utc)
        except Exception:
            try:
                ts = datetime.fromtimestamp(child.stat().st_mtime, timezone.utc)
            except Exception:
                ts = _now()
        out.append((ts, name, child))
    return out


def prune_runs(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Delete run directories beyond retention/count bounds.

    Honors ``logging.retention_days`` (default 30) and ``logging.max_runs``
    (default 200). Only directories matching the run-id pattern are considered;
    other ``.runs/`` entries are never touched.
    """

    log_cfg = _config_logging(config)
    try:
        retention_days = int(log_cfg.get("retention_days", DEFAULT_RETENTION_DAYS))
    except Exception:
        retention_days = DEFAULT_RETENTION_DAYS
    try:
        max_runs = int(log_cfg.get("max_runs", DEFAULT_MAX_RUNS))
    except Exception:
        max_runs = DEFAULT_MAX_RUNS

    project_root = _resolve_project_root(config)
    if project_root is None:
        return {"removed": [], "kept": 0}

    runs_dir = project_root / ".runs"
    entries = _run_dirs(runs_dir)
    # Newest first.
    entries.sort(key=lambda item: (item[0], item[1]), reverse=True)

    cutoff = _now() - timedelta(days=retention_days)
    removed: list[str] = []
    kept = 0
    for index, (ts, run_id, path) in enumerate(entries):
        too_old = retention_days >= 0 and ts < cutoff
        beyond_count = max_runs >= 0 and index >= max_runs
        if too_old or beyond_count:
            try:
                shutil.rmtree(path)
                removed.append(run_id)
            except Exception:
                kept += 1
        else:
            kept += 1

    removed.sort()
    return {"removed": removed, "kept": kept}


def add_cli_args(parser: argparse.ArgumentParser) -> None:
    """Add ``--log-level`` and ``--quiet`` to an argparse parser."""

    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        default=None,
        help="Operational log level (default: config logging.level or INFO)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Silence console operational logging (file logging is unaffected)",
    )


__all__ = [
    "REDACTED",
    "init_run",
    "log_event",
    "finish_run",
    "current_run_id",
    "prune_runs",
    "add_cli_args",
]
