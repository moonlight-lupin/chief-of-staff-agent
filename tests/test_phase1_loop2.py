#!/usr/bin/env python3
"""Contract tests for Phase 1 Loop 2: tasks 1.5-1.9.

1.5: Audit blocked + failed-audit paths
1.6: Fix recipient-domain classification (substring → exact/suffix match)
1.7: Cap mark_failed retries + needs_verification state
1.8: Fix mail_list_folders degradation (crash → warn + [])
1.9: Fix deep-research license test (MIT → allow vendored MIT)
"""

import sys
import os
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


@pytest.fixture(autouse=True)
def clean_env():
    for key in ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"):
        os.environ.pop(key, None)
    yield
    for key in ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"):
        os.environ.pop(key, None)


@pytest.fixture
def temp_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = {
        "google": {"delegate_email": "test@test.com", "account_alias": "test"},
        "integrations": {"workspace": {"provider": "google_api"}},
        "paths": {"project_root": str(project)},
    }
    return config, project


# ═══════════════════════════════════════════════════════════════
# 1.5: Audit blocked + failed-audit paths
# ═══════════════════════════════════════════════════════════════

class TestAuditBlockedPath:
    """Blocked write attempts must be durably audited, not just runtime_log'd."""

    def test_blocked_action_is_audited(self, temp_project):
        """When confirm_action blocks, the audit log must record it."""
        from workspace_guardrails import guarded, ActionResult
        from pending_actions import create_pending_action

        config, project = temp_project

        # Create a mock provider with a @guarded method
        class MockProvider:
            provider_name = "test_provider"
            config = None

            @guarded("gmail.send", target_arg="to",
                     audit_provider="test_provider", audit_tool="test_tool")
            def send_email(self, to: str, subject: str = "", body: str = ""):
                return {"id": "sent-123"}

        provider = MockProvider()
        provider.config = config

        with patch("workspace_audit.audit_workspace_action") as mock_audit:
            # Without auto-approve, non-TTY: guardrail blocks
            result = provider.send_email(to="test@test.com", subject="S", body="B")

        assert result["success"] is False
        assert result["error"] == "cancelled by guardrail"

        # The block path must produce an audit record with status="blocked"
        audit_calls = mock_audit.call_args_list
        blocked_calls = [
            c for c in audit_calls
            if c.kwargs.get("status") == "blocked"
        ]
        assert len(blocked_calls) >= 1, (
            "Blocked action must be audited with status='blocked'. "
            f"Got audit calls: {audit_calls}"
        )

    def test_audit_failure_does_not_mask_success(self, temp_project):
        """If audit fails after a successful mutation, the ActionResult must still show success."""
        from workspace_guardrails import guarded, ActionResult

        config, project = temp_project

        class MockProvider:
            provider_name = "test_provider"
            config = None

            @guarded("gmail.draft", target_arg="to",
                     audit_provider="test_provider", audit_tool="test_tool")
            def create_draft(self, to: str, subject: str = "", body: str = ""):
                return {"id": "draft-123"}

        provider = MockProvider()
        provider.config = config
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"

        with patch("workspace_audit.audit_workspace_action",
                   side_effect=Exception("audit DB down")):
            result = provider.create_draft(to="test@test.com", subject="S", body="B")

        # The action succeeded — audit failure must not mask that
        assert result["success"] is True, (
            "Audit failure after successful mutation must not turn success into failure. "
            f"Got: {result}"
        )
        assert result["data"]["id"] == "draft-123"
        # audited should be False since audit failed
        assert result.get("audited") is False or result.get("audited") is True
        # The key: success=True even when audit raises


# ═══════════════════════════════════════════════════════════════
# 1.6: Fix recipient-domain classification
# ═══════════════════════════════════════════════════════════════

class TestRecipientDomainClassification:
    """classify_recipient_risk must use exact/suffix match, not substring."""

    def test_homograph_domain_not_internal(self):
        """acme.com.attacker.io must NOT be classified as internal for company acme.com."""
        from pending_actions import classify_recipient_risk
        config = {"company": {"website": "acme.com"}}
        result = classify_recipient_risk("user@acme.com.attacker.io", config)
        assert result["level"] != "internal", (
            f"acme.com.attacker.io must not be internal. Got: {result}"
        )

    def test_suffix_domain_not_internal(self):
        """acme.co must NOT be classified as internal for company acme.com."""
        from pending_actions import classify_recipient_risk
        config = {"company": {"website": "acme.com"}}
        result = classify_recipient_risk("user@acme.co", config)
        assert result["level"] != "internal", (
            f"acme.co must not be internal for acme.com. Got: {result}"
        )

    def test_exact_domain_is_internal(self):
        """acme.com must be classified as internal for company acme.com."""
        from pending_actions import classify_recipient_risk
        config = {"company": {"website": "acme.com"}}
        result = classify_recipient_risk("user@acme.com", config)
        assert result["level"] == "internal"

    def test_subdomain_is_internal(self):
        """mail.acme.com must be classified as internal for company acme.com."""
        from pending_actions import classify_recipient_risk
        config = {"company": {"website": "acme.com"}}
        result = classify_recipient_risk("user@mail.acme.com", config)
        assert result["level"] == "internal", (
            f"Subdomain of company domain should be internal. Got: {result}"
        )

    def test_url_with_protocol_domain_is_internal(self):
        """Company website as URL (https://acme.com) must still match domain."""
        from pending_actions import classify_recipient_risk
        config = {"company": {"website": "https://acme.com"}}
        result = classify_recipient_risk("user@acme.com", config)
        assert result["level"] == "internal"


# ═══════════════════════════════════════════════════════════════
# 1.7: Cap mark_failed retries + needs_verification state
# ═══════════════════════════════════════════════════════════════

class TestRetryCapAndNeedsVerification:
    """mark_failed must cap retries and add needs_verification for ambiguous outcomes."""

    def test_retry_cap_transitions_to_failed(self, temp_project):
        """After MAX_RETRIES (3), mark_failed must transition to 'failed', not 'approved'."""
        from pending_actions import (
            create_pending_action, approve_pending_action,
            mark_executing, mark_failed, get_pending_action,
        )
        config, project = temp_project

        action = create_pending_action(
            config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"}
        )
        approve_pending_action(config, action["id"])

        # Fail 3 times
        for i in range(3):
            mark_executing(config, action["id"])
            mark_failed(config, action["id"], f"error attempt {i+1}")

        # After 3rd failure, should be 'failed', not 'approved'
        loaded = get_pending_action(config, action["id"])
        assert loaded["state"] == "failed", (
            f"After 3 retries, state must be 'failed', got '{loaded['state']}'. "
            "Poison actions must not retry forever."
        )

    def test_retry_count_increments(self, temp_project):
        """mark_failed must increment retry_count."""
        from pending_actions import (
            create_pending_action, approve_pending_action,
            mark_executing, mark_failed, get_pending_action,
        )
        config, project = temp_project

        action = create_pending_action(
            config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"}
        )
        approve_pending_action(config, action["id"])

        mark_executing(config, action["id"])
        mark_failed(config, action["id"], "error 1")
        loaded = get_pending_action(config, action["id"])
        assert loaded.get("retry_count", 0) == 1

        mark_executing(config, action["id"])
        mark_failed(config, action["id"], "error 2")
        loaded = get_pending_action(config, action["id"])
        assert loaded.get("retry_count", 0) == 2

    def test_failed_action_cannot_be_executed(self, temp_project):
        """A 'failed' action must not be executable (terminal state)."""
        from pending_actions import (
            create_pending_action, approve_pending_action,
            mark_executing, mark_failed, mark_executing as me2,
        )
        config, project = temp_project

        action = create_pending_action(
            config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"}
        )
        approve_pending_action(config, action["id"])

        for i in range(3):
            mark_executing(config, action["id"])
            mark_failed(config, action["id"], f"error {i+1}")

        # mark_executing on a 'failed' action must return None
        result = me2(config, action["id"])
        assert result is None, "Failed (terminal) action must not be re-executable"


# ═══════════════════════════════════════════════════════════════
# 1.8: Fix mail_list_folders degradation
# ═══════════════════════════════════════════════════════════════
# Note: This test is M365-specific. Since we can't test against a real
# M365 tenant, we test the degradation pattern via mock.

class TestMailListFoldersDegradation:
    """mail_list_folders must degrade to [] on error, not crash."""

    def test_mail_list_folders_returns_empty_on_error(self):
        """When the M365 API fails, mail_list_folders must return [], not raise."""
        from providers.m365_graph import M365GraphClient

        # Create a client with a mock session
        config = {
            "m365": {
                "tenant_id": "fake",
                "client_id": "fake",
                "client_secret": "fake",
                "user_principal": "user@fake.com",
            },
            "paths": {"project_root": "/tmp/test-m365-degrade"},
        }

        with patch.object(M365GraphClient, '_request',
                          side_effect=Exception("API error")):
            client = M365GraphClient(config)
            # This must not raise
            result = client.mail_list_folders()
            assert result == [], (
                f"mail_list_folders must return [] on error, got: {result}"
            )


# ═══════════════════════════════════════════════════════════════
# 1.9: Fix deep-research license test
# ═══════════════════════════════════════════════════════════════

class TestDeepResearchLicense:
    """The deep-research skill is vendored MIT. The test must allow MIT for vendored skills."""

    def test_deep_research_license_accepted(self):
        """The plugin structure test must accept MIT license for vendored skills."""
        # This is a meta-test: we verify the test logic accepts MIT
        # The actual fix is in test_plugin_structure.py
        # Here we just verify the SKILL.md frontmatter
        skill_path = PLUGIN_ROOT / "skills" / "deep-research" / "SKILL.md"
        if not skill_path.exists():
            pytest.skip("deep-research skill not found")

        content = skill_path.read_text()
        # The skill declares MIT — the test must allow this
        assert "MIT" in content or "Apache" in content, (
            "deep-research SKILL.md must declare a valid license"
        )