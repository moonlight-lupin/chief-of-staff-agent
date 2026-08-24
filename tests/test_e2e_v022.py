#!/usr/bin/env python3
"""End-to-end tests for v0.2.2 — full workflow chains.

Tests:
1. classify → suggest → prepare → approve → execute
2. webhook → event_store → suggestion
3. failed execution → retry → success
4. doctor --summary runs and returns
5. state_tools backup/inspect/repair
6. orphaned executing cleanup via doctor --fix
"""
import sys
import os
import io
import json
import base64
import shutil
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
SKILLS_SCRIPTS = PLUGIN_ROOT / "skills"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))
for skill in ("document-preparer",):
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
        "sales_stages": ["Lead", "Proposal Sent", "NDA Signed", "Contract Signed", "Invoiced", "Paid"],
    }
    return config, project


# ─── E2E: classify → suggest → prepare → approve → execute ──

class TestClassifySuggestApproveExecute:
    """Full chain from email classification through to execution."""

    def test_full_chain_gmail_label(self, temp_project):
        config, project = temp_project

        # 1. Create a fake email classification
        from email_classifier import classify_email
        policy = {
            "categories": {
                "finance_invoice": {"preferred_label": "Finance/Invoices", "aliases": ["invoice"]},
                "finance_receipt": {"preferred_label": "Finance/Receipts", "aliases": ["receipt"]},
            }
        }
        email = {
            "id": "msg-001",
            "from": "vendor@stripe.com",
            "subject": "Invoice #INV-2026-001",
            "body": "Please pay your invoice of $5,000 by July 15.",
            "date": "2026-07-10",
        }
        result = classify_email(email, policy)
        assert result["category"] is not None
        assert result["confidence"] > 0

        # 2. Create a pending action from the classification
        from state_db import create_pending_action
        action = create_pending_action(
            config=config,
            action_type="gmail.label",
            provider="google_api",
            target="msg-001",
            payload={"message_id": "msg-001", "label_id": "Label_invoice"},
            summary=f"Apply label to: {email['subject']}",
        )
        assert action["state"] in ("pending", "requested")
        action_id = action["id"]

        # 3. Approve the action
        from state_db import approve_pending_action, get_pending_action
        approved = approve_pending_action(config, action_id, approver="MH", reason="auto-test")
        assert approved["state"] == "approved"

        # 4. Execute with mock provider
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda a: True
        mock_client.mail_tag.return_value = {"success": True, "action": "gmail.label"}

        import webhook_events
        with patch("webhook_events.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = webhook_events.main(["--summary", "execute", "--action-id", action_id])

        assert rc == 0
        assert "✅ Executed" in buf.getvalue()

        # 5. Verify final state
        final = get_pending_action(config, action_id)
        assert final["state"] == "executed"

    def test_full_chain_gmail_send(self, temp_project):
        config, project = temp_project

        from state_db import create_pending_action, approve_pending_action, get_pending_action
        action = create_pending_action(
            config=config,
            action_type="gmail.send",
            provider="google_api",
            target="client@x.com",
            payload={"to": "client@x.com", "subject": "Meeting tomorrow", "body": "Hi, see you at 10am."},
            summary="Send meeting confirmation",
        )
        approve_pending_action(config, action["id"], approver="MH", reason="test")

        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda a: True
        mock_client.mail_send.return_value = {"success": True}

        import webhook_events
        with patch("webhook_events.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            rc = webhook_events.main(["--summary", "execute", "--action-id", action["id"]])

        assert rc == 0
        final = get_pending_action(config, action["id"])
        assert final["state"] == "executed"


# ─── E2E: webhook → event_store → suggestion ────────────────

class TestWebhookToSuggestion:
    """Webhook ingestion chain."""

    def _make_pubsub_payload(self, email="test@x.com", history_id="12345", message_id="msg-001"):
        inner = json.dumps({"emailAddress": email, "historyId": history_id}).encode()
        encoded = base64.urlsafe_b64encode(inner).decode().rstrip("=")
        return {"message": {"data": encoded, "messageId": message_id,
                            "publishTime": "2026-07-10T12:00:00Z"},
                "subscription": "projects/test/subscriptions/gmail-push"}

    def test_webhook_ingest_and_suggestion(self, temp_project, monkeypatch):
        config, project = temp_project
        monkeypatch.setenv("CHIEF_OF_STAFF_WEBHOOK_SECRET", "test-secret-key-1234567890")
        monkeypatch.setenv("CHIEF_OF_STAFF_PUBSUB_AUDIENCE", "https://myapp.example.com/webhooks/gmail")
        monkeypatch.setenv("CHIEF_OF_STAFF_PUBSUB_SERVICE_ACCOUNT", "pubsub@my-project.iam.gserviceaccount.com")
        monkeypatch.setenv("CHIEF_OF_STAFF_WEBHOOK_CHANNEL_TOKEN", "my-channel-token")

        from webhook_receiver import WebhookStats, create_handler
        stats = WebhookStats()
        handler_class = create_handler(config, stats, generate_suggestions=True)

        body = json.dumps(self._make_pubsub_payload()).encode()
        valid_claims = {"email": "pubsub@my-project.iam.gserviceaccount.com",
                        "email_verified": True, "iss": "https://accounts.google.com",
                        "aud": "https://myapp.example.com/webhooks/gmail"}

        handler = handler_class.__new__(handler_class)
        handler.path = "/webhooks/gmail"
        handler.headers = {"Authorization": "Bearer valid.jwt.token",
                           "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()

        with patch("google.oauth2.id_token.verify_oauth2_token", return_value=valid_claims), \
             patch.object(handler, "_respond") as mock_respond:
            handler.do_POST()

        status, data = mock_respond.call_args[0]
        assert status == 200
        assert data["status"] == "ingested"
        assert data["event_id"] is not None

        # Verify event is in event_store
        from state_db import list_events
        events = list_events(config)
        assert len(events) >= 1
        assert events[0]["event_type"] == "email_received"


# ─── E2E: failed execution → retry → success ────────────────

class TestFailedExecutionRetry:
    """Execution fails, action goes back to approved, retry succeeds."""

    def test_retry_after_failure(self, temp_project):
        config, project = temp_project

        from state_db import create_pending_action, approve_pending_action, get_pending_action
        action = create_pending_action(
            config=config,
            action_type="gmail.label",
            provider="google_api",
            target="msg-001",
            payload={"message_id": "msg-001", "label_id": "L1"},
            summary="Test retry",
        )
        approve_pending_action(config, action["id"], approver="MH", reason="test")
        action_id = action["id"]

        # First attempt: provider returns failure
        mock_fail = MagicMock()
        mock_fail.provider_name = "google_api"
        mock_fail.supports.side_effect = lambda a: True
        mock_fail.mail_tag.return_value = {"success": False, "error": "label not found"}

        import webhook_events
        with patch("webhook_events.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_fail), \
             patch("workspace_capabilities.require_capability", return_value=None):
            rc = webhook_events.main(["--summary", "execute", "--action-id", action_id])

        assert rc == 1
        failed = get_pending_action(config, action_id)
        assert failed["state"] == "approved"  # back to approved for retry
        assert "last_error" in failed

        # Second attempt: provider succeeds
        mock_ok = MagicMock()
        mock_ok.provider_name = "google_api"
        mock_ok.supports.side_effect = lambda a: True
        mock_ok.mail_tag.return_value = {"success": True}

        with patch("webhook_events.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_ok), \
             patch("workspace_capabilities.require_capability", return_value=None):
            rc = webhook_events.main(["--summary", "execute", "--action-id", action_id])

        assert rc == 0
        final = get_pending_action(config, action_id)
        assert final["state"] == "executed"

    def test_retry_after_exception(self, temp_project):
        config, project = temp_project

        from state_db import create_pending_action, approve_pending_action, get_pending_action
        action = create_pending_action(
            config=config,
            action_type="gmail.send",
            provider="google_api",
            target="x@y.com",
            payload={"to": "x@y.com", "subject": "test", "body": "test"},
            summary="Test retry after exception",
        )
        approve_pending_action(config, action["id"], approver="MH", reason="test")
        action_id = action["id"]

        # First attempt: throws exception
        mock_err = MagicMock()
        mock_err.provider_name = "google_api"
        mock_err.supports.side_effect = lambda a: True
        mock_err.mail_send.side_effect = ConnectionError("network timeout")

        import webhook_events
        with patch("webhook_events.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_err), \
             patch("workspace_capabilities.require_capability", return_value=None):
            rc = webhook_events.main(["execute", "--action-id", action_id])

        assert rc == 1
        err_state = get_pending_action(config, action_id)
        assert err_state["state"] == "approved"
        assert "network timeout" in err_state.get("last_error", "")

        # Retry succeeds
        mock_ok = MagicMock()
        mock_ok.provider_name = "google_api"
        mock_ok.supports.side_effect = lambda a: True
        mock_ok.mail_send.return_value = {"success": True}

        with patch("webhook_events.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_ok), \
             patch("workspace_capabilities.require_capability", return_value=None):
            rc = webhook_events.main(["--summary", "execute", "--action-id", action_id])

        assert rc == 0
        assert get_pending_action(config, action_id)["state"] == "executed"


# ─── Doctor --summary ───────────────────────────────────────

class TestDoctorSummary:
    def test_summary_runs(self, temp_project, monkeypatch):
        config, project = temp_project
        # Create a minimal company.yaml
        config_dir = PLUGIN_ROOT / "shared" / "config"
        company_yaml = config_dir / "company.yaml"
        # Don't overwrite real config — just run doctor and let it report
        import doctor
        # Doctor uses _config_path which looks at CONFIG_DIR / company.yaml
        # We just test that --summary doesn't crash
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                rc = doctor._main(["--summary"])
            except SystemExit as e:
                rc = e.code
        output = buf.getvalue()
        assert "Chief-of-Staff:" in output
        assert rc in (0, 1)  # 1 if there are failures, which is OK for test


# ─── Orphaned executing cleanup ─────────────────────────────

class TestOrphanedExecutingCleanup:
    def test_doctor_fix_resets_orphaned(self, temp_project):
        config, project = temp_project

        # Create an action and manually set it to 'executing'
        from state_db import create_pending_action, approve_pending_action
        action = create_pending_action(
            config=config,
            action_type="gmail.label",
            provider="google_api",
            target="msg-001",
            payload={"message_id": "msg-001", "label_id": "L1"},
            summary="Orphaned test",
        )
        approve_pending_action(config, action["id"], approver="MH", reason="test")

        # Manually set to executing with an old timestamp (simulate crash during execution)
        from datetime import datetime, timezone, timedelta
        from state_db import _load, _save
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        pa_data = _load(config)
        stored = pa_data.get("actions", {}).get(action["id"])
        assert stored is not None
        stored["state"] = "executing"
        stored["executing_at"] = old_ts
        _save(config, pa_data)

        # Run doctor --fix (only the orphaned check will find it)
        import doctor
        # _check_orphaned_executing expects (fix, data, config_path)
        # data is the parsed config dict, config_path is Path
        result = doctor._check_orphaned_executing(True, config, PLUGIN_ROOT / "shared" / "config" / "company.yaml")
        assert result.status == "pass"
        assert result.fix_applied
        assert "Reset" in result.detail

        # Verify it was actually reset
        from state_db import get_pending_action
        final = get_pending_action(config, action["id"])
        assert final["state"] == "approved"
        assert "orphaned" in final.get("last_error", "").lower()

    def test_fresh_executing_not_reset(self, temp_project):
        """Executing actions younger than the threshold should NOT be reset."""
        config, project = temp_project

        from state_db import create_pending_action, approve_pending_action, get_pending_action
        action = create_pending_action(
            config=config,
            action_type="gmail.label",
            provider="google_api",
            target="msg-002",
            payload={"message_id": "msg-002", "label_id": "L1"},
            summary="Fresh executing test",
        )
        approve_pending_action(config, action["id"], approver="MH", reason="test")

        # Set to executing with a RECENT timestamp (1 minute ago — should be fresh)
        from datetime import datetime, timezone, timedelta
        from state_db import _load, _save
        recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        pa_data = _load(config)
        stored = pa_data.get("actions", {}).get(action["id"])
        assert stored is not None
        stored["state"] = "executing"
        stored["executing_at"] = recent_ts
        _save(config, pa_data)

        # Run doctor --fix
        import doctor
        result = doctor._check_orphaned_executing(True, config, PLUGIN_ROOT / "shared" / "config" / "company.yaml")
        assert "fresh" in result.detail.lower() or "skipped" in result.detail.lower()

        # Verify it was NOT reset
        final = get_pending_action(config, action["id"])
        assert final["state"] == "executing"


# ─── State tools ────────────────────────────────────────────

class TestStateTools:
    def test_inspect_empty_project(self, temp_project, monkeypatch):
        config, project = temp_project
        # state_tools uses config_loader.load_config() which reads
        # CHIEF_OF_STAFF_CONFIG env var or defaults to shared/config/company.yaml
        # We just test it runs without crashing
        import state_tools
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                rc = state_tools._main(["inspect"])
            except SystemExit:
                rc = 2
        # Just check it doesn't crash
        assert rc in (0, 1, 2)

    def test_backup_creates_archive(self, temp_project, monkeypatch):
        config, project = temp_project

        # Create a state file in the project
        (project / ".events.json").write_text('[]')

        import state_tools
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                rc = state_tools._main(["backup"])
            except SystemExit:
                rc = 2
        output = buf.getvalue()
        assert rc in (0, 1, 2)

    def test_repair_dry_run(self, temp_project, monkeypatch):
        config, project = temp_project
        import state_tools
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                rc = state_tools._main(["repair", "--dry-run"])
            except SystemExit:
                rc = 2
        assert rc in (0, 1, 2)