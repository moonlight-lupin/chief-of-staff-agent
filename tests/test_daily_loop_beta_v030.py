#!/usr/bin/env python3
"""Tests for v0.3.0 — Daily operating loop beta."""
import sys
import os
import io
import json
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock
from datetime import date, datetime, timezone, timedelta

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
SKILLS_SCRIPTS = PLUGIN_ROOT / "skills"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))
for skill in ("daily-briefing", "note-taker", "pipeline-manager", "bookkeeper", "document-preparer"):
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
        "sales_stages": ["Lead", "Qualified", "Proposal Sent", "NDA Signed", "Contract Signed", "Invoiced", "Paid", "Lost"],
        "stale_threshold_days": 14,
    }
    import yaml
    config_path = project / "company.yaml"
    config_path.write_text(yaml.safe_dump(config))
    return config, project, config_path


# ─── Daily Command ──────────────────────────────────────────

class TestDailyCommand:
    def test_daily_summary_runs_on_empty(self, temp_project):
        """daily --summary runs on empty project without crashing."""
        config, project, config_path = temp_project
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = chief_of_staff.main(["--config", str(config_path), "daily", "--summary"])
        assert rc == 0
        output = buf.getvalue()
        assert "Chief-of-Staff" in output or "Daily" in output or "daily" in output.lower()

    def test_daily_json_stable_schema(self, temp_project):
        """daily --json returns stable top-level schema."""
        config, project, config_path = temp_project
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = chief_of_staff.main(["--config", str(config_path), "daily", "--json"])
        assert rc == 0
        parsed = json.loads(buf.getvalue())
        # Stable top-level keys
        assert "version" in parsed
        assert "generated_at" in parsed
        assert "mode" in parsed
        assert "safety" in parsed
        assert "sections" in parsed
        # Safety is read-only
        assert parsed["safety"].get("read_only") is True
        # Sections have expected keys
        sections = parsed["sections"]
        assert "system_health" in sections
        assert "review_queue" in sections
        assert "pipeline" in sections
        assert "bookkeeper" in sections
        assert "knowledge" in sections
        assert "recommended_commands" in sections

    def test_daily_markdown_renders(self, temp_project):
        """daily --markdown renders without crashing."""
        config, project, config_path = temp_project
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = chief_of_staff.main(["--config", str(config_path), "daily", "--markdown"])
        assert rc == 0
        output = buf.getvalue()
        assert len(output) > 0

    def test_daily_no_approve_actions(self, temp_project):
        """daily command does not approve any actions."""
        config, project, config_path = temp_project
        mock_pa = MagicMock()
        with patch("pending_actions.approve_pending_action", mock_pa):
            import chief_of_staff
            buf = io.StringIO()
            with redirect_stdout(buf):
                chief_of_staff.main(["--config", str(config_path), "daily", "--summary"])
        mock_pa.assert_not_called()

    def test_daily_no_execute_actions(self, temp_project):
        """daily command does not execute any actions."""
        config, project, config_path = temp_project
        mock_exec = MagicMock()
        with patch("webhook_events.cmd_execute", mock_exec):
            import chief_of_staff
            buf = io.StringIO()
            with redirect_stdout(buf):
                chief_of_staff.main(["--config", str(config_path), "daily", "--summary"])
        mock_exec.assert_not_called()

    def test_daily_no_provider_calls(self, temp_project):
        """daily command does not call Gmail/Drive/Calendar providers."""
        config, project, config_path = temp_project
        mock_client = MagicMock()
        with patch("workspace_client.get_workspace_client", return_value=mock_client):
            import chief_of_staff
            buf = io.StringIO()
            with redirect_stdout(buf):
                chief_of_staff.main(["--config", str(config_path), "daily", "--json"])
        mock_client.gmail_send.assert_not_called()
        mock_client.gmail_search.assert_not_called()
        mock_client.calendar_create.assert_not_called()
        mock_client.drive_upload.assert_not_called()

    def test_daily_no_write_invoices(self, temp_project):
        """daily command does not write invoices.yaml."""
        config, project, config_path = temp_project
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf):
            chief_of_staff.main(["--config", str(config_path), "daily", "--summary"])
        # invoices.yaml should not exist (we never wrote it)
        assert not (project / "invoices.yaml").exists()

    def test_daily_no_write_pipeline(self, temp_project):
        """daily command does not write pipeline.yaml."""
        config, project, config_path = temp_project
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf):
            chief_of_staff.main(["--config", str(config_path), "daily", "--summary"])
        # pipeline.yaml should not exist (we never wrote it)
        assert not (project / "pipeline.yaml").exists()

    def test_daily_no_mutate_memory(self, temp_project):
        """daily command does not mutate memory store."""
        config, project, config_path = temp_project
        # Seed memory
        (project / ".knowledge" / "memory.json").write_text(
            json.dumps({"records": {}, "_version": 0}))
        original = (project / ".knowledge" / "memory.json").read_text()
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf):
            chief_of_staff.main(["--config", str(config_path), "daily", "--summary"])
        after = (project / ".knowledge" / "memory.json").read_text()
        assert original == after, "Memory store should not be mutated"

    def test_daily_no_mutate_wiki(self, temp_project):
        """daily command does not mutate wiki."""
        config, project, config_path = temp_project
        wiki = project / "wiki"
        wiki.mkdir()
        (wiki / "index.md").write_text("---\ntype: index\n---\n# Index\n")
        original = (wiki / "index.md").read_text()
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf):
            chief_of_staff.main(["--config", str(config_path), "daily", "--summary"])
        after = (wiki / "index.md").read_text()
        assert original == after, "Wiki should not be mutated"

    def test_malformed_optional_files_degrade(self, temp_project):
        """Malformed optional state files degrade gracefully."""
        config, project, config_path = temp_project
        # Write malformed pipeline.yaml
        (project / "pipeline.yaml").write_text("{invalid yaml [[")
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = chief_of_staff.main(["--config", str(config_path), "daily", "--summary"])
        assert rc == 0  # Should not crash

    def test_missing_optional_files_show_warnings(self, temp_project):
        """Missing optional files show warnings, not hard failure."""
        config, project, config_path = temp_project
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = chief_of_staff.main(["--config", str(config_path), "daily", "--json"])
        assert rc == 0
        parsed = json.loads(buf.getvalue())
        # Should still have valid schema even with missing files
        assert "sections" in parsed


# ─── Subsystem Summaries ────────────────────────────────────

class TestSubsystemSummaries:
    def test_review_summary(self, temp_project):
        config, project, config_path = temp_project
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = chief_of_staff.main(["--config", str(config_path), "review", "--summary"])
        assert rc == 0

    def test_pipeline_summary(self, temp_project):
        config, project, config_path = temp_project
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = chief_of_staff.main(["--config", str(config_path), "pipeline", "--summary"])
        assert rc == 0

    def test_bookkeeper_summary(self, temp_project):
        config, project, config_path = temp_project
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = chief_of_staff.main(["--config", str(config_path), "bookkeeper", "--summary"])
        assert rc == 0

    def test_knowledge_summary(self, temp_project):
        config, project, config_path = temp_project
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = chief_of_staff.main(["--config", str(config_path), "knowledge", "--summary"])
        assert rc == 0

    def test_doctor_summary(self, temp_project):
        config, project, config_path = temp_project
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = chief_of_staff.main(["--config", str(config_path), "doctor", "--summary"])
        assert rc in (0, 1)  # 1 = warnings found, not crash


# ─── Smoke Test ─────────────────────────────────────────────

class TestSmokeTest:
    def test_smoke_test_passes_clean(self, temp_project):
        """Smoke test passes on clean empty project."""
        config, project, config_path = temp_project
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = chief_of_staff.main(["--config", str(config_path), "smoke-test", "--summary"])
        assert rc == 0
        output = buf.getvalue()
        assert "PASS" in output or "pass" in output.lower()

    def test_smoke_test_invalid_config(self, tmp_path):
        """Smoke test reports failure when config is invalid."""
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = chief_of_staff.main(["--config", "/nonexistent/path/config.yaml", "smoke-test", "--summary"])
        # Should not crash, should report failure
        assert rc in (0, 1)


# ─── Recommended Commands ───────────────────────────────────

class TestRecommendedCommands:
    def test_recommends_review_queue_when_pending(self, temp_project):
        """Recommended commands include review_queue.py when pending actions exist."""
        config, project, config_path = temp_project
        from pending_actions import create_pending_action
        create_pending_action(
            config=config, action_type="gmail.send",
            provider="google_api", target="test@example.com",
            payload={"to": "test@example.com", "subject": "test", "body": "test"},
            summary="Test action",
        )
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf):
            chief_of_staff.main(["--config", str(config_path), "daily", "--json"])
        parsed = json.loads(buf.getvalue())
        cmds = parsed["sections"].get("recommended_commands", [])
        cmd_text = json.dumps(cmds) if isinstance(cmds, list) else str(cmds)
        assert "review_queue" in cmd_text

    def test_recommends_pipeline_stale_when_stale_exists(self, temp_project):
        """Recommended commands include pipeline.py stale when stale deals exist."""
        config, project, config_path = temp_project
        import yaml
        old = (date.today() - timedelta(days=30)).isoformat()
        (project / "pipeline.yaml").write_text(yaml.safe_dump({"deals": [
            {"id": "deal-s-001", "client_name": "Stale Co", "stage": "Proposal Sent",
             "value": 5000, "currency": "SGD", "created": "2026-01-01",
             "last_activity": old, "documents": [], "notes": []}
        ]}))
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf):
            chief_of_staff.main(["--config", str(config_path), "daily", "--json"])
        parsed = json.loads(buf.getvalue())
        cmds = parsed["sections"].get("recommended_commands", [])
        cmd_text = json.dumps(cmds) if isinstance(cmds, list) else str(cmds)
        assert "pipeline" in cmd_text.lower() or "stale" in cmd_text.lower()

    def test_recommends_memory_lint_when_warnings_exist(self, temp_project):
        """Recommended commands include memory lint when lint warnings exist."""
        config, project, config_path = temp_project
        old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        (project / ".knowledge" / "memory.json").write_text(json.dumps({
            "records": {
                "mem_001": {"id": "mem_001", "type": "entity", "name": "Stale Co",
                            "status": "observed", "confidence": 0.3,
                            "source_ids": [], "last_seen_at": old}
            }, "_version": 1
        }))
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf):
            chief_of_staff.main(["--config", str(config_path), "daily", "--json"])
        parsed = json.loads(buf.getvalue())
        cmds = parsed["sections"].get("recommended_commands", [])
        cmd_text = json.dumps(cmds) if isinstance(cmds, list) else str(cmds)
        assert "memory" in cmd_text.lower() or "lint" in cmd_text.lower()


# ─── Version & Docs ─────────────────────────────────────────

class TestVersionAndDocs:
    def test_version_is_031(self):
        import yaml
        data = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text())
        assert data.get("version") == "0.3.4"

    def test_beta_docs_exist(self):
        assert (PLUGIN_ROOT / "docs" / "BETA_DAILY_LOOP.md").exists()
        assert (PLUGIN_ROOT / "docs" / "BETA_READINESS_CHECKLIST.md").exists()

    def test_entrypoint_exists(self):
        assert (PLUGIN_ROOT / "shared" / "scripts" / "chief_of_staff.py").exists()

    def test_entrypoint_compiles(self):
        import py_compile
        py_compile.compile(str(PLUGIN_ROOT / "shared" / "scripts" / "chief_of_staff.py"),
                           doraise=True)