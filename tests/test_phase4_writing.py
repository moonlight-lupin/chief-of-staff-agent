#!/usr/bin/env python3
"""Contract tests for Phase 4B: Writing-for-agents fixes.

Based on combined Codex + Opus review of 18 SKILL.md + 2 docs.
13 items ranked by impact.
"""

import sys
import re
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = PLUGIN_ROOT / "skills"
DOCS_DIR = PLUGIN_ROOT / "docs"


def _read_skill(name: str) -> str:
    return (SKILLS_DIR / name / "SKILL.md").read_text()


def _skill_line_count(name: str) -> int:
    p = SKILLS_DIR / name / "SKILL.md"
    return sum(1 for _ in p.open()) if p.exists() else 0


def _frontmatter(text: str) -> dict:
    import yaml
    if text.startswith("---"):
        end = text.index("---", 3)
        return yaml.safe_load(text[3:end]) or {}
    return {}


# ═══════════════════════════════════════════════════════════════
# 1. Workspace access ladder extracted to shared reference
# ═══════════════════════════════════════════════════════════════

class TestWorkspaceAccessExtraction:
    """The ~40-line workspace access ladder must be extracted to one
    shared reference file, not copy-pasted into 5 skills."""

    def test_shared_reference_exists(self):
        """A shared workspace-access reference must exist."""
        candidates = [
            SKILLS_DIR / "daily-briefing" / "references" / "workspace-access.md",
            PLUGIN_ROOT / "shared" / "docs" / "workspace-access.md",
            PLUGIN_ROOT / "docs" / "workspace-access.md",
        ]
        assert any(p.exists() for p in candidates), (
            "workspace-access.md must exist in one of: "
            "skills/daily-briefing/references/, shared/docs/, docs/"
        )

    def test_daily_briefing_no_full_ladder(self):
        """daily-briefing must not contain the full 3-path workspace ladder inline."""
        content = _read_skill("daily-briefing")
        # The ladder has a distinctive pattern: 3 paths (agent/workspace_client/input)
        ladder_markers = [
            "agent-side connector",
            "workspace_client.py",
            "pre-fetched",
        ]
        matches = sum(1 for m in ladder_markers if m in content)
        # At most 1 marker (a pointer line), not all 3
        assert matches <= 1, (
            f"daily-briefing still has {matches}/3 workspace ladder markers inline "
            "(should be extracted to reference)"
        )


# ═══════════════════════════════════════════════════════════════
# 2. Routing tail removed from descriptions
# ═══════════════════════════════════════════════════════════════

class TestRoutingTailRemoved:
    """The 'When the user addresses Chief of Staff' routing tail must
    not be in the description (context pointer). It belongs in the body."""

    ROUTING_SKILLS = [
        "calendar-manager",
        "daily-briefing",
        "drive-filer",
        "email-organisation",
        "meeting-prep",
    ]

    @pytest.mark.parametrize("skill_name", ROUTING_SKILLS)
    def test_no_routing_tail_in_description(self, skill_name):
        """Description must not contain the routing instruction."""
        content = _read_skill(skill_name)
        fm = _frontmatter(content)
        desc = fm.get("description", "")
        assert "Chief of Staff" not in desc or "addresses" not in desc, (
            f"{skill_name} description still has routing tail — "
            "move to body, keep description as a trigger only"
        )


# ═══════════════════════════════════════════════════════════════
# 3. deep-research trimmed
# ═══════════════════════════════════════════════════════════════

class TestDeepResearchTrimmed:
    """deep-research must shed changelog, negation pitfalls, and off-domain content."""

    def test_under_450_lines(self):
        """deep-research must be under 450 lines (was 512)."""
        count = _skill_line_count("deep-research")
        assert count < 450, (
            f"deep-research is {count} lines — must shed changelog + "
            "negation pitfalls + off-domain content to get under 450"
        )

    def test_no_changelog_sediment(self):
        """Changelog blocks (v1.x changes) must be removed."""
        content = _read_skill("deep-research")
        assert "v1." not in content or "changes" not in content.lower(), (
            "deep-research still has changelog sediment"
        )


# ═══════════════════════════════════════════════════════════════
# 4. self-sign detection_patterns removed
# ═══════════════════════════════════════════════════════════════

class TestSelfSignPatternsRemoved:
    """The detection_patterns YAML block must be removed from self-sign
    (it duplicates sign_detector.py defaults)."""

    def test_no_inline_detection_patterns(self):
        """self-sign must not contain the full detection_patterns YAML block."""
        content = _read_skill("self-sign")
        # The block has a distinctive pattern
        assert "signature_patterns" not in content or content.count("signature_patterns") <= 1, (
            "self-sign still has inline detection_patterns YAML — "
            "defaults live in sign_detector.py"
        )


# ═══════════════════════════════════════════════════════════════
# 5. docs/HOOKS.md fixed
# ═══════════════════════════════════════════════════════════════

class TestHooksDocFixed:
    """docs/HOOKS.md must not describe disabled hooks as live guardrails."""

    def test_hooks_doc_first_line_status(self):
        """HOOKS.md must state the enabled/disabled status early."""
        content = (DOCS_DIR / "HOOKS.md").read_text()
        # Must mention "enabled" or "active" or "registered" in first 20 lines
        first_lines = "\n".join(content.split("\n")[:20])
        assert any(w in first_lines.lower() for w in ("enabled", "active", "registered", "7 quality hooks")), (
            "HOOKS.md must state hook status (enabled/active/registered) in first 20 lines"
        )

    def test_correct_skill_count(self):
        """HOOKS.md must say 18 skills, not 16 or 17."""
        content = (DOCS_DIR / "HOOKS.md").read_text()
        # Must not say "16 skills" or "17 skills"
        assert "17 skills" not in content, "HOOKS.md says 17 skills — should be 18"
        # Either says 18 or doesn't state a wrong count
        assert "16 skills" not in content, "HOOKS.md says 16 skills — should be 18"


# ═══════════════════════════════════════════════════════════════
# 6. docs/README.md fixed
# ═══════════════════════════════════════════════════════════════

class TestReadmeDocFixed:
    """docs/README.md must have correct skill count."""

    def test_correct_skill_count(self):
        """README.md must say 18 skills, not 16."""
        content = (DOCS_DIR / "README.md").read_text()
        assert "16 skills" not in content, (
            "docs/README.md says 16 skills — should be 18"
        )


# ═══════════════════════════════════════════════════════════════
# 7. Completion criteria added
# ═══════════════════════════════════════════════════════════════

class TestCompletionCriteria:
    """Skills that had zero or minimal completion criteria must now have them."""

    @pytest.mark.parametrize("skill_name", ["bookkeeper", "backup"])
    def test_has_completion_criterion(self, skill_name):
        """Each skill must have at least one 'Completion criterion:' line."""
        content = _read_skill(skill_name)
        count = content.lower().count("completion criterion")
        assert count >= 2, (
            f"{skill_name} has {count} completion criteria — need at least 2"
        )

    def test_todo_list_has_criteria(self):
        """todo-list must have completion criteria on more than just Add."""
        content = _read_skill("todo-list")
        count = content.lower().count("completion criterion")
        assert count >= 3, (
            f"todo-list has {count} completion criteria — need at least 3"
        )

    def test_pipeline_manager_has_criteria(self):
        """pipeline-manager must have completion criteria on more than 2 operations."""
        content = _read_skill("pipeline-manager")
        count = content.lower().count("completion criterion")
        assert count >= 3, (
            f"pipeline-manager has {count} completion criteria — need at least 3"
        )


# ═══════════════════════════════════════════════════════════════
# 9. esign-connector has verification checklist
# ═══════════════════════════════════════════════════════════════

class TestEsignVerificationChecklist:
    """esign-connector must have a verification checklist (only file without one)."""

    def test_has_verification_checklist(self):
        """esign-connector must have a Verification Checklist section."""
        content = _read_skill("esign-connector")
        assert "verification checklist" in content.lower() or "## verification" in content.lower(), (
            "esign-connector must have a Verification Checklist section"
        )


# ═══════════════════════════════════════════════════════════════
# 11. Descriptions rewritten as triggers
# ═══════════════════════════════════════════════════════════════

class TestDescriptionsAsTriggers:
    """Descriptions must be trigger-form, not capability-form."""

    TRIGGER_SKILLS = [
        "backup",
        "note-taker",
        "document-preparer",
        "self-sign",
        "deep-research",
    ]

    @pytest.mark.parametrize("skill_name", TRIGGER_SKILLS)
    def test_description_starts_with_use_when(self, skill_name):
        """Description must start with 'Use when' or a domain trigger word."""
        content = _read_skill(skill_name)
        fm = _frontmatter(content)
        desc = fm.get("description", "").strip().lower()
        triggers = [
            "use when", "use for", "use to",
            "briefing:", "calendar:", "backups:", "e-signature:",
            "notes:", "tasks:", "deadlines:", "pipeline:", "bookkeeping:",
            "deep research:", "entity research:", "travel itinerary:",
            "drive filing:", "meeting prep:", "weekly review:",
            "document preparation:", "self-sign:", "email organization:",
        ]
        assert any(desc.startswith(t) for t in triggers), (
            f"{skill_name} description must start with 'Use when/for/to' or "
            "a domain trigger word — not a capability statement"
        )


# ═══════════════════════════════════════════════════════════════
# 13. Negation converted to positive
# ═══════════════════════════════════════════════════════════════

class TestNegationToPositive:
    """Key negation patterns should be converted to positive statements."""

    def test_daily_briefing_positive_readonly(self):
        """daily-briefing should state read-only positively, not as a list of 'never X'."""
        content = _read_skill("daily-briefing")
        # The old form has "must never mark email as read, update a calendar..."
        assert "must never mark email as read" not in content, (
            "daily-briefing still has negation-form read-only rule — "
            "convert to positive: 'Read-only. Every mutation routes to its source skill.'"
        )