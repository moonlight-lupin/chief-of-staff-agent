#!/usr/bin/env python3
"""Tests for v0.2.0.1 — execution and authentication hotfix.

Tests all blocking issues from v0.2.0 review:
1. Pub/Sub OIDC JWT validation (valid, invalid, missing, wrong audience/SA)
2. Channel token fail-closed (no token configured = rejected)
3. Payload validation (malformed Gmail, missing Calendar/Drive headers)
4. Execution: check result before mark_executed
5. mark_failed receives error string
6. Provider method signatures correct (calendar_create, drive_upload, drive_download)
7. gmail.draft rejected before mark_executing
8. Guardrail context established (env vars set)
9. Drive state mapping (add/remove/update/trash/untrash/change/sync)
10. Failure paths: provider success=False, provider exception, unsupported, unknown action
"""
import sys
import os
import json
import io
import base64
import hashlib
import hmac
import time
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


TEST_SECRET = "test-webhook-secret-key-1234567890"


@pytest.fixture
def temp_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = {
        "google": {"delegate_email": "test@test.com", "account_alias": "test",
                   "domain": "phronesis-applied.com"},
        "integrations": {"workspace": {"provider": "google_api"}},
        "paths": {"project_root": str(project)},
    }
    return config, project


@pytest.fixture
def with_secret(temp_project, monkeypatch):
    monkeypatch.setenv("CHIEF_OF_STAFF_WEBHOOK_SECRET", TEST_SECRET)
    return temp_project


@pytest.fixture
def with_pubsub(with_secret, monkeypatch):
    monkeypatch.setenv("CHIEF_OF_STAFF_PUBSUB_AUDIENCE", "https://myapp.example.com/webhooks/gmail")
    monkeypatch.setenv("CHIEF_OF_STAFF_PUBSUB_SERVICE_ACCOUNT", "pubsub@my-project.iam.gserviceaccount.com")
    return with_secret


@pytest.fixture
def with_token(with_pubsub, monkeypatch):
    monkeypatch.setenv("CHIEF_OF_STAFF_WEBHOOK_CHANNEL_TOKEN", "my-channel-token")
    return with_pubsub


def _make_jwt(iss="https://accounts.google.com", aud="https://myapp.example.com/webhooks/gmail",
              email="pubsub@my-project.iam.gserviceaccount.com", email_verified=True):
    """Create a fake JWT (unsigned, for testing claims only)."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = {"iss": iss, "aud": aud, "email": email, "email_verified": email_verified}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(b"").decode().rstrip("=")
    return f"{header}.{payload_b64}.{sig}"


def _mock_verify_token(claims=None, side_effect=None):
    """Mock google.oauth2.id_token.verify_oauth2_token.

    If side_effect is set, it simulates a verification failure.
    Otherwise returns the claims dict (simulating successful verification).
    """
    if side_effect:
        return patch("google.oauth2.id_token.verify_oauth2_token", side_effect=side_effect)
    return patch("google.oauth2.id_token.verify_oauth2_token", return_value=claims or {})


def _make_pubsub_payload(email="test@x.com", history_id="12345", message_id="msg-001"):
    inner = json.dumps({"emailAddress": email, "historyId": history_id}).encode()
    encoded = base64.urlsafe_b64encode(inner).decode().rstrip("=")
    return {"message": {"data": encoded, "messageId": message_id, "publishTime": "2026-07-10T12:00:00Z"},
            "subscription": "projects/test/subscriptions/gmail-push"}


# ─── Pub/Sub OIDC JWT Validation ─────────────────────────────

class TestPubSubOIDC:
    def test_valid_jwt(self, with_pubsub):
        from webhook_validation import verify_pubsub_oidc
        jwt = _make_jwt()
        valid_claims = {"email": "pubsub@my-project.iam.gserviceaccount.com",
                        "email_verified": True, "iss": "https://accounts.google.com",
                        "aud": "https://myapp.example.com/webhooks/gmail"}
        with _mock_verify_token(valid_claims):
            ok, reason = verify_pubsub_oidc(f"Bearer {jwt}")
        assert ok
        assert reason == "OK"

    def test_missing_auth_header(self, with_pubsub):
        from webhook_validation import verify_pubsub_oidc
        ok, reason = verify_pubsub_oidc(None)
        assert not ok
        assert "Missing" in reason

    def test_not_bearer(self, with_pubsub):
        from webhook_validation import verify_pubsub_oidc
        ok, reason = verify_pubsub_oidc("Basic abc123")
        assert not ok
        assert "Bearer" in reason

    def test_wrong_issuer(self, with_pubsub):
        from webhook_validation import verify_pubsub_oidc
        # Google verifies signature OK, but issuer is wrong
        claims = {"email": "pubsub@my-project.iam.gserviceaccount.com",
                  "email_verified": True, "iss": "https://evil.com",
                  "aud": "https://myapp.example.com/webhooks/gmail"}
        with _mock_verify_token(claims):
            ok, reason = verify_pubsub_oidc("Bearer valid.signed.token")
        assert not ok
        assert "issuer" in reason.lower()

    def test_wrong_audience(self, with_pubsub):
        from webhook_validation import verify_pubsub_oidc
        # Google rejects wrong audience at signature verification
        with _mock_verify_token(side_effect=ValueError("Wrong audience")):
            ok, reason = verify_pubsub_oidc("Bearer valid.signed.token")
        assert not ok
        assert "JWT verification failed" in reason

    def test_wrong_service_account(self, with_pubsub):
        from webhook_validation import verify_pubsub_oidc
        claims = {"email": "evil@attacker.com", "email_verified": True,
                  "iss": "https://accounts.google.com",
                  "aud": "https://myapp.example.com/webhooks/gmail"}
        with _mock_verify_token(claims):
            ok, reason = verify_pubsub_oidc("Bearer valid.signed.token")
        assert not ok
        assert "service account" in reason.lower()

    def test_email_not_verified(self, with_pubsub):
        from webhook_validation import verify_pubsub_oidc
        claims = {"email": "pubsub@my-project.iam.gserviceaccount.com",
                  "email_verified": False, "iss": "https://accounts.google.com",
                  "aud": "https://myapp.example.com/webhooks/gmail"}
        with _mock_verify_token(claims):
            ok, reason = verify_pubsub_oidc("Bearer valid.signed.token")
        assert not ok
        assert "not verified" in reason

    def test_missing_audience_config(self, with_pubsub, monkeypatch):
        monkeypatch.setenv("CHIEF_OF_STAFF_PUBSUB_SERVICE_ACCOUNT", "pubsub@my-project.iam.gserviceaccount.com")
        monkeypatch.delenv("CHIEF_OF_STAFF_PUBSUB_AUDIENCE", raising=False)
        from webhook_validation import verify_pubsub_oidc
        jwt = _make_jwt()
        ok, reason = verify_pubsub_oidc(f"Bearer {jwt}")
        assert not ok
        assert "configuration incomplete" in reason.lower()

    def test_missing_sa_config(self, with_pubsub, monkeypatch):
        monkeypatch.setenv("CHIEF_OF_STAFF_PUBSUB_AUDIENCE", "https://myapp.example.com/webhooks/gmail")
        monkeypatch.delenv("CHIEF_OF_STAFF_PUBSUB_SERVICE_ACCOUNT", raising=False)
        from webhook_validation import verify_pubsub_oidc
        jwt = _make_jwt()
        ok, reason = verify_pubsub_oidc(f"Bearer {jwt}")
        assert not ok
        assert "configuration incomplete" in reason.lower()


# ─── Channel Token Fail-Closed ───────────────────────────────

class TestChannelTokenFailClosed:
    def test_no_token_configured_rejected(self, with_secret):
        """When channel token is NOT configured, verify returns False (fail-closed)."""
        from webhook_validation import verify_channel_token
        assert verify_channel_token("anything") is False
        assert verify_channel_token(None) is False

    def test_valid_token_accepted(self, with_token):
        from webhook_validation import verify_channel_token
        assert verify_channel_token("my-channel-token") is True

    def test_wrong_token_rejected(self, with_token):
        from webhook_validation import verify_channel_token
        assert verify_channel_token("wrong") is False

    def test_missing_token_rejected(self, with_token):
        from webhook_validation import verify_channel_token
        assert verify_channel_token(None) is False


# ─── Payload Validation ─────────────────────────────────────

class TestPayloadValidation:
    def test_valid_pubsub_payload(self):
        from webhook_validation import validate_gmail_pubsub_payload
        payload = _make_pubsub_payload()
        ok, reason, data = validate_gmail_pubsub_payload(payload)
        assert ok
        assert data["emailAddress"] == "test@x.com"

    def test_missing_message_object(self):
        from webhook_validation import validate_gmail_pubsub_payload
        ok, reason, data = validate_gmail_pubsub_payload({"subscription": "x"})
        assert not ok
        assert "message" in reason

    def test_missing_data_field(self):
        from webhook_validation import validate_gmail_pubsub_payload
        ok, reason, data = validate_gmail_pubsub_payload({"message": {"messageId": "1"}})
        assert not ok
        assert "data" in reason

    def test_missing_message_id(self):
        from webhook_validation import validate_gmail_pubsub_payload
        ok, reason, data = validate_gmail_pubsub_payload({"message": {"data": "d"}})
        assert not ok
        assert "messageId" in reason

    def test_malformed_base64_rejected(self):
        from webhook_validation import validate_gmail_pubsub_payload
        ok, reason, data = validate_gmail_pubsub_payload(
            {"message": {"data": "!!!invalid!!!", "messageId": "1"}})
        assert not ok
        assert "decode" in reason.lower()

    def test_missing_emailAddress_in_decoded(self):
        from webhook_validation import validate_gmail_pubsub_payload
        inner = json.dumps({"historyId": "123"}).encode()
        encoded = base64.urlsafe_b64encode(inner).decode().rstrip("=")
        ok, reason, data = validate_gmail_pubsub_payload(
            {"message": {"data": encoded, "messageId": "1"}})
        assert not ok
        assert "emailAddress" in reason

    def test_calendar_missing_headers_rejected(self):
        from webhook_validation import validate_calendar_headers
        ok, reason = validate_calendar_headers({})
        assert not ok
        assert "X-Goog-Channel-ID" in reason

    def test_drive_missing_headers_rejected(self):
        from webhook_validation import validate_drive_headers
        ok, reason = validate_drive_headers({})
        assert not ok
        assert "X-Goog-Channel-ID" in reason

    def test_adapter_raises_on_malformed_pubsub(self):
        from webhook_adapters import adapt_gmail_pubsub
        with pytest.raises(ValueError):
            adapt_gmail_pubsub({"message": {"data": "!!!invalid!!!"}})

    def test_adapter_raises_on_missing_calendar_headers(self):
        from webhook_adapters import adapt_calendar_headers
        with pytest.raises(ValueError):
            adapt_calendar_headers({})


# ─── Drive State Mapping ────────────────────────────────────

class TestDriveStateMapping:
    @pytest.mark.parametrize("state,event_type", [
        ("add", "document_added"),
        ("remove", "document_removed"),
        ("update", "document_updated"),
        ("trash", "document_trashed"),
        ("untrash", "document_restored"),
        ("change", "drive_changed"),
        ("sync", "drive_sync"),
        ("not_exists", "document_deleted"),
    ])
    def test_state_mapping(self, state, event_type):
        from webhook_adapters import adapt_drive_headers, DRIVE_STATE_MAP
        headers = {
            "X-Goog-Channel-ID": "ch-1",
            "X-Goog-Message-Number": "1",
            "X-Goog-Resource-ID": "r-1",
            "X-Goog-Resource-State": state,
        }
        result = adapt_drive_headers(headers)
        assert result["event_type"] == event_type

    def test_unknown_state_defaults_to_shared(self):
        from webhook_adapters import adapt_drive_headers
        headers = {
            "X-Goog-Channel-ID": "ch-1",
            "X-Goog-Message-Number": "1",
            "X-Goog-Resource-ID": "r-1",
            "X-Goog-Resource-State": "unknown_state",
        }
        result = adapt_drive_headers(headers)
        assert result["event_type"] == "document_shared"


# ─── Execution Failure Paths ────────────────────────────────

class TestExecutionFailurePaths:
    """Test all failure paths in the generic executor."""

    def _create_and_approve(self, config, action_type, payload, mock_client):
        from state_db import create_pending_action, approve_pending_action
        action = create_pending_action(
            config=config, action_type=action_type, provider="google_api",
            target="test", payload=payload, summary=f"Test {action_type}",
        )
        approve_pending_action(config, action["id"], approver="MH", reason="test")
        return action["id"]

    def test_provider_returns_failure(self, with_secret):
        """Provider success=False → mark_failed, NOT mark_executed."""
        config, project = with_secret
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda a: True
        mock_client.mail_tag.return_value = {"success": False, "error": "label not found"}
        action_id = self._create_and_approve(config, "gmail.label",
            {"message_id": "m1", "label_id": "L1"}, mock_client)

        import webhook_events
        with patch("webhook_events.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = webhook_events.main(["--summary", "execute", "--action-id", action_id])
        assert rc == 1
        assert "❌ Provider returned error" in buf.getvalue()

        from state_db import get_pending_action
        action = get_pending_action(config, action_id)
        assert action["state"] == "approved"  # failed → back to approved for retry
        assert "last_error" in action

    def test_provider_exception(self, with_secret):
        """Provider raises exception → mark_failed with error string."""
        config, project = with_secret
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda a: True
        mock_client.mail_send.side_effect = Exception("network timeout")
        action_id = self._create_and_approve(config, "gmail.send",
            {"to": "x@y.com", "subject": "test", "body": "test"}, mock_client)

        import webhook_events
        with patch("webhook_events.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            rc = webhook_events.main(["--summary", "execute", "--action-id", action_id])
        assert rc == 1

        from state_db import get_pending_action
        action = get_pending_action(config, action_id)
        assert action["state"] == "approved"  # failed → approved for retry

    def test_unsupported_capability(self, with_secret):
        """Unsupported action → mark_failed with error string."""
        config, project = with_secret
        mock_client = MagicMock()
        mock_client.provider_name = "composio:mcp"
        mock_client.supports.side_effect = lambda a: a != "gmail.label"
        action_id = self._create_and_approve(config, "gmail.label",
            {"message_id": "m1", "label_id": "L1"}, mock_client)

        import webhook_events
        with patch("webhook_events.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client):
            rc = webhook_events.main(["--summary", "execute", "--action-id", action_id])
        assert rc == 1

        from state_db import get_pending_action
        action = get_pending_action(config, action_id)
        assert action["state"] == "approved"  # failed → approved

    def test_gmail_draft_rejected(self, with_secret):
        """gmail.draft → rejected before mark_executing."""
        config, project = with_secret
        action_id = self._create_and_approve(config, "gmail.draft",
            {"to": "x@y.com", "subject": "test", "body": "test"}, MagicMock())

        import webhook_events
        with patch("webhook_events.load_config", return_value=config):
            rc = webhook_events.main(["--summary", "execute", "--action-id", action_id])
        assert rc == 1

        from state_db import get_pending_action
        action = get_pending_action(config, action_id)
        assert action["state"] == "approved"  # still approved, not executed

    def test_successful_execution_stays_executed(self, with_secret):
        """Successful execution → state=executed (not approved)."""
        config, project = with_secret
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda a: True
        mock_client.mail_tag.return_value = {"success": True, "action": "gmail.label"}
        action_id = self._create_and_approve(config, "gmail.label",
            {"message_id": "m1", "label_id": "L1"}, mock_client)

        import webhook_events
        with patch("webhook_events.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            rc = webhook_events.main(["--summary", "execute", "--action-id", action_id])
        assert rc == 0

        from state_db import get_pending_action
        action = get_pending_action(config, action_id)
        assert action["state"] == "executed"

    def test_guardrail_env_set_during_execution(self, with_secret):
        """Guardrail env vars set during provider call, restored after."""
        config, project = with_secret
        os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)
        os.environ.pop("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", None)
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda a: True
        mock_client.mail_send.return_value = {"success": True}

        # Capture env vars during the provider call
        captured = {}
        def capture_send(**kwargs):
            captured["auto"] = os.environ.get("CHIEF_OF_STAFF_AUTO_APPROVE")
            captured["destructive"] = os.environ.get("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE")
            return {"success": True}
        mock_client.mail_send.side_effect = capture_send

        action_id = self._create_and_approve(config, "gmail.send",
            {"to": "x@y.com", "subject": "test", "body": "test"}, mock_client)

        import webhook_events
        with patch("webhook_events.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            webhook_events.main(["execute", "--action-id", action_id])

        # During execution, env vars were set
        assert captured["auto"] == "1"
        assert captured["destructive"] == "1"
        # After execution, they're restored (removed since they weren't set before)
        assert "CHIEF_OF_STAFF_AUTO_APPROVE" not in os.environ
        assert "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE" not in os.environ


# ─── Method Signature Tests ──────────────────────────────────

class TestMethodSignatures:
    def test_calendar_create_uses_title(self, with_secret):
        config, project = with_secret
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda a: True
        mock_client.calendar_create.return_value = {"success": True}
        from state_db import create_pending_action, approve_pending_action
        action = create_pending_action(
            config=config, action_type="calendar.create", provider="google_api",
            target="test", payload={"summary": "Meeting", "start": "2026-07-10T10:00:00", "end": "2026-07-10T11:00:00"},
            summary="Test",
        )
        approve_pending_action(config, action["id"], approver="MH", reason="test")
        import webhook_events
        with patch("webhook_events.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            webhook_events.main(["execute", "--action-id", action["id"]])
        mock_client.calendar_create.assert_called_once_with(
            title="Meeting", start="2026-07-10T10:00:00", end="2026-07-10T11:00:00")

    def test_drive_upload_uses_file_path(self, with_secret):
        config, project = with_secret
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda a: True
        mock_client.files_upload.return_value = {"success": True}
        from state_db import create_pending_action, approve_pending_action
        action = create_pending_action(
            config=config, action_type="drive.upload", provider="google_api",
            target="test", payload={"file_path": "/tmp/test.pdf", "parent_id": "folder-1"},
            summary="Test",
        )
        approve_pending_action(config, action["id"], approver="MH", reason="test")
        import webhook_events
        with patch("webhook_events.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            webhook_events.main(["execute", "--action-id", action["id"]])
        mock_client.files_upload.assert_called_once_with(
            file_path="/tmp/test.pdf", parent_id="folder-1")

    def test_drive_download_uses_output_path(self, with_secret):
        config, project = with_secret
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda a: True
        mock_client.files_download.return_value = {"success": True}
        from state_db import create_pending_action, approve_pending_action
        action = create_pending_action(
            config=config, action_type="drive.download", provider="google_api",
            target="test", payload={"file_id": "f1", "output_path": "/tmp/out.pdf"},
            summary="Test",
        )
        approve_pending_action(config, action["id"], approver="MH", reason="test")
        import webhook_events
        with patch("webhook_events.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            webhook_events.main(["execute", "--action-id", action["id"]])
        mock_client.files_download.assert_called_once_with(
            file_id="f1", output_path="/tmp/out.pdf")


# ─── Receiver OIDC Integration ──────────────────────────────

class TestReceiverOIDC:
    def test_gmail_with_valid_oidc(self, with_token):
        config, project = with_token
        from webhook_receiver import WebhookStats
        stats = WebhookStats()
        from webhook_receiver import create_handler
        handler_class = create_handler(config, stats)

        body = json.dumps(_make_pubsub_payload()).encode()
        jwt = _make_jwt()
        valid_claims = {"email": "pubsub@my-project.iam.gserviceaccount.com",
                        "email_verified": True, "iss": "https://accounts.google.com",
                        "aud": "https://myapp.example.com/webhooks/gmail"}

        handler = handler_class.__new__(handler_class)
        handler.path = "/webhooks/gmail"
        handler.headers = {"Authorization": f"Bearer {jwt}", "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()

        with _mock_verify_token(valid_claims), \
             patch.object(handler, "_respond") as mock_respond:
            handler.do_POST()

        status, data = mock_respond.call_args[0]
        assert status == 200
        assert data["status"] == "ingested"

    def test_gmail_with_invalid_oidc_rejected(self, with_token):
        config, project = with_token
        from webhook_receiver import WebhookStats, create_handler
        stats = WebhookStats()
        handler_class = create_handler(config, stats)

        body = json.dumps(_make_pubsub_payload()).encode()
        jwt = _make_jwt(aud="https://wrong.example.com")

        handler = handler_class.__new__(handler_class)
        handler.path = "/webhooks/gmail"
        handler.headers = {"Authorization": f"Bearer {jwt}", "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()

        with _mock_verify_token(side_effect=ValueError("Wrong audience")), \
             patch.object(handler, "_respond") as mock_respond:
            handler.do_POST()

        status, data = mock_respond.call_args[0]
        assert status == 401
        assert "OIDC" in data["error"]

    def test_calendar_without_token_rejected(self, with_pubsub):
        """When channel token not configured, Calendar endpoint rejects."""
        config, project = with_pubsub
        # Note: no CHIEF_OF_STAFF_WEBHOOK_CHANNEL_TOKEN set
        from webhook_receiver import WebhookStats, create_handler
        stats = WebhookStats()
        handler_class = create_handler(config, stats)

        handler = handler_class.__new__(handler_class)
        handler.path = "/webhooks/calendar"
        handler.headers = {"Content-Length": "0"}
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()

        with patch.object(handler, "_respond") as mock_respond:
            handler.do_POST()

        status, data = mock_respond.call_args[0]
        assert status == 401
        assert "channel token" in data["error"].lower()

    def test_malformed_pubsub_returns_400(self, with_token):
        config, project = with_token
        from webhook_receiver import WebhookStats, create_handler
        stats = WebhookStats()
        handler_class = create_handler(config, stats)

        body = json.dumps({"message": {"data": "!!!invalid!!!"}}).encode()
        jwt = _make_jwt()
        valid_claims = {"email": "pubsub@my-project.iam.gserviceaccount.com",
                        "email_verified": True, "iss": "https://accounts.google.com",
                        "aud": "https://myapp.example.com/webhooks/gmail"}

        handler = handler_class.__new__(handler_class)
        handler.path = "/webhooks/gmail"
        handler.headers = {"Authorization": f"Bearer {jwt}", "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()

        with _mock_verify_token(valid_claims), \
             patch.object(handler, "_respond") as mock_respond:
            handler.do_POST()

        status, data = mock_respond.call_args[0]
        assert status == 400
        assert "Invalid payload" in data["error"]


# ─── validate_secret_config ─────────────────────────────────

class TestValidateSecretConfig:
    def test_all_configured(self, with_token):
        from webhook_validation import validate_secret_config
        result = validate_secret_config()
        assert result["valid"] is True
        assert result["endpoints"]["gmail"] == "native (OIDC)"
        assert result["endpoints"]["calendar"] == "enabled"
        assert result["endpoints"]["drive"] == "enabled"
        assert result["endpoints"]["generic"] == "enabled"

    def test_no_channel_token(self, with_pubsub):
        from webhook_validation import validate_secret_config
        result = validate_secret_config()
        assert result["valid"] is False
        assert result["endpoints"]["calendar"] == "disabled"
        assert result["endpoints"]["drive"] == "disabled"
        assert result["endpoints"]["gmail"] == "native (OIDC)"

    def test_nothing_configured(self, temp_project, monkeypatch):
        monkeypatch.delenv("CHIEF_OF_STAFF_WEBHOOK_SECRET", raising=False)
        monkeypatch.delenv("CHIEF_OF_STAFF_PUBSUB_AUDIENCE", raising=False)
        monkeypatch.delenv("CHIEF_OF_STAFF_PUBSUB_SERVICE_ACCOUNT", raising=False)
        monkeypatch.delenv("CHIEF_OF_STAFF_WEBHOOK_CHANNEL_TOKEN", raising=False)
        from webhook_validation import validate_secret_config
        result = validate_secret_config()
        assert result["valid"] is False
        assert all(s == "disabled" for s in result["endpoints"].values())