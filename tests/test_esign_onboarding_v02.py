#!/usr/bin/env python3
"""Tests for esign onboarding: _esign_overlay, bootstrap --esign-url, doctor DocuSeal check.

Covers:
- _esign_overlay writes correct esign config block
- --esign-url validates HTTPS scheme
- --allow-insecure-esign-url permits http://
- Secrets (DOCUSEAL_MCP_TOKEN, DOCUSEAL_API_KEY) never written to company.yaml
- _esign_overlay with no --esign-url returns empty (back-compat)
- doctor _check_docuseal: valid key, invalid key (401), missing tokens, wrong mode
"""
from __future__ import annotations

import io
import json
import shutil
import socket
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

import bootstrap  # noqa: E402
import doctor  # noqa: E402

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


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@pytest.fixture
def tmp_config_dir(tmp_path, monkeypatch):
    """Point CONFIG_DIR at a tmp dir seeded with the real example, and stub
    run_checks so bootstrap() never runs doctor / touches the real repo."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    shutil.copy2(REAL_EXAMPLE, cfg / "company.yaml.example")
    monkeypatch.setattr(bootstrap, "CONFIG_DIR", cfg)
    monkeypatch.setattr(bootstrap, "run_checks", lambda *a, **k: [])


# ── _esign_overlay unit tests ───────────────────────────────────────────

class TestEsignOverlay:
    def test_no_esign_url_returns_empty(self):
        """Without --esign-url, overlay is empty (back-compat)."""
        args = _make_args()
        overlay, required_env, notices, next_cmds = bootstrap._esign_overlay(args)
        assert overlay == {}
        assert required_env == []
        assert notices == []
        assert next_cmds == []

    def test_https_url_writes_full_config(self):
        args = _make_args(esign_url="https://sign.example.com")
        overlay, required_env, notices, _ = bootstrap._esign_overlay(args)
        esign = overlay["esign"]
        assert esign["provider"] == "docuseal"
        assert esign["url"] == "https://sign.example.com"
        assert esign["domain"] == "sign.example.com"
        assert esign["auth_mode"] == "auto"
        assert esign["file_serving"]["mode"] == "existing"
        assert esign["defaults"]["signing_order"] == "random"
        assert esign["field_detection"]["page_indexing"] == "zero_based"
        assert "DOCUSEAL_MCP_TOKEN" in required_env
        assert "DOCUSEAL_API_KEY" in required_env

    def test_http_rejected_without_allow_insecure(self):
        args = _make_args(esign_url="http://localhost:3001")
        overlay, required_env, notices, _ = bootstrap._esign_overlay(args)
        assert overlay == {}
        assert any("HTTPS" in n for n in notices)

    def test_http_allowed_with_allow_insecure(self):
        args = _make_args(
            esign_url="http://localhost:3001",
            allow_insecure_esign_url=True,
        )
        overlay, _, _, _ = bootstrap._esign_overlay(args)
        assert overlay["esign"]["url"] == "http://localhost:3001"

    def test_schemeless_url_rejected(self):
        args = _make_args(esign_url="sign.example.com")
        overlay, _, notices, _ = bootstrap._esign_overlay(args)
        assert overlay == {}
        assert any("https://" in n for n in notices)

    def test_notices_mention_smtp_and_tokens(self):
        args = _make_args(esign_url="https://sign.example.com")
        _, _, notices, _ = bootstrap._esign_overlay(args)
        joined = "\n".join(notices)
        assert "MCP token" in joined
        assert "API key" in joined
        assert "SMTP" in joined

    def test_trailing_slash_stripped(self):
        args = _make_args(esign_url="https://sign.example.com/")
        overlay, _, _, _ = bootstrap._esign_overlay(args)
        assert overlay["esign"]["url"] == "https://sign.example.com"


# ── Bootstrap write-through tests ───────────────────────────────────────

class TestEsignWriteThrough:
    def test_esign_block_written_to_config(self, tmp_config_dir, tmp_path):
        args = _make_args(
            esign_url="https://sign.acme.com",
            project_root=str(tmp_path / "proj"),
        )
        overlay, *_ = bootstrap._esign_overlay(args)
        preset = bootstrap._merge_preset(args)
        bootstrap._deep_update(preset, overlay)
        path = bootstrap._write_config(preset)
        data = _load(path)
        assert data["esign"]["provider"] == "docuseal"
        assert data["esign"]["url"] == "https://sign.acme.com"
        assert data["esign"]["domain"] == "sign.acme.com"

    def test_secrets_never_written_to_file(self, tmp_config_dir, tmp_path, monkeypatch):
        monkeypatch.setenv("DOCUSEAL_MCP_TOKEN", "SECRET-MCP-TOKEN-abc123")
        monkeypatch.setenv("DOCUSEAL_API_KEY", "SECRET-API-KEY-xyz789")
        args = _make_args(
            esign_url="https://sign.acme.com",
            project_root=str(tmp_path / "proj"),
        )
        overlay, *_ = bootstrap._esign_overlay(args)
        preset = bootstrap._merge_preset(args)
        bootstrap._deep_update(preset, overlay)
        path = bootstrap._write_config(preset)
        text = Path(path).read_text(encoding="utf-8")
        assert "SECRET-MCP-TOKEN-abc123" not in text
        assert "SECRET-API-KEY-xyz789" not in text

    def test_operator_email_sets_provider_email(self, tmp_config_dir, tmp_path):
        args = _make_args(
            esign_url="https://sign.acme.com",
            operator="alicia@acme.com",
            project_root=str(tmp_path / "proj"),
        )
        preset = bootstrap._merge_preset(args)
        overlay, *_ = bootstrap._esign_overlay(args)
        bootstrap._deep_update(preset, overlay)
        path = bootstrap._write_config(preset)
        data = _load(path)
        assert data["esign"]["provider_email"] == "alicia@acme.com"

    def test_legacy_admin_email_normalized_to_provider_email(self, tmp_config_dir, tmp_path):
        path = bootstrap._write_config({
            "paths": {"project_root": str(tmp_path / "proj")},
            "esign": {"admin_email": "legacy@acme.com"},
        })
        data = _load(path)
        assert data["esign"]["provider_email"] == "legacy@acme.com"
        assert data["esign"]["admin_email"] == "legacy@acme.com"


# ── Bootstrap _main messaging ───────────────────────────────────────────

class TestEsignMainMessaging:
    def test_esign_main_prints_env_vars_and_notices(self, tmp_config_dir, tmp_path, capsys):
        rc = bootstrap._main([
            "--esign-url", "https://sign.acme.com",
            "--project-root", str(tmp_path / "proj"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "DocuSeal eSign Connector:" in out
        assert "DOCUSEAL_MCP_TOKEN" in out
        assert "DOCUSEAL_API_KEY" in out
        assert "SMTP" in out
        assert "doctor.py" in out

    def test_no_esign_url_no_esign_output(self, tmp_config_dir, tmp_path, capsys):
        rc = bootstrap._main([
            "--project-root", str(tmp_path / "proj"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "DocuSeal eSign Connector:" not in out


# ── Doctor _check_docuseal tests ─────────────────────────────────────────

class TestCheckDocuseal:
    @pytest.fixture(autouse=True)
    def _mock_dns(self, monkeypatch):
        """Mock DNS resolution so _unsafe_docuseal_url_reason doesn't fail on example.com."""
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])

    def _config(self, **esign_overrides):
        esign = {
            "provider": "docuseal",
            "url": "https://sign.example.com",
            "auth_mode": "auto",
        }
        esign.update(esign_overrides)
        return {"esign": esign}

    def _mock_response(self, status=200):
        resp = MagicMock()
        resp.status = status
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_skipped_when_no_esign_config(self):
        result = doctor._check_docuseal(False, None, Path("/tmp"))
        assert result.name == "docuseal"
        assert result.status == "warn"
        assert "skipped" in result.detail

    def test_skipped_when_not_docuseal(self):
        data = {"esign": {"provider": "docusign"}}
        result = doctor._check_docuseal(False, data, Path("/tmp"))
        assert result.status == "warn"

    @patch("os.getenv")
    @patch("doctor._docuseal_opener")
    def test_pass_with_both_tokens_and_valid_api_key(self, mock_opener, mock_getenv):
        mock_getenv.side_effect = lambda k, d="": {
            "DOCUSEAL_API_KEY": "valid-key",
            "DOCUSEAL_MCP_TOKEN": "valid-mcp",
        }.get(k, d)
        class _FakeOpen:
            def open(self, req, timeout=0):
                resp = MagicMock()
                resp.status = 200
                resp.__enter__ = lambda s: s
                resp.__exit__ = lambda *a: False
                return resp
        mock_opener.return_value = _FakeOpen()
        data = self._config()
        result = doctor._check_docuseal(False, data, Path("/tmp"))
        assert result.status == "pass"
        assert "API key OK" in result.detail

    @patch("os.getenv")
    @patch("doctor._docuseal_opener")
    def test_fail_on_invalid_api_key_401(self, mock_opener, mock_getenv):
        mock_getenv.side_effect = lambda k, d="": {
            "DOCUSEAL_API_KEY": "bad-key",
            "DOCUSEAL_MCP_TOKEN": "valid-mcp",
        }.get(k, d)

        call_count = [0]
        class _FakeOpen:
            def open(self, req, timeout=0):
                call_count[0] += 1
                if call_count[0] == 1:
                    resp = MagicMock(); resp.status = 200
                    resp.__enter__ = lambda s: s; resp.__exit__ = lambda *a: False
                    return resp
                raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))

        mock_opener.return_value = _FakeOpen()
        data = self._config()
        result = doctor._check_docuseal(False, data, Path("/tmp"))
        assert result.status == "fail"
        assert "401" in result.detail

    @patch("os.getenv")
    @patch("doctor._docuseal_opener")
    def test_fail_missing_mcp_token_in_auto_mode(self, mock_opener, mock_getenv):
        mock_getenv.side_effect = lambda k, d="": {
            "DOCUSEAL_API_KEY": "valid-key",
            "DOCUSEAL_MCP_TOKEN": "",  # missing
        }.get(k, d)
        class _FakeOpen:
            def open(self, req, timeout=0):
                resp = MagicMock(); resp.status = 200
                resp.__enter__ = lambda s: s; resp.__exit__ = lambda *a: False
                return resp
        mock_opener.return_value = _FakeOpen()
        data = self._config(auth_mode="auto")
        result = doctor._check_docuseal(False, data, Path("/tmp"))
        assert result.status == "fail"
        assert "DOCUSEAL_MCP_TOKEN" in result.detail

    @patch("os.getenv")
    @patch("doctor._docuseal_opener")
    def test_pro_api_only_mode_only_needs_api_key(self, mock_opener, mock_getenv):
        mock_getenv.side_effect = lambda k, d="": {
            "DOCUSEAL_API_KEY": "valid-key",
            "DOCUSEAL_MCP_TOKEN": "",  # not required in pro_api_only
        }.get(k, d)
        class _FakeOpen:
            def open(self, req, timeout=0):
                resp = MagicMock(); resp.status = 200
                resp.__enter__ = lambda s: s; resp.__exit__ = lambda *a: False
                return resp
        mock_opener.return_value = _FakeOpen()
        data = self._config(auth_mode="pro_api_only")
        result = doctor._check_docuseal(False, data, Path("/tmp"))
        assert result.status == "pass"

    @patch("os.getenv")
    @patch("doctor._docuseal_opener")
    def test_warn_on_network_error(self, mock_opener, mock_getenv):
        mock_getenv.side_effect = lambda k, d="": {
            "DOCUSEAL_API_KEY": "valid-key",
            "DOCUSEAL_MCP_TOKEN": "valid-mcp",
        }.get(k, d)
        class _FakeOpen:
            def open(self, req, timeout=0):
                raise ConnectionError("refused")
        mock_opener.return_value = _FakeOpen()
        data = self._config()
        result = doctor._check_docuseal(False, data, Path("/tmp"))
        assert result.status == "warn"
        assert "ping failed" in result.detail
