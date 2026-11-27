#!/usr/bin/env python3
# ruff: noqa: F811 - the fixtures imported from test_google_mcp_workspace are used as parameters
"""v0.10.0 part 2 — Google's confirmation prompt end to end.

Pins the review findings on the google_mcp provider and the confirmation flow:

* a pause for Google's prompt leaves the action approved and is not a retry,
  on unsigned and signed (enrolled) stores alike (A1);
* a denial or an administrator block is final for every action type (A2);
* direct CLIs report a pause instead of crashing on it (A3);
* on a write, an error where the server certainly ran nothing is "not
  attempted" (A4); an SSE stream without our response id is an error (A5);
* the confirmer answers only from a snapshot of the offered choices, and
  persistent choices need the operator's opt-in (A6);
* the prompt's message stays out of the public form, audit and logs (A7);
* a corrupt download writes nothing (A8);
* the retry is byte-identical apart from requestState and inputResponses;
* the stored prompt record, the terminal confirmer, ``/cos confirm``,
  ``review_queue.py preview`` and the REST-provider deprecation warning (B).

No network: requests.post and the service-account credentials are mocked.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import action_execution as ax  # noqa: E402
import approval_signing as aps  # noqa: E402
import google_confirmation as gc  # noqa: E402
import google_mcp_transport as gmt  # noqa: E402
import state_db  # noqa: E402
import workspace_guardrails  # noqa: E402
from providers import google_mcp_workspace as gmw  # noqa: E402
from test_google_mcp_workspace import (  # noqa: E402,F401 - fixtures are used by name
    ENUM,
    REQUEST_STATE,
    TOKEN,
    FakeHTTP,
    FakeResponse,
    _no_break_glass,
    cfg,
    client,
    creds,
    granted,
    http,
    input_required,
    ok,
    tool_error,
)

MESSAGE_TEXT = "Quarterly numbers"


# ── helpers ─────────────────────────────────────────────────────────────────

def _create(cfg, action_type="mail.send", target="bob@partner.example", payload=None):
    payload = payload if payload is not None else {"to": target, "subject": "Hi", "body": "Body"}
    with state_db._open_db(cfg) as db:
        return db.create_action(action_type, "google_mcp", target, payload)


def _approve(cfg, action, signer=None):
    sig = signer.sign_decision(action, "approve", "Alicia Tan", cfg) if signer else None
    assert state_db.approve_pending_action(cfg, action["id"], approver="Alicia Tan", reason="ok", signature=sig)
    return state_db.get_pending_action(cfg, action["id"])


def _row(cfg, action):
    return state_db.get_pending_action(cfg, action["id"])


def _send(c, action):
    p = action["payload"]
    return c.mail_send(p["to"], p["subject"], p["body"])


CALLS = {
    "mail.send": _send,
    "mail.trash": lambda c, a: c.mail_trash(a["target"]),
    "mail.archive": lambda c, a: c.mail_archive(a["target"]),
    "files.trash": lambda c, a: c.files_trash(a["target"]),
    "calendar.delete": lambda c, a: c.calendar_delete(a["target"]),
    "calendar.update": lambda c, a: c.calendar_update(a["target"], title="New"),
}
TARGETS = {"mail.send": "bob@partner.example", "mail.trash": "m-1", "mail.archive": "m-1",
           "files.trash": "f-1", "calendar.delete": "e-1", "calendar.update": "e-1"}


def _run(cfg, client, action, **kw):
    with patch("company_registry.preflight", return_value=(None, {"checked": True})):
        return ax.run_approved(cfg, action, client, CALLS[action["type"]], **kw)


def _audit_bytes(cfg):
    root = Path(cfg["paths"]["project_root"])
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file() and ".audit" in p.parts)


def _load_script(name, rel):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def signer(cfg):
    s = aps.GatewaySigner.generate()
    s.enrol(cfg)
    return s


# ── A1: a pause is not a retry ──────────────────────────────────────────────

class TestPauseIsNotARetry:
    def test_three_prompts_leave_an_unsigned_action_approved(self, cfg, client, http):
        action = _approve(cfg, _create(cfg))
        for _ in range(3):
            http.add(input_required())
            out = _run(cfg, client, action)
            assert out.stage == "confirm" and not out.ok
            row = _row(cfg, action)
            assert row["state"] == "approved"
            assert int(row["retry_count"] or 0) == 0
        assert "confirmation" in (row.get("last_error") or "").lower()
        http.add(input_required(), ok({"id": "m-new"}))
        out = _run(cfg, client, action, confirmer=lambda p: "ALLOW_ONCE")
        assert out.ok, out
        assert _row(cfg, action)["state"] == "executed"

    def test_three_prompts_leave_a_signed_action_approved(self, cfg, client, http, signer):
        action = _approve(cfg, _create(cfg), signer)
        for _ in range(3):
            http.add(input_required())
            out = _run(cfg, client, action)
            assert out.stage == "confirm"
            row = _row(cfg, action)
            assert row["state"] == "approved" and int(row["retry_count"] or 0) == 0
            assert row["approval_signature"] == action["approval_signature"]
        http.add(input_required(), ok({"id": "m-new"}))
        assert _run(cfg, client, action, confirmer=lambda p: "ALLOW_ONCE").ok
        assert _row(cfg, action)["state"] == "executed"

    def test_signature_is_reverified_at_the_next_claim(self, cfg, client, http, signer):
        import sqlite3

        action = _approve(cfg, _create(cfg), signer)
        http.add(input_required())
        assert _run(cfg, client, action).stage == "confirm"
        db = Path(cfg["paths"]["project_root"]) / "state.db"
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE pending_actions SET approver='Mallory' WHERE id=?", (action["id"],))
        assert state_db.mark_executing(cfg, action["id"]) is None

    def test_binding_is_reverified_at_the_next_claim(self, cfg, client, http):
        action = _approve(cfg, _create(cfg))
        http.add(input_required())
        assert _run(cfg, client, action).stage == "confirm"
        with patch("state_db._action_binding", return_value={"provider": "other", "account": "x"}), \
                patch("state_db._binding_drift", return_value="account changed"):
            assert state_db.mark_executing(cfg, action["id"]) is None

    def test_each_execution_needs_its_own_grant(self, cfg, client, http, monkeypatch):
        action = _approve(cfg, _create(cfg))
        http.add(input_required())
        assert _run(cfg, client, action).stage == "confirm"
        # Back in approved: no grant can be opened until a fresh claim.
        with pytest.raises(PermissionError):
            with workspace_guardrails.execution_grant(cfg, action["id"]):
                pass
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
        assert workspace_guardrails.confirm_action("mail.send", config=cfg, to=TARGETS["mail.send"]) is False

    def test_awaiting_confirmation_only_from_executing(self, cfg):
        action = _approve(cfg, _create(cfg))
        assert state_db.mark_awaiting_confirmation(cfg, action["id"], "Gmail asked") is None
        assert state_db.mark_executing(cfg, action["id"])
        row = state_db.mark_awaiting_confirmation(cfg, action["id"], "Gmail asked")
        assert row["state"] == "approved" and int(row["retry_count"] or 0) == 0
        assert row["approved_at"] == action["approved_at"]
        assert b"awaiting_confirmation" in _audit_bytes(cfg)

    def test_a_pause_does_not_rearm_through_the_approve_path(self, cfg, signer):
        """The re-arm refusal still stands: approve from executing is refused on signed stores."""
        action = _approve(cfg, _create(cfg), signer)
        assert state_db.mark_executing(cfg, action["id"])
        assert state_db.approve_pending_action(cfg, action["id"], approver="Alicia Tan", reason="x") is None
        assert _row(cfg, action)["state"] == "executing"


# ── A2: denial and admin block are final ────────────────────────────────────

class TestDenialIsFinal:
    @pytest.mark.parametrize("action_type", ["mail.send", "mail.trash", "files.trash", "calendar.delete",
                                             "mail.archive", "calendar.update"])
    @pytest.mark.parametrize("reason,text", [
        ("UserDeniedAction", "Action canceled. The user declined the confirmation request."),
        ("AdminPolicyBlock", "Action could not be completed. Your administrator has blocked "
                             "third-party agents from performing this action."),
    ])
    def test_final_for_every_type(self, cfg, client, http, action_type, reason, text):
        target = TARGETS[action_type]
        action = _approve(cfg, _create(cfg, action_type, target, {"target": target}
                                       if action_type != "mail.send" else None))
        http.add(tool_error(text, reason))
        out = _run(cfg, client, action)
        assert not out.ok and reason in out.error
        row = _row(cfg, action)
        assert row["state"] == "failed", (action_type, reason, row["state"])
        assert state_db.mark_executing(cfg, action["id"]) is None
        assert len(http.calls) == 1

    def test_denial_after_the_users_deny_choice_is_final(self, cfg, client, http):
        action = _approve(cfg, _create(cfg, "mail.trash", "m-1", {"target": "m-1"}))
        http.add(input_required(), tool_error("Action canceled. The user declined the confirmation request.",
                                              "UserDeniedAction"))
        out = _run(cfg, client, action, confirmer=lambda p: "DENY_ONCE")
        assert not out.ok and _row(cfg, action)["state"] == "failed"

    def test_final_failure_marker_raised_by_the_call(self, cfg, client):
        action = _approve(cfg, _create(cfg, "mail.archive", "m-1", {"target": "m-1"}))

        def call(c, a):
            raise ax.FinalFailure("never again")
        with patch("company_registry.preflight", return_value=(None, {})):
            out = ax.run_approved(cfg, action, client, call)
        assert out.stage == "call" and _row(cfg, action)["state"] == "failed"

    def test_mark_failed_final(self, cfg):
        action = _approve(cfg, _create(cfg, "mail.archive", "m-1", {"target": "m-1"}))
        state_db.mark_executing(cfg, action["id"])
        state_db.mark_failed(cfg, action["id"], "denied", final=True)
        assert _row(cfg, action)["state"] == "failed"

    def test_denied_errors_are_final_failures(self):
        assert issubclass(gmt.UserDeniedAction, workspace_guardrails.FinalFailure)
        assert issubclass(gmt.AdminPolicyBlock, workspace_guardrails.FinalFailure)
        assert ax.FinalFailure is workspace_guardrails.FinalFailure
        assert ax.NotAttempted is workspace_guardrails.NotAttempted


# ── A3: direct CLIs report the pause ────────────────────────────────────────

def _prompt(**kw):
    fields = dict(server="gmailmcp.googleapis.com", product="Gmail", tool="create_draft",
                  message=f"**{MESSAGE_TEXT}** to bob@partner.example", title="Allow?",
                  choices=[("ALLOW_ONCE", "Allow once"), ("DENY_ONCE", "Don't allow")],
                  request_state="opaque", arguments={"body": "secret body"})
    fields.update(kw)
    return gmt.GoogleConfirmationRequired(**fields)


class _PausingClient:
    provider_name = "google_mcp"

    def __init__(self, prompt=None, upload_ok=True):
        self.prompt = prompt or _prompt()
        self.upload_ok = upload_ok
        self.confirmer = None

    def supports(self, _action):
        return True

    def unsupported_reason(self, _action):
        return None

    def _pause(self, *_a, **_k):
        raise self.prompt

    calendar_create = calendar_update = files_download = mail_create_draft = mail_create_tag = _pause

    def files_upload(self, *_a, **_k):
        if self.upload_ok:
            return {"success": True, "provider": "google_mcp", "data": {"id": "f-9", "link": "https://d/f-9"}}
        raise self.prompt

    def mail_list_tags(self):
        return []


class TestDirectCLIs:
    def _ns(self, **kw):
        base = dict(config=None, summary=False, dry_run=False)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def _check(self, capsys, rc):
        out = capsys.readouterr()
        text = out.out + out.err
        assert rc == 1
        assert "confirmation" in text.lower()
        assert MESSAGE_TEXT not in text and "secret body" not in text
        return text

    def test_calendar_create_and_update(self, cfg, capsys, monkeypatch):
        mod = _load_script("cal_actions_t", "skills/calendar-manager/scripts/calendar_actions.py")
        monkeypatch.setattr(mod, "load_config", lambda *_a, **_k: cfg)
        monkeypatch.setattr(mod, "get_client", lambda _c: _PausingClient())
        rc = mod.cmd_create(self._ns(title="T", start="2026-12-01", end="2026-12-01",
                                     attendees=None, description=None))
        self._check(capsys, rc)
        rc = mod.cmd_update(self._ns(event_id="e-1", title="T2", start=None, end=None))
        self._check(capsys, rc)

    def test_handoff_reports_that_the_upload_already_happened(self, cfg, capsys, monkeypatch, tmp_path):
        mod = _load_script("doc_actions_t", "skills/document-preparer/scripts/document_actions.py")
        f = tmp_path / "nda.txt"
        f.write_text("x")
        monkeypatch.setattr(mod, "load_config", lambda *_a, **_k: cfg)
        monkeypatch.setattr(mod, "get_client", lambda _c: _PausingClient())
        rc = mod.cmd_handoff(self._ns(file=str(f), parent=None, to="bob@partner.example", subject="NDA",
                                      body="see", cc=None, allow_partial=False, preflight=False))
        text = self._check(capsys, rc)
        assert "already uploaded" in text.lower() and "f-9" in text

    def test_draft_email(self, cfg, capsys, monkeypatch):
        mod = _load_script("doc_actions_t2", "skills/document-preparer/scripts/document_actions.py")
        monkeypatch.setattr(mod, "load_config", lambda *_a, **_k: cfg)
        monkeypatch.setattr(mod, "get_client", lambda _c: _PausingClient())
        rc = mod.cmd_draft_email(self._ns(to="bob@partner.example", subject="S", body="B", cc=None))
        self._check(capsys, rc)

    def test_drive_upload_and_download(self, cfg, capsys, monkeypatch, tmp_path):
        mod = _load_script("drive_file_t", "skills/drive-filer/scripts/drive_file.py")
        f = tmp_path / "r.pdf"
        f.write_text("x")
        monkeypatch.setattr(mod, "load_config", lambda *_a, **_k: cfg)
        monkeypatch.setattr(mod, "get_client", lambda _c: _PausingClient(upload_ok=False))
        self._check(capsys, mod.cmd_upload(self._ns(file=str(f), parent=None)))
        self._check(capsys, mod.cmd_download(self._ns(file_id="f-1", output=str(tmp_path / "o.pdf"))))

    def test_label_policy_create_tag(self, capsys):
        import email_label_policy

        assert email_label_policy.resolve_tag_id(_PausingClient(), "Clients", create_if_missing=True) is None
        err = capsys.readouterr().err
        assert "confirmation" in err.lower() and MESSAGE_TEXT not in err

    def test_direct_cli_never_answers_without_a_tty(self, cfg, monkeypatch):
        c = _PausingClient()
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
        gc.attach_confirmer(c, cfg)
        assert c.confirmer is None


# ── A4: certain refusals on a write are not attempts ────────────────────────

def _transport():
    return gmt.GoogleMCPTransport(lambda: TOKEN, client_version="1")


class TestNotAttempted:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 429])
    def test_4xx_write_is_not_attempted(self, creds, status):
        fake = FakeHTTP(FakeResponse(status=status, text="no"))
        with patch("google_mcp_transport.requests.post", side_effect=fake):
            with pytest.raises(gmt.GoogleMCPTransportError) as exc:
                _transport().call_tool("gmail", "send_message", {"to": ["x@y.z"]}, read_only=False)
        assert isinstance(exc.value, ax.NotAttempted)
        assert exc.value.outcome_unknown is False

    @pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
    def test_timeout_like_or_5xx_write_is_unknown(self, creds, status):
        fake = FakeHTTP(FakeResponse(status=status, text="no"))
        with patch("google_mcp_transport.requests.post", side_effect=fake):
            with pytest.raises(gmt.GoogleMCPTransportError) as exc:
                _transport().call_tool("gmail", "send_message", {"to": ["x@y.z"]}, read_only=False)
        assert not isinstance(exc.value, ax.NotAttempted)
        assert exc.value.outcome_unknown is True

    def test_429_on_a_send_keeps_it_approved(self, cfg, client, http):
        action = _approve(cfg, _create(cfg))
        http.add(FakeResponse(status=429, text="slow down"))
        out = _run(cfg, client, action)
        assert not out.ok
        assert _row(cfg, action)["state"] == "approved"

    def test_timeout_on_a_send_is_final(self, cfg, client, http):
        import requests

        action = _approve(cfg, _create(cfg))
        http.add(requests.Timeout("slow"))
        _run(cfg, client, action)
        assert _row(cfg, action)["state"] == "failed"

    def test_500_on_a_send_is_final(self, cfg, client, http):
        action = _approve(cfg, _create(cfg))
        http.add(FakeResponse(status=500, text="boom"))
        _run(cfg, client, action)
        assert _row(cfg, action)["state"] == "failed"

    @pytest.mark.parametrize("code,message", [
        (-32021, "This action requires user confirmation, which this client does not support."),
        (-32602, "Invalid requestState: retry without inputResponses."),
        (-32600, "bad request"),
    ])
    def test_rpc_error_on_a_send_keeps_it_approved(self, cfg, client, http, code, message):
        action = _approve(cfg, _create(cfg))
        http.add(FakeResponse(body={"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}}))
        out = _run(cfg, client, action)
        assert not out.ok
        assert _row(cfg, action)["state"] == "approved"

    def test_retry_without_input_responses_tool_error_is_not_attempted(self, cfg, client, http):
        action = _approve(cfg, _create(cfg))
        http.add(input_required(), tool_error("Approval binding failed: retry without inputResponses."))
        out = _run(cfg, client, action, confirmer=lambda p: "ALLOW_ONCE")
        assert not out.ok
        assert _row(cfg, action)["state"] == "approved"

    def test_non_form_elicitation_is_not_attempted(self, cfg, client, http):
        body = json.loads(input_required().text)
        body["result"]["inputRequests"]["approval_choice"]["params"]["mode"] = "url"
        action = _approve(cfg, _create(cfg))
        http.add(FakeResponse(body=body))
        out = _run(cfg, client, action)
        assert not out.ok and out.stage != "confirm"
        assert _row(cfg, action)["state"] == "approved"


# ── A5: SSE must carry our response ─────────────────────────────────────────

class TestSSE:
    def test_no_matching_id_raises(self):
        text = 'data: {"jsonrpc":"2.0","id":99,"result":{"x":1}}\n\n'
        with pytest.raises(gmt.GoogleMCPError):
            gmt.parse_body(text, "text/event-stream", request_id=3)

    def test_matching_id_is_taken_among_others(self):
        text = ('data: {"jsonrpc":"2.0","id":99,"result":{"x":1}}\n\n'
                'data: {"jsonrpc":"2.0","id":3,"result":{"x":3}}\n\n'
                'data: {"jsonrpc":"2.0","id":98,"result":{"x":2}}\n\n')
        assert gmt.parse_body(text, "text/event-stream", request_id=3)["result"] == {"x": 3}

    def test_stream_without_our_id_through_the_client(self, client, http):
        body = {"jsonrpc": "2.0", "id": 4242, "result": {"resultType": "complete", "content": []}}
        http.add(FakeResponse(text=f"data: {json.dumps(body)}\n\n", content_type="text/event-stream"))
        with pytest.raises(gmt.GoogleMCPError):
            client.mail_list_tags()


# ── A6: choices ─────────────────────────────────────────────────────────────

class TestChoices:
    def test_only_once_choices_are_offered_by_default(self, cfg, client, http):
        http.add(input_required())
        with granted(cfg, "mail.send", "bob@partner.example"):
            with pytest.raises(gmt.GoogleConfirmationRequired) as exc:
                client.mail_send("bob@partner.example", "Hi", "Body")
        assert exc.value.offered_ids == ["ALLOW_ONCE", "DENY_ONCE"]
        assert exc.value.choice_ids == ENUM          # what Google sent, kept for the record

    @pytest.mark.parametrize("choice", ["ALLOW_TOOL_ALWAYS", "ALLOW_AUDIENCE_ALWAYS"])
    def test_persistent_choice_is_refused_without_opt_in(self, cfg, client, http, choice):
        http.add(input_required())
        client.confirmer = lambda prompt: choice
        with granted(cfg, "mail.send", "bob@partner.example"):
            with pytest.raises(gmt.ConfirmationChoiceRefused):
                client.mail_send("bob@partner.example", "Hi", "Body")
        assert len(http.calls) == 1

    def test_persistent_choice_with_opt_in(self, cfg, http):
        cfg["integrations"]["workspace"]["google_mcp"] = {"allow_persistent_choices": True}
        c = gmw.GoogleMCPWorkspaceClient(cfg)
        http.add(input_required(), ok({"id": "m-new"}))
        seen = []
        c.confirmer = lambda prompt: seen.append(prompt.offered_ids) or "ALLOW_TOOL_ALWAYS"
        with granted(cfg, "mail.send", "bob@partner.example"):
            assert c.mail_send("bob@partner.example", "Hi", "Body")["success"] is True
        assert seen == [ENUM]
        assert http.tool_calls[1]["inputResponses"]["approval_choice"]["content"] == {
            "approval_choice": "ALLOW_TOOL_ALWAYS"}

    def test_choices_are_snapshotted_before_the_confirmer(self, cfg, client, http):
        http.add(input_required())

        def sneaky(prompt):
            prompt.choices.append(("ALLOW_EVERYTHING", "x"))
            prompt.offered.append(("ALLOW_EVERYTHING", "x"))
            return "ALLOW_EVERYTHING"
        client.confirmer = sneaky
        with granted(cfg, "mail.send", "bob@partner.example"):
            with pytest.raises(gmt.ConfirmationChoiceRefused):
                client.mail_send("bob@partner.example", "Hi", "Body")
        assert len(http.calls) == 1

    def test_allow_persistent_is_off_by_default(self, cfg):
        assert gc.allow_persistent(cfg) is False
        cfg["integrations"]["workspace"]["google_mcp"] = {"allow_persistent_choices": "yes"}
        assert gc.allow_persistent(cfg) is False          # only a real true opts in
        cfg["integrations"]["workspace"]["google_mcp"] = {"allow_persistent_choices": True}
        assert gc.allow_persistent(cfg) is True


# ── A7: the message stays private ───────────────────────────────────────────

class TestPrivacy:
    def test_to_dict_excludes_the_message(self):
        p = _prompt()
        public = p.to_dict()
        assert "message" not in public and MESSAGE_TEXT not in json.dumps(public)
        assert p.to_dict(include_private=True)["message"].startswith(f"**{MESSAGE_TEXT}")

    def test_audit_never_carries_the_message(self, cfg, client, http):
        action = _approve(cfg, _create(cfg))
        http.add(input_required())
        out = _run(cfg, client, action)
        assert out.stage == "confirm"
        audit = _audit_bytes(cfg)
        assert audit and MESSAGE_TEXT.encode() not in audit
        assert MESSAGE_TEXT not in (_row(cfg, action).get("last_error") or "")
        assert MESSAGE_TEXT not in out.error


# ── A8: download ────────────────────────────────────────────────────────────

class TestDownload:
    def test_invalid_base64_writes_nothing(self, cfg, client, http, tmp_path):
        http.add(ok({"id": "f-1", "title": "a.pdf", "content": "not*base64!!"}))
        dest = tmp_path / "out" / "a.pdf"
        with granted(cfg, "files.download", "f-1"):
            out = client.files_download("f-1", str(dest))
        assert out["success"] is False
        assert not dest.exists()


# ── A9: the retry is the same request ───────────────────────────────────────

class TestIdenticalRetry:
    def test_retry_body_is_byte_identical_apart_from_state_and_responses(self, cfg, client, http):
        http.add(input_required(), ok({"id": "m-new"}))
        client.confirmer = lambda p: "ALLOW_ONCE"
        with granted(cfg, "mail.send", "bob@partner.example"):
            assert client.mail_send("bob@partner.example", "Hi", "Body")["success"] is True
        first, retry = (c["json"] for c in http.calls)
        assert retry["params"].pop("requestState") == REQUEST_STATE
        assert retry["params"].pop("inputResponses")
        first.pop("id")
        retry.pop("id")
        assert json.dumps(first).encode() == json.dumps(retry).encode()
        assert http.calls[0]["headers"] == http.calls[1]["headers"]


# ── B1: the pending prompt record ───────────────────────────────────────────

class TestPromptRecord:
    def test_recorded_on_pause(self, cfg, client, http):
        action = _approve(cfg, _create(cfg))
        http.add(input_required())
        _run(cfg, client, action)
        rec = gc.get_prompt(cfg, action["id"])
        assert rec["server"] == "gmailmcp.googleapis.com" and rec["tool"] == "send_message"
        assert rec["product"] == "Gmail"
        assert rec["message"].startswith(f"**{MESSAGE_TEXT}")
        assert rec["title"].startswith("Allow Chief of Staff")
        assert [c["id"] for c in rec["choices"]] == ["ALLOW_ONCE", "DENY_ONCE"]
        assert rec["recorded_at"]
        assert "request_state" not in rec and "arguments" not in rec

    def test_cleared_when_executed(self, cfg, client, http):
        action = _approve(cfg, _create(cfg))
        http.add(input_required())
        _run(cfg, client, action)
        http.add(input_required(), ok({"id": "m-new"}))
        assert _run(cfg, client, action, confirmer=lambda p: "ALLOW_ONCE").ok
        assert gc.get_prompt(cfg, action["id"]) is None

    def test_cleared_when_failed_finally(self, cfg, client, http):
        action = _approve(cfg, _create(cfg))
        http.add(input_required())
        _run(cfg, client, action)
        http.add(input_required(), tool_error("Action canceled.", "UserDeniedAction"))
        _run(cfg, client, action, confirmer=lambda p: "DENY_ONCE")
        assert gc.get_prompt(cfg, action["id"]) is None

    def test_cleared_when_cancelled(self, cfg, client, http):
        action = _approve(cfg, _create(cfg))
        http.add(input_required())
        _run(cfg, client, action)
        assert gc.get_prompt(cfg, action["id"])
        assert state_db.cancel_pending_action(cfg, action["id"], reason="no")
        assert gc.get_prompt(cfg, action["id"]) is None


# ── B2: the terminal confirmer ──────────────────────────────────────────────

class TestTerminalConfirmer:
    def _ask(self, cfg, answers, prompt=None):
        out = io.StringIO()
        it = iter(answers)

        def fake_input(_prompt=""):
            try:
                return next(it)
            except StopIteration:
                raise EOFError from None
        confirm = gc.terminal_confirmer(cfg, input_fn=fake_input, out=out)
        return confirm(prompt or _prompt()), out.getvalue()

    def test_prints_the_prompt_and_returns_the_choice(self, cfg):
        choice, text = self._ask(cfg, ["1"])
        assert choice == "ALLOW_ONCE"
        assert "Gmail" in text and "create_draft" in text and MESSAGE_TEXT in text and "Allow?" in text
        assert "[1] Allow once" in text and "[2] Don't allow" in text

    def test_deny(self, cfg):
        assert self._ask(cfg, ["2"])[0] == "DENY_ONCE"

    @pytest.mark.parametrize("answers", [["c"], [], ["9", "x", "0"]])
    def test_cancel_eof_or_nonsense_is_none(self, cfg, answers):
        assert self._ask(cfg, answers)[0] is None

    def test_persistent_choices_hidden_by_default(self, cfg):
        p = _prompt(choices=list(zip(ENUM, ["Allow once", "Aud", "Tool", "Don't allow"])))
        choice, text = self._ask(cfg, ["2"], p)
        assert choice == "DENY_ONCE"
        assert "ALWAYS" not in text and "Tool" not in text

    def test_default_confirmer_needs_both_ttys_and_no_hermes(self, cfg, monkeypatch):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(gc.plugin_secrets, "under_hermes", lambda: False)
        assert callable(gc.default_confirmer(cfg))
        monkeypatch.setattr(gc.plugin_secrets, "under_hermes", lambda: True)
        assert gc.default_confirmer(cfg) is None
        monkeypatch.setattr(gc.plugin_secrets, "under_hermes", lambda: False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
        assert gc.default_confirmer(cfg) is None

    def test_run_approved_hands_the_confirmer_to_the_client(self, cfg, client, http):
        action = _approve(cfg, _create(cfg))
        http.add(input_required(), ok({"id": "m-new"}))
        assert client.confirmer is None
        assert _run(cfg, client, action, confirmer=lambda p: "ALLOW_ONCE").ok
        assert client.confirmer is None          # restored after the call

    def test_send_email_without_a_tty_prints_the_next_step(self, cfg, client, http, capsys, monkeypatch):
        mod = _load_script("send_email_conf_t", "skills/document-preparer/scripts/send_email.py")
        action = _approve(cfg, _create(cfg, "gmail.send"))
        http.add(input_required())
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
        monkeypatch.setattr(mod, "get_client", lambda _c: client, raising=False)
        monkeypatch.setattr(mod, "load_config", lambda *_a, **_k: cfg, raising=False)
        with patch("company_registry.preflight", return_value=(None, {})):
            rc = mod.cmd_execute(types.SimpleNamespace(action_id=action["id"], config=None, summary=False))
        out = capsys.readouterr()
        text = out.out + out.err
        assert rc == 1
        assert f"review_queue.py execute --action-id {action['id']}" in text
        assert f"confirm {action['id']}" in text
        assert MESSAGE_TEXT not in text
        assert _row(cfg, action)["state"] == "approved"

    def test_webhook_execute_with_a_tty_asks_the_human(self, cfg, client, http, capsys, monkeypatch):
        mod = _load_script("webhook_conf_t", "skills/document-preparer/scripts/webhook_events.py")
        action = _approve(cfg, _create(cfg, "gmail.send"))
        http.add(input_required(), ok({"id": "m-new"}))
        monkeypatch.setattr(gc, "default_confirmer", lambda _cfg: (lambda p: "ALLOW_ONCE"))
        monkeypatch.setattr("workspace_client.get_workspace_client", lambda _c: client)
        monkeypatch.setattr(mod, "load_config", lambda *_a, **_k: cfg, raising=False)
        with patch("company_registry.preflight", return_value=(None, {})):
            rc = mod.cmd_execute(types.SimpleNamespace(action_id=action["id"], config=None, summary=True))
        assert rc == 0
        assert _row(cfg, action)["state"] == "executed"


# ── B3: /cos confirm ────────────────────────────────────────────────────────

@pytest.fixture
def surface(cfg, client, monkeypatch):
    import hermes_surface as hs

    monkeypatch.setattr(hs, "_load_config", lambda: cfg)
    monkeypatch.setattr(hs, "_owner_name", lambda _cfg: "Alicia Tan", raising=False)
    monkeypatch.setattr("workspace_client.get_workspace_client", lambda _c: client)
    hs.activate_gateway_signer(cfg)
    yield hs
    hs.deactivate_gateway_signer()


def _paused(cfg, surface, http, client):
    action = _create(cfg, "mail.send")        # what send_email.py prepare queues
    assert "approved" in surface.cos_command(f"approve {action['id']} fine", platform="cli").lower()
    action = _row(cfg, action)
    http.add(input_required())
    with patch("company_registry.preflight", return_value=(None, {})):
        assert ax.run_approved(cfg, action, client, _send).stage == "confirm"
    return action


class TestCosConfirm:
    def test_show_the_stored_prompt(self, cfg, surface, http, client):
        action = _paused(cfg, surface, http, client)
        out = surface.cos_command(f"confirm {action['id']}", platform="cli")
        assert MESSAGE_TEXT in out and "[1] Allow once" in out and "[2] Don't allow" in out
        assert f"/cos confirm {action['id']} <n>" in out
        assert len(http.calls) == 1

    def test_nothing_waiting(self, cfg, surface):
        action = _create(cfg)
        out = surface.cos_command(f"confirm {action['id']}", platform="cli")
        assert "no google confirmation" in out.lower()

    def test_answer_runs_the_action_with_that_choice(self, cfg, surface, http, client):
        action = _paused(cfg, surface, http, client)
        http.add(input_required(), ok({"id": "m-new"}))
        with patch("company_registry.preflight", return_value=(None, {})):
            out = surface.cos_command(f"confirm {action['id']} 1", platform="cli")
        assert _row(cfg, action)["state"] == "executed", out
        assert http.tool_calls[-1]["inputResponses"]["approval_choice"]["content"] == {
            "approval_choice": "ALLOW_ONCE"}
        audit = _audit_bytes(cfg)
        assert b"cos.confirm" in audit and b"ALLOW_ONCE" in audit and b"Alicia Tan" in audit
        assert gc.get_prompt(cfg, action["id"]) is None

    def test_a_changed_prompt_is_not_answered(self, cfg, surface, http, client):
        action = _paused(cfg, surface, http, client)
        changed = json.loads(input_required().text)
        changed["result"]["inputRequests"]["approval_choice"]["params"]["message"] = "**Other** To: eve@x.example"
        http.add(FakeResponse(body=changed))
        with patch("company_registry.preflight", return_value=(None, {})):
            out = surface.cos_command(f"confirm {action['id']} 1", platform="cli")
        assert "Other" in out and "[1] Allow once" in out
        assert "inputResponses" not in http.tool_calls[-1]
        assert _row(cfg, action)["state"] == "approved"
        assert gc.get_prompt(cfg, action["id"])["message"].startswith("**Other**")

    def test_unlisted_chat_user_is_refused_and_audited(self, cfg, surface, http, client):
        action = _paused(cfg, surface, http, client)
        out = surface.cos_command(f"confirm {action['id']} 1", platform="telegram",
                                  source=types.SimpleNamespace(user_id="666"))
        assert "not allowed" in out
        assert b"slash_approval_refused" in _audit_bytes(cfg)
        assert len(http.calls) == 1
        assert _row(cfg, action)["state"] == "approved"

    def test_listed_chat_approver_may_answer(self, cfg, surface, http, client):
        action = _paused(cfg, surface, http, client)
        cfg["approvals"] = {"chat_approvers": {"telegram:42": "Alicia Tan"}}
        http.add(input_required(), tool_error("Action canceled.", "UserDeniedAction"))
        with patch("company_registry.preflight", return_value=(None, {})):
            surface.cos_command(f"confirm {action['id']} 2", platform="telegram",
                                source=types.SimpleNamespace(user_id="42"))
        assert http.tool_calls[-1]["inputResponses"]["approval_choice"]["content"] == {
            "approval_choice": "DENY_ONCE"}
        assert _row(cfg, action)["state"] == "failed"

    @pytest.mark.parametrize("n", ["0", "3", "x"])
    def test_bad_choice_number(self, cfg, surface, http, client, n):
        action = _paused(cfg, surface, http, client)
        out = surface.cos_command(f"confirm {action['id']} {n}", platform="cli")
        assert "choose" in out.lower()
        assert len(http.calls) == 1

    def test_outside_the_gateway_it_cannot_answer(self, cfg, surface, http, client):
        action = _paused(cfg, surface, http, client)
        surface.deactivate_gateway_signer()
        out = surface.cos_command(f"confirm {action['id']} 1", platform="cli")
        assert "gateway" in out.lower()
        assert len(http.calls) == 1

    def test_usage_lists_confirm(self, surface):
        assert "confirm <id>" in surface._usage("cos")

    def test_review_queue_approve_still_refused_for_enrolled_store(self, cfg, surface):
        action = _create(cfg)
        assert state_db.approve_pending_action(cfg, action["id"], approver="Alicia Tan", reason="x") is None


# ── B4: preview shows the wait ──────────────────────────────────────────────

class TestPreview:
    def test_preview_shows_the_wait(self, cfg, client, http, capsys, monkeypatch):
        import review_queue

        action = _approve(cfg, _create(cfg))
        http.add(input_required())
        _run(cfg, client, action)
        monkeypatch.setattr(review_queue, "_load_config_or_exit", lambda _p: cfg)
        assert review_queue.cmd_preview(types.SimpleNamespace(config=None, action_id=action["id"])) == 0
        out = capsys.readouterr().out
        assert "Waiting on Google's confirmation" in out
        assert f"review_queue.py execute --action-id {action['id']}" in out
        assert f"confirm {action['id']}" in out


# ── B5: deprecation warning and verification note ───────────────────────────

class TestDeprecation:
    @pytest.mark.parametrize("workspace,warns", [
        ({"provider": "google_api"}, True),
        ({"provider": "composio"}, True),
        ({"provider": "composio", "family": "microsoft"}, False),
        ({"provider": "google_mcp"}, False),
        ({"provider": "m365"}, False),
    ])
    def test_capabilities_warns_for_rest_google(self, workspace, warns):
        from capability_report import build_capability_report

        report = build_capability_report({"integrations": {"workspace": workspace}})
        warning = " ".join(report.get("warnings") or [])
        assert ("1 February 2027" in warning and "google_mcp" in warning) is warns

    def test_google_mcp_note_is_catalog_level(self):
        from capability_report import build_capability_report

        note = build_capability_report({"integrations": {"workspace": {"provider": "google_mcp"}}})[
            "provider_verification_note"]
        assert "catalog" in note.lower()
        assert "only against a mocked transport" not in note
        assert "--verify-writes --record-evidence" in note

    def test_doctor_warns_not_fails(self):
        import doctor_base

        check = doctor_base._check_google_rest_deprecation(
            False, {"integrations": {"workspace": {"provider": "google_api"}}}, Path("company.yaml"))
        assert check.status == "warn" and "1 February 2027" in check.detail
        assert doctor_base._check_google_rest_deprecation in doctor_base.CHECKS
        ok_check = doctor_base._check_google_rest_deprecation(
            False, {"integrations": {"workspace": {"provider": "google_mcp"}}}, Path("company.yaml"))
        assert ok_check.status == "pass"


# ── hardening from the adversarial review ───────────────────────────────────

def _spoof_tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(gc.plugin_secrets, "under_hermes", lambda: False)


class TestPtyOnEnrolledStore:
    def test_no_terminal_confirmer_on_an_enrolled_store(self, cfg, signer, monkeypatch):
        _spoof_tty(monkeypatch)              # a pty (script -qec ...) and env -u HERMES_SESSION_ID
        assert gc.default_confirmer(cfg) is None
        c = _PausingClient()
        gc.attach_confirmer(c, cfg)
        assert c.confirmer is None

    def test_unsigned_store_still_gets_one(self, cfg, monkeypatch):
        _spoof_tty(monkeypatch)
        assert callable(gc.default_confirmer(cfg))

    def test_next_step_on_an_enrolled_store_names_only_cos_confirm(self, cfg, signer):
        text = gc.next_step("a-1", cfg)
        assert "/cos confirm a-1" in text and "review_queue.py execute" not in text

    def test_executor_on_an_enrolled_store_never_answers(self, cfg, client, http, signer, monkeypatch, capsys):
        mod = _load_script("send_email_pty_t", "skills/document-preparer/scripts/send_email.py")
        action = _approve(cfg, _create(cfg), signer)
        http.add(input_required())
        _spoof_tty(monkeypatch)
        monkeypatch.setattr("builtins.input", lambda *_a: "1")
        monkeypatch.setattr(mod, "get_client", lambda _c: client, raising=False)
        monkeypatch.setattr(mod, "load_config", lambda *_a, **_k: cfg, raising=False)
        with patch("company_registry.preflight", return_value=(None, {})):
            rc = mod.cmd_execute(types.SimpleNamespace(action_id=action["id"], config=None, summary=False))
        assert rc == 1 and len(http.calls) == 1
        assert "inputResponses" not in http.tool_calls[0]
        assert _row(cfg, action)["state"] == "approved"
        assert f"/cos confirm {action['id']}" in capsys.readouterr().err

    def test_terminal_never_offers_persistent_choices_even_opted_in(self, cfg):
        cfg["integrations"]["workspace"]["google_mcp"] = {"allow_persistent_choices": True}
        p = _prompt(choices=list(zip(ENUM, ["Allow once", "Aud", "Tool", "Don't allow"])),
                    offered=list(zip(ENUM, ["Allow once", "Aud", "Tool", "Don't allow"])))
        out = io.StringIO()
        answers = iter(["2"])
        choice = gc.terminal_confirmer(cfg, input_fn=lambda _p="": next(answers), out=out)(p)
        assert choice == "DENY_ONCE"
        assert "ALWAYS" not in out.getvalue()

    def test_terminal_answer_cannot_be_persistent_through_the_client(self, cfg, http):
        cfg["integrations"]["workspace"]["google_mcp"] = {"allow_persistent_choices": True}
        c = gmw.GoogleMCPWorkspaceClient(cfg)
        http.add(input_required())
        confirm = gc.terminal_confirmer(cfg, input_fn=lambda _p="": "3", out=io.StringIO())
        c.confirmer = confirm
        with granted(cfg, "mail.send", "bob@partner.example"):
            with pytest.raises(gmt.GoogleConfirmationRequired):   # "3" is not a listed number
                c.mail_send("bob@partner.example", "Hi", "Body")
        assert len(http.calls) == 1


class TestDispatch:
    QUEUED = ["mail.send", "gmail.send", "gmail.label", "gmail.archive", "gmail.create_label",
              "gmail.trash", "drive.trash", "calendar.cancel", "calendar.update", "calendar.create",
              "drive.upload", "drive.download"]

    def _mod(self):
        return _load_script("webhook_dispatch_t", "skills/document-preparer/scripts/webhook_events.py")

    @pytest.mark.parametrize("action_type", QUEUED)
    def test_each_queued_type_is_dispatchable(self, action_type):
        mod = self._mod()
        assert mod.dispatch_key(action_type) in mod.DISPATCHABLE_TYPES

    def test_neutral_aliases_of_dispatchable_types_are_dispatchable(self):
        import workspace_capabilities as caps

        mod = self._mod()
        for legacy, neutral in caps.LEGACY_ACTION_ALIASES.items():
            if legacy in mod.DISPATCHABLE_TYPES:
                assert mod.dispatch_key(neutral) == legacy

    def test_mail_send_runs(self, cfg, client, http, monkeypatch):
        mod = self._mod()
        action = _approve(cfg, _create(cfg, "mail.send"))
        http.add(ok({"id": "m-new"}))
        monkeypatch.setattr("workspace_client.get_workspace_client", lambda _c: client)
        with patch("company_registry.preflight", return_value=(None, {})):
            assert mod.execute_action(cfg, action["id"]).ok
        assert http.tool_calls[0]["name"] == "send_message"

    @pytest.mark.parametrize("action_type", ["mail.draft", "calendar.delete", "made.up"])
    def test_unknown_type_is_refused_before_the_claim(self, cfg, client, http, signer, monkeypatch, action_type):
        mod = self._mod()
        action = _approve(cfg, _create(cfg, action_type, "x", {"target": "x"}), signer)
        monkeypatch.setattr("workspace_client.get_workspace_client", lambda _c: client)
        out = mod.execute_action(cfg, action["id"])
        assert out.stage == "lookup" and not out.ok
        row = _row(cfg, action)
        assert row["state"] == "approved" and not row.get("executing_at")
        assert len(http.calls) == 0


class TestRpcOutcome:
    @pytest.mark.parametrize("code", [-32603, -32001, -32000])
    def test_other_rpc_codes_on_a_send_are_final(self, cfg, client, http, code):
        action = _approve(cfg, _create(cfg))
        http.add(FakeResponse(body={"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": "internal"}}))
        out = _run(cfg, client, action)
        assert not out.ok
        assert _row(cfg, action)["state"] == "failed"

    @pytest.mark.parametrize("code", [-32603, -32001])
    def test_transport_marks_them_unknown(self, creds, code):
        fake = FakeHTTP(FakeResponse(body={"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": "x"}}))
        with patch("google_mcp_transport.requests.post", side_effect=fake):
            with pytest.raises(gmt.GoogleMCPRPCError) as exc:
                _transport().call_tool("gmail", "send_message", {"to": ["x@y.z"]}, read_only=False)
        assert not isinstance(exc.value, ax.NotAttempted)
        assert exc.value.outcome_unknown is True

    @pytest.mark.parametrize("code", [-32021, -32600, -32601, -32602, -32700])
    def test_protocol_refusals_are_not_attempted(self, creds, code):
        fake = FakeHTTP(FakeResponse(body={"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": "x"}}))
        with patch("google_mcp_transport.requests.post", side_effect=fake):
            with pytest.raises(gmt.GoogleMCPRPCError) as exc:
                _transport().call_tool("gmail", "send_message", {"to": ["x@y.z"]}, read_only=False)
        assert isinstance(exc.value, ax.NotAttempted) and exc.value.outcome_unknown is False

    def test_failed_precondition_retry_without_input_responses_is_not_attempted(self, cfg, client, http):
        action = _approve(cfg, _create(cfg))
        http.add(FakeResponse(body={"jsonrpc": "2.0", "id": 1, "error": {
            "code": -32000, "message": "FAILED_PRECONDITION: retry without inputResponses"}}))
        _run(cfg, client, action)
        assert _row(cfg, action)["state"] == "approved"


class TestStalePrompts:
    def test_lapsed_at_claim_clears_the_record(self, cfg, client, http):
        import sqlite3

        action = _approve(cfg, _create(cfg))
        http.add(input_required())
        _run(cfg, client, action)
        assert gc.get_prompt(cfg, action["id"])
        db = Path(cfg["paths"]["project_root"]) / "state.db"
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE pending_actions SET approved_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                         (action["id"],))
        assert state_db.mark_executing(cfg, action["id"]) is None
        assert _row(cfg, action)["state"] == "expired"
        assert gc.get_prompt(cfg, action["id"]) is None

    def test_purge_clears_the_record(self, cfg):
        import sqlite3

        action = _create(cfg)
        gc.record_prompt(cfg, action["id"], _prompt())
        db = Path(cfg["paths"]["project_root"]) / "state.db"
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE pending_actions SET state='executed', executed_at='2000-01-01T00:00:00+00:00' "
                         "WHERE id=?", (action["id"],))
        assert state_db.cleanup_old_actions(cfg, days=30) == 1
        assert gc.get_prompt(cfg, action["id"]) is None

    @pytest.mark.parametrize("answer", ["", " 1"])
    def test_cos_confirm_needs_an_approved_action(self, cfg, surface, http, answer):
        action = _create(cfg)                     # requested, never approved
        gc.record_prompt(cfg, action["id"], _prompt())
        out = surface.cos_command(f"confirm {action['id']}{answer}", platform="cli")
        assert "no longer waiting" in out.lower()
        assert MESSAGE_TEXT not in out
        assert gc.get_prompt(cfg, action["id"]) is None
        assert len(http.calls) == 0


class TestGatewayEnv:
    FLAGS = ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE")

    def _watch(self, monkeypatch):
        import os

        seen = []
        original = workspace_guardrails.confirm_action

        def watching(*a, **k):
            seen.append({f: os.environ.get(f) for f in self.FLAGS})
            return original(*a, **k)
        monkeypatch.setattr(workspace_guardrails, "confirm_action", watching)
        return seen

    def test_cos_confirm_never_sets_break_glass_in_the_gateway(self, cfg, surface, http, client, monkeypatch):
        import os

        action = _paused(cfg, surface, http, client)
        seen = self._watch(monkeypatch)
        http.add(input_required(), ok({"id": "m-new"}))
        with patch("company_registry.preflight", return_value=(None, {})):
            surface.cos_command(f"confirm {action['id']} 1", platform="cli")
        assert _row(cfg, action)["state"] == "executed"
        assert seen and all(v is None for snap in seen for v in snap.values())
        assert not any(os.environ.get(f) for f in self.FLAGS)

    def test_unsigned_store_keeps_the_env_gate(self, cfg, client, http, monkeypatch):
        seen = self._watch(monkeypatch)
        action = _approve(cfg, _create(cfg))
        http.add(ok({"id": "m-new"}))
        assert _run(cfg, client, action).ok
        assert seen and seen[0]["CHIEF_OF_STAFF_AUTO_APPROVE"] == "1"


class TestMappingConfig:
    def test_mapping_proxy_config_is_read(self):
        from types import MappingProxyType

        from capability_report import build_capability_report, google_rest_deprecation

        workspace = MappingProxyType({"provider": "m365"})
        assert google_rest_deprecation(workspace) == ""
        report = build_capability_report(MappingProxyType(
            {"integrations": MappingProxyType({"workspace": workspace})}))
        assert report["provider"] == "m365"
        assert not report["warnings"]
