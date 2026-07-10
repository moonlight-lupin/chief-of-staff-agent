#!/usr/bin/env python3
"""Tests for v0.1.28 — email organisation digest and daily briefing integration.

Verifies:
- render_email_org_digest produces structured digest with text
- Digest includes classified count, by_category, suggestions, pending
- Digest text is human-readable
- email_org_status_for_briefing returns compact summary
- CLI digest command works
- CLI notify --channel cli prints digest
- CLI notify --channel email creates pending action, never auto-sends
- Daily briefing includes email_org source
- No Gmail mutations during digest or notify
"""

import sys
import os
import json
import io
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
SKILLS_SCRIPTS = PLUGIN_ROOT / "skills"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))
for skill in ("email-organisation", "daily-briefing"):
    d = SKILLS_SCRIPTS / skill / "scripts"
    if d.exists() and str(d) not in sys.path:
        sys.path.insert(0, str(d))


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
def temp_with_policy_and_data(temp_project):
    """Project with approved policy, classified emails, and generated suggestions."""
    config, project = temp_project
    from email_label_policy import save_approved_policy
    policy = {
        "version": 1, "mode": "use_existing_first", "status": "proposed",
        "source": {"provider": "google_api", "label_count": 14, "user_label_count": 3, "system_label_count": 11},
        "categories": {
            "finance_invoice": {"preferred_label": "Finance/Invoices", "label_id": "Label_1",
                                "confidence": 0.91, "source": "existing_label", "aliases": ["Invoices"]},
            "newsletter_marketing": {"preferred_label": "Newsletter", "label_id": "Label_8",
                                     "confidence": 0.84, "source": "existing_label", "aliases": ["Newsletter"]},
        },
        "unmapped_labels": [],
        "new_label_policy": {"default": "approval_required",
                             "create_only_if": {"min_matching_emails": 5, "min_days_observed": 14, "no_existing_label_fits": True}},
        "safety": {"allow_auto_create_labels": False, "allow_auto_apply_labels": False,
                   "allow_auto_archive": False, "allow_auto_trash": False},
    }
    save_approved_policy(config, policy, approved_by="MH")

    # Classify some emails
    from email_classifier import classify_inbox, generate_org_suggestions
    emails = [
        {"id": "m1", "subject": "Invoice for June services", "from": "billing@vendor.com", "snippet": "Payment due", "threadId": "t1"},
        {"id": "m2", "subject": "Weekly Newsletter - Tech", "from": "newsletter@tech.com", "snippet": "Tech updates", "threadId": "t2"},
        {"id": "m3", "subject": "Random question", "from": "colleague@company.com", "snippet": "Can we discuss?", "threadId": "t3"},
    ]
    classify_inbox(config, emails, limit=10)
    generate_org_suggestions(config, limit=50)
    return config, project


# ─── Digest Rendering ────────────────────────────────────────

class TestDigestRendering:
    """Test render_email_org_digest."""

    def test_digest_has_required_fields(self, temp_with_policy_and_data):
        from email_classifier import render_email_org_digest
        config, project = temp_with_policy_and_data
        digest = render_email_org_digest(config)
        required = {"total_classified", "with_category", "unmapped", "by_category",
                    "label_suggestions", "archive_suggestions", "create_label_suggestions",
                    "total_suggestions", "pending_actions", "text"}
        assert required.issubset(set(digest.keys()))

    def test_digest_total_matches(self, temp_with_policy_and_data):
        from email_classifier import render_email_org_digest
        config, project = temp_with_policy_and_data
        digest = render_email_org_digest(config)
        assert digest["total_classified"] == 3
        assert digest["with_category"] >= 2  # invoice + newsletter

    def test_digest_text_readable(self, temp_with_policy_and_data):
        from email_classifier import render_email_org_digest
        config, project = temp_with_policy_and_data
        digest = render_email_org_digest(config)
        text = digest["text"]
        assert "📬 Email Organisation Digest" in text
        assert "No Gmail changes were made" in text

    def test_digest_empty(self, temp_project):
        from email_classifier import render_email_org_digest
        config, project = temp_project
        digest = render_email_org_digest(config)
        assert digest["total_classified"] == 0
        assert "📬 Email Organisation Digest — 0" in digest["text"] or "Classified: 0" in digest["text"]


# ─── Briefing Integration ────────────────────────────────────

class TestBriefingIntegration:
    """Test email_org_status_for_briefing."""

    def test_status_has_compact_fields(self, temp_with_policy_and_data):
        from email_classifier import email_org_status_for_briefing
        config, project = temp_with_policy_and_data
        status = email_org_status_for_briefing(config)
        required = {"classified", "with_category", "unmapped", "suggestions",
                    "label_suggestions", "archive_suggestions",
                    "create_label_suggestions", "pending_actions"}
        assert required.issubset(set(status.keys()))

    def test_status_is_read_only(self, temp_with_policy_and_data, temp_project):
        """Briefing status should not include text or details — just counts."""
        from email_classifier import email_org_status_for_briefing
        config, project = temp_with_policy_and_data
        status = email_org_status_for_briefing(config)
        assert "text" not in status
        assert "details" not in status
        assert isinstance(status["classified"], int)


# ─── CLI Tests ───────────────────────────────────────────────

class TestCLIDigestNotify:
    """Test CLI digest and notify commands."""

    def test_digest_cli(self, temp_with_policy_and_data):
        config, project = temp_with_policy_and_data
        with patch("email_organisation.load_config", return_value=config):
            import email_organisation
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = email_organisation.main(["--summary", "digest"])
        assert rc == 0
        assert "📬 Email Organisation Digest" in buf.getvalue()

    def test_digest_json_cli(self, temp_with_policy_and_data):
        config, project = temp_with_policy_and_data
        with patch("email_organisation.load_config", return_value=config):
            import email_organisation
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = email_organisation.main(["digest"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["total_classified"] == 3

    def test_notify_cli(self, temp_with_policy_and_data):
        config, project = temp_with_policy_and_data
        with patch("email_organisation.load_config", return_value=config):
            import email_organisation
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = email_organisation.main(["--summary", "notify", "--channel", "cli"])
        assert rc == 0
        assert "📬" in buf.getvalue()

    def test_notify_cli_empty(self, temp_project):
        config, project = temp_project
        with patch("email_organisation.load_config", return_value=config):
            import email_organisation
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = email_organisation.main(["--summary", "notify", "--channel", "cli"])
        assert rc == 0
        assert "No email classifications" in buf.getvalue()

    def test_notify_email_creates_pending(self, temp_with_policy_and_data):
        config, project = temp_with_policy_and_data
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda a: a == "gmail.send"
        with patch("email_organisation.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            import email_organisation
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = email_organisation.main(["notify", "--channel", "email", "--to", "me@test.com"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["pending_action_id"] is not None
        assert "Pending action created" in data["message"]

    def test_notify_email_does_not_auto_send(self, temp_with_policy_and_data):
        """Email notification must NOT call gmail_send directly."""
        config, project = temp_with_policy_and_data
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        with patch("email_organisation.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            import email_organisation
            email_organisation.main(["notify", "--channel", "email", "--to", "me@test.com"])
        mock_client.gmail_send.assert_not_called()

    def test_notify_email_requires_to(self, temp_with_policy_and_data):
        config, project = temp_with_policy_and_data
        with patch("email_organisation.load_config", return_value=config):
            import email_organisation
            rc = email_organisation.main(["notify", "--channel", "email"])
        assert rc == 1  # missing --to


# ─── Safety: No Mutations ─────────────────────────────────────

class TestNoMutations:
    """Prove digest and notify never mutate Gmail."""

    def test_digest_never_calls_provider(self, temp_with_policy_and_data):
        from email_classifier import render_email_org_digest
        config, project = temp_with_policy_and_data
        mock_client = MagicMock()
        with patch("workspace_client.get_workspace_client", return_value=mock_client):
            render_email_org_digest(config)
        mock_client.gmail_send.assert_not_called()
        mock_client.gmail_label.assert_not_called()
        mock_client.gmail_archive.assert_not_called()

    def test_notify_email_never_calls_approve(self, temp_with_policy_and_data):
        config, project = temp_with_policy_and_data
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        with patch("email_organisation.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None), \
             patch("pending_actions.approve_pending_action") as mock_approve:
            import email_organisation
            email_organisation.main(["notify", "--channel", "email", "--to", "me@test.com"])
            mock_approve.assert_not_called()


# ─── Daily Briefing Integration ──────────────────────────────

class TestDailyBriefingIntegration:
    """Test that daily briefing includes email organisation."""

    def test_collect_email_org_returns_status(self, temp_with_policy_and_data):
        from daily_briefing import collect_email_org
        from config_loader import load_config
        config, project = temp_with_policy_and_data
        result = collect_email_org(config, project)
        assert len(result) == 1
        assert result[0]["classified"] == 3
        assert result[0]["with_category"] >= 2

    def test_collect_email_org_empty(self, temp_project):
        from daily_briefing import collect_email_org
        config, project = temp_project
        result = collect_email_org(config, project)
        assert len(result) == 1
        assert result[0]["classified"] == 0

    def test_email_org_in_source_names(self):
        from daily_briefing import SOURCE_NAMES
        assert "email_org" in SOURCE_NAMES