#!/usr/bin/env python3
"""Git-backed state sync — keeping project_root alive across cloud sessions.

In a hosted Claude Code session the VM is ephemeral: anything under
``paths.project_root`` is lost at teardown. ``state_sync`` lets that directory
be a clone of a *private* data repository and commits/pushes it on request.

The contracts that matter:

1. Data never lands in the plugin repository (which may be public) — neither by
   placing project_root inside the plugin checkout nor by pointing the data
   repo's remote at the plugin's own remote.
2. Secrets (``.env``) and SQLite sidecars (``-wal``/``-shm``) are never
   committed; a data repo that already tracks ``.env`` is refused.
3. ``state.db`` is WAL-checkpointed before commit, so the committed file holds
   every committed transaction.
4. Pull is fast-forward only; a diverged or dirty tree is refused, not merged.
5. Credentials embedded in a remote URL are never echoed back.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

import state_sync


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def _identify(repo: Path) -> None:
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")


@pytest.fixture
def data_repo(tmp_path, monkeypatch):
    """A clone of an empty bare remote, standing in for the private data repo."""
    # Keep the plugin-repo guard pointed somewhere unrelated to tmp_path.
    fake_plugin = tmp_path / "plugin"
    fake_plugin.mkdir()
    monkeypatch.setattr(state_sync, "PLUGIN_ROOT", fake_plugin)

    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    data = tmp_path / "data"
    _git(tmp_path, "clone", str(remote), str(data))
    _identify(data)
    _git(data, "checkout", "-B", "main")
    return data


def _second_clone(tmp_path: Path, name: str = "other") -> Path:
    other = tmp_path / name
    _git(tmp_path, "clone", str(tmp_path / "remote.git"), str(other))
    _identify(other)
    return other


# ─── status ──────────────────────────────────────────────────────────────────

class TestStatus:
    def test_plain_directory_is_not_git_backed(self, tmp_path):
        report = state_sync.sync_status(tmp_path)
        assert report["git_backed"] is False
        assert "sync" in report["note"].lower() or "git" in report["note"].lower()

    def test_clone_is_git_backed_and_lists_dirty_files(self, data_repo):
        (data_repo / "todos.yaml").write_text("todos: []\n", encoding="utf-8")
        report = state_sync.sync_status(data_repo)
        assert report["git_backed"] is True
        assert report["has_remote"] is True
        assert "todos.yaml" in report["dirty"]

    def test_remote_credentials_are_redacted(self, data_repo):
        _git(data_repo, "remote", "set-url", "origin", "https://x-token:s3cr3t@github.com/me/data.git")
        report = state_sync.sync_status(data_repo)
        assert "s3cr3t" not in json.dumps(report)
        assert "github.com/me/data" in report["remote"]


# ─── push ────────────────────────────────────────────────────────────────────

class TestPush:
    def test_commits_and_pushes_changes(self, data_repo, tmp_path):
        (data_repo / "pipeline.yaml").write_text("deals: []\n", encoding="utf-8")
        result = state_sync.sync_push(data_repo)
        assert result["committed"] is True
        assert result["pushed"] is True
        other = _second_clone(tmp_path)
        assert (other / "pipeline.yaml").read_text(encoding="utf-8") == "deals: []\n"

    def test_clean_tree_is_a_noop(self, data_repo):
        (data_repo / "pipeline.yaml").write_text("deals: []\n", encoding="utf-8")
        state_sync.sync_push(data_repo)
        again = state_sync.sync_push(data_repo)
        assert again["committed"] is False

    def test_works_without_a_configured_git_identity(self, data_repo, monkeypatch):
        """A fresh cloud VM may have no user.name/user.email configured."""
        _git(data_repo, "config", "--unset", "user.name")
        _git(data_repo, "config", "--unset", "user.email")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        (data_repo / "todos.yaml").write_text("todos: []\n", encoding="utf-8")
        assert state_sync.sync_push(data_repo)["committed"] is True

    def test_never_commits_secrets_or_sqlite_sidecars(self, data_repo):
        (data_repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
        (data_repo / "state.db-wal").write_bytes(b"wal")
        (data_repo / "state.db-shm").write_bytes(b"shm")
        (data_repo / "todos.yaml").write_text("todos: []\n", encoding="utf-8")
        state_sync.sync_push(data_repo)
        tracked = _git(data_repo, "ls-files").splitlines()
        assert "todos.yaml" in tracked
        assert ".gitignore" in tracked
        for name in (".env", "state.db-wal", "state.db-shm"):
            assert name not in tracked

    def test_refuses_a_repo_that_already_tracks_env(self, data_repo):
        (data_repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
        _git(data_repo, "add", "-f", ".env")
        _git(data_repo, "commit", "-m", "oops")
        with pytest.raises(state_sync.SyncError, match=r"\.env"):
            state_sync.sync_push(data_repo)

    def test_refuses_project_root_inside_the_plugin_checkout(self, tmp_path, monkeypatch):
        plugin = tmp_path / "plugin"
        _git(tmp_path, "init", "-b", "main", str(plugin))
        monkeypatch.setattr(state_sync, "PLUGIN_ROOT", plugin)
        inside = plugin / "projects" / "acme"
        inside.mkdir(parents=True)
        with pytest.raises(state_sync.SyncError, match="plugin"):
            state_sync.sync_push(inside)

    def test_refuses_a_data_repo_pointing_at_the_plugin_remote(self, data_repo, tmp_path, monkeypatch):
        plugin = tmp_path / "plugin2"
        _git(tmp_path, "init", "-b", "main", str(plugin))
        _git(plugin, "remote", "add", "origin", "https://github.com/acme/chief-of-staff-agent")
        monkeypatch.setattr(state_sync, "PLUGIN_ROOT", plugin)
        _git(data_repo, "remote", "set-url", "origin", "https://github.com/acme/chief-of-staff-agent.git")
        (data_repo / "todos.yaml").write_text("todos: []\n", encoding="utf-8")
        with pytest.raises(state_sync.SyncError, match="plugin"):
            state_sync.sync_push(data_repo)

    def test_refuses_a_directory_that_is_not_a_git_repo(self, tmp_path):
        with pytest.raises(state_sync.SyncError, match="git"):
            state_sync.sync_push(tmp_path)

    def test_checkpoints_sqlite_wal_before_commit(self, data_repo, tmp_path):
        db = data_repo / "state.db"
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES ('kept')")
        conn.commit()
        try:
            state_sync.sync_push(data_repo)
        finally:
            conn.close()
        other = _second_clone(tmp_path)
        rows = sqlite3.connect(str(other / "state.db")).execute("SELECT v FROM t").fetchall()
        assert rows == [("kept",)]

    def test_warns_about_actions_still_executing(self, data_repo):
        db = data_repo / "state.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE pending_actions (id TEXT PRIMARY KEY, state TEXT NOT NULL)")
        conn.execute("INSERT INTO pending_actions VALUES ('a1', 'executing')")
        conn.commit()
        conn.close()
        result = state_sync.sync_push(data_repo)
        assert result["committed"] is True
        assert any("executing" in w for w in result["warnings"])


# ─── pull ────────────────────────────────────────────────────────────────────

class TestPull:
    def test_fast_forwards_to_the_remote(self, data_repo, tmp_path):
        (data_repo / "todos.yaml").write_text("todos: []\n", encoding="utf-8")
        state_sync.sync_push(data_repo)
        other = _second_clone(tmp_path)
        (other / "pipeline.yaml").write_text("deals: []\n", encoding="utf-8")
        state_sync.sync_push(other)

        result = state_sync.sync_pull(data_repo)
        assert result["updated"] is True
        assert (data_repo / "pipeline.yaml").exists()

    def test_empty_remote_is_not_an_error(self, data_repo):
        result = state_sync.sync_pull(data_repo)
        assert result["updated"] is False

    def test_refuses_to_pull_over_uncommitted_changes(self, data_repo):
        (data_repo / "todos.yaml").write_text("todos: []\n", encoding="utf-8")
        with pytest.raises(state_sync.SyncError, match="uncommitted"):
            state_sync.sync_pull(data_repo)

    def test_refuses_a_diverged_history(self, data_repo, tmp_path):
        (data_repo / "todos.yaml").write_text("todos: []\n", encoding="utf-8")
        state_sync.sync_push(data_repo)
        other = _second_clone(tmp_path)
        (other / "pipeline.yaml").write_text("deals: []\n", encoding="utf-8")
        state_sync.sync_push(other)
        (data_repo / "invoices.yaml").write_text("invoices: []\n", encoding="utf-8")
        _git(data_repo, "add", "-A")
        _git(data_repo, "commit", "-m", "local")
        with pytest.raises(state_sync.SyncError, match="diverged"):
            state_sync.sync_pull(data_repo)


# ─── stop-hook check ─────────────────────────────────────────────────────────

class TestStopCheck:
    def test_clean_and_pushed_passes(self, data_repo):
        (data_repo / "todos.yaml").write_text("todos: []\n", encoding="utf-8")
        state_sync.sync_push(data_repo)
        assert state_sync.stop_check(data_repo) == (0, "")

    def test_uncommitted_state_blocks_with_the_remedy(self, data_repo):
        (data_repo / "todos.yaml").write_text("todos: []\n", encoding="utf-8")
        code, message = state_sync.stop_check(data_repo)
        assert code == 2
        assert "sync push" in message

    def test_unpushed_commit_blocks(self, data_repo):
        (data_repo / "todos.yaml").write_text("todos: []\n", encoding="utf-8")
        state_sync.sync_push(data_repo)
        (data_repo / "pipeline.yaml").write_text("deals: []\n", encoding="utf-8")
        _git(data_repo, "add", "-A")
        _git(data_repo, "commit", "-m", "local only")
        code, _ = state_sync.stop_check(data_repo)
        assert code == 2

    def test_not_git_backed_never_blocks(self, tmp_path):
        assert state_sync.stop_check(tmp_path)[0] == 0


# ─── CLI + capability report wiring ──────────────────────────────────────────

class TestWiring:
    def test_sync_is_a_chief_of_staff_subcommand(self, data_repo, capsys, monkeypatch):
        import chief_of_staff

        monkeypatch.setenv("CHIEF_OF_STAFF_PROJECT_ROOT", str(data_repo))
        rc = chief_of_staff.main(["sync", "status", "--project-root", str(data_repo)])
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out[out.index("{"):])
        assert payload["git_backed"] is True

    def test_sync_refusal_exits_non_zero(self, tmp_path, capsys):
        import chief_of_staff

        rc = chief_of_staff.main(["sync", "push", "--project-root", str(tmp_path)])
        assert rc != 0

    def test_capabilities_treat_a_git_backed_root_as_durable_in_the_cloud(self, data_repo, monkeypatch):
        import chief_of_staff

        monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "cse_abc123")
        report = chief_of_staff.build_capability_report(
            {"integrations": {"workspace": {"provider": "agent"}},
             "paths": {"project_root": str(data_repo)}}
        )
        assert report["state_persistent"] is True
        assert report["state_sync"]["git_backed"] is True
        assert "sync push" in report["state_note"]

    def test_capabilities_never_call_the_plugin_checkout_durable(self, tmp_path, monkeypatch):
        """A root inside the plugin repo is git-backed, but syncing it is refused,
        so it must not be reported as durable storage."""
        import chief_of_staff

        plugin = tmp_path / "plugin3"
        _git(tmp_path, "init", "-b", "main", str(plugin))
        _git(plugin, "remote", "add", "origin", "https://github.com/acme/chief-of-staff-agent")
        monkeypatch.setattr(state_sync, "PLUGIN_ROOT", plugin)
        inside = plugin / "examples"
        inside.mkdir()
        monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "cse_abc123")
        report = chief_of_staff.build_capability_report(
            {"integrations": {"workspace": {"provider": "agent"}},
             "paths": {"project_root": str(inside)}}
        )
        assert report["state_persistent"] is False
        assert "plugin" in report["state_sync"]["sync_refusal"]

    def test_capabilities_still_warn_without_git(self, tmp_path, monkeypatch):
        import chief_of_staff

        monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "cse_abc123")
        report = chief_of_staff.build_capability_report(
            {"integrations": {"workspace": {"provider": "agent"}},
             "paths": {"project_root": str(tmp_path)}}
        )
        assert report["state_persistent"] is False
        assert "ephemeral" in report["state_note"].lower()
