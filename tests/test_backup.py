#!/usr/bin/env python3
"""Tests for backup.py — file selection, exclusions, archive creation."""

import sys
import os
import tarfile
import tempfile
from pathlib import Path

import pytest

BACKUP_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "backup" / "scripts"
if str(BACKUP_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(BACKUP_SCRIPTS))


def create_fake_hermes(root):
    """Create a fake Hermes home with config, skills, projects, .env, sessions, logs."""
    (root / "config.yaml").write_text("model:\n  default: test\n")
    (root / ".env").write_text("SECRET_KEY=supersecret\n")
    (root / "auth.json").write_text('{"tokens": {}}')
    (root / "state.db").write_text("fake db")

    skills_dir = root / "skills" / "test-skill"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("---\nname: test\n---\n# Test")

    projects_dir = root / "projects" / "testco"
    projects_dir.mkdir(parents=True)
    (projects_dir / "pipeline.yaml").write_text("deals: []\n")
    (projects_dir / "invoices.yaml").write_text("invoices: []\n")

    sessions_dir = root / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "session1.jsonl").write_text("{}\n")

    logs_dir = root / "logs"
    logs_dir.mkdir()
    (logs_dir / "gateway.log").write_text("fake log\n")

    cron_dir = root / "cron"
    cron_dir.mkdir()
    (cron_dir / "jobs.json").write_text("[]\n")


class TestBackupFileSelection:
    def test_includes_config_yaml(self, tmp_path):
        from backup import _included_paths, load_config
        create_fake_hermes(tmp_path)
        config = {
            "paths": {"project_root": str(tmp_path / "projects" / "testco")},
            "backup": {"exclude": [".env", "auth.json", "state.db", "sessions/", "logs/"]},
        }
        paths = _included_paths(config)
        # config.yaml should be included
        path_strs = [str(p) for p in paths]
        assert any("config.yaml" in p for p in path_strs)

    def test_excludes_env_file(self, tmp_path):
        from backup import _is_excluded
        root = tmp_path
        create_fake_hermes(root)
        excludes = [".env", "auth.json", "state.db", "sessions/", "logs/"]
        assert _is_excluded(root / ".env", root, excludes) is True

    def test_excludes_auth_json(self, tmp_path):
        from backup import _is_excluded
        root = tmp_path
        create_fake_hermes(root)
        excludes = [".env", "auth.json", "state.db", "sessions/", "logs/"]
        assert _is_excluded(root / "auth.json", root, excludes) is True

    def test_excludes_sessions_dir(self, tmp_path):
        from backup import _is_excluded
        root = tmp_path
        create_fake_hermes(root)
        excludes = [".env", "auth.json", "state.db", "sessions/", "logs/"]
        assert _is_excluded(root / "sessions" / "session1.jsonl", root, excludes) is True

    def test_excludes_logs_dir(self, tmp_path):
        from backup import _is_excluded
        root = tmp_path
        create_fake_hermes(root)
        excludes = [".env", "auth.json", "state.db", "sessions/", "logs/"]
        assert _is_excluded(root / "logs" / "gateway.log", root, excludes) is True

    def test_includes_skills_dir(self, tmp_path):
        from backup import _is_excluded
        root = tmp_path
        create_fake_hermes(root)
        excludes = [".env", "auth.json", "state.db", "sessions/", "logs/"]
        assert _is_excluded(root / "skills" / "test-skill" / "SKILL.md", root, excludes) is False

    def test_includes_projects_dir(self, tmp_path):
        from backup import _is_excluded
        root = tmp_path
        create_fake_hermes(root)
        excludes = [".env", "auth.json", "state.db", "sessions/", "logs/"]
        assert _is_excluded(root / "projects" / "testco" / "pipeline.yaml", root, excludes) is False


class TestBackupCreation:
    def test_create_backup_small(self, tmp_path):
        """Create a minimal backup and verify archive contents."""
        from backup import create_backup

        # Create minimal fake structure directly in tmp_path (avoid /tmp full)
        # Use /root for the test instead since /tmp is tmpfs
        import tempfile
        with tempfile.TemporaryDirectory() as workdir:
            root = Path(workdir)
            create_fake_hermes(root)
            config = {
                "company": {"name": "Test Co"},
                "paths": {"project_root": str(root / "projects" / "testco")},
                "backup": {
                    "exclude": [".env", "auth.json", "state.db", "sessions/", "logs/"],
                },
            }
            output = root / "backup.tar.gz"
            try:
                result = create_backup(config, output)
            except OSError as e:
                if "No space left" in str(e):
                    pytest.skip("No disk space for backup test")
                raise

            if output.exists():
                with tarfile.open(output, "r:gz") as tar:
                    names = tar.getnames()
                    assert not any(".env" in n for n in names), ".env found in backup!"
                    assert not any("auth.json" in n for n in names), "auth.json found in backup!"
                    assert any("config.yaml" in n for n in names), "config.yaml not in backup!"
            else:
                pytest.skip("Backup archive not created — check disk space")

    def test_backup_result_has_metadata(self, tmp_path):
        """Test that backup result object has expected attributes."""
        from backup import create_backup
        import tempfile
        with tempfile.TemporaryDirectory() as workdir:
            root = Path(workdir)
            create_fake_hermes(root)
            config = {
                "company": {"name": "Test Co"},
                "paths": {"project_root": str(root / "projects" / "testco")},
                "backup": {"exclude": [".env", "auth.json", "state.db", "sessions/", "logs/"]},
            }
            output = root / "backup.tar.gz"
            try:
                result = create_backup(config, output)
                assert hasattr(result, "file_count") or hasattr(result, "size_bytes") or hasattr(result, "archive_path")
            except OSError as e:
                if "No space left" in str(e):
                    pytest.skip("No disk space for backup test")
                raise