"""Phase 1 multi-company contract tests (docs/MULTI_COMPANY.md).

Covers: registry loading, active-company resolution, disjoint-store
validation, company binding on pending actions, and capabilities reporting.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

import company_registry as reg
import config_loader
import state_db


COMPANY_TMPL = """\
company:
  name: "{name}"
  jurisdiction: SG
  incorporation_date: "2024-01-15"
  financial_year_end: "31 Dec"
  currency: SGD
{extra}
paths:
  project_root: "{root}"
"""


def _write_company(base: Path, slug: str, root: Path | None = None, declared_id: str | None = None) -> Path:
    cfg_dir = base / "companies" / slug
    cfg_dir.mkdir(parents=True, exist_ok=True)
    root = root or (base / "data" / slug)
    extra = f'  id: "{declared_id}"\n' if declared_id else ""
    path = cfg_dir / "company.yaml"
    path.write_text(COMPANY_TMPL.format(name=f"{slug.title()} Pte Ltd", extra=extra, root=root))
    return path


def _write_registry(base: Path, companies: dict[str, dict], groups: dict[str, dict] | None = None) -> Path:
    lines: list[str] = []
    if groups:
        lines.append("groups:")
        for slug, spec in groups.items():
            lines.append(f"  {slug}:")
            lines.append(f'    name: "{spec.get("name", slug)}"')
    lines.append("companies:")
    for slug, spec in companies.items():
        lines.append(f"  {slug}:")
        lines.append(f'    config: "{spec["config"]}"')
        if spec.get("group"):
            lines.append(f'    group: "{spec["group"]}"')
    path = base / "companies.yaml"
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def two_companies(tmp_path, monkeypatch):
    """A registry with acme + beta in group lupin, and no selection made."""
    _write_company(tmp_path, "acme")
    _write_company(tmp_path, "beta")
    registry = _write_registry(
        tmp_path,
        {
            "acme": {"config": "companies/acme/company.yaml", "group": "lupin"},
            "beta": {"config": "companies/beta/company.yaml", "group": "lupin"},
        },
        groups={"lupin": {"name": "Lupin Holdings"}},
    )
    monkeypatch.delenv("CHIEF_OF_STAFF_CONFIG", raising=False)
    monkeypatch.delenv("CHIEF_OF_STAFF_COMPANY", raising=False)
    monkeypatch.setenv("CHIEF_OF_STAFF_REGISTRY", str(registry))
    return tmp_path


# ---------------------------------------------------------------- registry


def test_no_registry_means_legacy_single_company_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEF_OF_STAFF_REGISTRY", str(tmp_path / "missing.yaml"))
    assert reg.load_registry() is None


def test_registry_loads_companies_and_groups(two_companies):
    registry = reg.load_registry()
    assert set(registry.companies) == {"acme", "beta"}
    assert registry.companies["acme"].group == "lupin"
    assert registry.companies["acme"].config_path == (two_companies / "companies/acme/company.yaml").resolve()
    assert registry.groups["lupin"].name == "Lupin Holdings"


def test_single_registered_company_is_selected_automatically(tmp_path, monkeypatch):
    _write_company(tmp_path, "solo")
    registry = _write_registry(tmp_path, {"solo": {"config": "companies/solo/company.yaml"}})
    monkeypatch.delenv("CHIEF_OF_STAFF_COMPANY", raising=False)
    assert reg.resolve_active_company(reg.load_registry(registry)).slug == "solo"


def test_multiple_companies_without_selection_is_refused(two_companies):
    with pytest.raises(reg.AmbiguousCompanyError) as exc:
        reg.resolve_active_company(reg.load_registry())
    assert "acme" in str(exc.value) and "beta" in str(exc.value)
    assert "--company" in str(exc.value)


def test_env_selects_company_and_explicit_beats_env(two_companies, monkeypatch):
    registry = reg.load_registry()
    monkeypatch.setenv("CHIEF_OF_STAFF_COMPANY", "beta")
    assert reg.resolve_active_company(registry).slug == "beta"
    assert reg.resolve_active_company(registry, "acme").slug == "acme"


def test_unknown_company_is_refused(two_companies):
    with pytest.raises(reg.UnknownCompanyError):
        reg.resolve_active_company(reg.load_registry(), "gamma")


def test_unknown_group_is_rejected(tmp_path):
    _write_company(tmp_path, "acme")
    path = _write_registry(tmp_path, {"acme": {"config": "companies/acme/company.yaml", "group": "nope"}})
    with pytest.raises(reg.RegistryError, match="group"):
        reg.load_registry(path)


def test_shared_project_root_is_rejected(tmp_path):
    shared = tmp_path / "data" / "shared"
    _write_company(tmp_path, "acme", root=shared)
    _write_company(tmp_path, "beta", root=shared)
    path = _write_registry(
        tmp_path,
        {"acme": {"config": "companies/acme/company.yaml"}, "beta": {"config": "companies/beta/company.yaml"}},
    )
    with pytest.raises(reg.RegistryError, match="project_root"):
        reg.load_registry(path)


def test_nested_project_root_is_rejected(tmp_path):
    _write_company(tmp_path, "acme", root=tmp_path / "data" / "acme")
    _write_company(tmp_path, "beta", root=tmp_path / "data" / "acme" / "beta")
    path = _write_registry(
        tmp_path,
        {"acme": {"config": "companies/acme/company.yaml"}, "beta": {"config": "companies/beta/company.yaml"}},
    )
    with pytest.raises(reg.RegistryError, match="project_root"):
        reg.load_registry(path)


def test_declared_company_id_must_match_registry_slug(tmp_path):
    _write_company(tmp_path, "acme", declared_id="other")
    path = _write_registry(tmp_path, {"acme": {"config": "companies/acme/company.yaml"}})
    with pytest.raises(reg.RegistryError, match="id"):
        reg.load_registry(path)


# ---------------------------------------------------------------- load_config


def test_load_config_resolves_selected_company(two_companies, monkeypatch):
    monkeypatch.setenv("CHIEF_OF_STAFF_COMPANY", "beta")
    config = config_loader.load_config()
    assert config is not None
    assert config["company"]["id"] == "beta"
    assert config_loader.get_project_root(config) == (two_companies / "data" / "beta").resolve()


def test_load_config_refuses_ambiguous_registry(two_companies, capsys):
    assert config_loader.load_config() is None
    assert "--company" in capsys.readouterr().err


def test_company_selection_beats_ambient_config_env(two_companies, monkeypatch, tmp_path):
    stray = _write_company(tmp_path / "elsewhere", "stray")
    monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(stray))
    monkeypatch.setenv("CHIEF_OF_STAFF_COMPANY", "acme")
    config = config_loader.load_config()
    assert config["company"]["id"] == "acme"


def test_explicit_config_path_still_wins(two_companies, monkeypatch, tmp_path):
    stray = _write_company(tmp_path / "elsewhere", "stray")
    monkeypatch.setenv("CHIEF_OF_STAFF_COMPANY", "acme")
    config = config_loader.load_config(stray)
    assert config["company"]["name"] == "Stray Pte Ltd"


# ---------------------------------------------------------------- action binding


def _config_for(slug: str, monkeypatch):
    monkeypatch.setenv("CHIEF_OF_STAFF_COMPANY", slug)
    config = config_loader.load_config()
    assert config is not None
    return config


def test_pending_action_records_company_id(two_companies, monkeypatch):
    config = _config_for("acme", monkeypatch)
    action = state_db.create_pending_action(config, "mail.send", "agent", "x@example.com", {"subject": "hi"})
    assert action["company_id"] == "acme"
    assert state_db.get_pending_action(config, action["id"])["company_id"] == "acme"


def test_claim_from_another_company_is_refused_and_audited(two_companies, monkeypatch):
    acme = _config_for("acme", monkeypatch)
    action = state_db.create_pending_action(acme, "mail.send", "agent", "x@example.com", {"subject": "hi"})
    assert state_db.approve_pending_action(acme, action["id"], approver="op", reason="ok")

    # Simulate a store leaking across the boundary: beta's state.db is a copy of acme's.
    beta = _config_for("beta", monkeypatch)
    beta_root = config_loader.get_project_root(beta)
    beta_root.mkdir(parents=True, exist_ok=True)
    shutil.copy(config_loader.get_project_root(acme) / "state.db", beta_root / "state.db")

    assert state_db.mark_executing(beta, action["id"]) is None
    assert state_db.get_pending_action(beta, action["id"])["state"] == "approved"
    audit_text = "".join(p.read_text() for p in (beta_root / ".audit").rglob("*") if p.is_file())
    assert "company_mismatch" in audit_text

    # The owning company can still claim it.
    assert state_db.mark_executing(acme, action["id"])["state"] == "executing"


def test_company_id_is_part_of_the_approval_fingerprint():
    base = {"type": "mail.send", "target": "x@example.com", "payload": {"a": 1}}
    assert state_db.approval_hash(base) == state_db.approval_hash({**base, "company_id": None})
    assert state_db.approval_hash({**base, "company_id": "acme"}) != state_db.approval_hash(base)
    assert state_db.approval_hash({**base, "company_id": "acme"}) != state_db.approval_hash({**base, "company_id": "beta"})


def test_rewriting_company_id_after_approval_breaks_integrity(two_companies, monkeypatch):
    acme = _config_for("acme", monkeypatch)
    action = state_db.create_pending_action(acme, "mail.send", "agent", "x@example.com", {"subject": "hi"})
    state_db.approve_pending_action(acme, action["id"], approver="op", reason="ok")
    db_path = config_loader.get_project_root(acme) / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE pending_actions SET company_id = NULL WHERE id = ?", (action["id"],))
    assert state_db.mark_executing(acme, action["id"]) is None


def test_legacy_install_without_company_id_is_unchanged(sample_company_yaml, tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEF_OF_STAFF_REGISTRY", str(tmp_path / "missing.yaml"))
    config = config_loader.load_config(sample_company_yaml)
    config["paths"]["project_root"] = str(tmp_path / "legacy")
    action = state_db.create_pending_action(config, "mail.send", "agent", "x@example.com", {"s": 1})
    assert action.get("company_id") is None
    state_db.approve_pending_action(config, action["id"], approver="op", reason="ok")
    assert state_db.mark_executing(config, action["id"])["state"] == "executing"


# ---------------------------------------------------------------- capabilities


def test_capabilities_reports_active_company(two_companies, monkeypatch):
    import capability_report

    config = _config_for("beta", monkeypatch)
    report = capability_report.build_capability_report(config)
    assert report["company"] == {"id": "beta", "group": "lupin", "registered_companies": 2}
