#!/usr/bin/env python3
"""Tests for v0.2.0.2 — cryptographic JWT verification and guardrail cleanup.

Tests:
1. Forged JWT with valid-looking claims → rejected (signature not verified by Google)
2. Tampered payload after signing → rejected
3. Invalid signature → rejected
4. Expired token → rejected
5. Future iat (not-yet-valid) → rejected
6. Wrong signing key / unknown kid → rejected
7. Valid Google-signed token with wrong audience → rejected
8. Valid token with wrong service-account email → rejected
9. Guardrail env vars restored after execution
10. Guardrail env vars restored after execution failure
11. Guardrail env vars restored after provider exception
"""
import sys
import os
import io
import base64
import json
import time
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
for skill in ("document-preparer",):
    d = SKILLS_SCRIPTS / skill / "scripts"
    if d.exists() and str(d) not in sys.path:
        sys.path.insert(0, str(d))

TEST_SECRET = "test-webhook-secret-key-1234567890"
TEST_AUDIENCE = "https://myapp.example.com/webhooks/gmail"
TEST_SA = "pubsub@my-project.iam.gserviceaccount.com"


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
def with_pubsub(temp_project, monkeypatch):
    monkeypatch.setenv("CHIEF_OF_STAFF_WEBHOOK_SECRET", TEST_SECRET)
    monkeypatch.setenv("CHIEF_OF_STAFF_PUBSUB_AUDIENCE", TEST_AUDIENCE)
    monkeypatch.setenv("CHIEF_OF_STAFF_PUBSUB_SERVICE_ACCOUNT", TEST_SA)
    return temp_project


def _make_forged_jwt(iss="https://accounts.google.com", aud=TEST_AUDIENCE,
                     email=TEST_SA, email_verified=True, exp=None, iat=None):
    """Create a forged (unsigned) JWT with valid-looking claims."""
    now = int(time.time())
    if exp is None:
        exp = now + 3600
    if iat is None:
        iat = now
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode().rstrip("=")
    payload = {
        "iss": iss, "aud": aud, "email": email,
        "email_verified": email_verified, "exp": exp, "iat": iat,
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(b"").decode().rstrip("=")
    return f"{header}.{payload_b64}.{sig}"


# ─── Forged/Tampered JWT Rejection ───────────────────────────

class TestForgedJWTRejection:
    """All forged JWTs must be rejected by verify_pubsub_oidc.

    These tests mock google.oauth2.id_token.verify_oauth2_token to simulate
    the real behavior of Google's library, which cryptographically verifies
    the JWT signature. Forged tokens will fail signature verification.
    """

    def test_forged_jwt_with_valid_claims_rejected(self, with_pubsub):
        """A forged JWT with correct iss/aud/email but no valid signature."""
        from webhook_validation import verify_pubsub_oidc
        forged = _make_forged_jwt()
        with patch("google.oauth2.id_token.verify_oauth2_token",
                   side_effect=ValueError("Signature verification failed")):
            ok, reason = verify_pubsub_oidc(f"Bearer {forged}")
        assert not ok
        assert "JWT verification failed" in reason

    def test_tampered_payload_rejected(self, with_pubsub):
        """Tampering with the payload after signing invalidates the signature."""
        from webhook_validation import verify_pubsub_oidc
        # Simulate: Google's library detects signature mismatch after tampering
        with patch("google.oauth2.id_token.verify_oauth2_token",
                   side_effect=ValueError("Token signature invalid")):
            ok, reason = verify_pubsub_oidc("Bearer tampered.token.here")
        assert not ok
        assert "JWT verification failed" in reason

    def test_invalid_signature_rejected(self, with_pubsub):
        from webhook_validation import verify_pubsub_oidc
        with patch("google.oauth2.id_token.verify_oauth2_token",
                   side_effect=ValueError("Invalid signature")):
            ok, reason = verify_pubsub_oidc("Bearer some.invalid.sig")
        assert not ok
        assert "JWT verification failed" in reason

    def test_expired_token_rejected(self, with_pubsub):
        from webhook_validation import verify_pubsub_oidc
        with patch("google.oauth2.id_token.verify_oauth2_token",
                   side_effect=ValueError("Token expired")):
            ok, reason = verify_pubsub_oidc("Bearer expired.token.here")
        assert not ok
        assert "JWT verification failed" in reason

    def test_future_iat_rejected(self, with_pubsub):
        from webhook_validation import verify_pubsub_oidc
        with patch("google.oauth2.id_token.verify_oauth2_token",
                   side_effect=ValueError("Token issued in the future")):
            ok, reason = verify_pubsub_oidc("Bearer future.token.here")
        assert not ok
        assert "JWT verification failed" in reason

    def test_wrong_signing_key_rejected(self, with_pubsub):
        from webhook_validation import verify_pubsub_oidc
        with patch("google.oauth2.id_token.verify_oauth2_token",
                   side_effect=KeyError("unknown_kid")):
            ok, reason = verify_pubsub_oidc("Bearer unknown.key.token")
        assert not ok
        assert "JWT verification failed" in reason

    def test_wrong_audience_rejected(self, with_pubsub):
        """Google's verify_oauth2_token rejects wrong audience."""
        from webhook_validation import verify_pubsub_oidc
        with patch("google.oauth2.id_token.verify_oauth2_token",
                   side_effect=ValueError("Wrong audience")):
            ok, reason = verify_pubsub_oidc("Bearer valid.signed.but.wrong.aud")
        assert not ok
        assert "JWT verification failed" in reason

    def test_wrong_service_account_after_verification(self, with_pubsub):
        """Token passes Google verification but has wrong SA email."""
        from webhook_validation import verify_pubsub_oidc
        # Simulate: Google verifies signature OK, but claims have wrong email
        wrong_claims = {"email": "evil@attacker.com", "email_verified": True,
                        "iss": "https://accounts.google.com", "aud": TEST_AUDIENCE}
        with patch("google.oauth2.id_token.verify_oauth2_token",
                   return_value=wrong_claims):
            ok, reason = verify_pubsub_oidc("Bearer valid.token.wrong.sa")
        assert not ok
        assert "Unexpected service account" in reason

    def test_email_not_verified_after_verification(self, with_pubsub):
        from webhook_validation import verify_pubsub_oidc
        claims = {"email": TEST_SA, "email_verified": False,
                  "iss": "https://accounts.google.com", "aud": TEST_AUDIENCE}
        with patch("google.oauth2.id_token.verify_oauth2_token",
                   return_value=claims):
            ok, reason = verify_pubsub_oidc("Bearer valid.token.not.verified")
        assert not ok
        assert "not verified" in reason

    def test_wrong_issuer_after_verification(self, with_pubsub):
        from webhook_validation import verify_pubsub_oidc
        claims = {"email": TEST_SA, "email_verified": True,
                  "iss": "https://evil.com", "aud": TEST_AUDIENCE}
        with patch("google.oauth2.id_token.verify_oauth2_token",
                   return_value=claims):
            ok, reason = verify_pubsub_oidc("Bearer valid.token.wrong.iss")
        assert not ok
        assert "Unexpected issuer" in reason

    def test_valid_token_accepted(self, with_pubsub):
        """A cryptographically valid token with all correct claims passes."""
        from webhook_validation import verify_pubsub_oidc
        valid_claims = {"email": TEST_SA, "email_verified": True,
                        "iss": "https://accounts.google.com", "aud": TEST_AUDIENCE}
        with patch("google.oauth2.id_token.verify_oauth2_token",
                   return_value=valid_claims):
            ok, reason = verify_pubsub_oidc("Bearer valid.signed.token")
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

    def test_config_incomplete(self, with_pubsub, monkeypatch):
        monkeypatch.delenv("CHIEF_OF_STAFF_PUBSUB_AUDIENCE", raising=False)
        from webhook_validation import verify_pubsub_oidc
        ok, reason = verify_pubsub_oidc("Bearer some.token.here")
        assert not ok
        assert "configuration incomplete" in reason.lower()


# ─── Guardrail Environment Restoration ──────────────────────

class TestGuardrailRestoration:
    """Verify env vars are saved before execution and restored after."""

    def _create_and_approve(self, config, action_type, payload, mock_client):
        from state_db import create_pending_action, approve_pending_action
        action = create_pending_action(
            config=config, action_type=action_type, provider="google_api",
            target="test", payload=payload, summary=f"Test {action_type}",
        )
        approve_pending_action(config, action["id"], approver="MH", reason="test")
        return action["id"]

    def test_env_restored_after_success(self, with_pubsub):
        config, project = with_pubsub
        # Set pre-existing values
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "0"
        os.environ["CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"] = "0"

        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda a: True
        mock_client.gmail_label.return_value = {"success": True}
        action_id = self._create_and_approve(config, "gmail.label",
            {"message_id": "m1", "label_id": "L1"}, mock_client)

        import webhook_events
        with patch("webhook_events.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            webhook_events.main(["execute", "--action-id", action_id])

        # Should be restored to pre-execution values
        assert os.environ.get("CHIEF_OF_STAFF_AUTO_APPROVE") == "0"
        assert os.environ.get("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE") == "0"

    def test_env_restored_after_failure(self, with_pubsub):
        config, project = with_pubsub
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "0"
        os.environ["CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"] = "0"

        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda a: True
        mock_client.gmail_label.return_value = {"success": False, "error": "label not found"}
        action_id = self._create_and_approve(config, "gmail.label",
            {"message_id": "m1", "label_id": "L1"}, mock_client)

        import webhook_events
        with patch("webhook_events.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            webhook_events.main(["execute", "--action-id", action_id])

        assert os.environ.get("CHIEF_OF_STAFF_AUTO_APPROVE") == "0"
        assert os.environ.get("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE") == "0"

    def test_env_restored_after_exception(self, with_pubsub):
        config, project = with_pubsub
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "0"
        os.environ["CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"] = "0"

        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda a: True
        mock_client.gmail_send.side_effect = Exception("network error")
        action_id = self._create_and_approve(config, "gmail.send",
            {"to": "x@y.com", "subject": "test", "body": "test"}, mock_client)

        import webhook_events
        with patch("webhook_events.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            webhook_events.main(["execute", "--action-id", action_id])

        assert os.environ.get("CHIEF_OF_STAFF_AUTO_APPROVE") == "0"
        assert os.environ.get("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE") == "0"

    def test_env_restored_when_not_set_before(self, with_pubsub):
        """If env vars were not set before execution, they should be removed after."""
        config, project = with_pubsub
        os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)
        os.environ.pop("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", None)

        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda a: True
        mock_client.gmail_label.return_value = {"success": True}
        action_id = self._create_and_approve(config, "gmail.label",
            {"message_id": "m1", "label_id": "L1"}, mock_client)

        import webhook_events
        with patch("webhook_events.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            webhook_events.main(["execute", "--action-id", action_id])

        # Should be removed (not left as "1")
        assert "CHIEF_OF_STAFF_AUTO_APPROVE" not in os.environ
        assert "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE" not in os.environ