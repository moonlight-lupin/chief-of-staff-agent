#!/usr/bin/env python3
"""Bootstrap chief-of-staff plugin from a fresh clone.

Usage:
    python bootstrap.py --company "Acme Pte Ltd" --jurisdiction SG --operator founder@acme.com
    python bootstrap.py --config preset.yaml  # non-interactive
"""

from __future__ import annotations

import argparse
import json
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
        preset.setdefault("esign", {})["admin_email"] = args.operator
    if args.project_root:
        preset.setdefault("paths", {})["project_root"] = args.project_root
    if args.business_type:
        preset.setdefault("company", {})["business_type"] = args.business_type
    return preset


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
    google.setdefault("service_account_path", "~/.hermes/google_service_account.json")
    google.setdefault("domain", "example.com")
    google.setdefault("delegate_email", "operator@example.com")
    paths.setdefault("project_root", "~/.hermes/projects/chief-of-staff/")
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
    config_path = _write_config(preset)
    config = _load_yaml(config_path)
    root = _project_root(config, config_path)
    stores = _init_stores(root)
    wiki = _init_wiki(config, root)
    final = run_checks(fix=True, config=str(config_path))
    return {
        "config": str(config_path),
        "project_root": str(root),
        "copied_examples": copied,
        "initialized_stores": stores,
        "initialized_wiki": wiki,
        "doctor_initial": [r.__dict__ for r in initial],
        "doctor_final": [r.__dict__ for r in final],
        "next_steps": [
            "Set up Google service account/OAuth credentials in company.yaml.",
            "Run: python shared/scripts/install_cron.py --config shared/config/company.yaml --dry-run",
            "Then run: python shared/scripts/install_cron.py --config shared/config/company.yaml --install",
            "Test briefing by loading the chief-of-staff:daily-briefing skill.",
        ],
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministically bootstrap the Chief-of-Staff plugin")
    parser.add_argument("--company", help="Company name")
    parser.add_argument("--jurisdiction", help="Jurisdiction code, e.g. SG")
    parser.add_argument("--operator", help="Operator/delegate email")
    parser.add_argument("--project-root", help="Project root directory")
    parser.add_argument("--business-type", help="Business type for wiki seed")
    parser.add_argument("--config", help="Preset YAML to merge non-interactively")
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    args = parser.parse_args(argv)
    result = bootstrap(args)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Bootstrapped Chief-of-Staff config: {result['config']}")
        print(f"Project root: {result['project_root']}")
        print("Next steps:")
        for step in result["next_steps"]:
            print(f"- {step}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
