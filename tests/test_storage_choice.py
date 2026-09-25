#!/usr/bin/env python3
"""Storage choice at onboarding — git-backed project_root is opt-in.

Operators choose where project data lives when they set up:

* ``local`` — plain files on this machine (the default; nothing changes).
* ``git``   — project_root is a clone of a *private* data repo, synced with
  ``chief_of_staff.py sync`` (needed to keep state across Claude Code cloud
  sessions).

Both onboarding paths expose the choice: ``bootstrap.py --storage`` (the flag
path agents drive) and the interactive ``onboard.py`` wizard. The choice is
recorded as ``storage.mode`` and honoured afterwards: ``local`` never syncs,
even if project_root happens to sit inside some git repository.
"""

from __future__ import annotations

import builtins
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import bootstrap
import onboard
import state_sync

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REAL_EXAMPLE = PLUGIN_ROOT / "shared" / "config" / "company.yaml.example"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Keep git discovery inside tmp_path and the plugin guard pointed at a
    throwaway plugin checkout."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    fake_plugin = tmp_path / "plugin"
    fake_plugin.mkdir()
    monkeypatch.setattr(state_sync, "PLUGIN_ROOT", fake_plugin)
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "Test")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "t@example.com")


@pytest.fixture
def remote(tmp_path):
    bare = tmp_path / "data.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    return bare


@pytest.fixture
def tmp_config_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    shutil.copy2(REAL_EXAMPLE, cfg / "company.yaml.example")
    monkeypatch.setattr(bootstrap, "CONFIG_DIR", cfg)
    monkeypatch.setattr(bootstrap, "run_checks", lambda *a, **k: [])
    return cfg


def _args(**overrides):
    base = dict(
        company="Acme", jurisdiction="SG", operator="ops@acme.com", project_root=None,
        business_type=None, config=None, json=False,
        workspace_provider=None, m365_auth="client_credentials",
        tenant_id=None, client_id=None, user_principal=None,
        m365_secret_env="M365_CLIENT_SECRET", composio_user_id=None,
        composio_family="google", esign_url=None, allow_insecure_esign_url=False,
        assistant_name=None, operator_name=None, storage=None, data_repo=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _load(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


# ─── state_sync.prepare_git_storage ──────────────────────────────────────────

class TestPrepareGitStorage:
    def test_clones_the_data_repo_into_a_new_root(self, tmp_path, remote):
        root = tmp_path / "data"
        result = state_sync.prepare_git_storage(root, str(remote))
        assert result["action"] == "cloned"
        assert (root / ".git").is_dir()
        assert _git(root, "remote", "get-url", "origin") == str(remote)

    def test_keeps_an_existing_clone(self, tmp_path, remote):
        root = tmp_path / "data"
        _git(tmp_path, "clone", str(remote), str(root))
        result = state_sync.prepare_git_storage(root, str(remote))
        assert result["action"] == "existing"

    def test_without_a_remote_initialises_and_says_how_to_add_one(self, tmp_path):
        root = tmp_path / "data"
        result = state_sync.prepare_git_storage(root, None)
        assert result["action"] == "initialised"
        assert (root / ".git").is_dir()
        assert any("remote" in n for n in result["notices"])

    def test_refuses_a_root_inside_the_plugin_checkout(self, tmp_path, monkeypatch):
        plugin = tmp_path / "plugin-repo"
        _git(tmp_path, "init", "-b", "main", str(plugin))
        monkeypatch.setattr(state_sync, "PLUGIN_ROOT", plugin)
        with pytest.raises(state_sync.SyncError, match="plugin"):
            state_sync.prepare_git_storage(plugin / "projects" / "acme", None)

    def test_refuses_to_clone_over_existing_files(self, tmp_path, remote):
        root = tmp_path / "data"
        root.mkdir()
        (root / "todos.yaml").write_text("todos: []\n", encoding="utf-8")
        with pytest.raises(state_sync.SyncError, match="already"):
            state_sync.prepare_git_storage(root, str(remote))

    def test_expands_owner_slash_name_to_github(self):
        assert state_sync.data_repo_url("me/cos-data") == "https://github.com/me/cos-data"
        assert state_sync.data_repo_url("git@github.com:me/x.git") == "git@github.com:me/x.git"


# ─── bootstrap.py --storage ──────────────────────────────────────────────────

class TestBootstrapStorage:
    def test_default_writes_no_storage_block(self, tmp_config_dir, tmp_path):
        """No flag, no change: existing installs and scripts are unaffected."""
        result = bootstrap.bootstrap(_args(project_root=str(tmp_path / "data")))
        assert "storage" not in _load(result["config"])
        assert not (tmp_path / "data" / ".git").exists()

    def test_local_records_the_choice_without_git(self, tmp_config_dir, tmp_path):
        result = bootstrap.bootstrap(_args(project_root=str(tmp_path / "data"), storage="local"))
        assert _load(result["config"])["storage"] == {"mode": "local"}
        assert not (tmp_path / "data" / ".git").exists()

    def test_git_clones_the_data_repo_before_seeding_stores(self, tmp_config_dir, tmp_path, remote):
        root = tmp_path / "data"
        result = bootstrap.bootstrap(
            _args(project_root=str(root), storage="git", data_repo=str(remote))
        )
        cfg = _load(result["config"])
        assert cfg["storage"] == {"mode": "git", "data_repo": str(remote)}
        assert (root / ".git").is_dir()
        assert (root / "todos.yaml").exists()
        assert result["storage"]["action"] == "cloned"

    def test_data_repo_without_git_storage_is_rejected(self):
        err = bootstrap._validate_storage_args(_args(data_repo="me/x"))
        assert err and "--storage git" in err

    def test_cli_refuses_git_storage_inside_the_plugin_checkout(
        self, tmp_config_dir, tmp_path, monkeypatch, capsys
    ):
        plugin = tmp_path / "plugin-repo"
        _git(tmp_path, "init", "-b", "main", str(plugin))
        monkeypatch.setattr(state_sync, "PLUGIN_ROOT", plugin)
        rc = bootstrap._main([
            "--company", "Acme", "--jurisdiction", "SG", "--operator", "ops@acme.com",
            "--project-root", str(plugin / "data"), "--storage", "git",
        ])
        assert rc == 1
        assert "plugin" in capsys.readouterr().err
        assert not (tmp_config_dir / "company.yaml").exists(), "refuse before writing config"


# ─── onboard.py wizard ───────────────────────────────────────────────────────

class TestOnboardWizard:
    def _answers(self, monkeypatch, answers):
        it = iter(answers)
        monkeypatch.setattr(builtins, "input", lambda *_: next(it))

    def test_local_is_the_default_answer(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_REMOTE_SESSION_ID", raising=False)
        self._answers(monkeypatch, [""])
        assert onboard.prompt_storage() == {"mode": "local"}

    def test_choosing_git_asks_for_the_data_repo(self, monkeypatch):
        self._answers(monkeypatch, ["y", "me/cos-data"])
        assert onboard.prompt_storage() == {"mode": "git", "data_repo": "me/cos-data"}

    def test_git_is_suggested_in_a_cloud_session(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "cse_1")
        self._answers(monkeypatch, ["", "me/cos-data"])
        assert onboard.prompt_storage()["mode"] == "git"

    def test_validate_rejects_an_unknown_mode(self):
        cfg = onboard.normalize_non_interactive_config({
            "company": {"name": "Acme", "incorporation_date": "2020-01-01"},
            "storage": {"mode": "dropbox"},
        })
        with pytest.raises(onboard.OnboardingError, match="storage.mode"):
            onboard.validate_config(cfg)

    def test_non_interactive_git_preset_prepares_the_repo(self, tmp_path, remote):
        root = tmp_path / "data"
        preset = tmp_path / "preset.yaml"
        preset.write_text(yaml.safe_dump({
            "company": {"name": "Acme", "incorporation_date": "2020-01-01"},
            "user": {"email": "ops@acme.com"},
            "paths": {"project_root": str(root), "wiki_path": str(root / "wiki")},
            "storage": {"mode": "git", "data_repo": str(remote)},
        }), encoding="utf-8")
        out = tmp_path / "company.yaml"
        rc = onboard.main(["--non-interactive", "--config", str(preset), "--output", str(out)])
        assert rc == 0
        assert (root / ".git").is_dir()
        assert (root / "wiki" / "purpose.md").exists()


# ─── the choice is honoured afterwards ───────────────────────────────────────

class TestChoiceIsHonoured:
    @pytest.fixture
    def git_root(self, tmp_path, remote):
        root = tmp_path / "data"
        _git(tmp_path, "clone", str(remote), str(root))
        return root

    def _config(self, tmp_path, root, mode):
        cfg = {
            "company": {"name": "Acme", "jurisdiction": "SG"},
            "integrations": {"workspace": {"provider": "agent"}},
            "paths": {"project_root": str(root)},
        }
        if mode:
            cfg["storage"] = {"mode": mode}
        return cfg

    def test_local_mode_is_not_durable_in_the_cloud_even_inside_a_repo(self, tmp_path, git_root, monkeypatch):
        import chief_of_staff

        monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "cse_1")
        report = chief_of_staff.build_capability_report(self._config(tmp_path, git_root, "local"))
        assert report["state_persistent"] is False
        assert report["state_sync"]["mode"] == "local"

    def test_git_mode_without_a_repo_says_what_is_wrong(self, tmp_path, monkeypatch):
        import chief_of_staff

        plain = tmp_path / "plain"
        plain.mkdir()
        report = chief_of_staff.build_capability_report(self._config(tmp_path, plain, "git"))
        assert "storage.mode is git" in report["state_note"]

    def test_sync_push_refuses_local_mode(self, tmp_path, git_root, monkeypatch, capsys):
        import chief_of_staff

        cfg_path = tmp_path / "company.yaml"
        cfg_path.write_text(yaml.safe_dump(self._config(tmp_path, git_root, "local")), encoding="utf-8")
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(cfg_path))
        (git_root / "todos.yaml").write_text("todos: []\n", encoding="utf-8")
        rc = chief_of_staff.main(["sync", "push", "--project-root", str(git_root)])
        assert rc == 1
        out = capsys.readouterr().out
        assert "local" in json.loads(out[out.index("{"):])["error"]
