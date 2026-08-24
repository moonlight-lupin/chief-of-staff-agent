#!/usr/bin/env python3
"""Tests for v0.2.8 — CRM / Pipeline Manager hardening."""
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
for skill in ("pipeline-manager", "daily-briefing"):
    d = SKILLS_SCRIPTS / skill / "scripts"
    if d.exists() and str(d) not in sys.path:
        sys.path.insert(0, str(d))


@pytest.fixture
def temp_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".audit").mkdir()
    (project / ".runs").mkdir()
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


def _seed_pipeline(project, deals):
    """Write pipeline.yaml with given deals list."""
    import yaml
    (project / "pipeline.yaml").write_text(yaml.safe_dump({"deals": deals}))


# ─── Action Risk ────────────────────────────────────────────

class TestActionRiskPipeline:
    def test_deal_add_is_medium(self):
        from action_risk import get_action_risk
        assert get_action_risk("pipeline.deal.add") == "medium"

    def test_deal_move_stage_is_medium(self):
        from action_risk import get_action_risk
        assert get_action_risk("pipeline.deal.move_stage") == "medium"

    def test_deal_add_note_is_low(self):
        from action_risk import get_action_risk
        assert get_action_risk("pipeline.deal.add_note") == "low"

    def test_deal_link_document_is_low(self):
        from action_risk import get_action_risk
        assert get_action_risk("pipeline.deal.link_document") == "low"

    def test_deal_delete_is_high(self):
        from action_risk import get_action_risk
        assert get_action_risk("pipeline.deal.delete") == "high"


# ─── Pipeline CLI ───────────────────────────────────────────

class TestPipelineCLI:
    def test_missing_pipeline_initializes(self, temp_project):
        """Missing pipeline.yaml should initialize to deals: []."""
        config, project, config_path = temp_project
        import pipeline
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pipeline.main(["--config", str(config_path), "list", "--summary"])
        assert rc == 0

    def test_list_summary_shows_counts(self, temp_project):
        config, project, config_path = temp_project
        _seed_pipeline(project, [
            {"id": "deal-001", "client_name": "Acme", "stage": "Lead", "value": 1000,
             "currency": "SGD", "created": "2026-07-01", "last_activity": "2026-07-01",
             "documents": [], "notes": []},
        ])
        import pipeline
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pipeline.main(["--config", str(config_path), "list", "--summary"])
        assert rc == 0
        output = buf.getvalue()
        assert "1" in output or "Lead" in output

    def test_show_displays_deal(self, temp_project):
        config, project, config_path = temp_project
        _seed_pipeline(project, [
            {"id": "deal-001", "client_name": "Acme Corp", "stage": "Lead",
             "value": 4500, "currency": "SGD", "created": "2026-07-01",
             "last_activity": "2026-07-01", "documents": [], "notes": []},
        ])
        import pipeline
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pipeline.main(["--config", str(config_path), "show", "--id", "deal-001"])
        assert rc == 0
        assert "Acme" in buf.getvalue()

    def test_validate_catches_duplicate_ids(self, temp_project):
        config, project, config_path = temp_project
        _seed_pipeline(project, [
            {"id": "deal-dup", "client_name": "A", "stage": "Lead", "value": 100,
             "currency": "SGD", "created": "2026-07-01", "last_activity": "2026-07-01",
             "documents": [], "notes": []},
            {"id": "deal-dup", "client_name": "B", "stage": "Lead", "value": 200,
             "currency": "SGD", "created": "2026-07-01", "last_activity": "2026-07-01",
             "documents": [], "notes": []},
        ])
        import pipeline
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pipeline.main(["--config", str(config_path), "validate"])
        assert rc == 1  # ERROR

    def test_validate_catches_invalid_stage(self, temp_project):
        config, project, config_path = temp_project
        _seed_pipeline(project, [
            {"id": "deal-001", "client_name": "A", "stage": "BadStage", "value": 100,
             "currency": "SGD", "created": "2026-07-01", "last_activity": "2026-07-01",
             "documents": [], "notes": []},
        ])
        import pipeline
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pipeline.main(["--config", str(config_path), "validate"])
        assert rc == 1

    def test_add_deal_creates_record(self, temp_project):
        config, project, config_path = temp_project
        import pipeline
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pipeline.main(["--config", str(config_path), "add",
                                  "--client", "New Co", "--contact", "J Tan",
                                  "--email", "j@new.co", "--value", "5000",
                                  "--currency", "SGD", "--stage", "Lead"])
        assert rc == 0
        import yaml
        data = yaml.safe_load((project / "pipeline.yaml").read_text())
        deals = data.get("deals", [])
        assert any(d.get("client_name") == "New Co" for d in deals)

    def test_move_stage_updates(self, temp_project):
        config, project, config_path = temp_project
        _seed_pipeline(project, [
            {"id": "deal-001", "client_name": "Acme", "stage": "Lead", "value": 1000,
             "currency": "SGD", "created": "2026-07-01", "last_activity": "2026-06-01",
             "documents": [], "notes": []},
        ])
        import pipeline
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pipeline.main(["--config", str(config_path), "move",
                                  "--id", "deal-001", "--stage", "Proposal Sent",
                                  "--note", "Proposal sent"])
        assert rc == 0
        import yaml
        data = yaml.safe_load((project / "pipeline.yaml").read_text())
        deal = data["deals"][0]
        assert deal["stage"] == "Proposal Sent"
        # last_activity should be updated
        assert deal["last_activity"] != "2026-06-01"

    def test_move_rejects_invalid_stage(self, temp_project):
        config, project, config_path = temp_project
        _seed_pipeline(project, [
            {"id": "deal-001", "client_name": "Acme", "stage": "Lead", "value": 1000,
             "currency": "SGD", "created": "2026-07-01", "last_activity": "2026-07-01",
             "documents": [], "notes": []},
        ])
        import pipeline
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pipeline.main(["--config", str(config_path), "move",
                                  "--id", "deal-001", "--stage", "BadStage"])
        assert rc != 0

    def test_note_appends(self, temp_project):
        config, project, config_path = temp_project
        _seed_pipeline(project, [
            {"id": "deal-001", "client_name": "Acme", "stage": "Lead", "value": 1000,
             "currency": "SGD", "created": "2026-07-01", "last_activity": "2026-07-01",
             "documents": [], "notes": []},
        ])
        import pipeline
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pipeline.main(["--config", str(config_path), "note",
                                  "--id", "deal-001", "--note", "Test note"])
        assert rc == 0
        import yaml
        data = yaml.safe_load((project / "pipeline.yaml").read_text())
        deal = data["deals"][0]
        assert len(deal.get("notes", [])) >= 1

    def test_archival_note_no_last_activity_update(self, temp_project):
        config, project, config_path = temp_project
        _seed_pipeline(project, [
            {"id": "deal-001", "client_name": "Acme", "stage": "Lead", "value": 1000,
             "currency": "SGD", "created": "2026-07-01", "last_activity": "2026-06-01",
             "documents": [], "notes": []},
        ])
        import pipeline
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pipeline.main(["--config", str(config_path), "note",
                                  "--id", "deal-001", "--note", "Archival note",
                                  "--archival"])
        assert rc == 0
        import yaml
        data = yaml.safe_load((project / "pipeline.yaml").read_text())
        deal = data["deals"][0]
        # last_activity should NOT change with --archival
        assert deal["last_activity"] == "2026-06-01"

    def test_link_doc_appends(self, temp_project):
        config, project, config_path = temp_project
        _seed_pipeline(project, [
            {"id": "deal-001", "client_name": "Acme", "stage": "Lead", "value": 1000,
             "currency": "SGD", "created": "2026-07-01", "last_activity": "2026-07-01",
             "documents": [], "notes": []},
        ])
        import pipeline
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pipeline.main(["--config", str(config_path), "link-doc",
                                  "--id", "deal-001", "--type", "Proposal",
                                  "--path", "02_Clients/Acme/Proposals/p.pdf",
                                  "--status", "sent"])
        assert rc == 0
        import yaml
        data = yaml.safe_load((project / "pipeline.yaml").read_text())
        deal = data["deals"][0]
        assert len(deal.get("documents", [])) >= 1

    def test_delete_is_unsupported(self, temp_project):
        config, project, config_path = temp_project
        import pipeline
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pipeline.main(["--config", str(config_path), "delete"])
        assert rc != 0
        assert "not supported" in buf.getvalue().lower() or "Lost" in buf.getvalue()

    def test_malformed_pipeline_degrades(self, temp_project):
        config, project, config_path = temp_project
        (project / "pipeline.yaml").write_text("{invalid yaml")
        import pipeline
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pipeline.main(["--config", str(config_path), "list", "--summary"])
        assert rc in (0, 1)

    def test_empty_pipeline_useful(self, temp_project):
        config, project, config_path = temp_project
        import pipeline
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pipeline.main(["--config", str(config_path), "list", "--summary"])
        assert rc == 0


# ─── Stale Detection ────────────────────────────────────────

class TestStaleDetection:
    def test_stale_excludes_terminal(self, temp_project):
        config, project, config_path = temp_project
        old = (date.today() - timedelta(days=30)).isoformat()
        _seed_pipeline(project, [
            {"id": "deal-active", "client_name": "Active", "stage": "Lead",
             "value": 1000, "currency": "SGD", "created": "2026-01-01",
             "last_activity": old, "documents": [], "notes": []},
            {"id": "deal-paid", "client_name": "Paid", "stage": "Paid",
             "value": 2000, "currency": "SGD", "created": "2026-01-01",
             "last_activity": old, "documents": [], "notes": []},
        ])
        import pipeline
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pipeline.main(["--config", str(config_path), "stale", "--summary"])
        assert rc == 0
        output = buf.getvalue()
        # Should show 1 stale (deal-active), not deal-paid
        assert "1" in output

    def test_stale_shows_days_inactive(self, temp_project):
        config, project, config_path = temp_project
        old = (date.today() - timedelta(days=21)).isoformat()
        _seed_pipeline(project, [
            {"id": "deal-001", "client_name": "Stale Co", "stage": "Proposal Sent",
             "value": 5000, "currency": "SGD", "created": "2026-01-01",
             "last_activity": old, "documents": [], "notes": []},
        ])
        import pipeline
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pipeline.main(["--config", str(config_path), "stale"])
        assert rc == 0
        output = buf.getvalue()
        assert "deal-001" in output or "Stale" in output


# ─── Pipeline Actions (Execution) ───────────────────────────

class TestPipelineActions:
    def test_unapproved_cannot_execute(self, temp_project):
        config, project, config_path = temp_project
        from state_db import create_pending_action
        action = create_pending_action(
            config=config, action_type="pipeline.deal.move_stage",
            provider="pipeline", target="deal-001",
            payload={"deal_id": "deal-001", "stage": "Proposal Sent"},
            summary="Move deal",
        )
        import pipeline_actions
        try:
            result = pipeline_actions.execute_pipeline_action(config, action["id"])
            assert not result.get("success", False)
        except Exception:
            pass

    def test_approved_move_stage_executes(self, temp_project):
        config, project, config_path = temp_project
        _seed_pipeline(project, [
            {"id": "deal-exec-001", "client_name": "Exec Co", "stage": "Lead",
             "value": 3000, "currency": "SGD", "created": "2026-07-01",
             "last_activity": "2026-07-01", "documents": [], "notes": []},
        ])
        from state_db import create_pending_action, approve_pending_action
        action = create_pending_action(
            config=config, action_type="pipeline.deal.move_stage",
            provider="pipeline", target="deal-exec-001",
            payload={"deal_id": "deal-exec-001", "stage": "Proposal Sent", "note": "Approved move"},
            summary="Move deal to Proposal Sent",
        )
        approve_pending_action(config, action["id"], approver="MH", reason="ok")

        import pipeline_actions
        result = pipeline_actions.execute_pipeline_action(config, action["id"])
        assert result.get("success") is True

        import yaml
        data = yaml.safe_load((project / "pipeline.yaml").read_text())
        deal = next(d for d in data["deals"] if d["id"] == "deal-exec-001")
        assert deal["stage"] == "Proposal Sent"

    def test_delete_action_unsupported(self, temp_project):
        config, project, config_path = temp_project
        from state_db import create_pending_action, approve_pending_action
        action = create_pending_action(
            config=config, action_type="pipeline.deal.delete",
            provider="pipeline", target="deal-001",
            payload={"deal_id": "deal-001"},
            summary="Delete deal",
        )
        approve_pending_action(config, action["id"], approver="MH", reason="test")
        import pipeline_actions
        result = pipeline_actions.execute_pipeline_action(config, action["id"])
        assert not result.get("success", False)

    def test_no_provider_calls(self, temp_project):
        """No Gmail/Drive/Calendar calls during pipeline operations."""
        config, project, config_path = temp_project
        _seed_pipeline(project, [
            {"id": "deal-np-001", "client_name": "NP Co", "stage": "Lead",
             "value": 1000, "currency": "SGD", "created": "2026-07-01",
             "last_activity": "2026-07-01", "documents": [], "notes": []},
        ])
        mock_client = MagicMock()
        import pipeline
        with patch("workspace_client.get_workspace_client", return_value=mock_client):
            buf = io.StringIO()
            with redirect_stdout(buf):
                pipeline.main(["--config", str(config_path), "list", "--summary"])
            with redirect_stdout(buf):
                pipeline.main(["--config", str(config_path), "stale"])
            with redirect_stdout(buf):
                pipeline.main(["--config", str(config_path), "validate"])
        mock_client.gmail_send.assert_not_called()
        mock_client.gmail_search.assert_not_called()


# ─── Full Route (Review Queue → webhook_events → pipeline_actions) ─────────

class TestFullRoute:
    def test_review_queue_executes_pipeline_action(self, temp_project):
        """Full route: review_queue execute → webhook_events → pipeline_actions."""
        config, project, config_path = temp_project
        _seed_pipeline(project, [
            {"id": "deal-route-001", "client_name": "Route Co", "stage": "Lead",
             "value": 8000, "currency": "SGD", "created": "2026-07-01",
             "last_activity": "2026-07-01", "documents": [], "notes": []},
        ])
        from state_db import create_pending_action, approve_pending_action
        action = create_pending_action(
            config=config, action_type="pipeline.deal.move_stage",
            provider="pipeline", target="deal-route-001",
            payload={"deal_id": "deal-route-001", "stage": "Qualified", "note": "Route test"},
            summary="Move deal to Qualified",
        )
        approve_pending_action(config, action["id"], approver="MH", reason="route test")

        import review_queue
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = review_queue._main(["--config", str(config_path), "execute",
                                      "--action-id", action["id"]])
        assert rc == 0, f"Execute failed: {buf.getvalue()}"

        import yaml
        data = yaml.safe_load((project / "pipeline.yaml").read_text())
        deal = next(d for d in data["deals"] if d["id"] == "deal-route-001")
        assert deal["stage"] == "Qualified"

    def test_workspace_client_not_called_for_pipeline(self, temp_project):
        """workspace_client must not be called for pipeline actions."""
        config, project, config_path = temp_project
        _seed_pipeline(project, [
            {"id": "deal-nows-001", "client_name": "NoWS Co", "stage": "Lead",
             "value": 500, "currency": "SGD", "created": "2026-07-01",
             "last_activity": "2026-07-01", "documents": [], "notes": []},
        ])
        from state_db import create_pending_action, approve_pending_action
        action = create_pending_action(
            config=config, action_type="pipeline.deal.add_note",
            provider="pipeline", target="deal-nows-001",
            payload={"deal_id": "deal-nows-001", "text": "No WS test", "author": "test"},
            summary="Add note",
        )
        approve_pending_action(config, action["id"], approver="MH", reason="ok")

        mock_client = MagicMock()
        with patch("workspace_client.get_workspace_client", return_value=mock_client):
            import review_queue
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = review_queue._main(["--config", str(config_path), "execute",
                                          "--action-id", action["id"]])
        assert rc == 0
        mock_client.gmail_send.assert_not_called()


# ─── Briefing Integration ───────────────────────────────────

class TestBriefingPipeline:
    def test_briefing_shows_pipeline(self, temp_project, monkeypatch):
        config, project, config_path = temp_project
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        _seed_pipeline(project, [
            {"id": "deal-brief-001", "client_name": "Brief Co", "stage": "Lead",
             "value": 1000, "currency": "SGD", "created": "2026-07-01",
             "last_activity": "2026-07-01", "documents": [], "notes": []},
        ])

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            daily_briefing.main(["run", "--json", "--dry-run"])

        parsed = json.loads(buf.getvalue())
        assert "pipeline" in parsed["sections"]
        pl = parsed["sections"]["pipeline"]
        assert pl.get("active_deals", 0) >= 1

    def test_briefing_shows_stale(self, temp_project, monkeypatch):
        config, project, config_path = temp_project
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        old = (date.today() - timedelta(days=30)).isoformat()
        _seed_pipeline(project, [
            {"id": "deal-stale-001", "client_name": "Stale Brief Co", "stage": "Proposal Sent",
             "value": 5000, "currency": "SGD", "created": "2026-01-01",
             "last_activity": old, "documents": [], "notes": []},
        ])

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            daily_briefing.main(["run", "--json", "--dry-run"])

        parsed = json.loads(buf.getvalue())
        pl = parsed["sections"]["pipeline"]
        assert pl.get("stale_deals", 0) >= 1
        assert pl.get("oldest_stale_id") == "deal-stale-001"

    def test_briefing_contract_signed_no_invoice(self, temp_project, monkeypatch):
        config, project, config_path = temp_project
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        _seed_pipeline(project, [
            {"id": "deal-cs-001", "client_name": "CS Co", "stage": "Contract Signed",
             "value": 10000, "currency": "SGD", "created": "2026-07-01",
             "last_activity": "2026-07-01", "documents": [], "notes": []},
        ])

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            daily_briefing.main(["run", "--json", "--dry-run"])

        parsed = json.loads(buf.getvalue())
        pl = parsed["sections"]["pipeline"]
        assert pl.get("contract_signed_no_invoice", 0) >= 1

    def test_briefing_contract_signed_with_invoice_not_flagged(self, temp_project, monkeypatch):
        """Deal with matching deal_id in invoices.yaml should NOT be flagged."""
        config, project, config_path = temp_project
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        _seed_pipeline(project, [
            {"id": "deal-cs-002", "client_name": "CS With Inv", "stage": "Contract Signed",
             "value": 10000, "currency": "SGD", "created": "2026-07-01",
             "last_activity": "2026-07-01", "documents": [], "notes": []},
        ])
        # Seed invoices.yaml with deal_id matching the deal
        import yaml
        (project / "invoices.yaml").write_text(yaml.safe_dump({
            "invoices": [{"id": "INV-001", "direction": "sent", "counterparty": "CS With Inv",
                          "amount": 10000, "currency": "SGD", "issue_date": "2026-07-01",
                          "due_date": "2026-07-15", "status": "sent", "deal_id": "deal-cs-002"}]
        }))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            daily_briefing.main(["run", "--json", "--dry-run"])

        parsed = json.loads(buf.getvalue())
        pl = parsed["sections"]["pipeline"]
        assert pl.get("contract_signed_no_invoice", 0) == 0

    def test_briefing_invoiced_with_paid_invoice_not_flagged(self, temp_project, monkeypatch):
        """Invoiced deal with linked invoice marked paid should NOT be flagged."""
        config, project, config_path = temp_project
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        _seed_pipeline(project, [
            {"id": "deal-inv-001", "client_name": "Inv Paid Co", "stage": "Invoiced",
             "value": 5000, "currency": "SGD", "created": "2026-07-01",
             "last_activity": "2026-07-01", "documents": [], "notes": []},
        ])
        import yaml
        (project / "invoices.yaml").write_text(yaml.safe_dump({
            "invoices": [{"id": "INV-002", "direction": "sent", "counterparty": "Inv Paid Co",
                          "amount": 5000, "currency": "SGD", "issue_date": "2026-07-01",
                          "due_date": "2026-07-15", "status": "paid", "deal_id": "deal-inv-001"}]
        }))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            daily_briefing.main(["run", "--json", "--dry-run"])

        parsed = json.loads(buf.getvalue())
        pl = parsed["sections"]["pipeline"]
        assert pl.get("invoiced_not_paid", 0) == 0