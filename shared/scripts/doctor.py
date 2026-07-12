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
import urllib.error
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    raise RuntimeError("PyYAML is required for doctor.py") from exc

from config_loader import get_project_root, load_config, load_dotenv_file
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
            "entity-research", "travel-itinerary", "backup", "email-organisation", "self-sign",
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
            "entity-research", "travel-itinerary", "backup", "email-organisation", "self-sign",
        ])
    except Exception:
        return [
            "daily-briefing", "deadline-tracker", "note-taker", "todo-list",
            "calendar-manager", "drive-filer", "meeting-prep", "weekly-review",
            "document-preparer", "pipeline-manager", "bookkeeper", "deep-research",
            "entity-research", "travel-itinerary", "backup", "email-organisation", "self-sign",
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
    # Check if active provider is composio — skip Google auth check if so
    integrations = (data or {}).get("integrations", {}) if isinstance((data or {}).get("integrations"), dict) else {}
    workspace = integrations.get("workspace", {}) if isinstance(integrations, dict) else {}
    provider = workspace.get("provider", "google_api")
    if provider != "google_api":
        return CheckResult("google_auth", "warn", f"skipped — active provider is {provider}")

    google = (data or {}).get("google") if isinstance((data or {}).get("google"), dict) else {}
    if not google or not google.get("delegate_email"):
        return CheckResult("google_auth", "warn", "skipped: google config incomplete")

    # Google provider: run detailed service-account checks
    details: list[str] = []
    all_pass = True

    # Check google_api.py script exists
    try:
        sys.path.insert(0, str(PLUGIN_ROOT / "shared" / "scripts"))
        from providers.google_workspace import _find_google_api_script
        script = _find_google_api_script()
        details.append(f"google_api_script: found")
    except FileNotFoundError:
        details.append("google_api_script: NOT found")
        all_pass = False
        script = None

    # Check service account file
    sa_path = str(google.get("service_account_path", ""))
    if sa_path:
        sa = Path(sa_path).expanduser()
        if sa.exists():
            details.append(f"google_service_account_file: found")
        else:
            details.append(f"google_service_account_file: NOT found at {sa}")
            all_pass = False
    else:
        details.append("google_service_account_file: not configured")
        all_pass = False

    # Check account_alias
    account_alias = str(google.get("account_alias", ""))
    if account_alias:
        details.append(f"google_account_alias: {account_alias}")
    else:
        details.append("google_account_alias: NOT set")
        all_pass = False

    # Check delegate_email
    delegate = str(google.get("delegate_email", ""))
    if delegate:
        details.append(f"google_delegate_email: {delegate}")
    else:
        details.append("google_delegate_email: NOT set")
        all_pass = False

    # Health check: calendar list with delegation flags
    if all_pass:
        try:
            from workspace_client import get_workspace_client
            client = get_workspace_client(data or {})
            healthy = client.health_check()
            if healthy:
                details.append(f"google_auth: calendar list succeeded through --account {account_alias} --as {delegate}")
            else:
                details.append("google_auth: health check returned False")
                all_pass = False
        except Exception as exc:
            details.append(f"google_auth: failed — {exc}")
            all_pass = False

    return CheckResult("google_auth", "pass" if all_pass else "warn", "; ".join(details))


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
    api_key = os.getenv("DOCUSEAL_API_KEY", "")
    mcp_token = os.getenv("DOCUSEAL_MCP_TOKEN", "")
    auth_mode = str(esign.get("auth_mode", "auto")).lower()
    if not url or not (api_key or mcp_token):
        return CheckResult("docuseal", "warn", "skipped: missing DocuSeal URL or DOCUSEAL_API_KEY/DOCUSEAL_MCP_TOKEN")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "chief-of-staff-doctor/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec - configured URL health check
            ok = resp.status < 500
        detail = f"HTTP {resp.status}"
        # Check credential completeness based on auth_mode.
        missing_creds = []
        if auth_mode in ("auto", "mcp_and_api"):
            if not mcp_token:
                missing_creds.append("DOCUSEAL_MCP_TOKEN (needed for template creation)")
            if not api_key:
                missing_creds.append("DOCUSEAL_API_KEY (needed for field placement + submissions)")
        elif auth_mode == "pro_api_only":
            if not api_key:
                missing_creds.append("DOCUSEAL_API_KEY (required for pro_api_only mode)")
        if missing_creds:
            detail += ", missing: " + "; ".join(missing_creds)
            return CheckResult("docuseal", "fail", detail)
        # If API key is available, verify it works against REST /api/templates.
        if api_key:
            try:
                api_req = urllib.request.Request(
                    f"{url}/api/templates?limit=1",
                    headers={"X-Auth-Token": api_key, "User-Agent": "chief-of-staff-doctor/1.0"},
                )
                with urllib.request.urlopen(api_req, timeout=10) as api_resp:  # nosec - configured URL health check
                    detail += f", API key OK"
            except urllib.error.HTTPError as api_exc:
                if api_exc.code in (401, 403):
                    return CheckResult("docuseal", "fail", f"HTTP {resp.status}, API key invalid (HTTP {api_exc.code})")
                detail += f", API key check failed (HTTP {api_exc.code})"
        else:
            detail += ", no API key (PATCH fields unavailable)"
        return CheckResult("docuseal", "pass" if ok else "warn", detail)
    except urllib.error.HTTPError as exc:
        return CheckResult("docuseal", "warn", f"DocuSeal HTTP {exc.code}: {exc.reason}")
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

    # Report capabilities — mode-aware for composio
    try:
        sys.path.insert(0, str(PLUGIN_ROOT / "shared" / "scripts"))
        from workspace_capabilities import get_capabilities, unsupported_actions
        if provider == "composio":
            cap_provider = f"composio:{mode}"
        else:
            cap_provider = provider
        caps = get_capabilities(cap_provider)
        # Fallback to bare provider if mode-specific caps not found
        if not caps:
            caps = get_capabilities(provider)
        supported = [k for k, v in caps.items() if v]
        unsupported = unsupported_actions(cap_provider) or unsupported_actions(provider)
        detail = f"{provider} {mode} — supported: {', '.join(supported)}"
        if unsupported:
            detail += f"; unsupported: {', '.join(unsupported)}"
    except Exception:
        detail = f"{provider} {mode}"
    return CheckResult("workspace_provider", "pass", detail)


def _check_composio(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    """Check Composio configuration — MCP mode only."""
    integrations = (data or {}).get("integrations", {}) if isinstance((data or {}).get("integrations"), dict) else {}
    workspace = integrations.get("workspace", {}) if isinstance(integrations, dict) else {}
    provider = workspace.get("provider", "google_api")

    if provider != "composio":
        return CheckResult("composio", "pass", "skipped: provider is not composio")

    mode = workspace.get("mode", "mcp")

    if mode == "sdk":
        return CheckResult("composio", "fail",
                           "SDK mode removed in v0.1.9 — change to mode: mcp and set COMPOSIO_MCP_KEY")

    details: list[str] = [f"mode: {mode}"]
    all_pass = True

    # MCP mode: check COMPOSIO_MCP_KEY
    mcp_cfg = workspace.get("mcp", {})
    key_env = mcp_cfg.get("key_env", "COMPOSIO_MCP_KEY")
    if os.getenv(key_env):
        details.append(f"{key_env}: set")
    else:
        details.append(f"{key_env}: NOT set")
        all_pass = False

    endpoint = mcp_cfg.get("endpoint", "https://connect.composio.dev/mcp")
    details.append(f"endpoint: {endpoint}")

    # Check MCP initialize
    try:
        sys.path.insert(0, str(PLUGIN_ROOT / "shared" / "scripts"))
        from mcp_client import MCPClient
        client = MCPClient(endpoint=endpoint, key_env=key_env)
        client.initialize()
        details.append("mcp_initialize: pass")
        meta_tools = [t.get("name", "?") for t in client.list_tools()]
        details.append(f"meta_tools: {', '.join(meta_tools)}")
    except Exception as exc:
        details.append(f"mcp_initialize: failed — {exc}")
        all_pass = False

    # Check connections via metadata
    try:
        from providers.composio_mcp_workspace import load_session_meta
        meta = load_session_meta(data or {})
        if meta and meta.get("connections"):
            for tk in ("gmail", "googlecalendar", "googledrive"):
                status = meta.get("connections", {}).get(tk, {}).get("status", "unknown")
                if status == "connected":
                    details.append(f"{tk}: connected")
                else:
                    details.append(f"{tk}: {status}")
        else:
            details.append("no session metadata — run: connect_workspace.py --provider composio --connect gmail")
    except Exception as exc:
        details.append(f"connection check failed: {exc}")

    return CheckResult("composio", "pass" if all_pass else "warn", "; ".join(details))


def _check_m365(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    """Check Microsoft 365 (Graph) configuration when provider is m365."""
    integrations = (data or {}).get("integrations", {}) if isinstance((data or {}).get("integrations"), dict) else {}
    workspace = integrations.get("workspace", {}) if isinstance(integrations, dict) else {}
    provider = workspace.get("provider", "google_api")

    if provider != "m365":
        return CheckResult("m365", "pass", "skipped: provider is not m365")

    m365 = (data or {}).get("m365", {}) if isinstance((data or {}).get("m365"), dict) else {}
    auth_mode = str(m365.get("auth", "client_credentials") or "client_credentials")
    details: list[str] = [f"auth: {auth_mode}"]
    all_pass = True

    # Required config fields
    for field in ("tenant_id", "client_id"):
        if m365.get(field):
            details.append(f"{field}: set")
        else:
            details.append(f"{field}: NOT set")
            all_pass = False

    if auth_mode == "client_credentials":
        if m365.get("user_principal"):
            details.append(f"user_principal: {m365.get('user_principal')}")
        else:
            details.append("user_principal: NOT set (required for client_credentials)")
            all_pass = False
        secret_env = str(m365.get("client_secret_env", "M365_CLIENT_SECRET") or "M365_CLIENT_SECRET")
        if os.getenv(secret_env):
            details.append(f"{secret_env}: set")
        else:
            details.append(f"{secret_env}: NOT set")
            all_pass = False
    else:
        details.append("device_code: interactive sign-in (no client secret needed)")

    # msal importable?
    if importlib.util.find_spec("msal") is not None:
        details.append("msal: importable")
    else:
        details.append("msal: NOT installed (pip install msal)")
        all_pass = False

    # Optional live token + health check — must not blow up offline.
    if all_pass:
        try:
            sys.path.insert(0, str(PLUGIN_ROOT / "shared" / "scripts"))
            from workspace_client import get_workspace_client
            client = get_workspace_client(data or {})
            healthy = client.health_check()
            details.append(f"health_check: {'pass' if healthy else 'failed'}")
            if not healthy:
                all_pass = False
        except Exception as exc:
            details.append(f"health_check: skipped — {exc}")

    return CheckResult("m365", "pass" if all_pass else "warn", "; ".join(details))


def _check_webhook_config(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    """Check webhook security configuration."""
    try:
        from webhook_security import validate_secret_config
        result = validate_secret_config()
        endpoints = result.get("endpoints", {})
        issues = result.get("issues", [])
        enabled = [ep for ep, st in endpoints.items() if st != "disabled"]
        if not enabled:
            return CheckResult("webhook_config", "warn",
                "No webhook endpoints enabled. Set CHIEF_OF_STAFF_WEBHOOK_SECRET and/or "
                "CHIEF_OF_STAFF_PUBSUB_AUDIENCE + CHIEF_OF_STAFF_PUBSUB_SERVICE_ACCOUNT + "
                "CHIEF_OF_STAFF_WEBHOOK_CHANNEL_TOKEN. " + "; ".join(issues))
        details = [f"{ep}: {st}" for ep, st in endpoints.items()]
        if issues:
            details.extend(issues)
        return CheckResult("webhook_config", "pass" if not issues else "warn",
            "; ".join(details))
    except Exception as exc:
        return CheckResult("webhook_config", "warn", f"check failed: {exc}")


def _check_state_files(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    """Check that state files exist and are valid JSON."""
    root = _project_root_from_data(data, config_path)
    if not root:
        return CheckResult("state_files", "warn", "project root unknown")
    state_files = [
        ".events.json", ".pending_actions.json",
        ".email_organisation_policy.json",
        ".email_organisation_classifications.json",
        ".email_organisation_suggestions.json",
        ".webhook_replay_cache.json",
    ]
    state_dirs = [".audit", ".runs"]
    details = []
    all_pass = True
    for name in state_files:
        p = root / name
        if not p.exists():
            continue  # optional file
        try:
            json.loads(p.read_text())
            details.append(f"{name}: ok")
        except json.JSONDecodeError as exc:
            details.append(f"{name}: MALFORMED ({exc})")
            all_pass = False
    for name in state_dirs:
        p = root / name
        if p.exists():
            details.append(f"{name}/: ok")
        else:
            if fix:
                p.mkdir(parents=True, exist_ok=True)
                details.append(f"{name}/: created")
            else:
                details.append(f"{name}/: missing")
                all_pass = False
    return CheckResult("state_files", "pass" if all_pass else "warn",
        "; ".join(details) if details else "no state files yet")


ORPHANED_EXECUTING_MINUTES = 15  # Only reset executing actions older than this


def _check_orphaned_executing(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    """Check for orphaned 'executing' actions stuck in .pending_actions.json.

    Only resets actions older than ORPHANED_EXECUTING_MINUTES to avoid
    resetting a genuinely executing action (duplicate-execution risk).
    Actions with missing/invalid executing_at are reported but NOT auto-reset.
    """
    root = _project_root_from_data(data, config_path)
    if not root:
        return CheckResult("orphaned_executing", "warn", "project root unknown")
    pa_path = root / ".pending_actions.json"
    if not pa_path.exists():
        return CheckResult("orphaned_executing", "pass", "no pending actions file")
    try:
        pa_data = json.loads(pa_path.read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        return CheckResult("orphaned_executing", "pass", "pending actions unreadable (checked elsewhere)")
    # Handle both dict format ({"actions": {}}) and list format
    if isinstance(pa_data, dict) and "actions" in pa_data:
        actions_dict = pa_data["actions"]
        executing = [(aid, a) for aid, a in actions_dict.items()
                     if isinstance(a, dict) and a.get("state") == "executing"]
    elif isinstance(pa_data, list):
        executing = [(a.get("id", "?"), a) for a in pa_data
                     if isinstance(a, dict) and a.get("state") == "executing"]
    else:
        return CheckResult("orphaned_executing", "pass", "pending actions empty or new")
    if not executing:
        return CheckResult("orphaned_executing", "pass", "no orphaned executing actions")

    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(minutes=ORPHANED_EXECUTING_MINUTES)

    stale = []      # older than threshold → safe to reset
    fresh = []       # younger than threshold → still running, skip
    no_ts = []       # missing/invalid executing_at → report but don't reset

    for aid, a in executing:
        ts_str = a.get("executing_at")
        if not ts_str:
            no_ts.append((aid, a))
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            no_ts.append((aid, a))
            continue
        if ts < threshold:
            stale.append((aid, a))
        else:
            fresh.append((aid, a))

    stale_ids = [aid for aid, _ in stale]
    fresh_ids = [aid for aid, _ in fresh]
    no_ts_ids = [aid for aid, _ in no_ts]

    parts = []
    if stale_ids:
        parts.append(f"{len(stale_ids)} stale (>{ORPHANED_EXECUTING_MINUTES}min): {', '.join(stale_ids)}")
    if fresh_ids:
        parts.append(f"{len(fresh_ids)} fresh (still running): {', '.join(fresh_ids)}")
    if no_ts_ids:
        parts.append(f"{len(no_ts_ids)} missing executing_at: {', '.join(no_ts_ids)}")

    if fix and stale_ids:
        ts = now.isoformat()
        for _, a in stale:
            a["state"] = "approved"
            a["last_error"] = f"Reset from orphaned 'executing' by doctor --fix at {ts} (was stale >{ORPHANED_EXECUTING_MINUTES}min)"
        pa_path.write_text(json.dumps(pa_data, indent=2))
        detail = f"Reset {len(stale_ids)} stale action(s) to 'approved': {', '.join(stale_ids)}"
        if fresh_ids:
            detail += f"; {len(fresh_ids)} fresh skipped"
        if no_ts_ids:
            detail += f"; {len(no_ts_ids)} missing executing_at (not reset)"
        return CheckResult("orphaned_executing", "pass" if not fresh_ids else "warn",
            detail, fix_applied=True)
    elif fix and not stale_ids:
        # Nothing stale to reset
        detail = "; ".join(parts) if parts else "no executing actions"
        return CheckResult("orphaned_executing", "warn" if fresh_ids or no_ts_ids else "pass", detail)

    return CheckResult("orphaned_executing", "warn",
        "; ".join(parts) + (f" — run with --fix to reset stale (>{ORPHANED_EXECUTING_MINUTES}min) actions" if stale_ids else ""))


def _check_capability_report(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    """Report provider capabilities for the configured workspace provider."""
    if not data:
        return CheckResult("capabilities", "warn", "config not loaded")
    integrations = data.get("integrations", {})
    workspace = integrations.get("workspace", {}) if isinstance(integrations, dict) else {}
    provider = str(workspace.get("provider", "google_api") or "google_api")
    try:
        from workspace_capabilities import get_capabilities, unsupported_actions, all_actions
        caps = get_capabilities(provider)
        unsupported = unsupported_actions(provider)
        total = len(all_actions())
        supported = total - len(unsupported)
        details = [f"provider: {provider}", f"capabilities: {supported}/{total} supported"]
        if unsupported:
            details.append(f"unsupported: {', '.join(unsupported[:5])}{'…' if len(unsupported) > 5 else ''}")
        return CheckResult("capabilities", "pass", "; ".join(details))
    except Exception as exc:
        return CheckResult("capabilities", "warn", f"check failed: {exc}")


def _check_smoke_test(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    """Check if a smoke-test checklist exists and is recent."""
    checklist = PLUGIN_ROOT / "docs" / "SMOKE_TEST_CHECKLIST.md"
    if checklist.exists():
        return CheckResult("smoke_test", "pass", f"checklist at {checklist.relative_to(PLUGIN_ROOT)}")
    return CheckResult("smoke_test", "warn", "no smoke-test checklist found")


CHECKS: list[Callable[[bool, dict[str, Any] | None, Path], CheckResult]] = [
    _check_plugin_root, _check_skills, _check_company_yaml, _check_required_sections,
    _check_project_root, _check_yaml_stores, _check_google_workspace, _check_google_auth,
    _check_jurisdiction_pack, _check_config_file("drive-map.yaml"), _check_config_file("queries.yaml"),
    _check_signature, _check_wiki, _check_docuseal, _check_cron, _check_compile,
    _check_packages, _check_audit_runs,
    _check_workspace_provider, _check_composio, _check_m365,
    _check_webhook_config, _check_state_files, _check_orphaned_executing,
    _check_capability_report, _check_smoke_test,
]


def run_checks(fix: bool = False, config: str | None = None) -> list[CheckResult]:
    # Auto-load plugin-root .env so the env-secret checks (composio/m365) see
    # secrets documented for .env. Shell env always wins; values never logged.
    load_dotenv_file()
    config_path = _config_path(config)
    data, _ = _parse_config(config_path)
    return [check(fix, data, config_path) for check in CHECKS]


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chief-of-Staff plugin health check")
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    parser.add_argument("--summary", action="store_true", help="Print one-line summary")
    parser.add_argument("--fix", action="store_true", help="Attempt safe fixes")
    parser.add_argument("--config", help="Path to company.yaml")
    args = parser.parse_args(argv)
    report = run_checks(fix=args.fix, config=args.config)
    payload = [asdict(r) for r in report]
    if args.json:
        print(json.dumps(payload, indent=2))
    elif args.summary:
        passed = sum(1 for r in report if r.status == "pass")
        warned = sum(1 for r in report if r.status == "warn")
        failed = sum(1 for r in report if r.status == "fail")
        fixed = sum(1 for r in report if r.fix_applied)
        overall = "READY" if failed == 0 and warned == 0 else ("READY WITH WARNINGS" if failed == 0 else "NOT READY")
        print(f"Chief-of-Staff: {overall}")
        print(f"  {passed} passed, {warned} warnings, {failed} failures" + (f", {fixed} fixed" if fixed else ""))
        if failed:
            fails = [r.name for r in report if r.status == "fail"]
            print(f"  Failures: {', '.join(fails)}")
        if warned:
            warns = [r.name for r in report if r.status == "warn"]
            print(f"  Warnings: {', '.join(warns)}")
    else:
        for r in report:
            icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}.get(r.status, "?")
            fix_note = " (fixed)" if r.fix_applied else ""
            print(f"{icon} {r.name}: {r.status} — {r.detail}{fix_note}")
    return 1 if any(r.status == "fail" for r in report) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
