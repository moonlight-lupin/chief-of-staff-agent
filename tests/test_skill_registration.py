"""Every shipped skill is registered, intact, and advertised.

Guards the catalog-readiness gaps found in v0.8.0: a skill directory that no
profile registers (task-brief shipped in v0.7.12 but never loaded), a
SKILL.md with a second frontmatter block spliced into its body, and manifest
descriptions that predate the growth skills.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIRS = sorted(p.parent.name for p in (PLUGIN_ROOT / "skills").glob("*/SKILL.md"))
FRONTMATTER_KEY = re.compile(r"^(name|description|version|author|license|platforms|metadata):", re.MULTILINE)


def _profiles() -> dict:
    return yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))["skill_profiles"]


@pytest.mark.parametrize("profile", ["default", "enterprise"])
def test_every_skill_directory_is_registered(profile):
    registered = set(_profiles()[profile]["registered"])
    missing = [s for s in SKILL_DIRS if s not in registered]
    assert not missing, f"{profile} profile does not register: {missing}"


@pytest.mark.parametrize("profile", ["default", "enterprise"])
def test_profiles_register_only_existing_skills(profile):
    unknown = [s for s in _profiles()[profile]["registered"] if s not in SKILL_DIRS]
    assert not unknown, f"{profile} profile registers skills with no SKILL.md: {unknown}"


def _body_outside_code(text: str) -> str:
    body = text.split("\n---", 1)[1] if text.startswith("---") else text
    body = body.split("\n", 1)[1] if "\n" in body else ""
    return re.sub(r"```.*?```", "", body, flags=re.DOTALL)


@pytest.mark.parametrize("skill", SKILL_DIRS)
def test_skill_has_a_single_frontmatter_block(skill):
    text = (PLUGIN_ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    stray = FRONTMATTER_KEY.findall(_body_outside_code(text))
    assert not stray, f"{skill}/SKILL.md has frontmatter keys in its body: {stray}"


def _descriptions() -> dict[str, str]:
    plugin_json = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((PLUGIN_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    return {
        "plugin.yaml": yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))["description"],
        "plugin.json": plugin_json["description"],
        "marketplace.json": market["plugins"][0]["description"],
    }


@pytest.mark.parametrize("term", ["prospecting", "SEO", "Notion"])
def test_manifest_descriptions_advertise_current_features(term):
    stale = [name for name, desc in _descriptions().items() if term.lower() not in desc.lower()]
    assert not stale, f"description does not mention {term!r}: {stale}"
