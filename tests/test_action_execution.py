"""The one approved-action execution path (P1.4).

send_email, delete_actions and webhook_events used to carry three copies of
claim → account pre-flight → capability gate → execution grant → call →
record. They now share ``action_execution.run_approved``. These tests pin its
contract; the executors' own suites pin their CLI behaviour.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
for sub in ("shared/scripts", "skills/document-preparer/scripts"):
    if str(ROOT / sub) not in sys.path:
        sys.path.insert(0, str(ROOT / sub))

import action_execution as ax  # noqa: E402
import state_db  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    return {"paths": {"project_root": str(tmp_path)}}


def _approved(cfg, action_type="gmail.send", target="x@example.com", payload=None):
    with state_db._open_db(cfg) as db:
        action = db.create_action(action_type, "google_api", target, payload or {"to": target})
    state_db.approve_pending_action(cfg, action["id"], approver="op")
    return state_db.get_pending_action(cfg, action["id"])


def _client(**kw):
    client = MagicMock(provider_name="google_api")
    client.supports.side_effect = lambda a: True
    for k, v in kw.items():
        setattr(client, k, v)
    return client


def _state(cfg, action):
    return state_db.get_pending_action(cfg, action["id"])["state"]


@pytest.fixture(autouse=True)
def _no_preflight_or_gate():
    with patch("company_registry.preflight", return_value=(None, {"checked": True})), \
         patch("workspace_capabilities.require_capability", return_value=None):
        yield


def test_success_records_executed_with_the_account_check(cfg):
    action = _approved(cfg)
    out = ax.run_approved(cfg, action, _client(), lambda c, a: {"success": True, "id": "m1"})
    assert out.ok and out.stage == "done"
    stored = state_db.get_pending_action(cfg, action["id"])
    assert stored["state"] == "executed"
    assert stored["result"]["account_check"] == {"checked": True}


def test_lapsed_or_unapproved_action_is_not_claimed(cfg):
    action = _approved(cfg)
    state_db.mark_executing(cfg, action["id"])          # someone else claimed it
    call = MagicMock()
    out = ax.run_approved(cfg, action, _client(), call)
    assert not out.ok and out.stage == "claim"
    call.assert_not_called()


def test_account_mismatch_refuses_before_the_call_and_stays_retryable(cfg):
    action = _approved(cfg)
    call = MagicMock()
    with patch("company_registry.preflight", return_value=("signed in as the wrong account", None)):
        out = ax.run_approved(cfg, action, _client(), call)
    assert out.stage == "preflight" and "wrong account" in out.error
    call.assert_not_called()
    assert _state(cfg, action) == "approved"


def test_unsupported_capability_refuses_before_the_call(cfg):
    action = _approved(cfg)
    call = MagicMock()
    with patch("workspace_capabilities.require_capability", return_value={"error": "no can do"}):
        out = ax.run_approved(cfg, action, _client(), call)
    assert out.stage == "capability" and out.error == "no can do"
    call.assert_not_called()
    assert _state(cfg, action) == "approved"


def test_capability_gate_can_be_skipped(cfg):
    action = _approved(cfg)
    with patch("workspace_capabilities.require_capability", return_value={"error": "x"}):
        out = ax.run_approved(cfg, action, _client(), lambda c, a: {"success": True}, check_capability=False)
    assert out.ok


def test_missing_grant_refuses_before_the_call(cfg):
    action = _approved(cfg)
    call = MagicMock()
    with patch("workspace_guardrails.execution_grant", side_effect=PermissionError("not signed")):
        out = ax.run_approved(cfg, action, _client(), call)
    assert out.stage == "grant"
    call.assert_not_called()
    assert _state(cfg, action) == "approved"


def test_refusal_inside_the_call_is_not_an_attempt(cfg):
    action = _approved(cfg)

    def call(client, a):
        raise ax.NotAttempted("payload path refused")
    out = ax.run_approved(cfg, action, _client(), call)
    assert out.stage == "refused" and _state(cfg, action) == "approved"


def test_exception_in_the_call_is_a_final_failure_for_a_send(cfg):
    action = _approved(cfg)

    def call(client, a):
        raise TimeoutError("gateway timeout")
    out = ax.run_approved(cfg, action, _client(), call)
    assert out.stage == "call" and "gateway timeout" in out.error
    assert _state(cfg, action) == "failed"


def test_provider_failure_result_is_recorded_as_failed(cfg):
    action = _approved(cfg)
    out = ax.run_approved(cfg, action, _client(), lambda c, a: {"success": False, "error": "quota"})
    assert out.stage == "provider" and out.error == "quota"
    assert _state(cfg, action) == "failed"


def test_non_dict_result_is_a_failure_unless_the_caller_accepts_it(cfg):
    a1 = _approved(cfg)
    assert ax.run_approved(cfg, a1, _client(), lambda c, a: None).stage == "provider"
    a2 = _approved(cfg, "gmail.archive", "m1", {"message_id": "m1"})
    out = ax.run_approved(cfg, a2, _client(), lambda c, a: "ok", accept_non_dict=True)
    assert out.ok and state_db.get_pending_action(cfg, a2["id"])["result"]["raw"] == "ok"


@pytest.mark.parametrize("outcome", ["ok", "raise", "refused"])
def test_break_glass_env_is_set_only_during_the_call_and_restored(cfg, monkeypatch, outcome):
    monkeypatch.setenv("CHIEF_OF_STAFF_AUTO_APPROVE", "previous")
    monkeypatch.delenv("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", raising=False)
    seen = {}

    def call(client, a):
        seen.update(auto=os.environ.get("CHIEF_OF_STAFF_AUTO_APPROVE"),
                    destr=os.environ.get("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"))
        if outcome == "raise":
            raise RuntimeError("boom")
        if outcome == "refused":
            raise ax.NotAttempted("no")
        return {"success": True}
    ax.run_approved(cfg, _approved(cfg), _client(), call)
    assert seen == {"auto": "1", "destr": "1"}
    assert os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] == "previous"
    assert "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE" not in os.environ


def test_grant_is_released_even_when_the_call_raises(cfg):
    grant = MagicMock()
    with patch("workspace_guardrails.execution_grant", return_value=grant):
        ax.run_approved(cfg, _approved(cfg), _client(), lambda c, a: 1 / 0)
    grant.__enter__.assert_called_once()
    grant.__exit__.assert_called_once()


# ── the executors use it, and a wrong-type action is never left executing ──

def test_executors_share_the_path():
    for name in ("send_email.py", "delete_actions.py", "webhook_events.py"):
        text = (ROOT / "skills/document-preparer/scripts" / name).read_text()
        assert "run_approved(" in text, name
        assert "execution_grant(" not in text, f"{name} still carries its own copy"


def test_delete_executor_refuses_a_wrong_type_before_claiming(cfg):
    import delete_actions

    action = _approved(cfg)   # a send, not a soft delete
    with patch("delete_actions.load_config", return_value=cfg):
        assert delete_actions.main(["execute", "--action-id", action["id"]]) == 1
    assert _state(cfg, action) == "approved"
