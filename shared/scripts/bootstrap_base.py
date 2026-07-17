#!/usr/bin/env python3
"""Bootstrap chief-of-staff plugin from a fresh clone.

Usage:
    python bootstrap.py --company "Acme Pte Ltd" --jurisdiction SG --operator founder@acme.com
    python bootstrap.py --config preset.yaml  # non-interactive
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    raise RuntimeError("PyYAML is required for bootstrap.py") from exc

from config_loader import is_default_assistant_name
from doctor import run_checks
from state_store import EMPTY_TEMPLATES

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PLUGIN_ROOT / "shared" / "config"


class BootstrapError(RuntimeError):
    """Raised when deterministic bootstrap cannot complete."""


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise BootstrapError(f"{path} must contain a mapping")
    return data


def _copy_examples() -> list[str]:
    copied: list[str] = []
    for name in ("company.yaml", "drive-map.yaml", "queries.yaml"):
        live = CONFIG_DIR / name
        example = CONFIG_DIR / f"{name}.example"
        if not live.exists() and example.exists():
            shutil.copy2(example, live)
            copied.append(str(live))
    return copied


def _merge_preset(args: argparse.Namespace) -> dict[str, Any]:
    preset: dict[str, Any] = {}
    if args.config:
        preset = _load_yaml(Path(args.config).expanduser())
    if args.company:
        preset.setdefault("company", {})["name"] = args.company
    if args.jurisdiction:
        preset.setdefault("company", {})["jurisdiction"] = args.jurisdiction
    if args.operator:
        preset.setdefault("google", {})["delegate_email"] = args.operator
        preset.setdefault("esign", {})["provider_email"] = args.operator
    if args.project_root:
        preset.setdefault("paths", {})["project_root"] = args.project_root
    if args.business_type:
        preset.setdefault("company", {})["business_type"] = args.business_type
    # Assistant identity is only written when explicitly provided; otherwise an
    # existing custom assistant name must survive re-bootstrap.
    assistant_name = getattr(args, "assistant_name", None)
    if assistant_name:
        preset.setdefault("assistant", {})["name"] = assistant_name
    return preset


# ── Identity derivation (audit #2: no more canned Acme fixture) ──────────────
#
# The example config ships a filled-in sample (Alicia Tan / acme-advisory.example
# / ~/.hermes/projects/acme-advisory/). Before this fix `_write_config` copied the
# example wholesale, so those values leaked into every bootstrapped config
# regardless of the flags passed. We now DERIVE the operator-facing identity from
# --company / --operator / --operator-name and OVERRIDE the sample values. Any
# field we cannot derive is written as an explicit placeholder and announced.

FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "yahoo.com", "hotmail.com",
    "live.com", "icloud.com", "proton.me", "protonmail.com", "aol.com",
}
USER_NAME_PLACEHOLDER = "<operator-name>"
USER_EMAIL_PLACEHOLDER = "<operator-email>"
USER_ROLE_PLACEHOLDER = "<operator-role>"
USER_PHONE_PLACEHOLDER = "<operator-phone>"
WEBSITE_PLACEHOLDER = "<company-website>"
REGISTRATION_PLACEHOLDER = "<registration-number>"
TAX_REGISTRATION_PLACEHOLDER = "<tax-registration-number>"
ADDRESS_PLACEHOLDER = "<company-address>"
COMPANY_PHONE_PLACEHOLDER = "<company-phone>"
GOOGLE_DOMAIN_PLACEHOLDER = "<workspace-domain>"
GOOGLE_ALIAS_PLACEHOLDER = "<account-alias>"
GOOGLE_SA_PATH_PLACEHOLDER = "~/.hermes/secrets/<account>-google-service-account.json"
GOOGLE_DRIVE_ROOT_PLACEHOLDER = "<drive-root-folder-id>"
ROUTING_OVERLAY_MANIFEST = ".routing-overlays.json"


def _slugify_company(name: str) -> str:
    """Lowercase, spaces→hyphens, strip anything that is not alnum or hyphen,
    then collapse repeated hyphens and trim. 'Acme, Inc.' → 'acme-inc'."""
    s = (name or "").strip().lower().replace(" ", "-")
    s = "".join(ch for ch in s if ch.isalnum() or ch == "-")
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def _name_from_email(email: str) -> str:
    """Title-case the email local-part, treating . _ - as word separators.
    'alicia@x.com' → 'Alicia'; 'mary.jane@x.com' → 'Mary Jane'."""
    local = email.split("@", 1)[0]
    cleaned = re.sub(r"[._-]+", " ", local).strip()
    return cleaned.title() if cleaned else local


def _placeholder_notice(field: str, reason: str) -> str:
    return f"{field}: placeholder — edit company.yaml ({reason})"


def _identity_overlay(
    args: argparse.Namespace,
    current_config: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Derive company/user/paths/google identity from the flags and return
    ``(overlay, notices)``. The overlay is deep-merged over the example so the
    canned Acme values never survive. ``notices`` announces any placeholder.

    On re-bootstrap, ``current_config`` is the existing company.yaml with CLI /
    preset values already merged over it. Non-derivable identity fields are only
    replaced when they still contain the shipped Acme sample, an empty value, or
    an explicit placeholder; real operator-edited values are preserved.
    """
    from config_loader import get_default_project_root

    overlay: dict[str, Any] = {}
    notices: list[str] = []
    company: dict[str, Any] = {}
    user: dict[str, Any] = {}
    google: dict[str, Any] = {}
    current = current_config or {}
    current_company = current.get("company", {}) if isinstance(current.get("company"), Mapping) else {}
    current_user = current.get("user", {}) if isinstance(current.get("user"), Mapping) else {}
    current_google = current.get("google", {}) if isinstance(current.get("google"), Mapping) else {}

    current_paths = current.get("paths", {}) if isinstance(current.get("paths"), Mapping) else {}

    # paths.project_root: explicit flag > existing real config > company slug >
    # generic default. wiki_path/staging follow the root so they never keep the
    # Acme sample path.
    explicit_root = getattr(args, "project_root", None)
    company_name = getattr(args, "company", None)
    company_slug = _slugify_company(company_name) if company_name else ""
    if explicit_root:
        root = str(Path(explicit_root).expanduser())
    elif not _is_identity_sample(current_paths.get("project_root")):
        root = str(Path(str(current_paths["project_root"])).expanduser())
    elif company_slug:
        root = str(get_default_project_root(company_slug))
    else:
        root = str(get_default_project_root("chief-of-staff"))
    overlay["paths"] = {
        "project_root": root,
        "wiki_path": str(Path(root) / "wiki"),
        "staging": str(Path(root) / "staging"),
    }

    # user block + company.website from --operator (and optional --operator-name).
    operator = getattr(args, "operator", None)
    operator_name = getattr(args, "operator_name", None)
    domain = ""
    if operator:
        user["email"] = operator
        user["name"] = operator_name if operator_name else _name_from_email(operator)
        domain = operator.split("@", 1)[1].lower() if "@" in operator else ""
        if domain and domain not in FREEMAIL_DOMAINS:
            company["website"] = f"https://{domain}"
        else:
            company["website"] = WEBSITE_PLACEHOLDER
            reason = (
                f"freemail domain {domain}" if domain in FREEMAIL_DOMAINS
                else "operator email had no domain"
            )
            notices.append(_placeholder_notice("company.website", reason))
        google["delegate_email"] = operator
    else:
        if _is_identity_sample(current_user.get("name")):
            user["name"] = USER_NAME_PLACEHOLDER
        if _is_identity_sample(current_user.get("email")):
            user["email"] = USER_EMAIL_PLACEHOLDER
        if _is_identity_sample(current_company.get("website")):
            company["website"] = WEBSITE_PLACEHOLDER
        if _is_identity_sample(current_google.get("delegate_email")):
            google["delegate_email"] = USER_EMAIL_PLACEHOLDER
        notices.append(_placeholder_notice("user.name/user.email", "no --operator given"))
        notices.append(_placeholder_notice("company.website", "no --operator given"))

    # Always scrub Acme legal IDs / phones / role — none of these are derivable
    # from bootstrap flags, so leave explicit placeholders rather than leaking
    # sample PII into a fresh install. Only overwrite if the current value is
    # still the Acme sample or empty — preserve operator-edited values on re-bootstrap.
    if _is_identity_sample(current_company.get("registration_number")):
        company["registration_number"] = REGISTRATION_PLACEHOLDER
    if _is_identity_sample(current_company.get("tax_registration_number")):
        company["tax_registration_number"] = TAX_REGISTRATION_PLACEHOLDER
    if _is_identity_sample(current_company.get("address")):
        company["address"] = ADDRESS_PLACEHOLDER
    if _is_identity_sample(current_company.get("phone")):
        company["phone"] = COMPANY_PHONE_PLACEHOLDER
    if _is_identity_sample(current_user.get("phone")):
        user["phone"] = USER_PHONE_PLACEHOLDER
    if _is_identity_sample(current_user.get("role")):
        user["role"] = USER_ROLE_PLACEHOLDER
    notices.append(_placeholder_notice(
        "company.registration_number/tax_registration_number/address/phone",
        "not derived from flags — edit company.yaml",
    ))
    notices.append(_placeholder_notice(
        "user.phone/user.role", "not derived from flags — edit company.yaml",
    ))

    # Google Workspace identity: derive domain/alias/SA path from operator when
    # possible; always placeholder the Drive root (cannot be inferred).
    usable_domain = domain if domain and domain not in FREEMAIL_DOMAINS else ""
    explicit_company_or_operator = bool(company_name or operator)
    if usable_domain:
        if operator or _is_identity_sample(current_google.get("domain")):
            google["domain"] = usable_domain
        alias = company_slug or usable_domain.split(".", 1)[0]
        if explicit_company_or_operator or _is_identity_sample(current_google.get("account_alias")):
            google["account_alias"] = alias
        if explicit_company_or_operator or _is_identity_sample(current_google.get("service_account_path")):
            google["service_account_path"] = (
                f"~/.hermes/secrets/{alias}-google-service-account.json"
            )
    else:
        if _is_identity_sample(current_google.get("domain")):
            google["domain"] = GOOGLE_DOMAIN_PLACEHOLDER
        if explicit_company_or_operator or _is_identity_sample(current_google.get("account_alias")):
            google["account_alias"] = company_slug or GOOGLE_ALIAS_PLACEHOLDER
        if explicit_company_or_operator or _is_identity_sample(current_google.get("service_account_path")):
            if company_slug:
                google["service_account_path"] = (
                    f"~/.hermes/secrets/{company_slug}-google-service-account.json"
                )
            else:
                google["service_account_path"] = GOOGLE_SA_PATH_PLACEHOLDER
        notices.append(_placeholder_notice(
            "google.domain",
            "freemail/no --operator — set your Workspace domain",
        ))
    if _is_identity_sample(current_google.get("drive_root_folder_id")):
        google["drive_root_folder_id"] = GOOGLE_DRIVE_ROOT_PLACEHOLDER
        notices.append(_placeholder_notice(
            "google.drive_root_folder_id", "set your Drive root folder id",
        ))

    # delivery.home_chat_id: do not unconditionally wipe — preserve real values.
    # The Acme sample sentinel (123456789) is scrubbed in _write_config only
    # when the current value matches the known sample.
    # (No overlay needed here — _deep_update would overwrite unconditionally.)

    overlay["company"] = company
    overlay["user"] = user
    overlay["google"] = google
    return overlay, notices


_IDENTITY_SAMPLE_VALUES = {
    "123456789",
    "202400001a",
    "m90000001a",
    "1 raffles place, #20-01, singapore 048616",
    "+65 6123 4567",
    "+65 9123 4567",
    "https://www.acme-advisory.example",
    "acme-advisory.example",
    "alicia tan",
    "alicia@acme-advisory.example",
    "managing director",
    "~/.hermes/secrets/acme-google-service-account.json",
    "acme-advisory",
    "1a2b3c4d5e6f_example_root_id",
    USER_NAME_PLACEHOLDER.lower(),
    USER_EMAIL_PLACEHOLDER.lower(),
    USER_ROLE_PLACEHOLDER.lower(),
    USER_PHONE_PLACEHOLDER.lower(),
    WEBSITE_PLACEHOLDER.lower(),
    REGISTRATION_PLACEHOLDER.lower(),
    TAX_REGISTRATION_PLACEHOLDER.lower(),
    ADDRESS_PLACEHOLDER.lower(),
    COMPANY_PHONE_PLACEHOLDER.lower(),
    GOOGLE_DOMAIN_PLACEHOLDER.lower(),
    GOOGLE_ALIAS_PLACEHOLDER.lower(),
    GOOGLE_SA_PATH_PLACEHOLDER.lower(),
    GOOGLE_DRIVE_ROOT_PLACEHOLDER.lower(),
}


def _is_identity_sample(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    lowered = text.lower()
    if lowered.startswith("<") and lowered.endswith(">"):
        return True
    return lowered in _IDENTITY_SAMPLE_VALUES or "acme-advisory" in lowered


def _next_steps(provider: str | None) -> list[str]:
    """Provider-gated 'Next steps' (audit #3). Only the selected workspace
    provider's credential guidance is shown; the cron/test steps are neutral."""
    provider = provider or "google_api"
    if provider == "google_api":
        cred = "Set up Google service account/OAuth credentials in company.yaml."
    elif provider == "composio":
        cred = (
            "Connect your Composio workspace: "
            "python shared/scripts/connect_workspace.py --provider composio --connect gmail"
        )
    elif provider == "m365":
        cred = (
            "Set your M365 client-secret env var and grant Entra admin consent "
            "(see docs/SETUP.md Option 3)."
        )
    else:
        cred = "Configure your workspace provider credentials in company.yaml."
    return [
        cred,
        "Run: python shared/scripts/install_cron.py --config shared/config/company.yaml --dry-run",
        "Then run: python shared/scripts/install_cron.py --config shared/config/company.yaml --install",
        "Test briefing by loading the chief-of-staff:daily-briefing skill.",
    ]


WORKSPACE_PROVIDERS = ("google_api", "composio", "m365")
M365_AUTH_MODES = ("client_credentials", "device_code")

# Placeholder values written when the operator omits an identifier. They mirror
# the "<...-guid>" style used in company.yaml.example / docs so it is obvious the
# value must be replaced before the provider will authenticate.
M365_TENANT_PLACEHOLDER = "<directory-tenant-guid>"
M365_CLIENT_PLACEHOLDER = "<application-client-guid>"
COMPOSIO_USER_PLACEHOLDER = "<composio-user-id>"


def _validate_provider_args(args: argparse.Namespace) -> str | None:
    """Return an error message if the chosen provider's flags are inconsistent,
    else None. m365 + client_credentials REQUIRES a mailbox UPN (the app-only
    flow operates on /users/{user_principal}/...)."""
    provider = getattr(args, "workspace_provider", None)
    if provider == "m365":
        auth = getattr(args, "m365_auth", None) or "client_credentials"
        if auth == "client_credentials" and not getattr(args, "user_principal", None):
            return (
                "m365 client_credentials auth requires --user-principal "
                "(the mailbox UPN, e.g. cos@yourtenant.com). Pass it, or use "
                "--m365-auth device_code for interactive delegated sign-in."
            )
    return None


def _provider_overlay(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], list[str], list[str], list[str]]:
    """Build the config overlay for the chosen workspace provider.

    Returns ``(overlay, required_env, notices, next_commands)``:
      * ``overlay``       — a config fragment deep-merged into company.yaml. It
        carries the ``integrations.workspace.provider`` block plus the provider's
        NON-SECRET config section (never a secret value). EMPTY for the default /
        ``google_api`` path so existing invocations are byte-for-byte unchanged.
      * ``required_env``  — env var names the operator must still export.
      * ``notices``       — human-readable notes (e.g. placeholders written).
      * ``next_commands`` — suggested follow-up commands (doctor + verify).
    """
    provider = getattr(args, "workspace_provider", None)
    overlay: dict[str, Any] = {}
    required_env: list[str] = []
    notices: list[str] = []
    next_commands: list[str] = []

    # Default / google_api: leave integrations untouched (back-compat).
    if not provider or provider == "google_api":
        return overlay, required_env, notices, next_commands

    if provider == "composio":
        user_id = getattr(args, "composio_user_id", None) or COMPOSIO_USER_PLACEHOLDER
        if user_id == COMPOSIO_USER_PLACEHOLDER:
            notices.append(
                f"Wrote placeholder integrations.workspace.user_id={COMPOSIO_USER_PLACEHOLDER}; "
                "set it to your real Composio user id before connecting."
            )
        family = (getattr(args, "composio_family", None) or "google").strip().lower()
        if family not in ("google", "microsoft"):
            family = "google"
        # share_point is required for OneDrive-for-Business files.untrash
        # (Personal Graph restore does not work on work accounts).
        toolkits = (
            ["outlook", "one_drive", "share_point"] if family == "microsoft"
            else ["gmail", "googlecalendar", "googledrive"]
        )
        overlay["integrations"] = {
            "workspace": {
                "provider": "composio",
                "family": family,
                "user_id": user_id,
                "toolkits": toolkits,
                "mcp": {
                    "endpoint": "https://connect.composio.dev/mcp",
                    "key_env": "COMPOSIO_MCP_KEY",
                },
            }
        }
        required_env.append("COMPOSIO_MCP_KEY")
        if family == "microsoft":
            notices.append(
                "Composio family=microsoft: connect Outlook, OneDrive, and SharePoint "
                "(SharePoint powers OneDrive Business files.untrash via the recycle "
                "bin). Optionally set integrations.workspace.sharepoint_site_name to "
                "your OneDrive personal path (e.g. /personal/user_contoso_com). "
                "Tool slugs are overridable via integrations.workspace.tool_slugs "
                "(see company.yaml.example)."
            )
            next_commands = [
                "python shared/scripts/doctor.py",
                "python shared/scripts/connect_workspace.py --provider composio --connect outlook",
                "python shared/scripts/connect_workspace.py --provider composio --connect one_drive",
                "python shared/scripts/connect_workspace.py --provider composio --connect share_point",
                "python shared/scripts/connect_workspace.py --provider composio --verify",
            ]
        else:
            next_commands = [
                "python shared/scripts/doctor.py",
                "python shared/scripts/connect_workspace.py --provider composio --verify",
            ]
        return overlay, required_env, notices, next_commands

    if provider == "m365":
        auth = getattr(args, "m365_auth", None) or "client_credentials"
        secret_env = getattr(args, "m365_secret_env", None) or "M365_CLIENT_SECRET"
        tenant_id = getattr(args, "tenant_id", None)
        client_id = getattr(args, "client_id", None)
        user_principal = getattr(args, "user_principal", None) or ""

        if not tenant_id:
            tenant_id = M365_TENANT_PLACEHOLDER
            notices.append(
                f"Wrote placeholder m365.tenant_id={M365_TENANT_PLACEHOLDER}; "
                "replace it with your Entra Directory (tenant) ID."
            )
        if not client_id:
            client_id = M365_CLIENT_PLACEHOLDER
            notices.append(
                f"Wrote placeholder m365.client_id={M365_CLIENT_PLACEHOLDER}; "
                "replace it with your Entra Application (client) ID."
            )

        m365_block: dict[str, Any] = {
            "tenant_id": tenant_id,
            "client_id": client_id,
            "client_secret_env": secret_env,
            "auth": auth,
        }
        if user_principal:
            m365_block["user_principal"] = user_principal

        overlay["integrations"] = {"workspace": {"provider": "m365"}}
        overlay["m365"] = m365_block

        if auth == "client_credentials":
            required_env.append(secret_env)
        else:
            notices.append(
                "device_code auth uses interactive delegated sign-in; no client "
                "secret env var is required."
            )
        next_commands = [
            "python shared/scripts/doctor.py",
            "python shared/scripts/connect_workspace.py --provider m365 --verify",
        ]
        return overlay, required_env, notices, next_commands

    return overlay, required_env, notices, next_commands


def _esign_overlay(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], list[str], list[str], list[str]]:
    """Build the config overlay for DocuSeal eSign Connector.

    Returns ``(overlay, required_env, notices, next_commands)`` — same shape
    as ``_provider_overlay`` but for the esign integration (not the workspace
    provider, which is orthogonal).
    """
    esign_url = getattr(args, "esign_url", None)
    if not esign_url:
        return {}, [], [], []

    esign_url = esign_url.rstrip("/")
    from urllib.parse import urlparse
    parsed = urlparse(esign_url)
    if parsed.scheme not in ("https", "http"):
        return {}, [], [f"Invalid esign URL: must start with https:// (got {esign_url})"], []
    if not parsed.hostname:
        return {}, [], [f"Invalid esign URL: no hostname (got {esign_url})"], []
    if parsed.scheme == "http" and not getattr(args, "allow_insecure_esign_url", False):
        return {}, [], [f"DocuSeal URL must be HTTPS: {esign_url} (use --allow-insecure-esign-url for local dev)"], []
    domain = parsed.hostname

    overlay: dict[str, Any] = {
        "esign": {
            "provider": "docuseal",
            "url": esign_url,
            "domain": domain,
            "auth_mode": "auto",
            "file_serving": {
                "mode": "existing",
                "public_base_url": None,
                "cleanup_after_send": True,
            },
            "defaults": {
                "signing_order": "random",
                "cancel_before_resend": True,
            },
            "field_detection": {
                "prefer": "auto",
                "page_indexing": "zero_based",
            },
        }
    }

    required_env = ["DOCUSEAL_MCP_TOKEN", "DOCUSEAL_API_KEY"]
    notices = [
        f"Configured esign.url={esign_url}",
        "Create both tokens in DocuSeal Settings:",
        "  - MCP token: Settings → MCP Server → create token",
        "  - API key: Settings → API → create access token",
        "  - Store both in .env (never in company.yaml)",
        "Ensure SMTP is configured in DocuSeal Settings → Email → SMTP",
        "  (signing request emails won't be sent without it)",
        "Ensure the DocuSeal URL is reachable by external signers",
        "  (e.g. via a tunnel or public domain pointing to the instance)",
    ]
    next_commands = [
        "python shared/scripts/doctor.py",
    ]
    return overlay, required_env, notices, next_commands


def _deep_update(base: dict[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)  # type: ignore[index]
        else:
            base[key] = value
    return base


def _current_config_with_preset(preset: Mapping[str, Any]) -> dict[str, Any]:
    """Return the current live/example config with explicit preset values merged."""
    path = CONFIG_DIR / "company.yaml"
    if path.exists():
        data = _load_yaml(path)
    else:
        example = CONFIG_DIR / "company.yaml.example"
        data = _load_yaml(example) if example.exists() else {}
    return _deep_update(data, copy.deepcopy(dict(preset)))


def _write_config(preset: Mapping[str, Any]) -> Path:
    path = CONFIG_DIR / "company.yaml"
    if not path.exists():
        example = CONFIG_DIR / "company.yaml.example"
        if example.exists():
            shutil.copy2(example, path)
        else:
            path.write_text("company: {}\ngoogle: {}\npaths: {}\ndelivery: {}\n", encoding="utf-8")
    data = _load_yaml(path)
    _deep_update(data, preset)
    company = data.setdefault("company", {})
    google = data.setdefault("google", {})
    paths = data.setdefault("paths", {})
    delivery = data.setdefault("delivery", {})
    assistant = data.setdefault("assistant", {})
    # Scrub the Acme sample home_chat_id sentinel only; preserve real values.
    if str(delivery.get("home_chat_id", "")).strip() == "123456789":
        delivery["home_chat_id"] = None
    esign = data.get("esign")
    if (
        isinstance(esign, dict)
        and esign.get("admin_email")
        and (not esign.get("provider_email") or esign.get("provider_email") == "you@yourdomain.com")
    ):
        esign["provider_email"] = esign["admin_email"]
    company.setdefault("name", "Acme Pte Ltd")
    company.setdefault("jurisdiction", "SG")
    company.setdefault("incorporation_date", "2026-01-01")
    company.setdefault("financial_year_end", "31 Dec")
    company.setdefault("currency", "SGD")
    company.setdefault("business_type", "professional_services")
    assistant.setdefault("name", "Chief of Staff")
    from config_loader import get_hermes_home, get_default_project_root
    _home = str(get_hermes_home())
    google.setdefault("service_account_path", f"{_home}/google_service_account.json")
    google.setdefault("domain", "example.com")
    google.setdefault("delegate_email", "operator@example.com")
    paths.setdefault("project_root", str(get_default_project_root("chief-of-staff")))
    paths.setdefault("wiki_path", str(Path(str(paths["project_root"])).expanduser() / "wiki"))
    paths.setdefault("templates", str(PLUGIN_ROOT / "shared" / "templates"))
    delivery.setdefault("channel", "telegram")
    delivery.setdefault("briefing_time", "08:00")
    delivery.setdefault("weekly_review_day", "friday")
    delivery.setdefault("weekly_review_time", "17:00")
    delivery.setdefault("timezone", "UTC")
    data.setdefault("sales_stages", ["Lead", "Proposal Sent", "NDA Signed", "Contract Signed", "Invoiced", "Paid"])
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


# Skills whose description field should include the assistant name for routing.
# The description is the first thing the agent sees in the system prompt's skill
# list, so embedding the name there ensures the agent routes named requests
# ("Ask Arthur to check my email") to CoS skills instead of generic handlers.
ROUTING_SKILLS = [
    "daily-briefing",
    "drive-filer",
    "email-organisation",
    "calendar-manager",
    "meeting-prep",
]


# Default location of the routing skills; module-level so tests can point it at
# a temporary copy — the test suite must NEVER write to the real SKILL.md files
# (see tests/conftest.py, which patches this for every test).
SKILLS_DIR = PLUGIN_ROOT / "skills"
RENDERED_SKILLS_DIR = PLUGIN_ROOT / "skills.local"

_ROUTING_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._&'()+,/-]{0,63}$")

ROUTING_DESCRIPTION_TEMPLATES = {
    "daily-briefing": (
        "Use when producing the Chief-of-Staff daily command-center briefing from Gmail, Calendar, "
        "deadlines, pipeline, to-dos, and bookkeeping sources. When the user addresses '{assistant_name}' "
        "(the CoS assistant name), route all mail/calendar/file operations through the company workspace "
        "account configured in company.yaml for {company_name}, NOT the agent's personal email."
    ),
    "drive-filer": (
        "File email attachments and local project documents into the Chief of Staff Google Drive structure "
        "using configurable drive-map.yaml rules. When the user addresses '{assistant_name}' (the CoS "
        "assistant name) to file or sync documents, use the company workspace account configured in "
        "company.yaml for {company_name} for all Gmail/Drive operations, NOT the agent's personal email."
    ),
    "email-organisation": (
        "Use when inspecting Gmail labels, proposing or saving a label policy, or when the operator "
        "addresses '{assistant_name}' (the CoS assistant name) to check email (e.g. 'Ask {assistant_name} "
        "to check my email'). Route all Gmail operations through the company workspace account configured "
        "in company.yaml for {company_name}, NOT the agent's personal email."
    ),
    "calendar-manager": (
        "Calendar visibility and safe Google Calendar operations for the Chief of Staff plugin, including "
        "proactive pre-meeting prep reminders via one-shot Hermes cron jobs. When the user addresses "
        "'{assistant_name}' (the CoS assistant name) for calendar operations, use the company workspace "
        "account configured in company.yaml for {company_name}, NOT the agent's personal email."
    ),
    "meeting-prep": (
        "Use when preparing a concise pre-meeting intelligence brief from calendar event metadata, recent "
        "Gmail threads, wiki notes, pipeline status, invoices, to-dos, and entity research. When the user "
        "addresses '{assistant_name}' (the CoS assistant name) for meeting prep, use the company workspace "
        "account configured in company.yaml for {company_name}, NOT the agent's personal email."
    ),
}


def _validate_routing_name(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise BootstrapError(f"{field} cannot be blank")
    if len(text) > 64:
        raise BootstrapError(f"{field} must be 64 characters or fewer")
    if "\n" in text or "\r" in text or '"' in text:
        raise BootstrapError(f"{field} may not contain newlines or double quotes")
    if not _ROUTING_NAME_RE.fullmatch(text):
        raise BootstrapError(
            f"{field} may contain only letters, numbers, spaces, and . _ & ' ( ) + , / -"
        )
    return text


def _yaml_quoted(value: str) -> str:
    return yaml.safe_dump(
        value,
        default_style='"',
        allow_unicode=True,
        width=4096,
        sort_keys=False,
    ).strip()


def _frontmatter_yaml(text: str) -> str:
    if not text.startswith("---"):
        raise BootstrapError("SKILL.md missing YAML frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise BootstrapError("SKILL.md has incomplete YAML frontmatter")
    return parts[1]


def _routing_overlay_manifest_path(overlay: Path) -> Path:
    return overlay / ROUTING_OVERLAY_MANIFEST


def _load_routing_overlay_manifest(overlay: Path) -> list[str]:
    path = _routing_overlay_manifest_path(overlay)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    skills = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(skills, list):
        return []
    return [str(skill) for skill in skills if str(skill) in ROUTING_SKILLS]


def _write_routing_overlay_manifest(overlay: Path, skills: list[str]) -> None:
    if not skills:
        return
    path = _routing_overlay_manifest_path(overlay)
    path.write_text(
        json.dumps({"skills": sorted(set(skills))}, indent=2) + "\n",
        encoding="utf-8",
    )


def _cleanup_routing_overlays(overlay: Path) -> None:
    generated_skills = _load_routing_overlay_manifest(overlay)
    if not generated_skills:
        return
    for skill_slug in generated_skills:
        shutil.rmtree(overlay / skill_slug, ignore_errors=True)
    manifest = _routing_overlay_manifest_path(overlay)
    if manifest.exists():
        manifest.unlink()
    try:
        next(overlay.iterdir())
    except StopIteration:
        overlay.rmdir()


def _inject_assistant_name_into_skills(
    config_path: Path,
    skills_dir: Path | None = None,
    rendered_dir: Path | None = None,
) -> list[str]:
    """Render custom routing-skill descriptions into an ignored overlay.

    Shipped ``skills/*/SKILL.md`` files stay git-clean. When a non-default
    assistant name is configured, rendered copies are written under
    ``skills.local/`` (or a test-supplied overlay directory).
    """
    config = _load_yaml(config_path)
    assistant_name = (
        config.get("assistant", {}).get("name", "")
        if isinstance(config.get("assistant"), dict)
        else ""
    )
    company_name = config.get("company", {}).get("name", "") if isinstance(config.get("company"), dict) else ""
    base = skills_dir if skills_dir is not None else SKILLS_DIR
    overlay = rendered_dir if rendered_dir is not None else (
        RENDERED_SKILLS_DIR if skills_dir is None else base.parent / "skills.local"
    )
    if is_default_assistant_name(assistant_name):
        if overlay.exists():
            _cleanup_routing_overlays(overlay)
        return []  # nothing to inject — still using the default
    assistant_name = _validate_routing_name(assistant_name, "assistant.name")
    company_name = _validate_routing_name(company_name, "company.name") if company_name else "your organization"

    messages: list[str] = []
    rendered_skills: list[str] = []
    for skill_slug in ROUTING_SKILLS:
        skill_md = base / skill_slug / "SKILL.md"
        if not skill_md.exists():
            continue
        lines = skill_md.read_text(encoding="utf-8").splitlines(keepends=True)
        desc_idx = None
        for i, line in enumerate(lines):
            if line.startswith("description:") and desc_idx is None:
                desc_idx = i
        if desc_idx is None:
            continue

        template_value = ROUTING_DESCRIPTION_TEMPLATES[skill_slug]
        rendered = (
            template_value
            .replace("{assistant_name}", assistant_name)
            .replace("{company_name}", company_name)
        )
        new_desc_line = f"description: {_yaml_quoted(rendered)}\n"

        out_dir = overlay / skill_slug
        out_dir.mkdir(parents=True, exist_ok=True)
        out_md = out_dir / "SKILL.md"
        lines[desc_idx] = new_desc_line
        rendered_text = "".join(lines)
        fm = yaml.safe_load(_frontmatter_yaml(rendered_text)) or {}
        if not isinstance(fm, dict) or fm.get("description") != rendered:
            raise BootstrapError(f"Rendered {skill_slug}/SKILL.md failed YAML frontmatter validation")
        previous = out_md.read_text(encoding="utf-8") if out_md.exists() else None
        if previous != rendered_text:
            out_md.write_text(rendered_text, encoding="utf-8")
            messages.append(f"Rendered '{assistant_name}' into {out_md}")
        rendered_skills.append(skill_slug)
    _write_routing_overlay_manifest(overlay, rendered_skills)
    return messages


def _project_root(config: Mapping[str, Any], config_path: Path) -> Path:
    raw = config["paths"]["project_root"]
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def _init_stores(root: Path) -> list[str]:
    root.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for store, template in EMPTY_TEMPLATES.items():
        path = root / f"{store}.yaml"
        if not path.exists():
            path.write_text(yaml.safe_dump(template, sort_keys=False), encoding="utf-8")
            written.append(str(path))
    return written


def _init_wiki(config: Mapping[str, Any], root: Path) -> list[str]:
    paths = config.get("paths", {}) if isinstance(config.get("paths"), Mapping) else {}
    wiki = Path(str(paths.get("wiki_path") or (root / "wiki"))).expanduser()
    if not wiki.is_absolute():
        wiki = root / wiki
    wiki.mkdir(parents=True, exist_ok=True)
    business_type = ((config.get("company") or {}).get("business_type") if isinstance(config.get("company"), Mapping) else None) or "business"
    files = {
        "purpose.md": f"# Purpose\n\nOperating wiki for a {business_type} company. Record durable context, decisions, and procedures here.\n",
        "SCHEMA.md": "# Wiki Schema\n\n- `purpose.md` explains company context.\n- Link source documents by relative path.\n- Prefer dated notes for decisions and recurring procedures.\n",
    }
    written: list[str] = []
    for name, text in files.items():
        path = wiki / name
        if not path.exists():
            path.write_text(text, encoding="utf-8")
            written.append(str(path))
    return written


def bootstrap(args: argparse.Namespace) -> dict[str, Any]:
    # Run doctor --fix first as requested; keep going so bootstrap can create config.
    initial = run_checks(fix=True, config=None)
    copied = _copy_examples()
    preset = _merge_preset(args)
    current_config = _current_config_with_preset(preset)
    identity_overlay, identity_notices = _identity_overlay(args, current_config)
    if identity_overlay:
        _deep_update(preset, identity_overlay)
    overlay, required_env, provider_notices, provider_next = _provider_overlay(args)
    esign_overlay, esign_required_env, esign_notices, esign_next = _esign_overlay(args)
    if overlay:
        _deep_update(preset, overlay)
    if esign_overlay:
        _deep_update(preset, esign_overlay)
    config_path = _write_config(preset)
    config = _load_yaml(config_path)
    root = _project_root(config, config_path)
    stores = _init_stores(root)
    wiki = _init_wiki(config, root)
    skill_injections = _inject_assistant_name_into_skills(config_path)
    final = run_checks(fix=True, config=str(config_path))
    provider = getattr(args, "workspace_provider", None)
    result: dict[str, Any] = {
        "config": str(config_path),
        "project_root": str(root),
        "copied_examples": copied,
        "initialized_stores": stores,
        "initialized_wiki": wiki,
        "doctor_initial": [r.__dict__ for r in initial],
        "doctor_final": [r.__dict__ for r in final],
        "next_steps": _next_steps(provider),
        "identity_notices": identity_notices,
        "assistant_name": getattr(args, "assistant_name", None) or "Chief of Staff",
        "skill_injections": skill_injections,
    }
    # Only surface provider metadata when a non-default provider is chosen, so a
    # default (google) invocation's JSON/text output stays byte-compatible.
    if provider and provider != "google_api":
        result["workspace_provider"] = provider
        result["required_env"] = required_env
        result["provider_notices"] = provider_notices
        result["provider_next_commands"] = provider_next
    # Surface esign onboarding metadata when --esign-url was provided.
    if esign_overlay:
        result["esign_configured"] = True
        result["required_env"] = result.get("required_env", []) + esign_required_env
        result["esign_notices"] = esign_notices
        result["esign_next_commands"] = esign_next
    return result


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministically bootstrap the Chief-of-Staff plugin")
    parser.add_argument("--company", help="Company name")
    parser.add_argument("--jurisdiction", help="Jurisdiction code, e.g. SG")
    parser.add_argument("--operator", help="Operator/delegate email")
    parser.add_argument(
        "--operator-name",
        help="Operator's display name (default: derived from the operator email local-part)",
    )
    parser.add_argument(
        "--assistant-name", default=None,
        help="Name for this assistant, written to assistant.name in company.yaml "
             "when provided. Defaults to 'Chief of Staff' on first install. Address it by name so requests route to "
             "these skills instead of generic handlers.",
    )
    parser.add_argument("--project-root", help="Project root directory")
    parser.add_argument("--business-type", help="Business type for wiki seed")
    parser.add_argument("--config", help="Preset YAML to merge non-interactively")
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    # Provider-aware workspace bootstrap. Omitting --workspace-provider preserves
    # the historical behaviour exactly (no integrations block is rewritten).
    parser.add_argument(
        "--workspace-provider", choices=WORKSPACE_PROVIDERS,
        help="Workspace backend to configure (default: leave integrations unchanged)",
    )
    parser.add_argument(
        "--m365-auth", choices=M365_AUTH_MODES, default="client_credentials",
        help="M365 auth mode (default: client_credentials)",
    )
    parser.add_argument("--tenant-id", help="M365 Entra Directory (tenant) ID")
    parser.add_argument("--client-id", help="M365 Entra Application (client) ID")
    parser.add_argument(
        "--user-principal",
        help="M365 mailbox UPN (required for m365 client_credentials)",
    )
    parser.add_argument(
        "--m365-secret-env", default="M365_CLIENT_SECRET",
        help="Env var holding the M365 client secret (default: M365_CLIENT_SECRET)",
    )
    parser.add_argument("--composio-user-id", help="Composio user id")
    parser.add_argument(
        "--composio-family", choices=("google", "microsoft"), default="google",
        help="Composio toolkit family: google (Gmail/Calendar/Drive) or "
             "microsoft (Outlook/OneDrive). Default: google.",
    )
    parser.add_argument(
        "--esign-url", default=None,
        help="DocuSeal instance URL (e.g. https://sign.yourdomain.com). "
             "Enables esign-connector onboarding. Requires DOCUSEAL_MCP_TOKEN "
             "and DOCUSEAL_API_KEY in .env.",
    )
    parser.add_argument(
        "--allow-insecure-esign-url", action="store_true",
        help="Allow http:// (non-HTTPS) DocuSeal URLs for local development.",
    )
    args = parser.parse_args(argv)

    err = _validate_provider_args(args)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    result = bootstrap(args)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Bootstrapped Chief-of-Staff config: {result['config']}")
        print(f"Project root: {result['project_root']}")
        print("Next steps:")
        for step in result["next_steps"]:
            print(f"- {step}")
        # Announce any field left as a placeholder (audit #2).
        placeholders = result.get("identity_notices", [])
        if placeholders:
            print("Placeholders to complete:")
            for note in placeholders:
                print(f"- {note}")
        # Assistant naming guidance (feature 2c).
        aname = result.get("assistant_name") or "Chief of Staff"
        print(
            f"\nYour Chief of Staff is named '{aname}'. Address it by name "
            f"(\"Ask {aname} to check my email\") so requests route to these "
            f"skills instead of generic handlers."
        )

    # Provider-specific guidance: placeholders written, required env vars, and
    # the suggested next commands (doctor + connect_workspace verify).
    if result.get("workspace_provider"):
        print(f"\nWorkspace provider: {result['workspace_provider']}")
        for note in result.get("provider_notices", []):
            print(f"- {note}")
        for var in result.get("required_env", []):
            print(f"Set {var} before running doctor/verify.")
        next_cmds = result.get("provider_next_commands", [])
        if next_cmds:
            print("Suggested next commands:")
            for cmd in next_cmds:
                print(f"  {cmd}")

    # esign onboarding output
    if result.get("esign_configured"):
        print("\nDocuSeal eSign Connector:")
        for note in result.get("esign_notices", []):
            print(f"- {note}")
        for var in result.get("required_env", []):
            if var.startswith("DOCUSEAL"):
                print(f"Set {var} in .env before running doctor.")
        esign_cmds = result.get("esign_next_commands", [])
        if esign_cmds:
            print("Suggested next commands:")
            for cmd in esign_cmds:
                print(f"  {cmd}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
