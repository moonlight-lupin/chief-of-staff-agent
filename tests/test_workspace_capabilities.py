#!/usr/bin/env python3
"""Tests for workspace_capabilities.py."""

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


class TestCapabilities:
    def test_google_api_capabilities(self):
        from workspace_capabilities import get_capabilities
        caps = get_capabilities("google_api")
        assert caps["gmail.search"] is True
        assert caps["gmail.draft"] is True         # SA REST drafts.create (v0.3.15)
        assert caps["gmail.send"] is True          # supported but destructive
        assert caps["calendar.create"] is True
        assert caps["drive.upload"] is True
        assert caps["files.untrash"] is True       # SA REST files.update trashed=False (v0.3.17)
        assert caps["drive.untrash"] is True

    def test_composio_capabilities(self):
        from workspace_capabilities import get_capabilities
        caps = get_capabilities("composio")
        assert caps["gmail.search"] is True
        assert caps["gmail.draft"] is True
        assert caps["gmail.send"] is True          # execution-verified 2026-07-16
        assert caps["mail.list_tags"] is True      # execution-verified 2026-07-16
        assert caps["mail.archive"] is True        # execution-verified 2026-07-16 (v0.3.14 hardened path)
        assert caps["drive.search"] is True
        assert caps["files.untrash"] is True       # GOOGLEDRIVE_UNTRASH_FILE (v0.3.17)
        assert caps["calendar.cancel"] is False

    def test_composio_microsoft_writes_reflect_live_execution(self):
        # A write is True ONLY if it EXECUTED successfully against the live
        # service; anything not execution-verified stays False. This tripwire
        # exists because catalog-plausible slugs and never-run code paths have
        # repeatedly proven wrong when actually executed (v0.3.7, v0.3.9). Keep
        # this list keyed to the dated live runs below — do NOT add a capability
        # here until a live run has actually exercised it.
        #
        # LIVE VERIFICATION LEDGER (all against live Outlook + OneDrive):
        #   2026-07-16 PR #7 — mail.draft, mail.trash, mail.archive/unarchive/
        #       untrash (full cycle), calendar.create/update/delete.
        #   2026-07-16 PR #8 — mail.send (OUTLOOK_SEND_EMAIL sent AND received),
        #       mail.list_folders (26 folders), mail.move (custom folder id).
        #   2026-07-16 PR #9 — mail.list_tags, mail.tag, mail.create_tag
        #       (CoS-Verify created + applied); files.download + files.trash via
        #       ONE_DRIVE_DOWNLOAD_FILE → ONE_DRIVE_DELETE_ITEM.
        #   2026-07-17 PR #14 — files.upload for text AND binary with only the MCP
        #       key: text via CREATE_TEXT_FILE, binary via ONE_DRIVE_ONEDRIVE_
        #       UPLOAD_FILE with a FileUploadable staged over the MCP remote
        #       sandbox (no COMPOSIO_API_KEY). A throwaway .pdf uploaded + trashed.
        from workspace_capabilities import get_capabilities
        caps = get_capabilities("composio_microsoft:mcp")
        assert caps["mail.search"] is True
        assert caps["calendar.list"] is True
        assert caps["files.search"] is True

        # Every write below has an execution-verified entry in the ledger above.
        for action in (
            "mail.draft", "gmail.draft",
            "mail.trash", "gmail.trash",
            "mail.archive", "gmail.archive", "mail.unarchive", "mail.untrash",
            "calendar.create", "calendar.update", "calendar.delete",
            "mail.send", "mail.list_folders", "mail.move",
            "mail.list_tags", "mail.tag", "mail.create_tag",
            "files.upload", "drive.upload",
            "files.download", "drive.download",
            "files.trash", "drive.trash",
        ):
            assert caps[action] is True, f"{action} should be live-verified True"

        # Not verified / policy-blocked → still False.
        assert caps["calendar.cancel"] is False
        # OneDrive restore (v0.3.20): Personal Graph + Business SharePoint
        # recycle bin with personal-site site_name scoping.
        assert caps["files.untrash"] is True
        assert caps["drive.untrash"] is True

    def test_supports(self):
        from workspace_capabilities import supports
        assert supports("google_api", "gmail.send") is True
        assert supports("composio", "gmail.send") is True
        assert supports("composio", "drive.upload") is True  # binary via MCP sandbox staging (PR #14, no COMPOSIO_API_KEY)
        assert supports("composio", "files.untrash") is True
        assert supports("composio_microsoft", "files.untrash") is True
        assert supports("m365", "files.untrash") is False
        assert supports("composio", "calendar.cancel") is False

    def test_unsupported_actions(self):
        from workspace_capabilities import unsupported_actions
        google_unsup = unsupported_actions("google_api")
        assert "gmail.draft" not in google_unsup
        assert "gmail.send" not in google_unsup
        composio_unsup = unsupported_actions("composio")
        assert "calendar.cancel" in composio_unsup
        assert "gmail.send" not in composio_unsup
        assert "files.trash" not in composio_unsup   # GDrive trash execution-verified 2026-07-16
        assert "files.upload" not in composio_unsup   # binary via MCP sandbox staging (PR #14)

    def test_unknown_provider_returns_empty(self):
        from workspace_capabilities import get_capabilities, supports
        assert get_capabilities("unknown") == {}
        assert supports("unknown", "anything") is False

    def test_all_actions(self):
        from workspace_capabilities import all_actions
        actions = all_actions()
        assert "gmail.search" in actions

    def test_workflow_requirements_exist(self):
        from workspace_capabilities import WORKFLOW_REQUIREMENTS
        assert "document.handoff" in WORKFLOW_REQUIREMENTS
        assert "meeting.gather" in WORKFLOW_REQUIREMENTS
        assert "weekly.collect" in WORKFLOW_REQUIREMENTS

    def test_unsupported_reasons_exist(self):
        from workspace_capabilities import UNSUPPORTED_REASONS
        assert ("google_api", "gmail.draft") not in UNSUPPORTED_REASONS
        assert ("composio:mcp", "calendar.cancel") in UNSUPPORTED_REASONS
        assert ("composio:mcp", "files.upload") not in UNSUPPORTED_REASONS  # supported via MCP sandbox staging (PR #14)
        assert ("composio:mcp", "files.trash") not in UNSUPPORTED_REASONS  # execution-verified True

    def test_provider_recommendations_exist(self):
        from workspace_capabilities import PROVIDER_RECOMMENDATIONS
        assert "google_api" in PROVIDER_RECOMMENDATIONS["gmail.draft"]
        assert "google_api" in PROVIDER_RECOMMENDATIONS["document.handoff"]
