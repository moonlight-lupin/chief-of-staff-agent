#!/usr/bin/env python3
"""v0.3.4 — instrumentation half of "Observability".

Drives the real control flow of the M365 provider, the query compiler, the
guardrails, the audit logs and the pending-action lifecycle through the
structured runtime log (shared/scripts/runtime_log.py) and asserts the FROZEN
event vocabulary is emitted with the right fields — and, critically, that no
secrets, request bodies or query text ever reach events.jsonl.

No network, no msal: a scripted ``_send`` supplies canned responses exactly as
the existing m365 tests do. Runs are initialised at DEBUG (so debug-level
started/completed/query_compiled events land on disk) and quiet (no console
noise).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

import runtime_log as rl  # noqa: E402


# ── Fixtures / fakes ───────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_context():
    """No run context / env var may bleed between tests."""
    rl._CURRENT.set(None)
    keys = (
        rl.RUN_ID_ENV, rl.LOG_LEVEL_ENV, rl.PROJECT_ROOT_ENV,
        "CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE",
    )
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        rl._CURRENT.set(None)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "project"
    (p / ".runs").mkdir(parents=True)
    return p


def _config(project: Path) -> dict:
    return {
        "integrations": {"workspace": {"provider": "m365"}},
        "m365": {
            "tenant_id": "tenant-guid",
            "client_id": "client-guid",
            "client_secret_env": "M365_CLIENT_SECRET",
            "auth": "client_credentials",
            "user_principal": "cos@acme.com",
        },
        "paths": {"project_root": str(project)},
    }


@pytest.fixture
def run(project):
    """Start a DEBUG, quiet run rooted at the tmp project; yield its context."""
    cfg = _config(project)
    run_id = rl.init_run("test-instrumentation", cfg, level="debug", quiet=True)
    run_dir = project / ".runs" / run_id
    return SimpleNamespace(config=cfg, project=project, run_id=run_id, run_dir=run_dir)


class FakeResp:
    def __init__(self, status, *, json_body=None, headers=None, text=""):
        self.status_code = status
        self._json = {} if json_body is None else json_body
        self.headers = headers or {}
        self.text = text
        self.content = b'{"_": 1}'

    def json(self):
        return self._json


def scripted_send(responses):
    seq = list(responses)

    def _send(method, url, **kwargs):
        _send.calls.append((method, url, kwargs))
        return seq.pop(0)

    _send.calls = []
    return _send


def make_client(config, responses):
    from providers.m365_graph import M365GraphClient
    c = M365GraphClient(config)
    c._get_token = MagicMock(return_value="fake-token")
    c._slept: list[float] = []
    c._sleep = lambda s: c._slept.append(s)
    c._send = scripted_send(responses)
    return c


def read_events(run_dir: Path) -> list[dict]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def events_named(run_dir: Path, name: str) -> list[dict]:
    return [e for e in read_events(run_dir) if e.get("event") == name]


def _msg(mid="m1", sender="a@corp.com", subject="Hi"):
    return {
        "id": mid,
        "from": {"emailAddress": {"address": sender}},
        "subject": subject,
        "receivedDateTime": "2026-01-01T00:00:00Z",
    }


# ── (a) happy read: started + completed with real timing/category ───────

class TestHappyRead:
    def test_started_and_completed_emitted(self, run):
        client = make_client(run.config, [FakeResp(200, json_body={"value": [_msg()]})])
        out = client.mail_search("is:unread", max_results=10)
        assert len(out) == 1

        started = events_named(run.run_dir, "provider_request_started")
        completed = events_named(run.run_dir, "provider_request_completed")
        assert started and completed
        s = started[0]
        assert s["method"] == "GET"
        assert s["endpoint_category"] == "mail"
        assert s["provider"] == "m365"
        assert s["operation"] == "mail_search"
        c = completed[0]
        assert isinstance(c["duration_ms"], int) and c["duration_ms"] >= 0
        assert c["status_code"] == 200
        assert c["endpoint_category"] == "mail"
        assert c["result_count"] == 1
        assert c["attempt"] == 1


# ── (b) throttle-retry: provider_retry with wait_s + reason ─────────────

class TestThrottleRetry:
    def test_retry_after_header_then_success(self, run):
        client = make_client(run.config, [
            FakeResp(429, headers={"Retry-After": "2"}),
            FakeResp(200, json_body={"value": [_msg()]}),
        ])
        out = client.mail_search("is:unread")
        assert len(out) == 1
        retries = events_named(run.run_dir, "provider_retry")
        assert len(retries) == 1
        r = retries[0]
        assert r["status_code"] == 429
        assert r["wait_s"] == 2
        assert r["reason"] == "retry_after_header"
        assert r["attempt"] == 1
        assert client._slept == [2]

    def test_exponential_backoff_reason(self, run):
        client = make_client(run.config, [
            FakeResp(429),  # no Retry-After -> exponential fallback
            FakeResp(200, json_body={"value": []}),
        ])
        client.mail_search("is:unread")
        r = events_named(run.run_dir, "provider_retry")[0]
        assert r["reason"] == "exponential_backoff"
        assert r["wait_s"] == 1  # 2**0


# ── (c) deferral: retry_deferred with retry_after_s ─────────────────────

class TestDeferral:
    def test_retry_after_exceeds_budget_defers(self, run):
        client = make_client(run.config, [FakeResp(429, headers={"Retry-After": "60"})])
        # mail_search degrades to [] but retry_deferred fires before the raise.
        with pytest.warns(UserWarning):
            out = client.mail_search("is:unread")
        assert out == []
        deferred = events_named(run.run_dir, "retry_deferred")
        assert len(deferred) == 1
        d = deferred[0]
        assert d["status_code"] == 429
        assert d["retry_after_s"] == 60
        assert client._slept == []  # never slept a deferred wait


# ── (d) ambiguous write: event + audited failure still produced ─────────

class TestAmbiguousWrite:
    def test_post_504_ambiguous_and_audited_failure(self, run):
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        client = make_client(run.config, [FakeResp(504, json_body={})])
        result = client.mail_create_draft("client@x.com", "Subj", "Body")

        amb = events_named(run.run_dir, "ambiguous_write")
        assert len(amb) == 1
        assert amb[0]["status_code"] == 504
        assert amb[0]["method"] == "POST"
        # The guarded write still surfaces an audited-failure ActionResult.
        assert result["success"] is False
        assert result["audited"] is True
        audit_log = run.project / ".audit" / "workspace.log"
        assert audit_log.exists()
        assert '"status": "failed"' in audit_log.read_text()


# ── (e) pagination cap: pagination_truncated ────────────────────────────

class TestPaginationCap:
    def test_truncated_at_max_results(self, run):
        client = make_client(run.config, [
            FakeResp(200, json_body={
                "value": [_msg("m1"), _msg("m2")],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/next",
            }),
        ])
        with pytest.warns(UserWarning):
            out = client.mail_search("is:unread", max_results=1)
        assert len(out) == 1
        trunc = events_named(run.run_dir, "pagination_truncated")
        assert len(trunc) == 1
        assert trunc[0]["cap"] == 1
        assert trunc[0]["pages_followed"] == 1
        assert trunc[0]["operation"] == "mail_search"


# ── (f) guardrail block: guardrail_blocked ──────────────────────────────

class TestGuardrailBlock:
    def test_destructive_send_blocked(self, run):
        # No CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE -> mail.send is blocked at the gate.
        client = make_client(run.config, [])  # body never runs -> no _send needed
        result = client.mail_send("client@x.com", "Subj", "Body")
        assert result["success"] is False
        blocked = events_named(run.run_dir, "guardrail_blocked")
        assert len(blocked) == 1
        assert blocked[0]["action"] == "mail.send"
        assert blocked[0]["reason"] == "destructive_not_allowed"


# ── (g) pending-action lifecycle: three events with action_id ───────────

class TestActionLifecycle:
    def test_requested_executed_failed(self, run):
        import pending_actions as pa
        cfg = run.config

        # create -> action_requested
        act = pa.create_pending_action(cfg, "mail.send", "m365", "client@x.com",
                                       payload={"body": "hi"})
        aid = act["id"]
        req = events_named(run.run_dir, "action_requested")
        assert len(req) == 1
        assert req[0]["action_id"] == aid
        assert req[0]["action_type"] == "mail.send"
        assert req[0]["provider"] == "m365"

        # approve -> executing -> mark_executed -> action_executed
        pa.approve_pending_action(cfg, aid)
        pa.mark_executing(cfg, aid)
        pa.mark_executed(cfg, aid, {"success": True})
        ex = events_named(run.run_dir, "action_executed")
        assert len(ex) == 1
        assert ex[0]["action_id"] == aid
        assert ex[0]["action_type"] == "mail.send"

        # a second action -> mark_failed -> action_failed
        act2 = pa.create_pending_action(cfg, "mail.send", "m365", "y@x.com",
                                        payload={"body": "hi"})
        aid2 = act2["id"]
        pa.approve_pending_action(cfg, aid2)
        pa.mark_executing(cfg, aid2)
        pa.mark_failed(cfg, aid2, "boom")
        failed = events_named(run.run_dir, "action_failed")
        assert len(failed) == 1
        assert failed[0]["action_id"] == aid2
        assert failed[0]["error"] == "boom"


# ── (h) audit linkage: run_id present with a run, absent without ────────

class TestAuditLinkage:
    def test_run_id_present_when_run_active(self, run):
        from workspace_audit import audit_workspace_action
        audit_workspace_action(run.config, "m365", "mail.draft", "graph_api",
                               target="client@x.com")
        log_path = run.project / ".audit" / "workspace.log"
        record = json.loads(log_path.read_text().strip())
        assert record["run_id"] == run.run_id
        # Audit and operational logs are SEPARATE files.
        assert log_path != (run.run_dir / "events.jsonl")

    def test_run_id_absent_when_no_run(self, project):
        # No active run (autouse fixture cleared context).
        from workspace_audit import audit_workspace_action
        cfg = _config(project)
        audit_workspace_action(cfg, "m365", "mail.draft", "graph_api",
                               target="client@x.com")
        log_path = project / ".audit" / "workspace.log"
        record = json.loads(log_path.read_text().strip())
        assert "run_id" not in record

    def test_append_audit_run_id_present(self, run):
        from audit_log import append_audit, read_audit
        append_audit("pipeline", action="move_stage",
                     before={"stage": "Lead"}, after={"stage": "Paid"},
                     config=run.config)
        entries = read_audit("pipeline", limit=5, config=run.config)
        assert entries[0]["run_id"] == run.run_id


# ── (i) query_compiled carries NO query text ────────────────────────────

class TestQueryCompiledNoText:
    def test_no_query_text_in_events(self, run):
        secret_query = "from:bigclient@secret.example subject:ProjectMercury"
        client = make_client(run.config, [FakeResp(200, json_body={"value": []})])
        client.mail_search(secret_query)

        compiled = events_named(run.run_dir, "query_compiled")
        assert len(compiled) == 1
        qc = compiled[0]
        assert qc["dialect"] == "m365"
        assert "has_filter" in qc and "has_search" in qc

        raw = (run.run_dir / "events.jsonl").read_text()
        # Neither the raw gmail string nor its distinctive tokens may appear.
        assert secret_query not in raw
        assert "ProjectMercury" not in raw
        assert "bigclient" not in raw
        assert "secret.example" not in raw


# ── (j) secret leak: Authorization header + body never on disk ──────────

class TestSecretLeak:
    def test_header_and_body_absent(self, run):
        client = make_client(run.config, [FakeResp(200, json_body={"value": []})])
        client._request(
            "POST", "/users/cos@acme.com/messages",
            headers={"Authorization": "Bearer supersecret-token-XYZ"},
            json_body={"secret_body": "topsecret-payload-ABC"},
        )
        raw = (run.run_dir / "events.jsonl").read_text()
        assert "supersecret-token-XYZ" not in raw
        assert "topsecret-payload-ABC" not in raw
        assert "fake-token" not in raw  # the acquired bearer never logged either


# ── (k) no active run: every instrumented path is a safe no-op ──────────

class TestNoActiveRun:
    def test_paths_work_without_run_and_write_no_events(self, project):
        cfg = _config(project)
        # runtime_log has no active run (autouse fixture cleared context).
        assert rl.current_run_id() is None

        # Provider read.
        client = make_client(cfg, [FakeResp(200, json_body={"value": [_msg()]})])
        assert len(client.mail_search("is:unread")) == 1

        # Query compile.
        from query_compiler import compile_query
        assert compile_query("is:unread", "gmail") == "is:unread"

        # Guardrail block path.
        from workspace_guardrails import confirm_action
        assert confirm_action("mail.send", to="x@y.com") is False

        # Pending-action lifecycle.
        import pending_actions as pa
        act = pa.create_pending_action(cfg, "mail.send", "m365", "x@y.com",
                                       payload={"body": "hi"})
        pa.approve_pending_action(cfg, act["id"])
        pa.mark_executing(cfg, act["id"])
        pa.mark_executed(cfg, act["id"], {"success": True})

        # Audit write.
        from workspace_audit import audit_workspace_action
        audit_workspace_action(cfg, "m365", "mail.draft", "graph_api", target="x@y.com")

        # No operational run directory / events file was created anywhere.
        run_dirs = [d for d in (project / ".runs").iterdir() if d.is_dir()]
        assert run_dirs == []
        assert list((project / ".runs").glob("**/events.jsonl")) == []
