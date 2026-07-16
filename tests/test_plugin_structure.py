#!/usr/bin/env python3
"""Tests for plugin structure — all 18 skills present, frontmatter valid."""

from pathlib import Path
import yaml

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# Must match plugin.yaml skill_profiles.default.registered (and enterprise).
ALL_SKILLS = [
    "daily-briefing", "deadline-tracker", "note-taker",
    "todo-list", "calendar-manager", "drive-filer",
    "meeting-prep", "weekly-review", "document-preparer",
    "pipeline-manager", "bookkeeper", "deep-research",
    "entity-research", "travel-itinerary", "backup",
    "email-organisation", "self-sign", "esign-connector",
]


class TestPluginStructure:
    def test_plugin_yaml_exists(self):
        assert (PLUGIN_ROOT / "plugin.yaml").exists(), "plugin.yaml missing"

    def test_init_py_exists(self):
        assert (PLUGIN_ROOT / "__init__.py").exists(), "__init__.py missing"

    def test_all_skill_directories_exist(self):
        for name in ALL_SKILLS:
            d = PLUGIN_ROOT / "skills" / name
            assert d.exists(), f"skills/{name}/ directory missing"
            assert (d / "SKILL.md").exists(), f"skills/{name}/SKILL.md missing"

    def test_plugin_yaml_valid(self):
        with open(PLUGIN_ROOT / "plugin.yaml") as f:
            data = yaml.safe_load(f)
        assert data["name"] == "chief-of-staff"
        assert data["version"] == "0.3.14"
        assert data["license"] == "Apache-2.0"
        assert data["requires_skills"] == []
        assert "google-workspace" in data.get("optional_skills", [])

    def test_plugin_yaml_registers_all_skills(self):
        """Source of truth is skill_profiles — not a hardcoded fallback string in __init__."""
        with open(PLUGIN_ROOT / "plugin.yaml") as f:
            data = yaml.safe_load(f)
        profiles = data.get("skill_profiles") or {}
        for profile_name in ("default", "enterprise"):
            registered = profiles.get(profile_name, {}).get("registered") or []
            missing = [s for s in ALL_SKILLS if s not in registered]
            assert not missing, (
                f"plugin.yaml skill_profiles.{profile_name}.registered missing: {missing}"
            )
            extra = [s for s in registered if s not in ALL_SKILLS]
            assert not extra, (
                f"plugin.yaml skill_profiles.{profile_name}.registered has unknown skills: {extra}"
            )
            assert len(registered) == 18

    def test_release_versions_agree(self):
        """plugin.yaml, pyproject.toml, and the chief_of_staff entrypoint must
        carry the same version so a release bump can't be applied partially."""
        import tomllib

        with open(PLUGIN_ROOT / "plugin.yaml") as f:
            plugin_version = yaml.safe_load(f)["version"]
        with open(PLUGIN_ROOT / "pyproject.toml", "rb") as f:
            pyproject_version = tomllib.load(f)["project"]["version"]

        import sys
        sys.path.insert(0, str(PLUGIN_ROOT / "shared" / "scripts"))
        try:
            import chief_of_staff
            entrypoint_version = chief_of_staff.VERSION
        finally:
            sys.path.pop(0)

        assert plugin_version == pyproject_version == entrypoint_version, (
            f"version drift: plugin.yaml={plugin_version} "
            f"pyproject.toml={pyproject_version} chief_of_staff.py={entrypoint_version}"
        )

        # The README status line is cosmetic but visible — keep it in lockstep.
        readme = (PLUGIN_ROOT / "README.md").read_text()
        assert f"v{plugin_version} internal beta" in readme, (
            f"README.md status line does not mention v{plugin_version}"
        )

    def test_pyproject_covers_requirements(self):
        """Every package pinned in requirements.txt must appear in pyproject dependencies."""
        import tomllib
        import re

        reqs = {
            re.split(r"[><=~\[]", line.strip())[0].lower()
            for line in (PLUGIN_ROOT / "requirements.txt").read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        with open(PLUGIN_ROOT / "pyproject.toml", "rb") as f:
            deps = {
                re.split(r"[><=~\[]", d)[0].lower()
                for d in tomllib.load(f)["project"]["dependencies"]
            }
        missing = reqs - deps
        assert not missing, f"requirements.txt packages missing from pyproject.toml: {missing}"


class TestSkillFrontmatter:
    @pytest.mark.parametrize("skill_name", ALL_SKILLS)
    def test_has_valid_frontmatter(self, skill_name):
        path = PLUGIN_ROOT / "skills" / skill_name / "SKILL.md"
        content = path.read_text()
        assert content.startswith("---"), f"{skill_name}: no frontmatter"

        # Extract frontmatter
        parts = content.split("---", 2)
        assert len(parts) >= 3, f"{skill_name}: malformed frontmatter"
        fm = yaml.safe_load(parts[1])
        assert fm is not None, f"{skill_name}: empty frontmatter"
        assert "name" in fm, f"{skill_name}: missing 'name' in frontmatter"
        assert "description" in fm, f"{skill_name}: missing 'description' in frontmatter"

    @pytest.mark.parametrize("skill_name", ALL_SKILLS)
    def test_version_is_set(self, skill_name):
        path = PLUGIN_ROOT / "skills" / skill_name / "SKILL.md"
        content = path.read_text()
        parts = content.split("---", 2)
        assert len(parts) >= 3, f"{skill_name}: malformed frontmatter"
        fm = yaml.safe_load(parts[1])
        assert "version" in fm, f"{skill_name}: missing 'version' in frontmatter"
        assert str(fm["version"]).strip(), f"{skill_name}: empty version"
        # Accept dotted version strings (skills evolve independently of plugin).
        assert isinstance(fm["version"], (str, int, float))

    @pytest.mark.parametrize("skill_name", ALL_SKILLS)
    def test_license_is_apache(self, skill_name):
        path = PLUGIN_ROOT / "skills" / skill_name / "SKILL.md"
        content = path.read_text()
        parts = content.split("---", 2)
        assert len(parts) >= 3, f"{skill_name}: malformed frontmatter"
        fm = yaml.safe_load(parts[1])
        assert "license" in fm, f"{skill_name}: missing 'license' in frontmatter"
        assert str(fm["license"]) == "Apache-2.0", (
            f"{skill_name}: license is {fm['license']}, expected Apache-2.0"
        )

    def test_shipped_skills_have_no_unreplaced_placeholders(self):
        """Default installs must not expose literal {assistant_name}/{company_name}."""
        import re

        pat = re.compile(r"\{(?:assistant_name|company_name)\}")
        offenders = []
        for skill_md in sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md")):
            matches = pat.findall(skill_md.read_text(encoding="utf-8"))
            if matches:
                offenders.append(f"{skill_md.parent.name}: {sorted(set(matches))}")
        assert not offenders, (
            "unreplaced placeholders in shipped SKILL.md files:\n  "
            + "\n  ".join(offenders)
        )


class TestConfigExamples:
    def test_company_yaml_example_exists(self):
        assert (PLUGIN_ROOT / "shared" / "config" / "company.yaml.example").exists()

    def test_company_yaml_example_valid(self):
        path = PLUGIN_ROOT / "shared" / "config" / "company.yaml.example"
        with open(path) as f:
            data = yaml.safe_load(f)
        assert "company" in data
        assert "google" in data
        assert "paths" in data
        assert "delivery" in data

    def test_drive_map_example_valid(self):
        path = PLUGIN_ROOT / "shared" / "config" / "drive-map.yaml.example"
        if path.exists():
            with open(path) as f:
                data = yaml.safe_load(f)
            assert "filing_rules" in data

    def test_queries_example_valid(self):
        path = PLUGIN_ROOT / "shared" / "config" / "queries.yaml.example"
        if path.exists():
            with open(path) as f:
                data = yaml.safe_load(f)
            assert data is not None

    def test_all_jurisdiction_packs_valid(self):
        for code in ["sg", "hk", "us", "uk"]:
            path = PLUGIN_ROOT / "shared" / "config" / "jurisdictions" / f"{code}.yaml"
            with open(path) as f:
                data = yaml.safe_load(f)
            assert data["jurisdiction"] in ("SG", "HK", "US", "UK")
            assert "statutory" in data
            assert isinstance(data["statutory"], list)
            assert len(data["statutory"]) >= 3


class TestScriptsCompile:
    @pytest.mark.parametrize("script_rel", [
        "shared/scripts/config_loader.py",
        "shared/scripts/date_utils.py",
        "skills/bookkeeper/scripts/pl_report.py",
        "skills/self-sign/scripts/sign_detector.py",
        "skills/document-preparer/scripts/doc_utils.py",
        "skills/backup/scripts/backup.py",
    ])
    def test_script_compiles(self, script_rel):
        import py_compile
        path = PLUGIN_ROOT / script_rel
        if path.exists():
            py_compile.compile(str(path), doraise=True)
        else:
            pytest.skip(f"{script_rel} not yet built")