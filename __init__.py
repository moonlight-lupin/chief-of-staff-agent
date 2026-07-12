import os
from pathlib import Path

import yaml


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

    plugin_yaml = Path(__file__).resolve().parent / "plugin.yaml"
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
    plugin_yaml = Path(__file__).resolve().parent / "plugin.yaml"
    profile_name = _get_skill_profile()

    if plugin_yaml.exists():
        try:
            with open(plugin_yaml) as f:
                data = yaml.safe_load(f)
            profiles = data.get("skill_profiles", {})
            profile = profiles.get(profile_name, {})
            skills = profile.get("registered", [])
            if skills:
                return skills
        except Exception:
            pass

    # Fallback: default 18 skills (mirrors plugin.yaml skill_profiles.default)
    return [
        "daily-briefing", "deadline-tracker", "note-taker",
        "todo-list", "calendar-manager", "drive-filer",
        "meeting-prep", "weekly-review", "document-preparer",
        "pipeline-manager", "bookkeeper", "deep-research",
        "entity-research", "travel-itinerary", "backup",
        "email-organisation", "self-sign", "esign-connector",
    ]


def register(ctx):
    """Register skills based on the active profile + 7 quality hooks.

    Profiles are defined in plugin.yaml → skill_profiles.
    Set CHIEF_OF_STAFF_SKILL_PROFILE=enterprise to use the enterprise profile
    (swaps self-sign for esign-connector).
    """
    skills = _get_registered_skills()
    for skill_name in skills:
        ctx.register_skill(skill_name, f"skills/{skill_name}/SKILL.md")

    # Register all 7 quality hooks
    from . import hooks
    hooks.register_all_hooks(ctx)