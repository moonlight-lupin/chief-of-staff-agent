#!/usr/bin/env python3
"""Load and validate Chief-of-Staff plugin company configuration.

The central configuration file is ``shared/config/company.yaml`` by default.
A caller can override the path explicitly or via ``CHIEF_OF_STAFF_CONFIG``.

This module intentionally depends only on the Python standard library plus
PyYAML when available. If PyYAML is absent, it falls back to a conservative
YAML subset parser that supports the structures used by the example config.

Environment secrets (``.env``)
------------------------------
The onboarding docs instruct operators to place secrets such as
``COMPOSIO_MCP_KEY``, ``M365_CLIENT_SECRET`` and ``DOCUSEAL_*`` in a ``.env``
file at the plugin root. :func:`load_dotenv_file` implements a tiny, dependency
-free ``KEY=VALUE`` parser for that file and is invoked automatically from
:func:`load_config` (config discovery) so every entrypoint that loads
configuration also picks up those secrets. The shell environment always wins:
a key already present in ``os.environ`` is never overwritten, and values are
never logged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

try:  # PyYAML is expected in Hermes environments, but keep a graceful fallback.
    import yaml  # type: ignore
except Exception:  # pragma: no cover - exercised only when PyYAML is unavailable.
    yaml = None


class Config(dict):
    """Dict-like configuration object with attribute access and source path.

    Nested dictionaries are recursively converted to ``Config`` objects so both
    ``config["company"]["name"]`` and ``config.company.name`` work.
    """

    def __init__(self, *args: Any, source_path: Path | None = None, **kwargs: Any) -> None:
        super().__init__()
        self._source_path = source_path
        data = dict(*args, **kwargs)
        for key, value in data.items():
            self[key] = self._wrap(value)

    @staticmethod
    def _wrap(value: Any) -> Any:
        if isinstance(value, Config):
            return value
        if isinstance(value, Mapping):
            return Config(value)
        if isinstance(value, list):
            return [Config._wrap(item) for item in value]
        return value

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            self[name] = self._wrap(value)

    @property
    def source_path(self) -> Path | None:
        return self._source_path

    def to_plain_dict(self) -> dict[str, Any]:
        """Return a JSON/YAML-serializable plain dictionary."""

        def unwrap(value: Any) -> Any:
            if isinstance(value, Config):
                return {k: unwrap(v) for k, v in value.items()}
            if isinstance(value, list):
                return [unwrap(item) for item in value]
            return value

        return {key: unwrap(value) for key, value in self.items()}


_PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def get_hermes_home() -> Path:
    """Return the Hermes home directory.

    Resolution order:
    1. CHIEF_OF_STAFF_HERMES_HOME env var (for non-Hermes agents like OpenClaw)
    2. HERMES_HOME env var (Hermes-native)
    3. ~/.hermes (default, Hermes convention)

    This centralises all ~/.hermes references so the plugin can run
    outside a Hermes installation by setting CHIEF_OF_STAFF_HERMES_HOME.
    """
    env = os.getenv("CHIEF_OF_STAFF_HERMES_HOME") or os.getenv("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def get_default_project_root(slug: str = "default") -> Path:
    """Return the default project root for a given project slug."""
    return get_hermes_home() / "projects" / slug


class ConfigError(ValueError):
    """Raised internally for validation errors."""


def get_config_dir() -> Path:
    """Return this plugin's ``shared/config`` directory."""

    return _PLUGIN_ROOT / "shared" / "config"


def _default_config_path() -> Path:
    env_path = os.getenv("CHIEF_OF_STAFF_CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    return get_config_dir() / "company.yaml"


def load_dotenv_file(path: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """Populate ``os.environ`` from a ``.env`` file (no python-dotenv dependency).

    Minimal ``KEY=VALUE`` parser used so that secrets documented for the
    plugin's ``.env`` (e.g. ``COMPOSIO_MCP_KEY``, ``M365_CLIENT_SECRET``,
    ``DOCUSEAL_*``) become available to every entrypoint that loads
    configuration, matching the onboarding docs.

    Semantics:
      * Default path is the plugin-root ``.env``; ``path`` overrides it (tests).
      * Each recognised line is ``KEY=VALUE``; surrounding whitespace is stripped.
      * An optional leading ``export `` (shell-style) is stripped before parsing
        so ``export M365_CLIENT_SECRET=...`` works the same as a bare assignment.
      * Blank lines and lines whose first non-space character is ``#`` are ignored.
      * A single matching pair of surrounding single/double quotes around the
        value is stripped.
      * Malformed lines (no ``=``, empty key, or whitespace inside the key) are
        ignored.
      * A key is set only when it is NOT already present in ``os.environ`` — the
        shell environment always wins.
      * Values are never logged.

    Returns a mapping of the keys/values this call newly set (never contains
    keys that were already present in the environment).
    """
    env_path = Path(path).expanduser() if path is not None else (_PLUGIN_ROOT / ".env")
    applied: dict[str, str] = {}
    try:
        if not env_path.is_file():
            return applied
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return applied

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Accept shell-style "export KEY=VALUE" (python-dotenv compatible).
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[6:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or any(ch.isspace() for ch in key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"", "null", "Null", "NULL", "~"}:
        return None
    if value in {"true", "True", "TRUE"}:
        return True
    if value in {"false", "False", "FALSE"}:
        return False
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('\\"', '"')
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def _simple_yaml_load(text: str) -> dict[str, Any]:
    """Parse a small, indentation-based YAML subset.

    This fallback handles mappings, nested mappings, lists of scalars, lists of
    mappings, comments, null/bool/number scalars, and inline lists. It is not a
    general YAML parser; when PyYAML is installed it is always preferred.
    """

    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]

    def parent_for(indent: int) -> Any:
        while stack and stack[-1][0] >= indent:
            stack.pop()
        return stack[-1][1]

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        if " #" in stripped:
            stripped = stripped.split(" #", 1)[0].rstrip()

        if stripped.startswith("- "):
            parent = parent_for(indent)
            if not isinstance(parent, list):
                raise ConfigError("fallback YAML parser encountered a list where parent is not a list")
            item_text = stripped[2:].strip()
            if not item_text:
                item: Any = {}
                parent.append(item)
                stack.append((indent, item))
            elif ":" in item_text and not item_text.startswith(('"', "'")):
                key, value = item_text.split(":", 1)
                item = {key.strip(): _parse_scalar(value.strip()) if value.strip() else {}}
                parent.append(item)
                stack.append((indent, item))
                if value.strip() == "":
                    stack.append((indent + 2, item[key.strip()]))
            else:
                parent.append(_parse_scalar(item_text))
            continue

        if ":" not in stripped:
            raise ConfigError(f"fallback YAML parser cannot parse line: {raw!r}")
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        parent = parent_for(indent)
        if not isinstance(parent, dict):
            raise ConfigError("fallback YAML parser encountered a mapping where parent is not a mapping")

        if value == "":
            # Look ahead to infer list vs dict.
            next_kind = "dict"
            for future in lines[i:]:
                if not future.strip() or future.lstrip().startswith("#"):
                    continue
                future_indent = len(future) - len(future.lstrip(" "))
                if future_indent <= indent:
                    break
                if future.strip().startswith("- "):
                    next_kind = "list"
                break
            parent[key] = [] if next_kind == "list" else {}
            stack.append((indent, parent[key]))
        else:
            parent[key] = _parse_scalar(value)
    return root


def _load_yaml(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        loaded = yaml.safe_load(text)
    else:
        loaded = _simple_yaml_load(text)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError("company.yaml must contain a top-level mapping")
    return loaded


def _require_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise ConfigError(f"Missing or invalid required mapping: {key}")
    return value


def _require_fields(data: Mapping[str, Any], prefix: str, fields: Iterable[str]) -> None:
    for field in fields:
        value = data.get(field)
        if value is None or value == "":
            raise ConfigError(f"Missing required config field: {prefix}.{field}")


def _validate_config(data: Mapping[str, Any], source_path: Path) -> None:
    company = _require_mapping(data, "company")
    paths = _require_mapping(data, "paths")
    delivery = data.get("delivery", {})
    if delivery is not None and not isinstance(delivery, Mapping):
        raise ConfigError("delivery must be a mapping when provided")

    _require_fields(company, "company", ["name", "jurisdiction", "incorporation_date", "financial_year_end", "currency"])
    _require_fields(paths, "paths", ["project_root"])

    jurisdiction = str(company.get("jurisdiction", "")).upper()
    if jurisdiction not in {"SG", "HK", "US", "UK"}:
        raise ConfigError("company.jurisdiction must be one of SG, HK, US, UK")

    sales_stages = data.get("sales_stages")
    if sales_stages is not None and (not isinstance(sales_stages, list) or not all(isinstance(s, str) for s in sales_stages)):
        raise ConfigError("sales_stages must be a list of strings")

    stale_threshold = data.get("stale_threshold_days")
    if stale_threshold is not None:
        try:
            if int(stale_threshold) < 1:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ConfigError("stale_threshold_days must be a positive integer") from exc

    deadlines = data.get("deadlines")
    if deadlines is not None and not isinstance(deadlines, Mapping):
        raise ConfigError("deadlines must be a mapping when provided")

    # Resolve relative project_root paths relative to the config file directory;
    # do not require the directory to exist during onboarding.
    project_root = str(paths.get("project_root", "")).strip()
    if not project_root:
        raise ConfigError("paths.project_root cannot be empty")
    if project_root.startswith("."):
        (source_path.parent / project_root).resolve()


def load_config(path: str | os.PathLike[str] | None = None) -> Config | None:
    """Load and validate ``company.yaml``.

    Args:
        path: Optional explicit config path. When omitted, uses
            ``CHIEF_OF_STAFF_CONFIG`` if set, otherwise
            ``shared/config/company.yaml`` under this plugin.

    Returns:
        ``Config`` on success. On missing/unreadable/invalid config, prints a
        clear error to stderr and returns ``None`` instead of raising.
    """

    # Auto-load plugin-root .env so documented secrets are available to every
    # entrypoint that loads config (shell env always wins; values never logged).
    load_dotenv_file()

    config_path = Path(path).expanduser() if path else _default_config_path()
    try:
        config_path = config_path.resolve()
        if not config_path.exists():
            example = get_config_dir() / "company.yaml.example"
            print(
                f"Chief-of-Staff config not found: {config_path}\n"
                f"Create it from {example} or pass --config /path/to/company.yaml.",
                file=sys.stderr,
            )
            return None
        data = _load_yaml(config_path)
        _validate_config(data, config_path)
        return Config(data, source_path=config_path)
    except (OSError, ConfigError, Exception) as exc:
        print(f"Failed to load Chief-of-Staff config from {config_path}: {exc}", file=sys.stderr)
        return None


def get_project_root(config: Mapping[str, Any] | Config | None) -> Path | None:
    """Return the configured project root as an expanded absolute path.

    Returns ``None`` and prints an error if the config is missing or malformed.
    """

    if config is None:
        print("Cannot resolve project root: config is not loaded", file=sys.stderr)
        return None
    try:
        raw = config["paths"]["project_root"]  # type: ignore[index]
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            source = getattr(config, "source_path", None)
            base = source.parent if isinstance(source, Path) else Path.cwd()
            path = base / path
        return path.resolve()
    except Exception as exc:
        print(f"Cannot resolve paths.project_root from config: {exc}", file=sys.stderr)
        return None


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load and validate Chief-of-Staff company.yaml")
    parser.add_argument("--config", help="Path to company.yaml (default: shared/config/company.yaml or CHIEF_OF_STAFF_CONFIG)")
    parser.add_argument("--json", action="store_true", help="Print normalized config as JSON on success")
    parser.add_argument("--project-root", action="store_true", help="Print the resolved project root on success")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if config is None:
        return 1
    if args.project_root:
        root = get_project_root(config)
        if root is None:
            return 1
        print(root)
    elif args.json:
        print(json.dumps(config.to_plain_dict(), indent=2, default=str))
    else:
        print(f"OK: loaded {config.source_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
