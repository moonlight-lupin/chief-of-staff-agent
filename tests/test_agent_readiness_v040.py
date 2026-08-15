#!/usr/bin/env python3
"""Tests for v0.4.0 — what an agent needs to know before it acts.

Three related gaps, all about an agent (or any operator) being able to
establish its own operating envelope without reading prose:

1. ``doctor`` must fail loudly and legibly when a declared dependency is
   installed-but-broken, not only when it is absent. A distro-managed
   ``cryptography`` missing ``_cffi_backend`` raises ``pyo3_runtime.
   PanicException`` — a BaseException, not an Exception — and
   ``importlib.util.find_spec`` reports it as present.
2. ``chief_of_staff.py capabilities`` must state, in one machine-readable
   call, which provider is active, what it may do, and what is unverified.
3. Credential-holding providers must be refused in a hosted cloud session,
   where environment variables are plain text and readable by anyone using
   the environment.
"""

import json
import os
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

import doctor_base  # noqa: E402
import workspace_guardrails  # noqa: E402


class _FakePanic(BaseException):
    """Stand-in for pyo3_runtime.PanicException, which is NOT an Exception."""


# ─── 1. Dependency preflight ─────────────────────────────────────────────────

class TestDependencyPreflight:
    def test_covers_every_declared_dependency(self):
        """requirements.txt and the doctor check must not drift apart."""
        declared = set()
        for line in (PLUGIN_ROOT / "requirements.txt").read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name = line.split(">=")[0].split("==")[0].split("[")[0].strip()
            declared.add(name.lower())

        checked = {label.lower() for _mod, label in doctor_base.REQUIRED_PACKAGES}
        missing = declared - checked
        assert not missing, (
            f"requirements.txt declares packages the doctor never checks: {sorted(missing)}"
        )

    def test_passes_when_everything_imports(self):
        result = doctor_base._check_packages(False, None, Path("/nonexistent"))
        assert result.status == "pass", result.detail

    def test_fails_on_broken_import_not_just_missing(self, monkeypatch):
        """find_spec sees a broken package as present; importing it does not."""
        real_import = doctor_base.importlib.import_module

        def fake_import(name, *a, **kw):
            if name == "cryptography":
                raise ImportError("No module named '_cffi_backend'")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(doctor_base.importlib, "import_module", fake_import)
        result = doctor_base._check_packages(False, None, Path("/nonexistent"))
        assert result.status == "fail"
        assert "cryptography" in result.detail

    def test_survives_a_base_exception_from_the_import(self, monkeypatch):
        """A pyo3 panic must be caught and reported, not crash the doctor."""
        real_import = doctor_base.importlib.import_module

        def fake_import(name, *a, **kw):
            if name == "cryptography":
                raise _FakePanic("Python API call failed")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(doctor_base.importlib, "import_module", fake_import)
        result = doctor_base._check_packages(False, None, Path("/nonexistent"))
        assert result.status == "fail"
        assert "cryptography" in result.detail

    def test_failure_detail_names_a_remedy(self, monkeypatch):
        real_import = doctor_base.importlib.import_module

        def fake_import(name, *a, **kw):
            if name == "pymupdf":
                raise ImportError("boom")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(doctor_base.importlib, "import_module", fake_import)
        result = doctor_base._check_packages(False, None, Path("/nonexistent"))
        assert "pip install" in result.detail

    def test_import_chatter_does_not_reach_stdout(self, monkeypatch, capsys):
        """A package that prints on import must not corrupt `doctor --json`."""
        real_import = doctor_base.importlib.import_module

        def noisy_import(name, *a, **kw):
            print(f"warning: {name} says hello")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(doctor_base.importlib, "import_module", noisy_import)
        doctor_base._check_packages(False, None, Path("/nonexistent"))
        assert capsys.readouterr().out == ""


# ─── 2. Machine-readable capabilities ────────────────────────────────────────

@pytest.fixture
def agent_config(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    return {
        "company": {"name": "Acme", "jurisdiction": "SG"},
        "integrations": {"workspace": {"provider": "agent"}},
        "paths": {"project_root": str(project)},
    }


class TestCapabilitiesReport:
    def test_reports_provider_and_actions(self, agent_config):
        import chief_of_staff

        report = chief_of_staff.build_capability_report(agent_config)
        assert report["provider"] == "agent"
        assert isinstance(report["supported"], list)
        assert isinstance(report["unsupported"], list)

    def test_unsupported_actions_carry_reasons(self):
        """An agent must be told *why* something is refused, not just that it is."""
        import chief_of_staff

        config = {"integrations": {"workspace": {"provider": "m365"}}}
        report = chief_of_staff.build_capability_report(config)
        reasons = report["unsupported_reasons"]
        assert reasons, "m365 has known-unsupported actions but reported no reasons"
        for action, reason in reasons.items():
            assert reason.strip(), f"{action} refused with an empty reason"

    def test_flags_m365_as_never_live_verified(self):
        import chief_of_staff

        config = {"integrations": {"workspace": {"provider": "m365"}}}
        report = chief_of_staff.build_capability_report(config)
        assert report["provider_verified"] is False
        assert "verif" in report["provider_verification_note"].lower()

    def test_google_provider_is_not_flagged_unverified(self):
        import chief_of_staff

        config = {"integrations": {"workspace": {"provider": "google_api"}}}
        report = chief_of_staff.build_capability_report(config)
        assert report["provider_verified"] is True

    def test_reports_where_state_lives(self, agent_config):
        import chief_of_staff

        report = chief_of_staff.build_capability_report(agent_config)
        assert report["project_root"] == agent_config["paths"]["project_root"]

    def test_report_is_json_serialisable(self, agent_config):
        import chief_of_staff

        json.dumps(chief_of_staff.build_capability_report(agent_config))

    def test_json_is_the_default_output(self, agent_config, capsys, monkeypatch):
        """CLIs here emit JSON by default so an agent can parse without a flag."""
        import chief_of_staff

        monkeypatch.setattr(chief_of_staff, "load_config", lambda _p: agent_config)
        args = chief_of_staff.build_parser().parse_args(["capabilities"])
        assert chief_of_staff.cmd_capabilities(args) == 0
        json.loads(capsys.readouterr().out)

    def test_summary_flag_actually_selects_the_table(self, agent_config, capsys, monkeypatch):
        """--json defaulting to True once made --summary unreachable."""
        import chief_of_staff

        monkeypatch.setattr(chief_of_staff, "load_config", lambda _p: agent_config)
        args = chief_of_staff.build_parser().parse_args(["capabilities", "--summary"])
        assert chief_of_staff.cmd_capabilities(args) == 0
        out = capsys.readouterr().out
        assert out.startswith("Chief of Staff Capabilities")
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


# ─── 3. Hosted-session guardrails ────────────────────────────────────────────

@pytest.fixture
def cloud_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "cse_abc123")
    yield
    os.environ.pop("CLAUDE_CODE_REMOTE_SESSION_ID", None)


@pytest.fixture
def local_session(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_REMOTE_SESSION_ID", raising=False)


class TestHostedSessionGuardrails:
    def test_detects_a_hosted_session(self, cloud_session):
        assert workspace_guardrails.in_hosted_session() is True

    def test_local_session_is_not_hosted(self, local_session):
        assert workspace_guardrails.in_hosted_session() is False

    @pytest.mark.parametrize("provider", ["google_api", "m365", "composio", "composio_microsoft"])
    def test_credential_providers_refused_in_hosted_session(self, provider, cloud_session):
        refusal = workspace_guardrails.hosted_session_refusal(provider)
        assert refusal is not None
        assert "agent" in refusal, "the refusal must point at the safe alternative"

    def test_agent_provider_allowed_in_hosted_session(self, cloud_session):
        """The agent provider holds no credentials, so it is the safe one."""
        assert workspace_guardrails.hosted_session_refusal("agent") is None

    @pytest.mark.parametrize("provider", ["google_api", "m365", "composio", "agent"])
    def test_nothing_is_refused_locally(self, provider, local_session):
        assert workspace_guardrails.hosted_session_refusal(provider) is None

    def test_refusal_explains_the_credential_risk(self, cloud_session):
        refusal = workspace_guardrails.hosted_session_refusal("m365")
        lowered = refusal.lower()
        assert "environment variable" in lowered or "plain text" in lowered

    def test_capability_report_surfaces_the_refusal(self, cloud_session):
        import chief_of_staff

        report = chief_of_staff.build_capability_report(
            {"integrations": {"workspace": {"provider": "m365"}}}
        )
        assert report["hosted_session"] is True
        assert report["hosted_session_refusal"]

    def test_capability_report_warns_state_is_ephemeral(self, cloud_session):
        import chief_of_staff

        report = chief_of_staff.build_capability_report(
            {"integrations": {"workspace": {"provider": "agent"}}}
        )
        assert report["state_persistent"] is False
        assert "ephemeral" in report["state_note"].lower()

    def test_local_state_is_reported_persistent(self, agent_config, local_session):
        import chief_of_staff

        report = chief_of_staff.build_capability_report(agent_config)
        assert report["state_persistent"] is True
