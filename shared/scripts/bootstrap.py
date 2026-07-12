#!/usr/bin/env python3
"""Bootstrap chief-of-staff plugin from a fresh clone.

Usage:
    python bootstrap.py --company "Acme Pte Ltd" --jurisdiction SG --operator founder@acme.com
    python bootstrap.py --config preset.yaml  # non-interactive
"""

from __future__ import annotations

import argparse
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
    # Assistant identity — always written (argparse defaults to "Chief of Staff").
    # Existing unit tests build args without this attribute, so guard with getattr.
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

FREEMAIL_DOMAINS = {"gmail.com", "outlook.com", "yahoo.com", "hotmail.com"}
USER_NAME_PLACEHOLDER = "<operator-name>"
USER_EMAIL_PLACEHOLDER = "<operator-email>"
WEBSITE_PLACEHOLDER = "<company-website>"


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
) -> tuple[dict[str, Any], list[str]]:
    """Derive company/user/paths identity from the flags and return
    ``(overlay, notices)``. The overlay is deep-merged over the example so the
    canned Acme values never survive. ``notices`` announces any placeholder."""
    from config_loader import get_default_project_root

    overlay: dict[str, Any] = {}
    notices: list[str] = []
    company: dict[str, Any] = {}
    user: dict[str, Any] = {}

    # paths.project_root: explicit flag > company slug > generic default.
    # wiki_path/staging follow the root so they never keep the Acme sample path.
    explicit_root = getattr(args, "project_root", None)
    company_name = getattr(args, "company", None)
    if explicit_root:
        root = str(Path(explicit_root).expanduser())
    elif company_name and _slugify_company(company_name):
        root = str(get_default_project_root(_slugify_company(company_name)))
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
    else:
        user["name"] = USER_NAME_PLACEHOLDER
        user["email"] = USER_EMAIL_PLACEHOLDER
        company["website"] = WEBSITE_PLACEHOLDER
        notices.append(_placeholder_notice("user.name/user.email", "no --operator given"))
        notices.append(_placeholder_notice("company.website", "no --operator given"))

    if company:
        overlay["company"] = company
    if user:
        overlay["user"] = user
    return overlay, notices


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
        overlay["integrations"] = {
            "workspace": {
                "provider": "composio",
                "user_id": user_id,
                "mcp": {
                    "endpoint": "https://connect.composio.dev/mcp",
                    "key_env": "COMPOSIO_MCP_KEY",
                },
            }
        }
        required_env.append("COMPOSIO_MCP_KEY")
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
    company.setdefault("name", "Acme Pte Ltd")
    company.setdefault("jurisdiction", "SG")
    company.setdefault("incorporation_date", "2026-01-01")
    company.setdefault("financial_year_end", "31 Dec")
    company.setdefault("currency", "SGD")
    company.setdefault("business_type", "professional_services")
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
    identity_overlay, identity_notices = _identity_overlay(args)
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
        "--assistant-name", default="Chief of Staff",
        help="Name for this assistant, written to assistant.name in company.yaml "
             "(default: 'Chief of Staff'). Address it by name so requests route to "
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
