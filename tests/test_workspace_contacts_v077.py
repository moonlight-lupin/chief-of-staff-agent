#!/usr/bin/env python3
"""v0.7.7 — contacts writes against the google_api.py that is actually installed.

Field report (plugin v0.7.6, provider google_api, SA + domain-wide
delegation): v0.7.1's contacts.create/update/delete shelled out to
``google_api.py contacts create|update|delete``, but the shipped
google-workspace skill's script offers only ``contacts list``. doctor was
green, the approval chain ran, and the action failed at the last step with
``invalid choice: 'create' (choose from 'list')``. The v0.7.1/v0.7.3 tests
mocked ``_run``, so nothing ever checked the real CLI surface.

Now:
1. The installed script's contacts surface is probed (``contacts --help``,
   no credentials) — against a real subprocess here, not a mock.
2. With a service account configured, contacts writes go through the People
   API directly (the SA + delegation path drafts and calendar already use),
   so the external skill is no longer needed for them.
3. When neither path can do the write, it is refused *before* anything runs:
   in ``capabilities``, in ``doctor``, at approve time and at execute time,
   each with the reason.
4. A contact can carry several email addresses (``emails``).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "shared" / "scripts"))
sys.path.insert(0, str(PLUGIN_ROOT / "skills" / "document-preparer" / "scripts"))

FIXTURES = PLUGIN_ROOT / "tests" / "fixtures" / "google_api"
LIST_ONLY = FIXTURES / "contacts_list_only.py"
FULL = FIXTURES / "contacts_full.py"
WRITES = ("contacts.create", "contacts.update", "contacts.delete")
# Captured at import, before conftest sandboxes the environment: the contract
# test below reads (only reads) the operator's real google_api.py.
_OPERATOR_SCRIPT_ENV = {k: os.environ.get(k) for k in
                        ("GOOGLE_WORKSPACE_API", "CHIEF_OF_STAFF_HERMES_HOME", "HERMES_HOME")}


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    from providers import google_contacts
    google_contacts._PROBE_CACHE.clear()
    yield
    google_contacts._PROBE_CACHE.clear()


@pytest.fixture
def cli_log(tmp_path, monkeypatch):
    log = tmp_path / "google_api_calls.jsonl"
    monkeypatch.setenv("FAKE_GOOGLE_API_LOG", str(log))

    def calls() -> list[list[str]]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]
    return calls


def _config(tmp_path, *, service_account: bool) -> dict:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    google = {"delegate_email": "founder@test.com", "account_alias": "test", "domain": "test.com"}
    if service_account:
        sa = tmp_path / "sa.json"
        sa.write_text("{}")
        google["service_account_path"] = str(sa)
    return {
        "google": google,
        "integrations": {"workspace": {"provider": "google_api", "mode": "direct"}},
        "paths": {"project_root": str(project)},
    }


def _use_script(monkeypatch, script: Path) -> None:
    monkeypatch.setenv("GOOGLE_WORKSPACE_API", str(script))


def _client(config):
    from providers.google_workspace import GoogleWorkspaceClient
    return GoogleWorkspaceClient(config)


# ─── 1. probing the real CLI surface ─────────────────────────────────────────

class TestProbe:
    def test_shipped_skill_surface_is_list_only(self):
        from providers.google_contacts import probe_cli_contacts
        assert probe_cli_contacts(LIST_ONLY) == frozenset({"list"})

    def test_full_surface(self):
        from providers.google_contacts import probe_cli_contacts
        assert probe_cli_contacts(FULL) == frozenset({"list", "create", "update", "delete"})

    def test_probe_runs_help_only(self, cli_log):
        from providers.google_contacts import probe_cli_contacts
        probe_cli_contacts(LIST_ONLY)
        assert cli_log() == [["contacts", "--help"]]

    def test_unreadable_surface_is_unknown(self, tmp_path):
        from providers.google_contacts import probe_cli_contacts
        broken = tmp_path / "google_api.py"
        broken.write_text("import sys; sys.exit(3)\n")
        assert probe_cli_contacts(broken) is None
        assert probe_cli_contacts(tmp_path / "missing.py") is None

    def test_result_is_cached_until_the_script_changes(self, tmp_path, cli_log):
        from providers.google_contacts import probe_cli_contacts
        script = tmp_path / "google_api.py"
        script.write_text(LIST_ONLY.read_text())
        probe_cli_contacts(script)
        probe_cli_contacts(script)
        assert len(cli_log()) == 1
        script.write_text(FULL.read_text())
        assert probe_cli_contacts(script) == frozenset({"list", "create", "update", "delete"})

    def test_installed_skill_contract(self, monkeypatch):
        """Runs the REAL google-workspace skill's script when one is installed.

        No credentials needed — ``contacts --help`` only. Whatever it offers,
        the probe must be able to read it, or the plugin cannot tell what it
        may do.
        """
        from providers.google_contacts import probe_cli_contacts
        from providers.google_workspace import _find_google_api_script
        for key, value in _OPERATOR_SCRIPT_ENV.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        try:
            script = _find_google_api_script()
        except FileNotFoundError:
            pytest.skip("google-workspace skill not installed")
        surface = probe_cli_contacts(script)
        assert surface is not None and "list" in surface


# ─── 2. which backend performs a write ───────────────────────────────────────

class TestBackend:
    @pytest.mark.parametrize("action", WRITES)
    def test_service_account_uses_people_api(self, tmp_path, action):
        from providers.google_contacts import contacts_write_backend
        backend, _ = contacts_write_backend(_config(tmp_path, service_account=True), LIST_ONLY, action)
        assert backend == "rest"

    @pytest.mark.parametrize("action", WRITES)
    def test_list_only_script_without_service_account_is_refused(self, tmp_path, action):
        from providers.google_contacts import contacts_write_backend
        backend, reason = contacts_write_backend(_config(tmp_path, service_account=False), LIST_ONLY, action)
        op = action.split(".")[1]
        assert backend is None
        assert f"contacts {op}" in reason
        assert "offers: list" in reason
        assert "service_account_path" in reason

    @pytest.mark.parametrize("action", WRITES)
    def test_full_script_uses_the_cli(self, tmp_path, action):
        from providers.google_contacts import contacts_write_backend
        assert contacts_write_backend(_config(tmp_path, service_account=False), FULL, action)[0] == "cli"

    def test_unknown_surface_still_tries_the_cli(self, tmp_path):
        from providers.google_contacts import contacts_write_backend
        backend, _ = contacts_write_backend(_config(tmp_path, service_account=False),
                                            tmp_path / "missing.py", "contacts.create")
        assert backend == "cli"

    def test_missing_service_account_file_does_not_count(self, tmp_path):
        from providers.google_contacts import contacts_write_backend
        config = _config(tmp_path, service_account=False)
        config["google"]["service_account_path"] = str(tmp_path / "nope.json")
        backend, reason = contacts_write_backend(config, LIST_ONLY, "contacts.create")
        assert backend is None and "nope.json" in reason


# ─── 3a. the field failure, end to end: refused before anything runs ─────────

def _queue(config, action_type, target, payload):
    from state_db import create_pending_action
    return create_pending_action(config=config, action_type=action_type, provider="google_api",
                                 target=target, payload=payload, summary=f"test {action_type}")["id"]


def _approve(config, action_id):
    from state_db import approve_pending_action
    return approve_pending_action(config, action_id, approver="tester", reason="test")


def _execute(config, action_id):
    import webhook_events
    with patch("webhook_events.load_config", return_value=config):
        return webhook_events.main(["execute", "--action-id", action_id])


class TestFieldFailure:
    def test_client_reports_the_writes_unsupported(self, tmp_path, monkeypatch):
        _use_script(monkeypatch, LIST_ONLY)
        client = _client(_config(tmp_path, service_account=False))
        assert client.supports("contacts.list")
        for action in WRITES:
            assert not client.supports(action)
            assert "contacts " + action.split(".")[1] in client.unsupported_reason(action)

    def test_execute_refuses_without_invoking_the_subcommand(self, tmp_path, monkeypatch, cli_log):
        from state_db import get_pending_action
        _use_script(monkeypatch, LIST_ONLY)
        config = _config(tmp_path, service_account=False)
        action_id = _queue(config, "contacts.create", "Jane", {"given_name": "Jane"})
        _approve(config, action_id)
        assert _execute(config, action_id) == 1
        action = get_pending_action(config, action_id)
        assert action["state"] != "executed"
        assert "contacts create" in action["last_error"]
        assert "service_account_path" in action["last_error"]
        assert not [c for c in cli_log() if "create" in c]

    def test_approve_refuses_with_the_reason(self, tmp_path, monkeypatch, capsys):
        import review_queue
        from state_db import get_pending_action
        _use_script(monkeypatch, LIST_ONLY)
        config = _config(tmp_path, service_account=False)
        action_id = _queue(config, "contacts.delete", "people/1", {"person_id": "people/1"})
        with patch("review_queue._load_config_or_exit", return_value=config):
            rc = review_queue._main(["approve", "--action-id", action_id, "--approver", "t", "--reason", "r"])
        assert rc == 1
        assert "contacts delete" in capsys.readouterr().err
        assert get_pending_action(config, action_id)["state"] == "requested"

    def test_approve_still_works_when_the_install_can_do_it(self, tmp_path, monkeypatch):
        import review_queue
        from state_db import get_pending_action
        _use_script(monkeypatch, LIST_ONLY)
        config = _config(tmp_path, service_account=True)
        action_id = _queue(config, "contacts.delete", "people/1", {"person_id": "people/1"})
        with patch("review_queue._load_config_or_exit", return_value=config):
            rc = review_queue._main(["approve", "--action-id", action_id, "--approver", "t", "--reason", "r"])
        assert rc == 0
        assert get_pending_action(config, action_id)["state"] == "approved"

    def test_capabilities_lists_the_refusal(self, tmp_path, monkeypatch):
        from capability_report import build_capability_report
        _use_script(monkeypatch, LIST_ONLY)
        report = build_capability_report(_config(tmp_path, service_account=False))
        for action in WRITES:
            assert action in report["unsupported"] and action not in report["supported"]
            assert "offers: list" in report["unsupported_reasons"][action]
        assert "contacts.list" in report["supported"]

    def test_capabilities_with_service_account(self, tmp_path, monkeypatch):
        from capability_report import build_capability_report
        _use_script(monkeypatch, LIST_ONLY)
        report = build_capability_report(_config(tmp_path, service_account=True))
        assert set(WRITES) <= set(report["supported"])
        assert report["contacts_write_backend"] == {a: "rest" for a in WRITES}

    def test_doctor_warns(self, tmp_path, monkeypatch):
        from doctor_base import _check_google_contacts
        _use_script(monkeypatch, LIST_ONLY)
        result = _check_google_contacts(False, _config(tmp_path, service_account=False), Path("c"))
        assert result.name == "google_contacts" and result.status == "warn"
        assert "contacts create" in result.detail

    def test_doctor_passes_with_service_account(self, tmp_path, monkeypatch):
        from doctor_base import _check_google_contacts
        _use_script(monkeypatch, LIST_ONLY)
        result = _check_google_contacts(False, _config(tmp_path, service_account=True), Path("c"))
        assert result.status == "pass" and "People API" in result.detail

    def test_doctor_not_applicable_to_other_providers(self, tmp_path):
        from doctor_base import _check_google_contacts
        config = _config(tmp_path, service_account=False)
        config["integrations"]["workspace"]["provider"] = "agent"
        assert _check_google_contacts(False, config, Path("c")).status == "pass"

    def test_doctor_registers_the_check(self):
        import doctor_base
        assert "_check_google_contacts" in [c.__name__ for c in doctor_base.CHECKS]


# ─── 3b. the CLI path still works where the CLI can do it ────────────────────

def test_full_cli_create_runs_the_real_subprocess(tmp_path, monkeypatch, cli_log):
    from state_db import get_pending_action
    _use_script(monkeypatch, FULL)
    config = _config(tmp_path, service_account=False)
    action_id = _queue(config, "contacts.create", "Jane", {"given_name": "Jane", "email": "j@example.com"})
    _approve(config, action_id)
    assert _execute(config, action_id) == 0
    assert get_pending_action(config, action_id)["state"] == "executed"
    creates = [c for c in cli_log() if "create" in c]
    assert creates and "--email" in creates[0]


def test_cli_cannot_take_several_emails(tmp_path, monkeypatch, cli_log):
    _use_script(monkeypatch, FULL)
    monkeypatch.setenv("CHIEF_OF_STAFF_AUTO_APPROVE", "1")
    client = _client(_config(tmp_path, service_account=False))
    result = client.contacts_create(given_name="Jane", emails=["a@example.com", "b@example.com"])
    assert result["success"] is False
    assert "service_account_path" in result["error"]
    assert not [c for c in cli_log() if "create" in c]


# ─── 2b. People API REST ─────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.content = json.dumps(self._body).encode()
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


@pytest.fixture
def rest(tmp_path, monkeypatch, cli_log):
    """A REST-backed client with the HTTP layer and SA token faked."""
    _use_script(monkeypatch, LIST_ONLY)
    monkeypatch.setenv("CHIEF_OF_STAFF_AUTO_APPROVE", "1")
    monkeypatch.setenv("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", "1")
    creds = MagicMock(token="tok")
    http = MagicMock()
    with patch("providers.google_workspace._sa_credentials", return_value=creds) as sa, \
         patch("requests.post", side_effect=lambda *a, **k: http.post(*a, **k)), \
         patch("requests.get", side_effect=lambda *a, **k: http.get(*a, **k)), \
         patch("requests.patch", side_effect=lambda *a, **k: http.patch(*a, **k)), \
         patch("requests.delete", side_effect=lambda *a, **k: http.delete(*a, **k)):
        yield type("Rest", (), {"client": _client(_config(tmp_path, service_account=True)),
                                "http": http, "sa": sa, "cli": cli_log})


EXISTING = {
    "resourceName": "people/c9", "etag": "etag-1",
    "names": [{"givenName": "Jane", "familyName": "Doe", "metadata": {"primary": True}}],
    "emailAddresses": [{"value": "old@example.com", "type": "work", "metadata": {"primary": True}},
                       {"value": "home@example.com"}],
    "phoneNumbers": [{"value": "+65 1"}],
    "organizations": [{"name": "OldCo", "title": "CFO"}],
}


class TestPeopleApi:
    def test_create(self, rest):
        rest.http.post.return_value = _Resp(200, {"resourceName": "people/c1", "etag": "e"})
        result = rest.client.contacts_create(
            given_name="Jane", family_name="Doe", email="a@example.com",
            emails=["b@example.com", "a@example.com"], phone="+65 9", organization="Acme",
            note="met at expo")
        assert result["success"] is True and result["data"]["resourceName"] == "people/c1"
        url = rest.http.post.call_args[0][0]
        body = rest.http.post.call_args[1]["json"]
        assert url.endswith("/people:createContact")
        assert body["names"] == [{"givenName": "Jane", "familyName": "Doe"}]
        assert [e["value"] for e in body["emailAddresses"]] == ["a@example.com", "b@example.com"]
        assert body["phoneNumbers"] == [{"value": "+65 9"}]
        assert body["organizations"] == [{"name": "Acme"}]
        assert body["biographies"][0]["value"] == "met at expo"
        assert rest.http.post.call_args[1]["headers"]["Authorization"] == "Bearer tok"
        assert "https://www.googleapis.com/auth/contacts" in rest.sa.call_args[1]["scopes"]
        assert not [c for c in rest.cli() if "create" in c], "REST path must not shell out"

    def test_update_touches_only_the_supplied_fields(self, rest):
        rest.http.get.return_value = _Resp(200, EXISTING)
        rest.http.patch.return_value = _Resp(200, {"resourceName": "people/c9"})
        result = rest.client.contacts_update(person_id="c9", email="new@example.com", given_name="Janet")
        assert result["success"] is True
        url = rest.http.patch.call_args[0][0]
        params = rest.http.patch.call_args[1]["params"]
        body = rest.http.patch.call_args[1]["json"]
        assert url.endswith("/people/c9:updateContact")
        assert set(params["updatePersonFields"].split(",")) == {"names", "emailAddresses"}
        assert body["etag"] == "etag-1"
        assert body["names"][0] == {"givenName": "Janet", "familyName": "Doe"}
        assert [e["value"] for e in body["emailAddresses"]] == ["new@example.com", "home@example.com"]
        assert body["emailAddresses"][0]["type"] == "work", "keep the entry's type"
        assert "metadata" not in json.dumps(body)
        assert "phoneNumbers" not in body and "organizations" not in body

    def test_update_emails_replaces_the_list(self, rest):
        rest.http.get.return_value = _Resp(200, EXISTING)
        rest.http.patch.return_value = _Resp(200, {"resourceName": "people/c9"})
        rest.client.contacts_update(person_id="people/c9", emails=["x@example.com", "y@example.com"])
        body = rest.http.patch.call_args[1]["json"]
        assert [e["value"] for e in body["emailAddresses"]] == ["x@example.com", "y@example.com"]

    def test_update_organization_keeps_the_title(self, rest):
        rest.http.get.return_value = _Resp(200, EXISTING)
        rest.http.patch.return_value = _Resp(200, {"resourceName": "people/c9"})
        rest.client.contacts_update(person_id="people/c9", organization="NewCo")
        assert rest.http.patch.call_args[1]["json"]["organizations"] == [{"name": "NewCo", "title": "CFO"}]

    def test_delete(self, rest):
        rest.http.delete.return_value = _Resp(200, {})
        result = rest.client.contacts_delete(person_id="people/c9")
        assert result["success"] is True
        assert rest.http.delete.call_args[0][0].endswith("/people/c9:deleteContact")
        assert result["data"]["person_id"] == "people/c9"

    def test_http_error_is_a_failure_not_a_success(self, rest):
        rest.http.post.return_value = _Resp(403, {"error": {"message": "insufficient scope"}})
        result = rest.client.contacts_create(given_name="Jane")
        assert result["success"] is False and "403" in result["error"]

    def test_guardrail_still_applies(self, rest, monkeypatch):
        import io
        monkeypatch.delenv("CHIEF_OF_STAFF_AUTO_APPROVE")
        monkeypatch.setattr(sys, "stdin", io.StringIO())
        result = rest.client.contacts_create(given_name="Jane")
        assert result["success"] is False
        rest.http.post.assert_not_called()
        rest.sa.assert_not_called()


# ─── 4. several emails through the queue ─────────────────────────────────────

def test_queued_emails_reach_the_provider(tmp_path, monkeypatch):
    from state_db import get_pending_action
    _use_script(monkeypatch, LIST_ONLY)
    config = _config(tmp_path, service_account=True)
    action_id = _queue(config, "contacts.create", "Jane",
                       {"given_name": "Jane", "emails": ["a@example.com", "b@example.com"]})
    _approve(config, action_id)
    http = MagicMock(return_value=_Resp(200, {"resourceName": "people/c1"}))
    with patch("providers.google_workspace._sa_credentials", return_value=MagicMock(token="t")), \
         patch("requests.post", http):
        assert _execute(config, action_id) == 0
    assert [e["value"] for e in http.call_args[1]["json"]["emailAddresses"]] == ["a@example.com", "b@example.com"]
    assert get_pending_action(config, action_id)["state"] == "executed"


def test_preview_names_emails_without_values():
    import review_queue
    effect = review_queue._expected_effect("contacts.update", "people/1",
                                           {"person_id": "people/1", "emails": ["secret@example.com"]})
    assert "emails" in effect and "secret@example.com" not in effect


def test_suite_never_sees_the_operators_google_credentials():
    """With GOOGLE_SERVICE_ACCOUNT_PATH set, contacts tests would reach the live
    account through the People API path."""
    assert "GOOGLE_SERVICE_ACCOUNT_PATH" not in os.environ
    assert "GOOGLE_WORKSPACE_API" not in os.environ
