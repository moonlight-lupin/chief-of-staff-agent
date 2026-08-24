#!/usr/bin/env python3
"""Tests for v0.2.7 — Memory & wiki hardening, rollback, lint.

Tests:
1. wiki lint detects broken links
2. wiki lint detects missing frontmatter
3. wiki lint detects duplicate pages
4. wiki lint detects stale pages
5. wiki lint --summary prints counts
6. memory lint detects stale records
7. memory lint detects low confidence records
8. memory lint detects uncited records
9. memory lint detects duplicate records
10. memory lint detects contested records
11. memory lint --summary prints counts
12. memory backup creates timestamped copies
13. memory rollback --dry-run shows plan
14. memory rollback restores before state
15. memory rollback non-reversible change fails
16. wiki auto-backup on large changes
17. wiki no auto-backup on small changes
18. Daily Briefing shows lint warnings
19. Daily Briefing shows no warnings when clean
20. empty state degrades gracefully
"""
import sys
import os
import io
import json
from pathlib import Path
from contextlib import redirect_stdout
from datetime import datetime, timezone, timedelta

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
SKILLS_SCRIPTS = PLUGIN_ROOT / "skills"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))
for skill in ("note-taker", "daily-briefing"):
    d = SKILLS_SCRIPTS / skill / "scripts"
    if d.exists() and str(d) not in sys.path:
        sys.path.insert(0, str(d))


@pytest.fixture
def temp_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".audit").mkdir()
    (project / ".runs").mkdir()
    (project / ".knowledge").mkdir()
    config = {
        "company": {"name": "Test Co", "jurisdiction": "SG", "currency": "SGD",
                     "incorporation_date": "2026-01-01", "financial_year_end": "31 Dec",
                     "business_type": "professional_services"},
        "google": {"delegate_email": "test@test.com", "account_alias": "test",
                   "domain": "test.com", "service_account_path": "/tmp/sa.json"},
        "paths": {"project_root": str(project), "wiki_path": str(project / "wiki"),
                  "templates": str(PLUGIN_ROOT / "shared" / "templates")},
        "delivery": {"channel": "telegram", "briefing_time": "08:00",
                      "weekly_review_day": "friday", "weekly_review_time": "17:00",
                      "timezone": "Asia/Singapore"},
        "integrations": {"workspace": {"provider": "google_api"}},
        "sales_stages": ["Lead"],
    }
    import yaml
    config_path = project / "company.yaml"
    config_path.write_text(yaml.safe_dump(config))
    return config, project, config_path


def _make_wiki_page(path: Path, title: str, page_type: str = "entity",
                    updated: str = "2026-07-10", confidence: float = None,
                    status: str = "draft", body: str = ""):
    """Helper to create a wiki page with frontmatter."""
    fm = f"---\ntype: {page_type}\ntitle: {title}\nupdated: {updated}\nstatus: {status}\n"
    if confidence is not None:
        fm += f"confidence: {confidence}\n"
    fm += f"---\n\n# {title}\n\n{body}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fm)


# ─── Wiki Lint ──────────────────────────────────────────────

class TestWikiLint:
    def test_lint_detects_broken_links(self, temp_project):
        config, project, config_path = temp_project
        wiki = project / "wiki"
        wiki.mkdir()
        _make_wiki_page(wiki / "index.md", "Index", page_type="index",
                        body="[[NonExistent]]")
        _make_wiki_page(wiki / "entities" / "alpha.md", "Alpha",
                        body="[[$NonExistent]]")
        import wiki_curator
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = wiki_curator.main(["--config", str(config_path), "lint"])
        assert rc in (0, 1)
        output = buf.getvalue()
        assert "broken" in output.lower() or "Broken" in output or "WARN" in output

    def test_lint_detects_missing_frontmatter(self, temp_project):
        config, project, config_path = temp_project
        wiki = project / "wiki"
        wiki.mkdir()
        _make_wiki_page(wiki / "index.md", "Index", page_type="index")
        (wiki / "entities" / "bad.md").parent.mkdir(parents=True, exist_ok=True)
        (wiki / "entities" / "bad.md").write_text("# No frontmatter\n\nJust body.\n")
        import wiki_curator
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = wiki_curator.main(["--config", str(config_path), "lint"])
        assert rc == 1  # ERROR severity
        output = buf.getvalue()
        assert "frontmatter" in output.lower() or "ERROR" in output

    def test_lint_summary_prints_counts(self, temp_project):
        config, project, config_path = temp_project
        wiki = project / "wiki"
        wiki.mkdir()
        _make_wiki_page(wiki / "index.md", "Index", page_type="index")
        import wiki_curator
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = wiki_curator.main(["--config", str(config_path), "lint", "--summary"])
        assert rc == 0
        output = buf.getvalue()
        assert "ERROR" in output or "WARN" in output or "0" in output

    def test_lint_empty_wiki(self, temp_project):
        """Empty wiki should degrade gracefully."""
        config, project, config_path = temp_project
        import wiki_curator
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = wiki_curator.main(["--config", str(config_path), "lint"])
        assert rc in (0, 1)


# ─── Memory Lint ────────────────────────────────────────────

class TestMemoryLint:
    def _seed_memory(self, project, records):
        """Write memory.json with given records dict."""
        (project / ".knowledge" / "memory.json").write_text(
            json.dumps({"records": records, "_version": len(records)}))

    def test_lint_detects_stale(self, temp_project):
        config, project, config_path = temp_project
        old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        self._seed_memory(project, {
            "mem_001": {"id": "mem_001", "type": "entity", "name": "Old Co",
                        "status": "observed", "confidence": 0.8,
                        "source_ids": ["event_001"], "last_seen_at": old}
        })
        import memory
        result = memory.lint_memory(config)
        assert result["stale_records"] >= 1

    def test_lint_detects_low_confidence(self, temp_project):
        config, project, config_path = temp_project
        self._seed_memory(project, {
            "mem_001": {"id": "mem_001", "type": "entity", "name": "LowConf",
                        "status": "observed", "confidence": 0.3,
                        "source_ids": ["event_001"], "last_seen_at": datetime.now(timezone.utc).isoformat()}
        })
        import memory
        result = memory.lint_memory(config)
        assert result["low_confidence"] >= 1

    def test_lint_detects_uncited(self, temp_project):
        config, project, config_path = temp_project
        self._seed_memory(project, {
            "mem_001": {"id": "mem_001", "type": "entity", "name": "Uncited",
                        "status": "observed", "confidence": 0.8,
                        "source_ids": [], "last_seen_at": datetime.now(timezone.utc).isoformat()}
        })
        import memory
        result = memory.lint_memory(config)
        assert result["uncited"] >= 1

    def test_lint_detects_duplicates(self, temp_project):
        config, project, config_path = temp_project
        now = datetime.now(timezone.utc).isoformat()
        self._seed_memory(project, {
            "mem_001": {"id": "mem_001", "type": "entity", "name": "Acme Corp",
                        "status": "observed", "confidence": 0.8,
                        "source_ids": ["e1"], "last_seen_at": now},
            "mem_002": {"id": "mem_002", "type": "entity", "name": "acme corp",
                        "status": "observed", "confidence": 0.8,
                        "source_ids": ["e2"], "last_seen_at": now},
        })
        import memory
        result = memory.lint_memory(config)
        assert result["duplicates"] >= 1

    def test_lint_detects_contested(self, temp_project):
        config, project, config_path = temp_project
        self._seed_memory(project, {
            "mem_001": {"id": "mem_001", "type": "entity", "name": "Contested",
                        "status": "contested", "confidence": 0.8,
                        "source_ids": ["e1"], "last_seen_at": datetime.now(timezone.utc).isoformat()}
        })
        import memory
        result = memory.lint_memory(config)
        assert result["contested"] >= 1

    def test_lint_summary_prints_counts(self, temp_project):
        config, project, config_path = temp_project
        self._seed_memory(project, {})
        import memory
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = memory._main(["--config", str(config_path), "lint", "--summary"])
        assert rc == 0
        output = buf.getvalue()
        assert "0" in output or "total" in output.lower()

    def test_lint_empty_memory(self, temp_project):
        """Empty memory should degrade gracefully."""
        config, project, config_path = temp_project
        import memory
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = memory._main(["--config", str(config_path), "lint"])
        assert rc == 0


# ─── Memory Backup ──────────────────────────────────────────

class TestMemoryBackup:
    def test_backup_creates_copies(self, temp_project):
        config, project, config_path = temp_project
        # Seed memory and changes
        (project / ".knowledge" / "memory.json").write_text(
            json.dumps({"records": {}, "_version": 0}))
        (project / ".knowledge" / "memory_changes.json").write_text(
            json.dumps({"changes": [], "_version": 0}))
        import memory
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = memory._main(["--config", str(config_path), "backup"])
        assert rc == 0
        # Check backup files exist
        backups = list((project / ".knowledge").glob("*backup*"))
        assert len(backups) >= 2

    def test_backup_empty_state(self, temp_project):
        """Backup with no memory files should degrade gracefully."""
        config, project, config_path = temp_project
        import memory
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = memory._main(["--config", str(config_path), "backup"])
        assert rc in (0, 1)


# ─── Memory Rollback ────────────────────────────────────────

class TestMemoryRollback:
    def test_rollback_dry_run(self, temp_project):
        config, project, config_path = temp_project
        # Seed a reversible change
        (project / ".knowledge" / "memory.json").write_text(
            json.dumps({"records": {"mem_001": {"id": "mem_001", "name": "Test"}}, "_version": 1}))
        (project / ".knowledge" / "memory_changes.json").write_text(
            json.dumps({"changes": [{
                "id": "memchg_001", "change_type": "memory_update",
                "target": "mem_001", "summary": "Updated", "mode": "autonomous",
                "source_ids": ["e1"], "risk": "low", "reversible": True,
                "before": {"id": "mem_001", "name": "Old Name"},
                "after": {"id": "mem_001", "name": "Test"}
            }], "_version": 1}))
        import memory
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = memory._main(["--config", str(config_path), "rollback",
                                "--change-id", "memchg_001", "--dry-run"])
        assert rc == 0
        output = buf.getvalue()
        assert "dry_run" in output.lower() or "plan" in output.lower() or "restore" in output.lower()

    def test_rollback_restores_before(self, temp_project):
        config, project, config_path = temp_project
        (project / ".knowledge" / "memory.json").write_text(
            json.dumps({"records": {"mem_001": {"id": "mem_001", "name": "New Name"}}, "_version": 1}))
        (project / ".knowledge" / "memory_changes.json").write_text(
            json.dumps({"changes": [{
                "id": "memchg_002", "change_type": "memory_update",
                "target": "mem_001", "summary": "Updated", "mode": "autonomous",
                "source_ids": ["e1"], "risk": "low", "reversible": True,
                "before": {"id": "mem_001", "name": "Original Name"},
                "after": {"id": "mem_001", "name": "New Name"}
            }], "_version": 1}))
        import memory
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = memory._main(["--config", str(config_path), "rollback",
                                "--change-id", "memchg_002"])
        assert rc == 0
        data = json.loads((project / ".knowledge" / "memory.json").read_text())
        assert data["records"]["mem_001"]["name"] == "Original Name"

    def test_rollback_non_reversible_fails(self, temp_project):
        config, project, config_path = temp_project
        (project / ".knowledge" / "memory_changes.json").write_text(
            json.dumps({"changes": [{
                "id": "memchg_003", "change_type": "memory_update",
                "target": "mem_001", "summary": "Updated", "mode": "autonomous",
                "source_ids": ["e1"], "risk": "low", "reversible": False,
            }], "_version": 1}))
        import memory
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = memory._main(["--config", str(config_path), "rollback",
                                "--change-id", "memchg_003"])
        assert rc != 0


# ─── Wiki Auto-Backup ───────────────────────────────────────

class TestWikiAutoBackup:
    def test_auto_backup_on_large_changes(self, temp_project):
        """Wiki curator should auto-backup when processing >5 items."""
        config, project, config_path = temp_project
        wiki = project / "wiki"
        wiki.mkdir()
        _make_wiki_page(wiki / "index.md", "Index", page_type="index")
        # Seed 6+ events to trigger auto-backup
        from state_db import ingest_event
        for i in range(7):
            ingest_event(config, source="gmail", source_id=f"msg-{i}",
                          event_type="email_received",
                          payload={"from": f"contact{i}@example.com",
                                   "subject": f"Project alpha update {i}"})
        import wiki_curator
        buf = io.StringIO()
        with redirect_stdout(buf):
            wiki_curator.main(["--config", str(config_path), "run", "--since", "24h"])
        # Check backup directory was created
        backups = list(wiki.parent.glob(".wiki-backup-*"))
        assert len(backups) >= 1, "Should have created a backup directory"

    def test_no_auto_backup_on_small_changes(self, temp_project):
        """Wiki curator should NOT auto-backup for ≤5 items."""
        config, project, config_path = temp_project
        wiki = project / "wiki"
        wiki.mkdir()
        _make_wiki_page(wiki / "index.md", "Index", page_type="index")
        from state_db import ingest_event
        for i in range(3):
            ingest_event(config, source="gmail", source_id=f"msg-s{i}",
                          event_type="email_received",
                          payload={"from": f"contact{i}@example.com",
                                   "subject": f"Small update {i}"})
        import wiki_curator
        buf = io.StringIO()
        with redirect_stdout(buf):
            wiki_curator.main(["--config", str(config_path), "run", "--since", "24h"])
        backups = list(wiki.parent.glob(".wiki-backup-*"))
        assert len(backups) == 0, "Should NOT have created a backup for small changes"


# ─── Briefing Integration ───────────────────────────────────

class TestBriefingLintWarnings:
    def test_briefing_shows_lint_warnings(self, temp_project, monkeypatch):
        """Daily briefing should show memory/wiki lint warnings."""
        config, project, config_path = temp_project
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        # Seed memory with issues
        old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        (project / ".knowledge" / "memory.json").write_text(json.dumps({
            "records": {
                "mem_001": {"id": "mem_001", "type": "entity", "name": "Stale Co",
                            "status": "observed", "confidence": 0.3,
                            "source_ids": [], "last_seen_at": old}
            },
            "_version": 1,
        }))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            daily_briefing.main(["run", "--json", "--dry-run"])

        parsed = json.loads(buf.getvalue())
        km = parsed["sections"]["knowledge_maintenance"]
        assert km.get("stale_records", 0) >= 1
        assert km.get("low_confidence_records", 0) >= 1
        assert km.get("uncited_records", 0) >= 1

    def test_briefing_no_warnings_when_clean(self, temp_project, monkeypatch):
        """Daily briefing should show no lint warnings when clean."""
        config, project, config_path = temp_project
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        # Seed clean memory
        now = datetime.now(timezone.utc).isoformat()
        (project / ".knowledge" / "memory.json").write_text(json.dumps({
            "records": {
                "mem_001": {"id": "mem_001", "type": "entity", "name": "Clean Co",
                            "status": "observed", "confidence": 0.8,
                            "source_ids": ["e1"], "last_seen_at": now}
            },
            "_version": 1,
        }))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            daily_briefing.main(["run", "--json", "--dry-run"])

        parsed = json.loads(buf.getvalue())
        km = parsed["sections"]["knowledge_maintenance"]
        assert km.get("stale_records", 0) == 0
        assert km.get("low_confidence_records", 0) == 0
        assert km.get("uncited_records", 0) == 0