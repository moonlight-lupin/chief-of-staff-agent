import os
from pathlib import Path

import yaml


PLUGIN_ROOT = Path(__file__).resolve().parent


def _get_skill_profile() -> str:
    """Determine which skill profile to use.

    Priority:
    1. CHIEF_OF_STAFF_SKILL_PROFILE env var
    2. 'skill_profile' key in plugin.yaml
    3. 'default'
    """
    profile = os.getenv("CHIEF_OF_STAFF_SKILL_PROFILE")
    if profile:
        return profile

    plugin_yaml = PLUGIN_ROOT / "plugin.yaml"
    if plugin_yaml.exists():
        try:
            with open(plugin_yaml) as f:
                data = yaml.safe_load(f)
            profile = data.get("skill_profile")
            if profile:
                return profile
        except Exception:
            pass

    return "default"


def _get_registered_skills() -> list[str]:
    """Read the skill list for the active profile from plugin.yaml."""
    plugin_yaml = PLUGIN_ROOT / "plugin.yaml"
    profile_name = _get_skill_profile()

    if plugin_yaml.exists():
        try:
            with open(plugin_yaml) as f:
                data = yaml.safe_load(f)
            profiles = data.get("skill_profiles", {})
            profile = profiles.get(profile_name, {})
            skills = profile.get("registered", [])
            if skills:
                return _filter_configured_skills(skills)
        except Exception:
            pass

    # Fallback: default skills (mirrors plugin.yaml skill_profiles.default).
    return _filter_configured_skills([
        "daily-briefing", "deadline-tracker", "note-taker",
        "todo-list", "calendar-manager", "drive-filer",
        "meeting-prep", "weekly-review", "document-preparer",
        "pipeline-manager", "bookkeeper", "deep-research",
        "entity-research", "travel-itinerary", "backup",
        "email-organisation", "self-sign", "esign-connector",
    ])


def _filter_configured_skills(skills: list[str]) -> list[str]:
    """Do not expose external e-sign workflows until DocuSeal is configured."""
    if "esign-connector" not in skills or _esign_url_configured():
        return list(skills)
    return [skill for skill in skills if skill != "esign-connector"]


def _esign_url_configured() -> bool:
    raw_path = os.getenv("CHIEF_OF_STAFF_CONFIG")
    config_path = Path(raw_path).expanduser() if raw_path else PLUGIN_ROOT / "shared" / "config" / "company.yaml"
    try:
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return False
    esign = data.get("esign") if isinstance(data, dict) else None
    return isinstance(esign, dict) and bool(str(esign.get("url") or "").strip())


def register(ctx):
    """Register skills based on the active profile + 8 quality hooks.

    Profiles are defined in plugin.yaml → skill_profiles.
    Set CHIEF_OF_STAFF_SKILL_PROFILE=enterprise to use the enterprise profile
    (also enables esign-connector when esign.url is configured).
    """
    skills = _get_registered_skills()
    for skill_name in skills:
        # Prefer skills.local/ overlay (custom assistant-name rendering) when present.
        overlay = PLUGIN_ROOT / "skills.local" / skill_name / "SKILL.md"
        shipped = PLUGIN_ROOT / "skills" / skill_name / "SKILL.md"
        skill_path = overlay if overlay.exists() else shipped
        ctx.register_skill(skill_name, skill_path)

    # Register all 8 quality hooks
    from . import hooks
    hooks.register_all_hooks(ctx)
