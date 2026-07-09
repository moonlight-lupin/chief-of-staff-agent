#!/usr/bin/env python3
"""Tests for v0.1.17 — approval hardening, concurrency, expiry, risk, metadata.

Verifies:
- Double-approval fails (second approve returns None)
- Double-execute fails (second execute returns None)
- Expired actions cannot be approved or executed
- Concurrent writes detected via optimistic versioning (ConcurrencyError)
- Recipient risk classification (internal/external/unknown)
- Approver and reason metadata stored and audited
- Cancel reason stored
- Summary command shows counts and high-risk items
- preview marks expired actions
- cleanup removes expired actions
"""

import sys
import os
import json
import io
import shutil
import tempfile
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
SKILLS_SCRIPTS = PLUGIN_ROOT / "skills"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))
for skill in ("document-preparer",):
    d = SKILLS_SCRIPTS / skill / "scripts"
    if d.exists() and str(d) not in sys.path:
        sys.path.insert(0, str(d))


@pytest.fixture
def temp_project(tmp_path):
    """Create a temp project root with config pointing to it."""
    project = tmp_path / "project"
    project.mkdir()
    config = {
        "google": {"delegate_email": "test@test.com", "account_alias": "test",
                   "domain": "phronesis-applied.com"},
        "company": {"website": "phronesis-applied.com"},
        "integrations": {"workspace": {"provider": "google_api"}},
        "paths": {"project_root": str(project)},
    }
    return config, project


@pytest.fixture
def auto_approve():
    os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
    yield
    os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)


@pytest.fixture
def google_mock():
    mock = MagicMock()
    mock.provider_name = "google_api"
    mock.supports.side_effect = lambda action: action != "gmail.draft"
    mock.gmail_send.return_value = {
        "success": True, "action": "gmail.send", "provider": "google_api",
        "target": "client@test.com", "data": {"id": "msg123"},
        "audited": True,
    }
    return mock


def _age_action(config, action_id, hours_old):
    """Manually set created_at to N hours ago."""
    from pending_actions import _load, _save
    data = _load(config)
    expected_version = data.get("_version", 0)
    data["actions"][action_id]["created_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=hours_old)
    ).isoformat()
    _save(config, data, expected_version=expected_version)


# ─── Concurrency ───────────────────────────────────────────────

class TestOptimisticVersioning:
    """Test optimistic versioning for concurrent write protection."""

    def test_version_increments_on_save(self, temp_project):
        from pending_actions import create_pending_action, _load
        config, project = temp_project
        create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        data = _load(config)
        assert data["_version"] >= 1

    def test_concurrent_write_raises(self, temp_project):
        from pending_actions import create_pending_action, _load, _save, ConcurrencyError
        config, project = temp_project
        # Create initial action
        create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        # Load at version V
        data1 = _load(config)
        v1 = data1["_version"]
        # Simulate another writer saves in between
        data2 = _load(config)
        data2["actions"]["xxx"] = {"id": "xxx", "state": "requested"}
        _save(config, data2, expected_version=data2.get("_version", 0))
        # Now try to save with stale version — should raise
        with pytest.raises(ConcurrencyError):
            _save(config, data1, expected_version=v1)

    def test_save_without_version_check_always_works(self, temp_project):
        from pending_actions import create_pending_action, _load, _save
        config, project = temp_project
        create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        v1 = _load(config)["_version"]
        # Save without version check — reloads and increments
        data = _load(config)
        _save(config, data)
        v2 = _load(config)["_version"]
        assert v2 > v1


# ─── Double-approve / Double-execute ──────────────────────────

class TestDoubleApproveDoubleExecute:
    """Verify state machine prevents double transitions."""

    def test_double_approve_fails(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        first = approve_pending_action(config, action["id"])
        assert first is not None
        assert first["state"] == "approved"
        second = approve_pending_action(config, action["id"])
        assert second is None  # already approved

    def test_double_execute_fails(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, mark_executing, mark_executed
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        mark_executing(config, action["id"])
        first = mark_executed(config, action["id"], {"success": True})
        assert first is not None
        assert first["state"] == "executed"
        second = mark_executed(config, action["id"], {"success": True})
        assert second is None  # already executed

    def test_execute_without_approve_fails(self, temp_project):
        from pending_actions import create_pending_action, mark_executed
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        result = mark_executed(config, action["id"], {"success": True})
        assert result is None

    def test_approve_executed_fails(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, mark_executed
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        mark_executed(config, action["id"], {"success": True})
        assert approve_pending_action(config, action["id"]) is None


# ─── Expiry ───────────────────────────────────────────────────

class TestExpiry:
    """Test stale action handling."""

    def test_expired_action_cannot_approve(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, EXPIRY_HOURS
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        # Age it past expiry
        _age_action(config, action["id"], EXPIRY_HOURS + 1)
        result = approve_pending_action(config, action["id"])
        assert result is None

    def test_check_expired_marks_action(self, temp_project):
        from pending_actions import create_pending_action, check_expired, get_pending_action, EXPIRY_HOURS
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        _age_action(config, action["id"], EXPIRY_HOURS + 1)
        is_exp = check_expired(config, action["id"])
        assert is_exp is True
        loaded = get_pending_action(config, action["id"])
        assert loaded["state"] == "expired"
        assert loaded["expired_at"] is not None

    def test_fresh_action_not_expired(self, temp_project):
        from pending_actions import create_pending_action, check_expired
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        assert check_expired(config, action["id"]) is False

    def test_preview_marks_expired(self, temp_project):
        from pending_actions import create_pending_action, preview_pending_action, EXPIRY_HOURS
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        _age_action(config, action["id"], EXPIRY_HOURS + 1)
        preview = preview_pending_action(config, action["id"])
        assert preview["state"] == "expired"

    def test_cancel_can_cancel_expired(self, temp_project):
        from pending_actions import create_pending_action, cancel_pending_action, check_expired, EXPIRY_HOURS
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        _age_action(config, action["id"], EXPIRY_HOURS + 1)
        check_expired(config, action["id"])
        cancelled = cancel_pending_action(config, action["id"])
        assert cancelled is not None
        assert cancelled["state"] == "cancelled"

    def test_cleanup_removes_expired(self, temp_project):
        from pending_actions import (
            create_pending_action, check_expired, cleanup_old_actions, get_pending_action, EXPIRY_HOURS
        )
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        _age_action(config, action["id"], EXPIRY_HOURS + 1)
        check_expired(config, action["id"])
        # Also age the expired_at timestamp
        from pending_actions import _load, _save
        data = _load(config)
        expected_version = data.get("_version", 0)
        data["actions"][action["id"]]["expired_at"] = (
            datetime.now(timezone.utc) - timedelta(days=31)
        ).isoformat()
        _save(config, data, expected_version=expected_version)
        removed = cleanup_old_actions(config, days=30)
        assert removed == 1
        assert get_pending_action(config, action["id"]) is None


# ─── Risk Classification ──────────────────────────────────────

class TestRiskClassification:
    """Test recipient risk classification."""

    def test_internal_domain(self, temp_project):
        from pending_actions import classify_recipient_risk
        config, project = temp_project
        risk = classify_recipient_risk("colleague@phronesis-applied.com", config)
        assert risk["level"] == "internal"
        assert "phronesis-applied.com" in risk["reason"]

    def test_external_domain(self, temp_project):
        from pending_actions import classify_recipient_risk
        config, project = temp_project
        risk = classify_recipient_risk("client@gmail.com", config)
        assert risk["level"] == "external"
        assert "gmail.com" in risk["reason"]

    def test_unknown_email(self):
        from pending_actions import classify_recipient_risk
        risk = classify_recipient_risk("notanemail", None)
        assert risk["level"] == "unknown"

    def test_risk_stored_in_action(self, temp_project):
        from pending_actions import create_pending_action, get_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api",
                                       "client@gmail.com", {"to": "client@gmail.com"})
        loaded = get_pending_action(config, action["id"])
        assert loaded["risk"] is not None
        assert loaded["risk"]["level"] == "external"

    def test_internal_risk_stored(self, temp_project):
        from pending_actions import create_pending_action, get_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api",
                                       "team@phronesis-applied.com", {"to": "team@phronesis-applied.com"})
        loaded = get_pending_action(config, action["id"])
        assert loaded["risk"]["level"] == "internal"


# ─── Approver / Reason Metadata ───────────────────────────────

class TestApproverMetadata:
    """Test approver and reason metadata on approve/cancel."""

    def test_approve_stores_approver(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approved = approve_pending_action(config, action["id"],
                                          approver="MH", reason="Client confirmed NDA terms")
        assert approved["approver"] == "MH"
        assert approved["approval_reason"] == "Client confirmed NDA terms"

    def test_approve_without_metadata(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approved = approve_pending_action(config, action["id"])
        assert approved["approver"] is None
        assert approved["approval_reason"] is None

    def test_cancel_stores_reason(self, temp_project):
        from pending_actions import create_pending_action, cancel_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        cancelled = cancel_pending_action(config, action["id"], reason="Wrong recipient")
        assert cancelled["cancel_reason"] == "Wrong recipient"

    def test_approver_in_preview(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, preview_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"], approver="MH", reason="Approved")
        preview = preview_pending_action(config, action["id"])
        assert preview["approver"] == "MH"
        assert preview["approval_reason"] == "Approved"

    def test_approver_audited(self, temp_project):
        from pending_actions import create_pending_action
        config, project = temp_project
        with patch("workspace_audit.audit_workspace_action") as mock_audit:
            from pending_actions import approve_pending_action
            action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
            approve_pending_action(config, action["id"], approver="MH", reason="OK")
        # Find the approve audit call
        approve_calls = [c for c in mock_audit.call_args_list
                         if c.kwargs.get("status") == "approved"]
        assert len(approve_calls) == 1
        extra = approve_calls[0].kwargs.get("extra", {})
        assert extra.get("approver") == "MH"
        assert extra.get("approval_reason") == "OK"


# ─── Summary ──────────────────────────────────────────────────

class TestPendingSummary:
    """Test the summary command and get_pending_summary()."""

    def test_summary_counts_by_state(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, cancel_pending_action
        from pending_actions import get_pending_summary
        config, project = temp_project
        a1 = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        a2 = create_pending_action(config, "gmail.send", "google_api", "c@d.com", {"to": "c@d.com"})
        approve_pending_action(config, a1["id"])
        cancel_pending_action(config, a2["id"])
        summary = get_pending_summary(config)
        assert summary["by_state"]["approved"] == 1
        assert summary["by_state"]["cancelled"] == 1

    def test_summary_high_risk_external(self, temp_project):
        from pending_actions import create_pending_action, get_pending_summary
        config, project = temp_project
        create_pending_action(config, "gmail.send", "google_api",
                              "client@gmail.com", {"to": "client@gmail.com"})
        summary = get_pending_summary(config)
        assert len(summary["high_risk_pending"]) == 1
        assert summary["high_risk_pending"][0]["target"] == "client@gmail.com"

    def test_summary_no_high_risk_for_internal(self, temp_project):
        from pending_actions import create_pending_action, get_pending_summary
        config, project = temp_project
        create_pending_action(config, "gmail.send", "google_api",
                              "team@phronesis-applied.com", {"to": "team@phronesis-applied.com"})
        summary = get_pending_summary(config)
        assert len(summary["high_risk_pending"]) == 0


# ─── CLI Integration: Approver ────────────────────────────────

class TestCLIApproverMetadata:
    """Test --approver and --reason flags on the CLI."""

    def test_approve_with_approver_flag(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock):
            import send_email
            buf = io.StringIO()
            with redirect_stdout(buf):
                send_email.main(["prepare", "--to", "a@b.com", "--subject", "S", "--body", "B"])
            action = json.loads(buf.getvalue())
            rc = send_email.main([
                "approve", "--action-id", action["id"],
                "--approver", "MH", "--reason", "Confirmed by phone",
            ])
        assert rc == 0
        from pending_actions import get_pending_action
        loaded = get_pending_action(config, action["id"])
        assert loaded["approver"] == "MH"
        assert loaded["approval_reason"] == "Confirmed by phone"

    def test_cancel_with_reason_flag(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock):
            import send_email
            buf = io.StringIO()
            with redirect_stdout(buf):
                send_email.main(["prepare", "--to", "a@b.com", "--subject", "S", "--body", "B"])
            action = json.loads(buf.getvalue())
            rc = send_email.main(["cancel", "--action-id", action["id"], "--reason", "Wrong address"])
        assert rc == 0
        from pending_actions import get_pending_action
        loaded = get_pending_action(config, action["id"])
        assert loaded["cancel_reason"] == "Wrong address"

    def test_summary_command(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock):
            import send_email
            send_email.main(["prepare", "--to", "a@b.com", "--subject", "S", "--body", "B"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = send_email.main(["--summary", "summary"])
        assert rc == 0
        out = buf.getvalue()
        assert "Pending actions" in out
        assert "requested" in out

    def test_list_shows_risk_tag(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock):
            import send_email
            send_email.main(["prepare", "--to", "client@gmail.com", "--subject", "S", "--body", "B"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                send_email.main(["--summary", "list"])
        out = buf.getvalue()
        assert "⚠️external" in out

    def test_preview_summary_shows_risk(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock):
            import send_email
            buf = io.StringIO()
            with redirect_stdout(buf):
                send_email.main(["prepare", "--to", "client@gmail.com", "--subject", "S", "--body", "B"])
            action = json.loads(buf.getvalue())
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                send_email.main(["--summary", "preview", "--action-id", action["id"]])
        out = buf2.getvalue()
        assert "Risk:" in out
        assert "external" in out


# ─── Delivery Channel Hook ────────────────────────────────────

class TestDeliveryHook:
    """Test format_preview_for_delivery and get_actions_for_delivery."""

    def test_format_preview_requested(self, temp_project):
        from pending_actions import create_pending_action, preview_pending_action, format_preview_for_delivery
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api",
                                       "client@gmail.com", {"to": "client@gmail.com",
                                                            "subject": "NDA Review",
                                                            "body": "Please sign the NDA."})
        preview = preview_pending_action(config, action["id"])
        msg = format_preview_for_delivery(action["id"], preview)
        assert "📨" in msg
        assert "client@gmail.com" in msg
        assert "NDA Review" in msg
        assert "Approve:" in msg
        assert "Cancel:" in msg
        assert action["id"] in msg

    def test_format_preview_approved(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action
        from pending_actions import preview_pending_action, format_preview_for_delivery
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api",
                                       "a@b.com", {"to": "a@b.com", "subject": "S", "body": "B"})
        approve_pending_action(config, action["id"])
        preview = preview_pending_action(config, action["id"])
        msg = format_preview_for_delivery(action["id"], preview)
        assert "✅" in msg
        assert "Execute:" in msg

    def test_format_preview_shows_risk(self, temp_project):
        from pending_actions import create_pending_action, preview_pending_action, format_preview_for_delivery
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api",
                                       "client@gmail.com", {"to": "client@gmail.com",
                                                            "subject": "S", "body": "B"})
        preview = preview_pending_action(config, action["id"])
        msg = format_preview_for_delivery(action["id"], preview)
        assert "⚠️" in msg
        assert "external" in msg

    def test_get_actions_for_delivery(self, temp_project):
        from pending_actions import create_pending_action, get_actions_for_delivery
        config, project = temp_project
        create_pending_action(config, "gmail.send", "google_api",
                              "a@b.com", {"to": "a@b.com", "subject": "S1", "body": "B1"})
        create_pending_action(config, "gmail.send", "google_api",
                              "c@d.com", {"to": "c@d.com", "subject": "S2", "body": "B2"})
        items = get_actions_for_delivery(config)
        assert len(items) == 2
        for item in items:
            assert "formatted_message" in item
            assert "risk_level" in item
            assert item["id"] in item["formatted_message"]

    def test_get_actions_for_delivery_excludes_expired(self, temp_project):
        from pending_actions import create_pending_action, get_actions_for_delivery, EXPIRY_HOURS
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api",
                                       "a@b.com", {"to": "a@b.com", "subject": "S", "body": "B"})
        _age_action(config, action["id"], EXPIRY_HOURS + 1)
        items = get_actions_for_delivery(config)
        assert len(items) == 0  # expired, excluded

    def test_get_actions_for_delivery_excludes_non_requested(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, get_actions_for_delivery
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api",
                                       "a@b.com", {"to": "a@b.com", "subject": "S", "body": "B"})
        approve_pending_action(config, action["id"])
        items = get_actions_for_delivery(config)
        assert len(items) == 0  # approved, not requested