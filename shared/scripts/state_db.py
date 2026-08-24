#!/usr/bin/env python3
"""SQLite WAL state store — single DB for all Chief-of-Staff state.

Replaces state_store.py, pending_actions.py, event_store.py, and
webhook_security.py replay cache with ACID transactions.

Usage:
    from state_db import StateDB
    db = StateDB(config)           # opens {project_root}/state.db
    db.put_kv("pipeline", data)   # store pipeline as JSON
    data = db.get_kv("pipeline")  # load pipeline
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

from audit_log import append_audit
from schemas import SchemaError, validate_store

try:
    from config_loader import get_project_root as _config_project_root
    from config_loader import load_config
except Exception:  # pragma: no cover
    _config_project_root = None  # type: ignore
    load_config = None  # type: ignore

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None  # type: ignore


# ─── Constants ────────────────────────────────────────────────

KV_ROOT_KEY = "__root__"
EXPIRY_HOURS = 72
APPROVED_EXPIRY_HOURS = 24
MAX_RETRIES = 3
REPLAY_TTL_SECONDS = 3600 * 24
PROCESSING_LEASE_SECONDS = 300
MAX_BACKUPS = 20
MAX_BACKUP_DAYS = 30
KNOWN_SAFE_DOMAINS: set[str] = set()

EMPTY_TEMPLATES: dict[str, dict[str, list[Any]]] = {
    "pipeline": {"deals": []},
    "invoices": {"invoices": []},
    "expenses": {"expenses": []},
    "todos": {"todos": []},
}

EVENT_CATEGORIES = {
    "email_received": {
        "label": "Email received",
        "suggested_actions": ["gmail.search", "gmail.draft"],
        "destructive": False,
    },
    "email_urgent": {
        "label": "Urgent email",
        "suggested_actions": ["gmail.search", "gmail.draft", "gmail.send"],
        "destructive": False,
    },
    "calendar_changed": {
        "label": "Calendar event changed",
        "suggested_actions": ["calendar.list"],
        "destructive": False,
    },
    "calendar_cancelled": {
        "label": "Calendar event cancelled",
        "suggested_actions": ["calendar.list"],
        "destructive": False,
    },
    "deadline_approaching": {
        "label": "Deadline approaching",
        "suggested_actions": ["calendar.list", "drive.search"],
        "destructive": False,
    },
    "document_shared": {
        "label": "Document shared",
        "suggested_actions": ["drive.search", "drive.download"],
        "destructive": False,
    },
    "unknown": {
        "label": "Unclassified event",
        "suggested_actions": [],
        "destructive": False,
    },
}

_PREVIOUS_STATES: dict[str, tuple[str, ...]] = {
    "approved": ("requested", "executing"),
    "executing": ("approved",),
    "executed": ("executing",),
    "cancelled": ("requested", "approved", "expired", "executing", "failed"),
    "dismissed": ("requested", "approved", "expired"),
    "expired": ("requested", "approved"),
    "failed": ("executing",),
}

# Column names that _cas_update may interpolate into SET clauses.
_ALLOWED_CAS_COLUMNS: frozenset[str] = frozenset({
    "type",
    "provider",
    "target",
    "payload",
    "summary",
    "state",
    "risk",
    "approver",
    "approval_reason",
    "created_at",
    "approved_at",
    "executing_at",
    "executed_at",
    "cancelled_at",
    "dismissed_at",
    "expired_at",
    "failed_at",
    "cancel_reason",
    "dismiss_reason",
    "retry_count",
    "last_error",
    "result",
})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kv_stores (
    store_name TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (store_name, key)
);

CREATE TABLE IF NOT EXISTS pending_actions (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    provider TEXT NOT NULL,
    target TEXT NOT NULL,
    payload TEXT NOT NULL,
    summary TEXT,
    state TEXT NOT NULL,
    risk TEXT,
    approver TEXT,
    approval_reason TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    executing_at TEXT,
    executed_at TEXT,
    cancelled_at TEXT,
    dismissed_at TEXT,
    expired_at TEXT,
    failed_at TEXT,
    cancel_reason TEXT,
    dismiss_reason TEXT,
    retry_count INTEGER DEFAULT 0,
    last_error TEXT,
    result TEXT
);
CREATE INDEX IF NOT EXISTS idx_pa_state ON pending_actions(state);
CREATE INDEX IF NOT EXISTS idx_pa_created ON pending_actions(created_at);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    summary TEXT,
    state TEXT NOT NULL,
    classification TEXT,
    received_at TEXT NOT NULL,
    classified_at TEXT,
    surfaced_at TEXT,
    processed_at TEXT,
    processed_by TEXT,
    processing_notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_ev_state ON events(state);
CREATE INDEX IF NOT EXISTS idx_ev_received ON events(received_at);

CREATE TABLE IF NOT EXISTS webhook_replay (
    delivery_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    ts REAL NOT NULL,
    lease_token TEXT
);
CREATE INDEX IF NOT EXISTS idx_wr_ts ON webhook_replay(ts);

CREATE TABLE IF NOT EXISTS migration_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    migrated_at TEXT NOT NULL,
    sources TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1)
);
"""


# ─── Exceptions ───────────────────────────────────────────────

class StateStoreError(RuntimeError):
    """Raised for state-store IO or project-root failures."""


class StateCorruptionError(Exception):
    """Raised when a state file or database exists but cannot be parsed."""


class ConcurrencyError(Exception):
    """Raised when optimistic version check fails (compat shim)."""


T = TypeVar("T")


# ─── Helpers ──────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plain(value: Any) -> Any:
    if hasattr(value, "to_plain_dict"):
        return value.to_plain_dict()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(raw: Any, default: Any = None) -> Any:
    """Parse persisted JSON.

    ``default`` is used only for genuinely missing values (SQL NULL). Malformed
    JSON is corruption and raises ``StateCorruptionError``.
    """
    if raw is None:
        return default
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise StateCorruptionError(f"Corrupt JSON in persisted state: {exc}") from exc


_BUSY_RETRIES = 3
_TOKEN_OMITTED: Any = object()


def _is_lock_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "busy" in msg or "locked" in msg


def _begin_immediate(conn: sqlite3.Connection, retries: int = _BUSY_RETRIES) -> None:
    """Acquire a reserved write lock. Retry SQLITE_BUSY/LOCKED; then raise.

    Does not swallow failures. ``BEGIN IMMEDIATE`` must succeed (or already be
    held) before the caller reads any version/row it intends to overwrite.
    """
    delay = 0.02
    last_exc: BaseException | None = None
    for attempt in range(max(1, retries)):
        try:
            if conn.in_transaction:
                # A leftover deferred transaction does not hold a reserved lock.
                conn.rollback()
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            last_exc = exc
            msg = str(exc).lower()
            if "within a transaction" in msg:
                # Already in a transaction on this connection.
                return
            if _is_lock_error(exc) and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            if _is_lock_error(exc):
                raise ConcurrencyError(f"Could not acquire write lock: {exc}") from exc
            raise
    raise ConcurrencyError(f"Could not acquire write lock: {last_exc}") from last_exc


class DeliveryReservation(tuple):
    """``(ok, reason)`` for backward-compatible unpacking, plus ``lease_token``.

    Contract tests unpack two values. Token-aware callers read ``.lease_token``.
    """

    def __new__(cls, ok: bool, reason: str, lease_token: str | None = None) -> DeliveryReservation:
        inst = super().__new__(cls, (ok, reason))
        inst._lease_token = lease_token  # type: ignore[attr-defined]
        return inst

    @property
    def lease_token(self) -> str | None:
        return self._lease_token  # type: ignore[attr-defined]

    def __getitem__(self, index: int | slice) -> Any:  # type: ignore[override]
        if index == 2:
            return self.lease_token
        return tuple.__getitem__(self, index)


def _log_event(event: str, **fields: Any) -> None:
    try:
        from runtime_log import log_event
        log_event(event, **fields)
    except Exception:  # pragma: no cover
        pass


def _get_default_project_root_fallback() -> Path:
    env = os.getenv("CHIEF_OF_STAFF_HERMES_HOME") or os.getenv("HERMES_HOME")
    home = Path(env).expanduser() if env else Path.home() / ".hermes"
    return home / "projects" / "default"


def _project_root(config: Any) -> Path:
    """Resolve project root. Raises if neither config nor env provides it."""
    root = None
    if isinstance(config, Mapping):
        paths = config.get("paths", {})
        if isinstance(paths, Mapping):
            root = paths.get("project_root")
    if not root:
        root = os.getenv("CHIEF_OF_STAFF_PROJECT_ROOT")
    if not root:
        raise RuntimeError(
            "Missing project root: set paths.project_root in config or "
            "CHIEF_OF_STAFF_PROJECT_ROOT"
        )
    return Path(str(root)).expanduser()


def _db_root(config: Any) -> Path:
    """Resolve project root for StateDB.

    Prefer an explicit ``paths.project_root``. When config is omitted (or has
    no project_root), fall back to env / ``load_config()`` so callers that
    only set ``CHIEF_OF_STAFF_CONFIG`` still work.
    """
    if isinstance(config, Mapping):
        paths = config.get("paths")
        if isinstance(paths, Mapping) and paths.get("project_root"):
            return _project_root(config)
    try:
        return _resolve_project_root(config if isinstance(config, Mapping) else None)
    except StateStoreError:
        return _project_root(config)


def _resolve_project_root(config: Mapping[str, Any] | None = None) -> Path:
    env_root = os.getenv("CHIEF_OF_STAFF_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    cfg: Any = config
    if cfg is None and load_config is not None:
        cfg = load_config()
    if cfg is not None:
        if _config_project_root is not None:
            root = _config_project_root(cfg)
            if root is not None:
                return root
        try:
            raw = cfg["paths"]["project_root"]  # type: ignore[index]
            return Path(str(raw)).expanduser().resolve()
        except Exception as exc:
            raise StateStoreError(f"Cannot resolve paths.project_root from config: {exc}") from exc
    raise StateStoreError(
        "Missing project root: pass config, set CHIEF_OF_STAFF_CONFIG, or set CHIEF_OF_STAFF_PROJECT_ROOT"
    )


def _pending_path(config: Any) -> Path:
    """Compat: previously the JSON file; now the SQLite database path."""
    return _project_root(config) / "state.db"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _is_expired(action: dict[str, Any], expiry_hours: int = EXPIRY_HOURS) -> bool:
    if action.get("state") != "requested":
        return False
    dt = _parse_ts(action.get("created_at", ""))
    if dt is None:
        return False
    return datetime.now(timezone.utc) - dt > timedelta(hours=expiry_hours)


def _is_approval_lapsed(action: dict[str, Any], expiry_hours: int = APPROVED_EXPIRY_HOURS) -> bool:
    if action.get("state") != "approved":
        return False
    dt = _parse_ts(action.get("approved_at", ""))
    if dt is None:
        return False
    return datetime.now(timezone.utc) - dt > timedelta(hours=expiry_hours)


def _template(store_name: str) -> dict[str, Any]:
    return copy.deepcopy(EMPTY_TEMPLATES.get(store_name, {}))


def _fill_required_store_fields(store_name: str, data: dict[str, Any]) -> None:
    if store_name != "pipeline":
        return
    deals = data.get("deals")
    if not isinstance(deals, list):
        return
    for deal in deals:
        if not isinstance(deal, dict):
            continue
        if not deal.get("client_name"):
            deal["client_name"] = str(deal.get("name") or deal.get("id") or "unknown")
        if not deal.get("stage"):
            deal["stage"] = "Lead"


def classify_recipient_risk(recipient: str, config: Any | None = None) -> dict[str, str]:
    """Classify the risk of an email recipient."""
    domain = ""
    if "@" in recipient:
        domain = recipient.split("@", 1)[1].lower()

    if not domain:
        return {"level": "unknown", "domain": "", "reason": "Invalid or missing email domain"}

    if config and isinstance(config, Mapping):
        company = config.get("company", {})
        if isinstance(company, Mapping):
            company_domain = str(company.get("website", "")).lower()
            if company_domain:
                if "://" in company_domain:
                    company_domain = company_domain.split("://", 1)[1]
                company_domain = company_domain.rstrip("/").split("/")[0]
                if domain == company_domain or domain.endswith("." + company_domain):
                    return {
                        "level": "internal",
                        "domain": domain,
                        "reason": f"Same domain as company ({company_domain})",
                    }
        google = config.get("google", {})
        if isinstance(google, Mapping):
            google_domain = str(google.get("domain", "")).lower()
            if google_domain and google_domain == domain:
                return {
                    "level": "internal",
                    "domain": domain,
                    "reason": f"Same domain as Google workspace ({google_domain})",
                }

    if domain in KNOWN_SAFE_DOMAINS:
        return {
            "level": "external",
            "domain": domain,
            "reason": f"External but known domain ({domain})",
        }

    return {
        "level": "external",
        "domain": domain,
        "reason": f"External domain ({domain}) — verify recipient before approving",
    }


def classify_event(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Classify an inbound event and determine suggested actions."""
    category = EVENT_CATEGORIES.get(event_type, EVENT_CATEGORIES["unknown"])
    return {
        "category": event_type if event_type in EVENT_CATEGORIES else "unknown",
        "label": category["label"],
        "suggested_actions": category["suggested_actions"],
        "auto_execute": False,
        "destructive": category["destructive"],
    }


def _row_to_action(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    d = dict(row)
    d["payload"] = _loads(d.get("payload"), {})
    d["risk"] = _loads(d.get("risk"), None)
    d["result"] = _loads(d.get("result"), None)
    if d.get("retry_count") is None:
        d["retry_count"] = 0
    return d


def _row_to_event(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    d = dict(row)
    d["payload"] = _loads(d.get("payload"), {})
    d["classification"] = _loads(d.get("classification"), None)
    return d


def _action_insert_params(action: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        action["id"],
        action.get("type") or "",
        action.get("provider") or "",
        action.get("target") or "",
        _dumps(action.get("payload") if action.get("payload") is not None else {}),
        action.get("summary"),
        action.get("state") or "requested",
        _dumps(action["risk"]) if action.get("risk") is not None else None,
        action.get("approver"),
        action.get("approval_reason"),
        action.get("created_at") or _now(),
        action.get("approved_at"),
        action.get("executing_at"),
        action.get("executed_at"),
        action.get("cancelled_at"),
        action.get("dismissed_at"),
        action.get("expired_at"),
        action.get("failed_at"),
        action.get("cancel_reason"),
        action.get("dismiss_reason"),
        int(action.get("retry_count") or 0),
        action.get("last_error"),
        _dumps(action["result"]) if action.get("result") is not None else None,
    )


_ACTION_INSERT_SQL = """
INSERT OR REPLACE INTO pending_actions (
    id, type, provider, target, payload, summary, state, risk,
    approver, approval_reason, created_at, approved_at, executing_at,
    executed_at, cancelled_at, dismissed_at, expired_at, failed_at,
    cancel_reason, dismiss_reason, retry_count, last_error, result
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

_EVENT_INSERT_SQL = """
INSERT INTO events (
    id, key, source, source_id, event_type, payload, summary, state,
    classification, received_at, classified_at, surfaced_at,
    processed_at, processed_by, processing_notes
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def _event_insert_params(event: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        event["id"],
        event.get("key") or "",
        event.get("source") or "",
        event.get("source_id") or "",
        event.get("event_type") or "",
        _dumps(event.get("payload") if event.get("payload") is not None else {}),
        event.get("summary"),
        event.get("state") or "classified",
        _dumps(event["classification"]) if event.get("classification") is not None else None,
        event.get("received_at") or _now(),
        event.get("classified_at"),
        event.get("surfaced_at"),
        event.get("processed_at"),
        event.get("processed_by"),
        event.get("processing_notes"),
    )


def _audit_action(config: Any, action: Mapping[str, Any], status: str, extra: dict[str, Any] | None = None) -> None:
    try:
        from workspace_audit import audit_workspace_action
        payload = {"action_id": action.get("id", "")}
        if extra:
            payload.update(extra)
        audit_workspace_action(
            config,
            action.get("provider", ""),
            action.get("type", ""),
            "pending",
            target=str(action.get("target", "")),
            status=status,
            extra=payload,
        )
    except Exception:
        pass


# ─── StateDB ──────────────────────────────────────────────────

class StateDB:
    """SQLite WAL state store. One connection per instance."""

    def __init__(self, config: Any):
        self.config = config
        self.root = _db_root(config)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "state.db"
        try:
            self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout=10000")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(_SCHEMA_SQL)
            self.conn.execute("DROP INDEX IF EXISTS idx_ev_key")
            self._ensure_column("pending_actions", "failed_at", "TEXT")
            self.conn.execute("INSERT OR IGNORE INTO audit_lock (id) VALUES (1)")
            self.conn.commit()
            # BEGIN IMMEDIATE so two writers cannot both read version N and
            # last-writer-wins on a full-table replace (compat _load/_save).
            self.conn.isolation_level = "IMMEDIATE"
        except sqlite3.Error as exc:
            msg = str(exc).lower()
            if "locked" in msg or "busy" in msg:
                raise
            raise StateCorruptionError(f"state.db is corrupt or unreadable: {exc}") from exc
        # Idempotent: run on every open so a crash between commit and
        # legacy-file rename cannot skip remaining sources.
        self._migrate_legacy()

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        cols = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self) -> StateDB:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ── version helpers (compat with _load/_save) ──

    def _get_meta(self, key: str, default: int = 0) -> int:
        row = self.conn.execute(
            "SELECT value FROM kv_stores WHERE store_name=? AND key=?",
            ("_meta", key),
        ).fetchone()
        if not row:
            return default
        try:
            return int(json.loads(row["value"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            try:
                return int(row["value"])
            except (TypeError, ValueError):
                return default

    def _set_meta(self, key: str, value: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO kv_stores (store_name, key, value, updated_at) VALUES (?,?,?,?)",
            ("_meta", key, json.dumps(value), _now()),
        )

    def _bump_meta(self, key: str) -> int:
        current = self._get_meta(key, 0)
        new = current + 1
        self._set_meta(key, new)
        return new

    # ── KV stores ──

    def get_kv(self, store_name: str) -> dict | None:
        row = self.conn.execute(
            "SELECT value FROM kv_stores WHERE store_name=? AND key=?",
            (store_name, KV_ROOT_KEY),
        ).fetchone()
        if row is None:
            return None
        data = _loads(row["value"])
        if not isinstance(data, dict):
            raise StateCorruptionError(
                f"KV store {store_name!r} is not a JSON object"
            )
        # Structural schema check — a non-list records field is corruption,
        # not a missing store. Per-record validation stays with mutate/save
        # so incomplete documents can still be read (contract roundtrip).
        key = {
            "pipeline": "deals",
            "invoices": "invoices",
            "expenses": "expenses",
            "todos": "todos",
        }.get(store_name)
        if key is not None:
            records = data.get(key, [])
            if records is not None and not isinstance(records, list):
                raise SchemaError(f"{key}: must be a list")
        return data

    def put_kv(
        self,
        store_name: str,
        data: dict,
        action: str | None = None,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        actor: str = "agent",
    ) -> None:
        plain = _plain(dict(data))
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO kv_stores (store_name, key, value, updated_at) VALUES (?,?,?,?)",
                (store_name, KV_ROOT_KEY, _dumps(plain), _now()),
            )
        if action:
            self._append_kv_audit(store_name, action, before, after if after is not None else plain, actor)

    def mutate_kv(
        self,
        store_name: str,
        mutate_fn: Callable[[dict[str, Any]], Any],
        *,
        _fill_defaults: bool = False,
    ) -> Any:
        """Atomically read → mutate → validate → write a KV store.

        Holds BEGIN IMMEDIATE for the entire sequence so two workers cannot
        both load the same document and last-writer-wins on ``__root__``.
        ``mutate_fn`` receives the current store dict (empty template if
        missing) and may mutate it in place. Its return value is returned
        to the caller.
        """
        _begin_immediate(self.conn)
        try:
            row = self.conn.execute(
                "SELECT value FROM kv_stores WHERE store_name=? AND key=?",
                (store_name, KV_ROOT_KEY),
            ).fetchone()
            if row is None:
                data: dict[str, Any] = _template(store_name)
            else:
                loaded = _loads(row["value"])
                if not isinstance(loaded, dict):
                    raise StateCorruptionError(
                        f"KV store {store_name!r} is not a JSON object"
                    )
                data = loaded
            result = mutate_fn(data)
            if not isinstance(data, dict):
                raise TypeError("mutate_fn must keep the KV store as a dict")
            if _fill_defaults:
                _fill_required_store_fields(store_name, data)
            validate_store(store_name, data, config=self.config)
            self.conn.execute(
                "INSERT OR REPLACE INTO kv_stores (store_name, key, value, updated_at) VALUES (?,?,?,?)",
                (store_name, KV_ROOT_KEY, _dumps(_plain(data)), _now()),
            )
            self.conn.commit()
            return result
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass
            raise

    @contextlib.contextmanager
    def audit_lock(self) -> Iterator[None]:
        """Serialize hash-chain audit appends with BEGIN EXCLUSIVE."""
        delay = 0.02
        last_exc: BaseException | None = None
        acquired = False
        for attempt in range(_BUSY_RETRIES):
            try:
                if self.conn.in_transaction:
                    self.conn.rollback()
                self.conn.execute("BEGIN EXCLUSIVE")
                acquired = True
                break
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if _is_lock_error(exc) and attempt < _BUSY_RETRIES - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                if _is_lock_error(exc):
                    raise ConcurrencyError(f"Could not acquire audit lock: {exc}") from exc
                raise
        if not acquired:
            raise ConcurrencyError(f"Could not acquire audit lock: {last_exc}") from last_exc
        try:
            yield
            self.conn.commit()
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass
            raise

    def _append_kv_audit(
        self,
        store_name: str,
        action: str,
        before: Mapping[str, Any] | None,
        after: Mapping[str, Any],
        actor: str,
    ) -> None:
        strict_stores = [s.strip() for s in os.getenv("CHIEF_OF_STAFF_AUDIT_STRICT", "").split(",") if s.strip()]
        try:
            append_audit(
                store_name,
                action=action or "save",
                before=dict(before or {}),
                after=dict(after),
                actor=actor,
                config=self.config,
            )
        except Exception as audit_exc:
            if store_name in strict_stores:
                raise StateStoreError(
                    f"Mutation succeeded but audit log failed (strict mode for {store_name}): {audit_exc}"
                ) from audit_exc
            print(
                f"Warning: audit log write failed for {store_name} (mutation succeeded): {audit_exc}",
                file=sys.stderr,
            )

    # ── Pending actions ──

    def create_action(
        self,
        type: str,  # noqa: A002 — matches contract tests
        provider: str,
        target: str,
        payload: dict,
        summary: str | None = None,
    ) -> dict | None:
        action_id = str(uuid.uuid4())[:12]
        risk = None
        if type in ("gmail.send", "mail.send"):
            risk = classify_recipient_risk(target, self.config)
        action = {
            "id": action_id,
            "type": type,
            "provider": provider,
            "target": target,
            "payload": payload if payload is not None else {},
            "summary": summary or f"{type} to {target}",
            "state": "requested",
            "created_at": _now(),
            "approved_at": None,
            "executing_at": None,
            "executed_at": None,
            "cancelled_at": None,
            "dismissed_at": None,
            "expired_at": None,
            "failed_at": None,
            "result": None,
            "approver": None,
            "approval_reason": None,
            "risk": risk,
            "retry_count": 0,
            "last_error": None,
            "cancel_reason": None,
            "dismiss_reason": None,
        }
        with self.conn:
            self.conn.execute(_ACTION_INSERT_SQL, _action_insert_params(action))
            self._bump_meta("pending_actions_version")
        _audit_action(self.config, action, "requested", {"risk_level": risk["level"]} if risk else None)
        _log_event(
            "action_requested",
            level="info",
            component="pending_actions",
            action_id=action_id,
            action_type=type,
            provider=provider,
            target=target,
        )
        return self.get_action(action_id)

    def get_action(self, action_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM pending_actions WHERE id=?", (action_id,)
        ).fetchone()
        return _row_to_action(row) if row else None

    def list_actions(self, state: str | None = None, include_expired: bool = True) -> list[dict]:
        if state:
            rows = self.conn.execute(
                "SELECT * FROM pending_actions WHERE state=? ORDER BY created_at ASC",
                (state,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM pending_actions ORDER BY created_at ASC"
            ).fetchall()
        actions = [_row_to_action(r) for r in rows]
        if state == "requested" and not include_expired:
            actions = [a for a in actions if not _is_expired(a)]
        return actions

    def transition_action(self, action_id: str, new_state: str, **fields: Any) -> dict | None:
        current = self.get_action(action_id)
        if current is None:
            return None
        requested_state = new_state
        allowed = _PREVIOUS_STATES.get(requested_state)
        if allowed is not None and current["state"] not in allowed:
            return None

        if requested_state == "approved" and current["state"] == "requested" and _is_expired(current):
            self._cas_update(action_id, "requested", "expired", expired_at=_now())
            _audit_action(self.config, current, "expired")
            return None

        if requested_state == "executing" and _is_approval_lapsed(current):
            self._cas_update(action_id, "approved", "expired", expired_at=_now())
            _audit_action(self.config, current, "expired", {"reason": "approval_lapsed"})
            return None

        extra: dict[str, Any] = {}
        now = _now()
        if requested_state == "failed":
            retry_count = int(current.get("retry_count") or 0) + 1
            extra["retry_count"] = retry_count
            if "last_error" in fields:
                extra["last_error"] = fields["last_error"]
            elif "error" in fields:
                extra["last_error"] = fields["error"]
            if retry_count >= MAX_RETRIES:
                extra["failed_at"] = now
                new_state = "failed"
            else:
                # Below the cap: return to approved for another attempt.
                # Do not refresh approved_at — keep the original approval.
                new_state = "approved"
        elif new_state == "approved":
            extra["approved_at"] = now
            if "approver" in fields:
                extra["approver"] = fields["approver"]
            if "reason" in fields:
                extra["approval_reason"] = fields["reason"]
            if "approval_reason" in fields:
                extra["approval_reason"] = fields["approval_reason"]
        elif new_state == "executing":
            extra["executing_at"] = now
        elif new_state == "executed":
            extra["executed_at"] = now
            if "result" in fields:
                extra["result"] = fields["result"]
        elif new_state == "cancelled":
            extra["cancelled_at"] = now
            if fields.get("reason") or fields.get("cancel_reason"):
                extra["cancel_reason"] = fields.get("cancel_reason") or fields.get("reason")
        elif new_state == "dismissed":
            extra["dismissed_at"] = now
            extra["dismiss_reason"] = (
                fields.get("dismiss_reason")
                if fields.get("dismiss_reason") is not None
                else (fields.get("reason") if fields.get("reason") is not None else "No dismiss reason provided")
            )
        elif new_state == "expired":
            extra["expired_at"] = now

        for key in ("last_error", "retry_count", "result", "executing_at"):
            if key in fields and key not in extra:
                extra[key] = fields[key]

        updated = self._cas_update(action_id, current["state"], new_state, **extra)
        if updated is None:
            return None
        audit_extra: dict[str, Any] = {}
        if new_state == "approved":
            audit_extra["approver"] = updated.get("approver") or ""
            audit_extra["approval_reason"] = updated.get("approval_reason") or ""
            if requested_state == "failed":
                audit_extra["error"] = updated.get("last_error") or ""
        elif new_state == "executed":
            result = updated.get("result") or {}
            audit_extra["result_success"] = result.get("success", False) if isinstance(result, dict) else False
            audit_extra["approver"] = updated.get("approver") or ""
        elif new_state == "cancelled":
            audit_extra["cancel_reason"] = updated.get("cancel_reason") or ""
        elif new_state == "dismissed":
            audit_extra["dismiss_reason"] = updated.get("dismiss_reason") or ""
        elif new_state == "failed":
            audit_extra["error"] = updated.get("last_error") or ""
        _audit_action(self.config, updated, new_state, audit_extra or None)
        if new_state == "executed":
            _log_event(
                "action_executed",
                level="info",
                component="pending_actions",
                action_id=action_id,
                action_type=updated.get("type"),
                provider=updated.get("provider"),
            )
        elif requested_state == "failed":
            _log_event(
                "action_failed",
                level="warning",
                component="pending_actions",
                action_id=action_id,
                action_type=updated.get("type"),
                provider=updated.get("provider"),
                error=updated.get("last_error") or "",
            )
        return updated

    def _cas_update(self, action_id: str, expected_state: str, new_state: str, **fields: Any) -> dict | None:
        json_fields = {"payload", "risk", "result"}
        sets = ["state=?"]
        params: list[Any] = [new_state]
        for key, value in fields.items():
            if key not in _ALLOWED_CAS_COLUMNS:
                raise ValueError(f"Disallowed pending_actions column in CAS update: {key}")
            sets.append(f"{key}=?")
            if key in json_fields:
                params.append(_dumps(value) if value is not None else None)
            else:
                params.append(value)
        params.extend([action_id, expected_state])
        sql = f"UPDATE pending_actions SET {', '.join(sets)} WHERE id=? AND state=?"
        with self.conn:
            cur = self.conn.execute(sql, params)
            if cur.rowcount == 0:
                return None
            self._bump_meta("pending_actions_version")
        return self.get_action(action_id)

    # ── Events ──

    def ingest_event(
        self,
        source: str,
        source_id: str,
        event_type: str,
        payload: dict,
        summary: str | None = None,
    ) -> dict | None:
        key = f"{source}:{source_id}"
        classification = classify_event(event_type, payload or {})
        event_id = str(uuid.uuid4())[:12]
        now = _now()
        event = {
            "id": event_id,
            "key": key,
            "source": source,
            "source_id": source_id,
            "event_type": event_type,
            "payload": payload if payload is not None else {},
            "summary": summary or f"{event_type} from {source}",
            "state": "classified",
            "classification": classification,
            "received_at": now,
            "classified_at": now,
            "surfaced_at": None,
            "processed_at": None,
            "processed_by": None,
            "processing_notes": None,
        }
        try:
            with self.conn:
                self.conn.execute(_EVENT_INSERT_SQL, _event_insert_params(event))
                self._bump_meta("events_version")
        except sqlite3.IntegrityError:
            return None
        return self.get_event(event_id)

    def get_event(self, event_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return _row_to_event(row) if row else None

    def list_events(
        self,
        state: str | None = None,
        source: str | None = None,
        category: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        sql = "SELECT * FROM events WHERE 1=1"
        params: list[Any] = []
        if state:
            sql += " AND state=?"
            params.append(state)
        if source:
            sql += " AND source=?"
            params.append(source)
        sql += " ORDER BY received_at DESC"
        rows = self.conn.execute(sql, params).fetchall()
        events = [_row_to_event(r) for r in rows]
        if category:
            events = [e for e in events if (e.get("classification") or {}).get("category") == category]
        return events[:limit]

    def mark_event_processed(
        self,
        event_id: str,
        processed_by: str | None = None,
        notes: str | None = None,
    ) -> dict | None:
        with self.conn:
            cur = self.conn.execute(
                """UPDATE events SET state='processed', processed_at=?, processed_by=?, processing_notes=?
                   WHERE id=? AND state IN ('received','classified','surfaced')""",
                (_now(), processed_by, notes, event_id),
            )
            if cur.rowcount == 0:
                return None
            self._bump_meta("events_version")
        return self.get_event(event_id)

    def mark_event_surfaced(self, event_id: str) -> dict | None:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE events SET state='surfaced', surfaced_at=? WHERE id=? AND state='classified'",
                (_now(), event_id),
            )
            if cur.rowcount == 0:
                return None
            self._bump_meta("events_version")
        return self.get_event(event_id)

    # ── Webhook replay ──

    def reserve_delivery(self, delivery_id: str, ttl: int = REPLAY_TTL_SECONDS) -> DeliveryReservation:
        now = time.time()
        with self.conn:
            self.conn.execute("DELETE FROM webhook_replay WHERE ts < ?", (now - ttl,))
            row = self.conn.execute(
                "SELECT delivery_id, state, ts, lease_token FROM webhook_replay WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            if row:
                if row["state"] == "done":
                    return DeliveryReservation(False, "Replay detected: delivery already completed")
                if row["state"] == "processing":
                    age = now - (row["ts"] or 0)
                    if age < PROCESSING_LEASE_SECONDS:
                        return DeliveryReservation(False, "Replay detected: delivery already processing")
                    token = str(uuid.uuid4())
                    self.conn.execute(
                        "UPDATE webhook_replay SET state=?, ts=?, lease_token=? WHERE delivery_id=?",
                        ("processing", now, token, delivery_id),
                    )
                    return DeliveryReservation(True, "OK", token)
                return DeliveryReservation(False, "Replay detected")
            token = str(uuid.uuid4())
            self.conn.execute(
                "INSERT INTO webhook_replay (delivery_id, state, ts, lease_token) VALUES (?,?,?,?)",
                (delivery_id, "processing", now, token),
            )
            return DeliveryReservation(True, "OK", token)

    def complete_delivery(self, delivery_id: str, lease_token: str | None | object = _TOKEN_OMITTED) -> None:
        with self.conn:
            row = self.conn.execute(
                "SELECT lease_token FROM webhook_replay WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                return
            stored = row["lease_token"]
            if stored:
                if lease_token is _TOKEN_OMITTED:
                    pass  # legacy callers that omit the token
                elif lease_token is None or lease_token != stored:
                    return
            self.conn.execute(
                "UPDATE webhook_replay SET state=?, ts=? WHERE delivery_id=?",
                ("done", time.time(), delivery_id),
            )

    def release_delivery(self, delivery_id: str, lease_token: str | None | object = _TOKEN_OMITTED) -> None:
        with self.conn:
            row = self.conn.execute(
                "SELECT lease_token FROM webhook_replay WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                return
            stored = row["lease_token"]
            if stored:
                if lease_token is _TOKEN_OMITTED:
                    pass  # legacy callers that omit the token
                elif lease_token is None or lease_token != stored:
                    return
            self.conn.execute("DELETE FROM webhook_replay WHERE delivery_id=?", (delivery_id,))

    def renew_delivery(self, delivery_id: str, lease_token: str | None = None) -> bool:
        with self.conn:
            row = self.conn.execute(
                "SELECT state, lease_token FROM webhook_replay WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            if not row or row["state"] != "processing":
                return False
            stored = row["lease_token"]
            if stored:
                if lease_token is None or lease_token != stored:
                    return False
            elif lease_token:
                return False
            self.conn.execute(
                "UPDATE webhook_replay SET ts=? WHERE delivery_id=?",
                (time.time(), delivery_id),
            )
            return True

    def cleanup_expired_replay(self, ttl: int = REPLAY_TTL_SECONDS) -> None:
        cutoff = time.time() - ttl
        with self.conn:
            self.conn.execute("DELETE FROM webhook_replay WHERE ts < ?", (cutoff,))

    # ── Maintenance ──

    def cleanup_old_events(self, days: int = 30) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM events WHERE state='processed' AND processed_at IS NOT NULL AND processed_at < ?",
                (cutoff,),
            )
            return int(cur.rowcount or 0)

    def cleanup_old_actions(self, days: int = 30) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self.conn:
            cur = self.conn.execute(
                """DELETE FROM pending_actions
                   WHERE
                     (state = 'executed' AND executed_at IS NOT NULL AND executed_at < ?)
                     OR (state = 'cancelled' AND cancelled_at IS NOT NULL AND cancelled_at < ?)
                     OR (state = 'expired' AND expired_at IS NOT NULL AND expired_at < ?)
                     OR (state = 'dismissed' AND dismissed_at IS NOT NULL AND dismissed_at < ?)
                     OR (state = 'failed' AND COALESCE(failed_at, executing_at, created_at) IS NOT NULL
                         AND COALESCE(failed_at, executing_at, created_at) < ?)
                """,
                (cutoff, cutoff, cutoff, cutoff, cutoff),
            )
            return int(cur.rowcount or 0)

    # ── Migration ──

    def _migrated_sources(self) -> set[str]:
        """Return source names already recorded in migration_log (per-source)."""
        found: set[str] = set()
        try:
            rows = self.conn.execute("SELECT sources FROM migration_log").fetchall()
        except sqlite3.Error:
            return found
        for row in rows:
            raw = row["sources"]
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                found.add(str(raw))
                continue
            if isinstance(parsed, list):
                found.update(str(s) for s in parsed)
            else:
                found.add(str(parsed))
        return found

    def _record_migration_source(self, source: str) -> None:
        self.conn.execute(
            "INSERT INTO migration_log (migrated_at, sources) VALUES (?,?)",
            (_now(), json.dumps([source])),
        )

    def _rename_legacy(self, path: Path) -> None:
        dest = Path(str(path) + ".migrated")
        try:
            if path.exists() and not dest.exists():
                path.rename(dest)
        except OSError:
            try:
                path.rename(Path(str(path) + ".bak"))
            except OSError:
                pass

    def _migrate_legacy(self) -> None:
        root = self.root
        already = self._migrated_sources()
        to_rename: list[Path] = []

        pa_path = root / ".pending_actions.json"
        ev_path = root / ".events.json"
        wr_path = root / ".webhook_replay_cache.json"
        yaml_paths = [(name, root / f"{name}.yaml") for name in EMPTY_TEMPLATES]

        json_sources = [
            (pa_path, ".pending_actions.json"),
            (ev_path, ".events.json"),
            (wr_path, ".webhook_replay_cache.json"),
        ]

        def pending(path: Path, source: str) -> bool:
            return path.exists() and source not in already

        rename_only = [path for path, source in json_sources if path.exists() and source in already]
        has_work = (
            any(pending(path, source) for path, source in json_sources)
            or any(pending(ypath, ypath.name) for _, ypath in yaml_paths)
        )
        if not has_work:
            for path in rename_only:
                self._rename_legacy(path)
            return

        try:
            with self.conn:
                if pending(pa_path, ".pending_actions.json"):
                    try:
                        data = json.loads(pa_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError as exc:
                        raise StateCorruptionError(
                            f"Legacy state file is corrupt: {exc}"
                        ) from exc
                    actions = data.get("actions", {}) if isinstance(data, dict) else {}
                    if isinstance(actions, dict):
                        for action in actions.values():
                            if isinstance(action, dict) and action.get("id"):
                                self.conn.execute(_ACTION_INSERT_SQL, _action_insert_params(action))
                    if isinstance(data, dict) and "_version" in data:
                        self._set_meta("pending_actions_version", int(data.get("_version") or 0))
                    self._record_migration_source(".pending_actions.json")
                    to_rename.append(pa_path)

                if pending(ev_path, ".events.json"):
                    try:
                        data = json.loads(ev_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError as exc:
                        raise StateCorruptionError(
                            f"Legacy state file is corrupt: {exc}"
                        ) from exc
                    events = data.get("events", {}) if isinstance(data, dict) else {}
                    if isinstance(events, dict):
                        for event in events.values():
                            if isinstance(event, dict) and event.get("id"):
                                try:
                                    self.conn.execute(_EVENT_INSERT_SQL, _event_insert_params(event))
                                except sqlite3.IntegrityError:
                                    pass
                    if isinstance(data, dict) and "_version" in data:
                        self._set_meta("events_version", int(data.get("_version") or 0))
                    self._record_migration_source(".events.json")
                    to_rename.append(ev_path)

                if pending(wr_path, ".webhook_replay_cache.json"):
                    try:
                        data = json.loads(wr_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError as exc:
                        raise StateCorruptionError(
                            f"Legacy state file is corrupt: {exc}"
                        ) from exc
                    entries = data.get("entries", {}) if isinstance(data, dict) else {}
                    if isinstance(entries, dict):
                        for did, entry in entries.items():
                            if not isinstance(entry, dict):
                                continue
                            self.conn.execute(
                                "INSERT OR REPLACE INTO webhook_replay (delivery_id, state, ts, lease_token) VALUES (?,?,?,?)",
                                (
                                    str(did),
                                    entry.get("state") or "processing",
                                    float(entry.get("ts") or time.time()),
                                    entry.get("lease_token"),
                                ),
                            )
                    self._record_migration_source(".webhook_replay_cache.json")
                    to_rename.append(wr_path)

                if yaml is not None:
                    for store_name, ypath in yaml_paths:
                        if not pending(ypath, ypath.name):
                            continue
                        existing = self.conn.execute(
                            "SELECT 1 FROM kv_stores WHERE store_name=? AND key=?",
                            (store_name, KV_ROOT_KEY),
                        ).fetchone()
                        if existing:
                            # SQLite already owns this store; leftover YAML is a
                            # snapshot, not a source of truth.
                            self._record_migration_source(ypath.name)
                            continue
                        try:
                            loaded = yaml.safe_load(ypath.read_text(encoding="utf-8"))
                        except Exception as exc:
                            raise StateCorruptionError(
                                f"Legacy YAML {ypath.name} is corrupt: {exc}"
                            ) from exc
                        if loaded is None:
                            loaded = {}
                        if not isinstance(loaded, dict):
                            raise StateCorruptionError(
                                f"Legacy YAML {ypath.name} is corrupt: top-level value must be a mapping"
                            )
                        self.conn.execute(
                            "INSERT OR REPLACE INTO kv_stores (store_name, key, value, updated_at) VALUES (?,?,?,?)",
                            (store_name, KV_ROOT_KEY, _dumps(loaded), _now()),
                        )
                        self._record_migration_source(ypath.name)
                        to_rename.append(ypath)
        except json.JSONDecodeError as exc:
            raise StateCorruptionError(f"Legacy state file is corrupt: {exc}") from exc

        for path in to_rename:
            self._rename_legacy(path)
        for path in rename_only:
            self._rename_legacy(path)
        # Explicit human-readable snapshots (never read back as source of truth).
        if yaml is not None:
            for store_name, ypath in yaml_paths:
                if ypath not in to_rename:
                    continue
                row = self.conn.execute(
                    "SELECT value FROM kv_stores WHERE store_name=? AND key=?",
                    (store_name, KV_ROOT_KEY),
                ).fetchone()
                if not row:
                    continue
                try:
                    dumped = yaml.safe_dump(
                        _loads(row["value"]), sort_keys=False, allow_unicode=True
                    )
                    ypath.write_text(dumped, encoding="utf-8")
                except OSError:
                    pass


# ─── Connection helper ────────────────────────────────────────

@contextlib.contextmanager
def _open_db(config: Any) -> Iterator[StateDB]:
    db = StateDB(config)
    try:
        yield db
    finally:
        db.close()


# ─── KV compatibility (state_store) ───────────────────────────

def get_store_path(store_name: str, config: Mapping[str, Any] | None = None) -> Path:
    """Return ``{project_root}/{store_name}.yaml`` as an absolute path (compat)."""
    if not store_name or not str(store_name).strip():
        raise StateStoreError("store_name is required")
    if "/" in store_name or "\\" in store_name or store_name.startswith("."):
        raise StateStoreError(
            f"Invalid store_name {store_name!r}: must not contain path separators or start with '.'"
        )
    root = _resolve_project_root(config)
    result = root / f"{store_name}.yaml"
    try:
        result.resolve().relative_to(root.resolve())
    except ValueError:
        raise StateStoreError(f"store_name {store_name!r} resolves outside project root")
    return result


@contextlib.contextmanager
def with_store_lock(
    store_name: str,
    config: Mapping[str, Any] | None = None,
    timeout: float = 10,
) -> Iterator[Path]:
    """Deprecated: no-op lock. SQLite WAL no longer serializes KV read-modify-write.

    The previous file lock was deleted with the YAML store. This context
    manager remains so existing ``with with_store_lock(...)`` call sites
    keep importing, but it provides no isolation. Use ``mutate_kv()`` to
    wrap load → mutate → save in one ``BEGIN IMMEDIATE`` transaction.
    """
    yield get_store_path(store_name, config=config)


def load_store(
    store_name: str,
    config: Mapping[str, Any] | None = None,
    *,
    validate: bool = True,
) -> dict[str, Any]:
    """Load a KV store from SQLite, creating and returning its empty template if missing.

    YAML files are ingested once during migration and renamed to ``.migrated``.
    They are never read as authoritative state after that.
    """
    get_store_path(store_name, config=config)  # validate name
    with _open_db(config) as db:
        data = db.get_kv(store_name)
        if data is None:
            data = _template(store_name)
            if store_name in EMPTY_TEMPLATES:
                db.put_kv(store_name, data)
            return data
        if not data and store_name in EMPTY_TEMPLATES:
            return _template(store_name)
        if validate:
            validate_store(store_name, data, config=config)
        return data


def _backup_timestamp(path: Path, store_name: str) -> datetime:
    """Parse timestamp from ``{store}.{YYYYMMDDTHHMMSS.ffffffZ}.yaml``, else mtime."""
    name = path.name
    prefix = f"{store_name}."
    suffix = ".yaml"
    if name.startswith(prefix) and name.endswith(suffix):
        ts_str = name[len(prefix) : -len(suffix)]
        try:
            return datetime.strptime(ts_str, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _prune_backups(backup_dir: Path, store_name: str) -> None:
    """Keep at most MAX_BACKUPS recent files; drop backups older than MAX_BACKUP_DAYS."""
    files = list(backup_dir.glob(f"{store_name}.*.yaml"))
    if not files:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_BACKUP_DAYS)
    remaining: list[tuple[Path, datetime]] = []
    for path in files:
        ts = _backup_timestamp(path, store_name)
        if ts < cutoff:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        remaining.append((path, ts))
    remaining.sort(key=lambda item: item[1], reverse=True)
    for path, _ in remaining[MAX_BACKUPS:]:
        try:
            path.unlink()
        except OSError:
            pass


def _backup_yaml_and_prune(store_name: str, ypath: Path, root: Path) -> None:
    """Copy the previous YAML snapshot into ``.backups/`` and prune old copies."""
    backup_dir = root / ".backups"
    if ypath.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        dest = backup_dir / f"{store_name}.{stamp}.yaml"
        try:
            shutil.copy2(ypath, dest)
        except OSError:
            pass
    if backup_dir.exists():
        _prune_backups(backup_dir, store_name)


def save_store_atomic(
    store_name: str,
    data: Mapping[str, Any],
    action: str | None = None,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    actor: str = "agent",
    config: Mapping[str, Any] | None = None,
    _fill_defaults: bool = False,
) -> Path:
    """Validate and write a KV store, then append audit if requested."""
    get_store_path(store_name, config=config)
    plain_data = _plain(dict(data))
    if _fill_defaults:
        _fill_required_store_fields(store_name, plain_data)
    validate_store(store_name, plain_data, config=config)
    with _open_db(config) as db:
        db.put_kv(store_name, plain_data)
        db_path = db.db_path
        if yaml is not None:
            try:
                ypath = get_store_path(store_name, config=config)
                ypath.parent.mkdir(parents=True, exist_ok=True)
                _backup_yaml_and_prune(store_name, ypath, ypath.parent)
                ypath.write_text(
                    yaml.safe_dump(plain_data, sort_keys=False, allow_unicode=True),
                    encoding="utf-8",
                )
            except OSError:
                pass
    # Audit after the mutation is on disk (SQLite + YAML), matching the
    # original state_store contract: strict mode still leaves the write in place.
    strict_stores = [
        s.strip() for s in os.getenv("CHIEF_OF_STAFF_AUDIT_STRICT", "").split(",") if s.strip()
    ]
    if action or store_name in strict_stores:
        try:
            append_audit(
                store_name,
                action=action or "save",
                before=dict(before or {}),
                after=dict(after if after is not None else plain_data),
                actor=actor,
                config=config,
            )
        except Exception as audit_exc:
            if store_name in strict_stores:
                raise StateStoreError(
                    f"Mutation succeeded but audit log failed (strict mode for {store_name}): {audit_exc}"
                ) from audit_exc
            print(
                f"Warning: audit log write failed for {store_name} (mutation succeeded): {audit_exc}",
                file=sys.stderr,
            )
    return db_path


def mutate_kv(
    store_name: str,
    mutate_fn: Callable[[dict[str, Any]], T],
    config: Mapping[str, Any] | None = None,
    action: str | None = None,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    actor: str = "agent",
    _fill_defaults: bool = False,
) -> T:
    """Read-modify-write a KV store under a single BEGIN IMMEDIATE transaction.

    Prefer this over ``load_store()`` → mutate → ``save_store_atomic()``.
    """
    get_store_path(store_name, config=config)
    before_box: dict[str, Any] = {}
    after_box: dict[str, Any] = {}

    def _wrapped(data: dict[str, Any]) -> T:
        before_box["data"] = copy.deepcopy(data)
        result = mutate_fn(data)
        after_box["data"] = data
        return result

    with _open_db(config) as db:
        result = db.mutate_kv(store_name, _wrapped, _fill_defaults=_fill_defaults)
        plain_data = _plain(dict(after_box.get("data") or {}))
        if yaml is not None:
            try:
                ypath = get_store_path(store_name, config=config)
                ypath.parent.mkdir(parents=True, exist_ok=True)
                _backup_yaml_and_prune(store_name, ypath, ypath.parent)
                ypath.write_text(
                    yaml.safe_dump(plain_data, sort_keys=False, allow_unicode=True),
                    encoding="utf-8",
                )
            except OSError:
                pass

    audit_before = before if before is not None else before_box.get("data") or {}
    audit_after = after if after is not None else after_box.get("data") or plain_data
    strict_stores = [
        s.strip() for s in os.getenv("CHIEF_OF_STAFF_AUDIT_STRICT", "").split(",") if s.strip()
    ]
    if action or store_name in strict_stores:
        try:
            append_audit(
                store_name,
                action=action or "save",
                before=dict(audit_before or {}),
                after=dict(audit_after),
                actor=actor,
                config=config,
            )
        except Exception as audit_exc:
            if store_name in strict_stores:
                raise StateStoreError(
                    f"Mutation succeeded but audit log failed (strict mode for {store_name}): {audit_exc}"
                ) from audit_exc
            print(
                f"Warning: audit log write failed for {store_name} (mutation succeeded): {audit_exc}",
                file=sys.stderr,
            )
    return result


# ─── Pending-action compatibility ─────────────────────────────


def _with_retry(config: Any, mutate_fn: Callable[[Any], T], max_attempts: int = 8) -> T | None:
    """Retry a mutate function on ConcurrencyError (compat for tests that patch _save)."""
    import random as _random

    for attempt in range(max_attempts):
        try:
            return mutate_fn(config)
        except ConcurrencyError:
            retry = True
        except sqlite3.OperationalError as exc:
            retry = "locked" in str(exc).lower() or "busy" in str(exc).lower()
            if not retry:
                raise
        else:
            retry = False
        if not retry:
            continue
        if attempt == max_attempts - 1:
            _log_event(
                "concurrency_exhausted",
                level="warning",
                component="pending_actions",
                attempt=attempt + 1,
                max=max_attempts,
            )
            return None
        time.sleep(_random.uniform(0.01, 0.05))
    return None


def _call_db(config: Any, fn: Callable[[StateDB], T]) -> T | None:
    """Run ``fn`` against a StateDB, retrying on ConcurrencyError / SQLITE_BUSY."""

    def _mutate(cfg: Any) -> T:
        with _open_db(cfg) as db:
            return fn(db)

    return _with_retry(config, _mutate)


def _load(config: Any) -> dict[str, Any]:
    """Load pending actions as the legacy ``{actions, _version}`` dict."""
    with _open_db(config) as db:
        rows = db.conn.execute("SELECT * FROM pending_actions").fetchall()
        actions = {_row_to_action(r)["id"]: _row_to_action(r) for r in rows}
        version = db._get_meta("pending_actions_version", 0)
        return {"actions": actions, "_version": version}


def _save(config: Any, data: dict[str, Any], expected_version: int | None = None) -> int:
    """Replace pending actions from a legacy dict. Honors expected_version."""
    with _open_db(config) as db:
        _begin_immediate(db.conn)
        try:
            current_version = db._get_meta("pending_actions_version", 0)
            if expected_version is not None and current_version != expected_version:
                raise ConcurrencyError(
                    f"Pending actions changed since load (expected v{expected_version}, "
                    f"found v{current_version}). Reload and retry."
                )
            new_version = (
                (max(data.get("_version", 0) or 0, current_version) + 1)
                if expected_version is None
                else current_version + 1
            )
            db.conn.execute("DELETE FROM pending_actions")
            for action in (data.get("actions") or {}).values():
                if isinstance(action, dict) and action.get("id"):
                    db.conn.execute(_ACTION_INSERT_SQL, _action_insert_params(action))
            db._set_meta("pending_actions_version", new_version)
            data["_version"] = new_version
            db.conn.commit()
            return new_version
        except Exception:
            try:
                db.conn.rollback()
            except Exception:
                pass
            raise


def _load_events(config: Any) -> dict[str, Any]:
    with _open_db(config) as db:
        rows = db.conn.execute("SELECT * FROM events").fetchall()
        events = {}
        for r in rows:
            ev = _row_to_event(r)
            events[ev.get("key") or ev["id"]] = ev
        return {"events": events, "_version": db._get_meta("events_version", 0)}


def _save_events(config: Any, data: dict[str, Any], expected_version: int | None = None) -> int:
    with _open_db(config) as db:
        _begin_immediate(db.conn)
        try:
            current_version = db._get_meta("events_version", 0)
            if expected_version is not None and current_version != expected_version:
                raise ConcurrencyError(
                    f"Events store changed since load (expected v{expected_version}, "
                    f"found v{current_version})."
                )
            new_version = current_version + 1
            db.conn.execute("DELETE FROM events")
            for event in (data.get("events") or {}).values():
                if isinstance(event, dict) and event.get("id"):
                    db.conn.execute(
                        _EVENT_INSERT_SQL.replace("INSERT INTO", "INSERT OR REPLACE INTO"),
                        _event_insert_params(event),
                    )
            db._set_meta("events_version", new_version)
            data["_version"] = new_version
            db.conn.commit()
            return new_version
        except Exception:
            try:
                db.conn.rollback()
            except Exception:
                pass
            raise


def create_pending_action(
    config: Any,
    action_type: str,
    provider: str,
    target: str,
    payload: dict[str, Any],
    summary: str | None = None,
    approver: str | None = None,
    reason: str | None = None,
) -> dict[str, Any] | None:
    # approver/reason kept for API compatibility; create_action starts in 'requested'.
    del approver, reason
    return _call_db(
        config,
        lambda db: db.create_action(
            type=action_type,
            provider=provider,
            target=target,
            payload=payload,
            summary=summary,
        ),
    )


def list_pending_actions(
    config: Any,
    state: str | None = None,
    include_expired: bool = True,
) -> list[dict[str, Any]]:
    with _open_db(config) as db:
        return db.list_actions(state=state, include_expired=include_expired)


def get_pending_action(config: Any, action_id: str) -> dict[str, Any] | None:
    with _open_db(config) as db:
        return db.get_action(action_id)


def check_expired(config: Any, action_id: str) -> bool:
    with _open_db(config) as db:
        action = db.get_action(action_id)
        if not action:
            return False
        if action.get("state") == "expired":
            return True
        if not _is_expired(action):
            return False
        db.transition_action(action_id, "expired")
        return True


def approve_pending_action(
    config: Any,
    action_id: str,
    approver: str | None = None,
    reason: str | None = None,
) -> dict[str, Any] | None:
    return _call_db(
        config,
        lambda db: db.transition_action(action_id, "approved", approver=approver, reason=reason),
    )


def cancel_pending_action(
    config: Any,
    action_id: str,
    reason: str | None = None,
) -> dict[str, Any] | None:
    return _call_db(
        config,
        lambda db: db.transition_action(action_id, "cancelled", reason=reason),
    )


def dismiss_pending_action(
    config: Any,
    action_id: str,
    reason: str | None = None,
) -> dict[str, Any] | None:
    return _call_db(
        config,
        lambda db: db.transition_action(action_id, "dismissed", reason=reason),
    )


def mark_executing(config: Any, action_id: str) -> dict[str, Any] | None:
    return _call_db(config, lambda db: db.transition_action(action_id, "executing"))


def mark_executed(config: Any, action_id: str, result: dict[str, Any]) -> dict[str, Any] | None:
    return _call_db(
        config,
        lambda db: db.transition_action(action_id, "executed", result=result),
    )


def mark_failed(config: Any, action_id: str, error: str) -> dict[str, Any] | None:
    return _call_db(
        config,
        lambda db: db.transition_action(action_id, "failed", last_error=error),
    )


def find_stuck_actions(config: Any, max_minutes: int = 15) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_minutes)
    with _open_db(config) as db:
        stuck: list[dict[str, Any]] = []
        for action in db.list_actions(state="executing"):
            dt = _parse_ts(action.get("executing_at") or "")
            if dt is None:
                continue
            if dt < cutoff:
                stuck.append(action)
        return stuck


def revert_stuck_action(config: Any, action_id: str, max_minutes: int = 15) -> dict[str, Any] | None:
    with _open_db(config) as db:
        action = db.get_action(action_id)
        if not action or action.get("state") != "executing":
            return None
        dt = _parse_ts(action.get("executing_at") or "")
        if dt is None:
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_minutes)
        if dt >= cutoff:
            return None
        retry_count = int(action.get("retry_count") or 0) + 1
        new_state = "failed" if retry_count >= MAX_RETRIES else "approved"
        updated = db._cas_update(
            action_id,
            "executing",
            new_state,
            retry_count=retry_count,
            executing_at=None,
            last_error=(
                f"Reset from orphaned 'executing' by doctor --fix "
                f"(was stale >{max_minutes}min)"
            ),
        )
        if updated is None:
            return None
        _audit_action(config, updated, "approved", {"action_id": action_id, "reason": "stuck_reverted"})
        return updated


def assert_executable(config: Any, action_id: str) -> dict[str, Any] | None:
    with _open_db(config) as db:
        action = db.get_action(action_id)
        if not action or action["state"] != "approved":
            return None
        if _is_approval_lapsed(action):
            db.transition_action(action_id, "expired")
            return None
        return action


def preview_pending_action(config: Any, action_id: str) -> dict[str, Any] | None:
    check_expired(config, action_id)
    with _open_db(config) as db:
        action = db.get_action(action_id)
        if not action:
            return None
        payload = action.get("payload") or {}
        return {
            "id": action["id"],
            "type": action["type"],
            "provider": action["provider"],
            "target": action["target"],
            "summary": action["summary"],
            "state": action["state"],
            "risk": action.get("risk"),
            "preview": {
                "to": payload.get("to"),
                "subject": payload.get("subject"),
                "body_preview": (payload.get("body") or "")[:200],
            },
            "created_at": action["created_at"],
            "approved_at": action.get("approved_at"),
            "approver": action.get("approver"),
            "approval_reason": action.get("approval_reason"),
        }


def cleanup_old_actions(config: Any, days: int = 30) -> int:
    with _open_db(config) as db:
        return db.cleanup_old_actions(days=days)


def get_pending_summary(config: Any) -> dict[str, Any]:
    with _open_db(config) as db:
        actions = db.list_actions()
    counts: dict[str, int] = {}
    expired_count = 0
    for a in actions:
        state = a.get("state", "unknown")
        if state == "requested" and _is_expired(a):
            expired_count += 1
            state = "expired"
        counts[state] = counts.get(state, 0) + 1
    high_risk = []
    for a in actions:
        if a.get("state") == "requested" and not _is_expired(a):
            risk = a.get("risk")
            if risk and risk.get("level") == "external":
                high_risk.append({
                    "id": a["id"],
                    "target": a["target"],
                    "summary": a.get("summary", ""),
                    "risk_reason": risk.get("reason", ""),
                })
    return {
        "total": len(actions),
        "by_state": counts,
        "expired_unmarked": expired_count,
        "high_risk_pending": high_risk,
    }


def format_preview_for_delivery(action_id: str, preview: dict[str, Any]) -> str:
    state = preview.get("state", "?")
    icon = {
        "requested": "📨", "approved": "✅", "executed": "📤",
        "cancelled": "❌", "dismissed": "🚫", "expired": "⏰",
    }.get(state, "?")
    lines = [
        f"{icon} Pending Gmail Send — {action_id}",
        f"State: {state}",
        f"To: {preview.get('preview', {}).get('to', '?')}",
        f"Subject: {preview.get('preview', {}).get('subject', '?')}",
    ]
    risk = preview.get("risk")
    if risk:
        risk_icon = "⚠️" if risk["level"] == "external" else "✅"
        lines.append(f"Risk: {risk_icon} {risk['level']} — {risk['reason']}")
    body_preview = preview.get("preview", {}).get("body_preview", "")
    if body_preview:
        lines.append(f"Body: {body_preview[:100]}...")
    if state == "requested":
        lines.append("")
        lines.append(f"Approve: send_email.py approve --action-id {action_id}")
        lines.append(f"Cancel:  send_email.py cancel --action-id {action_id}")
    elif state == "approved":
        lines.append("")
        lines.append(f"Execute: send_email.py execute --action-id {action_id}")
    return "\n".join(lines)


def get_actions_for_delivery(config: Any) -> list[dict[str, Any]]:
    actions = list_pending_actions(config, state="requested", include_expired=False)
    results = []
    for a in actions:
        preview = preview_pending_action(config, a["id"])
        if not preview:
            continue
        msg = format_preview_for_delivery(a["id"], preview)
        risk = a.get("risk") or {}
        results.append({
            "id": a["id"],
            "formatted_message": msg,
            "risk_level": risk.get("level", "unknown"),
            "target": a.get("target", ""),
        })
    return results


# ─── Event compatibility ──────────────────────────────────────

def ingest_event(
    config: Any,
    source: str,
    source_id: str,
    event_type: str,
    payload: dict[str, Any],
    summary: str | None = None,
) -> dict[str, Any] | None:
    with _open_db(config) as db:
        return db.ingest_event(source, source_id, event_type, payload, summary=summary)


def list_events(
    config: Any,
    state: str | None = None,
    source: str | None = None,
    category: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    with _open_db(config) as db:
        return db.list_events(state=state, source=source, category=category, limit=limit)


def get_event(config: Any, event_id: str) -> dict[str, Any] | None:
    with _open_db(config) as db:
        return db.get_event(event_id)


def mark_surfaced(config: Any, event_id: str) -> dict[str, Any] | None:
    with _open_db(config) as db:
        return db.mark_event_surfaced(event_id)


def mark_processed(
    config: Any,
    event_id: str,
    processed_by: str | None = None,
    notes: str | None = None,
) -> dict[str, Any] | None:
    with _open_db(config) as db:
        return db.mark_event_processed(event_id, processed_by=processed_by, notes=notes)


def get_event_summary(config: Any) -> dict[str, Any]:
    with _open_db(config) as db:
        events = db.list_events(limit=10_000)
    by_state: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for e in events:
        state = e.get("state", "unknown")
        by_state[state] = by_state.get(state, 0) + 1
        cat = (e.get("classification") or {}).get("category", "unknown")
        by_category[cat] = by_category.get(cat, 0) + 1
    pending = [e for e in events if e["state"] in ("received", "classified", "surfaced")]
    return {
        "total": len(events),
        "by_state": by_state,
        "by_category": by_category,
        "pending_count": len(pending),
    }


def cleanup_old_events(config: Any, days: int = 30) -> int:
    with _open_db(config) as db:
        return db.cleanup_old_events(days=days)


# ─── Webhook replay compatibility ─────────────────────────────

def reserve_delivery(
    config: Any,
    delivery_id: str,
    ttl_seconds: int = REPLAY_TTL_SECONDS,
) -> DeliveryReservation:
    with _open_db(config) as db:
        return db.reserve_delivery(delivery_id, ttl=ttl_seconds)


def complete_delivery(
    config: Any,
    delivery_id: str,
    lease_token: str | None | object = _TOKEN_OMITTED,
) -> None:
    with _open_db(config) as db:
        db.complete_delivery(delivery_id, lease_token=lease_token)


def release_delivery(
    config: Any,
    delivery_id: str,
    lease_token: str | None | object = _TOKEN_OMITTED,
) -> None:
    with _open_db(config) as db:
        db.release_delivery(delivery_id, lease_token=lease_token)


def renew_delivery(config: Any, delivery_id: str, lease_token: str | None = None) -> bool:
    with _open_db(config) as db:
        return db.renew_delivery(delivery_id, lease_token=lease_token)


def _load_replay_cache(config: Any) -> dict[str, Any]:
    with _open_db(config) as db:
        rows = db.conn.execute(
            "SELECT delivery_id, state, ts, lease_token FROM webhook_replay"
        ).fetchall()
        entries = {}
        for r in rows:
            entries[r["delivery_id"]] = {
                "state": r["state"],
                "ts": r["ts"],
                "lease_token": r["lease_token"],
            }
        return {"entries": entries, "_version": 0}


def _save_replay_cache_unlocked(config: Any, data: dict[str, Any]) -> None:
    with _open_db(config) as db:
        with db.conn:
            db.conn.execute("DELETE FROM webhook_replay")
            for did, entry in (data.get("entries") or {}).items():
                if not isinstance(entry, dict):
                    continue
                db.conn.execute(
                    "INSERT INTO webhook_replay (delivery_id, state, ts, lease_token) VALUES (?,?,?,?)",
                    (
                        str(did),
                        entry.get("state") or "processing",
                        float(entry.get("ts") or time.time()),
                        entry.get("lease_token"),
                    ),
                )


# ─── CLI ──────────────────────────────────────────────────────

def _records(store_name: str, data: Mapping[str, Any]) -> Any:
    key = {"pipeline": "deals", "invoices": "invoices", "expenses": "expenses", "todos": "todos"}.get(store_name)
    return data.get(key, data) if isinstance(data, Mapping) else data


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read and write Chief-of-Staff SQLite stores")
    parser.add_argument("--store", required=True, help="Store name (pipeline, invoices, expenses, todos)")
    parser.add_argument("--config", help="Path to company.yaml")
    parser.add_argument("--action", choices=["list", "path"], default="list", help="CLI action")
    parser.add_argument("--json", action="store_true", help="Print JSON")
    args = parser.parse_args(argv)
    cfg = load_config(args.config) if args.config and load_config is not None else None
    if args.action == "path":
        print(get_store_path(args.store, config=cfg))
        return 0
    data = load_store(args.store, config=cfg)
    output = _records(args.store, data)
    if args.json:
        print(json.dumps(output, indent=2, default=str))
    else:
        if yaml is not None:
            print(yaml.safe_dump(output, sort_keys=False, allow_unicode=True).rstrip())
        else:
            print(json.dumps(output, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
