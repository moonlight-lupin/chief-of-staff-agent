#!/usr/bin/env python3
"""Tests for workspace_audit.py — write action audit records."""

import sys
import os
import tempfile
import json
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


@pytest.fixture
def tmp_project():
    with tempfile.TemporaryDirectory() as d:
        config = {"paths": {"project_root": d}}
        yield config, Path(d)


class TestWorkspaceAudit:
    def test_audit_write_creates_file(self, tmp_project):
        from workspace_audit import audit_workspace_action
        config, project = tmp_project
        audit_workspace_action(config, "composio", "gmail.create_draft",
                               "GMAIL_CREATE_EMAIL_DRAFT", target="client@test.com")
        log_path = project / ".audit" / "workspace.log"
        assert log_path.exists()
        record = json.loads(log_path.read_text().strip())
        assert record["provider"] == "composio"
        assert record["operation"] == "gmail.create_draft"
        assert record["tool"] == "GMAIL_CREATE_EMAIL_DRAFT"
        assert record["target"] == "client@test.com"
        assert record["status"] == "success"
        assert "timestamp" in record

    def test_audit_appends_multiple_records(self, tmp_project):
        from workspace_audit import audit_workspace_action
        config, project = tmp_project
        audit_workspace_action(config, "composio", "calendar.create", "GOOGLECALENDAR_CREATE_EVENT", target="Meeting")
        audit_workspace_action(config, "composio", "drive.upload", "GOOGLEDRIVE_UPLOAD_FILE", target="/tmp/file.pdf")
        log_path = project / ".audit" / "workspace.log"
        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 2
        rec1 = json.loads(lines[0])
        rec2 = json.loads(lines[1])
        assert rec1["operation"] == "calendar.create"
        assert rec2["operation"] == "drive.upload"

    def test_audit_with_failed_status(self, tmp_project):
        from workspace_audit import audit_workspace_action
        config, project = tmp_project
        audit_workspace_action(config, "composio", "gmail.create_draft",
                               "GMAIL_CREATE_EMAIL_DRAFT", target="x@test.com", status="failed")
        log_path = project / ".audit" / "workspace.log"
        record = json.loads(log_path.read_text().strip())
        assert record["status"] == "failed"

    def test_audit_with_extra_fields(self, tmp_project):
        from workspace_audit import audit_workspace_action
        config, project = tmp_project
        audit_workspace_action(config, "google_api", "drive.download",
                               "GOOGLEDRIVE_DOWNLOAD_FILE", target="file_123",
                               extra={"file_size": 1024})
        log_path = project / ".audit" / "workspace.log"
        record = json.loads(log_path.read_text().strip())
        assert record["extra"]["file_size"] == 1024