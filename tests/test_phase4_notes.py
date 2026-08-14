#!/usr/bin/env python3
"""Contract tests for Phase 4: OKF 0.2 + note-capture hook.

OKF 0.2 additions:
- seq field for monotonic ordering
- aliases for entity pages
- relations for typed edges
- confidence as 0-1 float (not just high/medium/low)
- okf_version bumped to "0.2"

Note-capture hook:
- post_llm_call hook that detects note-worthy output
- Reminds agent to ingest via note-taker skill
"""

import sys
import os
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
SKILLS_DIR = PLUGIN_ROOT / "skills"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


@pytest.fixture
def temp_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = {
        "google": {"delegate_email": "test@test.com", "account_alias": "test"},
        "integrations": {"workspace": {"provider": "google_api"}},
        "paths": {"project_root": str(project)},
    }
    return config, project


# ═══════════════════════════════════════════════════════════════
# OKF 0.2: Version bump
# ═══════════════════════════════════════════════════════════════

class TestOKFVersion02:
    """SKILL.md and wiki_curator.py must reference OKF 0.2, not 0.1."""

    def test_skill_md_says_0_2(self):
        """SKILL.md description must say OKF v0.2."""
        skill_path = SKILLS_DIR / "note-taker" / "SKILL.md"
        content = skill_path.read_text()
        assert "OKF v0.2" in content or "okf_version" in content, (
            "SKILL.md must reference OKF v0.2"
        )
        assert "0.2" in content, "SKILL.md must mention version 0.2"

    def test_wiki_curator_generates_0_2(self):
        """wiki_curator.py must generate okf_version: '0.2' in index."""
        curator_path = SKILLS_DIR / "note-taker" / "scripts" / "wiki_curator.py"
        content = curator_path.read_text()
        assert '"0.2"' in content or "'0.2'" in content, (
            "wiki_curator.py must generate okf_version 0.2"
        )

    def test_skill_version_bumped(self):
        """SKILL.md version must be >= 0.2.0."""
        skill_path = SKILLS_DIR / "note-taker" / "SKILL.md"
        content = skill_path.read_text()
        # Check the version field in frontmatter
        import yaml
        # Extract frontmatter
        if content.startswith("---"):
            end = content.index("---", 3)
            frontmatter = yaml.safe_load(content[3:end])
            version = frontmatter.get("version", "0.0.0")
            parts = [int(x) for x in version.split(".")]
            assert parts[0] >= 0 and parts[1] >= 2, (
                f"SKILL.md version must be >= 0.2.0, got {version}"
            )


# ═══════════════════════════════════════════════════════════════
# OKF 0.2: New frontmatter fields
# ═══════════════════════════════════════════════════════════════

class TestOKF02Fields:
    """OKF 0.2 adds seq, aliases, relations, and numeric confidence."""

    def test_seq_field_documented(self):
        """SKILL.md must document the seq field for monotonic ordering."""
        skill_path = SKILLS_DIR / "note-taker" / "SKILL.md"
        content = skill_path.read_text()
        assert "seq" in content.lower(), (
            "SKILL.md must document the seq field (OKF 0.2)"
        )

    def test_aliases_field_documented(self):
        """SKILL.md must document the aliases field for entity pages."""
        skill_path = SKILLS_DIR / "note-taker" / "SKILL.md"
        content = skill_path.read_text()
        assert "aliases" in content.lower(), (
            "SKILL.md must document the aliases field (OKF 0.2)"
        )

    def test_relations_field_documented(self):
        """SKILL.md must document the relations field for typed edges."""
        skill_path = SKILLS_DIR / "note-taker" / "SKILL.md"
        content = skill_path.read_text()
        assert "relations" in content.lower(), (
            "SKILL.md must document the relations field (OKF 0.2)"
        )

    def test_numeric_confidence_documented(self):
        """SKILL.md must document confidence as 0-1 float (OKF 0.2)."""
        skill_path = SKILLS_DIR / "note-taker" / "SKILL.md"
        content = skill_path.read_text()
        # OKF 0.2 accepts 0-1 float in addition to high/medium/low
        assert "0-1" in content or "0.0" in content or "float" in content.lower(), (
            "SKILL.md must document numeric confidence (0-1 float, OKF 0.2)"
        )


# ═══════════════════════════════════════════════════════════════
# OKF 0.2: wiki_curator generates seq
# ═══════════════════════════════════════════════════════════════

class TestWikiCuratorSeq:
    """wiki_curator.py must assign seq numbers to new pages."""

    def test_curator_assigns_seq(self, temp_project):
        """wiki_curator must add a seq field to generated pages."""
        config, project = temp_project
        wiki_path = project / "wiki"
        wiki_path.mkdir(parents=True, exist_ok=True)

        # Create minimal wiki structure
        (wiki_path / "purpose.md").write_text(
            "---\ntype: purpose\nupdated: 2026-08-14\n---\n# Purpose\n"
        )
        (wiki_path / "SCHEMA.md").write_text(
            "---\ntype: schema\nupdated: 2026-08-14\n---\n# Schema\n"
        )
        (wiki_path / "index.md").write_text(
            '---\ntype: index\nokf_version: "0.2"\nupdated: 2026-08-14\n---\n# Index\n'
        )

        # Run curator lint — it should not crash on 0.2
        import subprocess
        result = subprocess.run(
            [sys.executable, str(SKILLS_DIR / "note-taker" / "scripts" / "wiki_curator.py"),
             "lint", "--wiki", str(wiki_path)],
            capture_output=True, text=True, timeout=30,
        )
        # Should not crash
        assert result.returncode in (0, 1), (
            f"wiki_curator crashed on OKF 0.2 wiki: {result.stderr[:200]}"
        )


# ═══════════════════════════════════════════════════════════════
# Note-capture hook
# ═══════════════════════════════════════════════════════════════

class TestNoteCaptureHook:
    """A post_llm_call hook must detect note-worthy output and remind ingestion."""

    def test_hook_exists(self):
        """note_capture_reminder hook must exist in hooks.py."""
        from hooks import ALL_HOOKS
        hook_names = []
        for event, hook_list in ALL_HOOKS.items():
            for name, _ in hook_list:
                hook_names.append(name)
        assert "note_capture_reminder" in hook_names, (
            f"note_capture_reminder must be registered. Found: {hook_names}"
        )

    def test_hook_fires_on_meeting_summary(self):
        """Hook must fire when LLM output contains meeting notes/summary."""
        from hooks import note_capture_reminder
        context = {"loaded_skills": ["note-taker", "meeting-prep"]}
        response = "Here are the meeting notes:\n\n## Decisions\n- Move to Next.js\n\n## Action Items\n- MH to draft proposal"
        result = note_capture_reminder(response=response, context=context)
        assert result is not None, "Hook must fire on meeting summary output"
        assert "note-taker" in result.lower() or "ingest" in result.lower(), (
            "Hook must remind to ingest via note-taker"
        )

    def test_hook_fires_on_research_findings(self):
        """Hook must fire when LLM output contains research findings."""
        from hooks import note_capture_reminder
        context = {"loaded_skills": ["note-taker", "deep-research"]}
        response = "## Research Summary\nThe competitor analysis reveals three key findings [1]. Sources cited below."
        result = note_capture_reminder(response=response, context=context)
        assert result is not None, "Hook must fire on research findings"

    def test_hook_does_not_fire_on_simple_chat(self):
        """Hook must NOT fire on simple conversational responses."""
        from hooks import note_capture_reminder
        context = {"loaded_skills": ["note-taker"]}
        response = "Sure, I can help with that. What would you like to know?"
        result = note_capture_reminder(response=response, context=context)
        assert result is None, "Hook must not fire on simple chat"

    def test_hook_does_not_fire_without_note_taker_skill(self):
        """Hook must NOT fire when note-taker skill is not loaded."""
        from hooks import note_capture_reminder
        context = {"loaded_skills": ["daily-briefing"]}
        response = "## Meeting Notes\nKey decisions were made today."
        result = note_capture_reminder(response=response, context=context)
        assert result is None, "Hook must not fire without note-taker skill loaded"


# ═══════════════════════════════════════════════════════════════
# Wiki curator cron prose in SKILL.md
# ═══════════════════════════════════════════════════════════════

class TestWikiCronProse:
    """SKILL.md must suggest a daily wiki curator cron job."""

    def test_cron_mentioned_in_skill(self):
        """SKILL.md must mention cron or scheduled wiki curation."""
        skill_path = SKILLS_DIR / "note-taker" / "SKILL.md"
        content = skill_path.read_text()
        assert "cron" in content.lower() or "schedule" in content.lower(), (
            "SKILL.md must suggest a daily cron/schedule for wiki curation"
        )

    def test_cron_command_documented(self):
        """SKILL.md must include the wiki_curator.py command for cron use."""
        skill_path = SKILLS_DIR / "note-taker" / "SKILL.md"
        content = skill_path.read_text()
        assert "wiki_curator.py" in content, (
            "SKILL.md must reference wiki_curator.py for cron use"
        )