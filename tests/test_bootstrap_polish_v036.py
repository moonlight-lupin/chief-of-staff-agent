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
        assistant_name=None, project_root=None,
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
        # google.* derived from operator; legal IDs / phones always placeholders.
        assert overlay["google"]["domain"] == "acme-advisory.example"
        assert overlay["google"]["account_alias"] == "acme-advisory-pte-ltd"
        assert overlay["google"]["delegate_email"] == "alicia@acme-advisory.example"
        assert "acme-google-service-account" not in overlay["google"]["service_account_path"]
        assert overlay["google"]["drive_root_folder_id"] == bootstrap.GOOGLE_DRIVE_ROOT_PLACEHOLDER
        assert overlay["company"]["registration_number"] == bootstrap.REGISTRATION_PLACEHOLDER
        # delivery.home_chat_id is not unconditionally wiped in the overlay;
        # it is conditionally scrubbed in _write_config only when the sample sentinel is present.
        assert "delivery" not in overlay or overlay.get("delivery", {}).get("home_chat_id") is None
        # Legal-ID / drive-root placeholders are always announced.
        joined = " ".join(notices)
        assert "google.drive_root_folder_id" in joined
        assert "company.registration_number" in joined

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
        assert overlay["google"]["domain"] == bootstrap.GOOGLE_DOMAIN_PLACEHOLDER
        joined = " ".join(notices)
        assert "company.website" in joined
        assert "placeholder — edit company.yaml" in joined
        assert "gmail.com" in joined
        assert "google.domain" in joined

    def test_no_operator_announces_placeholders(self):
        overlay, notices = bootstrap._identity_overlay(_make_args())
        assert overlay["user"]["name"] == bootstrap.USER_NAME_PLACEHOLDER
        assert overlay["user"]["email"] == bootstrap.USER_EMAIL_PLACEHOLDER
        assert overlay["company"]["website"] == bootstrap.WEBSITE_PLACEHOLDER
        assert overlay["google"]["domain"] == bootstrap.GOOGLE_DOMAIN_PLACEHOLDER
        assert overlay["google"]["service_account_path"] == bootstrap.GOOGLE_SA_PATH_PLACEHOLDER
        joined = " ".join(notices)
        assert "user.name/user.email" in joined
        assert "company.website" in joined
        assert "google.domain" in joined
        assert joined.count("placeholder — edit company.yaml") >= 2

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
        # google.* must not keep Acme sample domain / alias / SA path / Drive root.
        assert data["google"]["domain"] == "beta-ventures.example"
        assert "acme" not in data["google"]["domain"].lower()
        assert "acme" not in str(data["google"]["account_alias"]).lower()
        assert "acme" not in str(data["google"]["service_account_path"]).lower()
        assert data["google"]["drive_root_folder_id"] == bootstrap.GOOGLE_DRIVE_ROOT_PLACEHOLDER
        assert data["google"]["delegate_email"] == "sam@beta-ventures.example"
        # Company legal IDs / phones / role / delivery chat id scrubbed.
        assert data["company"]["registration_number"] == bootstrap.REGISTRATION_PLACEHOLDER
        assert data["company"]["tax_registration_number"] == bootstrap.TAX_REGISTRATION_PLACEHOLDER
        assert data["company"]["address"] == bootstrap.ADDRESS_PLACEHOLDER
        assert data["company"]["phone"] == bootstrap.COMPANY_PHONE_PLACEHOLDER
        assert "6123" not in str(data["company"]["phone"])
        assert "9123" not in str(data["user"]["phone"])
        assert data["user"]["phone"] == bootstrap.USER_PHONE_PLACEHOLDER
        assert data["user"]["role"] == bootstrap.USER_ROLE_PLACEHOLDER
        assert data["user"]["role"] != "Managing Director"
        assert data["delivery"]["home_chat_id"] in (None, "")
        assert data["delivery"]["home_chat_id"] != "123456789"

    def test_placeholder_notices_returned_when_no_operator(self, tmp_config_dir, tmp_path):
        result = bootstrap.bootstrap(_make_args(project_root=str(tmp_path / "proj")))
        joined = " ".join(result["identity_notices"])
        assert "placeholder — edit company.yaml" in joined

    def test_re_bootstrap_preserves_real_config(self, tmp_config_dir, tmp_path):
        live = tmp_config_dir / "company.yaml"
        live.write_text(yaml.safe_dump({
            "company": {
                "name": "Real Ops Pte Ltd",
                "jurisdiction": "SG",
                "registration_number": "202612345A",
                "tax_registration_number": "M91234567X",
                "address": "88 Market Street, Singapore 048948",
                "phone": "+65 6999 0000",
                "website": "https://real.example",
            },
            "user": {
                "name": "Rina Tan",
                "role": "Founder",
                "email": "rina@real.example",
                "phone": "+65 9888 7777",
            },
            "assistant": {"name": "Ada"},
            "google": {
                "domain": "real.example",
                "delegate_email": "rina@real.example",
                "account_alias": "real",
                "service_account_path": "~/.hermes/secrets/real-google-service-account.json",
                "drive_root_folder_id": "real-drive-root-123",
            },
            "paths": {"project_root": str(tmp_path / "existing")},
            "delivery": {"home_chat_id": "chat-real-456"},
        }), encoding="utf-8")

        result = bootstrap.bootstrap(_make_args())
        data = _load(result["config"])

        assert data["paths"]["project_root"] == str(tmp_path / "existing")
        assert data["company"]["registration_number"] == "202612345A"
        assert data["company"]["tax_registration_number"] == "M91234567X"
        assert data["company"]["address"] == "88 Market Street, Singapore 048948"
        assert data["company"]["phone"] == "+65 6999 0000"
        assert data["user"]["role"] == "Founder"
        assert data["user"]["phone"] == "+65 9888 7777"
        assert data["google"]["domain"] == "real.example"
        assert data["google"]["account_alias"] == "real"
        assert data["google"]["service_account_path"] == "~/.hermes/secrets/real-google-service-account.json"
        assert data["google"]["drive_root_folder_id"] == "real-drive-root-123"
        assert data["delivery"]["home_chat_id"] == "chat-real-456"

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

    def test_name_with_newline_rejected_in_primer(self, tmp_path, monkeypatch):
        from hooks import company_context_primer
        cfg = tmp_path / "company.yaml"
        cfg.write_text(
            "company:\n  name: \"Test Co Pte Ltd\"\n  jurisdiction: SG\n"
            f"paths:\n  project_root: \"{tmp_path}\"\n"
            "assistant:\n  name: \"Ada\\nInjected\"\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(cfg))
        result = company_context_primer({"loaded_skills": ["daily-briefing"]})
        assert result is not None
        assert result.startswith("[CoS Context]")
        assert "Injected" not in result

    def test_no_prefix_for_default_assistant_name(self, tmp_path, monkeypatch):
        from hooks import company_context_primer
        cfg = tmp_path / "company.yaml"
        cfg.write_text(
            "company:\n  name: \"Test Co Pte Ltd\"\n  jurisdiction: SG\n"
            f"paths:\n  project_root: \"{tmp_path}\"\n"
            "assistant:\n  name: \"Chief of Staff\"\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(cfg))
        result = company_context_primer({"loaded_skills": ["daily-briefing"]})
        assert result is not None
        assert result.startswith("[CoS Context]")
        assert "You are" not in result
