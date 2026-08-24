#!/usr/bin/env python3
"""Tests for v0.4.0 — the agent execution seam.

Under the fetch/compute split the ``agent`` provider has no Python client: the
AI agent performs workspace mutations with its own connector tools. Before
v0.4.0 that was a dead end — an action could be approved but never recorded as
executed, because ``mark_executed`` was only reachable from inside the guarded
Python execution path.

``review_queue.py claim`` and ``review_queue.py record-execution`` close that
gap. The safety requirement is that they must be a *recording* seam, never a
second execution path:

- claim only moves approved -> executing, and validates the approval first
- record-execution only moves executing -> executed / failed
- neither may skip a state, resurrect a terminal action, or fabricate approval
"""

import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

import review_queue  # noqa: E402
from state_db import (  # noqa: E402
    approve_pending_action,
    create_pending_action,
    get_pending_action,
)


@pytest.fixture
def temp_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = {
        "google": {"delegate_email": "me@acme.com", "domain": "acme.com"},
        "company": {"website": "acme.com"},
        "integrations": {"workspace": {"provider": "agent"}},
        "paths": {"project_root": str(project)},
    }
    return config


def _new_action(config, action_type="mail.send", target="client@example.com"):
    action = create_pending_action(
        config, action_type, "agent", target,
        {"subject": "Hello", "body": "Test"}, summary="Send a note",
    )
    assert action is not None
    return action["id"]


def _approved_action(config, **kw):
    action_id = _new_action(config, **kw)
    approved = approve_pending_action(config, action_id, approver="MH", reason="reviewed")
    assert approved is not None
    return action_id


def _run(config, argv):
    """Invoke a review_queue subcommand against an in-memory config."""
    import argparse

    args = review_queue.build_parser().parse_args(argv)
    args.config = None
    out, err = io.StringIO(), io.StringIO()
    handler = {
        "claim": review_queue.cmd_claim,
        "record-execution": review_queue.cmd_record_execution,
    }[argv[0]]
    # Bypass config file loading — the tests drive an in-memory config dict.
    original = review_queue._load_config_or_exit
    review_queue._load_config_or_exit = lambda _path: config
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = handler(args)
    finally:
        review_queue._load_config_or_exit = original
    assert isinstance(args, argparse.Namespace)
    return code, out.getvalue(), err.getvalue()


# ─── claim ───────────────────────────────────────────────────────────────────

class TestClaim:
    def test_claim_transitions_approved_to_executing(self, temp_project):
        action_id = _approved_action(temp_project)
        code, out, _ = _run(temp_project, ["claim", "--action-id", action_id])
        assert code == 0
        assert get_pending_action(temp_project, action_id)["state"] == "executing"

    def test_claim_emits_the_execution_envelope(self, temp_project):
        """The agent needs type, target and payload to know what to execute."""
        action_id = _approved_action(temp_project)
        _, out, _ = _run(temp_project, ["claim", "--action-id", action_id])
        envelope = json.loads(out)
        assert envelope["action_id"] == action_id
        assert envelope["state"] == "executing"
        assert envelope["type"] == "mail.send"
        assert envelope["target"] == "client@example.com"
        assert envelope["payload"]["subject"] == "Hello"
        # It must tell the agent how to close the loop.
        assert "record-execution" in json.dumps(envelope)

    def test_claim_refuses_unapproved_action(self, temp_project):
        action_id = _new_action(temp_project)  # state: requested
        code, _, err = _run(temp_project, ["claim", "--action-id", action_id])
        assert code == 1
        assert "requested" in err
        assert get_pending_action(temp_project, action_id)["state"] == "requested"

    def test_claim_refuses_second_claim(self, temp_project):
        """Two agents must not both believe they own the execution."""
        action_id = _approved_action(temp_project)
        assert _run(temp_project, ["claim", "--action-id", action_id])[0] == 0
        code, _, err = _run(temp_project, ["claim", "--action-id", action_id])
        assert code == 1
        assert "executing" in err

    def test_claim_refuses_unknown_action(self, temp_project):
        code, _, err = _run(temp_project, ["claim", "--action-id", "does-not-exist"])
        assert code == 1
        assert "not found" in err.lower()


# ─── record-execution ────────────────────────────────────────────────────────

class TestRecordExecution:
    def test_records_success_on_claimed_action(self, temp_project):
        action_id = _approved_action(temp_project)
        _run(temp_project, ["claim", "--action-id", action_id])
        code, out, _ = _run(temp_project, [
            "record-execution", "--action-id", action_id,
            "--status", "success",
            "--result-json", json.dumps({"message_id": "abc123"}),
            "--executor", "claude-cowork",
        ])
        assert code == 0
        action = get_pending_action(temp_project, action_id)
        assert action["state"] == "executed"
        assert action["result"]["success"] is True
        assert action["result"]["data"]["message_id"] == "abc123"
        assert action["result"]["executor"] == "claude-cowork"
        # The audit must be able to distinguish agent-executed from client-executed.
        assert action["result"]["executed_externally"] is True

    def test_records_failure_and_rearms_for_retry(self, temp_project):
        action_id = _approved_action(temp_project)
        _run(temp_project, ["claim", "--action-id", action_id])
        code, _, _ = _run(temp_project, [
            "record-execution", "--action-id", action_id,
            "--status", "failure", "--error", "connector returned 503",
        ])
        assert code == 0
        action = get_pending_action(temp_project, action_id)
        # Under the retry cap a failure returns to approved so it can be retried.
        assert action["state"] == "approved"
        assert action["retry_count"] == 1
        assert "503" in action["last_error"]

    def test_refuses_unclaimed_action(self, temp_project):
        """This is the bypass that matters: approved must not jump to executed."""
        action_id = _approved_action(temp_project)
        code, _, err = _run(temp_project, [
            "record-execution", "--action-id", action_id, "--status", "success",
        ])
        assert code == 1
        assert "claim" in err.lower()
        assert get_pending_action(temp_project, action_id)["state"] == "approved"

    def test_refuses_merely_requested_action(self, temp_project):
        """An unapproved action must never be recordable as executed."""
        action_id = _new_action(temp_project)
        code, _, err = _run(temp_project, [
            "record-execution", "--action-id", action_id, "--status", "success",
        ])
        assert code == 1
        assert get_pending_action(temp_project, action_id)["state"] == "requested"

    def test_refuses_double_record(self, temp_project):
        action_id = _approved_action(temp_project)
        _run(temp_project, ["claim", "--action-id", action_id])
        assert _run(temp_project, [
            "record-execution", "--action-id", action_id, "--status", "success",
        ])[0] == 0
        code, _, err = _run(temp_project, [
            "record-execution", "--action-id", action_id, "--status", "success",
        ])
        assert code == 1
        assert get_pending_action(temp_project, action_id)["state"] == "executed"

    def test_rejects_malformed_result_json(self, temp_project):
        action_id = _approved_action(temp_project)
        _run(temp_project, ["claim", "--action-id", action_id])
        code, _, err = _run(temp_project, [
            "record-execution", "--action-id", action_id,
            "--status", "success", "--result-json", "{not json",
        ])
        assert code == 1
        assert "json" in err.lower()
        # A malformed payload must not advance the state machine.
        assert get_pending_action(temp_project, action_id)["state"] == "executing"

    def test_rejects_non_object_result_json(self, temp_project):
        action_id = _approved_action(temp_project)
        _run(temp_project, ["claim", "--action-id", action_id])
        code, _, err = _run(temp_project, [
            "record-execution", "--action-id", action_id,
            "--status", "success", "--result-json", '["a", "b"]',
        ])
        assert code == 1
        assert get_pending_action(temp_project, action_id)["state"] == "executing"

    def test_failure_requires_an_error_message(self, temp_project):
        action_id = _approved_action(temp_project)
        _run(temp_project, ["claim", "--action-id", action_id])
        code, _, err = _run(temp_project, [
            "record-execution", "--action-id", action_id, "--status", "failure",
        ])
        assert code == 1
        assert "--error" in err
        assert get_pending_action(temp_project, action_id)["state"] == "executing"


# ─── audit ───────────────────────────────────────────────────────────────────

class TestAuditTrail:
    def test_externally_executed_action_is_audited(self, temp_project):
        action_id = _approved_action(temp_project)
        _run(temp_project, ["claim", "--action-id", action_id])
        _run(temp_project, [
            "record-execution", "--action-id", action_id,
            "--status", "success", "--executor", "claude-cowork",
        ])

        records = review_queue._load_audit_records(temp_project, 50)
        statuses = [r.get("status") for r in records]
        assert "executing" in statuses, f"no executing audit record; got {statuses}"
        assert "executed" in statuses, f"no executed audit record; got {statuses}"

    def test_audit_chain_stays_verifiable(self, temp_project):
        from workspace_audit import verify_audit_chain

        action_id = _approved_action(temp_project)
        _run(temp_project, ["claim", "--action-id", action_id])
        _run(temp_project, [
            "record-execution", "--action-id", action_id, "--status", "success",
        ])
        assert verify_audit_chain(temp_project) is True
