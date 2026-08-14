#!/usr/bin/env python3
"""Contract tests for Phase 4C: Wiki retrieval protocol + context hook + Opus fixes.

Tests:
1. wiki_curator.py search subcommand — retrieval protocol
2. wiki_context_injection hook — pre_llm_call wiki context injection
3. Opus P4B remaining: deep-research disambiguation, version bump, bookkeeper description
"""

import json
import sys
import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = PLUGIN_ROOT / "skills"
DOCS_DIR = PLUGIN_ROOT / "docs"
CURATOR = SKILLS_DIR / "note-taker" / "scripts" / "wiki_curator.py"


def _read_skill(name: str) -> str:
    return (SKILLS_DIR / name / "SKILL.md").read_text()


def _frontmatter(text: str) -> dict:
    import yaml
    if text.startswith("---"):
        end = text.index("---", 3)
        return yaml.safe_load(text[3:end]) or {}
    return {}


# ═══════════════════════════════════════════════════════════════
# 1. Wiki search subcommand
# ═══════════════════════════════════════════════════════════════

class TestWikiSearchSubcommand:
    """wiki_curator.py must have a search subcommand for retrieval."""

    def test_search_subcommand_exists(self):
        """wiki_curator.py must accept 'search' as a subcommand."""
        result = subprocess.run(
            [sys.executable, str(CURATOR), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"wiki_curator --help failed: {result.stderr}"
        assert "search" in result.stdout, (
            "wiki_curator.py must have a 'search' subcommand"
        )

    def test_search_returns_json(self):
        """search --format json must return valid JSON."""
        # Use a temp wiki with no pages — should return empty list
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp) / "wiki"
            wiki.mkdir()
            (wiki / "index.md").write_text("---\ntype: index\n---\n# Index\n")
            result = subprocess.run(
                [sys.executable, str(CURATOR), "search", "test",
                 "--format", "json", "--wiki", str(wiki)],
                capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, f"search failed: {result.stderr}"
            data = json.loads(result.stdout)
            assert isinstance(data, list), "search --format json must return a list"

    def test_search_returns_results(self):
        """search must find pages matching the query."""
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp) / "wiki"
            (wiki / "entities").mkdir(parents=True)
            (wiki / "concepts").mkdir(parents=True)
            (wiki / "index.md").write_text("---\ntype: index\n---\n# Index\n")

            # Create a test entity page
            (wiki / "entities" / "acme-corp.md").write_text(
                "---\ntype: entity\ntitle: ACME Corporation\ntags: [clients]\n"
                "aliases: [ACME, Acme Corp]\n---\n"
                "# ACME Corporation\nACME is a key client in manufacturing.\n"
            )
            # Create a test concept page
            (wiki / "concepts" / "supply-chain.md").write_text(
                "---\ntype: concept\ntitle: Supply Chain\ntags: [operations]\n---\n"
                "# Supply Chain\nManaging logistics and suppliers.\n"
            )

            result = subprocess.run(
                [sys.executable, str(CURATOR), "search", "ACME",
                 "--format", "json", "--wiki", str(wiki)],
                capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, f"search failed: {result.stderr}"
            data = json.loads(result.stdout)
            assert len(data) >= 1, "search for 'ACME' must find the acme-corp page"
            # Must return structured fields
            first = data[0]
            assert "path" in first or "title" in first, (
                "search results must have at least path or title field"
            )

    def test_search_alias_match(self):
        """search must match aliases in frontmatter."""
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp) / "wiki"
            (wiki / "entities").mkdir(parents=True)
            (wiki / "index.md").write_text("---\ntype: index\n---\n# Index\n")

            (wiki / "entities" / "acme-corp.md").write_text(
                "---\ntype: entity\ntitle: ACME Corporation\n"
                "aliases: [ACME, Acme Corp]\n---\n"
                "# ACME Corporation\nA client.\n"
            )

            # Search by alias "Acme Corp" should find the page
            result = subprocess.run(
                [sys.executable, str(CURATOR), "search", "Acme Corp",
                 "--format", "json", "--wiki", str(wiki)],
                capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, f"search failed: {result.stderr}"
            data = json.loads(result.stdout)
            assert len(data) >= 1, (
                "search must match aliases in frontmatter — 'Acme Corp' is an alias"
            )

    def test_search_text_format(self):
        """search --format text must return human-readable output."""
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp) / "wiki"
            (wiki / "entities").mkdir(parents=True)
            (wiki / "index.md").write_text("---\ntype: index\n---\n# Index\n")

            (wiki / "entities" / "acme-corp.md").write_text(
                "---\ntype: entity\ntitle: ACME Corporation\n---\n"
                "# ACME Corporation\nA client.\n"
            )

            result = subprocess.run(
                [sys.executable, str(CURATOR), "search", "ACME",
                 "--format", "text", "--wiki", str(wiki)],
                capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, f"search failed: {result.stderr}"
            assert "ACME" in result.stdout, (
                "search --format text must show matching page title"
            )

    def test_search_limit_flag(self):
        """search --limit N must cap the number of results."""
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp) / "wiki"
            (wiki / "concepts").mkdir(parents=True)
            (wiki / "index.md").write_text("---\ntype: index\n---\n# Index\n")

            for i in range(10):
                (wiki / "concepts" / f"concept-{i}.md").write_text(
                    f"---\ntype: concept\ntitle: Concept {i}\n---\n"
                    f"# Concept {i}\nA test concept about testing.\n"
                )

            result = subprocess.run(
                [sys.executable, str(CURATOR), "search", "concept",
                 "--format", "json", "--limit", "3", "--wiki", str(wiki)],
                capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, f"search failed: {result.stderr}"
            data = json.loads(result.stdout)
            assert len(data) <= 3, (
                f"search --limit 3 must cap results, got {len(data)}"
            )


# ═══════════════════════════════════════════════════════════════
# 2. Wiki context injection hook
# ═══════════════════════════════════════════════════════════════

class TestWikiContextInjection:
    """A pre_llm_call hook must inject relevant wiki context."""

    def test_hook_exists(self):
        """wiki_context_injection hook must exist in hooks.py."""
        sys.path.insert(0, str(PLUGIN_ROOT))
        try:
            from hooks import ALL_HOOKS
            pre_hooks = ALL_HOOKS.get("pre_llm_call", [])
            hook_names = [name for name, _ in pre_hooks]
            assert "wiki_context_injection" in hook_names, (
                f"wiki_context_injection must be in ALL_HOOKS['pre_llm_call']. "
                f"Found: {hook_names}"
            )
        finally:
            sys.path.pop(0)

    def test_hook_callable(self):
        """wiki_context_injection must be callable and return str or None."""
        sys.path.insert(0, str(PLUGIN_ROOT))
        try:
            from hooks import wiki_context_injection
            result = wiki_context_injection(context={}, message="")
            assert result is None or isinstance(result, str), (
                "hook must return str or None"
            )
        finally:
            sys.path.pop(0)

    def test_hook_skips_short_messages(self):
        """Hook must not fire on very short messages (< 5 words)."""
        sys.path.insert(0, str(PLUGIN_ROOT))
        try:
            from hooks import wiki_context_injection
            result = wiki_context_injection(context={}, message="hi")
            assert result is None, (
                "Hook must not fire on very short messages"
            )
        finally:
            sys.path.pop(0)


# ═══════════════════════════════════════════════════════════════
# 3. Opus P4B remaining fixes
# ═══════════════════════════════════════════════════════════════

class TestOpusP4BFixes:
    """Remaining Opus P4B findings applied."""

    def test_deep_research_description_has_disambiguation(self):
        """deep-research description must mention entity-research or sibling skills."""
        content = _read_skill("deep-research")
        fm = _frontmatter(content)
        desc = fm.get("description", "").lower()
        assert "entity-research" in desc or "entity research" in desc, (
            "deep-research description must disambiguate from entity-research"
        )

    def test_deep_research_version_bumped(self):
        """deep-research version must be bumped from 1.5.0 after changelog removal."""
        content = _read_skill("deep-research")
        fm = _frontmatter(content)
        version = fm.get("version", "0")
        # Must be higher than 1.5.0
        parts = str(version).split(".")
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        assert (major, minor) > (1, 5), (
            f"deep-research version is {version} — must be bumped past 1.5.0"
        )

    def test_bookkeeper_description_no_scope_note(self):
        """bookkeeper description must not end with scope note about accounting system."""
        content = _read_skill("bookkeeper")
        fm = _frontmatter(content)
        desc = fm.get("description", "")
        assert "without becoming a full accounting system" not in desc.lower(), (
            "bookkeeper description must not have scope note — "
            "move to body, keep description as a trigger"
        )

    def test_deep_research_mit_attribution_exists(self):
        """MIT attribution for sn-deep-research must exist somewhere in the skill."""
        # Check both SKILL.md and references/
        skill_content = _read_skill("deep-research")
        ref_dir = SKILLS_DIR / "deep-research" / "references"
        ref_content = ""
        if ref_dir.exists():
            for f in ref_dir.glob("*.md"):
                ref_content += f.read_text()

        combined = skill_content + ref_content
        assert "SenseNova" in combined or "sn-deep-research" in combined, (
            "MIT attribution for sn-deep-research must exist in SKILL.md or references/"
        )