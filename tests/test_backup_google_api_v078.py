#!/usr/bin/env python3
"""v0.7.8 — backup.py argv contract against the google-workspace google_api.py CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPTS = PLUGIN_ROOT / "skills" / "backup" / "scripts"
FIXTURE = PLUGIN_ROOT / "tests" / "fixtures" / "google_api" / "backup_drive.py"

if str(BACKUP_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(BACKUP_SCRIPTS))


@pytest.fixture
def cli_log(tmp_path, monkeypatch):
    log = tmp_path / "google_api_calls.jsonl"
    monkeypatch.setenv("FAKE_GOOGLE_API_LOG", str(log))
    monkeypatch.setenv("GOOGLE_WORKSPACE_API", str(FIXTURE))

    def calls() -> list[list[str]]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]

    return calls


def _config(tmp_path) -> dict:
    return {
        "company": {"name": "Test Co", "slug": "testco"},
        "paths": {"project_root": str(tmp_path / "project")},
        "backup": {"drive_folder_id": "folder-abc123"},
        "google": {},
    }


class TestBackupGoogleApiArgv:
    def test_upload_uses_positional_file_and_parent(self, tmp_path, cli_log):
        from backup import upload_backup

        archive = tmp_path / "backup.tar.gz"
        archive.write_bytes(b"data")
        upload_backup(_config(tmp_path), archive, "folder-abc123")
        [cmd] = cli_log()
        assert "--account" not in cmd and "--as" not in cmd
        assert cmd[:3] == ["drive", "upload", str(archive)]
        assert "--parent" in cmd and "folder-abc123" in cmd
        assert "--file" not in cmd and "--parent-id" not in cmd

    def test_prune_search_uses_raw_query_not_folder_list(self, tmp_path, cli_log):
        from backup import prune_old_backups

        prune_old_backups("folder-abc123", 4, 12, config=_config(tmp_path), dry_run=True)
        [cmd] = cli_log()
        assert cmd[0:2] == ["drive", "search"]
        assert "--raw-query" in cmd
        assert "'folder-abc123' in parents" in cmd[2]
        assert "--folder-id" not in cmd

    def test_prune_delete_uses_positional_file_id(self, tmp_path):
        from backup import prune_old_backups

        items = {
            "files": [
                {"id": "old1", "name": "chief-of-staff-testco-20200101.tar.gz", "createdTime": "2020-01-01T00:00:00Z"},
                {"id": "keep1", "name": "chief-of-staff-testco-20260101.tar.gz", "createdTime": "2026-01-01T00:00:00Z"},
            ]
        }
        delete_args: list[list[str]] = []

        def fake_run(_config, service, command, args):
            if service == "drive" and command == "search":
                return items
            if service == "drive" and command == "delete":
                delete_args.append(list(args))
                return {"status": "deleted"}
            raise AssertionError(f"unexpected: {service} {command}")

        with patch("backup._run_google_api", side_effect=fake_run):
            prune_old_backups("folder-x", 1, 1, config=_config(tmp_path), dry_run=False)

        assert delete_args == [["old1"]]

    def test_prefers_skill_venv_python_when_present(self, tmp_path, monkeypatch):
        from backup import _google_api_python

        skill_root = tmp_path / "google-workspace"
        scripts = skill_root / "scripts"
        scripts.mkdir(parents=True)
        script = scripts / "google_api.py"
        script.write_text("# stub\n")
        venv_py = skill_root / ".venv" / "bin" / "python"
        venv_py.parent.mkdir(parents=True)
        venv_py.write_text("#!/bin/sh\n")
        venv_py.chmod(0o755)
        assert _google_api_python(script) == str(venv_py)
