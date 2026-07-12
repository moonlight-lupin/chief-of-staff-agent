#!/usr/bin/env python3
"""v0.3.6 — bootstrap onboarding polish + assistant-name feature.

Covers the fresh-operator audit fixes and the assistant-naming feature:
  * Audit #2 — identity is DERIVED from --company / --operator / --operator-name
    instead of shipping the canned Acme fixture; freemail websites are skipped
    with a placeholder + notice; every remaining placeholder is announced.
  * Audit #3 — the "Next steps" credential line is gated on the selected
    workspace provider (an m365 run shows no Google service-account line, and a
    google run shows no m365 line).
  * Feature — --assistant-name writes ``assistant.name`` into company.yaml,
    defaults to "Chief of Staff", and the bootstrap output explains addressing
    the assistant by name. The company_context_primer hook prefixes its context
    strip with the configured name.

CONFIG_DIR is pointed at a tmp copy of the real example and run_checks is stubbed
so nothing touches the real repo config or runs doctor.
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
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import bootstrap  # noqa: E402

REAL_EXAMPLE = PLUGIN_ROOT / "shared" / "config" / "company.yaml.example"


def _make_args(**overrides):
    """Fully-populated args Namespace mirroring bootstrap's parser defaults,
    including the new --operator-name / --assistant-name flags."""
    base = dict(
        company=None, jurisdiction=None, operator=None, operator_name=None,
        assistant_name="Chief of Staff", project_root=None,
        business_type=None, config=None, json=False,
        workspace_provider=None, m365_auth="client_credentials",
        tenant_id=None, client_id=None, user_principal=None,
        m365_secret_env="M365_CLIENT_SECRET", composio_user_id=None,
        esign_url=None, allow_insecure_esign_url=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _load(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


@pytest.fixture
def tmp_config_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    shutil.copy2(REAL_EXAMPLE, cfg / "company.yaml.example")
    monkeypatch.setattr(bootstrap, "CONFIG_DIR", cfg)
    monkeypatch.setattr(bootstrap, "run_checks", lambda *a, **k: [])
    return cfg


# ── Slug derivation ─────────────────────────────────────────────────────────

class TestSlugify:
    @pytest.mark.parametrize("name,expected", [
        ("Acme Advisory Pte Ltd", "acme-advisory-pte-ltd"),
        ("Acme, Inc.", "acme-inc"),
        ("ACME  STUDIO", "acme-studio"),          # case + repeated spaces
        ("  Spaced  Out  ", "spaced-out"),
        ("Foo & Bar!", "foo-bar"),                # punctuation stripped
        ("já-vu Co", "já-vu-co"),                 # alnum keeps unicode letters
    ])
    def test_slug(self, name, expected):
        assert bootstrap._slugify_company(name) == expected

    def test_name_from_email(self):
        assert bootstrap._name_from_email("alicia@acme.com") == "Alicia"
        assert bootstrap._name_from_email("mary.jane@x.com") == "Mary Jane"
        assert bootstrap._name_from_email("a_b-c@x.com") == "A B C"


# ── Identity overlay (audit #2) ─────────────────────────────────────────────

class TestIdentityOverlay:
    def test_operator_derives_user_block_and_website(self):
        overlay, notices = bootstrap._identity_overlay(_make_args(
            company="Acme Advisory Pte Ltd",
            operator="alicia@acme-advisory.example"))
        assert overlay["user"]["email"] == "alicia@acme-advisory.example"
        assert overlay["user"]["name"] == "Alicia"          # derived from local-part
        assert overlay["company"]["website"] == "https://acme-advisory.example"
        # project_root derives from the company slug; no Acme sample path.
        assert overlay["paths"]["project_root"].endswith("projects/acme-advisory-pte-ltd")
        assert overlay["paths"]["wiki_path"].endswith("acme-advisory-pte-ltd/wiki")
        assert notices == []                                # nothing left a placeholder

    def test_operator_name_flag_overrides_derivation(self):
        overlay, _ = bootstrap._identity_overlay(_make_args(
            operator="a@acme.com", operator_name="Dr. A. Tan"))
        assert overlay["user"]["name"] == "Dr. A. Tan"

    def test_freemail_website_skipped_with_notice(self):
        overlay, notices = bootstrap._identity_overlay(_make_args(
            operator="founder@gmail.com"))
        assert overlay["company"]["website"] == bootstrap.WEBSITE_PLACEHOLDER
        # user email still honoured — only the website is a placeholder.
        assert overlay["user"]["email"] == "founder@gmail.com"
        joined = " ".join(notices)
        assert "company.website" in joined
        assert "placeholder — edit company.yaml" in joined
        assert "gmail.com" in joined

    def test_no_operator_announces_placeholders(self):
        overlay, notices = bootstrap._identity_overlay(_make_args())
        assert overlay["user"]["name"] == bootstrap.USER_NAME_PLACEHOLDER
        assert overlay["user"]["email"] == bootstrap.USER_EMAIL_PLACEHOLDER
        assert overlay["company"]["website"] == bootstrap.WEBSITE_PLACEHOLDER
        joined = " ".join(notices)
        assert "user.name/user.email" in joined
        assert "company.website" in joined
        assert joined.count("placeholder — edit company.yaml") == 2

    def test_no_company_uses_generic_root_not_acme(self):
        overlay, _ = bootstrap._identity_overlay(_make_args())
        root = overlay["paths"]["project_root"]
        assert "acme-advisory" not in root
        assert root.endswith("projects/chief-of-staff")

    def test_explicit_project_root_wins(self, tmp_path):
        overlay, _ = bootstrap._identity_overlay(_make_args(
            company="Acme", project_root=str(tmp_path / "proj")))
        assert overlay["paths"]["project_root"] == str(tmp_path / "proj")


# ── End-to-end write-through: canned Acme fixture no longer leaks ────────────

class TestIdentityWriteThrough:
    def test_bootstrap_overwrites_canned_fixture(self, tmp_config_dir, tmp_path):
        args = _make_args(
            company="Beta Ventures LLP",
            operator="sam@beta-ventures.example",
            project_root=str(tmp_path / "proj"))
        result = bootstrap.bootstrap(args)
        data = _load(result["config"])
        # Sample values from company.yaml.example must be gone.
        assert data["user"]["name"] == "Sam"
        assert data["user"]["email"] == "sam@beta-ventures.example"
        assert data["user"]["name"] != "Alicia Tan"
        assert data["company"]["website"] == "https://beta-ventures.example"
        assert "acme-advisory" not in data["paths"]["project_root"]
        assert "acme-advisory" not in data["paths"]["wiki_path"]

    def test_placeholder_notices_returned_when_no_operator(self, tmp_config_dir, tmp_path):
        result = bootstrap.bootstrap(_make_args(project_root=str(tmp_path / "proj")))
        joined = " ".join(result["identity_notices"])
        assert "placeholder — edit company.yaml" in joined


# ── Provider-gated Next steps (audit #3) ────────────────────────────────────

class TestNextStepsGating:
    def test_google_has_no_m365_line(self):
        steps = " ".join(bootstrap._next_steps("google_api"))
        assert "Google service account" in steps
        assert "m365" not in steps.lower()
        assert "Entra" not in steps

    def test_m365_has_no_google_service_account_line(self):
        steps = " ".join(bootstrap._next_steps("m365"))
        assert "Google service account" not in steps
        assert "Entra" in steps

    def test_composio_line(self):
        steps = " ".join(bootstrap._next_steps("composio"))
        assert "Google service account" not in steps
        assert "composio" in steps.lower()

    def test_m365_main_output_has_no_google_service_account(self, tmp_config_dir, tmp_path, capsys):
        rc = bootstrap._main([
            "--workspace-provider", "m365",
            "--tenant-id", "t", "--client-id", "c",
            "--user-principal", "cos@acme.com",
            "--project-root", str(tmp_path / "proj"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Google service account" not in out

    def test_google_main_output_has_no_m365_lines(self, tmp_config_dir, tmp_path, capsys):
        rc = bootstrap._main(["--project-root", str(tmp_path / "proj")])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Google service account" in out
        assert "M365_CLIENT_SECRET" not in out
        assert "Entra" not in out


# ── Assistant name feature ──────────────────────────────────────────────────

class TestAssistantName:
    def test_default_written_to_config(self, tmp_config_dir, tmp_path):
        result = bootstrap.bootstrap(_make_args(project_root=str(tmp_path / "proj")))
        data = _load(result["config"])
        assert data["assistant"]["name"] == "Chief of Staff"
        assert result["assistant_name"] == "Chief of Staff"

    def test_custom_name_written_through(self, tmp_config_dir, tmp_path):
        args = _make_args(assistant_name="Ada", project_root=str(tmp_path / "proj"))
        result = bootstrap.bootstrap(args)
        data = _load(result["config"])
        assert data["assistant"]["name"] == "Ada"

    def test_main_output_explains_naming(self, tmp_config_dir, tmp_path, capsys):
        rc = bootstrap._main([
            "--assistant-name", "Ada",
            "--project-root", str(tmp_path / "proj"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "named 'Ada'" in out
        assert "Ask Ada to check my email" in out

    def test_main_default_name_in_output(self, tmp_config_dir, tmp_path, capsys):
        rc = bootstrap._main(["--project-root", str(tmp_path / "proj")])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Chief of Staff" in out


# ── Hook: company_context_primer prefix ─────────────────────────────────────

class TestPrimerPrefix:
    def _write_config(self, tmp_path, assistant=True):
        cfg = tmp_path / "company.yaml"
        block = 'assistant:\n  name: "Ada"\n' if assistant else ""
        cfg.write_text(
            "company:\n  name: \"Test Co Pte Ltd\"\n  jurisdiction: SG\n"
            f"paths:\n  project_root: \"{tmp_path}\"\n" + block,
            encoding="utf-8",
        )
        return cfg

    def test_prefix_present_when_named(self, tmp_path, monkeypatch):
        from hooks import company_context_primer
        cfg = self._write_config(tmp_path, assistant=True)
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(cfg))
        result = company_context_primer({"loaded_skills": ["daily-briefing"]})
        assert result is not None
        assert result.startswith("You are Ada, the operator's Chief of Staff.")
        assert "[CoS Context]" in result
        assert "Test Co Pte Ltd" in result

    def test_no_prefix_without_name(self, tmp_path, monkeypatch):
        from hooks import company_context_primer
        cfg = self._write_config(tmp_path, assistant=False)
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(cfg))
        result = company_context_primer({"loaded_skills": ["daily-briefing"]})
        assert result is not None
        assert result.startswith("[CoS Context]")
        assert "You are" not in result
