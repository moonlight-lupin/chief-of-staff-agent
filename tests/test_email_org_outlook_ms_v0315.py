#!/usr/bin/env python3
"""v0.3.15 — email-organisation classify → suggest → prepare on Composio MS."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
SKILL_SCRIPTS = PLUGIN_ROOT / "skills" / "email-organisation" / "scripts"
for p in (SHARED_SCRIPTS, SKILL_SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


@pytest.fixture
def temp_project():
    with tempfile.TemporaryDirectory() as d:
        os.environ["CHIEF_OF_STAFF_PROJECT_ROOT"] = d
        config = {
            "integrations": {
                "workspace": {
                    "provider": "composio",
                    "mode": "mcp",
                    "family": "microsoft",
                }
            },
            "paths": {"project_root": d},
        }
        yield config, Path(d)
        os.environ.pop("CHIEF_OF_STAFF_PROJECT_ROOT", None)


OUTLOOK_POLICY = {
    "version": 1,
    "mode": "use_existing_first",
    "status": "approved",
    "provider": "composio_microsoft:mcp",
    "categories": {
        "finance_invoice": {
            "preferred_label": "Finance/Invoices",
            "label_id": "Finance/Invoices",  # Outlook: id == displayName
            "confidence": 0.9,
            "aliases": [],
        },
    },
    "unmapped_labels": [],
    "new_label_policy": {"create_only_if": {"min_matching_emails": 5}},
    "safety": {"never_auto_apply": True},
}


class TestOutlookClassifySuggestPrepare:
    def test_classify_outlook_message_shape(self, temp_project):
        from email_classifier import classify_email
        from email_label_policy import save_approved_policy

        config, _ = temp_project
        save_approved_policy(config, dict(OUTLOOK_POLICY), approved_by="test")
        email = {
            "id": "AAMkAGOutlookMsg",
            "sender": "billing@vendor.com",
            "subject": "Invoice #4421 attached",
            "snippet": "Please find your invoice for March",
            "source": "outlook",
        }
        cls = classify_email(email, OUTLOOK_POLICY)
        assert cls["message_id"] == "AAMkAGOutlookMsg"
        assert cls["category"] == "finance_invoice"
        assert cls["label_id"] == "Finance/Invoices"
        assert cls["matched_policy_label"] == "Finance/Invoices"

    def test_suggest_uses_display_name_as_tag_id(self, temp_project):
        from email_classifier import classify_inbox, generate_org_suggestions
        from email_label_policy import save_approved_policy

        config, _ = temp_project
        save_approved_policy(config, dict(OUTLOOK_POLICY), approved_by="test")
        emails = [{
            "id": "AAMk-1",
            "sender": "ap@corp.com",
            "subject": "Invoice ready for payment",
            "snippet": "invoice total due",
        }]
        classified = classify_inbox(config, emails)
        assert classified["classified"] == 1
        result = generate_org_suggestions(config, dry_run=False)
        assert result["label_suggestions"] >= 1
        sug = result["details"][0]
        assert sug["action_type"] == "gmail.label"
        assert "category" in sug["title"].lower()
        assert sug["payload"]["label_id"] == "Finance/Invoices"
        assert sug["payload"]["message_id"] == "AAMk-1"

    def test_prepare_pending_on_composio_ms(self, temp_project):
        from email_classifier import (
            classify_inbox,
            generate_org_suggestions,
            prepare_pending_from_suggestion,
        )
        from email_label_policy import save_approved_policy

        config, project = temp_project
        save_approved_policy(config, dict(OUTLOOK_POLICY), approved_by="test")
        classify_inbox(config, [{
            "id": "AAMk-prep",
            "sender": "ap@corp.com",
            "subject": "Monthly invoice statement",
            "snippet": "invoice",
        }])
        sug_result = generate_org_suggestions(config)
        sug_id = sug_result["details"][0]["id"]

        mock = MagicMock()
        mock.provider_name = "composio_microsoft:mcp"
        mock.family = "microsoft"
        mock.supports.return_value = True

        with patch("workspace_client.get_workspace_client", return_value=mock):
            prepared = prepare_pending_from_suggestion(config, sug_id)
        assert prepared["success"] is True
        assert prepared.get("action_id")
        pending_path = project / ".pending_actions.json"
        assert pending_path.exists()
        data = json.loads(pending_path.read_text())
        action = next(iter(data["actions"].values()))
        assert action["type"] == "gmail.label"
        assert action["payload"]["label_id"] == "Finance/Invoices"
        assert action["payload"]["message_id"] == "AAMk-prep"
        # Never mutates mailbox during prepare.
        mock.mail_tag.assert_not_called()
        mock.mail_create_tag.assert_not_called()
        mock.mail_archive.assert_not_called()
