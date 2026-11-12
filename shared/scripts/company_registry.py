#!/usr/bin/env python3
"""Multi-company registry: which companies exist, and which one is active.

Design: docs/MULTI_COMPANY.md. A registry is optional. Without one the plugin
runs in legacy single-company mode and nothing here changes behaviour.

The registry is the isolation boundary. ``load_registry`` refuses a registry
in which two companies would share a store (same or nested ``project_root``),
and ``resolve_active_company`` refuses to guess when more than one company is
registered and none was chosen.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

COMPANY_ENV = "CHIEF_OF_STAFF_COMPANY"
REGISTRY_ENV = "CHIEF_OF_STAFF_REGISTRY"

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]


class RegistryError(ValueError):
    """The registry file is malformed or would break isolation."""


class AmbiguousCompanyError(RegistryError):
    """Several companies are registered and none was selected."""


class UnknownCompanyError(RegistryError):
    """The selected company slug is not in the registry."""


@dataclass(frozen=True)
class Group:
    slug: str
    name: str


@dataclass(frozen=True)
class Company:
    slug: str
    config_path: Path
    project_root: Path
    group: str | None = None


@dataclass(frozen=True)
class Registry:
    path: Path
    companies: dict[str, Company] = field(default_factory=dict)
    groups: dict[str, Group] = field(default_factory=dict)


def default_registry_path() -> Path:
    env = os.getenv(REGISTRY_ENV)
    if env:
        return Path(env).expanduser()
    return _PLUGIN_ROOT / "shared" / "config" / "companies.yaml"


def _resolve_root(raw: Any, config_path: Path) -> Path:
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def load_registry(path: str | os.PathLike[str] | None = None) -> Registry | None:
    """Load and validate the registry. Returns ``None`` when no registry exists."""

    from config_loader import _load_yaml  # local import: config_loader imports us

    reg_path = (Path(path).expanduser() if path else default_registry_path()).resolve()
    if not reg_path.exists():
        return None
    data = _load_yaml(reg_path) or {}
    if not isinstance(data, Mapping):
        raise RegistryError(f"{reg_path}: registry must be a mapping")

    groups: dict[str, Group] = {}
    for slug, spec in (data.get("groups") or {}).items():
        spec = spec if isinstance(spec, Mapping) else {}
        groups[str(slug)] = Group(slug=str(slug), name=str(spec.get("name") or slug))

    raw_companies = data.get("companies") or {}
    if not isinstance(raw_companies, Mapping) or not raw_companies:
        raise RegistryError(f"{reg_path}: 'companies' must list at least one company")

    companies: dict[str, Company] = {}
    for slug, spec in raw_companies.items():
        slug = str(slug)
        if not isinstance(spec, Mapping) or not spec.get("config"):
            raise RegistryError(f"company '{slug}': 'config' path is required")
        cfg_path = Path(str(spec["config"])).expanduser()
        if not cfg_path.is_absolute():
            cfg_path = reg_path.parent / cfg_path
        cfg_path = cfg_path.resolve()
        if not cfg_path.exists():
            raise RegistryError(f"company '{slug}': config not found at {cfg_path}")

        group = spec.get("group")
        group = str(group) if group else None
        if group and group not in groups:
            raise RegistryError(f"company '{slug}': group '{group}' is not declared under 'groups'")

        cfg = _load_yaml(cfg_path) or {}
        company_block = cfg.get("company") or {}
        declared = company_block.get("id") if isinstance(company_block, Mapping) else None
        if declared and str(declared) != slug:
            raise RegistryError(
                f"company '{slug}': company.id in {cfg_path} is '{declared}'; it must match the registry slug"
            )
        try:
            raw_root = cfg["paths"]["project_root"]
        except (KeyError, TypeError) as exc:
            raise RegistryError(f"company '{slug}': paths.project_root missing in {cfg_path}") from exc

        companies[slug] = Company(
            slug=slug,
            config_path=cfg_path,
            project_root=_resolve_root(raw_root, cfg_path),
            group=group,
        )

    seen: list[Company] = []
    for company in companies.values():
        for other in seen:
            if _overlaps(company.project_root, other.project_root):
                raise RegistryError(
                    f"companies '{other.slug}' and '{company.slug}' share or nest project_root "
                    f"({other.project_root} / {company.project_root}); each company needs its own store"
                )
        seen.append(company)

    return Registry(path=reg_path, companies=companies, groups=groups)


def resolve_active_company(registry: Registry, explicit: str | None = None) -> Company:
    """Pick the active company: explicit slug, then the env var, then the only one."""

    choice = explicit or os.getenv(COMPANY_ENV) or None
    slugs = sorted(registry.companies)
    if choice:
        if choice not in registry.companies:
            raise UnknownCompanyError(f"Unknown company '{choice}'. Registered: {', '.join(slugs)}")
        return registry.companies[choice]
    if len(slugs) == 1:
        return registry.companies[slugs[0]]
    raise AmbiguousCompanyError(
        f"{len(slugs)} companies are registered ({', '.join(slugs)}) and none is selected. "
        f"Pass --company <slug> or set {COMPANY_ENV}."
    )


def company_context(config: Any) -> dict[str, Any] | None:
    """Return ``{id, group, registered_companies}`` for a registry-resolved config."""

    try:
        company_id = config["company"].get("id")
    except (KeyError, TypeError, AttributeError):
        company_id = None
    if not company_id:
        return None
    try:
        registry = load_registry()
    except RegistryError:
        registry = None
    entry = registry.companies.get(str(company_id)) if registry else None
    return {
        "id": str(company_id),
        "group": entry.group if entry else None,
        "registered_companies": len(registry.companies) if registry else 0,
    }


def apply_company_flag(argv: list[str] | None) -> list[str]:
    """Strip ``--company <slug>`` / ``--company=<slug>`` from argv and select it.

    Selection goes through ``CHIEF_OF_STAFF_COMPANY`` so every module that
    loads config in this process agrees on the active company.
    """

    args = list(argv) if argv is not None else []
    out: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--company":
            if i + 1 >= len(args) or args[i + 1].startswith("-"):
                raise RegistryError("--company needs a company slug")
            os.environ[COMPANY_ENV] = args[i + 1]
            i += 2
            continue
        if arg.startswith("--company="):
            value = arg.split("=", 1)[1]
            if not value:
                raise RegistryError("--company needs a company slug")
            os.environ[COMPANY_ENV] = value
            i += 1
            continue
        out.append(arg)
        i += 1
    return out
