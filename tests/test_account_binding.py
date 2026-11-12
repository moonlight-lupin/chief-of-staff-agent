"""Actions are bound to the account (and provider) they will act as.

Threat (P0.2): company.yaml changes between approval and execution, or an
agent acts from the wrong connector account, and a different mailbox,
calendar, drive or Notion workspace is written to than the one approved.

* At creation every workspace action records ``binding`` = the account the
  company declares for its family and the configured workspace provider; the
  binding is part of the approval fingerprint (new actions only, so existing
  approvals keep their fingerprint).
* Before execution the binding is re-derived from the live config; any drift
  is refused (``binding_changed``) before the side effect.
* The Python path asks the client who it acts as, where that is knowable,
  and refuses a mismatch before the provider call.
* The agent path must state its account at ``claim`` when one is declared,
  and a mismatch is refused before the action is marked executing.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "shared" / "scripts"))

import company_registry as reg  # noqa: E402
import config_loader  # noqa: E402
import state_db  # noqa: E402
from test_notion_mirror import _config  # noqa: E402

ACCOUNTS = """
accounts:
  mail: "ops@acme.example"
  notion: "acme-workspace"
"""


@pytest.fixture
def cfg(tmp_path):
    base = _config(tmp_path)
    path = Path(base.source_path)
    path.write_text(path.read_text() + ACCOUNTS)
    return config_loader.load_config(path)


def _reload(cfg, text_change):
    path = Path(cfg.source_path)
    path.write_text(text_change(path.read_text()))
    return config_loader.load_config(path)


def _audit(cfg) -> str:
    root = config_loader.get_project_root(cfg)
    return "".join(p.read_text(errors="ignore") for p in root.rglob("*") if p.is_file() and "audit" in str(p))


FAMILIES = [("gmail.send", "ops@acme.example"), ("calendar.create", "ops@acme.example"),
            ("drive.upload", "ops@acme.example"), ("notion.db.upsert", "acme-workspace")]


@pytest.mark.parametrize("action_type,account", FAMILIES)
def test_new_actions_record_their_binding(cfg, action_type, account):
    action = state_db.create_pending_action(cfg, action_type, "google_api", "t", {"a": 1})
    assert action["binding"]["account"] == account


def test_binding_is_part_of_the_fingerprint(cfg):
    action = state_db.create_pending_action(cfg, "gmail.send", "google_api", "x@example.com", {"a": 1})
    other = {**action, "binding": {**action["binding"], "account": "ops@beta.example"}}
    assert state_db.approval_hash(action) != state_db.approval_hash(other)


def test_legacy_rows_without_a_binding_keep_their_fingerprint():
    base = {"type": "gmail.send", "target": "x", "payload": {"a": 1}}
    assert state_db.approval_hash(base) == state_db.approval_hash({**base, "binding": None})


@pytest.mark.parametrize("action_type,_acct", FAMILIES)
def test_account_drift_after_approval_is_refused_before_execution(cfg, action_type, _acct):
    action = state_db.create_pending_action(cfg, action_type, "google_api", "t", {"a": 1})
    assert state_db.approve_pending_action(cfg, action["id"], approver="op", reason="ok")
    drifted = _reload(cfg, lambda t: t.replace("ops@acme.example", "ops@beta.example")
                      .replace("acme-workspace", "beta-workspace"))
    assert state_db.mark_executing(drifted, action["id"]) is None
    assert state_db.get_pending_action(drifted, action["id"])["state"] == "approved"
    assert "binding_changed" in _audit(drifted)


def test_provider_drift_after_approval_is_refused(cfg):
    action = state_db.create_pending_action(cfg, "gmail.send", "google_api", "t", {"a": 1})
    state_db.approve_pending_action(cfg, action["id"], approver="op", reason="ok")
    drifted = _reload(cfg, lambda t: t + "\nintegrations:\n  workspace:\n    provider: m365\n")
    assert state_db.mark_executing(drifted, action["id"]) is None


def test_unchanged_config_executes(cfg):
    action = state_db.create_pending_action(cfg, "gmail.send", "google_api", "t", {"a": 1})
    state_db.approve_pending_action(cfg, action["id"], approver="op", reason="ok")
    assert state_db.mark_executing(cfg, action["id"])["state"] == "executing"


def test_local_actions_are_not_bound(cfg):
    action = state_db.create_pending_action(cfg, "pipeline.deal.add", "local", "deal", {"a": 1})
    assert action.get("binding") is None


# ------------------------------------------------------ Python path pre-flight


class FakeClient:
    provider_name = "google_api"

    def __init__(self, identity):
        self._identity = identity

    def acting_identity(self, family):
        return self._identity


@pytest.mark.parametrize("identity,refused", [
    ("ops@acme.example", False), ("OPS@acme.example", False),
    ("ops@beta.example", True), (None, False),          # unknowable: allowed, but visible as unverified
])
def test_preflight_compares_the_clients_identity(cfg, identity, refused):
    action = state_db.create_pending_action(cfg, "gmail.send", "google_api", "t", {"a": 1})
    error = reg.preflight_account(action, FakeClient(identity))
    assert (error is not None) == refused
    if refused:
        assert "ops@beta.example" in error and "ops@acme.example" in error


def test_google_client_reports_its_delegate(cfg, monkeypatch):
    from providers import google_workspace
    from providers.google_workspace import GoogleWorkspaceClient

    monkeypatch.setattr(google_workspace, "_find_google_api_script", lambda: Path("/nonexistent/google_api.py"))

    client = GoogleWorkspaceClient({**cfg, "google": {**(cfg.get("google") or {}),
                                                      "delegate_email": "ops@acme.example"}})
    assert client.acting_identity("mail") == "ops@acme.example"


def test_m365_client_credentials_reports_its_mailbox():
    from providers.m365_graph import M365GraphClient

    client = M365GraphClient({"m365": {"auth": "client_credentials", "tenant_id": "t", "client_id": "c",
                                       "user_principal": "ops@acme.example"}})
    assert client.acting_identity("mail") == "ops@acme.example"
    device = M365GraphClient({"m365": {"auth": "device_code", "tenant_id": "t", "client_id": "c"}})
    assert device.acting_identity("mail") is None      # whoever signed in: not knowable offline


def test_no_hardcoded_account_alias_heuristic(cfg, monkeypatch):
    from providers import google_workspace
    from providers.google_workspace import GoogleWorkspaceClient

    monkeypatch.setattr(google_workspace, "_find_google_api_script", lambda: Path("/nonexistent/google_api.py"))

    client = GoogleWorkspaceClient({**cfg, "google": {"service_account_path": "/keys/phronesis-sa.json"}})
    assert client.account_alias == ""


def test_webhook_execute_refuses_a_mismatched_client_before_the_write(cfg, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "webhook_events_t", PLUGIN_ROOT / "skills/document-preparer/scripts/webhook_events.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    action = state_db.create_pending_action(cfg, "gmail.send", "google_api", "x@example.com",
                                            {"to": "x@example.com", "subject": "s", "body": "b"})
    state_db.approve_pending_action(cfg, action["id"], approver="op", reason="ok")
    sent = []
    client = FakeClient("ops@beta.example")
    client.mail_send = lambda **kw: sent.append(kw) or {"success": True}
    import workspace_client

    monkeypatch.setattr(workspace_client, "get_workspace_client", lambda _c: client)
    monkeypatch.setattr(mod, "load_config", lambda *_a, **_k: cfg, raising=False)
    rc = mod.cmd_execute(types.SimpleNamespace(action_id=action["id"], config=str(cfg.source_path),
                                               summary=False, json=False))
    assert rc != 0 and sent == []
    assert state_db.get_pending_action(cfg, action["id"])["state"] == "failed" or \
        state_db.get_pending_action(cfg, action["id"])["last_error"]


# --------------------------------------------------------- agent path pre-flight


def _approved(cfg, action_type="mail.send"):
    action = state_db.create_pending_action(cfg, action_type, "agent", "x@example.com", {"s": 1})
    state_db.approve_pending_action(cfg, action["id"], approver="op", reason="ok")
    return action["id"]


def _claim(cfg, *extra):
    import review_queue

    return review_queue._main(["--config", str(cfg.source_path), "claim", "--action-id", *extra])


def test_claim_requires_the_executor_account_when_one_is_declared(cfg, capsys):
    action_id = _approved(cfg)
    assert _claim(cfg, action_id) != 0
    assert "--executor-account" in capsys.readouterr().err
    assert state_db.get_pending_action(cfg, action_id)["state"] == "approved"


def test_claim_from_the_wrong_account_is_refused_before_executing(cfg, capsys):
    action_id = _approved(cfg)
    assert _claim(cfg, action_id, "--executor-account", "ops@beta.example") != 0
    assert state_db.get_pending_action(cfg, action_id)["state"] == "approved"
    assert "account_mismatch" in _audit(cfg)


def test_claim_from_the_declared_account_proceeds(cfg, capsys):
    action_id = _approved(cfg)
    assert _claim(cfg, action_id, "--executor-account", "OPS@acme.example") == 0
    assert json.loads(capsys.readouterr().out)["expected_account"] == "ops@acme.example"


def test_claim_without_a_declared_account_needs_nothing(tmp_path, capsys):
    plain = _config(tmp_path)
    action_id = _approved(plain)
    assert _claim(plain, action_id) == 0


def test_record_execution_compares_with_the_approved_binding(cfg, capsys):
    import review_queue

    action_id = _approved(cfg)
    _claim(cfg, action_id, "--executor-account", "ops@acme.example")
    drifted = _reload(cfg, lambda t: t.replace("ops@acme.example", "ops@beta.example"))
    review_queue._main(["--config", str(drifted.source_path), "record-execution", "--action-id", action_id,
                        "--status", "success", "--executor-account", "ops@acme.example"])
    assert state_db.get_pending_action(cfg, action_id)["result"]["account_check"] == "match"


# ------------------------------------------------------------------- registry


def test_registry_refuses_two_companies_declaring_the_same_account(tmp_path):
    reg_path = tmp_path / "companies.yaml"
    for slug in ("acme", "beta"):
        d = tmp_path / slug
        d.mkdir()
        (d / "company.yaml").write_text(
            f"company:\n  id: {slug}\n  name: {slug}\npaths:\n  project_root: {d / 'data'}\n"
            f"accounts:\n  mail: shared@example.com\n")
    reg_path.write_text("companies:\n" + "".join(
        f"  {s}:\n    config: {tmp_path / s / 'company.yaml'}\n" for s in ("acme", "beta")))
    with pytest.raises(reg.RegistryError, match="shared@example.com"):
        reg.load_registry(reg_path)


def test_doctor_warns_when_executions_were_unverified(cfg, capsys):
    import doctor_base
    import review_queue

    action_id = _approved(cfg)
    _claim(cfg, action_id, "--executor-account", "ops@acme.example")
    review_queue._main(["--config", str(cfg.source_path), "record-execution", "--action-id", action_id,
                        "--status", "success"])
    result = doctor_base._check_executor_accounts(False, dict(cfg), Path(cfg.source_path))
    assert result.status == "warn" and action_id in result.detail
