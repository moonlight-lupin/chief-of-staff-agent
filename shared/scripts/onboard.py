#!/usr/bin/env python3
"""Interactive onboarding wizard for the Chief-of-Staff Hermes plugin.

Run interactively:
    python3 onboard.py

Run from a preset for automation:
    python3 onboard.py --non-interactive --config preset.yaml --output company.yaml

The script writes ``shared/config/company.yaml`` by default and initializes a
local wiki with ``purpose.md`` and ``SCHEMA.md``. It intentionally uses only the
Python standard library plus PyYAML.
"""

from __future__ import annotations

import argparse
import copy
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover - depends on host package state.
    print("PyYAML is required for this onboarding wizard. Install with: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from exc

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = PLUGIN_ROOT / "shared" / "config" / "company.yaml"
DEFAULT_SALES_STAGES = [
    "Lead",
    "Qualified",
    "Proposal Sent",
    "NDA Signed",
    "Contract Sent",
    "Contract Signed",
    "Invoiced",
    "Paid",
    "Lost",
]
SUPPORTED_JURISDICTIONS = {"SG", "HK", "US", "UK"}
SUPPORTED_CHANNELS = {"telegram", "whatsapp", "email"}
DEFAULT_BUSINESS_TYPE = "professional_services"
BUSINESS_TYPE_PROFILES: dict[str, dict[str, list[str] | str]] = {
    "professional_services": {
        "label": "Professional services / consulting",
        "purpose": "run a lean client-services firm with reliable follow-up, filings, finance, research, and document workflows",
        "entities": ["clients", "prospects", "vendors", "partners", "advisors", "authorities"],
        "concepts": ["offerings", "pricing", "case studies", "regulatory obligations", "playbooks"],
        "documents": ["proposals", "NDAs", "SOWs", "contracts", "invoices", "meeting notes"],
    },
    "agency": {
        "label": "Agency / studio",
        "purpose": "coordinate creative delivery, client approvals, campaign assets, pipeline, finance, and recurring reporting",
        "entities": ["clients", "campaigns", "vendors", "creators", "contractors", "platforms"],
        "concepts": ["brand guidelines", "campaign strategy", "creative references", "performance metrics", "retainers"],
        "documents": ["briefs", "proposals", "NDAs", "SOWs", "asset lists", "invoices"],
    },
    "software_saas": {
        "label": "Software / SaaS",
        "purpose": "manage product, sales, customer success, compliance, vendor agreements, and investor-ready operating records",
        "entities": ["customers", "prospects", "users", "vendors", "integrations", "investors"],
        "concepts": ["product areas", "roadmap themes", "security controls", "pricing plans", "support playbooks"],
        "documents": ["MSAs", "DPAs", "SOWs", "invoices", "release notes", "security questionnaires"],
    },
    "trading_ecommerce": {
        "label": "Trading / e-commerce",
        "purpose": "track suppliers, inventory, customer orders, logistics, marketplace operations, cashflow, and compliance deadlines",
        "entities": ["customers", "suppliers", "marketplaces", "logistics providers", "products", "regulators"],
        "concepts": ["SKUs", "margin rules", "shipping policies", "returns process", "tax obligations"],
        "documents": ["purchase orders", "invoices", "receipts", "shipping docs", "supplier contracts", "tax records"],
    },
    "investment_holdco": {
        "label": "Investment / holding company",
        "purpose": "maintain portfolio intelligence, governance records, reporting packs, transaction documents, and statutory obligations",
        "entities": ["portfolio companies", "investors", "directors", "service providers", "banks", "authorities"],
        "concepts": ["investment theses", "valuation methods", "risk factors", "governance", "reporting cadence"],
        "documents": ["board papers", "term sheets", "subscription docs", "financial reports", "resolutions", "tax filings"],
    },
}

PARTIAL_DATA: dict[str, Any] = {}
PARTIAL_OUTPUT: Path | None = None


class OnboardingError(ValueError):
    """Raised for validation errors that should be shown to the operator."""


def slugify(value: str, default: str = "company") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or default


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_yaml(path: Path) -> dict[str, Any]:
    with path.expanduser().open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise OnboardingError(f"Preset config must be a YAML mapping: {path}")
    return loaded


def dump_yaml(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.expanduser().open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True, default_flow_style=False)


def prompt_text(
    label: str,
    default: str | None = None,
    required: bool = True,
    validator: Callable[[str], str] | None = None,
    help_text: str | None = None,
) -> str:
    if help_text:
        print(help_text)
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        raw = input(f"{label}{suffix}: ").strip()
        if raw == "" and default is not None:
            raw = str(default)
        if raw == "" and not required:
            return ""
        if raw == "":
            print("  This field is required.")
            continue
        try:
            return validator(raw) if validator else raw
        except OnboardingError as exc:
            print(f"  {exc}")


def prompt_bool(label: str, default: bool = False) -> bool:
    marker = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{label} [{marker}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "true", "1"}:
            return True
        if raw in {"n", "no", "false", "0"}:
            return False
        print("  Enter yes or no.")


def prompt_int(label: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    while True:
        raw = input(f"{label} [{default}]: ").strip()
        if raw == "":
            return default
        try:
            value = int(raw)
        except ValueError:
            print("  Enter a whole number.")
            continue
        if minimum is not None and value < minimum:
            print(f"  Enter a value >= {minimum}.")
            continue
        if maximum is not None and value > maximum:
            print(f"  Enter a value <= {maximum}.")
            continue
        return value


def validate_choice(choices: set[str], transform: Callable[[str], str] = lambda s: s) -> Callable[[str], str]:
    def _validator(value: str) -> str:
        normalized = transform(value)
        if normalized not in choices:
            raise OnboardingError(f"Choose one of: {', '.join(sorted(choices))}")
        return normalized

    return _validator


def validate_date(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise OnboardingError("Use ISO date format YYYY-MM-DD.") from exc
    return value


def validate_fy_end(value: str) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return validate_date(value)
    if re.fullmatch(r"\d{1,2}\s+[A-Za-z]{3,9}", value):
        day_text, month_text = value.split(None, 1)
        day = int(day_text)
        if not 1 <= day <= 31:
            raise OnboardingError("Financial year end day must be 1-31.")
        try:
            datetime.strptime(month_text[:3].title(), "%b")
        except ValueError as exc:
            raise OnboardingError("Use a valid month name, e.g. 31 Dec.") from exc
        return f"{day:02d} {month_text[:3].title()}"
    raise OnboardingError("Use 'DD Mon' (e.g. 31 Dec) or ISO date YYYY-MM-DD.")


def validate_currency(value: str) -> str:
    normalized = value.upper()
    if not re.fullmatch(r"[A-Z]{3}", normalized):
        raise OnboardingError("Use a 3-letter ISO currency code, e.g. SGD, USD, GBP.")
    return normalized


def validate_email(value: str) -> str:
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        raise OnboardingError("Enter a valid email address.")
    return value


def validate_time(value: str) -> str:
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise OnboardingError("Use 24-hour time HH:MM, e.g. 20:00.") from exc
    return value


def validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise OnboardingError("Unknown timezone. Use an IANA name such as Asia/Singapore or Europe/London.") from exc
    return value


def normalize_business_type(value: str) -> str:
    normalized = slugify(value, DEFAULT_BUSINESS_TYPE).replace("-", "_")
    return normalized


def default_timezone_for_jurisdiction(jurisdiction: str) -> str:
    return {"SG": "Asia/Singapore", "HK": "Asia/Hong_Kong", "US": "America/New_York", "UK": "Europe/London"}.get(
        jurisdiction, "UTC"
    )


def default_currency_for_jurisdiction(jurisdiction: str) -> str:
    return {"SG": "SGD", "HK": "HKD", "US": "USD", "UK": "GBP"}.get(jurisdiction, "USD")


def check_prerequisites() -> dict[str, Any]:
    print("\n== Prerequisite checks ==")
    hermes_path = shutil.which("hermes")
    hermes: dict[str, Any] = {"installed": False, "path": hermes_path, "version": None}
    if hermes_path:
        try:
            result = subprocess.run([hermes_path, "--version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
            output = result.stdout.strip()
            hermes.update({"installed": result.returncode == 0, "version": output, "returncode": result.returncode})
            if result.returncode == 0:
                print(f"✓ Hermes installed: {output}")
            else:
                print(f"⚠ Hermes command returned {result.returncode}: {output}")
        except (OSError, subprocess.SubprocessError) as exc:
            hermes.update({"error": str(exc)})
            print(f"⚠ Could not run hermes --version: {exc}")
    else:
        print("⚠ Hermes command not found on PATH. Install Hermes before using scheduled workflows.")

    google_skill_path = Path.home() / ".hermes" / "skills" / "productivity" / "google-workspace"
    google_workspace = {"installed": google_skill_path.exists(), "path": str(google_skill_path)}
    if google_skill_path.exists():
        print(f"✓ google-workspace skill found: {google_skill_path}")
    else:
        print(f"⚠ google-workspace skill not found at {google_skill_path}")
        print("  Install it before running Gmail/Calendar/Drive workflows.")
    return {"hermes": hermes, "google_workspace_skill": google_workspace}


def make_default_config(company_name: str = "", jurisdiction: str = "SG", business_type: str = DEFAULT_BUSINESS_TYPE) -> dict[str, Any]:
    slug = slugify(company_name, "company")
    project_root = f"~/.hermes/projects/{slug}/"
    timezone = default_timezone_for_jurisdiction(jurisdiction)
    return {
        "company": {
            "name": company_name,
            "jurisdiction": jurisdiction,
            "incorporation_date": "",
            "financial_year_end": "31 Dec",
            "currency": default_currency_for_jurisdiction(jurisdiction),
            "business_type": business_type,
            "registration_number": "",
            "tax_registration_number": "",
            "address": "",
            "phone": "",
            "website": "",
        },
        "user": {"name": "", "role": "Owner", "email": "", "phone": ""},
        "google": {
            "service_account_path": "",
            "domain": "",
            "delegate_email": "",
            "drive_root_folder_id": "auto",
            "create_drive_structure": True,
            "auth_test": "not_run",
        },
        "paths": {
            "project_root": project_root,
            "wiki_path": f"{project_root}wiki/",
            "templates": str(PLUGIN_ROOT / "shared" / "templates") + "/",
            "staging": f"{project_root}staging/",
        },
        "delivery": {
            "channel": "telegram",
            "home_chat_id": "",
            "briefing_time": "20:00",
            "weekly_review_day": "friday",
            "weekly_review_time": "17:00",
            "timezone": timezone,
            "use_client_codes": False,
        },
        "sales_stages": list(DEFAULT_SALES_STAGES),
        "stale_threshold_days": 14,
        "deadlines": {"custom": []},
        "calendar": {
            "reminder_minutes": 15,
            "auto_prep_brief": True,
            "working_days": ["monday", "tuesday", "wednesday", "thursday", "friday"],
            "working_hours": {"start": "09:00", "end": "18:00"},
        },
        "self_sign": {
            "signature_image": "",
            "initials_image": "",
            "company_stamp": "",
            "auto_date": True,
            "output_format": "pdf",
            "party_aliases": ["Service Provider", "Consultant", "Contractor", "The Company"],
        },
        "bookkeeper": {
            "revenue_recognition": "cash",
            "default_payment_terms_days": 14,
            "expense_categories": ["software", "rent", "utilities", "travel", "meals", "professional", "equipment", "tax", "other"],
        },
        "backup": {
            "enabled": True,
            "schedule": "0 3 * * 0",
            "retention_weekly": 4,
            "retention_monthly": 12,
            "drive_folder": "09_Backups/",
            "exclude": [".env", "auth.json", "state.db", "sessions/", "logs/"],
        },
        "onboarding": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "plugin_root": str(PLUGIN_ROOT),
        },
    }


def prompt_sales_stages() -> list[str]:
    print("\n== Sales pipeline stages ==")
    print("Default stages:")
    print("  " + " → ".join(DEFAULT_SALES_STAGES))
    raw = input("Press Enter to keep defaults, or enter comma-separated stages: ").strip()
    if not raw:
        return list(DEFAULT_SALES_STAGES)
    stages = [stage.strip() for stage in raw.split(",") if stage.strip()]
    if len(stages) < 2:
        print("  Need at least two stages; keeping defaults.")
        return list(DEFAULT_SALES_STAGES)
    return stages


def prompt_deadlines(default_owner: str) -> list[dict[str, Any]]:
    print("\n== Custom deadlines ==")
    print("Add business-specific recurring or one-off dates not covered by the jurisdiction pack.")
    deadlines: list[dict[str, Any]] = []
    while prompt_bool("Add a custom deadline?", default=False):
        name = prompt_text("Deadline name")
        due = prompt_text("Due date", validator=validate_date, help_text="Use YYYY-MM-DD for one-off deadlines.")
        authority = prompt_text("Authority/category", default="Internal")
        owner = prompt_text("Owner", default=default_owner or "Owner")
        notes = prompt_text("Notes", default="", required=False)
        deadlines.append({"name": name, "due": due, "authority": authority, "notes": notes, "owner": owner})
    return deadlines


def build_interactive_config() -> dict[str, Any]:
    checks = check_prerequisites()

    print("\n== Company details ==")
    company_name = prompt_text("Company name")
    jurisdiction = prompt_text(
        "Jurisdiction (SG/HK/US/UK)", default="SG", validator=validate_choice(SUPPORTED_JURISDICTIONS, str.upper)
    )
    incorporation_date = prompt_text("Incorporation date", validator=validate_date, help_text="Use ISO date format YYYY-MM-DD.")
    fy_end = prompt_text("Financial year end", default="31 Dec", validator=validate_fy_end)
    currency = prompt_text("Currency", default=default_currency_for_jurisdiction(jurisdiction), validator=validate_currency)
    print("Business type examples:")
    for key, profile in BUSINESS_TYPE_PROFILES.items():
        print(f"  - {key}: {profile['label']}")
    business_type = prompt_text("Business type", default=DEFAULT_BUSINESS_TYPE, validator=normalize_business_type)

    config = make_default_config(company_name, jurisdiction, business_type)
    config["checks"] = checks
    config["company"].update(
        {
            "name": company_name,
            "jurisdiction": jurisdiction,
            "incorporation_date": incorporation_date,
            "financial_year_end": fy_end,
            "currency": currency,
            "business_type": business_type,
            "registration_number": prompt_text("Company registration number", default="", required=False),
            "tax_registration_number": prompt_text("Tax/GST/VAT registration number", default="", required=False),
            "address": prompt_text("Registered/business address", default="", required=False),
            "phone": prompt_text("Company phone", default="", required=False),
            "website": prompt_text("Company website", default="", required=False),
        }
    )
    PARTIAL_DATA.clear()
    PARTIAL_DATA.update(config)

    print("\n== Primary operator ==")
    user_name = prompt_text("Your name", default="", required=False)
    user_email_default = ""
    config["user"].update(
        {
            "name": user_name,
            "role": prompt_text("Your role", default="Owner"),
            "email": prompt_text("Your email", default=user_email_default, required=False, validator=lambda v: validate_email(v) if v else v),
            "phone": prompt_text("Your phone", default="", required=False),
        }
    )
    PARTIAL_DATA.update(config)

    print("\n== Google Workspace auth ==")
    service_account_path = prompt_text("Service account JSON path", default="", required=False)
    domain_default = ""
    if config["user"].get("email") and "@" in config["user"]["email"]:
        domain_default = config["user"]["email"].split("@", 1)[1]
    domain = prompt_text("Workspace domain", default=domain_default, required=False)
    delegate_default = config["user"].get("email") or ""
    delegate_email = prompt_text(
        "Delegate email", default=delegate_default, required=False, validator=lambda v: validate_email(v) if v else v
    )
    auth_test_requested = prompt_bool("Request Google auth test after setup? (wizard only records this)", default=False)
    drive_root = prompt_text("Drive root folder ID, or 'auto' to create structure later", default="auto")
    config["google"].update(
        {
            "service_account_path": service_account_path,
            "domain": domain,
            "delegate_email": delegate_email,
            "drive_root_folder_id": drive_root,
            "create_drive_structure": drive_root.lower() == "auto",
            "auth_test": "requested_not_run" if auth_test_requested else "not_run",
        }
    )
    PARTIAL_DATA.update(config)

    print("\n== Local project paths ==")
    slug = slugify(company_name)
    project_root = prompt_text("Project data root", default=f"~/.hermes/projects/{slug}/")
    wiki_path = prompt_text("Wiki directory", default=str(Path(project_root).expanduser() / "wiki") if not project_root.startswith("~") else f"{project_root.rstrip('/')}/wiki/")
    config["paths"].update(
        {
            "project_root": project_root,
            "wiki_path": wiki_path,
            "staging": f"{project_root.rstrip('/')}/staging/",
        }
    )
    PARTIAL_DATA.update(config)

    config["sales_stages"] = prompt_sales_stages()
    config["deadlines"] = {"custom": prompt_deadlines(config["user"].get("name", ""))}
    PARTIAL_DATA.update(config)

    print("\n== Delivery and reminders ==")
    channel = prompt_text("Delivery channel", default="telegram", validator=validate_choice(SUPPORTED_CHANNELS, str.lower))
    briefing_time = prompt_text("Daily briefing time", default="20:00", validator=validate_time)
    timezone = prompt_text("Timezone", default=default_timezone_for_jurisdiction(jurisdiction), validator=validate_timezone)
    home_chat_id = ""
    if channel in {"telegram", "whatsapp"}:
        home_chat_id = prompt_text(f"{channel.title()} home chat/contact ID", default="", required=False)
    config["delivery"].update({"channel": channel, "briefing_time": briefing_time, "timezone": timezone, "home_chat_id": home_chat_id})
    config["calendar"].update(
        {
            "reminder_minutes": prompt_int("Calendar reminder minutes before meetings", default=15, minimum=0, maximum=1440),
            "auto_prep_brief": prompt_bool("Auto-generate prep briefs before meetings?", default=True),
        }
    )
    PARTIAL_DATA.update(config)

    print("\n== Self-sign assets ==")
    signature_image = prompt_text("Signature image path", default="", required=False)
    if signature_image and not Path(signature_image).expanduser().exists():
        print("  ⚠ Signature image path does not exist yet; saved anyway so you can add it later.")
    config["self_sign"]["signature_image"] = signature_image
    PARTIAL_DATA.update(config)

    print("\n== Backups ==")
    backups_enabled = prompt_bool("Enable scheduled backups?", default=True)
    config["backup"].update(
        {
            "enabled": backups_enabled,
            "schedule": prompt_text("Backup cron schedule", default="0 3 * * 0") if backups_enabled else "",
            "retention_weekly": prompt_int("Weekly backup retention count", default=4, minimum=0) if backups_enabled else 0,
            "retention_monthly": prompt_int("Monthly backup retention count", default=12, minimum=0) if backups_enabled else 0,
        }
    )
    PARTIAL_DATA.update(config)
    return config


def normalize_non_interactive_config(preset: dict[str, Any]) -> dict[str, Any]:
    company = preset.get("company", {}) if isinstance(preset.get("company"), dict) else {}
    company_name = str(company.get("name") or preset.get("company_name") or "Company").strip()
    jurisdiction = str(company.get("jurisdiction") or preset.get("jurisdiction") or "SG").upper()
    business_type = normalize_business_type(str(company.get("business_type") or preset.get("business_type") or DEFAULT_BUSINESS_TYPE))
    base = make_default_config(company_name, jurisdiction, business_type)
    merged = deep_merge(base, preset)
    merged.setdefault("onboarding", {})["generated_at"] = datetime.now().isoformat(timespec="seconds")
    return merged


def validate_config(data: dict[str, Any]) -> None:
    company = data.get("company")
    if not isinstance(company, dict):
        raise OnboardingError("Missing company section.")
    required_company = ["name", "jurisdiction", "incorporation_date", "financial_year_end", "currency", "business_type"]
    for key in required_company:
        if not str(company.get(key, "")).strip():
            raise OnboardingError(f"Missing required company.{key}")
    if str(company["jurisdiction"]).upper() not in SUPPORTED_JURISDICTIONS:
        raise OnboardingError("company.jurisdiction must be one of SG, HK, US, UK")
    validate_date(str(company["incorporation_date"]))
    validate_fy_end(str(company["financial_year_end"]))
    company["currency"] = validate_currency(str(company["currency"]))
    company["jurisdiction"] = str(company["jurisdiction"]).upper()
    company["business_type"] = normalize_business_type(str(company["business_type"]))

    paths = data.get("paths")
    if not isinstance(paths, dict) or not str(paths.get("project_root", "")).strip() or not str(paths.get("wiki_path", "")).strip():
        raise OnboardingError("paths.project_root and paths.wiki_path are required.")

    google = data.setdefault("google", {})
    if not isinstance(google, dict):
        raise OnboardingError("google must be a mapping.")
    drive_root = str(google.get("drive_root_folder_id") or "auto")
    google["drive_root_folder_id"] = drive_root
    google["create_drive_structure"] = drive_root.lower() == "auto" or bool(google.get("create_drive_structure"))

    delivery = data.setdefault("delivery", {})
    if not isinstance(delivery, dict):
        raise OnboardingError("delivery must be a mapping.")
    channel = str(delivery.get("channel") or "telegram").lower()
    if channel not in SUPPORTED_CHANNELS:
        raise OnboardingError("delivery.channel must be telegram, whatsapp, or email")
    delivery["channel"] = channel
    delivery["briefing_time"] = validate_time(str(delivery.get("briefing_time") or "20:00"))
    delivery["timezone"] = validate_timezone(str(delivery.get("timezone") or default_timezone_for_jurisdiction(company["jurisdiction"])))

    stages = data.get("sales_stages")
    if not isinstance(stages, list) or not all(isinstance(stage, str) and stage.strip() for stage in stages):
        raise OnboardingError("sales_stages must be a non-empty list of strings.")

    deadlines = data.setdefault("deadlines", {"custom": []})
    if not isinstance(deadlines, dict):
        raise OnboardingError("deadlines must be a mapping.")
    custom = deadlines.setdefault("custom", [])
    if not isinstance(custom, list):
        raise OnboardingError("deadlines.custom must be a list.")
    for index, item in enumerate(custom, start=1):
        if not isinstance(item, dict):
            raise OnboardingError(f"deadlines.custom[{index}] must be a mapping.")
        if not item.get("name") or not item.get("due"):
            raise OnboardingError(f"deadlines.custom[{index}] requires name and due.")
        validate_date(str(item["due"]))

    calendar = data.setdefault("calendar", {})
    if not isinstance(calendar, dict):
        raise OnboardingError("calendar must be a mapping.")
    try:
        reminder = int(calendar.get("reminder_minutes", 15))
    except (TypeError, ValueError) as exc:
        raise OnboardingError("calendar.reminder_minutes must be an integer.") from exc
    if reminder < 0:
        raise OnboardingError("calendar.reminder_minutes cannot be negative.")
    calendar["reminder_minutes"] = reminder

    backup = data.setdefault("backup", {})
    if not isinstance(backup, dict):
        raise OnboardingError("backup must be a mapping.")
    backup["enabled"] = bool(backup.get("enabled", True))


def business_profile(business_type: str) -> dict[str, Any]:
    return BUSINESS_TYPE_PROFILES.get(business_type, BUSINESS_TYPE_PROFILES[DEFAULT_BUSINESS_TYPE])


def wiki_template_purpose(config: dict[str, Any]) -> str:
    company = config["company"]
    profile = business_profile(company["business_type"])
    generated = datetime.now().strftime("%Y-%m-%d")
    return f"""# {company['name']} Wiki Purpose

Generated by the Chief-of-Staff onboarding wizard on {generated}.

## Mission

This wiki is the operating memory for **{company['name']}**. Its purpose is to help the Chief-of-Staff agent {profile['purpose']}.

## What belongs here

- Durable facts that should survive individual chats, emails, and meetings.
- Entity pages for {', '.join(profile['entities'])}.
- Concept pages for {', '.join(profile['concepts'])}.
- Document notes and links for {', '.join(profile['documents'])}.
- Decisions, assumptions, open questions, and follow-up commitments.

## Operating principles

1. Prefer concise, sourced notes over long transcripts.
2. Separate facts from hypotheses and opinions.
3. Use stable page names so Drive, Calendar, Gmail, and research workflows can link back here.
4. Record dates in ISO format (YYYY-MM-DD) and use the configured timezone: {config['delivery']['timezone']}.
5. Do not store passwords, API keys, private tokens, or raw identity documents in wiki pages.

## Suggested top-level folders

- `Entities/` — companies, people, clients, vendors, authorities.
- `Projects/` — active delivery work, proposals, deals, internal initiatives.
- `Finance/` — invoice notes, expense policies, reporting commentary.
- `Research/` — market maps, entity dossiers, competitive intelligence.
- `Travel/` — trip plans and post-trip notes.
- `Decisions/` — dated decisions with rationale and follow-up.
"""


def wiki_template_schema(config: dict[str, Any]) -> str:
    company = config["company"]
    profile = business_profile(company["business_type"])
    entities = "\n".join(f"  - {item}" for item in profile["entities"])
    concepts = "\n".join(f"  - {item}" for item in profile["concepts"])
    documents = "\n".join(f"  - {item}" for item in profile["documents"])
    return f"""# Wiki Schema — {company['name']}

Use these schemas for notes created by Chief-of-Staff skills. YAML frontmatter is recommended for machine-readable fields.

## Entity page

```yaml
type: entity
entity_type: client | prospect | vendor | partner | authority | person | company
name: Example Name
aliases: []
status: active | inactive | prospect | archived
owner: ""
source_links: []
last_reviewed: {datetime.now().date().isoformat()}
```

Recommended entity classes for this business type:
{entities}

Sections:
- Summary
- Key contacts
- Relationship history
- Open loops
- Documents and Drive links
- Research notes

## Concept page

```yaml
type: concept
name: Example Concept
category: operations | finance | legal | sales | delivery | research
source_links: []
last_reviewed: {datetime.now().date().isoformat()}
```

Recommended concept areas:
{concepts}

Sections:
- Definition
- Why it matters
- Current policy/process
- Examples
- Related pages

## Project / deal page

```yaml
type: project
status: idea | active | waiting | complete | lost | archived
stage: Lead
owner: ""
client: ""
value: null
currency: {company['currency']}
next_action: ""
next_action_due: null
source_links: []
```

Use stages from `company.yaml` exactly:
{chr(10).join(f'- {stage}' for stage in config['sales_stages'])}

## Document note

```yaml
type: document
related_entity: ""
document_type: proposal | NDA | SOW | contract | invoice | receipt | filing | research | travel
status: draft | sent | signed | filed | archived
source_path: ""
drive_link: ""
date: {datetime.now().date().isoformat()}
```

Common document types for this business type:
{documents}

## Decision record

```yaml
type: decision
date: {datetime.now().date().isoformat()}
decision: ""
owner: ""
status: proposed | accepted | superseded
related_pages: []
```

Sections:
- Context
- Options considered
- Decision
- Consequences
- Review date
"""


def expand_user_path(path_text: str) -> Path:
    return Path(path_text).expanduser()


def initialize_wiki(config: dict[str, Any], force: bool = False) -> list[Path]:
    wiki_path = expand_user_path(str(config["paths"]["wiki_path"]))
    wiki_path.mkdir(parents=True, exist_ok=True)
    files = {
        wiki_path / "purpose.md": wiki_template_purpose(config),
        wiki_path / "SCHEMA.md": wiki_template_schema(config),
    }
    written: list[Path] = []
    for path, content in files.items():
        if path.exists() and not force:
            print(f"  Kept existing wiki file: {path}")
            continue
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def confirm_overwrite(path: Path, non_interactive: bool, force: bool) -> None:
    if not path.exists() or force:
        return
    if non_interactive:
        raise OnboardingError(f"Output already exists: {path}. Pass --force to overwrite.")
    if not prompt_bool(f"Output file exists ({path}). Overwrite?", default=False):
        raise OnboardingError("Onboarding cancelled without writing config.")


def save_partial() -> None:
    if not PARTIAL_DATA:
        return
    base = PARTIAL_OUTPUT or DEFAULT_OUTPUT
    partial_path = base.with_name(base.name + ".partial")
    try:
        dump_yaml(PARTIAL_DATA, partial_path)
        print(f"\nInterrupted. Saved partial onboarding progress to: {partial_path}", file=sys.stderr)
    except Exception as exc:  # pragma: no cover - best effort only.
        print(f"\nInterrupted. Failed to save partial progress: {exc}", file=sys.stderr)


def print_summary(config: dict[str, Any], output: Path, wiki_files: list[Path]) -> None:
    google = config.get("google", {})
    print("\n== Onboarding complete ==")
    print(f"Company:        {config['company']['name']} ({config['company']['jurisdiction']})")
    print(f"Business type:  {config['company']['business_type']}")
    print(f"Config written: {output}")
    print(f"Wiki path:      {config['paths']['wiki_path']}")
    if wiki_files:
        print("Wiki files:")
        for path in wiki_files:
            print(f"  - {path}")
    print(f"Delivery:       {config['delivery']['channel']} at {config['delivery']['briefing_time']} {config['delivery']['timezone']}")
    print(f"Drive root:     {google.get('drive_root_folder_id', 'auto')}")
    if google.get("auth_test") == "requested_not_run":
        print("Google auth:    test requested; run it after credentials are in place.")
    else:
        print("Google auth:    saved; test later with the google-workspace skill.")

    print("\nNext steps:")
    print("1. Review and edit the generated company.yaml if needed.")
    if google.get("drive_root_folder_id") == "auto":
        print("2. Create the Google Drive folder structure from shared/config/drive-map.yaml, then update google.drive_root_folder_id.")
    else:
        print("2. Verify the configured Google Drive root folder and filing map.")
    print("3. Test Google Workspace access using the google-workspace skill/service account.")
    print("4. Create cron jobs for daily briefing, weekly review, deadline scans, and backups.")
    print("5. Send a test briefing to the configured delivery channel.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chief-of-Staff plugin onboarding wizard")
    parser.add_argument("--non-interactive", action="store_true", help="Load answers from --config and do not prompt")
    parser.add_argument("--config", type=Path, help="Preset YAML for --non-interactive mode")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help=f"Output company.yaml path (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--force", action="store_true", help="Overwrite existing output and wiki template files")
    parser.add_argument("--skip-wiki", action="store_true", help="Do not create purpose.md and SCHEMA.md")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    global PARTIAL_OUTPUT
    output = args.output.expanduser().resolve()
    PARTIAL_OUTPUT = output

    if args.non_interactive:
        if not args.config:
            raise OnboardingError("--non-interactive requires --config preset.yaml")
        preset = load_yaml(args.config)
        config = normalize_non_interactive_config(preset)
        config.setdefault("checks", check_prerequisites())
    else:
        config = build_interactive_config()

    PARTIAL_DATA.clear()
    PARTIAL_DATA.update(config)
    validate_config(config)
    confirm_overwrite(output, non_interactive=args.non_interactive, force=args.force)
    wiki_files: list[Path] = [] if args.skip_wiki else initialize_wiki(config, force=args.force)
    dump_yaml(config, output)
    print_summary(config, output, wiki_files)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        return run(args)
    except KeyboardInterrupt:
        save_partial()
        return 130
    except OnboardingError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        save_partial()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
