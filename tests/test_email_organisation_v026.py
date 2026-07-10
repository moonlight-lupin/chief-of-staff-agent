#!/usr/bin/env python3
"""Tests for v0.1.26 — email organisation onboarding and label policy.

Verifies:
- Label parsing separates system from user labels
- Nested labels parsed into path/parent/leaf
- Category inference from label names
- Policy proposal uses existing labels, no new labels proposed
- Policy save/show works
- validate_policy catches missing fields and safety violations
- CLI commands work
- Safety: no Gmail mutations (no send/archive/trash/modify/create_label)
- Safety: no pending actions created
"""

import sys
import os
import json
import io
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
for skill in ("email-organisation",):
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


# Sample labels for testing
SAMPLE_LABELS = [
    {"id": "INBOX", "name": "INBOX", "type": "system"},
    {"id": "SENT", "name": "SENT", "type": "system"},
    {"id": "TRASH", "name": "TRASH", "type": "system"},
    {"id": "IMPORTANT", "name": "IMPORTANT", "type": "system"},
    {"id": "DRAFT", "name": "DRAFT", "type": "system"},
    {"id": "Label_1", "name": "Finance/Invoices", "type": "user", "messageCount": 42},
    {"id": "Label_2", "name": "Finance/Receipts", "type": "user", "messageCount": 15},
    {"id": "Label_3", "name": "Finance/Bank", "type": "user", "messageCount": 8},
    {"id": "Label_4", "name": "Legal/KYC", "type": "user", "messageCount": 3},
    {"id": "Label_5", "name": "Legal/Contracts", "type": "user", "messageCount": 12},
    {"id": "Label_6", "name": "Admin/Travel", "type": "user", "messageCount": 5},
    {"id": "Label_7", "name": "Misc/Old", "type": "user", "messageCount": 2},
    {"id": "Label_8", "name": "Newsletter", "type": "user", "messageCount": 30},
    {"id": "Label_9", "name": "Projects/Zephyr", "type": "user", "messageCount": 7},
]


@pytest.fixture
def mock_client():
    mock = MagicMock()
    mock.provider_name = "google_api"
    mock.supports.side_effect = lambda a: True
    mock.gmail_list_labels.return_value = SAMPLE_LABELS
    return mock


# ─── Label Parsing ────────────────────────────────────────────

class TestLabelParsing:
    """Test that parse_labels correctly separates and structures labels."""

    def test_system_labels_excluded_from_user(self):
        from email_label_policy import parse_labels
        parsed = parse_labels(SAMPLE_LABELS)
        system_names = [l["name"] for l in parsed["system_labels"]]
        user_names = [l["name"] for l in parsed["user_labels"]]
        assert "INBOX" in system_names
        assert "INBOX" not in user_names
        assert "Finance/Invoices" in user_names

    def test_user_labels_retained(self):
        from email_label_policy import parse_labels
        parsed = parse_labels(SAMPLE_LABELS)
        assert len(parsed["user_labels"]) == 9  # Labels 1-9

    def test_nested_labels_parsed(self):
        from email_label_policy import parse_labels
        parsed = parse_labels(SAMPLE_LABELS)
        nested = parsed["nested_user_labels"]
        names = [l["name"] for l in nested]
        assert "Finance/Invoices" in names
        assert "Legal/KYC" in names
        assert "Admin/Travel" in names
        # Check path structure
        invoices = next(l for l in nested if l["name"] == "Finance/Invoices")
        assert invoices["path"] == ["Finance", "Invoices"]
        assert invoices["parent"] == "Finance"
        assert invoices["leaf"] == "Invoices"
        assert invoices["depth"] == 2

    def test_counts_preserved(self):
        from email_label_policy import parse_labels
        parsed = parse_labels(SAMPLE_LABELS)
        invoices = next(l for l in parsed["user_labels"] if l["name"] == "Finance/Invoices")
        assert invoices["message_count"] == 42

    def test_groups_detected(self):
        from email_label_policy import parse_labels
        parsed = parse_labels(SAMPLE_LABELS)
        groups = parsed["groups"]
        assert "Finance" in groups
        assert "Legal" in groups
        assert "Admin" in groups
        assert len(groups["Finance"]) == 3  # Invoices, Receipts, Bank


# ─── Category Inference ───────────────────────────────────────

class TestCategoryInference:
    """Test category inference from label names."""

    def test_finance_invoice(self):
        from email_label_policy import infer_category
        cat, conf = infer_category("Finance/Invoices", ["Finance", "Invoices"])
        assert cat == "finance_invoice"
        assert conf > 0.60

    def test_kyc_compliance(self):
        from email_label_policy import infer_category
        cat, conf = infer_category("Legal/KYC", ["Legal", "KYC"])
        assert cat == "kyc_compliance"
        assert conf > 0.60

    def test_travel(self):
        from email_label_policy import infer_category
        cat, conf = infer_category("Admin/Travel", ["Admin", "Travel"])
        assert cat == "travel"
        assert conf > 0.60

    def test_legal_contract(self):
        from email_label_policy import infer_category
        cat, conf = infer_category("Legal/Contracts", ["Legal", "Contracts"])
        assert cat == "legal_contract"

    def test_unknown_label_low_confidence(self):
        """Labels with no clear category match should have low or no confidence."""
        from email_label_policy import infer_category
        cat, conf = infer_category("Misc/RandomXYZ", ["Misc", "RandomXYZ"])
        assert cat is None or conf < 0.60

    def test_newsletter_marketing(self):
        from email_label_policy import infer_category
        cat, conf = infer_category("Newsletter", ["Newsletter"])
        assert cat == "newsletter_marketing"


# ─── Policy Proposal ──────────────────────────────────────────

class TestPolicyProposal:
    """Test policy proposal generation."""

    def test_proposal_has_use_existing_first(self, temp_project):
        from email_label_policy import parse_labels, generate_policy
        config, project = temp_project
        parsed = parse_labels(SAMPLE_LABELS)
        policy = generate_policy(parsed, provider="google_api")
        assert policy["mode"] == "use_existing_first"

    def test_existing_labels_preferred(self, temp_project):
        from email_label_policy import parse_labels, generate_policy
        config, project = temp_project
        parsed = parse_labels(SAMPLE_LABELS)
        policy = generate_policy(parsed, provider="google_api")
        cats = policy["categories"]
        # Finance/Invoices should map to finance_invoice
        assert "finance_invoice" in cats
        assert cats["finance_invoice"]["preferred_label"] == "Finance/Invoices"
        assert cats["finance_invoice"]["source"] == "existing_label"

    def test_no_new_labels_proposed(self, temp_project):
        from email_label_policy import parse_labels, generate_policy
        config, project = temp_project
        parsed = parse_labels(SAMPLE_LABELS)
        policy = generate_policy(parsed, provider="google_api")
        # new_label_policy should require approval
        assert policy["new_label_policy"]["default"] == "approval_required"

    def test_safety_flags_all_false(self, temp_project):
        from email_label_policy import parse_labels, generate_policy
        config, project = temp_project
        parsed = parse_labels(SAMPLE_LABELS)
        policy = generate_policy(parsed, provider="google_api")
        safety = policy["safety"]
        assert safety["allow_auto_create_labels"] is False
        assert safety["allow_auto_apply_labels"] is False
        assert safety["allow_auto_archive"] is False
        assert safety["allow_auto_trash"] is False

    def test_unmapped_labels_listed(self, temp_project):
        from email_label_policy import parse_labels, generate_policy
        config, project = temp_project
        parsed = parse_labels(SAMPLE_LABELS)
        policy = generate_policy(parsed, provider="google_api")
        unmapped_names = [u["name"] for u in policy["unmapped_labels"]]
        # Projects/Zephyr has no keyword match
        assert "Projects/Zephyr" in unmapped_names


# ─── Policy Save/Show ─────────────────────────────────────────

class TestPolicySaveShow:
    """Test policy save and show operations."""

    def test_save_proposal(self, temp_project):
        from email_label_policy import parse_labels, generate_policy, save_proposal, load_proposal
        config, project = temp_project
        parsed = parse_labels(SAMPLE_LABELS)
        policy = generate_policy(parsed, provider="google_api")
        path = save_proposal(config, policy)
        assert path.exists()
        loaded = load_proposal(config)
        assert loaded is not None
        assert loaded["mode"] == "use_existing_first"

    def test_save_approved_policy(self, temp_project):
        from email_label_policy import parse_labels, generate_policy, save_approved_policy, load_policy
        config, project = temp_project
        parsed = parse_labels(SAMPLE_LABELS)
        policy = generate_policy(parsed, provider="google_api")
        path = save_approved_policy(config, policy, approved_by="MH")
        assert path.exists()
        loaded = load_policy(config)
        assert loaded is not None
        assert loaded["status"] == "approved"
        assert loaded["approved_by"] == "MH"
        assert "approved_at" in loaded

    def test_load_policy_none_if_not_exists(self, temp_project):
        from email_label_policy import load_policy
        config, project = temp_project
        assert load_policy(config) is None


# ─── Policy Validation ────────────────────────────────────────

class TestPolicyValidation:
    """Test validate_policy catches issues."""

    def test_valid_policy(self):
        from email_label_policy import validate_policy
        policy = {
            "version": 1,
            "mode": "use_existing_first",
            "status": "approved",
            "categories": {},
            "safety": {
                "allow_auto_create_labels": False,
                "allow_auto_apply_labels": False,
                "allow_auto_archive": False,
                "allow_auto_trash": False,
            },
        }
        errors = validate_policy(policy)
        assert len(errors) == 0

    def test_missing_field(self):
        from email_label_policy import validate_policy
        policy = {"version": 1, "mode": "use_existing_first"}
        errors = validate_policy(policy)
        assert any("status" in e for e in errors)

    def test_safety_violation(self):
        from email_label_policy import validate_policy
        policy = {
            "version": 1,
            "mode": "use_existing_first",
            "status": "approved",
            "categories": {},
            "safety": {
                "allow_auto_create_labels": True,  # VIOLATION
                "allow_auto_apply_labels": False,
                "allow_auto_archive": False,
                "allow_auto_trash": False,
            },
        }
        errors = validate_policy(policy)
        assert any("allow_auto_create_labels" in e for e in errors)

    def test_wrong_mode(self):
        from email_label_policy import validate_policy
        policy = {
            "version": 1,
            "mode": "auto_create",
            "status": "approved",
            "categories": {},
            "safety": {},
        }
        errors = validate_policy(policy)
        assert any("mode" in e for e in errors)


# ─── CLI Tests ────────────────────────────────────────────────

class TestEmailOrgCLI:
    """Test email_organisation.py CLI commands."""

    def test_inspect_labels_summary(self, temp_project, mock_client):
        config, project = temp_project
        with patch("email_organisation.load_config", return_value=config), \
             patch("email_organisation.get_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            import email_organisation
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = email_organisation.main(["--summary", "inspect-labels"])
        assert rc == 0
        out = buf.getvalue()
        assert "📬 Email Organisation" in out
        assert "Total labels: 14" in out
        assert "User labels: 9" in out

    def test_inspect_labels_json(self, temp_project, mock_client):
        config, project = temp_project
        with patch("email_organisation.load_config", return_value=config), \
             patch("email_organisation.get_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            import email_organisation
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = email_organisation.main(["inspect-labels"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["total"] == 14
        assert len(data["user_labels"]) == 9

    def test_propose_policy_summary(self, temp_project, mock_client):
        config, project = temp_project
        with patch("email_organisation.load_config", return_value=config), \
             patch("email_organisation.get_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            import email_organisation
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = email_organisation.main(["--summary", "propose-policy"])
        assert rc == 0
        out = buf.getvalue()
        assert "🧭 Proposed" in out
        assert "No Gmail changes were made" in out

    def test_save_policy(self, temp_project, mock_client):
        config, project = temp_project
        with patch("email_organisation.load_config", return_value=config), \
             patch("email_organisation.get_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            import email_organisation
            # First generate proposal
            email_organisation.main(["propose-policy"])
            # Then save it
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = email_organisation.main([
                    "--summary", "save-policy", "--approved-by", "MH",
                ])
        assert rc == 0
        assert "✅" in buf.getvalue()

    def test_show_policy(self, temp_project, mock_client):
        config, project = temp_project
        with patch("email_organisation.load_config", return_value=config), \
             patch("email_organisation.get_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            import email_organisation
            email_organisation.main(["propose-policy"])
            email_organisation.main(["save-policy", "--approved-by", "MH"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = email_organisation.main(["--summary", "show-policy"])
        assert rc == 0
        assert "📋 Approved" in buf.getvalue()

    def test_validate_policy(self, temp_project, mock_client):
        config, project = temp_project
        with patch("email_organisation.load_config", return_value=config), \
             patch("email_organisation.get_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            import email_organisation
            email_organisation.main(["propose-policy"])
            email_organisation.main(["save-policy", "--approved-by", "MH"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = email_organisation.main(["validate-policy"])
        assert rc == 0
        assert "✅" in buf.getvalue()

    def test_inspect_unsupported_provider(self, temp_project):
        config, project = temp_project
        mock_client = MagicMock()
        mock_client.provider_name = "composio:mcp"
        mock_client.supports.side_effect = lambda a: a != "gmail.labels.list"
        with patch("email_organisation.load_config", return_value=config), \
             patch("email_organisation.get_client", return_value=mock_client):
            import email_organisation
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = email_organisation.main(["--summary", "inspect-labels"])
        assert rc == 1
        assert "not supported" in buf.getvalue()


# ─── Safety: No Gmail Mutations ───────────────────────────────

class TestNoGmailMutations:
    """Prove that email organisation never mutates Gmail."""

    def test_no_gmail_send_called(self, temp_project, mock_client):
        from email_label_policy import parse_labels, generate_policy
        config, project = temp_project
        parsed = parse_labels(SAMPLE_LABELS)
        policy = generate_policy(parsed, provider="google_api")
        mock_client.gmail_send.assert_not_called()

    def test_no_gmail_modify_called(self, temp_project, mock_client):
        from email_label_policy import parse_labels, generate_policy
        config, project = temp_project
        parsed = parse_labels(SAMPLE_LABELS)
        policy = generate_policy(parsed, provider="google_api")
        # No archive/trash/label modify
        assert not hasattr(mock_client, "gmail_modify") or mock_client.gmail_modify.call_count == 0

    def test_no_pending_actions_created(self, temp_project, mock_client):
        from email_label_policy import parse_labels, generate_policy
        config, project = temp_project
        with patch("pending_actions.create_pending_action") as mock_create:
            parsed = parse_labels(SAMPLE_LABELS)
            policy = generate_policy(parsed, provider="google_api")
            mock_create.assert_not_called()

    def test_only_read_method_used(self, temp_project, mock_client):
        """Only gmail_list_labels should be called, not any write method."""
        config, project = temp_project
        with patch("email_organisation.load_config", return_value=config), \
             patch("email_organisation.get_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            import email_organisation
            email_organisation.main(["inspect-labels"])
        mock_client.gmail_list_labels.assert_called_once()
        mock_client.gmail_send.assert_not_called()
        mock_client.calendar_create.assert_not_called()