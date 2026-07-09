#!/usr/bin/env python3
"""Tests for v0.1.15 — dry-run, preflight, partial ⚠️ detection.

Verifies:
- dry-run never calls provider write methods
- preflight shows execution plan without side effects
- partial completion shows ⚠️ consistently (steps-based, not error-text-based)
- capability checks run before any provider calls
"""

import sys
import os
import json
import io
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
SKILLS_SCRIPTS = PLUGIN_ROOT / "skills"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))
for skill in ("document-preparer", "calendar-manager", "drive-filer"):
    d = SKILLS_SCRIPTS / skill / "scripts"
    if d.exists() and str(d) not in sys.path:
        sys.path.insert(0, str(d))


@pytest.fixture
def auto_approve():
    os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
    yield
    os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)


@pytest.fixture
def fake_config():
    return {
        "google": {"delegate_email": "test@test.com", "account_alias": "test"},
        "integrations": {"workspace": {"provider": "google_api"}},
        "paths": {"project_root": "/tmp/test-dryrun"},
    }


def make_composio_mock():
    """Mock client that supports all actions (composio:mcp)."""
    mock = MagicMock()
    mock.provider_name = "composio:mcp"
    mock.supports.side_effect = lambda action: True
    mock.drive_upload.return_value = {
        "success": True, "action": "drive.upload", "provider": "composio:mcp",
        "data": {"id": "f1", "webViewLink": "https://drive.google.com/file/d/f1/view"},
        "audited": True,
    }
    mock.gmail_create_draft.return_value = {
        "success": True, "action": "gmail.draft", "provider": "composio:mcp",
        "data": {"id": "d1"},
        "audited": True,
    }
    mock.calendar_create.return_value = {
        "success": True, "action": "calendar.create", "provider": "composio:mcp",
        "data": {"id": "e1"},
        "audited": True,
    }
    mock.calendar_update.return_value = {
        "success": True, "action": "calendar.update", "provider": "composio:mcp",
        "data": {"id": "e1"},
        "audited": True,
    }
    mock.drive_download.return_value = {
        "success": True, "action": "drive.download", "provider": "composio:mcp",
        "data": {"path": "/tmp/out.pdf"},
        "audited": True,
    }
    return mock


def make_google_mock():
    """Mock client that supports all except gmail.draft (google_api)."""
    mock = MagicMock()
    mock.provider_name = "google_api"
    mock.supports.side_effect = lambda action: action != "gmail.draft"
    return mock


class TestDryRunNeverCallsProvider:
    """--dry-run must never call provider write methods."""

    def test_calendar_create_dry_run(self, fake_config, auto_approve):
        mock = make_composio_mock()
        with patch("calendar_actions.load_config", return_value=fake_config), \
             patch("calendar_actions.get_client", return_value=mock):
            import calendar_actions
            rc = calendar_actions.main(["create", "--title", "Test", "--start", "2026-07-15", "--end", "2026-07-15", "--dry-run"])
        assert rc == 0
        mock.calendar_create.assert_not_called()

    def test_calendar_update_dry_run(self, fake_config, auto_approve):
        mock = make_composio_mock()
        with patch("calendar_actions.load_config", return_value=fake_config), \
             patch("calendar_actions.get_client", return_value=mock):
            import calendar_actions
            rc = calendar_actions.main(["update", "--event-id", "e1", "--title", "New", "--dry-run"])
        assert rc == 0
        mock.calendar_update.assert_not_called()

    def test_drive_upload_dry_run(self, fake_config, auto_approve, tmp_path):
        test_file = tmp_path / "test.pdf"
        test_file.write_text("dummy")
        mock = make_composio_mock()
        with patch("drive_file.load_config", return_value=fake_config), \
             patch("drive_file.get_client", return_value=mock):
            import drive_file
            rc = drive_file.main(["upload", "--file", str(test_file), "--dry-run"])
        assert rc == 0
        mock.drive_upload.assert_not_called()

    def test_drive_download_dry_run(self, fake_config, auto_approve):
        mock = make_composio_mock()
        with patch("drive_file.load_config", return_value=fake_config), \
             patch("drive_file.get_client", return_value=mock):
            import drive_file
            rc = drive_file.main(["download", "--file-id", "f1", "--output", "/tmp/out.pdf", "--dry-run"])
        assert rc == 0
        mock.drive_download.assert_not_called()

    def test_draft_email_dry_run(self, fake_config, auto_approve):
        mock = make_composio_mock()
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock):
            import document_actions
            rc = document_actions.main(["draft-email", "--to", "a@b.com", "--subject", "S", "--body", "B", "--dry-run"])
        assert rc == 0
        mock.gmail_create_draft.assert_not_called()

    def test_handoff_dry_run(self, fake_config, auto_approve, tmp_path):
        test_file = tmp_path / "NDA.docx"
        test_file.write_text("dummy")
        mock = make_composio_mock()
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock):
            import document_actions
            rc = document_actions.main([
                "handoff", "--file", str(test_file),
                "--to", "c@test.com", "--subject", "NDA", "--body", "Body", "--dry-run",
            ])
        assert rc == 0
        mock.drive_upload.assert_not_called()
        mock.gmail_create_draft.assert_not_called()

    def test_document_upload_dry_run(self, fake_config, auto_approve, tmp_path):
        test_file = tmp_path / "test.pdf"
        test_file.write_text("dummy")
        mock = make_composio_mock()
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock):
            import document_actions
            rc = document_actions.main(["upload", "--file", str(test_file), "--dry-run"])
        assert rc == 0
        mock.drive_upload.assert_not_called()


class TestPreflight:
    """--preflight shows execution plan without side effects."""

    def test_handoff_preflight_composio(self, fake_config, auto_approve, tmp_path):
        """Preflight under composio shows capabilities OK and exits without writes."""
        test_file = tmp_path / "NDA.docx"
        test_file.write_text("dummy")
        mock = make_composio_mock()
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock):
            import document_actions
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = document_actions.main([
                    "handoff", "--file", str(test_file),
                    "--to", "c@test.com", "--subject", "NDA", "--body", "Body", "--preflight",
                ])
            data = json.loads(buf.getvalue())
        assert rc == 0
        assert data["action"] == "document.handoff (preflight)"
        assert data["data"]["capabilities_ok"] is True
        assert data["data"]["missing"] == []
        mock.drive_upload.assert_not_called()
        mock.gmail_create_draft.assert_not_called()

    def test_handoff_preflight_google_api(self, fake_config, auto_approve, tmp_path):
        """Preflight under google_api shows missing gmail.draft."""
        test_file = tmp_path / "NDA.docx"
        test_file.write_text("dummy")
        mock = make_google_mock()
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock):
            import document_actions
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = document_actions.main([
                    "handoff", "--file", str(test_file),
                    "--to", "c@test.com", "--subject", "NDA", "--body", "Body", "--preflight",
                ])
            data = json.loads(buf.getvalue())
        assert rc == 1  # missing capabilities
        assert data["data"]["capabilities_ok"] is False
        assert "gmail.draft" in data["data"]["missing"]
        mock.drive_upload.assert_not_called()

    def test_handoff_preflight_summary_mode(self, fake_config, auto_approve, tmp_path):
        """Preflight in summary mode prints readable plan."""
        test_file = tmp_path / "NDA.docx"
        test_file.write_text("dummy")
        mock = make_composio_mock()
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock):
            import document_actions
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                document_actions.main([
                    "--summary", "handoff", "--file", str(test_file),
                    "--to", "c@test.com", "--subject", "NDA", "--body", "Body", "--preflight",
                ])
            output = buf.getvalue()
        assert "preflight" in output.lower() or "✅" in output


class TestPartialDetection:
    """Partial completion shows ⚠️ consistently (steps-based, not error-text-based)."""

    def test_partial_handoff_shows_warning_icon(self):
        """Steps with one success and one None should show ⚠️ not ❌."""
        from action_result_cli import summarize_result
        result = {
            "success": False,
            "action": "document.handoff",
            "provider": "google_api",
            "steps": {
                "drive_upload": {"success": True, "data": {"id": "f1"}},
                "gmail_draft": None,
            },
            "error": "gmail.draft is not supported by provider google_api",
            "audited": False,
        }
        summary = summarize_result(result, "Document handoff")
        assert "⚠️" in summary
        assert "partially completed" in summary.lower()

    def test_partial_handoff_allow_partial_shows_warning(self):
        """--allow-partial result (upload succeeded, draft unsupported) shows ⚠️."""
        from action_result_cli import summarize_result
        result = {
            "success": False,
            "action": "document.handoff",
            "provider": "google_api",
            "steps": {
                "drive_upload": {"success": True, "data": {"id": "f1"}},
                "gmail_draft": None,
            },
            "error": "gmail.draft is not supported by provider google_api because google_api.py has no draft subcommand.",
            "audited": False,
        }
        summary = summarize_result(result, "Document handoff partial")
        assert "⚠️" in summary

    def test_no_partial_when_all_steps_none(self):
        """When all steps are None (nothing happened), should show ❌ not ⚠️."""
        from action_result_cli import summarize_result
        result = {
            "success": False,
            "action": "document.handoff",
            "provider": "google_api",
            "steps": {
                "drive_upload": None,
                "gmail_draft": None,
            },
            "error": "not supported",
            "audited": False,
        }
        summary = summarize_result(result, "Document handoff")
        assert "⚠️" not in summary
        assert "❌" in summary

    def test_no_partial_when_all_steps_succeeded(self):
        """When all steps succeeded, should show ✅ not ⚠️."""
        from action_result_cli import summarize_result
        result = {
            "success": True,
            "action": "document.handoff",
            "provider": "composio:mcp",
            "steps": {
                "drive_upload": {"success": True},
                "gmail_draft": {"success": True},
            },
            "error": None,
            "audited": False,
        }
        summary = summarize_result(result, "Document handoff")
        assert "✅" in summary
        assert "⚠️" not in summary


class TestDryRunOutput:
    """Dry-run output contains actionable information."""

    def test_calendar_create_dry_run_json(self, fake_config, auto_approve):
        mock = make_composio_mock()
        with patch("calendar_actions.load_config", return_value=fake_config), \
             patch("calendar_actions.get_client", return_value=mock):
            import calendar_actions
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                calendar_actions.main(["create", "--title", "Test", "--start", "2026-07-15", "--end", "2026-07-15", "--dry-run"])
            data = json.loads(buf.getvalue())
        assert "dry-run" in data["action"]
        assert data["target"] == "Test"
        assert data["audited"] is False

    def test_handoff_dry_run_shows_steps(self, fake_config, auto_approve, tmp_path):
        test_file = tmp_path / "NDA.docx"
        test_file.write_text("dummy")
        mock = make_composio_mock()
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock):
            import document_actions
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                document_actions.main([
                    "handoff", "--file", str(test_file),
                    "--to", "c@test.com", "--subject", "NDA", "--body", "Body", "--dry-run",
                ])
            data = json.loads(buf.getvalue())
        assert "dry-run" in data["action"]
        assert "drive_upload" in data["steps"]
        assert "gmail_draft" in data["steps"]
        assert data["audited"] is False