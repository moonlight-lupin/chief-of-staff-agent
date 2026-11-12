"""Signed approvals: only the human-facing Hermes surface can approve.

Threat: an agent that read hostile content runs ``review_queue.py approve``
(or another approve CLI) and approves its own proposal. Once a store is
enrolled by the Hermes gateway, every approve/dismiss must carry an Ed25519
signature from a key that exists only in the gateway process, and every
execution re-verifies it. Break-glass env flags stop working for enrolled
stores; a guarded write passes only inside an execution grant tied to one
signed, executing action. Stores that never enrol (standalone, Claude Code)
behave exactly as before.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "shared" / "scripts"))

import approval_signing as aps  # noqa: E402
import config_loader  # noqa: E402
import state_db  # noqa: E402
from test_notion_mirror import _config  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    return _config(tmp_path)


@pytest.fixture
def signer(cfg):
    s = aps.GatewaySigner.generate()
    s.enrol(cfg)
    return s


def _audit_bytes(cfg) -> bytes:
    root = config_loader.get_project_root(cfg)
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file() and "audit" in str(p))


def _new(cfg, action_type="gmail.send"):
    return state_db.create_pending_action(cfg, action_type, "google_api", "x@example.com", {"subject": "hi"})


# ------------------------------------------------------------------ signing


def test_sign_and_verify_round_trip(cfg, signer):
    action = _new(cfg)
    sig = signer.sign_decision(action, "approve", "Alicia Tan", cfg)
    assert aps.verify_decision(cfg, action, "approve", "Alicia Tan", sig) is None


@pytest.mark.parametrize("change", ["approver", "decision", "payload", "company", "key"])
def test_any_change_breaks_the_signature(cfg, signer, change):
    action = _new(cfg)
    sig = signer.sign_decision(action, "approve", "Alicia Tan", cfg)
    approver, decision = "Alicia Tan", "approve"
    if change == "approver":
        approver = "Mallory"
    elif change == "decision":
        decision = "dismiss"
    elif change == "payload":
        action = {**action, "payload": {"subject": "changed"}}
    elif change == "company":
        action = {**action, "company_id": "other"}
    elif change == "key":
        sig = aps.GatewaySigner.generate().sign_decision(action, "approve", approver, cfg)  # never enrolled
    assert aps.verify_decision(cfg, action, decision, approver, sig) is not None


# --------------------------------------------------- not enrolled: unchanged


def test_unenrolled_store_keeps_plain_approval(cfg):
    assert aps.enforced(cfg) is False
    action = _new(cfg)
    assert state_db.approve_pending_action(cfg, action["id"], approver="op", reason="ok")
    assert state_db.mark_executing(cfg, action["id"])["state"] == "executing"


# -------------------------------------------------------- enrolled: enforced


def test_unsigned_approve_is_refused_and_audited(cfg, signer):
    action = _new(cfg)
    assert state_db.approve_pending_action(cfg, action["id"], approver="Alicia Tan", reason="ok") is None
    assert state_db.get_pending_action(cfg, action["id"])["state"] == "requested"
    assert b"approval_unsigned" in _audit_bytes(cfg)


def test_signed_approve_then_execute(cfg, signer):
    action = _new(cfg)
    sig = signer.sign_decision(action, "approve", "Alicia Tan", cfg)
    approved = state_db.approve_pending_action(cfg, action["id"], approver="Alicia Tan", reason="ok", signature=sig)
    assert approved and approved["state"] == "approved" and approved.get("approval_signature")
    assert state_db.mark_executing(cfg, action["id"])["state"] == "executing"


def test_signature_for_another_approver_is_refused(cfg, signer):
    action = _new(cfg)
    sig = signer.sign_decision(action, "approve", "Alicia Tan", cfg)
    assert state_db.approve_pending_action(cfg, action["id"], approver="Mallory", reason="ok", signature=sig) is None


def test_tampered_approved_row_cannot_execute(cfg, signer):
    action = _new(cfg)
    sig = signer.sign_decision(action, "approve", "Alicia Tan", cfg)
    state_db.approve_pending_action(cfg, action["id"], approver="Alicia Tan", reason="ok", signature=sig)
    db = config_loader.get_project_root(cfg) / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE pending_actions SET approver='Mallory' WHERE id=?", (action["id"],))
    assert state_db.mark_executing(cfg, action["id"]) is None
    assert b"approval_signature_invalid" in _audit_bytes(cfg)


def test_legacy_unsigned_approval_cannot_execute_after_enrolment(cfg):
    action = _new(cfg)
    state_db.approve_pending_action(cfg, action["id"], approver="op", reason="ok")   # before enrolment
    aps.GatewaySigner.generate().enrol(cfg)
    assert state_db.mark_executing(cfg, action["id"]) is None


def test_unsigned_dismiss_is_refused_signed_dismiss_works(cfg, signer):
    action = _new(cfg)
    assert state_db.dismiss_pending_action(cfg, action["id"], reason="nope") is None
    sig = signer.sign_decision(action, "dismiss", "Alicia Tan", cfg)
    out = state_db.dismiss_pending_action(cfg, action["id"], reason="nope", approver="Alicia Tan", signature=sig)
    assert out and out["state"] == "dismissed"


def test_every_approve_cli_goes_through_the_check(cfg, signer, monkeypatch):
    """review_queue.py approve (single and bulk) is refused for enrolled stores."""
    import review_queue

    monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(cfg.source_path))
    action = _new(cfg, "gmail.label")
    assert review_queue._main(["--config", str(cfg.source_path), "approve", "--action-id", action["id"], "--approver", "Alicia Tan",
                              "--reason", "ok"]) != 0
    review_queue._main(["--config", str(cfg.source_path), "approve", "--all", "--risk", "low", "--type", "gmail.label",
                       "--confirm-low-risk-bulk", "--reason", "bulk"])
    assert state_db.get_pending_action(cfg, action["id"])["state"] == "requested"


def test_enrolling_the_same_store_twice_keeps_both_keys(cfg):
    a, b = aps.GatewaySigner.generate(), aps.GatewaySigner.generate()
    a.enrol(cfg)
    b.enrol(cfg)
    action = _new(cfg)
    sig = a.sign_decision(action, "approve", "op", cfg)           # signed by the older gateway run
    assert aps.verify_decision(cfg, action, "approve", "op", sig) is None


# --------------------------------------------------------------- break-glass


def _guarded_write(cfg, monkeypatch, action_name="gmail.send"):
    import workspace_guardrails as g

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    return g.confirm_action(action_name, config=cfg, to="x@example.com")


def test_env_breakglass_still_works_when_not_enrolled(cfg, monkeypatch):
    monkeypatch.setenv("CHIEF_OF_STAFF_AUTO_APPROVE", "1")
    monkeypatch.setenv("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", "1")
    assert _guarded_write(cfg, monkeypatch) is True


def test_env_breakglass_is_ignored_when_enrolled(cfg, signer, monkeypatch):
    monkeypatch.setenv("CHIEF_OF_STAFF_AUTO_APPROVE", "1")
    monkeypatch.setenv("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", "1")
    assert _guarded_write(cfg, monkeypatch) is False


def test_tty_prompt_is_not_an_approval_when_enrolled(cfg, signer, monkeypatch):
    import workspace_guardrails as g

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *_: "y")      # an agent with a PTY can type "y"
    assert g.confirm_action("gmail.label", config=cfg, label="x") is False


def test_execution_grant_allows_only_the_approved_action(cfg, signer, monkeypatch):
    import workspace_guardrails as g

    action = _new(cfg, "gmail.send")
    sig = signer.sign_decision(action, "approve", "Alicia Tan", cfg)
    state_db.approve_pending_action(cfg, action["id"], approver="Alicia Tan", reason="ok", signature=sig)
    state_db.mark_executing(cfg, action["id"])
    with g.execution_grant(cfg, action["id"]):
        assert _guarded_write(cfg, monkeypatch, "drive.delete") is False      # not what was approved
        assert _guarded_write(cfg, monkeypatch, "mail.send") is True          # provider alias of gmail.send
    assert _guarded_write(cfg, monkeypatch, "gmail.send") is False            # grant ended


def test_execution_grant_refuses_an_unsigned_or_idle_action(cfg, signer):
    import workspace_guardrails as g

    action = _new(cfg)
    with pytest.raises(PermissionError):
        with g.execution_grant(cfg, action["id"]):                             # requested, not executing
            pass


def test_grant_works_unenrolled_for_backward_compatible_executors(cfg, monkeypatch):
    import workspace_guardrails as g

    action = _new(cfg)
    state_db.approve_pending_action(cfg, action["id"], approver="op", reason="ok")
    state_db.mark_executing(cfg, action["id"])
    with g.execution_grant(cfg, action["id"]):
        assert _guarded_write(cfg, monkeypatch, "gmail.send") is True


# ------------------------------------------------------------- Hermes /cos


@pytest.fixture
def surface(cfg, monkeypatch):
    import hermes_surface as hs

    monkeypatch.setattr(hs, "_load_config", lambda: cfg)
    monkeypatch.setattr(hs, "_owner_name", lambda _cfg: "Alicia Tan", raising=False)
    return hs


def test_cos_approve_signs_in_the_gateway(cfg, surface, monkeypatch):
    surface.activate_gateway_signer(cfg)                    # what register() does in the gateway
    action = _new(cfg)
    out = surface.cos_command(f"approve {action['id']} fine", platform="cli")
    assert "approved" in out.lower()
    row = state_db.get_pending_action(cfg, action["id"])
    assert row["state"] == "approved" and row["approval_signature"]
    assert state_db.mark_executing(cfg, action["id"])["state"] == "executing"


def test_cos_handler_called_outside_the_gateway_cannot_approve(cfg, surface, monkeypatch):
    """Direct invocation (e.g. an agent script importing the module) has no signer."""
    aps.GatewaySigner.generate().enrol(cfg)                 # the real gateway enrolled the store
    surface.deactivate_gateway_signer()
    action = _new(cfg)
    out = surface.cos_command(f"approve {action['id']}", platform="cli")
    assert "gateway" in out.lower()
    assert state_db.get_pending_action(cfg, action["id"])["state"] == "requested"


def test_refused_slash_approval_is_audited(cfg, surface):
    surface.activate_gateway_signer(cfg)
    action = _new(cfg)
    surface.cos_command(f"approve {action['id']}", platform="telegram", source=None)
    assert b"slash_approval_refused" in _audit_bytes(cfg)
    assert state_db.get_pending_action(cfg, action["id"])["state"] == "requested"


def test_cos_dismiss_signs_too(cfg, surface):
    surface.activate_gateway_signer(cfg)
    action = _new(cfg)
    surface.cos_command(f"dismiss {action['id']} duplicate", platform="desktop")
    assert state_db.get_pending_action(cfg, action["id"])["state"] == "dismissed"


def test_register_enrols_the_store(cfg, monkeypatch):
    import hermes_surface as hs
    from test_hermes_catalog import _load_plugin_package

    monkeypatch.setattr(hs, "_load_config", lambda: cfg)
    monkeypatch.setattr(hs, "_registry_configs", lambda: [cfg], raising=False)
    plugin = _load_plugin_package("cos_sign_reg")

    class Ctx:
        def register_skill(self, *_): pass
        def register_hook(self, *_): pass
        def register_tool(self, **_): pass
        def register_command(self, *a, **k): pass

    plugin.register(Ctx())
    assert aps.enforced(cfg) is True
    hs.deactivate_gateway_signer()


def test_private_key_never_reaches_disk_or_env(cfg, signer):
    root = config_loader.get_project_root(cfg)
    raw = signer.private_bytes_for_tests()
    for p in root.rglob("*"):
        if p.is_file():
            assert raw not in p.read_bytes()
    assert all(raw.hex() not in v for v in os.environ.values())


# ------------------------------------------------------------------- doctor


def test_doctor_reports_enforced_signing(cfg, signer):
    import doctor_base

    result = doctor_base._check_approval_signing(False, None, Path(cfg.source_path))
    assert result.status == "pass" and "enforced" in result.detail
    assert "approvals.deny" in result.detail          # points at the Hermes-side belt-and-braces


def test_doctor_reports_standalone_mode(cfg, monkeypatch):
    import doctor_base

    for key in [k for k in os.environ if k.startswith("HERMES_SESSION_")]:
        monkeypatch.delenv(key)
    result = doctor_base._check_approval_signing(False, None, Path(cfg.source_path))
    assert result.status == "pass" and "not enrolled" in result.detail


def test_doctor_warns_when_under_hermes_but_not_enrolled(cfg, monkeypatch):
    import doctor_base

    monkeypatch.setenv("HERMES_SESSION_ID", "s1")
    result = doctor_base._check_approval_signing(False, None, Path(cfg.source_path))
    assert result.status == "warn" and "/cos" in result.detail


def test_workflow_pointer_names_the_users_approval():
    import workflow_hooks

    text = workflow_hooks._next_action_text({"id": "send", "review_queue": {}, "action_id": "act-9"})
    assert "/cos approve act-9" in text and "await operator approval" in text


# ------------------------------------------- adversarial review regressions


def _signed_executing(cfg, signer, action_type="gmail.send", target="x@example.com", payload=None):
    action = state_db.create_pending_action(cfg, action_type, "google_api", target,
                                            payload or {"to": target, "subject": "hi"})
    sig = signer.sign_decision(action, "approve", "Alicia Tan", cfg)
    state_db.approve_pending_action(cfg, action["id"], approver="Alicia Tan", reason="ok", signature=sig)
    assert state_db.mark_executing(cfg, action["id"])["state"] == "executing"
    return action


def test_unsigned_approve_cannot_rearm_an_executing_action(cfg, signer):          # finding 2
    action = _signed_executing(cfg, signer)
    assert state_db.approve_pending_action(cfg, action["id"], approver="Alicia Tan", reason="again") is None
    assert state_db.get_pending_action(cfg, action["id"])["state"] == "executing"


def test_failure_is_final_when_enforced(cfg, signer):                              # finding 2
    action = _signed_executing(cfg, signer)
    state_db.mark_failed(cfg, action["id"], "provider 500")
    assert state_db.get_pending_action(cfg, action["id"])["state"] == "failed"
    assert state_db.mark_executing(cfg, action["id"]) is None


def test_failure_still_retries_when_not_enrolled(cfg):
    action = state_db.create_pending_action(cfg, "gmail.archive", "google_api", "m1", {"message_id": "m1"})
    state_db.approve_pending_action(cfg, action["id"], approver="op", reason="ok")
    state_db.mark_executing(cfg, action["id"])
    state_db.mark_failed(cfg, action["id"], "provider 500")
    assert state_db.get_pending_action(cfg, action["id"])["state"] == "approved"


def test_a_fresh_store_is_enforced_once_the_install_enrolled(cfg, signer, tmp_path, monkeypatch):  # finding 3
    (tmp_path / "other").mkdir()
    other = _config(tmp_path / "other")
    assert aps.enforced(other) is True
    action = _new(other)
    assert state_db.approve_pending_action(other, action["id"], approver="op", reason="ok") is None
    monkeypatch.setenv("CHIEF_OF_STAFF_AUTO_APPROVE", "1")
    monkeypatch.setenv("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", "1")
    assert _guarded_write(other, monkeypatch) is False


def test_enforcement_fails_closed_when_the_store_is_unreadable(cfg, signer, monkeypatch):  # finding 4
    import workspace_guardrails as g

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(state_db, "_open_db", boom)
    assert aps.enforced(cfg) is True
    monkeypatch.setenv("CHIEF_OF_STAFF_AUTO_APPROVE", "1")
    monkeypatch.setenv("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", "1")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert g.confirm_action("gmail.send", config=cfg, to="x@example.com") is False


def test_grant_is_bound_to_the_approved_target(cfg, signer, monkeypatch):       # finding 5
    import workspace_guardrails as g

    action = _signed_executing(cfg, signer)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    with g.execution_grant(cfg, action["id"]):
        assert g.confirm_action("gmail.send", config=cfg, to="attacker@evil.example") is False
        assert g.confirm_action("gmail.send", config=cfg, to="x@example.com") is True


def test_grant_is_single_use(cfg, signer, monkeypatch):                          # finding 5
    import workspace_guardrails as g

    action = _signed_executing(cfg, signer)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    with g.execution_grant(cfg, action["id"]):
        assert g.confirm_action("gmail.send", config=cfg, to="x@example.com") is True
        assert g.confirm_action("gmail.send", config=cfg, to="x@example.com") is False


def test_grant_ends_when_the_action_leaves_executing(cfg, signer, monkeypatch):  # finding 5
    import workspace_guardrails as g

    action = _signed_executing(cfg, signer)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    with g.execution_grant(cfg, action["id"]):
        state_db.mark_executed(cfg, action["id"], {"ok": True})
        assert g.confirm_action("gmail.send", config=cfg, to="x@example.com") is False


def test_unsigned_spend_envelope_cannot_be_spent_after_enrolment(cfg):        # finding 6
    import spend

    env = spend.propose(cfg, "monid", "Find leads", "leads", 10, "USD")
    state_db.approve_pending_action(cfg, env["id"], approver="op", reason="ok")    # before enrolment
    aps.GatewaySigner.generate().enrol(cfg)
    with pytest.raises(spend.SpendRefused):
        spend.reserve(cfg, env["id"], "leads", 1, "/x")


def test_executor_marks_failed_when_the_grant_is_refused(cfg, signer, monkeypatch):  # finding 7
    import importlib.util
    import types

    import workspace_guardrails as g

    spec = importlib.util.spec_from_file_location(
        "send_email_t", PLUGIN_ROOT / "skills/document-preparer/scripts/send_email.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    action = state_db.create_pending_action(cfg, "gmail.send", "google_api", "x@example.com",
                                            {"to": "x@example.com", "subject": "hi", "body": "b"})
    sig = signer.sign_decision(action, "approve", "Alicia Tan", cfg)
    state_db.approve_pending_action(cfg, action["id"], approver="Alicia Tan", reason="ok", signature=sig)

    def refuse(*_a, **_k):
        raise PermissionError("refused for test")

    monkeypatch.setattr(g, "execution_grant", refuse)
    monkeypatch.setattr(mod, "get_client", lambda _cfg: types.SimpleNamespace(), raising=False)
    monkeypatch.setattr(mod, "load_config", lambda *_a, **_k: cfg, raising=False)
    rc = mod.cmd_execute(types.SimpleNamespace(action_id=action["id"], config=str(cfg.source_path),
                                               summary=False, json=False))
    assert rc == 1
    assert state_db.get_pending_action(cfg, action["id"])["state"] != "executing"


def test_enforcement_cannot_be_switched_off_through_the_store_api(cfg, signer):  # finding 1
    with pytest.raises(PermissionError):
        with state_db._open_db(cfg) as db:
            db.set_kv(aps.STORE, {"enforced": False})
    with pytest.raises(PermissionError):
        with state_db._open_db(cfg) as db:
            db.mutate_kv(aps.STORE, lambda d: d.update({"enforced": False}))
    assert aps.enforced(cfg) is True


def test_every_enrolment_is_audited(cfg):                                       # finding 1
    aps.GatewaySigner.generate().enrol(cfg)
    audit = _audit_bytes(cfg)
    assert b"approval_signing.enrol" in audit and b"pid" in audit


def test_doctor_lists_enrolments(cfg, signer):                                  # finding 1
    import doctor_base

    result = doctor_base._check_approval_signing(False, None, Path(cfg.source_path))
    assert "1 gateway key" in result.detail
