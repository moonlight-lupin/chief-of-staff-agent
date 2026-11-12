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
GROUP_ENV = "CHIEF_OF_STAFF_GROUP"
REGISTRY_ENV = "CHIEF_OF_STAFF_REGISTRY"

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]


class RegistryError(ValueError):
    """The registry file is malformed or would break isolation."""


class AmbiguousCompanyError(RegistryError):
    """Several companies are registered and none was selected."""


class UnknownCompanyError(RegistryError):
    """The selected company slug is not in the registry."""


SHARE_DOMAINS = frozenset({"pipeline", "todos", "invoices", "expenses"})
GROUP_SCOPE_PREFIX = "group:"


@dataclass(frozen=True)
class Group:
    slug: str
    name: str
    config_path: Path | None = None
    project_root: Path | None = None
    share: dict[str, str] = field(default_factory=dict)
    members: tuple[str, ...] = ()

    @property
    def scope_id(self) -> str:
        return f"{GROUP_SCOPE_PREFIX}{self.slug}"


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


def _resolve_config(raw: Any, reg_path: Path, label: str) -> Path:
    cfg_path = Path(str(raw)).expanduser()
    if not cfg_path.is_absolute():
        cfg_path = reg_path.parent / cfg_path
    cfg_path = cfg_path.resolve()
    if not cfg_path.exists():
        raise RegistryError(f"{label}: config not found at {cfg_path}")
    return cfg_path


def _read_scope_config(cfg_path: Path, label: str, expected_id: str) -> tuple[dict[str, Any], Path]:
    from config_loader import _load_yaml

    cfg = _load_yaml(cfg_path) or {}
    company_block = cfg.get("company") or {}
    declared = company_block.get("id") if isinstance(company_block, Mapping) else None
    if declared and str(declared) != expected_id:
        raise RegistryError(
            f"{label}: company.id in {cfg_path} is '{declared}'; it must be '{expected_id}'"
        )
    try:
        raw_root = cfg["paths"]["project_root"]
    except (KeyError, TypeError) as exc:
        raise RegistryError(f"{label}: paths.project_root missing in {cfg_path}") from exc
    return cfg, _resolve_root(raw_root, cfg_path)


def config_approvers(cfg: Any) -> list[str]:
    """``approvals.approvers`` from a config, or [] when not restricted."""
    try:
        approvals = cfg.get("approvals") or {}
        raw = approvals.get("approvers") or []
    except AttributeError:
        return []
    return [str(a) for a in raw if str(a).strip()] if isinstance(raw, (list, tuple)) else []


def load_registry(path: str | os.PathLike[str] | None = None) -> Registry | None:
    """Load and validate the registry. Returns ``None`` when no registry exists."""

    from config_loader import _load_yaml  # local import: config_loader imports us

    reg_path = (Path(path).expanduser() if path else default_registry_path()).resolve()
    if not reg_path.exists():
        return None
    data = _load_yaml(reg_path) or {}
    if not isinstance(data, Mapping):
        raise RegistryError(f"{reg_path}: registry must be a mapping")

    raw_companies = data.get("companies") or {}
    if not isinstance(raw_companies, Mapping) or not raw_companies:
        raise RegistryError(f"{reg_path}: 'companies' must list at least one company")
    raw_groups = data.get("groups") or {}
    if not isinstance(raw_groups, Mapping):
        raise RegistryError(f"{reg_path}: 'groups' must be a mapping")

    companies: dict[str, Company] = {}
    claimed_accounts: dict[str, str] = {}   # account -> the company that declared it
    for slug, spec in raw_companies.items():
        slug = str(slug)
        if not isinstance(spec, Mapping) or not spec.get("config"):
            raise RegistryError(f"company '{slug}': 'config' path is required")
        cfg_path = _resolve_config(spec["config"], reg_path, f"company '{slug}'")
        group = spec.get("group")
        group = str(group) if group else None
        if group and group not in raw_groups:
            raise RegistryError(f"company '{slug}': group '{group}' is not declared under 'groups'")
        company_cfg, root = _read_scope_config(cfg_path, f"company '{slug}'", slug)
        for account in _declared_identities(company_cfg):
            owner = claimed_accounts.setdefault(account.lower(), slug)
            if owner != slug:
                raise RegistryError(
                    f"companies '{owner}' and '{slug}' both act as {account}: each company needs its own "
                    f"accounts, or one company's approval could act in the other's mailbox"
                )
        companies[slug] = Company(slug=slug, config_path=cfg_path, project_root=root, group=group)

    groups: dict[str, Group] = {}
    for slug, spec in raw_groups.items():
        slug = str(slug)
        spec = spec if isinstance(spec, Mapping) else {}
        label = f"group '{slug}'"

        share: dict[str, str] = {}
        raw_share = spec.get("share") or {}
        if not isinstance(raw_share, Mapping):
            raise RegistryError(f"{label}: 'share' must map a data domain to 'read'")
        for domain, mode in raw_share.items():
            domain = str(domain)
            if domain not in SHARE_DOMAINS:
                raise RegistryError(
                    f"{label}: unknown share domain '{domain}' (allowed: {', '.join(sorted(SHARE_DOMAINS))})"
                )
            if str(mode) != "read":
                raise RegistryError(
                    f"{label}: sharing is read-only; '{domain}: {mode}' is not allowed. "
                    f"Group writes go through the group's own store and queue."
                )
            share[domain] = "read"

        cfg_path = root = None
        if spec.get("config"):
            cfg_path = _resolve_config(spec["config"], reg_path, label)
            cfg, root = _read_scope_config(cfg_path, label, f"{GROUP_SCOPE_PREFIX}{slug}")
            for account in _declared_identities(cfg):
                owner = claimed_accounts.setdefault(account.lower(), f"group:{slug}")
                if owner != f"group:{slug}":
                    raise RegistryError(
                        f"{label} and '{owner}' both act as {account}: a group's approval must never "
                        f"act in a member company's mailbox"
                    )
            if not config_approvers(cfg):
                raise RegistryError(
                    f"{label}: a group config must list approvals.approvers; group writes need named approvers"
                )

        members = tuple(sorted(c.slug for c in companies.values() if c.group == slug))
        groups[slug] = Group(
            slug=slug, name=str(spec.get("name") or slug), config_path=cfg_path,
            project_root=root, share=share, members=members,
        )

    stores: list[tuple[str, Path]] = [(f"company '{c.slug}'", c.project_root) for c in companies.values()]
    stores += [(f"group '{g.slug}'", g.project_root) for g in groups.values() if g.project_root]
    for i, (label, root) in enumerate(stores):
        for other_label, other_root in stores[:i]:
            if _overlaps(root, other_root):
                raise RegistryError(
                    f"{other_label} and {label} share or nest project_root "
                    f"({other_root} / {root}); each needs its own store"
                )

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


def resolve_active_group(registry: Registry, slug: str) -> Group:
    """Return the group selected for group scope; it must have its own config."""

    group = registry.groups.get(slug)
    if group is None:
        raise UnknownCompanyError(f"Unknown group '{slug}'. Declared: {', '.join(sorted(registry.groups))}")
    if group.config_path is None:
        raise RegistryError(
            f"group '{slug}' has no config, so it has no store or approval queue. "
            f"Add 'config:' (with approvals.approvers) under groups.{slug} in the registry."
        )
    return group


def scope_id(config: Any) -> str | None:
    """The company slug or ``group:<slug>`` a config was resolved for, else None."""

    try:
        company = config["company"]
        value = company.get("id") if hasattr(company, "get") else None
    except (KeyError, TypeError, AttributeError):
        return None
    return str(value) if value else None


def company_context(config: Any) -> dict[str, Any] | None:
    """Return ``{id, group, registered_companies}`` for a registry-resolved config."""

    company_id = scope_id(config)
    if not company_id:
        return None
    try:
        registry = load_registry()
    except RegistryError:
        registry = None
    if company_id.startswith(GROUP_SCOPE_PREFIX):
        group = company_id[len(GROUP_SCOPE_PREFIX):]
    else:
        entry = registry.companies.get(company_id) if registry else None
        group = entry.group if entry else None
    return {
        "id": company_id,
        "group": group,
        "registered_companies": len(registry.companies) if registry else 0,
    }


_FLAGS = {"--company": COMPANY_ENV, "--group": GROUP_ENV}


def apply_company_flag(argv: list[str] | None) -> list[str]:
    """Strip ``--company``/``--group`` (``<slug>`` or ``=<slug>``) from argv and select it.

    Selection goes through the environment so every module that loads config
    in this process (and any subprocess) agrees on the active scope.
    """

    args = list(argv) if argv is not None else []
    out: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        flag, _, inline = arg.partition("=")
        if flag in _FLAGS:
            if _:
                value = inline
                i += 1
            else:
                value = args[i + 1] if i + 1 < len(args) else ""
                i += 2
            if not value or value.startswith("-"):
                raise RegistryError(f"{flag} needs a slug")
            os.environ[_FLAGS[flag]] = value
            continue
        out.append(arg)
        i += 1
    return out


# ------------------------------------------------------------------ sharing


class ShareDenied(PermissionError):
    """A cross-company read is not permitted by the registry's share policy."""


def _audit_share(config: Any, owner: str, domain: str, status: str, reason: str = "") -> None:
    try:
        from workspace_audit import audit_workspace_action

        extra = {"owner": owner, "domain": domain, "reader": scope_id(config)}
        if reason:
            extra["reason"] = reason
        audit_workspace_action(config, "registry", "shared.read", "registry",
                               target=f"{owner}/{domain}", status=status, extra=extra)
    except Exception:  # audit is best-effort, as everywhere else
        pass


def _read_store_readonly(db_path: Path, domain: str) -> dict[str, Any]:
    """Read one KV store from another scope's state.db without any write."""

    import copy
    import json
    import sqlite3

    from state_db import EMPTY_TEMPLATES, KV_ROOT_KEY

    empty = copy.deepcopy(EMPTY_TEMPLATES[domain])
    if not db_path.exists():
        return empty
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM kv_stores WHERE store_name=? AND key=?", (domain, KV_ROOT_KEY)
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        conn.close()
    if row is None:
        return empty
    data = json.loads(row[0])
    return data if isinstance(data, dict) else empty


def read_shared(config: Any, owner: str, domain: str) -> dict[str, Any]:
    """Read ``domain`` from company ``owner``'s store, if the share policy allows.

    Allowed when the reader is ``owner`` itself, or the reader is a sister
    company or the group scope of ``owner``'s group and that group shares
    ``domain``. Read-only: the owner's store is opened read-only and nothing is
    created in it. Every attempt is audited in the reader's log.
    """

    reader = scope_id(config)

    def deny(reason: str) -> None:
        _audit_share(config, owner, domain, "denied", reason)
        raise ShareDenied(reason)

    if domain not in SHARE_DOMAINS:
        deny(f"'{domain}' is not a shareable domain")
    try:
        registry = load_registry()
    except RegistryError as exc:
        deny(f"registry invalid: {exc}")
    if registry is None or not reader:
        deny("multi-company mode is not active")
    owner_co = registry.companies.get(owner)
    if owner_co is None:
        deny(f"'{owner}' is not a registered company")

    if reader != owner:
        group = registry.groups.get(owner_co.group or "")
        if group is None:
            deny(f"'{owner}' is not in a group, so nothing it holds is shared")
        reader_co = registry.companies.get(reader)
        same_group = reader == group.scope_id or (reader_co is not None and reader_co.group == group.slug)
        if not same_group:
            deny(f"'{reader}' is not in group '{group.slug}' with '{owner}'")
        if group.share.get(domain) != "read":
            deny(f"group '{group.slug}' does not share '{domain}'")

    data = _read_store_readonly(owner_co.project_root / "state.db", domain)
    _audit_share(config, owner, domain, "success")
    return data


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


def _summarise(domain: str, data: Mapping[str, Any]) -> dict[str, Any]:
    today = _today()
    if domain == "pipeline":
        active = [d for d in data.get("deals") or [] if str(d.get("status") or "active") == "active"]
        by_ccy: dict[str, float] = {}
        for deal in active:
            ccy = str(deal.get("currency") or "?")
            by_ccy[ccy] = by_ccy.get(ccy, 0) + (deal.get("value") or 0)
        return {"active_deals": len(active), "value_by_currency": by_ccy}
    if domain == "todos":
        open_ = [t for t in data.get("todos") or [] if str(t.get("status") or "open") == "open"]
        overdue = [t for t in open_ if t.get("due") and str(t["due"]) < today]
        return {"open": len(open_), "overdue": len(overdue)}
    if domain == "invoices":
        unpaid = [i for i in data.get("invoices") or []
                  if i.get("direction") == "sent" and str(i.get("status")) not in {"paid", "void", "cancelled"}]
        overdue = [i for i in unpaid if i.get("due_date") and str(i["due_date"]) < today]
        by_ccy: dict[str, float] = {}
        for inv in unpaid:
            ccy = str(inv.get("currency") or "?")
            by_ccy[ccy] = by_ccy.get(ccy, 0) + (inv.get("amount") or 0)
        return {"unpaid": len(unpaid), "overdue": len(overdue), "unpaid_by_currency": by_ccy}
    expenses = data.get("expenses") or []
    return {"count": len(expenses)}


def group_summary(config: Any) -> dict[str, Any]:
    """Read-only roll-up of every member's shared domains. Group scope only."""

    reader = scope_id(config) or ""
    if not reader.startswith(GROUP_SCOPE_PREFIX):
        raise ShareDenied("group-summary runs in group scope: pass --group <slug>")
    registry = load_registry()
    group = registry.groups[reader[len(GROUP_SCOPE_PREFIX):]] if registry else None
    if group is None:
        raise ShareDenied(f"unknown group scope '{reader}'")
    members: dict[str, Any] = {}
    for slug in group.members:
        entry: dict[str, Any] = {}
        for domain in sorted(SHARE_DOMAINS):
            if group.share.get(domain) != "read":
                entry[domain] = "not shared"
                continue
            entry[domain] = _summarise(domain, read_shared(config, slug, domain))
        members[slug] = entry
    return {"group": group.slug, "name": group.name, "shared_domains": sorted(group.share), "members": members}


# ------------------------------------------------------------------ accounts

_ACCOUNT_FAMILY = {
    "mail": "mail", "gmail": "mail", "contacts": "mail",
    "calendar": "calendar", "drive": "drive", "files": "drive", "notion": "notion",
}


def expected_account(config: Any, action_type: str) -> str | None:
    """The connector account a config declares for this action's family.

    ``accounts:`` in company.yaml maps families (mail, calendar, drive, notion)
    to an account. Calendar and drive fall back to the mail account.
    """

    family = _ACCOUNT_FAMILY.get(str(action_type).split(".", 1)[0])
    if family is None:
        return None
    try:
        accounts = config.get("accounts") or {}
    except AttributeError:
        return None
    if not hasattr(accounts, "get"):
        return None
    value = accounts.get(family) or (accounts.get("mail") if family in {"calendar", "drive"} else None)
    return str(value) if value else None


def _declared_identities(cfg: Any) -> set[str]:
    """Send-as identities a scope config acts as, which must be exclusive:
    accounts.mail, the Google delegate, the M365 mailbox. A shared calendar
    or Notion workspace is allowed."""
    out: set[str] = set()
    try:
        accounts = cfg.get("accounts") or {}
        mail = str(accounts.get("mail") or "").strip() if hasattr(accounts, "get") else ""
        if mail:
            out.add(mail)
        delegate = str(((cfg.get("google") or {}).get("delegate_email")) or "").strip()
        mailbox = str(((cfg.get("m365") or {}).get("user_principal")) or "").strip()
        out |= {v for v in (delegate, mailbox) if v}
    except AttributeError:
        pass
    return out


# Notion actions that only touch the local store (no Notion write).
LOCAL_ONLY_TYPES = frozenset({"notion.pull", "notion.link", "notion.resolve"})


def configured_identity(config: Any, provider: str | None) -> str:
    """Who the configured client acts as, from config alone: the Google
    delegate, the M365 client-credentials mailbox, or (Composio) a digest of
    the routing that selects the connected account. "" when not configured."""
    try:
        if provider == "google_api":
            return str(((config.get("google") or {}).get("delegate_email")) or "").strip()
        if provider == "m365":
            m365 = config.get("m365") or {}
            if str(m365.get("auth") or "client_credentials") == "device_code":
                return "device_code"
            return str(m365.get("user_principal") or "").strip()
        if provider and provider.startswith("composio"):
            import hashlib
            import json

            workspace = ((config.get("integrations") or {}).get("workspace") or {})
            routing = {"user_id": workspace.get("user_id"), "aliases": workspace.get("account_aliases"),
                       "key_env": (workspace.get("mcp") or {}).get("key_env") if isinstance(workspace.get("mcp"), Mapping) else None}
            return "composio:" + hashlib.sha256(json.dumps(routing, sort_keys=True, default=str).encode()).hexdigest()[:16]
    except AttributeError:
        return ""
    return ""


def configured_provider(config: Any) -> str:
    try:
        workspace = ((config.get("integrations") or {}).get("workspace") or {})
        return str(workspace.get("provider") or "google_api")
    except AttributeError:
        return "google_api"


def action_binding(config: Any, action_type: str) -> dict[str, Any] | None:
    """What a workspace action is bound to at creation: the declared account
    for its family ("" when none is declared) and the workspace provider.
    None for local actions (pipeline, bookkeeping...), which touch no account."""
    if str(action_type) in LOCAL_ONLY_TYPES:
        return None
    family = _ACCOUNT_FAMILY.get(str(action_type).split(".", 1)[0])
    if family is None:
        return None
    provider = None if family == "notion" else configured_provider(config)
    return {"account": expected_account(config, action_type) or "", "provider": provider,
            "identity": configured_identity(config, provider) if provider else ""}


def binding_drift(bound: Mapping[str, Any], now: Mapping[str, Any] | None) -> str | None:
    """None when the live config still matches the approved binding."""
    now = now or {}
    if str(bound.get("account") or "").strip().lower() != str(now.get("account") or "").strip().lower():
        return f"account {bound.get('account') or '(none)'} -> {now.get('account') or '(none)'}"
    if (bound.get("provider") or None) != (now.get("provider") or None):
        return f"provider {bound.get('provider')} -> {now.get('provider')}"
    if str(bound.get("identity") or "").strip().lower() != str(now.get("identity") or "").strip().lower():
        return "the configured identity (delegate, mailbox or connector routing) changed"
    return None


def bound_account(config: Any, action: Mapping[str, Any]) -> str | None:
    """The account the action was approved for (legacy rows: the live config)."""
    binding = action.get("binding")
    if isinstance(binding, Mapping):
        return str(binding.get("account") or "") or None
    return expected_account(config, str(action.get("type") or ""))


def _expected_identity(action: Mapping[str, Any], config: Any = None) -> str:
    """The account a Python-path write must act as: the declared account, else
    the configured identity it was approved under (an email-like one only)."""
    binding = action.get("binding")
    if not isinstance(binding, Mapping):   # legacy row: the live config is the best we know
        binding = action_binding(config, str(action.get("type") or "")) if config is not None else None
    if not isinstance(binding, Mapping):
        return ""
    declared = str(binding.get("account") or "").strip()
    if declared:
        return declared
    identity = str(binding.get("identity") or "").strip()
    return identity if "@" in identity else ""


def preflight(action: Mapping[str, Any], client: Any, config: Any = None) -> tuple[str | None, str]:
    """(refusal or None, account_check) before a Python-path write.

    Refuses when the client knowably acts as an account other than the one
    approved. ``account_check`` (match | unverified | not_declared) goes into
    the executed result so doctor sees unverifiable executions too."""
    expected = _expected_identity(action, config)
    family = _ACCOUNT_FAMILY.get(str(action.get("type") or "").split(".", 1)[0]) or "mail"
    identity = getattr(client, "acting_identity", None)
    actual = identity(family) if callable(identity) else None
    if not isinstance(actual, str) or not actual.strip():
        actual = None   # anything but a real identity string is "cannot know"
    check = account_check(expected or None, actual)
    if check == "mismatch":
        return (f"account_mismatch: approved for {expected}, but the client would act as {actual}. "
                f"Nothing was sent; fix the provider config or propose the action again."), check
    return None, check


def preflight_account(action: Mapping[str, Any], client: Any, config: Any = None) -> str | None:
    return preflight(action, client, config)[0]


def account_check(expected: str | None, actual: str | None) -> str:
    """``not_declared`` | ``unverified`` | ``match`` | ``mismatch``."""

    if not expected:
        return "not_declared"
    if not actual:
        return "unverified"
    return "match" if expected.strip().lower() == actual.strip().lower() else "mismatch"
