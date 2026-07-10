#!/usr/bin/env python3
"""Tests for v0.2.0 — provider-native webhooks and operational hardening.

Tests:
1. Gmail Pub/Sub envelope decoding (base64url message.data)
2. Calendar X-Goog-* header adaptation
3. Drive X-Goog-* header adaptation
4. Delivery-ID-based replay (not body-signature-based)
5. Reservation-before-ingest (release on failure)
6. Request size limit (413 on oversized)
7. Provider-specific endpoints
8. X-Goog-Channel-Token validation
9. Exception handling (500 on adapter/ingestion failure)
10. Source IDs use delivery IDs (not resource_id + state)
11. CLI event_type display fix
12. Generic approve/execute routing
13. Safety: no mutations during webhook processing
"""
import sys
import os
import json
import io
import base64
import time
import hashlib
import hmac
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock

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
def with_token(with_secret, monkeypatch):
    monkeypatch.setenv("CHIEF_OF_STAFF_WEBHOOK_CHANNEL_TOKEN", "my-channel-token")
    return with_secret


def _make_pubsub_payload(email="test@x.com", history_id="12345", message_id="msg-001"):
    """Create a realistic Gmail Pub/Sub push envelope."""
    inner = json.dumps({"emailAddress": email, "historyId": history_id}).encode()
    encoded = base64.urlsafe_b64encode(inner).decode().rstrip("=")
    return {
        "message": {
            "data": encoded,
            "messageId": message_id,
            "publishTime": "2026-07-10T12:00:00Z",
        },
        "subscription": "projects/test/subscriptions/gmail-push",
    }


def _make_calendar_headers(channel_id="ch-123", message_number="42",
                           resource_id="res-456", state="exists",
                           token=None, uri="https://www.googleapis.com/calendar/v3/"):
    headers = {
        "X-Goog-Channel-ID": channel_id,
        "X-Goog-Message-Number": message_number,
        "X-Goog-Resource-ID": resource_id,
        "X-Goog-Resource-State": state,
        "X-Goog-Resource-URI": uri,
    }
    if token:
        headers["X-Goog-Channel-Token"] = token
    return headers


def _make_drive_headers(channel_id="ch-789", message_number="99",
                        resource_id="res-012", state="exists",
                        token=None, uri="https://www.googleapis.com/drive/v3/"):
    headers = {
        "X-Goog-Channel-ID": channel_id,
        "X-Goog-Message-Number": message_number,
        "X-Goog-Resource-ID": resource_id,
        "X-Goog-Resource-State": state,
        "X-Goog-Resource-URI": uri,
    }
    if token:
        headers["X-Goog-Channel-Token"] = token
    return headers


# ─── Gmail Pub/Sub Adapter ───────────────────────────────────

class TestGmailPubSubAdapter:
    def test_decodes_envelope(self):
        from webhook_adapters import adapt_gmail_pubsub
        payload = _make_pubsub_payload()
        result = adapt_gmail_pubsub(payload)
        assert result["source"] == "webhook.gmail"
        assert result["source_id"] == "gmail-pubsub-msg-001"
        assert result["payload"]["email_address"] == "test@x.com"
        assert result["payload"]["history_id"] == "12345"
        assert result["delivery_id"] == "msg-001"

    def test_uses_message_id_for_dedup(self):
        from webhook_adapters import adapt_gmail_pubsub
        # Same email/history but different messageId → different source_id
        p1 = _make_pubsub_payload(message_id="msg-001")
        p2 = _make_pubsub_payload(message_id="msg-002")
        r1 = adapt_gmail_pubsub(p1)
        r2 = adapt_gmail_pubsub(p2)
        assert r1["source_id"] != r2["source_id"]
        assert r1["delivery_id"] != r2["delivery_id"]

    def test_handles_malformed_data(self):
        from webhook_adapters import adapt_gmail_pubsub
        from webhook_security import validate_gmail_pubsub_payload
        payload = {"message": {"data": "!!!invalid!!!", "messageId": "m1"}}
        # Validation should reject, not silently produce empty fields
        ok, reason, data = validate_gmail_pubsub_payload(payload)
        assert not ok
        assert "decode" in reason.lower()

    def test_empty_message(self):
        from webhook_security import validate_gmail_pubsub_payload
        payload = {"message": {}, "subscription": "x"}
        # Missing data field — should be rejected
        ok, reason, data = validate_gmail_pubsub_payload(payload)
        assert not ok


# ─── Calendar Header Adapter ─────────────────────────────────

class TestCalendarAdapter:
    def test_parses_headers(self):
        from webhook_adapters import adapt_calendar_headers
        headers = _make_calendar_headers()
        result = adapt_calendar_headers(headers)
        assert result["source"] == "webhook.calendar"
        assert "ch-123" in result["source_id"]
        assert "42" in result["source_id"]
        assert result["payload"]["channel_id"] == "ch-123"
        assert result["payload"]["message_number"] == "42"
        assert result["payload"]["resource_state"] == "exists"
        assert result["delivery_id"] == "ch-123:42"

    def test_cancelled_state(self):
        from webhook_adapters import adapt_calendar_headers
        headers = _make_calendar_headers(state="not_exists")
        result = adapt_calendar_headers(headers)
        assert result["event_type"] == "calendar_cancelled"

    def test_sync_state(self):
        from webhook_adapters import adapt_calendar_headers
        headers = _make_calendar_headers(state="sync")
        result = adapt_calendar_headers(headers)
        assert result["event_type"] == "calendar_sync"

    def test_different_messages_different_ids(self):
        """Two notifications for the same resource but different message numbers."""
        from webhook_adapters import adapt_calendar_headers
        h1 = _make_calendar_headers(message_number="42")
        h2 = _make_calendar_headers(message_number="43")
        r1 = adapt_calendar_headers(h1)
        r2 = adapt_calendar_headers(h2)
        assert r1["source_id"] != r2["source_id"]


# ─── Drive Header Adapter ────────────────────────────────────

class TestDriveAdapter:
    def test_parses_headers(self):
        from webhook_adapters import adapt_drive_headers
        headers = _make_drive_headers()
        result = adapt_drive_headers(headers)
        assert result["source"] == "webhook.drive"
        assert "ch-789" in result["source_id"]
        assert "99" in result["source_id"]
        assert result["delivery_id"] == "ch-789:99"

    def test_deleted_state(self):
        from webhook_adapters import adapt_drive_headers
        headers = _make_drive_headers(state="not_exists")
        result = adapt_drive_headers(headers)
        assert result["event_type"] == "document_deleted"


# ─── Endpoint Routing ───────────────────────────────────────

class TestEndpointRouting:
    def test_gmail_endpoint_pubsub(self):
        from webhook_adapters import adapt_for_endpoint
        payload = _make_pubsub_payload()
        result = adapt_for_endpoint("/webhooks/gmail", payload, {})
        assert result["source"] == "webhook.gmail"
        assert "pubsub" in result["source_id"]

    def test_calendar_endpoint_uses_headers(self):
        from webhook_adapters import adapt_for_endpoint
        headers = _make_calendar_headers()
        result = adapt_for_endpoint("/webhooks/calendar", {}, headers)
        assert result["source"] == "webhook.calendar"
        assert result["payload"]["channel_id"] == "ch-123"

    def test_drive_endpoint_uses_headers(self):
        from webhook_adapters import adapt_for_endpoint
        headers = _make_drive_headers()
        result = adapt_for_endpoint("/webhooks/drive", {}, headers)
        assert result["source"] == "webhook.drive"

    def test_generic_endpoint_auto_detects_gmail(self):
        from webhook_adapters import adapt_for_endpoint
        payload = _make_pubsub_payload()
        result = adapt_for_endpoint("/webhooks/generic", payload, {})
        # Should auto-detect as gmail_pubsub
        assert result["source"] == "webhook.gmail"

    def test_unknown_endpoint_falls_through_generic(self):
        from webhook_adapters import adapt_for_endpoint
        result = adapt_for_endpoint("/webhooks/generic", {"type": "custom"}, {})
        assert result["source"] == "webhook.generic"


# ─── Replay Protection (Delivery-ID-Based) ──────────────────

class TestReplayProtection:
    def test_first_delivery_reserved(self, with_secret):
        from webhook_security import reserve_delivery, complete_delivery
        config, project = with_secret
        ok, reason = reserve_delivery(config, "delivery-1")
        assert ok and reason == "OK"

    def test_replay_after_complete(self, with_secret):
        from webhook_security import reserve_delivery, complete_delivery
        config, project = with_secret
        reserve_delivery(config, "delivery-2")
        complete_delivery(config, "delivery-2")
        ok, reason = reserve_delivery(config, "delivery-2")
        assert not ok
        assert "completed" in reason

    def test_concurrent_processing_rejected(self, with_secret):
        from webhook_security import reserve_delivery
        config, project = with_secret
        reserve_delivery(config, "delivery-3")  # processing
        ok, reason = reserve_delivery(config, "delivery-3")
        assert not ok
        assert "processing" in reason

    def test_release_allows_retry(self, with_secret):
        from webhook_security import reserve_delivery, release_delivery
        config, project = with_secret
        reserve_delivery(config, "delivery-4")
        release_delivery(config, "delivery-4")  # simulate failure
        ok, _ = reserve_delivery(config, "delivery-4")
        assert ok  # can retry

    def test_different_empty_body_calendars_different_ids(self, with_secret):
        """Two empty-body Calendar notifications with different message numbers
        must NOT be treated as replays of each other."""
        from webhook_security import reserve_delivery
        config, project = with_secret
        ok1, _ = reserve_delivery(config, "ch-1:msg-1")
        ok2, _ = reserve_delivery(config, "ch-1:msg-2")
        assert ok1 and ok2  # Different delivery IDs — both accepted


# ─── Channel Token Validation ───────────────────────────────

class TestChannelToken:
    def test_valid_token(self, with_token):
        from webhook_security import verify_channel_token
        assert verify_channel_token("my-channel-token")

    def test_invalid_token(self, with_token):
        from webhook_security import verify_channel_token
        assert not verify_channel_token("wrong-token")

    def test_no_token_when_required(self, with_token):
        from webhook_security import verify_channel_token
        assert not verify_channel_token(None)

    def test_disabled_when_not_configured(self, with_secret):
        from webhook_security import verify_channel_token
        # Fail-closed: no token configured = rejected
        assert verify_channel_token("anything") is False
        assert verify_channel_token(None) is False


# ─── Receiver Integration ───────────────────────────────────

class TestReceiverIntegration:
    def _make_handler(self, config, stats, generate_suggestions=False):
        from webhook_receiver import create_handler, WebhookStats
        handler_class = create_handler(config, stats, generate_suggestions)
        return handler_class

    def test_gmail_pubsub_ingested(self, with_secret):
        config, project = with_secret
        from webhook_receiver import WebhookStats
        from webhook_security import sign_payload
        stats = WebhookStats()
        handler_class = self._make_handler(config, stats)

        body = json.dumps(_make_pubsub_payload()).encode()
        sig = sign_payload(body, TEST_SECRET)

        handler = handler_class.__new__(handler_class)
        handler.headers = {"X-Webhook-Signature": sig, "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()

        with patch.object(handler, "_respond") as mock_respond:
            handler.path = "/webhooks/gmail"
            handler.do_POST()

        status, data = mock_respond.call_args[0]
        assert status == 200
        assert data["status"] == "ingested"

    def test_calendar_channel_token_validated(self, with_token):
        config, project = with_token
        from webhook_receiver import WebhookStats
        stats = WebhookStats()
        handler_class = self._make_handler(config, stats)

        headers = _make_calendar_headers(token="my-channel-token")
        handler = handler_class.__new__(handler_class)
        handler.path = "/webhooks/calendar"
        handler.headers = {**headers, "Content-Length": "0"}
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()

        with patch.object(handler, "_respond") as mock_respond:
            handler.do_POST()

        status, data = mock_respond.call_args[0]
        assert status == 200
        assert data["status"] == "ingested"

    def test_calendar_wrong_token_rejected(self, with_token):
        config, project = with_token
        from webhook_receiver import WebhookStats
        stats = WebhookStats()
        handler_class = self._make_handler(config, stats)

        headers = _make_calendar_headers(token="wrong-token")
        handler = handler_class.__new__(handler_class)
        handler.path = "/webhooks/calendar"
        handler.headers = {**headers, "Content-Length": "0"}
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()

        with patch.object(handler, "_respond") as mock_respond:
            handler.do_POST()

        status, data = mock_respond.call_args[0]
        assert status == 401
        assert "channel token" in data["error"].lower()

    def test_oversized_body_rejected(self, with_secret):
        config, project = with_secret
        from webhook_receiver import WebhookStats, MAX_BODY_BYTES
        stats = WebhookStats()
        handler_class = self._make_handler(config, stats)

        handler = handler_class.__new__(handler_class)
        handler.path = "/webhooks/generic"
        handler.headers = {"X-Webhook-Signature": "x", "Content-Length": str(MAX_BODY_BYTES + 1)}
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()

        with patch.object(handler, "_respond") as mock_respond:
            handler.do_POST()

        status, data = mock_respond.call_args[0]
        assert status == 413

    def test_invalid_endpoint(self, with_secret):
        config, project = with_secret
        from webhook_receiver import WebhookStats
        stats = WebhookStats()
        handler_class = self._make_handler(config, stats)

        handler = handler_class.__new__(handler_class)
        handler.path = "/unknown"
        handler.headers = {"Content-Length": "0"}
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()

        with patch.object(handler, "_respond") as mock_respond:
            handler.do_POST()

        status, data = mock_respond.call_args[0]
        assert status == 404

    def test_ingestion_failure_releases_delivery(self, with_secret):
        config, project = with_secret
        from webhook_receiver import WebhookStats
        from webhook_security import sign_payload, reserve_delivery, _load_replay_cache
        stats = WebhookStats()
        handler_class = self._make_handler(config, stats)

        body = json.dumps(_make_pubsub_payload(message_id="fail-001")).encode()
        sig = sign_payload(body, TEST_SECRET)

        handler = handler_class.__new__(handler_class)
        handler.path = "/webhooks/gmail"
        handler.headers = {"X-Webhook-Signature": sig, "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()

        with patch("event_store.ingest_event", side_effect=Exception("DB error")), \
             patch.object(handler, "_respond") as mock_respond:
            handler.do_POST()

        status, data = mock_respond.call_args[0]
        assert status == 500

        # Delivery should be released — retryable
        cache = _load_replay_cache(config)
        assert "fail-001" not in cache.get("entries", {})


# ─── Generic Approve/Execute ─────────────────────────────────

class TestGenericApproveExecute:
    def test_approve_pending_action(self, with_secret):
        config, project = with_secret
        from pending_actions import create_pending_action
        action = create_pending_action(
            config=config, action_type="gmail.label", provider="google_api",
            target="msg-1", payload={"message_id": "msg-1", "label_id": "Label_1"},
            summary="Test label action",
        )
        action_id = action["id"]

        import webhook_events
        with patch("webhook_events.load_config", return_value=config):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = webhook_events.main(["--summary", "approve", "--action-id", action_id,
                                          "--approver", "MH", "--reason", "test"])
        assert rc == 0
        assert "✅ Approved" in buf.getvalue()

    def test_execute_approved_action(self, with_secret):
        config, project = with_secret
        from pending_actions import create_pending_action, approve_pending_action
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.mail_tag.return_value = {"success": True, "action": "gmail.label"}
        mock_client.supports.side_effect = lambda a: True

        action = create_pending_action(
            config=config, action_type="gmail.label", provider="google_api",
            target="msg-2", payload={"message_id": "msg-2", "label_id": "Label_1"},
            summary="Test label",
        )
        approve_pending_action(config, action["id"], approver="MH", reason="test")

        import webhook_events
        with patch("webhook_events.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = webhook_events.main(["--summary", "execute", "--action-id", action["id"]])
        assert rc == 0
        assert "✅ Executed" in buf.getvalue()
        mock_client.mail_tag.assert_called_once()

    def test_execute_not_approved_fails(self, with_secret):
        config, project = with_secret
        from pending_actions import create_pending_action
        action = create_pending_action(
            config=config, action_type="gmail.label", provider="google_api",
            target="msg-3", payload={}, summary="Test",
        )
        import webhook_events
        with patch("webhook_events.load_config", return_value=config):
            rc = webhook_events.main(["--summary", "execute", "--action-id", action["id"]])
        assert rc == 1


# ─── CLI event_type Fix ─────────────────────────────────────

class TestCLIDisplayFix:
    def test_inspect_shows_event_type(self, with_secret):
        config, project = with_secret
        from event_store import ingest_event
        ingest_event(config, "webhook.gmail", "display-test-1", "email_received", {"x": 1})
        import webhook_events
        with patch("webhook_events.load_config", return_value=config):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = webhook_events.main(["--summary", "inspect", "--limit", "5"])
        assert rc == 0
        out = buf.getvalue()
        assert "email_received" in out
        assert "Type: email_received" in out

    def test_replay_shows_event_type(self, with_secret):
        config, project = with_secret
        from event_store import ingest_event
        event = ingest_event(config, "webhook.gmail", "replay-type-1", "email_received", {"x": 1})
        import webhook_events
        with patch("webhook_events.load_config", return_value=config):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = webhook_events.main(["replay", "--event-id", event["id"], "--dry-run"])
        assert rc == 0
        assert "email_received" in buf.getvalue()


# ─── Safety ─────────────────────────────────────────────────

class TestSafety:
    def test_no_provider_writes_during_webhook(self, with_secret):
        config, project = with_secret
        mock_client = MagicMock()
        with patch("workspace_client.get_workspace_client", return_value=mock_client):
            from event_store import ingest_event
            ingest_event(config, "webhook.gmail", "safety-test-1", "email_received", {"x": 1})
            mock_client.mail_send.assert_not_called()
            mock_client.mail_tag.assert_not_called()

    def test_no_pending_actions_during_webhook(self, with_secret):
        config, project = with_secret
        with patch("pending_actions.create_pending_action") as mock_create:
            from event_store import ingest_event
            ingest_event(config, "webhook.gmail", "safety-test-2", "email_received", {"x": 1})
            mock_create.assert_not_called()

    def test_validate_secret_shows_endpoint_status(self, with_token):
        import webhook_events
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = webhook_events.main(["validate-secret"])
        # with_token sets secret + channel token but NOT pubsub vars,
        # so config is not fully valid — that's OK, we check output
        out = buf.getvalue()
        assert "gmail" in out
        assert "calendar" in out
        assert "drive" in out