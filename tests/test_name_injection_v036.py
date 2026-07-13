"""Tests for assistant-name skill-description injection + Gmail attachment client.

Covers the review findings on the name-routing commits:
- injection must be idempotent and rename-safe (description_template scheme)
- the test suite must never mutate the real skills/ tree (conftest guard)
- GmailClient attachment methods build the right CLI args, get_attachment
  remains a back-compat shim
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "shared" / "scripts"))

import bootstrap  # noqa: E402


def _write_config(tmp_path: Path, assistant_name: str, company: str = "Test Co") -> Path:
    cfg = tmp_path / "company.yaml"
    cfg.write_text(yaml.safe_dump({
        "assistant": {"name": assistant_name},
        "company": {"name": company},
    }), encoding="utf-8")
    return cfg


def _skills_copy(tmp_path: Path) -> Path:
    """Copy the real routing SKILL.md files into a sandbox skills dir."""
    dest = tmp_path / "skills-copy"
    for slug in bootstrap.ROUTING_SKILLS:
        src = PLUGIN_ROOT / "skills" / slug / "SKILL.md"
        target = dest / slug
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target / "SKILL.md")
    return dest


def _description(skills_dir: Path, slug: str) -> str:
    for line in (skills_dir / slug / "SKILL.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("description:"):
            return line
    return ""


class TestInjectionIdempotency:
    def test_first_injection_renders_and_keeps_template(self, tmp_path):
        skills = _skills_copy(tmp_path)
        cfg = _write_config(tmp_path, "Ada", "Acme Pte Ltd")
        messages = bootstrap._inject_assistant_name_into_skills(cfg, skills_dir=skills)
        assert messages, "expected at least one rendered skill"
        desc = _description(skills, "daily-briefing")
        assert "'Ada'" in desc
        assert "{assistant_name}" not in desc
        text = (skills / "daily-briefing" / "SKILL.md").read_text(encoding="utf-8")
        assert "description_template:" in text
        assert "{assistant_name}" in text  # preserved in the template line

    def test_rerun_same_name_is_noop(self, tmp_path):
        skills = _skills_copy(tmp_path)
        cfg = _write_config(tmp_path, "Ada")
        bootstrap._inject_assistant_name_into_skills(cfg, skills_dir=skills)
        before = (skills / "daily-briefing" / "SKILL.md").read_text(encoding="utf-8")
        messages = bootstrap._inject_assistant_name_into_skills(cfg, skills_dir=skills)
        after = (skills / "daily-briefing" / "SKILL.md").read_text(encoding="utf-8")
        assert before == after
        assert messages == []  # nothing changed, nothing reported

    def test_rename_rerenders_from_template(self, tmp_path):
        """The original bug: after first injection the placeholder was gone,
        so renaming the assistant silently no-opped."""
        skills = _skills_copy(tmp_path)
        bootstrap._inject_assistant_name_into_skills(_write_config(tmp_path, "Ada"), skills_dir=skills)
        cfg2 = _write_config(tmp_path, "Jarvis")
        messages = bootstrap._inject_assistant_name_into_skills(cfg2, skills_dir=skills)
        assert messages, "rename must re-render"
        desc = _description(skills, "daily-briefing")
        assert "'Jarvis'" in desc
        assert "Ada" not in desc

    def test_default_name_never_touches_files(self, tmp_path):
        skills = _skills_copy(tmp_path)
        before = {s: (skills / s / "SKILL.md").read_text() for s in bootstrap.ROUTING_SKILLS}
        cfg = _write_config(tmp_path, "Chief of Staff")
        assert bootstrap._inject_assistant_name_into_skills(cfg, skills_dir=skills) == []
        for s in bootstrap.ROUTING_SKILLS:
            assert (skills / s / "SKILL.md").read_text() == before[s]

    def test_missing_skill_dir_skipped(self, tmp_path):
        empty = tmp_path / "empty-skills"
        empty.mkdir()
        cfg = _write_config(tmp_path, "Ada")
        assert bootstrap._inject_assistant_name_into_skills(cfg, skills_dir=empty) == []

    def test_frontmatter_stays_valid_yaml(self, tmp_path):
        skills = _skills_copy(tmp_path)
        bootstrap._inject_assistant_name_into_skills(_write_config(tmp_path, "Ada"), skills_dir=skills)
        content = (skills / "daily-briefing" / "SKILL.md").read_text(encoding="utf-8")
        fm = yaml.safe_load(content.split("---", 2)[1])
        assert fm["name"] == "daily-briefing"
        assert "'Ada'" in fm["description"]
        assert "{assistant_name}" in fm["description_template"]


class TestSuiteNeverMutatesRealSkills:
    def test_conftest_guard_points_bootstrap_at_sandbox(self):
        """The autouse conftest fixture must redirect SKILLS_DIR for every test."""
        assert "skills-sandbox" in str(bootstrap.SKILLS_DIR)
        assert not str(bootstrap.SKILLS_DIR).startswith(str(PLUGIN_ROOT))

    def test_end_to_end_default_dir_is_sandboxed(self, tmp_path):
        """Calling the function WITHOUT skills_dir (as bootstrap() does) must hit
        the patched sandbox, leaving the real tree untouched."""
        real = {
            s: (PLUGIN_ROOT / "skills" / s / "SKILL.md").read_text()
            for s in bootstrap.ROUTING_SKILLS
        }
        cfg = _write_config(tmp_path, "Ada", "Acme Advisory Pte Ltd")
        bootstrap._inject_assistant_name_into_skills(cfg)  # default dir
        for s in bootstrap.ROUTING_SKILLS:
            assert (PLUGIN_ROOT / "skills" / s / "SKILL.md").read_text() == real[s], (
                f"real {s}/SKILL.md was mutated by the test suite"
            )


class TestGmailAttachmentClient:
    def _client(self):
        from google_client import GmailClient
        with patch.object(GmailClient, "__init__", lambda self, config=None: None):
            c = GmailClient()
        c._run = MagicMock(return_value={"ok": True})
        return c

    def test_list_attachments_args_and_list_coercion(self):
        c = self._client()
        c._run.return_value = [{"filename": "a.pdf"}]
        out = c.list_attachments("m1")
        c._run.assert_called_once_with("gmail", "attachments", "m1")
        assert out == [{"filename": "a.pdf"}]
        c._run.return_value = {"not": "a list"}
        assert c.list_attachments("m1") == []

    def test_download_by_filename(self):
        c = self._client()
        c.download_attachment("m1", filename="x.zip", output_dir="/tmp", output_name="y.zip")
        args = c._run.call_args[0]
        assert args[:3] == ("gmail", "attachment-download", "m1")
        assert ("--filename", "x.zip") == (args[3], args[4])
        assert "--output-dir" in args and "--output-name" in args

    def test_download_by_attachment_id(self):
        c = self._client()
        c.download_attachment("m1", attachment_id="ANGjd")
        args = c._run.call_args[0]
        assert ("--attachment-id", "ANGjd") == (args[3], args[4])

    def test_get_attachment_is_backcompat_shim(self):
        c = self._client()
        c.get_attachment("m1", "ANGjd")
        args = c._run.call_args[0]
        assert args[1] == "attachment-download"
        assert "--attachment-id" in args
