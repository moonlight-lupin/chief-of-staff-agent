#!/usr/bin/env python3
"""v0.3.1 — M365 mutation guardrail gating (PR-review blocker #1 + #3b).

Proves that EVERY mutating M365GraphClient method is gated by the workspace
guardrail:

* With a clean environment (no CHIEF_OF_STAFF_AUTO_APPROVE / ALLOW_DESTRUCTIVE)
  the guardrail blocks the action — the decorated body never runs, so neither
  ``_request`` nor ``_get_token`` is called, and the returned ActionResult has
  ``success=False`` with a guardrail-block error.
* With the applicable env var set, the action proceeds (fake ``_request``
  returning a success payload).

Two documented exceptions:
* ``mail_send`` is destructive and requires CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1.
* ``calendar_cancel`` has NO restore path on Graph, so even past the gate it
  refuses with an "unsupported" failure ActionResult (blocker #3b) — and the
  generic execute path refuses it pre-execution via require_capability.

Fake-transport patterns follow tests/test_m365_graph_v031.py — no network, no
msal.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))
for skill in ("document-preparer",):
    d = PLUGIN_ROOT / "skills" / skill / "scripts"
    if d.exists() and str(d) not in sys.path:
        sys.path.insert(0, str(d))


# ── Fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def m365_config(tmp_path):
    return {
        "integrations": {"workspace": {"provider": "m365"}},
        "m365": {
            "tenant_id": "tenant-guid",
            "client_id": "client-guid",
            "client_secret_env": "M365_CLIENT_SECRET",
            "auth": "client_credentials",
            "user_principal": "cos@acme.com",
        },
        "paths": {"project_root": str(tmp_path)},
    }


@pytest.fixture(autouse=True)
def clean_guardrail_env():
    """Ensure a clean approval environment around each test."""
    keys = ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE")
    saved = {k: os.environ.pop(k, None) for k in keys}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture
def client(m365_config):
    from providers.m365_graph import M365GraphClient
    return M365GraphClient(m365_config)


# ── Method invocation table ────────────────────────────────────────────
# name -> (callable(client), gate-kind)
#   "safe"        -> proceeds with CHIEF_OF_STAFF_AUTO_APPROVE=1
#   "destructive" -> proceeds only with CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1
#   "cancel"      -> gated, but refuses with unsupported even past the gate

def _upload_file() -> str:
    tf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tf.write(b"hello")
    tf.close()
    return tf.name


def _invocations(upload_path: str, download_path: str) -> dict:
    return {
        "mail_create_draft": (lambda c: c.mail_create_draft("a@b.com", "S", "B"), "safe"),
        "mail_send": (lambda c: c.mail_send("a@b.com", "S", "B"), "destructive"),
        "mail_archive": (lambda c: c.mail_archive("m1"), "safe"),
        "mail_unarchive": (lambda c: c.mail_unarchive("m1"), "safe"),
        "mail_trash": (lambda c: c.mail_trash("m1"), "safe"),
        "mail_untrash": (lambda c: c.mail_untrash("m1"), "safe"),
        "mail_tag": (lambda c: c.mail_tag("m1", "Legal"), "safe"),
        "mail_create_tag": (lambda c: c.mail_create_tag("Legal"), "safe"),
        "calendar_create": (lambda c: c.calendar_create("T", "2026-07-10", "2026-07-10"), "safe"),
        "calendar_update": (lambda c: c.calendar_update("e1", title="New"), "safe"),
        "calendar_cancel": (lambda c: c.calendar_cancel("e1"), "cancel"),
        "files_trash": (lambda c: c.files_trash("f1"), "safe"),
        "files_upload": (lambda c: c.files_upload(upload_path), "safe"),
        "files_download": (lambda c: c.files_download("f1", download_path), "safe"),
    }


ALL_METHODS = [
    "mail_create_draft", "mail_send", "mail_archive", "mail_unarchive",
    "mail_trash", "mail_untrash", "mail_tag", "mail_create_tag",
    "calendar_create", "calendar_update", "calendar_cancel",
    "files_trash", "files_upload", "files_download",
]


# ── Block path: clean env, guardrail refuses, body never runs ───────────

class TestGuardrailBlocksInCleanEnv:
    @pytest.mark.parametrize("method_name", ALL_METHODS)
    def test_blocked_without_approval(self, client, tmp_path, method_name):
        upload = _upload_file()
        download = str(tmp_path / "out.bin")
        invoke, _kind = _invocations(upload, download)[method_name]

        # Any touch of the transport/auth seam is a failure — the body must not run.
        client._request = MagicMock(side_effect=AssertionError("_request called while blocked"))
        client._get_token = MagicMock(side_effect=AssertionError("_get_token called while blocked"))

        try:
            result = invoke(client)
        finally:
            os.unlink(upload)

        assert result["success"] is False, method_name
        assert "guardrail" in (result["error"] or "").lower(), (method_name, result["error"])
        client._request.assert_not_called()
        client._get_token.assert_not_called()
        # Nothing executed => nothing audited.
        assert result.get("audited") in (False, None), method_name


# ── Proceed path: applicable env set, action executes ───────────────────

class TestProceedsWithApproval:
    @pytest.mark.parametrize("method_name", [m for m in ALL_METHODS if m != "calendar_cancel"])
    def test_proceeds_when_env_set(self, client, tmp_path, method_name):
        upload = _upload_file()
        download = str(tmp_path / "out.bin")
        invoke, kind = _invocations(upload, download)[method_name]

        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        if kind == "destructive":
            os.environ["CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"] = "1"

        # Fake transport: a dict payload satisfies every method (GET categories,
        # POST/PATCH/PUT ids, and files_download which ignores non-bytes).
        fake_request = MagicMock(return_value={"id": "new-id", "categories": ["Legal"]})
        client._request = fake_request
        client._get_token = MagicMock(return_value="fake-token")

        try:
            with patch("workspace_audit.audit_workspace_action"):
                result = invoke(client)
        finally:
            os.unlink(upload)

        assert result["success"] is True, (method_name, result.get("error"))
        assert result["provider"] == "m365"
        assert result["audited"] is True
        fake_request.assert_called()  # the body actually ran

    def test_mail_send_still_blocked_with_only_auto_approve(self, client, tmp_path):
        """Destructive send must NOT proceed on AUTO_APPROVE alone."""
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        client._request = MagicMock(side_effect=AssertionError("send must be blocked"))
        client._get_token = MagicMock(side_effect=AssertionError("send must be blocked"))
        result = client.mail_send("a@b.com", "S", "B")
        assert result["success"] is False
        assert "destructive" in result["error"].lower() or "guardrail" in result["error"].lower()
        client._request.assert_not_called()


# ── calendar_cancel: gated + honestly unsupported (blocker #3b) ─────────

class TestCalendarCancelUnsupported:
    def test_blocked_in_clean_env(self, client):
        client._request = MagicMock(side_effect=AssertionError("must not call transport"))
        client._get_token = MagicMock(side_effect=AssertionError("must not call auth"))
        result = client.calendar_cancel("e1")
        assert result["success"] is False
        assert result["error"] == "cancelled by guardrail"
        client._request.assert_not_called()
        client._get_token.assert_not_called()

    def test_refuses_unsupported_even_past_the_gate(self, client):
        """With AUTO_APPROVE the guardrail allows it, but Graph has no restore
        path so it still refuses — WITHOUT performing any cancel."""
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        client._request = MagicMock(side_effect=AssertionError("must not call cancel"))
        client._get_token = MagicMock(side_effect=AssertionError("must not call auth"))
        result = client.calendar_cancel("e1")
        assert result["success"] is False
        assert "not supported" in result["error"].lower()
        assert "outlook" in result["error"].lower() or "recreate" in result["error"].lower()
        client._request.assert_not_called()

    def test_capability_is_false(self):
        from workspace_capabilities import supports
        assert supports("m365", "calendar.cancel") is False

    def test_require_capability_reason_mentions_restore(self):
        from workspace_capabilities import require_capability
        client = MagicMock()
        client.provider_name = "m365"
        client.supports.side_effect = lambda a: False
        err = require_capability(client, "calendar.cancel", target="e1")
        assert err is not None
        assert err["success"] is False
        low = err["error"].lower()
        assert "uncancel" in low or "restore" in low

    def test_generic_execute_refuses_approved_m365_cancel(self, m365_config):
        """An APPROVED m365 calendar.cancel pending action is refused pre-execution
        by require_capability — the provider cancel is never attempted."""
        import webhook_events
        from pending_actions import (create_pending_action, approve_pending_action,
                                      get_pending_action)
        action = create_pending_action(
            m365_config, "calendar.cancel", "m365", "evt1",
            {"event_id": "evt1", "reason": "meeting off"},
        )
        approve_pending_action(m365_config, action["id"])
        with patch("webhook_events.load_config", return_value=m365_config):
            rc = webhook_events.main(["execute", "--action-id", action["id"]])
        assert rc == 1
        loaded = get_pending_action(m365_config, action["id"])
        # require_capability failed -> mark_failed -> back to approved for review.
        assert loaded["state"] == "approved"
        assert "not supported" in (loaded.get("last_error", "") or "").lower()
