#!/usr/bin/env python3
"""v0.3.3 — provider-aware bootstrap.

Proves ``bootstrap.py`` can write the ``integrations.workspace.provider`` block
plus the provider's NON-SECRET config section for google_api (unchanged),
composio, and m365 — while keeping the default (no-flag) invocation byte
compatible with the pre-change output.

All tests point ``bootstrap.CONFIG_DIR`` at a tmp dir (a copy of the real
company.yaml.example) and stub ``run_checks`` so nothing touches the real repo
config or runs doctor. No secret value is ever written to disk.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

import bootstrap  # noqa: E402

REAL_EXAMPLE = PLUGIN_ROOT / "shared" / "config" / "company.yaml.example"


# ── Helpers ─────────────────────────────────────────────────────────────

def _make_args(**overrides):
    """A fully-populated args Namespace mirroring bootstrap's parser defaults."""
    base = dict(
        company=None, jurisdiction=None, operator=None, project_root=None,
        business_type=None, config=None, json=False,
        workspace_provider=None, m365_auth="client_credentials",
        tenant_id=None, client_id=None, user_principal=None,
        m365_secret_env="M365_CLIENT_SECRET", composio_user_id=None,
        esign_url=None, allow_insecure_esign_url=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def tmp_config_dir(tmp_path, monkeypatch):
    """Point CONFIG_DIR at a tmp dir seeded with the real example, and stub
    run_checks so bootstrap() never runs doctor / touches the real repo."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    shutil.copy2(REAL_EXAMPLE, cfg / "company.yaml.example")
    monkeypatch.setattr(bootstrap, "CONFIG_DIR", cfg)
    monkeypatch.setattr(bootstrap, "run_checks", lambda *a, **k: [])
    return cfg


def _load(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


# ── _provider_overlay (pure) ────────────────────────────────────────────

class TestProviderOverlay:
    def test_default_none_is_empty(self):
        overlay, req, notices, nxt = bootstrap._provider_overlay(_make_args())
        assert overlay == {} and req == [] and notices == [] and nxt == []

    def test_google_api_is_empty(self):
        overlay, req, notices, nxt = bootstrap._provider_overlay(
            _make_args(workspace_provider="google_api"))
        assert overlay == {} and req == [] and nxt == []

    def test_m365_full(self):
        overlay, req, notices, nxt = bootstrap._provider_overlay(_make_args(
            workspace_provider="m365", tenant_id="T-GUID", client_id="C-GUID",
            user_principal="cos@acme.com"))
        assert overlay["integrations"]["workspace"]["provider"] == "m365"
        m = overlay["m365"]
        assert m == {
            "tenant_id": "T-GUID", "client_id": "C-GUID",
            "client_secret_env": "M365_CLIENT_SECRET",
            "auth": "client_credentials", "user_principal": "cos@acme.com",
        }
        assert req == ["M365_CLIENT_SECRET"]
        assert notices == []          # no placeholders when both ids supplied
        assert any("--verify" in c and "m365" in c for c in nxt)
        assert any("doctor" in c for c in nxt)

    def test_m365_custom_secret_env(self):
        overlay, req, *_ = bootstrap._provider_overlay(_make_args(
            workspace_provider="m365", tenant_id="t", client_id="c",
            user_principal="u@x.com", m365_secret_env="ACME_M365_SECRET"))
        assert overlay["m365"]["client_secret_env"] == "ACME_M365_SECRET"
        assert req == ["ACME_M365_SECRET"]

    def test_m365_placeholders_when_ids_omitted(self):
        overlay, req, notices, nxt = bootstrap._provider_overlay(_make_args(
            workspace_provider="m365", user_principal="cos@acme.com"))
        m = overlay["m365"]
        assert m["tenant_id"] == "<directory-tenant-guid>"
        assert m["client_id"] == "<application-client-guid>"
        # Placeholders are announced (the "say so" requirement).
        joined = " ".join(notices)
        assert "tenant_id" in joined and "client_id" in joined

    def test_m365_device_code_no_secret_required(self):
        overlay, req, notices, nxt = bootstrap._provider_overlay(_make_args(
            workspace_provider="m365", m365_auth="device_code",
            tenant_id="t", client_id="c"))
        assert overlay["m365"]["auth"] == "device_code"
        # device_code needs no user_principal and no secret env var.
        assert "user_principal" not in overlay["m365"]
        assert req == []
        assert any("interactive" in n for n in notices)

    def test_composio_full(self):
        overlay, req, notices, nxt = bootstrap._provider_overlay(_make_args(
            workspace_provider="composio", composio_user_id="acme-alicia"))
        ws = overlay["integrations"]["workspace"]
        assert ws["provider"] == "composio"
        assert ws["user_id"] == "acme-alicia"
        assert ws["mcp"] == {
            "endpoint": "https://connect.composio.dev/mcp",
            "key_env": "COMPOSIO_MCP_KEY",
        }
        assert req == ["COMPOSIO_MCP_KEY"]
        assert any("composio" in c and "--verify" in c for c in nxt)

    def test_composio_placeholder_user_id(self):
        overlay, req, notices, nxt = bootstrap._provider_overlay(_make_args(
            workspace_provider="composio"))
        assert overlay["integrations"]["workspace"]["user_id"] == "<composio-user-id>"
        assert any("user_id" in n for n in notices)


# ── Validation ──────────────────────────────────────────────────────────

class TestValidation:
    def test_m365_client_credentials_requires_user_principal(self):
        err = bootstrap._validate_provider_args(_make_args(
            workspace_provider="m365", m365_auth="client_credentials"))
        assert err is not None and "--user-principal" in err

    def test_m365_device_code_no_user_principal_ok(self):
        err = bootstrap._validate_provider_args(_make_args(
            workspace_provider="m365", m365_auth="device_code"))
        assert err is None

    def test_missing_user_principal_main_exits_1(self, capsys):
        rc = bootstrap._main(["--workspace-provider", "m365",
                              "--m365-auth", "client_credentials"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "--user-principal" in err

    def test_google_needs_no_provider_args(self):
        assert bootstrap._validate_provider_args(_make_args()) is None


# ── Back-compat: google path leaves the provider block untouched ────────

class TestBackCompat:
    def test_google_path_no_provider_block_change(self, tmp_config_dir):
        # Write with NO provider flag; the integrations block must equal the
        # example's byte-for-byte (no provider rewrite), and no m365 key added.
        preset = bootstrap._merge_preset(_make_args())
        overlay, *_ = bootstrap._provider_overlay(_make_args())
        assert overlay == {}
        path = bootstrap._write_config(preset)
        data = _load(path)
        example = _load(REAL_EXAMPLE)
        assert data["integrations"] == example["integrations"]
        assert data["integrations"]["workspace"]["provider"] == "google_api"
        assert "m365" not in data

    def test_default_bootstrap_result_has_no_provider_keys(self, tmp_config_dir, tmp_path):
        args = _make_args(project_root=str(tmp_path / "proj"))
        result = bootstrap.bootstrap(args)
        for k in ("workspace_provider", "required_env", "provider_notices",
                  "provider_next_commands"):
            assert k not in result


# ── m365 write-through ──────────────────────────────────────────────────

class TestM365Written:
    def _write_m365(self, tmp_path, **kw):
        args = _make_args(workspace_provider="m365",
                          project_root=str(tmp_path / "proj"), **kw)
        overlay, *_ = bootstrap._provider_overlay(args)
        preset = bootstrap._merge_preset(args)
        bootstrap._deep_update(preset, overlay)
        path = bootstrap._write_config(preset)
        return path, _load(path)

    def test_m365_block_written_correctly(self, tmp_config_dir, tmp_path):
        path, data = self._write_m365(
            tmp_path, tenant_id="T-GUID", client_id="C-GUID",
            user_principal="cos@acme.com")
        assert data["integrations"]["workspace"]["provider"] == "m365"
        m = data["m365"]
        assert m["tenant_id"] == "T-GUID"
        assert m["client_id"] == "C-GUID"
        assert m["client_secret_env"] == "M365_CLIENT_SECRET"
        assert m["auth"] == "client_credentials"
        assert m["user_principal"] == "cos@acme.com"

    def test_m365_placeholder_written(self, tmp_config_dir, tmp_path):
        path, data = self._write_m365(tmp_path, user_principal="cos@acme.com")
        assert data["m365"]["tenant_id"] == "<directory-tenant-guid>"
        assert data["m365"]["client_id"] == "<application-client-guid>"

    def test_secret_never_written_to_file(self, tmp_config_dir, tmp_path, monkeypatch):
        monkeypatch.setenv("M365_CLIENT_SECRET", "TOP-SECRET-VALUE-abc123")
        path, data = self._write_m365(
            tmp_path, tenant_id="t", client_id="c", user_principal="u@x.com")
        text = Path(path).read_text(encoding="utf-8")
        assert "TOP-SECRET-VALUE-abc123" not in text
        # We reference the env var NAME, never the value.
        assert data["m365"]["client_secret_env"] == "M365_CLIENT_SECRET"


# ── Composio write-through ──────────────────────────────────────────────

class TestComposioWritten:
    def test_composio_block_written(self, tmp_config_dir, tmp_path):
        args = _make_args(workspace_provider="composio",
                          composio_user_id="acme-alicia",
                          project_root=str(tmp_path / "proj"))
        overlay, *_ = bootstrap._provider_overlay(args)
        preset = bootstrap._merge_preset(args)
        bootstrap._deep_update(preset, overlay)
        data = _load(bootstrap._write_config(preset))
        ws = data["integrations"]["workspace"]
        assert ws["provider"] == "composio"
        assert ws["user_id"] == "acme-alicia"
        assert ws["mcp"]["endpoint"] == "https://connect.composio.dev/mcp"
        assert ws["mcp"]["key_env"] == "COMPOSIO_MCP_KEY"


# ── End-to-end _main: env-var message + next commands printed ───────────

class TestMainMessaging:
    def test_m365_main_prints_env_var_and_next_commands(self, tmp_config_dir, tmp_path, capsys):
        rc = bootstrap._main([
            "--workspace-provider", "m365",
            "--tenant-id", "t", "--client-id", "c",
            "--user-principal", "cos@acme.com",
            "--project-root", str(tmp_path / "proj"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Set M365_CLIENT_SECRET before running doctor/verify." in out
        assert "connect_workspace.py --provider m365 --verify" in out
        assert "doctor.py" in out

    def test_composio_main_prints_env_var(self, tmp_config_dir, tmp_path, capsys):
        rc = bootstrap._main([
            "--workspace-provider", "composio",
            "--composio-user-id", "acme-alicia",
            "--project-root", str(tmp_path / "proj"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Set COMPOSIO_MCP_KEY before running doctor/verify." in out
        assert "connect_workspace.py --provider composio --verify" in out

    def test_m365_placeholder_main_says_so(self, tmp_config_dir, tmp_path, capsys):
        rc = bootstrap._main([
            "--workspace-provider", "m365",
            "--user-principal", "cos@acme.com",
            "--project-root", str(tmp_path / "proj"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "<directory-tenant-guid>" in out
        assert "<application-client-guid>" in out

    def test_default_main_prints_no_provider_section(self, tmp_config_dir, tmp_path, capsys):
        rc = bootstrap._main(["--project-root", str(tmp_path / "proj")])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Workspace provider:" not in out
        assert "doctor/verify" not in out
