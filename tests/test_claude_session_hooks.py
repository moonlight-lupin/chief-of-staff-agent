#!/usr/bin/env python3
"""Claude Code session hooks — making a fresh cloud clone usable.

A Claude Code on the web session starts from a fresh clone: no ``.venv``, no
(gitignored) ``company.yaml``, and no project data. ``.claude/hooks`` fixes
that:

* ``session-start.sh`` builds the venv, locates the private data repo (cloning
  it when ``CHIEF_OF_STAFF_DATA_REPO`` is set), fast-forwards it, links the
  live config files into it, and exports the project root for the session.
* ``stop-sync-check.sh`` refuses to let the session end with unsynced state.

Both are no-ops outside a remote session, so local installs are untouched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HOOKS = PLUGIN_ROOT / ".claude" / "hooks"
SESSION_START = HOOKS / "session-start.sh"
STOP_CHECK = HOOKS / "stop-sync-check.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="hooks are bash scripts")

CONFIG_FILES = ("company.yaml", "drive-map.yaml", "queries.yaml")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def _base_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CHIEF_OF_STAFF_")}
    env.update({
        "CLAUDE_CODE_REMOTE": "true",
        "CHIEF_OF_STAFF_SKIP_VENV": "1",
        "CHIEF_OF_STAFF_PYTHON": sys.executable,
        "CLAUDE_ENV_FILE": str(tmp_path / "claude.env"),
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com",
    })
    env.update(extra)
    return env


@pytest.fixture
def fake_project(tmp_path):
    """A stand-in for the checked-out plugin: just the config directory."""
    root = tmp_path / "chief-of-staff-agent"
    (root / "shared" / "config").mkdir(parents=True)
    (root / ".claude").mkdir()
    return root


@pytest.fixture
def remote(tmp_path):
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    return bare


def _run(script: Path, env: dict[str, str], stdin: str = "{}") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)], env=env, input=stdin, capture_output=True, text=True, timeout=120
    )


# ─── settings.json ───────────────────────────────────────────────────────────

class TestSettings:
    def test_registers_both_hooks(self):
        settings = json.loads((PLUGIN_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        hooks = settings["hooks"]
        start_cmds = [h["command"] for block in hooks["SessionStart"] for h in block["hooks"]]
        stop_cmds = [h["command"] for block in hooks["Stop"] for h in block["hooks"]]
        assert any("session-start.sh" in c for c in start_cmds)
        assert any("stop-sync-check.sh" in c for c in stop_cmds)

    @pytest.mark.parametrize("script", [SESSION_START, STOP_CHECK])
    def test_hook_scripts_are_executable(self, script):
        assert script.is_file()
        assert os.access(script, os.X_OK)

    def test_venv_is_gitignored(self):
        ignored = (PLUGIN_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert ".venv/" in ignored


# ─── session-start.sh ────────────────────────────────────────────────────────

class TestSessionStart:
    def test_is_a_noop_outside_a_remote_session(self, tmp_path, fake_project):
        env = _base_env(tmp_path, CLAUDE_PROJECT_DIR=str(fake_project))
        env.pop("CLAUDE_CODE_REMOTE")
        proc = _run(SESSION_START, env)
        assert proc.returncode == 0
        assert not (tmp_path / "claude.env").exists()
        assert not (fake_project / ".venv").exists()

    def test_clones_the_data_repo_and_exports_its_root(self, tmp_path, fake_project, remote):
        env = _base_env(
            tmp_path,
            CLAUDE_PROJECT_DIR=str(fake_project),
            CHIEF_OF_STAFF_DATA_REPO=str(remote),
        )
        proc = _run(SESSION_START, env)
        assert proc.returncode == 0, proc.stderr
        data = tmp_path / "remote"
        assert (data / ".git").is_dir()
        exported = (tmp_path / "claude.env").read_text(encoding="utf-8")
        assert f"CHIEF_OF_STAFF_PROJECT_ROOT={data}" in exported
        assert (fake_project / ".claude" / "cos-data-dir.local").read_text(encoding="utf-8").strip() == str(data)

    def test_links_live_config_into_the_data_repo(self, tmp_path, fake_project, remote):
        data = tmp_path / "data"
        _git(tmp_path, "clone", str(remote), str(data))
        env = _base_env(tmp_path, CLAUDE_PROJECT_DIR=str(fake_project), CHIEF_OF_STAFF_DATA_DIR=str(data))
        proc = _run(SESSION_START, env)
        assert proc.returncode == 0, proc.stderr
        for name in CONFIG_FILES:
            link = fake_project / "shared" / "config" / name
            assert link.is_symlink()
            assert Path(os.readlink(link)) == data / "config" / name
        # Writing through the (dangling) link must land in the data repo.
        (fake_project / "shared" / "config" / "company.yaml").write_text("company: {}\n", encoding="utf-8")
        assert (data / "config" / "company.yaml").read_text(encoding="utf-8") == "company: {}\n"

    def test_never_replaces_an_existing_real_config(self, tmp_path, fake_project, remote):
        data = tmp_path / "data"
        _git(tmp_path, "clone", str(remote), str(data))
        real = fake_project / "shared" / "config" / "company.yaml"
        real.write_text("company: {name: Local}\n", encoding="utf-8")
        env = _base_env(tmp_path, CLAUDE_PROJECT_DIR=str(fake_project), CHIEF_OF_STAFF_DATA_DIR=str(data))
        proc = _run(SESSION_START, env)
        assert proc.returncode == 0, proc.stderr
        assert not real.is_symlink()
        assert real.read_text(encoding="utf-8") == "company: {name: Local}\n"

    def test_fast_forwards_an_existing_clone(self, tmp_path, fake_project, remote):
        data = tmp_path / "data"
        _git(tmp_path, "clone", str(remote), str(data))
        other = tmp_path / "other"
        _git(tmp_path, "clone", str(remote), str(other))
        (other / "todos.yaml").write_text("todos: []\n", encoding="utf-8")
        _git(other, "add", "-A")
        _git(other, "-c", "user.name=T", "-c", "user.email=t@e.com", "commit", "-m", "seed")
        _git(other, "push", "origin", "HEAD:main")
        env = _base_env(tmp_path, CLAUDE_PROJECT_DIR=str(fake_project), CHIEF_OF_STAFF_DATA_DIR=str(data))
        proc = _run(SESSION_START, env)
        assert proc.returncode == 0, proc.stderr
        assert (data / "todos.yaml").exists()

    def test_without_a_data_repo_it_warns_state_is_ephemeral(self, tmp_path, fake_project):
        env = _base_env(tmp_path, CLAUDE_PROJECT_DIR=str(fake_project))
        proc = _run(SESSION_START, env)
        assert proc.returncode == 0, proc.stderr
        assert "ephemeral" in proc.stdout.lower()
        assert "CHIEF_OF_STAFF_PROJECT_ROOT" not in (
            (tmp_path / "claude.env").read_text(encoding="utf-8") if (tmp_path / "claude.env").exists() else ""
        )

    def test_a_failed_clone_does_not_fail_the_session(self, tmp_path, fake_project):
        env = _base_env(
            tmp_path,
            CLAUDE_PROJECT_DIR=str(fake_project),
            CHIEF_OF_STAFF_DATA_REPO=str(tmp_path / "does-not-exist.git"),
        )
        proc = _run(SESSION_START, env)
        assert proc.returncode == 0
        assert "clone" in (proc.stdout + proc.stderr).lower()


# ─── stop-sync-check.sh ──────────────────────────────────────────────────────

class TestStopSyncCheck:
    @pytest.fixture
    def wired(self, tmp_path, fake_project, remote):
        data = tmp_path / "data"
        _git(tmp_path, "clone", str(remote), str(data))
        (fake_project / ".claude" / "cos-data-dir.local").write_text(f"{data}\n", encoding="utf-8")
        env = _base_env(tmp_path, CLAUDE_PROJECT_DIR=str(fake_project))
        return data, env

    def test_blocks_when_state_is_unsynced(self, wired):
        data, env = wired
        (data / "todos.yaml").write_text("todos: []\n", encoding="utf-8")
        proc = _run(STOP_CHECK, env, stdin=json.dumps({"stop_hook_active": False}))
        assert proc.returncode == 2
        assert "sync push" in proc.stderr

    def test_passes_when_clean(self, wired):
        _, env = wired
        proc = _run(STOP_CHECK, env, stdin=json.dumps({"stop_hook_active": False}))
        assert proc.returncode == 0, proc.stderr

    def test_does_not_loop_when_already_reminded(self, wired):
        data, env = wired
        (data / "todos.yaml").write_text("todos: []\n", encoding="utf-8")
        proc = _run(STOP_CHECK, env, stdin=json.dumps({"stop_hook_active": True}))
        assert proc.returncode == 0

    def test_is_a_noop_outside_a_remote_session(self, wired):
        data, env = wired
        env.pop("CLAUDE_CODE_REMOTE")
        (data / "todos.yaml").write_text("todos: []\n", encoding="utf-8")
        proc = _run(STOP_CHECK, env, stdin=json.dumps({"stop_hook_active": False}))
        assert proc.returncode == 0

    def test_no_data_repo_configured_never_blocks(self, tmp_path, fake_project):
        env = _base_env(tmp_path, CLAUDE_PROJECT_DIR=str(fake_project))
        proc = _run(STOP_CHECK, env, stdin=json.dumps({"stop_hook_active": False}))
        assert proc.returncode == 0


# ─── suite hygiene in cloud sessions ─────────────────────────────────────────

def test_suite_does_not_inherit_the_hosted_session_marker():
    """Running pytest inside a Claude cloud session must not flip every test
    into hosted mode; tests that want it opt in via monkeypatch.setenv."""
    assert "CLAUDE_CODE_REMOTE_SESSION_ID" not in os.environ
