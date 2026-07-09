#!/usr/bin/env python3
"""Tests for action_result_cli.py — shared CLI result printer."""

import sys
import io
import json
from pathlib import Path
from contextlib import redirect_stdout

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

from action_result_cli import print_result, print_json, summarize_result


class TestPrintResult:
    def test_json_mode_prints_json(self):
        result = {"success": True, "action": "calendar.create", "provider": "google_api", "data": {"id": "e1"}}
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_result(result, summary=False, label="Calendar event created")
        data = json.loads(buf.getvalue())
        assert data["success"] is True

    def test_summary_mode_success(self):
        result = {
            "success": True, "action": "calendar.create", "provider": "google_api",
            "target": "Team Sync", "data": {"id": "evt123"}, "audited": True, "error": None,
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_result(result, summary=True, label="Calendar event created")
        out = buf.getvalue()
        assert "✅" in out
        assert "Calendar event created" in out
        assert "Team Sync" in out
        assert "Provider: google_api" in out
        assert "Audited: yes" in out
        assert "id: evt123" in out

    def test_summary_mode_unsupported(self):
        result = {
            "success": False, "action": "gmail.draft", "provider": "google_api",
            "target": "client@test.com", "data": {},
            "error": "gmail.draft is not supported by provider google_api because google_api.py has no draft subcommand. Use provider=composio for this workflow.",
            "audited": False,
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_result(result, summary=True, label="Gmail draft")
        out = buf.getvalue()
        assert "❌" in out
        assert "google_api" in out
        assert "composio" in out
        assert "google_api.py has no draft subcommand" in out

    def test_summary_mode_failed(self):
        result = {
            "success": False, "action": "calendar.create", "provider": "composio:mcp",
            "target": "Sync", "data": {}, "error": "API timeout", "audited": True,
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_result(result, summary=True, label="Calendar event created")
        out = buf.getvalue()
        assert "❌" in out
        assert "Error: API timeout" in out

    def test_summary_mode_handoff_steps(self):
        """Workflow result with steps dict — partial completion shows ⚠️."""
        result = {
            "success": False, "action": "document.handoff", "provider": "google_api",
            "steps": {
                "drive_upload": {"success": True, "data": {"id": "f1"}},
                "gmail_draft": None,
            },
            "error": "gmail.draft is not supported by provider google_api because google_api.py has no draft subcommand.",
            "audited": False,
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_result(result, summary=True, label="Document handoff")
        out = buf.getvalue()
        # Partial: drive uploaded but draft not attempted → ⚠️
        assert "⚠️" in out
        assert "partially completed" in out.lower()
        assert "drive_upload" in out
        assert "gmail_draft" in out
        assert "composio" in out

    def test_summary_mode_handoff_success(self):
        """Handoff success with both steps completed."""
        result = {
            "success": True, "action": "document.handoff", "provider": "composio:mcp",
            "steps": {
                "drive_upload": {"success": True, "data": {"id": "f1"}},
                "gmail_draft": {"success": True, "data": {"id": "d1"}},
            },
            "error": None,
            "audited": False,
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_result(result, summary=True, label="Document handoff")
        out = buf.getvalue()
        assert "✅" in out
        assert "drive_upload: ✅ completed" in out
        assert "gmail_draft: ✅ completed" in out

    def test_print_json(self):
        result = {"success": True, "action": "test"}
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_json(result)
        data = json.loads(buf.getvalue())
        assert data["success"] is True


class TestSummarizeResult:
    def test_returns_string_not_prints(self):
        result = {"success": True, "action": "test", "provider": "google_api"}
        s = summarize_result(result, "Test action")
        assert isinstance(s, str)
        assert "✅" in s

    def test_drive_download_shows_path(self):
        result = {
            "success": True, "action": "drive.download", "provider": "google_api",
            "data": {"path": "/tmp/output.pdf"}, "audited": True,
        }
        s = summarize_result(result, "Drive file downloaded")
        assert "path: /tmp/output.pdf" in s