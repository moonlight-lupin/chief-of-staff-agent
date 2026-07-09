#!/usr/bin/env python3
"""Chief-of-staff plugin doctor — health check.

Usage:
    python doctor.py                    # text report
    python doctor.py --json             # JSON report
    python doctor.py --fix              # attempt fixes
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import py_compile
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    raise RuntimeError("PyYAML is required for doctor.py") from exc

from config_loader import get_project_root, load_config
from state_store import EMPTY_TEMPLATES

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PLUGIN_ROOT / "shared" / "config"


def _get_registered_skills() -> list[str]:
    """Read registered skills from plugin.yaml based on active profile."""
    import os as _os
    plugin_yaml = PLUGIN_ROOT / "plugin.yaml"
    if not plugin_yaml.exists():
        return [
            "daily-briefing", "deadline-tracker", "note-taker", "todo-list",
            "calendar-manager", "drive-filer", "meeting-prep", "weekly-review",
            "document-preparer", "pipeline-manager", "bookkeeper", "deep-research",
            "entity-research", "travel-itinerary", "backup", "self-sign",
        ]
    try:
        data = _load_yaml(plugin_yaml) or {}
        # Determine profile: env var > plugin.yaml key > "default"
        profile = _os.getenv("CHIEF_OF_STAFF_SKILL_PROFILE") or data.get("skill_profile") or "default"
        profiles = data.get("skill_profiles", {})
        profile_data = profiles.get(profile, {})
        skills = profile_data.get("registered", [])
        if skills:
            return skills
        # Fallback to default profile
        default_data = profiles.get("default", {})
        return default_data.get("registered", [
            "daily-briefing", "deadline-tracker", "note-taker", "todo-list",
            "calendar-manager", "drive-filer", "meeting-prep", "weekly-review",
            "document-preparer", "pipeline-manager", "bookkeeper", "deep-research",
            "entity-research", "travel-itinerary", "backup", "self-sign",
        ])
    except Exception:
        return [
            "daily-briefing", "deadline-tracker", "note-taker", "todo-list",
            "calendar-manager", "drive-filer", "meeting-prep", "weekly-review",
            "document-preparer", "pipeline-manager", "bookkeeper", "deep-research",
            "entity-research", "travel-itinerary", "backup", "self-sign",
        ]


def get_all_skills():
    """Get the list of skills to check (evaluated at call time, not import time)."""
    return _get_registered_skills()


# ALL_SKILLS is evaluated lazily — call get_all_skills() in the check function
ALL_SKILLS = None  # Will be set on first check call
REQUIRED_CONFIG_SECTIONS = ["company", "google", "paths", "delivery"]


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    fix_applied: bool = False


def _load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _copy_example(name: str) -> bool:
    live = CONFIG_DIR / name
    example = CONFIG_DIR / f"{name}.example"
    if not live.exists() and example.exists():
        shutil.copy2(example, live)
        return True
    return False


def _config_path(arg: str | None) -> Path:
    return Path(arg).expanduser().resolve() if arg else CONFIG_DIR / "company.yaml"


def _parse_config(config_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not config_path.exists():
        return None, f"missing: {config_path}"
    try:
        data = _load_yaml(config_path) or {}
        if not isinstance(data, dict):
            return None, "company.yaml top-level value is not a mapping"
        return data, None
    except Exception as exc:
        return None, str(exc)


def _project_root_from_data(data: Mapping[str, Any] | None, config_path: Path) -> Path | None:
    if not data:
        return None
    try:
        raw = data["paths"]["project_root"]
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            path = config_path.parent / path
        return path.resolve()
    except Exception:
        return None


def _check_plugin_root(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    manifest = PLUGIN_ROOT / "plugin.yaml"
    if not PLUGIN_ROOT.exists():
        return CheckResult("plugin_root", "fail", f"Plugin root missing: {PLUGIN_ROOT}")
    if not manifest.exists():
        return CheckResult("plugin_root", "fail", "plugin.yaml missing")
    try:
        loaded = _load_yaml(manifest)
        if not isinstance(loaded, dict) or loaded.get("name") != "chief-of-staff":
            return CheckResult("plugin_root", "fail", "plugin.yaml invalid or wrong name")
        return CheckResult("plugin_root", "pass", str(PLUGIN_ROOT))
    except Exception as exc:
        return CheckResult("plugin_root", "fail", f"plugin.yaml parse failed: {exc}")


def _check_skills(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    skills = get_all_skills()
    missing = [s for s in skills if not (PLUGIN_ROOT / "skills" / s / "SKILL.md").exists()]
    return CheckResult("skills", "fail" if missing else "pass", f"missing: {missing}" if missing else f"all {len(skills)} skills present")


def _check_company_yaml(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    applied = False
    if not config_path.exists() and fix and config_path == CONFIG_DIR / "company.yaml":
        applied = _copy_example("company.yaml")
        data, _ = _parse_config(config_path)
    if data is None:
        return CheckResult("company_yaml", "fail", f"company.yaml missing or invalid at {config_path}", applied)
    loaded = load_config(str(config_path))
    if loaded is None:
        return CheckResult("company_yaml", "warn", f"{config_path} parses but strict validation reported errors", applied)
    return CheckResult("company_yaml", "pass", str(config_path), applied)


def _check_required_sections(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    missing = [s for s in REQUIRED_CONFIG_SECTIONS if not isinstance((data or {}).get(s), dict)]
    return CheckResult("config_sections", "fail" if missing else "pass", f"missing sections: {missing}" if missing else "required sections present")


def _check_project_root(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    root = _project_root_from_data(data, config_path)
    if root is None:
        return CheckResult("project_root", "fail", "paths.project_root missing or invalid")
    applied = False
    if not root.exists() and fix:
        root.mkdir(parents=True, exist_ok=True)
        applied = True
    return CheckResult("project_root", "pass" if root.exists() else "fail", str(root), applied)


def _check_yaml_stores(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    root = _project_root_from_data(data, config_path)
    if root is None:
        return CheckResult("yaml_stores", "fail", "project root unavailable")
    missing: list[str] = []
    invalid: list[str] = []
    applied = False
    for store, template in EMPTY_TEMPLATES.items():
        path = root / f"{store}.yaml"
        if not path.exists():
            missing.append(store)
            if fix:
                root.mkdir(parents=True, exist_ok=True)
                path.write_text(yaml.safe_dump(template, sort_keys=False), encoding="utf-8")
                applied = True
        if path.exists():
            try:
                loaded = _load_yaml(path) or {}
                if not isinstance(loaded, dict):
                    invalid.append(store)
            except Exception as exc:
                invalid.append(f"{store}: {exc}")
    status = "pass" if not missing and not invalid else ("warn" if applied and not invalid else "fail")
    return CheckResult("yaml_stores", status, f"missing={missing} invalid={invalid}" if (missing or invalid) else "all stores parse", applied)


def _check_google_workspace(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
    paths = [home / "skills" / "productivity" / "google-workspace" / "SKILL.md", home / "skills" / "google-workspace" / "SKILL.md"]
    ok = any(p.exists() for p in paths)
    return CheckResult("google_workspace_skill", "pass" if ok else "warn", "installed" if ok else "google-workspace skill not found")


def _check_google_auth(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    google = (data or {}).get("google") if isinstance((data or {}).get("google"), dict) else {}
    if not google or not google.get("delegate_email"):
        return CheckResult("google_auth", "warn", "skipped: google config incomplete")
    # Use WorkspaceClient for provider-neutral auth check
    try:
        sys.path.insert(0, str(PLUGIN_ROOT / "shared" / "scripts"))
        from workspace_client import get_workspace_client
        client = get_workspace_client(data or {})
        healthy = client.health_check()
        if healthy:
            return CheckResult("google_auth", "pass", "workspace provider health check succeeded")
        else:
            return CheckResult("google_auth", "warn", "workspace provider health check returned False")
    except FileNotFoundError as exc:
        return CheckResult("google_auth", "warn", f"skipped: {exc}")
    except NotImplementedError as exc:
        return CheckResult("google_auth", "warn", f"skipped: {exc}")
    except Exception as exc:
        return CheckResult("google_auth", "warn", f"workspace provider check failed: {exc}")


def _check_jurisdiction_pack(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    code = str(((data or {}).get("company") or {}).get("jurisdiction", "")).lower() if isinstance((data or {}).get("company"), dict) else ""
    if not code:
        return CheckResult("jurisdiction_pack", "fail", "company.jurisdiction missing")
    path = CONFIG_DIR / "jurisdictions" / f"{code}.yaml"
    return CheckResult("jurisdiction_pack", "pass" if path.exists() else "fail", str(path))


def _check_config_file(name: str) -> Callable[[bool, dict[str, Any] | None, Path], CheckResult]:
    def check(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
        applied = False
        path = CONFIG_DIR / name
        if not path.exists() and fix:
            applied = _copy_example(name)
        if not path.exists():
            return CheckResult(name.replace(".yaml", ""), "fail", f"missing: {path}", applied)
        try:
            _load_yaml(path)
            return CheckResult(name.replace(".yaml", ""), "pass", str(path), applied)
        except Exception as exc:
            return CheckResult(name.replace(".yaml", ""), "fail", str(exc), applied)
    return check


def _check_signature(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    self_sign = (data or {}).get("self_sign") if isinstance((data or {}).get("self_sign"), dict) else None
    if not self_sign or not self_sign.get("signature_image"):
        return CheckResult("signature_image", "warn", "skipped: self-sign not configured")
    raw = Path(str(self_sign["signature_image"])).expanduser()
    path = raw if raw.is_absolute() else PLUGIN_ROOT / raw
    return CheckResult("signature_image", "pass" if path.exists() else "warn", str(path))


def _check_wiki(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    paths = (data or {}).get("paths") if isinstance((data or {}).get("paths"), dict) else {}
    raw = paths.get("wiki_path") if paths else None
    root = Path(str(raw)).expanduser().resolve() if raw else (_project_root_from_data(data, config_path) / "wiki" if _project_root_from_data(data, config_path) else None)
    if root is None:
        return CheckResult("wiki", "fail", "wiki path unavailable")
    applied = False
    if fix:
        root.mkdir(parents=True, exist_ok=True)
        for filename, text in {"purpose.md": "# Purpose\n\nDescribe the company operating context.\n", "SCHEMA.md": "# Wiki Schema\n\nDocument naming and linking conventions.\n"}.items():
            p = root / filename
            if not p.exists():
                p.write_text(text, encoding="utf-8")
                applied = True
    missing = [f for f in ("purpose.md", "SCHEMA.md") if not (root / f).exists()]
    return CheckResult("wiki", "pass" if not missing else "fail", f"{root}; missing={missing}" if missing else str(root), applied)


def _check_docuseal(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    esign = (data or {}).get("esign") if isinstance((data or {}).get("esign"), dict) else {}
    if not esign or str(esign.get("provider", "")).lower() != "docuseal":
        return CheckResult("docuseal", "warn", "skipped: e-sign not configured for DocuSeal")
    url = str(esign.get("url") or "").rstrip("/")
    if not url or not (os.getenv("DOCUSEAL_API_KEY") or os.getenv("DOCUSEAL_MCP_TOKEN")):
        return CheckResult("docuseal", "warn", "skipped: missing DocuSeal URL or DOCUSEAL_API_KEY/DOCUSEAL_MCP_TOKEN")
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # nosec - configured URL health check
            ok = resp.status < 500
        return CheckResult("docuseal", "pass" if ok else "warn", f"HTTP {resp.status}")
    except Exception as exc:
        return CheckResult("docuseal", "warn", f"DocuSeal ping failed: {exc}")


def _check_cron(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    try:
        proc = subprocess.run(["hermes", "cron", "list", "--all"], capture_output=True, text=True, timeout=15, check=False)
    except Exception as exc:
        return CheckResult("cron_jobs", "warn", f"cannot inspect cron jobs: {exc}")
    out = proc.stdout + proc.stderr
    missing = [name for name in ("daily-briefing", "deadline-tracker") if name not in out]
    return CheckResult("cron_jobs", "pass" if not missing else "warn", f"missing references: {missing}" if missing else "daily briefing and deadline tracker configured")


def _check_compile(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    failures: list[str] = []
    for path in list((PLUGIN_ROOT / "shared" / "scripts").glob("*.py")) + list((PLUGIN_ROOT / "skills").glob("*/scripts/*.py")) + [PLUGIN_ROOT / "__init__.py", PLUGIN_ROOT / "hooks.py"]:
        if path.exists():
            try:
                py_compile.compile(str(path), doraise=True)
            except py_compile.PyCompileError as exc:
                failures.append(f"{path.relative_to(PLUGIN_ROOT)}: {exc.msg}")
    return CheckResult("python_compile", "pass" if not failures else "fail", "all scripts compile" if not failures else "; ".join(failures[:5]))


def _check_packages(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    missing = []
    for module, label in (("yaml", "PyYAML"), ("docx", "python-docx"), ("fitz", "pymupdf")):
        if importlib.util.find_spec(module) is None:
            missing.append(label)
    return CheckResult("python_packages", "pass" if not missing else "fail", "installed" if not missing else f"missing: {missing}")


def _check_audit_runs(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    root = _project_root_from_data(data, config_path)
    if root is None:
        return CheckResult("audit_runs_dirs", "fail", "project root unavailable")
    applied = False
    for d in (root / ".audit", root / ".runs"):
        if not d.exists() and fix:
            d.mkdir(parents=True, exist_ok=True)
            applied = True
    missing = [str(d) for d in (root / ".audit", root / ".runs") if not d.exists()]
    return CheckResult("audit_runs_dirs", "pass" if not missing else "warn", "present" if not missing else f"missing: {missing}", applied)


def _check_workspace_provider(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    """Check which workspace provider is configured and report capabilities."""
    integrations = (data or {}).get("integrations", {}) if isinstance((data or {}).get("integrations"), dict) else {}
    workspace = integrations.get("workspace", {}) if isinstance(integrations, dict) else {}
    provider = workspace.get("provider", "google_api")
    mode = workspace.get("mode", "direct")

    # Report capabilities
    try:
        sys.path.insert(0, str(PLUGIN_ROOT / "shared" / "scripts"))
        from workspace_capabilities import get_capabilities, unsupported_actions
        caps = get_capabilities(provider)
        supported = [k for k, v in caps.items() if v]
        unsupported = unsupported_actions(provider)
        detail = f"{provider} {mode} — supported: {', '.join(supported)}"
        if unsupported:
            detail += f"; unsupported: {', '.join(unsupported)}"
    except Exception:
        detail = f"{provider} {mode}"
    return CheckResult("workspace_provider", "pass", detail)


def _check_composio(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    """Check Composio configuration if provider is composio."""
    integrations = (data or {}).get("integrations", {}) if isinstance((data or {}).get("integrations"), dict) else {}
    workspace = integrations.get("workspace", {}) if isinstance(integrations, dict) else {}
    provider = workspace.get("provider", "google_api")

    if provider != "composio":
        return CheckResult("composio", "pass", "skipped: provider is not composio")

    details: list[str] = []
    all_pass = True

    # Check API key
    if os.getenv("COMPOSIO_API_KEY"):
        details.append("API key set")
    else:
        details.append("API key NOT set — get one at https://dashboard.composio.dev/settings")
        all_pass = False

    # Check user_id
    user_id = workspace.get("user_id")
    if user_id:
        details.append(f"user_id: {user_id}")
    else:
        details.append("user_id NOT set in config")
        all_pass = False

    # Check session metadata and refresh connection statuses
    try:
        sys.path.insert(0, str(PLUGIN_ROOT / "shared" / "scripts"))
        from providers.composio_workspace import load_session_meta, ComposioWorkspaceClient
        meta = load_session_meta(data or {})
        if not meta or not meta.get("session_id"):
            details.append("no session — run: connect_workspace.py --provider composio --connect gmail")
            all_pass = False
        else:
            details.append(f"session: {meta['session_id']}")
            # Refresh actual connection state from Composio
            try:
                client = ComposioWorkspaceClient(data or {})
                refreshed = client.refresh_connection_statuses()
                connections = refreshed
            except Exception:
                connections = {tk: meta.get("connections", {}).get(tk, {}).get("status", "unknown") for tk in ("gmail", "googlecalendar")}

            for tk in ("gmail", "googlecalendar", "googledrive"):
                status = connections.get(tk, "unknown")
                if status == "connected":
                    details.append(f"{tk}: connected")
                else:
                    details.append(f"{tk}: not connected — run: connect_workspace.py --provider composio --connect {tk}")
                    all_pass = False
    except Exception as exc:
        details.append(f"session check failed: {exc}")
        all_pass = False

    return CheckResult("composio", "pass" if all_pass else "warn", "; ".join(details))


CHECKS: list[Callable[[bool, dict[str, Any] | None, Path], CheckResult]] = [
    _check_plugin_root, _check_skills, _check_company_yaml, _check_required_sections,
    _check_project_root, _check_yaml_stores, _check_google_workspace, _check_google_auth,
    _check_jurisdiction_pack, _check_config_file("drive-map.yaml"), _check_config_file("queries.yaml"),
    _check_signature, _check_wiki, _check_docuseal, _check_cron, _check_compile,
    _check_packages, _check_audit_runs,
    _check_workspace_provider, _check_composio,
]


def run_checks(fix: bool = False, config: str | None = None) -> list[CheckResult]:
    config_path = _config_path(config)
    data, _ = _parse_config(config_path)
    return [check(fix, data, config_path) for check in CHECKS]


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chief-of-Staff plugin health check")
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    parser.add_argument("--fix", action="store_true", help="Attempt safe fixes")
    parser.add_argument("--config", help="Path to company.yaml")
    args = parser.parse_args(argv)
    report = run_checks(fix=args.fix, config=args.config)
    payload = [asdict(r) for r in report]
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for r in report:
            icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}.get(r.status, "?")
            fix_note = " (fixed)" if r.fix_applied else ""
            print(f"{icon} {r.name}: {r.status} — {r.detail}{fix_note}")
    return 1 if any(r.status == "fail" for r in report) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
