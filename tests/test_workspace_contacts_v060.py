#!/usr/bin/env python3
"""Contacts actions on the workspace client layer (v0.6.x).

Contract tests for the neutral contacts surface:
- workspace_client.WorkspaceClient declares contacts_list / contacts_create /
  contacts_update / contacts_delete (list is a read; create/update/delete are
  guarded writes).
- providers.google_workspace.GoogleWorkspaceClient implements all four via the
  google_api.py contacts subcommands (live-verified 2026-09-25 against the
  Phronesis Workspace account; see gws-service-account skill).
- workspace_capabilities marks contacts.* True for google_api, False for
  composio (the GOOGLECONTACTS toolkit is not wired — False until live-verified,
  per the capability tripwire convention).
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


# ── Interface surface ──────────────────────────────────────────────────────

class TestContactsInterface:
    def test_base_class_declares_contacts_list(self):
        import workspace_client
        assert hasattr(workspace_client.WorkspaceClient, "contacts_list")

    def test_base_class_declares_contacts_write_methods(self):
        import workspace_client
        for name in ("contacts_create", "contacts_update", "contacts_delete"):
            assert hasattr(workspace_client.WorkspaceClient, name), name

    def test_unimplemented_provider_raises_not_implemented(self):
        import workspace_client
        class BareClient(workspace_client.WorkspaceClient):
            def mail_search(self, query, max_results=10):
                return []
            def calendar_list(self, start, end):
                return []
            def files_search(self, query, max_results=10):
                return []
            def files_upload(self, file_path, parent_id=None):
                return {}
            def health_check(self):
                return True
        client = BareClient()
        with pytest.raises(NotImplementedError):
            client.contacts_create(given_name="Jane", family_name="Doe")


# ── google_api provider implementation ────────────────────────────────────

@pytest.fixture
def google_config():
    return {
        "google": {
            "service_account_path": "~/.hermes/secrets/phronesis_service_account.json",
            "domain": "phronesis-applied.com",
            "delegate_email": "menghuey@phronesis-applied.com",
            "account_alias": "phronesis",
        },
        "integrations": {"workspace": {"provider": "google_api", "mode": "direct"}},
    }


@pytest.fixture
def client(google_config):
    from providers.google_workspace import GoogleWorkspaceClient
    return GoogleWorkspaceClient(google_config)


class TestGoogleContactsList:
    def test_calls_contacts_list_and_parses_json(self, client):
        mock = [{"name": "Jane Doe", "emails": ["jane@example.com"], "phones": []}]
        with patch.object(client, "_run", return_value=(0, json.dumps(mock), "")) as run:
            result = client.contacts_list(max_results=20)
        assert result == mock
        cmd = run.call_args[0][0]
        assert "contacts" in cmd and "list" in cmd
        assert "--max" in cmd and "20" in cmd

    def test_returns_empty_list_on_error(self, client):
        with patch.object(client, "_run", return_value=(1, "", "auth failed")):
            assert client.contacts_list() == []

    def test_tolerates_empty_string_stdout(self, client):
        # google_api.py emits "" or "No messages found." style empties on zero
        # results; contacts_list must not crash on them.
        with patch.object(client, "_run", return_value=(0, "", "")):
            assert client.contacts_list() == []


class TestGoogleContactsCreate:
    def _approve(self, monkeypatch):
        monkeypatch.setattr(
            "workspace_guardrails.confirm_action", lambda action, **d: True
        )

    def test_create_builds_correct_command(self, client, monkeypatch):
        self._approve(monkeypatch)
        mock = {"resourceName": "people/123", "name": "Jane Doe",
                "emails": ["jane@example.com"], "phones": [], "organizations": [], "notes": []}
        with patch.object(client, "_run", return_value=(0, json.dumps(mock), "")) as run:
            result = client.contacts_create(
                given_name="Jane", family_name="Doe", email="jane@example.com"
            )
        assert result["success"] is True
        assert result["data"]["resourceName"] == "people/123"
        cmd = run.call_args[0][0]
        assert "contacts" in cmd and "create" in cmd
        assert "--given-name" in cmd and "Jane" in cmd
        assert "--family-name" in cmd and "Doe" in cmd
        assert "--email" in cmd and "jane@example.com" in cmd

    def test_create_blocked_by_guardrail(self, client, monkeypatch):
        monkeypatch.setattr(
            "workspace_guardrails.confirm_action", lambda action, **d: False
        )
        result = client.contacts_create(given_name="Jane", family_name="Doe")
        assert result["success"] is False

    def test_create_requires_name(self, client):
        # With no name, the guardrail target (given_name) is empty → default-deny
        # blocks BEFORE the body's ValueError. An invalid call must never reach
        # the API. The result is a blocked ActionResult, not a raise.
        result = client.contacts_create()
        assert result["success"] is False
        assert result["action"] == "contacts.create"


class TestGoogleContactsUpdate:
    def _approve(self, monkeypatch):
        monkeypatch.setattr(
            "workspace_guardrails.confirm_action", lambda action, **d: True
        )

    def test_update_builds_correct_command(self, client, monkeypatch):
        self._approve(monkeypatch)
        mock = {"resourceName": "people/123", "name": "Janet Doe",
                "emails": ["jane@example.com"], "phones": ["+65 9999 9999"],
                "organizations": [], "notes": []}
        with patch.object(client, "_run", return_value=(0, json.dumps(mock), "")) as run:
            result = client.contacts_update(
                person_id="people/123", phone="+65 9999 9999"
            )
        assert result["success"] is True
        cmd = run.call_args[0][0]
        assert "update" in cmd
        assert "--person-id" in cmd and "people/123" in cmd
        assert "--phone" in cmd and "+65 9999 9999" in cmd

    def test_update_blocked_by_guardrail(self, client, monkeypatch):
        monkeypatch.setattr(
            "workspace_guardrails.confirm_action", lambda action, **d: False
        )
        result = client.contacts_update(person_id="people/123", phone="+65")
        assert result["success"] is False

    def test_update_requires_person_id(self, client):
        result = client.contacts_update(person_id="", phone="+65 9999 9999")
        assert result["success"] is False
        assert result["action"] == "contacts.update"

    def test_update_requires_at_least_one_field(self, client, monkeypatch):
        # With approval granted, the body's ValueError surfaces in the error
        # ActionResult (zero-field update must never reach the API).
        monkeypatch.setattr(
            "workspace_guardrails.confirm_action", lambda action, **d: True
        )
        result = client.contacts_update(person_id="people/123")
        assert result["success"] is False
        assert "at least one field" in str(result.get("error", ""))


class TestGoogleContactsDelete:
    def _approve(self, monkeypatch):
        monkeypatch.setattr(
            "workspace_guardrails.confirm_action", lambda action, **d: True
        )

    def test_delete_builds_correct_command(self, client, monkeypatch):
        self._approve(monkeypatch)
        mock = {"status": "deleted", "person_id": "people/123"}
        with patch.object(client, "_run", return_value=(0, json.dumps(mock), "")) as run:
            result = client.contacts_delete(person_id="people/123")
        assert result["success"] is True
        cmd = run.call_args[0][0]
        assert "delete" in cmd
        assert "--person-id" in cmd and "people/123" in cmd

    def test_delete_blocked_by_guardrail(self, client, monkeypatch):
        monkeypatch.setattr(
            "workspace_guardrails.confirm_action", lambda action, **d: False
        )
        result = client.contacts_delete(person_id="people/123")
        assert result["success"] is False

    def test_delete_requires_person_id(self, client):
        result = client.contacts_delete(person_id="")
        assert result["success"] is False
        assert result["action"] == "contacts.delete"


# ── Capability matrix ─────────────────────────────────────────────────────

class TestContactsCapabilities:
    def test_google_api_contacts_capabilities_true(self):
        from workspace_capabilities import get_capabilities, supports
        caps = get_capabilities("google_api")
        assert caps["contacts.list"] is True
        assert caps["contacts.create"] is True      # live-verified 2026-09-25 (google_api.py contacts write, DWD contacts scope)
        assert caps["contacts.update"] is True      # live-verified 2026-09-25 (merge-safe fetch-etag update)
        assert caps["contacts.delete"] is True     # live-verified 2026-09-25
        assert supports("google_api", "contacts.create") is True

    def test_composio_contacts_capabilities_false_until_wired(self):
        # GOOGLECONTACTS toolkit is not wired into the composio provider; a
        # write stays False until a live run exercises it (tripwire convention).
        from workspace_capabilities import get_capabilities, supports, unsupported_actions
        caps = get_capabilities("composio")
        assert caps.get("contacts.create", False) is False
        assert caps.get("contacts.update", False) is False
        assert caps.get("contacts.delete", False) is False
        assert supports("composio", "contacts.create") is False
        assert "contacts.create" in unsupported_actions("composio")

    def test_contacts_actions_in_all_actions(self):
        from workspace_capabilities import all_actions
        actions = all_actions()
        assert "contacts.create" in actions
        assert "contacts.delete" in actions

    def test_contacts_delete_is_high_risk(self):
        # Permanent delete of a contact — high risk like the other deletes.
        from action_risk import get_action_risk
        assert get_action_risk("contacts.delete") == "high"
        assert get_action_risk("contacts.create") == "medium"
        assert get_action_risk("contacts.update") == "medium"
        assert get_action_risk("contacts.list") == "low"