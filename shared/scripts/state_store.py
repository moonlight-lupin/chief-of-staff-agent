#!/usr/bin/env python3
"""Atomic YAML state store with locking, backup, validation, and audit.

Usage:
    from state_store import load_store, save_store_atomic
    data = load_store("pipeline")  # reads {project_root}/pipeline.yaml
    data["deals"].append(new_deal)
    save_store_atomic("pipeline", data, action="add_deal", before=old_data, after=data)
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    raise RuntimeError("PyYAML is required for state_store.py") from exc

from audit_log import append_audit
from file_lock import FileLockError, with_lock
from schemas import SchemaError, validate_store

try:
    from config_loader import get_project_root as _config_project_root
    from config_loader import load_config
except Exception:  # pragma: no cover
    _config_project_root = None  # type: ignore
    load_config = None  # type: ignore


class StateStoreError(RuntimeError):
    """Raised for state-store IO, YAML, or project-root failures."""


EMPTY_TEMPLATES: dict[str, dict[str, list[Any]]] = {
    "pipeline": {"deals": []},
    "invoices": {"invoices": []},
    "expenses": {"expenses": []},
    "todos": {"todos": []},
}


def _plain(value: Any) -> Any:
    if hasattr(value, "to_plain_dict"):
        return value.to_plain_dict()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


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
    raise StateStoreError("Missing project root: pass config, set CHIEF_OF_STAFF_CONFIG, or set CHIEF_OF_STAFF_PROJECT_ROOT")


def _template(store_name: str) -> dict[str, Any]:
    return copy.deepcopy(EMPTY_TEMPLATES.get(store_name, {}))


def get_store_path(store_name: str, config: Mapping[str, Any] | None = None) -> Path:
    """Return ``{project_root}/{store_name}.yaml`` as an absolute path."""

    if not store_name or not str(store_name).strip():
        raise StateStoreError("store_name is required")
    root = _resolve_project_root(config)
    return root / f"{store_name}.yaml"


def _safe_load_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise StateStoreError(f"Corrupt YAML in {path}: {exc}") from exc
    except OSError as exc:
        raise StateStoreError(f"Cannot read {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise StateStoreError(f"Invalid YAML in {path}: top-level value must be a mapping")
    return loaded


def load_store(store_name: str, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Load a YAML store, creating and returning its empty template if missing."""

    path = get_store_path(store_name, config=config)
    if not path.exists():
        data = _template(store_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with with_lock(path, timeout=5):
                if not path.exists():
                    with path.open("w", encoding="utf-8") as fh:
                        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
        except (OSError, FileLockError) as exc:
            raise StateStoreError(f"Cannot create empty store {path}: {exc}") from exc
        return data
    data = _safe_load_yaml(path)
    if not data and store_name in EMPTY_TEMPLATES:
        return _template(store_name)
    validate_store(store_name, data, config=config)
    return data


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def save_store_atomic(
    store_name: str,
    data: Mapping[str, Any],
    action: str | None = None,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    actor: str = "agent",
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Atomically validate and write a YAML store, then append audit if requested."""

    path = get_store_path(store_name, config=config)
    root = path.parent
    tmp_path = root / f"{store_name}.yaml.tmp"
    backup_dir = root / ".backups"
    plain_data = _plain(dict(data))
    validate_store(store_name, plain_data, config=config)

    try:
        root.mkdir(parents=True, exist_ok=True)
        backup_dir.mkdir(parents=True, exist_ok=True)
        with with_lock(path, timeout=10):
            with tmp_path.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(plain_data, fh, sort_keys=False, allow_unicode=True)
                fh.flush()
                os.fsync(fh.fileno())

            parsed = _safe_load_yaml(tmp_path)
            validate_store(store_name, parsed, config=config)

            if path.exists():
                backup_path = backup_dir / f"{store_name}.{_timestamp()}.yaml"
                shutil.copy2(path, backup_path)

            os.replace(tmp_path, path)
            dir_fd = os.open(str(root), os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except (OSError, FileLockError, SchemaError, StateStoreError) as exc:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        if isinstance(exc, StateStoreError):
            raise
        raise StateStoreError(f"Failed to save {store_name} store at {path}: {exc}") from exc

    if action:
        try:
            append_audit(store_name, action=action, before=dict(before or {}), after=dict(after or plain_data), actor=actor, config=config)
        except Exception as audit_exc:
            # Best-effort audit: mutation already succeeded on disk.
            # Log warning to stderr but don't fail the operation.
            # Strict mode can be enabled per-store via CHIEF_OF_STAFF_AUDIT_STRICT env var.
            strict_stores = os.getenv("CHIEF_OF_STAFF_AUDIT_STRICT", "").split(",")
            if store_name in strict_stores:
                raise StateStoreError(
                    f"Mutation succeeded but audit log failed (strict mode for {store_name}): {audit_exc}"
                ) from audit_exc
            print(f"Warning: audit log write failed for {store_name} (mutation succeeded): {audit_exc}", file=sys.stderr)
    return path


def _records(store_name: str, data: Mapping[str, Any]) -> Any:
    key = {"pipeline": "deals", "invoices": "invoices", "expenses": "expenses", "todos": "todos"}.get(store_name)
    return data.get(key, data) if isinstance(data, Mapping) else data


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read and write Chief-of-Staff YAML stores safely")
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
        print(yaml.safe_dump(output, sort_keys=False, allow_unicode=True).rstrip())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
