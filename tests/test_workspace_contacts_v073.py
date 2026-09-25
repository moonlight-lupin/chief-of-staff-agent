#!/usr/bin/env python3
"""v0.7.3 — contacts follow-ups from the v0.7.1 review.

1. Approved contacts actions could never execute: the review-queue execute
   router had no contacts.* branches ("Unknown action type").
2. contacts.update was documented as gated even under
   CHIEF_OF_STAFF_AUTO_APPROVE, but a non-interactive auto-approved run let it
   straight through. It now needs the destructive dual gate
   (AUTO_APPROVE + ALLOW_DESTRUCTIVE), which the approved-execute path sets.
3. The v0.7.1 tests replaced confirm_action with a stub, so reclassifying
   contacts.delete as a read passed the whole suite. These tests pin the set
   membership and run the REAL confirm_action across the flag combinations.
5. Refusal reasons for providers without contacts were generic and
   recommended a provider that also refuses.
6. The preview never said a contact delete is permanent.
8. A real work address sat in a v0.7.1 test fixture.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "skills" / "document-preparer" / "scripts"))

import workspace_guardrails as wg  # noqa: E402

CONTACT_WRITES = ("contacts.create", "contacts.update", "contacts.delete")
NO_CONTACTS_PROVIDERS = ("composio", "composio:mcp", "composio_microsoft",
                         "composio_microsoft:mcp", "m365", "agent")


@pytest.fixture
def no_tty(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO())


@pytest.fixture
def flags(monkeypatch):
    def _set(auto: bool, destructive: bool):
        for name, on in (("CHIEF_OF_STAFF_AUTO_APPROVE", auto),
                         ("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", destructive)):
            if on:
                monkeypatch.setenv(name, "1")
            else:
                monkeypatch.delenv(name, raising=False)
    return _set


@pytest.fixture
def google_config(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    return {
        "google": {"delegate_email": "founder@test.com", "account_alias": "test",
                   "domain": "test.com"},
        "integrations": {"workspace": {"provider": "google_api", "mode": "direct"}},
        "paths": {"project_root": str(project)},
    }


@pytest.fixture
def client(google_config):
    from providers.google_workspace import GoogleWorkspaceClient
    with patch("providers.google_workspace._find_google_api_script",
               return_value=Path("/fake/google_api.py")):
        return GoogleWorkspaceClient(google_config)


# ─── 3. classification is pinned, and the REAL gate is exercised ────────────

class TestClassification:
    def test_list_is_the_only_read(self):
        assert "contacts.list" in wg.READ_ACTIONS
        for action in CONTACT_WRITES:
            assert action in wg.WRITE_ACTIONS
            assert action not in wg.READ_ACTIONS

    def test_only_create_is_a_safe_write(self):
        assert "contacts.create" in wg.SAFE_WRITE_ACTIONS
        assert "contacts.update" not in wg.SAFE_WRITE_ACTIONS
        assert "contacts.delete" not in wg.SAFE_WRITE_ACTIONS

    def test_update_and_delete_need_the_destructive_gate(self):
        assert "contacts.update" in wg.DESTRUCTIVE_ACTIONS
        assert "contacts.delete" in wg.DESTRUCTIVE_ACTIONS


@pytest.mark.parametrize("auto,destructive,expected", [
    (False, False, {"contacts.create": False, "contacts.update": False, "contacts.delete": False}),
    (True, False, {"contacts.create": True, "contacts.update": False, "contacts.delete": False}),
    (False, True, {"contacts.create": False, "contacts.update": False, "contacts.delete": False}),
    (True, True, {"contacts.create": True, "contacts.update": True, "contacts.delete": True}),
])
def test_real_confirm_action_across_flags(no_tty, flags, auto, destructive, expected):
    """Non-interactive runs, as in cron, CI and the approved-execute path."""
    flags(auto, destructive)
    got = {a: wg.confirm_action(a, target="people/1") for a in CONTACT_WRITES}
    assert got == expected


def test_auto_approve_alone_cannot_overwrite_a_contact(client, no_tty, flags):
    flags(True, False)
    with patch.object(client, "_run") as run:
        result = client.contacts_update(person_id="people/123", phone="+65 1")
    assert result["success"] is False
    run.assert_not_called()


def test_blocked_attempt_is_audited_as_blocked(client, no_tty, flags):
    flags(False, False)
    calls = []
    with patch("workspace_audit.audit_workspace_action",
               side_effect=lambda *a, **k: calls.append(k)), \
         patch.object(client, "_run") as run:
        result = client.contacts_delete(person_id="people/123")
    assert result["success"] is False
    run.assert_not_called()
    assert calls and calls[0]["status"] == "blocked"
    assert calls[0]["target"] == "people/123"


# ─── 1. approved contacts actions execute through the queue ─────────────────

def _approved(config, action_type, target, payload):
    from state_db import approve_pending_action, create_pending_action
    action = create_pending_action(config=config, action_type=action_type, provider="google_api",
                                   target=target, payload=payload, summary=f"test {action_type}")
    approve_pending_action(config, action["id"], approver="tester", reason="test")
    return action["id"]


def _execute(config, client, action_id, run_result):
    import webhook_events
    with patch("webhook_events.load_config", return_value=config), \
         patch("workspace_client.get_workspace_client", return_value=client), \
         patch.object(client, "_run", return_value=run_result) as run:
        rc = webhook_events.main(["execute", "--action-id", action_id])
    return rc, run


CASES = [
    ("contacts.create", "Jane",
     {"given_name": "Jane", "family_name": "Doe", "email": "jane@example.com"},
     ["create", "--given-name", "Jane", "--email", "jane@example.com"]),
    ("contacts.update", "people/123",
     {"person_id": "people/123", "phone": "+65 9999 9999"},
     ["update", "--person-id", "people/123", "--phone", "+65 9999 9999"]),
    ("contacts.delete", "people/123",
     {"person_id": "people/123"},
     ["delete", "--person-id", "people/123"]),
]


@pytest.mark.parametrize("action_type,target,payload,expected_args", CASES,
                         ids=[c[0] for c in CASES])
def test_approved_contacts_action_executes(google_config, client, no_tty, flags,
                                           action_type, target, payload, expected_args):
    from state_db import get_pending_action
    flags(False, False)
    action_id = _approved(google_config, action_type, target, payload)
    ok = json.dumps({"resourceName": "people/123", "name": "Jane Doe"})
    rc, run = _execute(google_config, client, action_id, (0, ok, ""))
    assert rc == 0
    cmd = run.call_args[0][0]
    for arg in expected_args:
        assert arg in cmd
    assert get_pending_action(google_config, action_id)["state"] == "executed"


def test_update_falls_back_to_the_target_for_person_id(google_config, client, no_tty, flags):
    flags(False, False)
    action_id = _approved(google_config, "contacts.update", "people/777", {"email": "new@example.com"})
    rc, run = _execute(google_config, client, action_id, (0, json.dumps({"resourceName": "people/777"}), ""))
    assert rc == 0
    assert "people/777" in run.call_args[0][0]


def test_provider_failure_is_recorded_for_retry(google_config, client, no_tty, flags):
    """Below the retry cap a failure returns the action to 'approved' with the
    error and retry count recorded (state_db's retry contract)."""
    from state_db import get_pending_action
    flags(False, False)
    action_id = _approved(google_config, "contacts.delete", "people/123", {"person_id": "people/123"})
    rc, _ = _execute(google_config, client, action_id, (1, "", "404 not found"))
    assert rc == 1
    action = get_pending_action(google_config, action_id)
    assert action["state"] == "approved"
    assert action["retry_count"] == 1
    assert "404" in action["last_error"]


def test_execute_restores_the_gate_flags(google_config, client, no_tty, flags):
    import os
    flags(False, False)
    action_id = _approved(google_config, "contacts.update", "people/123",
                          {"person_id": "people/123", "phone": "+65 1"})
    _execute(google_config, client, action_id, (0, json.dumps({"resourceName": "people/123"}), ""))
    assert "CHIEF_OF_STAFF_AUTO_APPROVE" not in os.environ
    assert "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE" not in os.environ


# ─── 5. refusals explain themselves ──────────────────────────────────────────

@pytest.mark.parametrize("provider", NO_CONTACTS_PROVIDERS)
@pytest.mark.parametrize("action", ("contacts.list",) + CONTACT_WRITES)
def test_refusal_reason_is_specific(provider, action):
    from workspace_capabilities import get_unsupported_reason
    reason = get_unsupported_reason(provider, action)
    assert reason != f"{action} is not supported by {provider}"
    assert "google_api" in reason


@pytest.mark.parametrize("action", ("contacts.list",) + CONTACT_WRITES)
def test_recommendation_is_the_provider_that_supports_it(action):
    from workspace_capabilities import recommend_provider_for
    assert recommend_provider_for(action) == "google_api"


# ─── 6. preview tells the truth about reversibility ─────────────────────────

class TestPreview:
    def test_delete_is_declared_permanent(self):
        import review_queue
        effect = review_queue._expected_effect("contacts.delete", "people/123", {"person_id": "people/123"})
        hint = review_queue._reversal_hint("contacts.delete")
        assert "permanent" in effect.lower() and "people/123" in effect
        assert "no undo" in hint.lower()

    def test_update_says_prior_values_are_not_kept(self):
        import review_queue
        effect = review_queue._expected_effect("contacts.update", "people/123",
                                               {"person_id": "people/123", "phone": "+65 1", "email": ""})
        assert "phone" in effect and "+65 1" not in effect, "name the fields, not the values"
        assert "prior" in review_queue._reversal_hint("contacts.update").lower()

    def test_create_names_the_contact(self):
        import review_queue
        effect = review_queue._expected_effect("contacts.create", "Jane", {"given_name": "Jane"})
        assert "Jane" in effect
        assert "delete" in review_queue._reversal_hint("contacts.create").lower()

    @pytest.mark.parametrize("action", CONTACT_WRITES)
    def test_risk_explanation_is_specific(self, action):
        from action_risk import get_risk_explanation
        assert "contact" in get_risk_explanation(action, "high").lower()


# ─── 8. no real addresses in fixtures ────────────────────────────────────────

def test_v071_fixture_uses_a_placeholder_address():
    text = (PLUGIN_ROOT / "tests" / "test_workspace_contacts_v071.py").read_text(encoding="utf-8")
    assert "menghuey@" not in text
    assert "phronesis" not in text
