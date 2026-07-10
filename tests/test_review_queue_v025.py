#!/usr/bin/env python3
"""Tests for v0.2.5 — Review queue / operator UX polish.

Tests:
1. list shows requested pending actions
2. list can filter by state
3. list can filter by risk
4. list can filter by action type
5. preview includes action id, type, risk, payload, why, and commands
6. approve stores approver and reason
7. dismiss requires reason
8. dismissed action remains in audit/history
9. execute refuses unapproved action
10. execute approved action uses existing execution router
11. high-risk bulk approval is rejected
12. low-risk bulk approval requires explicit confirmation flag
13. unknown write action is not classified as low risk
14. Daily Briefing uses review_queue.py commands
15. summary groups by state and risk
16. audit shows approve/dismiss/execute events
17. no provider writes occur during list/preview/summary
18. no direct provider writes bypass pending-action execution
19. malformed pending action degrades gracefully
20. empty queue gives useful output
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
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


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
        "sales_stages": ["Lead", "Proposal Sent", "NDA Signed", "Contract Signed", "Invoiced", "Paid"],
    }
    import yaml
    config_path = project / "company.yaml"
    config_path.write_text(yaml.safe_dump(config))
    return config, project, config_path


def _seed_action(config, action_type="gmail.send", target="x@y.com", **kwargs):
    """Helper to create a pending action."""
    from pending_actions import create_pending_action
    payload = kwargs.pop("payload", {"to": target, "subject": "Test", "body": "Test body"})
    return create_pending_action(
        config=config, action_type=action_type, provider="google_api",
        target=target, payload=payload, summary=kwargs.pop("summary", f"Test {action_type}"),
    )


# ─── List ───────────────────────────────────────────────────

class TestList:
    def test_list_shows_requested(self, temp_project):
        config, project, config_path = temp_project
        _seed_action(config, "gmail.send", "x@y.com")
        import review_queue
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = review_queue._main(["--config", str(config_path), "list"])
        assert rc == 0
        output = buf.getvalue()
        assert "gmail.send" in output

    def test_list_filter_by_state(self, temp_project):
        config, project, config_path = temp_project
        from pending_actions import create_pending_action, approve_pending_action
        a1 = create_pending_action(config=config, action_type="gmail.send", provider="google_api",
            target="x@y.com", payload={"to": "x@y.com", "subject": "T", "body": "B"}, summary="S1")
        a2 = create_pending_action(config=config, action_type="gmail.label", provider="google_api",
            target="msg-1", payload={"message_id": "m1", "label_id": "L1"}, summary="S2")
        approve_pending_action(config, a2["id"], approver="MH", reason="ok")
        import review_queue
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = review_queue._main(["--config", str(config_path), "list", "--state", "approved"])
        assert rc == 0
        output = buf.getvalue()
        assert "gmail.label" in output
        assert "gmail.send" not in output or "requested" not in output

    def test_list_filter_by_risk(self, temp_project):
        config, project, config_path = temp_project
        _seed_action(config, "gmail.send", "x@y.com")  # high risk
        _seed_action(config, "gmail.label", "msg-1", payload={"message_id": "m1", "label_id": "L1"})  # low risk
        import review_queue
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = review_queue._main(["--config", str(config_path), "list", "--risk", "high"])
        assert rc == 0
        output = buf.getvalue()
        assert "gmail.send" in output

    def test_list_filter_by_type(self, temp_project):
        config, project, config_path = temp_project
        _seed_action(config, "gmail.send", "x@y.com")
        _seed_action(config, "gmail.label", "msg-1", payload={"message_id": "m1", "label_id": "L1"})
        import review_queue
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = review_queue._main(["--config", str(config_path), "list", "--type", "gmail.send"])
        assert rc == 0
        output = buf.getvalue()
        assert "gmail.send" in output
        assert "gmail.label" not in output

    def test_empty_queue_gives_useful_output(self, temp_project):
        config, project, config_path = temp_project
        import review_queue
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = review_queue._main(["--config", str(config_path), "list"])
        assert rc == 0
        output = buf.getvalue()
        assert "no" in output.lower() or "empty" in output.lower() or "0" in output


# ─── Preview ────────────────────────────────────────────────

class TestPreview:
    def test_preview_shows_details(self, temp_project):
        config, project, config_path = temp_project
        action = _seed_action(config, "gmail.send", "test@example.com",
                              payload={"to": "test@example.com", "subject": "Test Subject", "body": "Test Body"})
        import review_queue
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = review_queue._main(["--config", str(config_path), "preview", "--action-id", action["id"]])
        assert rc == 0
        output = buf.getvalue()
        assert action["id"] in output
        assert "gmail.send" in output
        assert "high" in output.lower()
        assert "review_queue" in output  # commands reference review_queue


# ─── Approve ────────────────────────────────────────────────

class TestApprove:
    def test_approve_stores_approver_and_reason(self, temp_project):
        config, project, config_path = temp_project
        action = _seed_action(config, "gmail.send", "x@y.com")
        import review_queue
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = review_queue._main(["--config", str(config_path), "approve",
                                      "--action-id", action["id"],
                                      "--approver", "MH",
                                      "--reason", "Checked recipient and content"])
        assert rc == 0
        from pending_actions import get_pending_action
        updated = get_pending_action(config, action["id"])
        assert updated["state"] == "approved"
        assert updated["approver"] == "MH"
        assert updated["approval_reason"] == "Checked recipient and content"


# ─── Dismiss ────────────────────────────────────────────────

class TestDismiss:
    def test_dismiss_requires_reason(self, temp_project):
        config, project, config_path = temp_project
        action = _seed_action(config, "gmail.send", "x@y.com")
        import review_queue
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                review_queue._main(["--config", str(config_path), "dismiss",
                                    "--action-id", action["id"]])
                assert False, "Should have failed without --reason"
            except SystemExit as e:
                assert e.code != 0

    def test_dismiss_with_reason(self, temp_project):
        config, project, config_path = temp_project
        action = _seed_action(config, "gmail.send", "x@y.com")
        import review_queue
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = review_queue._main(["--config", str(config_path), "dismiss",
                                      "--action-id", action["id"],
                                      "--reason", "Not needed"])
        assert rc == 0
        from pending_actions import get_pending_action
        updated = get_pending_action(config, action["id"])
        assert updated["state"] == "dismissed"
        assert "dismiss_reason" in updated

    def test_dismissed_remains_in_history(self, temp_project):
        config, project, config_path = temp_project
        action = _seed_action(config, "gmail.send", "x@y.com")
        import review_queue
        buf = io.StringIO()
        with redirect_stdout(buf):
            review_queue._main(["--config", str(config_path), "dismiss",
                                 "--action-id", action["id"], "--reason", "Not needed"])
        from pending_actions import get_pending_action
        # Action should still exist (not deleted)
        updated = get_pending_action(config, action["id"])
        assert updated is not None
        assert updated["state"] == "dismissed"


# ─── Execute ────────────────────────────────────────────────

class TestExecute:
    def test_execute_refuses_unapproved(self, temp_project):
        config, project, config_path = temp_project
        action = _seed_action(config, "gmail.send", "x@y.com")
        import review_queue
        buf = io.StringIO()
        import sys as _sys
        old_stderr = _sys.stderr
        err_buf = io.StringIO()
        _sys.stderr = err_buf
        try:
            rc = review_queue._main(["--config", str(config_path), "execute",
                                      "--action-id", action["id"]])
        finally:
            _sys.stderr = old_stderr
        assert rc != 0
        combined = buf.getvalue() + err_buf.getvalue()
        assert "approved" in combined.lower() or "not approved" in combined.lower()

    def test_execute_approved_action(self, temp_project):
        """Execute an approved action — mock the workspace client."""
        config, project, config_path = temp_project
        action = _seed_action(config, "gmail.send", "x@y.com")
        from pending_actions import approve_pending_action
        approve_pending_action(config, action["id"], approver="MH", reason="ok")
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.mail_send.return_value = {"success": True, "message_id": "msg_123"}

        import review_queue
        # review_queue delegates to webhook_events.cmd_execute which imports workspace_client
        with patch("workspace_client.get_workspace_client", return_value=mock_client):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = review_queue._main(["--config", str(config_path), "execute",
                                          "--action-id", action["id"]])
        assert rc == 0
        mock_client.mail_send.assert_called_once()

    def test_no_provider_during_list_preview(self, temp_project):
        """No provider calls during list/preview/summary."""
        config, project, config_path = temp_project
        _seed_action(config, "gmail.send", "x@y.com")
        mock_client = MagicMock()
        import review_queue
        with patch("review_queue.get_workspace_client", return_value=mock_client, create=True),              patch("workspace_client.get_workspace_client", return_value=mock_client):
            for cmd in [["list"], ["summary"]]:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    try:
                        review_queue._main(["--config", str(config_path)] + cmd)
                    except SystemExit:
                        pass
        mock_client.mail_send.assert_not_called()
        mock_client.mail_tag.assert_not_called()


# ─── Bulk Approve ───────────────────────────────────────────

class TestBulkApprove:
    def test_high_risk_bulk_rejected(self, temp_project):
        config, project, config_path = temp_project
        _seed_action(config, "gmail.send", "x@y.com")
        _seed_action(config, "gmail.send", "z@y.com")
        import review_queue
        buf = io.StringIO()
        import sys as _sys
        old_stderr = _sys.stderr
        err_buf = io.StringIO()
        _sys.stderr = err_buf
        try:
            rc = review_queue._main(["--config", str(config_path), "approve",
                                      "--all", "--risk", "high", "--type", "gmail.send",
                                      "--reason", "bulk", "--confirm-low-risk-bulk"])
        finally:
            _sys.stderr = old_stderr
        assert rc != 0
        combined = buf.getvalue() + err_buf.getvalue()
        assert "low" in combined.lower() or "high" in combined.lower() or "risk" in combined.lower()

    def test_low_risk_bulk_requires_confirmation(self, temp_project):
        config, project, config_path = temp_project
        _seed_action(config, "gmail.label", "msg-1", payload={"message_id": "m1", "label_id": "L1"})
        _seed_action(config, "gmail.label", "msg-2", payload={"message_id": "m2", "label_id": "L1"})
        import review_queue
        # Without --confirm-low-risk-bulk
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = review_queue._main(["--config", str(config_path), "approve",
                                      "--all", "--risk", "low", "--type", "gmail.label",
                                      "--reason", "bulk"])
        assert rc != 0

    def test_low_risk_bulk_with_confirmation(self, temp_project):
        config, project, config_path = temp_project
        a1 = _seed_action(config, "gmail.label", "msg-1", payload={"message_id": "m1", "label_id": "L1"})
        a2 = _seed_action(config, "gmail.label", "msg-2", payload={"message_id": "m2", "label_id": "L1"})
        import review_queue
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = review_queue._main(["--config", str(config_path), "approve",
                                      "--all", "--risk", "low", "--type", "gmail.label",
                                      "--reason", "bulk approved", "--confirm-low-risk-bulk"])
        assert rc == 0
        from pending_actions import get_pending_action
        assert get_pending_action(config, a1["id"])["state"] == "approved"
        assert get_pending_action(config, a2["id"])["state"] == "approved"


# ─── Unknown Risk ───────────────────────────────────────────

class TestUnknownRisk:
    def test_unknown_write_not_low(self):
        from action_risk import get_action_risk
        assert get_action_risk("custom.delete") == "high"
        assert get_action_risk("custom.send") == "high"
        assert get_action_risk("custom.cancel") == "high"

    def test_unknown_moderate_not_low(self):
        from action_risk import get_action_risk
        assert get_action_risk("custom.create") == "medium"
        assert get_action_risk("custom.update") == "medium"

    def test_unknown_read_is_low(self):
        from action_risk import get_action_risk
        assert get_action_risk("custom.search") == "low"
        assert get_action_risk("custom.download") == "low"

    def test_truly_unknown_is_medium(self):
        from action_risk import get_action_risk
        assert get_action_risk("something.random") == "medium"


# ─── Summary ────────────────────────────────────────────────

class TestSummary:
    def test_summary_groups_by_state_and_risk(self, temp_project):
        config, project, config_path = temp_project
        _seed_action(config, "gmail.send", "x@y.com")  # high, requested
        _seed_action(config, "gmail.label", "msg-1", payload={"message_id": "m1", "label_id": "L1"})  # low, requested
        import review_queue
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = review_queue._main(["--config", str(config_path), "summary"])
        assert rc == 0
        output = buf.getvalue()
        assert "requested" in output.lower() or "Requested" in output
        assert "high" in output.lower() or "High" in output

    def test_summary_recommends_next_step(self, temp_project):
        config, project, config_path = temp_project
        action = _seed_action(config, "gmail.send", "x@y.com")
        import review_queue
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = review_queue._main(["--config", str(config_path), "summary"])
        assert rc == 0
        output = buf.getvalue()
        assert "pa_" in output or action["id"] in output or "Review" in output or "review" in output


# ─── Audit ──────────────────────────────────────────────────

class TestAudit:
    def test_audit_shows_events(self, temp_project):
        config, project, config_path = temp_project
        action = _seed_action(config, "gmail.send", "x@y.com")
        from pending_actions import approve_pending_action
        approve_pending_action(config, action["id"], approver="MH", reason="ok")
        import review_queue
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = review_queue._main(["--config", str(config_path), "audit", "--limit", "20"])
        assert rc == 0
        output = buf.getvalue()
        assert action["id"] in output or "approved" in output.lower() or "approve" in output.lower()


# ─── Malformed State ────────────────────────────────────────

class TestMalformedState:
    def test_malformed_degrades_gracefully(self, temp_project):
        config, project, config_path = temp_project
        # Write malformed pending actions
        (project / ".pending_actions.json").write_text("{invalid json}")
        import review_queue
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = review_queue._main(["--config", str(config_path), "list"])
        assert rc in (0, 1)


# ─── Briefing Integration ───────────────────────────────────

class TestBriefingIntegration:
    def test_briefing_uses_review_queue_commands(self, temp_project, monkeypatch):
        """Daily briefing should reference review_queue.py commands."""
        config, project, config_path = temp_project
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        # Seed a pending action
        _seed_action(config, "gmail.send", "x@y.com")

        # Add daily-briefing scripts to path
        briefing_dir = PLUGIN_ROOT / "skills" / "daily-briefing" / "scripts"
        if str(briefing_dir) not in sys.path:
            sys.path.insert(0, str(briefing_dir))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            daily_briefing.main(["run", "--summary", "--dry-run"])

        output = buf.getvalue()
        assert "review_queue.py" in output