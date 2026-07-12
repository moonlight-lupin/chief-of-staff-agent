#!/usr/bin/env python3
"""Tests for the v0.3.6 fresh-operator onboarding fixes.

This file's CONFIG/ENV half (owned here) covers the two config-plumbing
blockers surfaced by the onboarding audit:

  1. ``connect_workspace._load_config`` now auto-discovers ``company.yaml``
     through the same chain as ``config_loader`` (explicit ``--config`` >
     ``CHIEF_OF_STAFF_CONFIG`` > plugin-root ``shared/config/company.yaml`` >
     ``{}``), so the documented commands (which pass neither ``--config`` nor
     the env var) resolve the real provider instead of an empty config.

  2. ``config_loader.load_dotenv_file`` implements dependency-free ``.env``
     auto-loading, invoked from config discovery so every entrypoint that loads
     config (doctor, connect_workspace, chief_of_staff, daily_briefing) picks up
     secrets documented for ``.env``. The shell environment always wins and
     values are never logged.

Other halves of this file (if present) are owned by a different agent.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

SCRIPT = SHARED_SCRIPTS / "connect_workspace.py"

import config_loader  # noqa: E402
import connect_workspace as cw  # noqa: E402
import doctor  # noqa: E402


# ── env isolation ────────────────────────────────────────────────────────────

# Keys these tests may write into the real process environment via
# load_dotenv_file (which sets os.environ directly, not through monkeypatch).
_MANAGED_ENV_KEYS = [
    "CHIEF_OF_STAFF_CONFIG",
    "COMPOSIO_MCP_KEY",
    "M365_CLIENT_SECRET",
    "DOCUSEAL_TOKEN",
    "COS_TEST_FOO",
    "COS_TEST_BAR",
    "COS_TEST_QUOTED",
    "COS_TEST_SQUOTED",
    "COS_TEST_EMPTY",
    "COS_TEST_SHELL",
    "COS_TEST_SPACED",
]


@pytest.fixture(autouse=True)
def clean_managed_env():
    saved = {k: os.environ.get(k) for k in _MANAGED_ENV_KEYS}
    for k in _MANAGED_ENV_KEYS:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ── config fixtures ──────────────────────────────────────────────────────────

_M365_YAML = """\
company:
  name: "Test Co"
  jurisdiction: SG
  incorporation_date: "2024-01-15"
  financial_year_end: "31 Dec"
  currency: SGD
paths:
  project_root: "{project}"
integrations:
  workspace:
    provider: m365
    mode: direct
m365:
  auth: client_credentials
  tenant_id: "tenant-123"
  client_id: "client-abc"
  user_principal: "operator@test.com"
  client_secret_env: "M365_CLIENT_SECRET"
"""

_GOOGLE_YAML = """\
integrations:
  workspace:
    provider: google_api
    mode: direct
google:
  delegate_email: "founder@test.com"
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


@pytest.fixture
def m365_config(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    return _write(tmp_path, "company.yaml", _M365_YAML.format(project=project))


# ═════════════════════════ Finding 1: config discovery ═══════════════════════


class TestConnectWorkspaceConfigDiscovery:
    def test_env_var_auto_discovers_m365(self, m365_config, monkeypatch, capsys):
        """No --config: CHIEF_OF_STAFF_CONFIG resolves the m365 provider."""
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(m365_config))
        config = cw._load_config(None)
        assert config.get("integrations", {}).get("workspace", {}).get("provider") == "m365"
        # falls back audibly on stderr with the resolved path (not values)
        err = capsys.readouterr().err
        assert str(m365_config) in err
        assert "loaded config from" in err

    def test_default_path_auto_discovered(self, m365_config, monkeypatch, capsys):
        """No --config and no env var: falls back to the plugin default path."""
        monkeypatch.delenv("CHIEF_OF_STAFF_CONFIG", raising=False)
        # Monkeypatch the shared default-path resolver to point at our tmp config.
        monkeypatch.setattr(config_loader, "_default_config_path", lambda: m365_config)
        config = cw._load_config(None)
        assert config["integrations"]["workspace"]["provider"] == "m365"
        assert "loaded config from" in capsys.readouterr().err

    def test_explicit_config_wins_over_env(self, m365_config, tmp_path, monkeypatch, capsys):
        """Explicit --config beats CHIEF_OF_STAFF_CONFIG; no discovery log."""
        google_cfg = _write(tmp_path, "google.yaml", _GOOGLE_YAML)
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(m365_config))
        config = cw._load_config(str(google_cfg))
        assert config["integrations"]["workspace"]["provider"] == "google_api"
        # explicit path must NOT emit the fallback discovery line
        assert "loaded config from" not in capsys.readouterr().err

    def test_empty_everything_degrades_gracefully(self, tmp_path, monkeypatch, capsys):
        """No --config, no env var, no default file: empty dict, no crash."""
        monkeypatch.delenv("CHIEF_OF_STAFF_CONFIG", raising=False)
        missing = tmp_path / "does-not-exist" / "company.yaml"
        monkeypatch.setattr(config_loader, "_default_config_path", lambda: missing)
        assert cw._load_config(None) == {}
        assert "loaded config from" not in capsys.readouterr().err

    def test_status_cli_resolves_m365_via_env(self, m365_config, monkeypatch):
        """End-to-end via subprocess: --status reports provider m365, no lies."""
        env = dict(os.environ)
        env["CHIEF_OF_STAFF_CONFIG"] = str(m365_config)
        env.pop("M365_CLIENT_SECRET", None)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--status"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        data = json.loads(result.stdout)
        assert data["provider"] == "m365"
        # config fields the audit wrongly reported as "NOT set" are now present
        assert data["user_principal"] == "operator@test.com"
        assert data["tenant_id_set"] is True
        assert data["client_id_set"] is True
        # discovery is announced on stderr (path only)
        assert "loaded config from" in result.stderr

    def test_verify_cli_resolves_m365_via_env(self, m365_config, monkeypatch):
        """--verify sees provider m365 from the auto-discovered config."""
        import workspace_verify as wv

        captured = {}

        def fake_run(config, include_writes=False):
            captured["provider"] = (
                config.get("integrations", {}).get("workspace", {}).get("provider")
            )
            return {"provider": captured["provider"], "checks": {}, "read_ready": True}

        monkeypatch.setattr(wv, "run_verification", fake_run)
        monkeypatch.setattr(wv, "format_report", lambda rep, fmt="human": json.dumps(rep))
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(m365_config))
        monkeypatch.setattr(sys, "argv", ["connect_workspace.py", "--verify", "--json"])

        rc = cw._main()
        assert rc == 0
        assert captured["provider"] == "m365"


# ═════════════════════════ Finding 2: .env auto-loading ══════════════════════


class TestDotenvLoader:
    def test_parses_quotes_comments_blank_malformed(self, tmp_path, monkeypatch):
        for k in ("COS_TEST_FOO", "COS_TEST_QUOTED", "COS_TEST_SQUOTED",
                  "COS_TEST_EMPTY", "COS_TEST_SPACED", "COMPOSIO_MCP_KEY"):
            monkeypatch.delenv(k, raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(
            "\n"
            "# a comment line\n"
            "   # indented comment\n"
            "COS_TEST_FOO=plain_value\n"
            'COS_TEST_QUOTED="double quoted"\n'
            "COS_TEST_SQUOTED='single quoted'\n"
            "COS_TEST_EMPTY=\n"
            "  COMPOSIO_MCP_KEY = spaced_around_equals  \n"
            "this line has no equals sign\n"
            "=novalueforkey\n"
            "COS TEST SPACED=has space in key\n"
            "\n"
        )
        applied = config_loader.load_dotenv_file(env_file)

        assert os.environ["COS_TEST_FOO"] == "plain_value"
        assert os.environ["COS_TEST_QUOTED"] == "double quoted"
        assert os.environ["COS_TEST_SQUOTED"] == "single quoted"
        assert os.environ["COS_TEST_EMPTY"] == ""
        # whitespace around key and value/equals is stripped
        assert os.environ["COMPOSIO_MCP_KEY"] == "spaced_around_equals"
        # malformed lines ignored: no-equals, empty-key, whitespace-in-key
        assert "COS TEST SPACED" not in os.environ
        assert applied["COS_TEST_FOO"] == "plain_value"
        assert "COS TEST SPACED" not in applied

    def test_shell_env_wins_over_dotenv(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COS_TEST_SHELL", "from_shell")
        env_file = tmp_path / ".env"
        env_file.write_text("COS_TEST_SHELL=from_dotenv\n")
        applied = config_loader.load_dotenv_file(env_file)
        assert os.environ["COS_TEST_SHELL"] == "from_shell"
        assert "COS_TEST_SHELL" not in applied  # never overwritten

    def test_missing_file_is_noop(self, tmp_path):
        assert config_loader.load_dotenv_file(tmp_path / "nope.env") == {}

    def test_values_never_logged(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("M365_CLIENT_SECRET", raising=False)
        secret = "S3cr3t-value-should-never-appear"
        env_file = tmp_path / ".env"
        env_file.write_text(f'M365_CLIENT_SECRET="{secret}"\n')
        config_loader.load_dotenv_file(env_file)
        out = capsys.readouterr()
        assert secret not in out.out
        assert secret not in out.err
        assert os.environ["M365_CLIENT_SECRET"] == secret

    def test_load_config_triggers_dotenv(self, tmp_path, monkeypatch):
        """config_loader.load_config auto-loads the plugin-root .env."""
        monkeypatch.delenv("COMPOSIO_MCP_KEY", raising=False)
        root = tmp_path / "plugin"
        (root / "shared" / "config").mkdir(parents=True)
        (root / ".env").write_text("COMPOSIO_MCP_KEY=key_from_env_file\n")
        monkeypatch.setattr(config_loader, "_PLUGIN_ROOT", root)
        # load_config on a missing file still runs discovery (and thus .env).
        config_loader.load_config(str(tmp_path / "missing.yaml"))
        assert os.environ["COMPOSIO_MCP_KEY"] == "key_from_env_file"


# ═══════════════════ Finding 2: entrypoint coverage (doctor) ═════════════════


class TestDoctorPicksUpDotenv:
    def test_doctor_env_checks_see_dotenv_secret(self, tmp_path, monkeypatch):
        """run_checks loads .env so m365's secret check sees the value."""
        monkeypatch.delenv("M365_CLIENT_SECRET", raising=False)
        # Plugin root whose .env carries the secret.
        root = tmp_path / "plugin"
        root.mkdir()
        (root / ".env").write_text('M365_CLIENT_SECRET="from-dotenv-file"\n')
        monkeypatch.setattr(config_loader, "_PLUGIN_ROOT", root)

        # m365 config with user_principal OMITTED so the m365 check short-circuits
        # before any live health-check/network call, yet still reports the secret.
        yaml_no_upn = _M365_YAML.replace(
            '  user_principal: "operator@test.com"\n', ""
        ).format(project=tmp_path / "project")
        (tmp_path / "project").mkdir()
        cfg = tmp_path / "company.yaml"
        cfg.write_text(yaml_no_upn)

        # Keep the run fast and network-free: only exercise the m365 check.
        monkeypatch.setattr(doctor, "CHECKS", [doctor._check_m365])
        results = doctor.run_checks(config=str(cfg))

        m365_result = next(r for r in results if r.name == "m365")
        assert "M365_CLIENT_SECRET: set" in m365_result.detail
        # the secret value itself is never emitted in the report detail
        assert "from-dotenv-file" not in m365_result.detail
        # run_checks actually populated the environment from .env
        assert os.environ["M365_CLIENT_SECRET"] == "from-dotenv-file"
