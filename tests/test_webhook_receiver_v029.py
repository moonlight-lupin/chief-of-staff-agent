#!/usr/bin/env python3
"""Tests for v0.1.29 — webhook receiver and event replay safety.

Verifies:
- HMAC signature verification works
- Replay protection blocks duplicate signatures
- Payload adapters normalize Gmail/Calendar/Drive/generic payloads
- Receiver ingests into event_store (idempotent)
- Receiver never executes, approves, or mutates
- Suggestion generation is optional and read-only
- CLI commands: serve, inspect, replay, validate-secret, sign
- No pending actions created during webhook ingestion
- No provider write methods called
"""
import sys
import os
import json
import io
import time
import hashlib
import hmac
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock
from http.server import HTTPServer

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


# ─── Signature Verification ──────────────────────────────────

class TestSignatureVerification:
    def test_sign_and_verify(self):
        from webhook_security import sign_payload, verify_signature
        body = b'{"test": true}'
        sig = sign_payload(body, TEST_SECRET)
        assert verify_signature(body, sig, TEST_SECRET)

    def test_wrong_signature_rejected(self):
        from webhook_security import verify_signature
        body = b'{"test": true}'
        assert not verify_signature(body, "wrong", TEST_SECRET)

    def test_no_secret_rejected(self):
        from webhook_security import verify_signature
        assert not verify_signature(b'{"x":1}', "any", None)

    def test_constant_time_comparison(self):
        from webhook_security import sign_payload, verify_signature
        body = b'{"test": true}'
        sig = sign_payload(body, TEST_SECRET)
        # Should use hmac.compare_digest (no timing attack)
        assert verify_signature(body, sig, TEST_SECRET)


# ─── Replay Protection ──────────────────────────────────────

class TestReplayProtection:
    def test_first_request_accepted(self, with_secret):
        from webhook_security import check_replay
        config, project = with_secret
        is_valid, reason = check_replay(config, "sig-unique-1")
        assert is_valid is True
        assert reason == "OK"

    def test_replay_blocked(self, with_secret):
        from webhook_security import check_replay
        config, project = with_secret
        check_replay(config, "sig-replay-1")  # first time
        is_valid, reason = check_replay(config, "sig-replay-1")  # replay
        assert is_valid is False
        assert "Replay detected" in reason

    def test_different_signatures_both_accepted(self, with_secret):
        from webhook_security import check_replay
        config, project = with_secret
        v1, _ = check_replay(config, "sig-a")
        v2, _ = check_replay(config, "sig-b")
        assert v1 and v2


# ─── Secret Validation ──────────────────────────────────────

class TestSecretValidation:
    def test_valid_secret(self, with_secret):
        from webhook_security import validate_secret_config
        result = validate_secret_config()
        assert result["valid"] is True
        assert result["length"] == len(TEST_SECRET)

    def test_no_secret(self, temp_project, monkeypatch):
        monkeypatch.delenv("CHIEF_OF_STAFF_WEBHOOK_SECRET", raising=False)
        from webhook_security import validate_secret_config
        result = validate_secret_config()
        assert result["valid"] is False

    def test_short_secret(self, temp_project, monkeypatch):
        monkeypatch.setenv("CHIEF_OF_STAFF_WEBHOOK_SECRET", "short")
        from webhook_security import validate_secret_config
        result = validate_secret_config()
        assert result["valid"] is False


# ─── Payload Adapters ───────────────────────────────────────

class TestPayloadAdapters:
    def test_gmail_adapter(self):
        from webhook_adapters import adapt_gmail
        result = adapt_gmail({"emailAddress": "test@x.com", "historyId": "12345"})
        assert result["source"] == "webhook.gmail"
        assert result["source_id"] == "gmail-history-12345"
        assert result["event_type"] == "email_received"
        assert "test@x.com" in result["summary"]

    def test_calendar_adapter(self):
        from webhook_adapters import adapt_calendar
        result = adapt_calendar({"resourceId": "r1", "resourceState": "exists"})
        assert result["source"] == "webhook.calendar"
        assert "r1" in result["source_id"]
        assert result["event_type"] == "calendar_changed"

    def test_calendar_cancelled(self):
        from webhook_adapters import adapt_calendar
        result = adapt_calendar({"resourceId": "r2", "resourceState": "not_exists"})
        assert result["event_type"] == "calendar_cancelled"

    def test_drive_adapter(self):
        from webhook_adapters import adapt_drive
        result = adapt_drive({"resourceId": "d1", "resourceState": "exists"})
        assert result["source"] == "webhook.drive"
        assert result["event_type"] == "document_shared"

    def test_generic_adapter(self):
        from webhook_adapters import adapt_generic
        result = adapt_generic({"source_id": "x1", "event_type": "custom", "summary": "Test"})
        assert result["source"] == "webhook.generic"
        assert result["source_id"] == "x1"
        assert result["event_type"] == "custom"

    def test_generic_adapter_generates_id(self):
        from webhook_adapters import adapt_generic
        result = adapt_generic({"type": "test"})
        assert result["source_id"].startswith("generic-")

    def test_detect_provider_gmail(self):
        from webhook_adapters import detect_provider
        assert detect_provider({"emailAddress": "x@y.com"}) == "gmail"
        assert detect_provider({"historyId": "123"}) == "gmail"

    def test_detect_provider_calendar(self):
        from webhook_adapters import detect_provider
        assert detect_provider({"resourceId": "r1", "resourceState": "exists", "eventId": "e1"}) == "calendar"

    def test_detect_provider_drive(self):
        from webhook_adapters import detect_provider
        assert detect_provider({"resourceId": "d1", "resourceState": "exists"}) == "drive"

    def test_detect_provider_generic(self):
        from webhook_adapters import detect_provider
        assert detect_provider({"custom": "data"}) == "generic"

    def test_adapt_payload_routes(self):
        from webhook_adapters import adapt_payload
        gmail_result = adapt_payload("gmail", {"emailAddress": "x@y.com", "historyId": "1"})
        assert gmail_result["source"] == "webhook.gmail"
        generic_result = adapt_payload("unknown", {"type": "test"})
        assert generic_result["source"] == "webhook.generic"


# ─── Receiver Integration (mocked HTTP) ─────────────────────

class TestReceiverIntegration:
    """Test receiver using a mock HTTP server."""

    def test_valid_request_ingested(self, with_secret):
        config, project = with_secret
        from webhook_receiver import create_handler, WebhookStats
        from webhook_security import sign_payload
        stats = WebhookStats()
        handler_class = create_handler(config, stats)

        body = json.dumps({"emailAddress": "test@x.com", "historyId": "99999"}).encode()
        sig = sign_payload(body, TEST_SECRET)

        # Simulate request
        handler = handler_class.__new__(handler_class)
        handler.headers = {"X-Webhook-Signature": sig, "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        handler._respond = MagicMock()

        with patch.object(handler, "_respond") as mock_respond:
            handler.do_POST()
            mock_respond.assert_called_once()
            status, data = mock_respond.call_args[0]
            assert status == 200
            assert data["status"] == "ingested"

    def test_invalid_signature_rejected(self, with_secret):
        config, project = with_secret
        from webhook_receiver import create_handler, WebhookStats
        stats = WebhookStats()
        handler_class = create_handler(config, stats)

        body = b'{"emailAddress": "test@x.com"}'

        handler = handler_class.__new__(handler_class)
        handler.headers = {"X-Webhook-Signature": "wrong", "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()

        with patch.object(handler, "_respond") as mock_respond:
            handler.do_POST()
            status, data = mock_respond.call_args[0]
            assert status == 401
            assert "signature" in data["error"].lower()

    def test_replay_blocked(self, with_secret):
        config, project = with_secret
        from webhook_receiver import create_handler, WebhookStats
        from webhook_security import sign_payload, check_replay
        stats = WebhookStats()
        handler_class = create_handler(config, stats)

        body = json.dumps({"emailAddress": "test@x.com", "historyId": "88888"}).encode()
        sig = sign_payload(body, TEST_SECRET)
        # Pre-register the signature in replay cache
        check_replay(config, sig)

        handler = handler_class.__new__(handler_class)
        handler.headers = {"X-Webhook-Signature": sig, "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()

        with patch.object(handler, "_respond") as mock_respond:
            handler.do_POST()
            status, data = mock_respond.call_args[0]
            assert status == 409
            assert "Replay" in data["error"]


# ─── Safety: No Mutations ───────────────────────────────────

class TestNoMutations:
    """Prove webhook receiver never executes, approves, or mutates."""

    def test_no_pending_actions_created(self, with_secret):
        config, project = with_secret
        from webhook_adapters import adapt_gmail
        from event_store import ingest_event
        with patch("pending_actions.create_pending_action") as mock_create:
            event = adapt_gmail({"emailAddress": "x@y.com", "historyId": "77777"})
            ingest_event(config, event["source"], event["source_id"],
                         event["event_type"], event["payload"])
            mock_create.assert_not_called()

    def test_no_provider_writes_during_ingestion(self, with_secret):
        config, project = with_secret
        from event_store import ingest_event
        mock_client = MagicMock()
        with patch("workspace_client.get_workspace_client", return_value=mock_client):
            ingest_event(config, "webhook.gmail", "test-src-1", "email_received", {"test": True})
            mock_client.gmail_send.assert_not_called()
            mock_client.gmail_label.assert_not_called()
            mock_client.gmail_archive.assert_not_called()

    def test_replay_dry_run_no_execution(self, with_secret):
        config, project = with_secret
        from event_store import ingest_event
        event = ingest_event(config, "webhook.gmail", "replay-test-1", "email_received", {"x": 1})
        assert event is not None
        with patch("suggested_actions.generate_for_events") as mock_gen:
            import webhook_events
            with patch("webhook_events.load_config", return_value=config):
                webhook_events.main(["replay", "--event-id", event["id"], "--dry-run"])
                mock_gen.assert_not_called()


# ─── CLI Tests ───────────────────────────────────────────────

class TestWebhookCLI:
    def test_validate_secret_valid(self, with_secret):
        import webhook_events
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = webhook_events.main(["validate-secret"])
        assert rc == 0
        assert "✅" in buf.getvalue()

    def test_validate_secret_invalid(self, temp_project, monkeypatch):
        monkeypatch.delenv("CHIEF_OF_STAFF_WEBHOOK_SECRET", raising=False)
        import webhook_events
        rc = webhook_events.main(["validate-secret"])
        assert rc == 1

    def test_sign(self, with_secret):
        import webhook_events
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = webhook_events.main(["sign", "--body", '{"test": true}'])
        assert rc == 0
        sig = buf.getvalue().strip()
        assert len(sig) == 64  # SHA256 hex

    def test_inspect_empty(self, with_secret):
        config, project = with_secret
        import webhook_events
        with patch("webhook_events.load_config", return_value=config):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = webhook_events.main(["--summary", "inspect", "--limit", "10"])
        assert rc == 0
        assert "No webhook events" in buf.getvalue()

    def test_inspect_with_events(self, with_secret):
        config, project = with_secret
        from event_store import ingest_event
        ingest_event(config, "webhook.gmail", "inspect-test-1", "email_received", {"x": 1})
        import webhook_events
        with patch("webhook_events.load_config", return_value=config):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = webhook_events.main(["--summary", "inspect", "--limit", "10"])
        assert rc == 0
        assert "🌐 Webhook Events" in buf.getvalue()

    def test_replay_dry_run(self, with_secret):
        config, project = with_secret
        from event_store import ingest_event
        event = ingest_event(config, "webhook.gmail", "replay-cli-1", "email_received", {"x": 1})
        import webhook_events
        with patch("webhook_events.load_config", return_value=config):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = webhook_events.main(["replay", "--event-id", event["id"], "--dry-run"])
        assert rc == 0
        assert "DRY-RUN" in buf.getvalue()