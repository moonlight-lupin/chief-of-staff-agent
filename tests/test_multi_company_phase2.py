"""Phase 2 multi-company contract tests (docs/MULTI_COMPANY.md).

Covers: group store and approvers, group selection, read-only sharing within
a group, the group roll-up, and connector-account checks on execution.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import company_registry as reg
import config_loader
import state_db
from test_multi_company import _write_company

GROUP_TMPL = """\
company:
  name: "Lupin Holdings Pte Ltd"
  jurisdiction: SG
  incorporation_date: "2024-01-15"
  financial_year_end: "31 Dec"
  currency: SGD
paths:
  project_root: "{root}"
{approvals}"""


def _write_group(base: Path, slug: str = "lupin", root: Path | None = None, approvers=("Alicia",)) -> Path:
    cfg_dir = base / "groups" / slug
    cfg_dir.mkdir(parents=True, exist_ok=True)
    root = root or (base / "data" / f"group-{slug}")
    approvals = ""
    if approvers:
        approvals = "approvals:\n  approvers:\n" + "".join(f'    - "{a}"\n' for a in approvers)
    path = cfg_dir / "group.yaml"
    path.write_text(GROUP_TMPL.format(root=root, approvals=approvals))
    return path


def _registry_text(share: dict[str, str] | None = None, group_config: bool = True, solo: bool = True) -> str:
    lines = ["groups:", "  lupin:", '    name: "Lupin Holdings"']
    if group_config:
        lines.append('    config: "groups/lupin/group.yaml"')
    if share:
        lines.append("    share:")
        lines += [f"      {domain}: {mode}" for domain, mode in share.items()]
    lines += [
        "companies:",
        "  acme:", '    config: "companies/acme/company.yaml"', '    group: "lupin"',
        "  beta:", '    config: "companies/beta/company.yaml"', '    group: "lupin"',
    ]
    if solo:
        lines += ["  solo:", '    config: "companies/solo/company.yaml"']
    return "\n".join(lines) + "\n"


@pytest.fixture
def group_setup(tmp_path, monkeypatch):
    for slug in ("acme", "beta", "solo"):
        _write_company(tmp_path, slug)
    _write_group(tmp_path)
    registry = tmp_path / "companies.yaml"
    registry.write_text(_registry_text(share={"pipeline": "read", "todos": "read"}))
    for var in ("CHIEF_OF_STAFF_CONFIG", "CHIEF_OF_STAFF_COMPANY", "CHIEF_OF_STAFF_GROUP"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CHIEF_OF_STAFF_REGISTRY", str(registry))
    return tmp_path


def _scope(monkeypatch, company: str | None = None, group: str | None = None):
    monkeypatch.delenv("CHIEF_OF_STAFF_COMPANY", raising=False)
    monkeypatch.delenv("CHIEF_OF_STAFF_GROUP", raising=False)
    if company:
        monkeypatch.setenv("CHIEF_OF_STAFF_COMPANY", company)
    if group:
        monkeypatch.setenv("CHIEF_OF_STAFF_GROUP", group)
    config = config_loader.load_config()
    assert config is not None
    return config


def _seed(config, deals=(), todos=(), invoices=()):
    state_db.save_store_atomic("pipeline", {"deals": list(deals)}, config=config)
    state_db.save_store_atomic("todos", {"todos": list(todos)}, config=config)
    state_db.save_store_atomic("invoices", {"invoices": list(invoices)}, config=config)


DEAL = {"id": "deal-1", "client_name": "Client", "stage": "Lead", "status": "active",
        "value": 1000, "currency": "SGD", "created": "2026-11-01", "last_activity": "2026-11-01"}


# ---------------------------------------------------------------- registry


def test_group_config_share_and_approvers_load(group_setup):
    registry = reg.load_registry()
    group = registry.groups["lupin"]
    assert group.config_path == (group_setup / "groups/lupin/group.yaml").resolve()
    assert group.share == {"pipeline": "read", "todos": "read"}
    assert group.members == ("acme", "beta")


def test_group_store_overlapping_a_company_is_rejected(group_setup):
    _write_group(group_setup, root=group_setup / "data" / "acme" / "group")
    with pytest.raises(reg.RegistryError, match="project_root"):
        reg.load_registry()


def test_share_must_be_read_only(group_setup):
    (group_setup / "companies.yaml").write_text(_registry_text(share={"pipeline": "write"}))
    with pytest.raises(reg.RegistryError, match="read-only"):
        reg.load_registry()


def test_share_rejects_unknown_domain(group_setup):
    (group_setup / "companies.yaml").write_text(_registry_text(share={"secrets": "read"}))
    with pytest.raises(reg.RegistryError, match="domain"):
        reg.load_registry()


def test_group_config_requires_approvers(group_setup):
    _write_group(group_setup, approvers=())
    with pytest.raises(reg.RegistryError, match="approvers"):
        reg.load_registry()


# ---------------------------------------------------------------- selection


def test_group_selection_loads_group_scope(group_setup, monkeypatch):
    config = _scope(monkeypatch, group="lupin")
    assert config["company"]["id"] == "group:lupin"
    assert config_loader.get_project_root(config) == (group_setup / "data" / "group-lupin").resolve()


def test_company_and_group_together_are_refused(group_setup, monkeypatch, capsys):
    monkeypatch.setenv("CHIEF_OF_STAFF_COMPANY", "acme")
    monkeypatch.setenv("CHIEF_OF_STAFF_GROUP", "lupin")
    assert config_loader.load_config() is None
    assert "both" in capsys.readouterr().err


def test_group_without_config_cannot_be_selected(group_setup, monkeypatch, capsys):
    (group_setup / "companies.yaml").write_text(_registry_text(group_config=False))
    monkeypatch.setenv("CHIEF_OF_STAFF_GROUP", "lupin")
    assert config_loader.load_config() is None
    assert "config" in capsys.readouterr().err


def test_group_flag_is_stripped_and_selects_group(monkeypatch):
    monkeypatch.delenv("CHIEF_OF_STAFF_GROUP", raising=False)
    assert reg.apply_company_flag(["--group", "lupin", "group-summary"]) == ["group-summary"]
    import os
    assert os.environ["CHIEF_OF_STAFF_GROUP"] == "lupin"


# ---------------------------------------------------------------- group writes


def test_group_action_needs_a_listed_approver(group_setup, monkeypatch):
    group = _scope(monkeypatch, group="lupin")
    action = state_db.create_pending_action(group, "pipeline.deal.create", "local", "group", {"x": 1})
    assert action["company_id"] == "group:lupin"

    assert state_db.approve_pending_action(group, action["id"], approver="Mallory", reason="ok") is None
    assert state_db.get_pending_action(group, action["id"])["state"] == "requested"
    audit = (config_loader.get_project_root(group) / ".audit" / "workspace.log").read_text()
    assert "approver_not_authorised" in audit

    assert state_db.approve_pending_action(group, action["id"], approver="Alicia", reason="ok")["state"] == "approved"


def test_group_action_cannot_be_claimed_by_a_member_company(group_setup, monkeypatch):
    group = _scope(monkeypatch, group="lupin")
    action = state_db.create_pending_action(group, "mail.send", "agent", "x@example.com", {"s": 1})
    state_db.approve_pending_action(group, action["id"], approver="Alicia", reason="ok")

    acme = _scope(monkeypatch, company="acme")
    acme_root = config_loader.get_project_root(acme)
    acme_root.mkdir(parents=True, exist_ok=True)
    shutil.copy(config_loader.get_project_root(group) / "state.db", acme_root / "state.db")
    assert state_db.mark_executing(acme, action["id"]) is None


def test_company_without_approver_list_accepts_any_approver(group_setup, monkeypatch):
    acme = _scope(monkeypatch, company="acme")
    action = state_db.create_pending_action(acme, "mail.send", "agent", "x@example.com", {"s": 1})
    assert state_db.approve_pending_action(acme, action["id"], approver="anyone", reason="ok")


# ---------------------------------------------------------------- shared reads


def test_member_reads_sister_company_shared_domain(group_setup, monkeypatch):
    beta = _scope(monkeypatch, company="beta")
    _seed(beta, deals=[DEAL])
    beta_db = config_loader.get_project_root(beta) / "state.db"
    before = beta_db.read_bytes()

    acme = _scope(monkeypatch, company="acme")
    data = reg.read_shared(acme, "beta", "pipeline")
    assert [d["id"] for d in data["deals"]] == ["deal-1"]
    assert beta_db.read_bytes() == before  # read-only: owner store untouched


def test_read_of_missing_store_creates_nothing(group_setup, monkeypatch):
    acme = _scope(monkeypatch, company="acme")
    assert reg.read_shared(acme, "beta", "pipeline") == {"deals": []}
    assert not (group_setup / "data" / "beta").exists()


def test_unshared_domain_is_denied(group_setup, monkeypatch):
    acme = _scope(monkeypatch, company="acme")
    with pytest.raises(reg.ShareDenied, match="invoices"):
        reg.read_shared(acme, "beta", "invoices")


def test_company_outside_the_group_is_denied(group_setup, monkeypatch):
    solo = _scope(monkeypatch, company="solo")
    with pytest.raises(reg.ShareDenied):
        reg.read_shared(solo, "acme", "pipeline")
    acme = _scope(monkeypatch, company="acme")
    with pytest.raises(reg.ShareDenied):
        reg.read_shared(acme, "solo", "pipeline")


def test_shared_reads_are_audited_in_the_reader_log(group_setup, monkeypatch):
    acme = _scope(monkeypatch, company="acme")
    reg.read_shared(acme, "beta", "todos")
    with pytest.raises(reg.ShareDenied):
        reg.read_shared(acme, "beta", "invoices")
    audit = (config_loader.get_project_root(acme) / ".audit" / "workspace.log").read_text()
    assert "shared.read" in audit and "denied" in audit


# ---------------------------------------------------------------- roll-up


def test_group_summary_rolls_up_shared_domains_only(group_setup, monkeypatch):
    acme = _scope(monkeypatch, company="acme")
    _seed(acme, deals=[DEAL, {**DEAL, "id": "deal-2", "value": 500}],
          todos=[{"id": "t1", "title": "x", "status": "open", "due": "2000-01-01"}],
          invoices=[{"id": "INV-1", "direction": "sent", "counterparty": "Client", "amount": 10,
                     "currency": "SGD", "issue_date": "2026-11-01", "due_date": "2026-11-30",
                     "status": "sent"}])
    group = _scope(monkeypatch, group="lupin")

    summary = reg.group_summary(group)
    assert summary["group"] == "lupin"
    assert summary["members"]["acme"]["pipeline"] == {"active_deals": 2, "value_by_currency": {"SGD": 1500}}
    assert summary["members"]["acme"]["todos"] == {"open": 1, "overdue": 1}
    assert summary["members"]["acme"]["invoices"] == "not shared"
    assert summary["members"]["beta"]["pipeline"] == {"active_deals": 0, "value_by_currency": {}}


def test_group_summary_needs_group_scope(group_setup, monkeypatch):
    acme = _scope(monkeypatch, company="acme")
    with pytest.raises(reg.ShareDenied):
        reg.group_summary(acme)


def test_group_summary_cli(group_setup, monkeypatch, capsys):
    import chief_of_staff

    monkeypatch.delenv("CHIEF_OF_STAFF_GROUP", raising=False)
    assert chief_of_staff.main(["--group", "lupin", "group-summary"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out["members"]) == {"acme", "beta"}


# ---------------------------------------------------------------- connector accounts


@pytest.fixture
def acme_with_accounts(group_setup, monkeypatch):
    cfg = group_setup / "companies" / "acme" / "company.yaml"
    cfg.write_text(cfg.read_text() + 'accounts:\n  mail: "ops@acme.example"\n')
    return _scope(monkeypatch, company="acme")


def _claimed_mail_action(config):
    action = state_db.create_pending_action(config, "mail.send", "agent", "x@example.com", {"s": 1})
    state_db.approve_pending_action(config, action["id"], approver="op", reason="ok")
    return action["id"]


def test_claim_tells_the_agent_which_account_to_use(acme_with_accounts, capsys):
    import review_queue

    action_id = _claimed_mail_action(acme_with_accounts)
    assert review_queue._main(["claim", "--action-id", action_id]) == 0
    assert json.loads(capsys.readouterr().out)["expected_account"] == "ops@acme.example"


@pytest.mark.parametrize("given, expected_check", [
    ("ops@acme.example", "match"),
    ("OPS@acme.example", "match"),
    ("ops@beta.example", "mismatch"),
    (None, "unverified"),
])
def test_record_execution_checks_the_executing_account(acme_with_accounts, capsys, given, expected_check):
    import review_queue

    action_id = _claimed_mail_action(acme_with_accounts)
    assert review_queue._main(["claim", "--action-id", action_id]) == 0
    argv = ["record-execution", "--action-id", action_id, "--status", "success"]
    if given:
        argv += ["--executor-account", given]
    assert review_queue._main(argv) == 0
    action = state_db.get_pending_action(acme_with_accounts, action_id)
    assert action["state"] == "executed"  # it already happened: record it either way
    assert action["result"]["account_check"] == expected_check
    if expected_check == "mismatch":
        audit = (config_loader.get_project_root(acme_with_accounts) / ".audit" / "workspace.log").read_text()
        assert "account_mismatch" in audit


def test_doctor_warns_on_account_mismatch(acme_with_accounts, monkeypatch):
    import doctor_base
    import review_queue

    action_id = _claimed_mail_action(acme_with_accounts)
    review_queue._main(["claim", "--action-id", action_id])
    review_queue._main(["record-execution", "--action-id", action_id, "--status", "success",
                        "--executor-account", "ops@beta.example"])
    results = {r.name: r for r in doctor_base.run_checks()}
    assert results["executor_accounts"].status == "warn"
    assert action_id in results["executor_accounts"].detail
    assert results["company_registry"].status == "pass"


def test_doctor_fails_on_ambiguous_registry(group_setup):
    import doctor_base

    results = {r.name: r for r in doctor_base.run_checks()}
    assert results["company_registry"].status == "fail"
    assert "--company" in results["company_registry"].detail
