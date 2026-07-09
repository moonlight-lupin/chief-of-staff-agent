#!/usr/bin/env python3
"""Tests for plugin structure — all 16 skills present, frontmatter valid."""

from pathlib import Path
import yaml

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

ALL_SKILLS = [
    "daily-briefing", "deadline-tracker", "note-taker",
    "todo-list", "calendar-manager", "drive-filer",
    "meeting-prep", "weekly-review", "document-preparer",
    "pipeline-manager", "bookkeeper", "deep-research",
    "entity-research", "travel-itinerary", "backup", "self-sign",
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
        assert data["version"] == "0.1.23"
        assert data["license"] == "MIT"
        assert data["requires_skills"] == []
        assert "google-workspace" in data.get("optional_skills", [])

    def test_init_registers_all_skills(self):
        content = (PLUGIN_ROOT / "__init__.py").read_text()
        for name in ALL_SKILLS:
            assert name in content, f"Skill '{name}' not registered in __init__.py"


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
        fm = yaml.safe_load(parts[1])
        if "version" in fm:
            assert fm["version"] == "0.1.0", f"{skill_name}: version is {fm['version']}, expected 0.1.0"

    @pytest.mark.parametrize("skill_name", ALL_SKILLS)
    def test_license_is_mit(self, skill_name):
        path = PLUGIN_ROOT / "skills" / skill_name / "SKILL.md"
        content = path.read_text()
        parts = content.split("---", 2)
        fm = yaml.safe_load(parts[1])
        if "license" in fm:
            assert str(fm["license"]).upper() == "MIT", f"{skill_name}: license is {fm['license']}, expected MIT"


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