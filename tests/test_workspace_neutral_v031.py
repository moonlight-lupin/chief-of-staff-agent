#!/usr/bin/env python3
"""Tests for v0.3.1 — provider-neutral method names, guarded decorator,
registry factory, and capability legacy-alias resolution (Phase 0 refactor).

Covers:
- Deprecated gmail_*/drive_* aliases delegate to neutral methods and warn.
- Registry factory: unknown provider (ValueError) and registered-but-missing
  module (ImportError); register_provider adds a working entry.
- guarded decorator: success, guardrail-block, and failure-audit paths.
- Capability matrix: legacy keys resolve to neutral keys; merged dict exposes both.
"""

import sys
import os
import warnings
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


@pytest.fixture
def google_client():
    from providers.google_workspace import GoogleWorkspaceClient
    with patch("providers.google_workspace._find_google_api_script", return_value=Path("/fake/google_api.py")):
        return GoogleWorkspaceClient({"google": {"delegate_email": "f@t.com", "account_alias": "acme"}})


@pytest.fixture(autouse=True)
def clean_env():
    for key in ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"):
        os.environ.pop(key, None)
    yield
    for key in ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"):
        os.environ.pop(key, None)


# ── Alias delegation + DeprecationWarning ──────────────────────────────

class TestDeprecatedAliases:
    def test_gmail_search_alias_warns_and_delegates(self, google_client):
        with patch.object(google_client, "mail_search", return_value=[{"id": "m1"}]) as neutral:
            with pytest.warns(DeprecationWarning, match="gmail_search"):
                result = google_client.gmail_search("is:unread", max_results=3)
        neutral.assert_called_once_with("is:unread", 3)
        assert result == [{"id": "m1"}]

    def test_drive_upload_alias_warns_and_delegates(self, google_client):
        with patch.object(google_client, "files_upload", return_value={"ok": True}) as neutral:
            with pytest.warns(DeprecationWarning, match="drive_upload"):
                google_client.drive_upload("/tmp/x.pdf", parent_id="folder1")
        neutral.assert_called_once_with("/tmp/x.pdf", "folder1")

    def test_gmail_label_alias_translates_kwargs(self, google_client):
        """gmail_label(message_id=, label_id=) must reach mail_tag(message_id, tag_id)."""
        with patch.object(google_client, "mail_tag", return_value={"label_id": "L1"}) as neutral:
            with pytest.warns(DeprecationWarning, match="gmail_label"):
                google_client.gmail_label(message_id="m1", label_id="L1")
        neutral.assert_called_once_with("m1", "L1")

    def test_gmail_create_label_alias_translates_kwargs(self, google_client):
        with patch.object(google_client, "mail_create_tag", return_value={"id": "L2"}) as neutral:
            with pytest.warns(DeprecationWarning, match="gmail_create_label"):
                google_client.gmail_create_label(label_name="Clients")
        neutral.assert_called_once_with("Clients")

    def test_neutral_call_does_not_warn(self, google_client):
        with patch.object(google_client, "_run", return_value=(0, "[]", "")):
            with warnings.catch_warnings():
                warnings.simplefilter("error", DeprecationWarning)
                # Neutral name must NOT emit a DeprecationWarning.
                google_client.mail_search("is:unread")


# ── Registry factory ───────────────────────────────────────────────────

class TestRegistryFactory:
    def test_unknown_provider_raises_valueerror_listing_names(self):
        from workspace_client import get_workspace_client
        with pytest.raises(ValueError, match="Unknown workspace provider") as exc:
            get_workspace_client({"integrations": {"workspace": {"provider": "nope"}}})
        # Message lists registered providers.
        assert "google_api" in str(exc.value)
        assert "composio" in str(exc.value)

    def test_registered_but_missing_module_raises_importerror(self):
        """A provider registered to a non-existent module raises ImportError.

        (m365 and agent now ship real modules — see the resolve tests below — so
        this uses a synthetic registration to exercise the missing-module path.)
        """
        from workspace_client import register_provider, get_workspace_client
        register_provider("ghost", "providers.does_not_exist_xyz", "GhostClient")
        with pytest.raises(ImportError) as exc:
            get_workspace_client({"integrations": {"workspace": {"provider": "ghost"}}})
        msg = str(exc.value)
        assert "ghost" in msg
        assert "providers.does_not_exist_xyz" in msg

    def test_m365_provider_resolves(self):
        # Phase 2 ships providers.m365_graph, so the pre-registered "m365"
        # provider now resolves to a working Graph client.
        from workspace_client import get_workspace_client
        from providers.m365_graph import M365GraphClient
        client = get_workspace_client({
            "integrations": {"workspace": {"provider": "m365"}},
            "m365": {"tenant_id": "t", "client_id": "c", "user_principal": "u@x.com"},
        })
        assert isinstance(client, M365GraphClient)
        assert client.provider_name == "m365"

    def test_agent_provider_resolves(self):
        # Phase 1 (fetch/compute split) ships providers.agent_workspace, so the
        # pre-registered "agent" provider now resolves to a working client whose
        # fetch methods raise NotImplementedError pointing at the --input workflow.
        from workspace_client import get_workspace_client
        client = get_workspace_client({"integrations": {"workspace": {"provider": "agent"}}})
        assert client.provider_name == "agent"
        assert client.health_check() is True
        with pytest.raises(NotImplementedError):
            client.mail_search("q")

    def test_register_provider_adds_working_entry(self):
        from workspace_client import register_provider, get_workspace_client, registered_providers
        # Point a fresh name at the real google module/factory.
        register_provider("google_clone", "providers.google_workspace", "GoogleWorkspaceClient")
        assert "google_clone" in registered_providers()
        with patch("providers.google_workspace._find_google_api_script", return_value=Path("/fake")):
            from providers.google_workspace import GoogleWorkspaceClient
            client = get_workspace_client({"integrations": {"workspace": {"provider": "google_clone"}}})
        assert isinstance(client, GoogleWorkspaceClient)

    def test_default_provider_is_google(self):
        from workspace_client import get_workspace_client
        from providers.google_workspace import GoogleWorkspaceClient
        with patch("providers.google_workspace._find_google_api_script", return_value=Path("/fake")):
            client = get_workspace_client({})
        assert isinstance(client, GoogleWorkspaceClient)


# ── guarded decorator ──────────────────────────────────────────────────

from workspace_guardrails import guarded  # noqa: E402


class _GuardedClient:
    def __init__(self):
        self.config = {}
        self._provider_name = "google_api"
        self.body_ran = False

    @guarded("calendar.create", target_arg="title", audit_provider="google_api")
    def do(self, title, mode="ok"):
        self.body_ran = True
        if mode == "boom":
            raise RuntimeError("kaboom")
        return {"output": mode}


class TestGuardedDecorator:
    def test_success_path_audits_and_wraps(self):
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        client = _GuardedClient()
        with patch("workspace_audit.audit_workspace_action") as mock_audit:
            result = client.do("Team Sync")
        assert result["success"] is True
        assert result["action"] == "calendar.create"
        assert result["provider"] == "google_api"
        assert result["target"] == "Team Sync"
        assert result["data"] == {"output": "ok"}
        assert result["audited"] is True
        assert client.body_ran is True
        mock_audit.assert_called_once()
        # Success audit has no status="failed"
        assert mock_audit.call_args.kwargs.get("status") != "failed"
        assert mock_audit.call_args[0][1] == "google_api"  # provider positional

    def test_guardrail_block_skips_body(self):
        # No auto-approve + non-tty => safe write blocked. Body must not run,
        # but the denial is durably audited with status="blocked".
        client = _GuardedClient()
        with patch("workspace_audit.audit_workspace_action") as mock_audit:
            result = client.do("Team Sync")
        assert result["success"] is False
        assert result["error"] == "cancelled by guardrail"
        assert result["audited"] is False
        assert client.body_ran is False
        mock_audit.assert_called_once()
        assert mock_audit.call_args.kwargs.get("status") == "blocked"

    def test_failure_path_audits_with_status_failed(self):
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        client = _GuardedClient()
        with patch("workspace_audit.audit_workspace_action") as mock_audit:
            result = client.do("Team Sync", mode="boom")
        assert result["success"] is False
        assert "kaboom" in result["error"]
        assert result["audited"] is True
        mock_audit.assert_called_once()
        assert mock_audit.call_args.kwargs.get("status") == "failed"

    def test_confirm_action_patch_is_honoured_at_call_time(self):
        client = _GuardedClient()
        with patch("workspace_guardrails.confirm_action", return_value=True), \
             patch("workspace_audit.audit_workspace_action"):
            result = client.do("Team Sync")
        assert result["success"] is True

    def test_destructive_block_error_message(self):
        """gmail.send carries the ALLOW_DESTRUCTIVE hint in its block error."""
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"

        class _Destructive:
            def __init__(self):
                self.config = {}
                self._provider_name = "google_api"

            @guarded("gmail.send", target_arg="to", audit_provider="google_api",
                     block_error="cancelled by guardrail (requires CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1)")
            def send(self, to):
                return {"output": "sent"}

        result = _Destructive().send("a@b.com")
        assert result["success"] is False
        assert "ALLOW_DESTRUCTIVE" in result["error"]


# ── Capability legacy-alias resolution ─────────────────────────────────

class TestCapabilityLegacyAliases:
    def test_legacy_and_neutral_keys_both_resolve(self):
        from workspace_capabilities import get_capabilities
        caps = get_capabilities("google_api")
        # Neutral keys present.
        assert caps["mail.send"] is True
        assert caps["files.upload"] is True
        assert caps["mail.list_tags"] is True
        # Legacy keys present with the same values.
        assert caps["gmail.send"] is True
        assert caps["drive.upload"] is True
        assert caps["gmail.labels.list"] is True

    def test_supports_resolves_legacy_key(self):
        from workspace_capabilities import supports
        assert supports("google_api", "gmail.labels.list") is True
        assert supports("google_api", "mail.list_tags") is True
        # Composio Google Drive trash execution-verified 2026-07-16 → True.
        assert supports("composio:mcp", "drive.trash") is True
        assert supports("composio:mcp", "files.trash") is True

    def test_client_supports_legacy_and_neutral(self):
        from providers.google_workspace import GoogleWorkspaceClient
        with patch("providers.google_workspace._find_google_api_script", return_value=Path("/fake")):
            client = GoogleWorkspaceClient({"google": {}})
        assert client.supports("gmail.send") is True     # legacy
        assert client.supports("mail.send") is True       # neutral
        assert client.supports("files.upload") is True     # neutral

    def test_m365_and_agent_capabilities_present(self):
        from workspace_capabilities import get_capabilities
        m365 = get_capabilities("m365")
        assert m365["mail.send"] is True
        assert m365["gmail.send"] is True  # legacy alias merged in
        agent = get_capabilities("agent")
        assert agent  # non-empty
        # Agent: all script-callable actions are False.
        assert all(v is False for v in agent.values())

    def test_legacy_alias_map_exists(self):
        from workspace_capabilities import LEGACY_ACTION_ALIASES
        assert LEGACY_ACTION_ALIASES["gmail.send"] == "mail.send"
        assert LEGACY_ACTION_ALIASES["drive.upload"] == "files.upload"
        assert LEGACY_ACTION_ALIASES["gmail.label"] == "mail.tag"
        assert LEGACY_ACTION_ALIASES["gmail.create_label"] == "mail.create_tag"
        assert LEGACY_ACTION_ALIASES["gmail.labels.list"] == "mail.list_tags"
