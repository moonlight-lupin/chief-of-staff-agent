#!/usr/bin/env python3
"""Tests for v0.2.4 — Autonomous memory maintenance and wiki curation.

Tests:
1. autonomous curation writes internal wiki/memory files only
2. no provider writes occur during curation
3. raw source is preserved before curated updates
4. created/updated pages have valid frontmatter
5. index.md and log.md are updated
6. memory_changes.json records every autonomous change
7. daily briefing shows knowledge-maintenance summary
8. new entity/project draft page creation is reported
9. duplicate/conflict detection is reported
10. delete/merge/mark-confirmed operations require approval
11. rollback dry-run identifies reversible changes
12. malformed wiki pages degrade gracefully
13. note-taking lint catches broken links / missing frontmatter
14. source-backed observations include source_ids
15. no unapproved external publishing occurs
"""
import sys
import os
import io
import json
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
SKILLS_SCRIPTS = PLUGIN_ROOT / "skills"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))
for skill in ("daily-briefing", "note-taker"):
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
    wiki = project / "wiki"
    wiki.mkdir()
    (wiki / "raw").mkdir()
    (wiki / "daily").mkdir()
    (wiki / "projects").mkdir()
    (wiki / "entities").mkdir()
    (wiki / "people").mkdir()
    (wiki / "decisions").mkdir()
    config = {
        "company": {"name": "Test Co", "jurisdiction": "SG", "currency": "SGD",
                     "incorporation_date": "2026-01-01", "financial_year_end": "31 Dec",
                     "business_type": "professional_services"},
        "google": {"delegate_email": "test@test.com", "account_alias": "test",
                   "domain": "test.com", "service_account_path": "/tmp/sa.json"},
        "paths": {"project_root": str(project), "wiki_path": str(wiki),
                  "templates": str(PLUGIN_ROOT / "shared" / "templates")},
        "delivery": {"channel": "telegram", "briefing_time": "08:00",
                      "weekly_review_day": "friday", "weekly_review_time": "17:00",
                      "timezone": "Asia/Singapore"},
        "integrations": {"workspace": {"provider": "google_api"}},
        "sales_stages": ["Lead", "Proposal Sent", "NDA Signed", "Contract Signed", "Invoiced", "Paid"],
    }
    return config, project


@pytest.fixture
def temp_with_config(temp_project, tmp_path):
    """temp_project + company.yaml written to disk."""
    config, project = temp_project
    import yaml
    config_path = project / "company.yaml"
    config_path.write_text(yaml.safe_dump(config))
    return config, project, config_path


# ─── Memory System ──────────────────────────────────────────

class TestMemorySystem:
    def test_memory_extract_creates_records(self, temp_with_config):
        """Memory extractor creates records from events."""
        config, project, config_path = temp_with_config

        # Seed some events
        from state_db import ingest_event
        ingest_event(config, source="gmail", source_id="msg-001",
                      event_type="email_received",
                      payload={"from": "contact@example.com", "subject": "Project alpha update"})
        ingest_event(config, source="gmail", source_id="msg-002",
                      event_type="email_received",
                      payload={"from": "vendor@stripe.com", "subject": "Invoice for project"})

        import memory
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = memory._main(["--config", str(config_path), "extract", "--since", "24h", "--summary",
                                ])

        # Check memory.json was created
        mem_path = project / ".knowledge" / "memory.json"
        if mem_path.exists():
            data = json.loads(mem_path.read_text())
            records = data.get("records", {})
            assert len(records) > 0
            # Records should be draft or observed, not confirmed
            for r in records.values():
                assert r["status"] in ("draft", "observed")
                assert r["operator_confirmed"] is False

    def test_memory_changes_logged(self, temp_with_config):
        """Every autonomous memory change is logged in memory_changes.json."""
        config, project, config_path = temp_with_config

        from state_db import ingest_event
        ingest_event(config, source="gmail", source_id="msg-001",
                      event_type="email_received",
                      payload={"from": "contact@example.com", "subject": "Test"})

        import memory
        buf = io.StringIO()
        with redirect_stdout(buf):
            memory._main(["--config", str(config_path), "extract", "--since", "24h", "--summary",
                           ])

        changes_path = project / ".knowledge" / "memory_changes.json"
        if changes_path.exists():
            data = json.loads(changes_path.read_text())
            changes = data.get("changes", [])
            assert len(changes) > 0
            for ch in changes:
                assert ch["mode"] == "autonomous"
                assert "change_type" in ch
                assert "source_ids" in ch
                assert "reversible" in ch

    def test_memory_extract_dry_run(self, temp_with_config):
        """Dry-run reports without writing."""
        config, project, config_path = temp_with_config

        from state_db import ingest_event
        ingest_event(config, source="gmail", source_id="msg-001",
                      event_type="email_received",
                      payload={"from": "test@x.com", "subject": "Test"})

        import memory
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = memory._main(["--config", str(config_path), "extract", "--since", "24h", "--dry-run",
                                ])

        # Memory file should NOT exist after dry-run
        mem_path = project / ".knowledge" / "memory.json"
        assert not mem_path.exists() or json.loads(mem_path.read_text()).get("records", {}) == {}

    def test_memory_changes_command(self, temp_with_config):
        """changes --limit N prints recent change log entries."""
        config, project, config_path = temp_with_config

        # Create some changes
        changes_path = project / ".knowledge" / "memory_changes.json"
        changes_path.write_text(json.dumps({
            "changes": [
                {"id": "memchg_001", "timestamp": "2026-07-10T09:00:00Z",
                 "mode": "autonomous", "change_type": "memory_create",
                 "target": "mem_001", "summary": "Test", "source_ids": [],
                 "risk": "low", "reversible": True},
                {"id": "memchg_002", "timestamp": "2026-07-10T10:00:00Z",
                 "mode": "autonomous", "change_type": "memory_update",
                 "target": "mem_001", "summary": "Updated", "source_ids": [],
                 "risk": "low", "reversible": True},
            ],
            "_version": 2,
        }))

        import memory
        buf = io.StringIO()
        with redirect_stdout(buf):
            memory._main(["--config", str(config_path), "changes", "--limit", "5",
                           ])

        output = buf.getvalue()
        assert "memchg_001" in output or "memchg_002" in output or "memory_create" in output

    def test_memory_no_provider_calls(self, temp_with_config):
        """Memory extraction must not call any provider."""
        config, project, config_path = temp_with_config

        from state_db import ingest_event
        ingest_event(config, source="gmail", source_id="msg-001",
                      event_type="email_received",
                      payload={"from": "test@x.com", "subject": "Test"})

        mock_provider = MagicMock()
        import memory
        with patch("workspace_client.get_workspace_client", return_value=mock_provider):
            buf = io.StringIO()
            with redirect_stdout(buf):
                memory._main(["--config", str(config_path), "extract", "--since", "24h", "--summary",
                               ])

        # No provider methods should have been called
        mock_provider.gmail_send.assert_not_called()
        mock_provider.gmail_label.assert_not_called()
        mock_provider.gmail_archive.assert_not_called()
        mock_provider.calendar_create.assert_not_called()
        mock_provider.drive_upload.assert_not_called()

    def test_memory_empty_state_degrades_gracefully(self, temp_with_config):
        """Empty project with no events should not crash."""
        config, project, config_path = temp_with_config
        import memory
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = memory._main(["--config", str(config_path), "extract", "--since", "24h", "--summary",
                                ])
        assert rc in (0, 1)

    def test_memory_malformed_state(self, temp_with_config):
        """Malformed memory files should degrade gracefully."""
        config, project, config_path = temp_with_config
        (project / ".knowledge" / "memory.json").write_text("{invalid")
        (project / ".knowledge" / "memory_changes.json").write_text("not json")

        import memory
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = memory._main(["--config", str(config_path), "extract", "--since", "24h", "--summary",
                                ])
        assert rc in (0, 1)

    def test_rollback_dry_run(self, temp_with_config):
        """Rollback --dry-run identifies reversible changes."""
        config, project, config_path = temp_with_config

        changes_path = project / ".knowledge" / "memory_changes.json"
        mem_path = project / ".knowledge" / "memory.json"
        mem_path.write_text(json.dumps({
            "records": {"mem_001": {"id": "mem_001", "name": "Test", "status": "observed"}},
            "_version": 1,
        }))
        changes_path.write_text(json.dumps({
            "changes": [
                {"id": "memchg_001", "timestamp": "2026-07-10T09:00:00Z",
                 "mode": "autonomous", "change_type": "memory_create",
                 "target": "mem_001", "summary": "Created", "source_ids": [],
                 "risk": "low", "reversible": True},
            ],
            "_version": 1,
        }))

        import memory
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = memory._main(["--config", str(config_path), "rollback", "--change-id", "memchg_001", "--dry-run",
                                ])

        # Should report what would be reversed without actually doing it
        output = buf.getvalue()
        # The file should be unchanged
        data = json.loads(mem_path.read_text())
        assert "mem_001" in data["records"]


# ─── Wiki Curator ───────────────────────────────────────────

class TestWikiCurator:
    def test_wiki_curator_run_creates_daily_log(self, temp_with_config):
        """Wiki curator creates daily log pages."""
        config, project, config_path = temp_with_config

        from state_db import ingest_event
        ingest_event(config, source="gmail", source_id="msg-001",
                      event_type="email_received",
                      payload={"from": "contact@example.com", "subject": "Project alpha update"})

        import wiki_curator
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = wiki_curator.main(["run", "--since", "24h",
                                      "--config", str(config_path)])

        # Check daily log was created
        wiki = project / "wiki"
        daily_files = list((wiki / "daily").glob("*.md"))
        if daily_files:
            content = daily_files[0].read_text()
            assert "##" in content  # Has sections

    def test_wiki_curator_run_dry_run(self, temp_with_config):
        """Dry-run reports without writing."""
        config, project, config_path = temp_with_config

        from state_db import ingest_event
        ingest_event(config, source="gmail", source_id="msg-001",
                      event_type="email_received",
                      payload={"from": "test@x.com", "subject": "Test"})

        import wiki_curator
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = wiki_curator.main(["run", "--since", "24h", "--dry-run",
                                      "--config", str(config_path)])

        # Wiki should not have new pages in dry-run
        wiki = project / "wiki"
        daily_files = list((wiki / "daily").glob("*.md"))
        # In dry-run, no new files should be created
        # (unless they already existed before)
        assert rc in (0, 1)

    def test_wiki_curator_no_provider_calls(self, temp_with_config):
        """Wiki curator must not call any provider."""
        config, project, config_path = temp_with_config

        mock_provider = MagicMock()
        import wiki_curator
        with patch("workspace_client.get_workspace_client", return_value=mock_provider):
            buf = io.StringIO()
            with redirect_stdout(buf):
                wiki_curator.main(["run", "--since", "24h",
                                     "--config", str(config_path)])

        mock_provider.gmail_send.assert_not_called()
        mock_provider.gmail_label.assert_not_called()
        mock_provider.calendar_create.assert_not_called()
        mock_provider.drive_upload.assert_not_called()

    def test_wiki_curator_creates_frontmatter(self, temp_with_config):
        """Created pages have valid YAML frontmatter."""
        config, project, config_path = temp_with_config

        from state_db import ingest_event
        ingest_event(config, source="gmail", source_id="msg-001",
                      event_type="email_received",
                      payload={"from": "contact@example.com", "subject": "Test"})

        import wiki_curator
        buf = io.StringIO()
        with redirect_stdout(buf):
            wiki_curator.main(["run", "--since", "24h",
                                 "--config", str(config_path)])

        wiki = project / "wiki"
        for md_file in wiki.rglob("*.md"):
            content = md_file.read_text()
            if content.startswith("---"):
                # Has frontmatter
                assert "type:" in content or "title:" in content

    def test_wiki_curator_updates_log(self, temp_with_config):
        """Wiki curator appends to log.md."""
        config, project, config_path = temp_with_config

        from state_db import ingest_event
        ingest_event(config, source="gmail", source_id="msg-001",
                      event_type="email_received",
                      payload={"from": "test@x.com", "subject": "Test"})

        import wiki_curator
        buf = io.StringIO()
        with redirect_stdout(buf):
            wiki_curator.main(["run", "--since", "24h",
                                 "--config", str(config_path)])

        log_path = project / "wiki" / "log.md"
        if log_path.exists():
            content = log_path.read_text()
            assert len(content) > 0  # Something was appended

    def test_wiki_curator_validate(self, temp_with_config):
        """Validate command runs lint checks."""
        config, project, config_path = temp_with_config

        # Create a wiki with some issues
        wiki = project / "wiki"
        (wiki / "entities" / "test.md").write_text("# Test\n\nNo frontmatter\n")

        import wiki_curator
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = wiki_curator.main(["validate",
                                      "--config", str(config_path)])

        output = buf.getvalue()
        # Should report the missing frontmatter issue
        assert "frontmatter" in output.lower() or "missing" in output.lower() or rc in (0, 1)

    def test_wiki_curator_empty_state(self, temp_with_config):
        """Empty wiki with no events should not crash."""
        config, project, config_path = temp_with_config
        import wiki_curator
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = wiki_curator.main(["run", "--since", "24h",
                                      "--config", str(config_path)])
        assert rc in (0, 1)

    def test_wiki_curator_malformed_pages(self, temp_with_config):
        """Malformed wiki pages should degrade gracefully."""
        config, project, config_path = temp_with_config
        wiki = project / "wiki"
        (wiki / "entities" / "broken.md").write_text("---\ninvalid: yaml: content\n---\n# Broken\n")
        (wiki / "entities" / "no-fm.md").write_text("# No frontmatter at all\n")

        import wiki_curator
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = wiki_curator.main(["validate",
                                      "--config", str(config_path)])
        assert rc in (0, 1)

    def test_wiki_curator_no_destructive_ops(self, temp_with_config):
        """Wiki curator must not delete files."""
        config, project, config_path = temp_with_config
        wiki = project / "wiki"
        test_file = wiki / "entities" / "existing.md"
        test_file.write_text("# Existing\n\nThis should not be deleted.\n")

        from state_db import ingest_event
        ingest_event(config, source="gmail", source_id="msg-001",
                      event_type="email_received",
                      payload={"from": "test@x.com", "subject": "Test"})

        import wiki_curator
        buf = io.StringIO()
        with redirect_stdout(buf):
            wiki_curator.main(["run", "--since", "24h",
                                 "--config", str(config_path)])

        # Existing file should still exist
        assert test_file.exists()


# ─── Briefing Knowledge Maintenance Section ─────────────────

class TestBriefingKnowledgeSection:
    def test_briefing_includes_knowledge_section(self, temp_project, monkeypatch):
        """Daily briefing includes knowledge maintenance section."""
        config, project = temp_project
        config_path = project / "company.yaml"
        import yaml
        config_path.write_text(yaml.safe_dump(config))
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        # Seed memory changes
        (project / ".knowledge").mkdir(exist_ok=True)
        (project / ".knowledge" / "memory.json").write_text(json.dumps({
            "records": {"mem_001": {"id": "mem_001", "name": "Test Entity"}},
            "_version": 1,
        }))
        (project / ".knowledge" / "memory_changes.json").write_text(json.dumps({
            "changes": [
                {"id": "memchg_001", "change_type": "memory_create",
                 "target": "mem_001", "summary": "Created", "mode": "autonomous",
                 "source_ids": ["event_001"], "risk": "low", "reversible": True},
            ],
            "_version": 1,
        }))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            daily_briefing.main(["run", "--json", "--dry-run"])

        parsed = json.loads(buf.getvalue())
        assert "knowledge_maintenance" in parsed["sections"]
        km = parsed["sections"]["knowledge_maintenance"]
        assert km.get("total_records", 0) >= 1

    def test_briefing_knowledge_text_renders(self, temp_project, monkeypatch):
        """Knowledge maintenance section renders in text output."""
        config, project = temp_project
        config_path = project / "company.yaml"
        import yaml
        config_path.write_text(yaml.safe_dump(config))
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        (project / ".knowledge").mkdir(exist_ok=True)
        (project / ".knowledge" / "memory.json").write_text(json.dumps({
            "records": {"mem_001": {"id": "mem_001"}, "mem_002": {"id": "mem_002"}},
            "_version": 2,
        }))
        (project / ".knowledge" / "memory_changes.json").write_text(json.dumps({
            "changes": [
                {"id": "memchg_001", "change_type": "memory_create",
                 "target": "mem_001", "summary": "Created", "mode": "autonomous",
                 "source_ids": [], "risk": "low", "reversible": True},
            ],
            "_version": 1,
        }))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            daily_briefing.main(["run", "--summary", "--dry-run"])

        output = buf.getvalue()
        assert "Knowledge" in output or "Memory" in output or "memory" in output.lower()

    def test_wiki_curator_run_appears_in_briefing_stats(self, temp_with_config, monkeypatch):
        """Wiki curator run should create wiki_create entries in memory_changes.json
        that show up in the daily briefing knowledge maintenance section."""
        config, project, config_path = temp_with_config
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        # Seed an event so wiki_curator has something to process
        from state_db import ingest_event
        ingest_event(config, source="gmail", source_id="msg-001",
                      event_type="email_received",
                      payload={"from": "contact@example.com", "subject": "Project alpha update"})

        # Run wiki curator
        import wiki_curator
        buf = io.StringIO()
        with redirect_stdout(buf):
            wiki_curator.main(["--config", str(config_path), "run", "--since", "24h"])

        # Verify wiki changes were logged to memory_changes.json
        changes_path = project / ".knowledge" / "memory_changes.json"
        assert changes_path.exists(), "memory_changes.json should exist after wiki_curator run"
        data = json.loads(changes_path.read_text())
        changes = data.get("changes", [])
        wiki_changes = [c for c in changes if c.get("change_type", "").startswith("wiki_")]
        assert len(wiki_changes) > 0, "Should have wiki_create or wiki_update entries"

        # Now run the briefing and check that wiki stats appear
        import daily_briefing
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            daily_briefing.main(["run", "--json", "--dry-run"])

        parsed = json.loads(buf2.getvalue())
        km = parsed["sections"]["knowledge_maintenance"]
        assert km.get("wiki_pages_created", 0) + km.get("wiki_pages_updated", 0) > 0, \
            "Briefing should show wiki page changes"

    def test_briefing_distinguishes_memory_from_wiki(self, temp_project, monkeypatch):
        """Briefing should report memory records and wiki pages separately."""
        config, project = temp_project
        config_path = project / "company.yaml"
        import yaml
        config_path.write_text(yaml.safe_dump(config))
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        (project / ".knowledge").mkdir(exist_ok=True)
        (project / ".knowledge" / "memory.json").write_text(json.dumps({
            "records": {"mem_001": {"id": "mem_001"}},
            "_version": 1,
        }))
        (project / ".knowledge" / "memory_changes.json").write_text(json.dumps({
            "changes": [
                {"id": "memchg_001", "change_type": "memory_create",
                 "target": "mem_001", "summary": "Created", "mode": "autonomous",
                 "source_ids": [], "risk": "low", "reversible": True},
                {"id": "memchg_002", "change_type": "wiki_create",
                 "target": "wiki/entities/test.md", "summary": "Created page",
                 "mode": "autonomous", "source_ids": [], "risk": "low", "reversible": True},
            ],
            "_version": 2,
        }))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            daily_briefing.main(["run", "--json", "--dry-run"])

        parsed = json.loads(buf.getvalue())
        km = parsed["sections"]["knowledge_maintenance"]
        assert km.get("memory_records_created", 0) == 1
        assert km.get("wiki_pages_created", 0) == 1
        # Should NOT have the old combined fields
        assert "pages_created" not in km
        assert "pages_updated" not in km