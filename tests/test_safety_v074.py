#!/usr/bin/env python3
"""v0.7.4 — safety hardening from the post-v0.7.3 assessment.

Each class pins one verified finding:

1. Approval was not bound to the action: payload/target/type could change
   after approval, and a row inserted as 'approved' could be claimed.
   Approval now stores a hash of (type, target, payload); the executing
   transition refuses a mismatch or a missing hash.
2. send_email.py execute sent mail for ANY approved action type, and marked
   a failed send as executed.
3. doctor --fix / state_tools repair returned a stale 'executing' action to
   'approved', so a send that happened but was never recorded could run
   again. Stale claims now go to a terminal 'failed' state for manual
   reconciliation.
4. pipeline and cron executors went ahead on an action another process had
   already claimed.
5. The break-glass switches could be set from .env, and nothing reported
   them.
6. The hosted-session refusal of credential providers existed only in the
   capabilities report; get_workspace_client built the client anyway.
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "skills" / "document-preparer" / "scripts"))

import state_db  # noqa: E402
from state_db import (  # noqa: E402
    approve_pending_action,
    create_pending_action,
    get_pending_action,
    mark_executing,
)

BREAK_GLASS = ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE")


@pytest.fixture
def config(tmp_path, monkeypatch):
    for name in BREAK_GLASS:
        monkeypatch.delenv(name, raising=False)
    project = tmp_path / "project"
    project.mkdir()
    return {
        "company": {"name": "Test Co", "jurisdiction": "SG"},
        "google": {"delegate_email": "founder@test.com", "account_alias": "test", "domain": "test.com"},
        "integrations": {"workspace": {"provider": "google_api"}},
        "paths": {"project_root": str(project)},
    }


def _db(config) -> sqlite3.Connection:
    return sqlite3.connect(str(Path(config["paths"]["project_root"]) / "state.db"))


def _approved(config, action_type="gmail.send", target="colleague@test.com", payload=None):
    payload = payload if payload is not None else {
        "to": "colleague@test.com", "subject": "Q3", "body": "Numbers attached."}
    action = create_pending_action(config=config, action_type=action_type, provider="google_api",
                                   target=target, payload=payload, summary=f"test {action_type}")
    approve_pending_action(config, action["id"], approver="founder", reason="looks right")
    return action["id"]


# ─── 1. approval is bound to what was approved ───────────────────────────────

class TestApprovalBinding:
    def test_approval_records_a_hash(self, config):
        action = get_pending_action(config, _approved(config))
        assert action["approval_hash"] and len(action["approval_hash"]) == 64

    def test_untouched_action_still_executes(self, config):
        assert mark_executing(config, _approved(config)) is not None

    @pytest.mark.parametrize("column,value", [
        ("payload", json.dumps({"to": "attacker@evil.example", "subject": "Q3", "body": "x"})),
        ("target", "attacker@evil.example"),
        ("type", "gmail.trash"),
    ])
    def test_changed_after_approval_is_refused(self, config, column, value):
        action_id = _approved(config)
        with _db(config) as conn:
            conn.execute(f"UPDATE pending_actions SET {column}=? WHERE id=?", (value, action_id))
        assert mark_executing(config, action_id) is None
        assert get_pending_action(config, action_id)["state"] != "executing"

    def test_row_inserted_as_approved_is_refused(self, config):
        action = create_pending_action(config=config, action_type="gmail.send", provider="google_api",
                                       target="x@test.com", payload={"to": "x@test.com"}, summary="s")
        with _db(config) as conn:
            conn.execute("UPDATE pending_actions SET state='approved', approved_at=? WHERE id=?",
                         (datetime.now(timezone.utc).isoformat(), action["id"]))
        assert mark_executing(config, action["id"]) is None

    def test_retry_after_failure_keeps_the_approval(self, config):
        action_id = _approved(config)
        assert mark_executing(config, action_id)
        state_db.mark_failed(config, action_id, "transient 503")
        assert get_pending_action(config, action_id)["state"] == "approved"
        assert mark_executing(config, action_id) is not None

    def test_legacy_round_trip_preserves_the_hash(self, config):
        action_id = _approved(config)
        data = state_db._load(config)
        state_db._save(config, data)
        assert mark_executing(config, action_id) is not None

    def test_refusal_is_audited(self, config):
        action_id = _approved(config)
        with _db(config) as conn:
            conn.execute("UPDATE pending_actions SET target='attacker@evil.example' WHERE id=?", (action_id,))
        calls = []
        with patch("workspace_audit.audit_workspace_action",
                   side_effect=lambda *a, **k: calls.append(k)):
            mark_executing(config, action_id)
        assert any(k.get("status") == "blocked" for k in calls)


# ─── 2. the email executor only sends email ─────────────────────────────────

def _send_email_execute(config, action_id, client):
    import send_email
    with patch("send_email.load_config", return_value=config), \
         patch("send_email.get_client", return_value=client):
        return send_email.main(["execute", "--action-id", action_id])


class TestSendEmailExecutor:
    def test_refuses_a_non_email_action(self, config):
        action_id = _approved(config, action_type="gmail.create_label", target="Receipts",
                              payload={"label": "Receipts", "to": "attacker@evil.example",
                                       "subject": "s", "body": "b"})
        client = MagicMock()
        rc = _send_email_execute(config, action_id, client)
        assert rc != 0
        client.mail_send.assert_not_called()
        assert get_pending_action(config, action_id)["state"] == "approved"

    def test_failed_send_is_not_marked_executed(self, config):
        action_id = _approved(config)
        client = MagicMock()
        client.mail_send.return_value = {"success": False, "error": "quota exceeded"}
        rc = _send_email_execute(config, action_id, client)
        assert rc != 0
        action = get_pending_action(config, action_id)
        assert action["state"] != "executed"
        assert "quota" in (action.get("last_error") or "")

    def test_successful_send_is_executed(self, config):
        action_id = _approved(config)
        client = MagicMock()
        client.mail_send.return_value = {"success": True, "message_id": "m1"}
        assert _send_email_execute(config, action_id, client) == 0
        assert get_pending_action(config, action_id)["state"] == "executed"


# ─── 3. a stale claim never re-arms ──────────────────────────────────────────

def _stale_claim(config):
    action_id = _approved(config)
    assert mark_executing(config, action_id)
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    with _db(config) as conn:
        conn.execute("UPDATE pending_actions SET executing_at=? WHERE id=?", (old, action_id))
    return action_id


class TestStaleClaims:
    def test_doctor_revert_moves_to_failed_not_approved(self, config):
        action_id = _stale_claim(config)
        calls = []
        with patch("workspace_audit.audit_workspace_action",
                   side_effect=lambda *a, **k: calls.append(k)):
            updated = state_db.revert_stuck_action(config, action_id, max_minutes=15)
        assert updated is not None
        action = get_pending_action(config, action_id)
        assert action["state"] == "failed"
        assert action["failed_at"]
        assert "reconcile" in action["last_error"].lower()
        assert mark_executing(config, action_id) is None, "a stale claim must never be claimable again"
        assert all(k.get("status") != "approved" for k in calls)

    def test_state_tools_repair_moves_to_failed_not_approved(self, config):
        import state_tools
        action_id = _stale_claim(config)
        project = Path(config["paths"]["project_root"])
        data = state_db._load(config)
        reset = state_tools._reset_executing_actions(data, project / ".pending_actions.json",
                                                      min_age_minutes=15)
        assert action_id in reset
        assert get_pending_action(config, action_id)["state"] == "failed"
        assert mark_executing(config, action_id) is None

    def test_forced_repair_does_not_rearm_a_fresh_claim(self, config):
        import state_tools
        action_id = _approved(config)
        assert mark_executing(config, action_id)
        project = Path(config["paths"]["project_root"])
        state_tools._reset_executing_actions(state_db._load(config), project / ".pending_actions.json",
                                             force=True)
        assert get_pending_action(config, action_id)["state"] != "approved"


# ─── 4. executors only run a claim they won ─────────────────────────────────

class TestClaimExclusivity:
    def test_pipeline_executor_refuses_an_action_claimed_elsewhere(self, config):
        import pipeline_actions
        action_id = _approved(config, action_type="pipeline.deal.add", target="New Co",
                              payload={"client_name": "New Co", "stage": "Lead"})
        assert mark_executing(config, action_id)  # another process holds the claim
        with patch.object(pipeline_actions, "_exec_deal_add") as add:
            result = pipeline_actions.execute_pipeline_action(config, action_id)
        assert result["success"] is False
        assert "claim" in result["error"].lower()
        add.assert_not_called()

    def test_cron_executor_refuses_an_action_claimed_elsewhere(self, config, monkeypatch):
        import workflow_cron
        from workflow_runs import WorkflowRunError
        installs = []
        monkeypatch.setattr(workflow_cron, "install_workflow_cron",
                            lambda *a, **k: installs.append(a) or {"schedule_id": "s1"})
        action_id = _approved(config, action_type="cron.create", target="nightly",
                              payload={"workflow_name": "nightly", "workflow": {"name": "nightly"}})
        assert mark_executing(config, action_id)
        with pytest.raises(WorkflowRunError, match="claim"):
            workflow_cron.execute_cron_create(config, action_id)
        assert installs == []


# ─── 5. break-glass switches are operator-only and visible ──────────────────

class TestBreakGlass:
    def test_dotenv_cannot_set_the_switches(self, tmp_path, monkeypatch):
        from config_loader import load_dotenv_file
        for name in BREAK_GLASS:
            monkeypatch.delenv(name, raising=False)
        env = tmp_path / ".env"
        env.write_text("CHIEF_OF_STAFF_AUTO_APPROVE=1\nCHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1\nOTHER_KEY=ok\n",
                       encoding="utf-8")
        monkeypatch.delenv("OTHER_KEY", raising=False)
        applied = load_dotenv_file(env)
        import os
        for name in BREAK_GLASS:
            assert name not in applied
            assert name not in os.environ
        assert applied.get("OTHER_KEY") == "ok"

    def test_capabilities_report_the_switches(self, config, monkeypatch):
        import chief_of_staff
        monkeypatch.setenv("CHIEF_OF_STAFF_AUTO_APPROVE", "1")
        report = chief_of_staff.build_capability_report(config)
        assert report["break_glass"] == {"CHIEF_OF_STAFF_AUTO_APPROVE": True,
                                         "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE": False}

    def test_doctor_warns_when_a_switch_is_on(self, monkeypatch):
        import doctor_base
        monkeypatch.setenv("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", "1")
        result = doctor_base._check_break_glass(False, None, Path("."))
        assert result.status == "warn"
        assert "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE" in result.detail
        assert doctor_base._check_break_glass in doctor_base.CHECKS

    def test_doctor_passes_when_off(self, monkeypatch):
        import doctor_base
        for name in BREAK_GLASS:
            monkeypatch.delenv(name, raising=False)
        assert doctor_base._check_break_glass(False, None, Path(".")).status == "pass"


# ─── 6. hosted sessions really refuse credential providers ──────────────────

class TestHostedRefusal:
    @pytest.mark.parametrize("provider", ["google_api", "m365", "composio"])
    def test_credential_provider_is_not_built(self, config, monkeypatch, provider):
        from workspace_client import get_workspace_client
        from workspace_guardrails import HostedSessionRefusal
        monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "cse_test")
        config["integrations"]["workspace"]["provider"] = provider
        with pytest.raises(HostedSessionRefusal, match="agent"):
            get_workspace_client(config)

    def test_agent_provider_is_allowed(self, config, monkeypatch):
        from workspace_client import get_workspace_client
        monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "cse_test")
        config["integrations"]["workspace"]["provider"] = "agent"
        assert get_workspace_client(config).provider_name == "agent"


# ─── 7. the audit tamper test can actually fail ─────────────────────────────

def test_tamper_test_no_longer_swallows_its_own_assertion():
    src = (PLUGIN_ROOT / "tests" / "test_phase3_loop2.py").read_text(encoding="utf-8")
    block = src[src.index("verify_audit_chain must detect the tampering"):][:600]
    assert "except Exception:\n            # If it raises, that's also detection\n            pass" not in block
