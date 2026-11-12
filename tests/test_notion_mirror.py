"""Phase 3 contract tests: Notion mirror (docs/MULTI_COMPANY.md, D1).

The plugin's store is the source of truth. Notion is a mirror:
- store changes are pushed as queued ``notion.db.upsert`` actions (agent path);
- Notion-side edits come back as queued ``notion.pull`` actions that a human
  approves before the store changes (Python path);
- edits on both sides are conflicts and produce no action.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import config_loader
import notion_mirror as nm
import state_db

CONFIG = """\
company:
  name: "Acme Pte Ltd"
  jurisdiction: SG
  incorporation_date: "2024-01-15"
  financial_year_end: "31 Dec"
  currency: SGD
paths:
  project_root: "{root}"
  wiki_path: "{root}/wiki"
sales_stages: [Lead, Proposal Sent, Contract Signed, Paid]
accounts:
  notion: acme-workspace
{notion}"""

NOTION = """\
notion:
  databases:
    pipeline: db-pipe
    todos: db-todo
  wiki_parent_page: page-wiki
"""

DEAL = {"id": "deal-001", "client_name": "Client Co", "stage": "Lead", "status": "active",
        "value": 1000, "currency": "SGD", "contact_name": "Jo", "contact_email": "jo@client.example",
        "created": "2026-11-01", "last_activity": "2026-11-01"}
TODO = {"id": "todo-001", "title": "Call Jo", "status": "open", "priority": "high",
        "due": "2026-11-20", "created": "2026-11-01"}


def _config(tmp_path: Path, notion: str = NOTION):
    path = tmp_path / "company.yaml"
    path.write_text(CONFIG.format(root=tmp_path / "data", notion=notion))
    config = config_loader.load_config(path)
    assert config is not None
    return config


@pytest.fixture
def config(tmp_path):
    cfg = _config(tmp_path)
    state_db.save_store_atomic("pipeline", {"deals": [dict(DEAL)]}, config=cfg)
    state_db.save_store_atomic("todos", {"todos": [dict(TODO)]}, config=cfg)
    return cfg


def _row(page_id: str, props: dict, edited: str = "2026-11-10T00:00:00Z") -> dict:
    return {"page_id": page_id, "last_edited_time": edited, "properties": props}


def _queue(config, domain, rows):
    plan = nm.plan_sync(config, domain, rows)
    return plan, nm.queue_plan(config, plan)


def _execute_agent(config, action_id, data):
    """Approve → claim → record success, as an agent would."""
    import review_queue

    state_db.approve_pending_action(config, action_id, approver="op", reason="ok")
    assert review_queue._main(["--config", str(config.source_path), "claim", "--action-id", action_id]) == 0
    assert review_queue._main([
        "--config", str(config.source_path), "record-execution", "--action-id", action_id,
        "--status", "success", "--result-json", json.dumps(data),
        "--executor-account", "acme-workspace",
    ]) == 0


def _push_all(config, domain, rows=()):
    _, queued = _queue(config, domain, list(rows))
    for i, action in enumerate(queued["created"]):
        if action["type"] == "notion.db.upsert":
            page = action["payload"]["page_id"] or f"page-{i}"
            _execute_agent(config, action["id"], {"page_id": page, "last_edited_time": "2026-11-10T00:00:00Z"})
    return queued


# ---------------------------------------------------------------- config


def test_notion_settings_are_read_from_config(config):
    settings = nm.notion_settings(config)
    assert settings == {"databases": {"pipeline": "db-pipe", "todos": "db-todo"}, "wiki_parent_page": "page-wiki"}


def test_planning_refuses_an_unmapped_domain(tmp_path):
    cfg = _config(tmp_path, notion="notion:\n  databases:\n    todos: db-todo\n")
    with pytest.raises(nm.NotionConfigError, match="pipeline"):
        nm.plan_sync(cfg, "pipeline", [])


def test_properties_map_store_fields(config):
    props = nm.to_properties("pipeline", DEAL)
    assert props["CoS ID"] == "deal-001"
    assert props["Name"] == "Client Co"
    assert props["Stage"] == "Lead"
    assert nm.to_properties("todos", TODO)["Due"] == "2026-11-20"


# ---------------------------------------------------------------- push


def test_new_store_record_is_pushed_as_an_upsert(config):
    plan, queued = _queue(config, "pipeline", [])
    assert [p["record_id"] for p in plan["push"]] == ["deal-001"]
    (action,) = queued["created"]
    assert action["type"] == "notion.db.upsert"
    assert action["provider"] == "agent"
    assert action["state"] == "requested"  # queued, never executed by planning
    assert action["payload"]["database_id"] == "db-pipe"
    assert action["payload"]["page_id"] is None


def test_planning_twice_does_not_duplicate_open_actions(config):
    _queue(config, "pipeline", [])
    _, queued = _queue(config, "pipeline", [])
    assert queued["created"] == []
    assert queued["skipped_open"] == ["notion.db.upsert:notion:pipeline/deal-001"]


def test_recorded_upsert_updates_the_mirror_and_then_is_in_sync(config):
    _push_all(config, "pipeline")
    mirror = nm.load_mirror(config)
    assert mirror["pipeline"]["deal-001"]["page_id"] == "page-0"

    rows = [_row("page-0", nm.to_properties("pipeline", DEAL))]
    plan = nm.plan_sync(config, "pipeline", rows)
    assert plan["in_sync"] == ["deal-001"] and not plan["push"] and not plan["pull"]


def test_create_without_page_id_is_not_mirrored(config, capsys):
    _, queued = _queue(config, "pipeline", [])
    _execute_agent(config, queued["created"][0]["id"], {})
    assert "deal-001" not in nm.load_mirror(config).get("pipeline", {})
    assert "page_id" in capsys.readouterr().err


def test_store_change_with_unchanged_notion_is_pushed_as_update(config):
    _push_all(config, "pipeline")
    notion_props = nm.to_properties("pipeline", DEAL)
    state_db.save_store_atomic("pipeline", {"deals": [{**DEAL, "value": 2000}]}, config=config)

    plan = nm.plan_sync(config, "pipeline", [_row("page-0", notion_props, edited="2026-11-10T00:00:00Z")])
    (push,) = plan["push"]
    assert push["page_id"] == "page-0"
    assert push["properties"]["Value"] == 2000
    assert push["expected_last_edited"] == "2026-11-10T00:00:00Z"


def test_store_record_removed_queues_an_archive(config):
    _push_all(config, "pipeline")
    state_db.save_store_atomic("pipeline", {"deals": []}, config=config)
    plan, queued = _queue(config, "pipeline", [_row("page-0", nm.to_properties("pipeline", DEAL))])
    assert [a["page_id"] for a in plan["archive"]] == ["page-0"]
    assert queued["created"][0]["type"] == "notion.page.archive"


def test_upsert_cannot_be_executed_on_the_python_path(config, capsys):
    import review_queue

    _, queued = _queue(config, "pipeline", [])
    action_id = queued["created"][0]["id"]
    state_db.approve_pending_action(config, action_id, approver="op", reason="ok")
    assert review_queue._main(["--config", str(config.source_path), "execute", "--action-id", action_id]) == 1
    assert state_db.get_pending_action(config, action_id)["state"] == "approved"
    assert "claim" in capsys.readouterr().err


# ---------------------------------------------------------------- pull


def test_notion_edit_becomes_a_pull_and_store_waits_for_approval(config):
    _push_all(config, "pipeline")
    edited = {**nm.to_properties("pipeline", DEAL), "Stage": "Proposal Sent"}
    plan, queued = _queue(config, "pipeline", [_row("page-0", edited, edited="2026-11-11T00:00:00Z")])

    (pull,) = plan["pull"]
    assert pull["op"] == "update" and pull["changes"] == {"stage": "Proposal Sent"}
    (action,) = queued["created"]
    assert action["type"] == "notion.pull" and action["provider"] == "local"
    assert state_db.load_store("pipeline", config)["deals"][0]["stage"] == "Lead"  # untouched


def test_approved_pull_updates_the_store_and_mirror(config):
    import review_queue

    _push_all(config, "pipeline")
    edited = {**nm.to_properties("pipeline", DEAL), "Stage": "Proposal Sent"}
    rows = [_row("page-0", edited, edited="2026-11-11T00:00:00Z")]
    _, queued = _queue(config, "pipeline", rows)
    action_id = queued["created"][0]["id"]
    state_db.approve_pending_action(config, action_id, approver="op", reason="ok")
    assert review_queue._main(["--config", str(config.source_path), "execute", "--action-id", action_id]) == 0

    deal = state_db.load_store("pipeline", config)["deals"][0]
    assert deal["stage"] == "Proposal Sent"
    assert deal["stage_history"][-1]["stage"] == "Proposal Sent"
    # The stage move also refreshed last_activity in the store; that goes back
    # to Notion as a push, never as another pull.
    plan = nm.plan_sync(config, "pipeline", rows)
    assert plan["pull"] == [] and plan["conflicts"] == []
    assert [p["properties"]["Last activity"] for p in plan["push"]] == [deal["last_activity"]]


def test_edits_to_fields_outside_the_allowlist_are_ignored_and_reported(config):
    _push_all(config, "pipeline")
    edited = {**nm.to_properties("pipeline", DEAL), "Name": "Renamed In Notion"}
    plan = nm.plan_sync(config, "pipeline", [_row("page-0", edited)])
    assert plan["pull"] == []
    assert plan["ignored"] == [{"record_id": "deal-001", "fields": ["Name"]}]


def test_invalid_stage_from_notion_is_rejected_at_planning(config):
    _push_all(config, "pipeline")
    edited = {**nm.to_properties("pipeline", DEAL), "Stage": "Maybe"}
    plan = nm.plan_sync(config, "pipeline", [_row("page-0", edited)])
    assert plan["pull"] == []
    assert plan["rejected"][0]["record_id"] == "deal-001"


def test_edits_on_both_sides_are_a_conflict_with_no_action(config):
    _push_all(config, "pipeline")
    state_db.save_store_atomic("pipeline", {"deals": [{**DEAL, "value": 2000}]}, config=config)
    edited = {**nm.to_properties("pipeline", DEAL), "Stage": "Proposal Sent"}
    plan, queued = _queue(config, "pipeline", [_row("page-0", edited)])
    assert plan["conflicts"][0]["record_id"] == "deal-001"
    assert queued["created"] == []


def test_new_notion_task_becomes_a_pull_create(config):
    import review_queue

    _push_all(config, "todos")
    new_row = _row("page-new", {"Name": "Book venue", "Status": "open", "Priority": "low", "Due": "2026-12-01"})
    rows = [_row("page-0", nm.to_properties("todos", TODO)), new_row]
    plan, queued = _queue(config, "todos", rows)
    (pull,) = plan["pull"]
    assert pull["op"] == "create" and pull["page_id"] == "page-new"

    action_id = queued["created"][0]["id"]
    state_db.approve_pending_action(config, action_id, approver="op", reason="ok")
    assert review_queue._main(["--config", str(config.source_path), "execute", "--action-id", action_id]) == 0
    todos = state_db.load_store("todos", config)["todos"]
    created = next(t for t in todos if t["title"] == "Book venue")
    assert created["source_skill"] == "notion-sync"
    assert nm.load_mirror(config)["todos"][created["id"]]["page_id"] == "page-new"

    # Next sync writes the CoS ID back to the Notion page, as a push.
    plan = nm.plan_sync(config, "todos", rows)
    assert [p["record_id"] for p in plan["push"]] == [created["id"]]


def test_pull_is_refused_if_the_store_changed_after_planning(config):
    import review_queue

    _push_all(config, "pipeline")
    edited = {**nm.to_properties("pipeline", DEAL), "Stage": "Proposal Sent"}
    _, queued = _queue(config, "pipeline", [_row("page-0", edited)])
    state_db.save_store_atomic("pipeline", {"deals": [{**DEAL, "value": 5}]}, config=config)
    action_id = queued["created"][0]["id"]
    state_db.approve_pending_action(config, action_id, approver="op", reason="ok")
    assert review_queue._main(["--config", str(config.source_path), "execute", "--action-id", action_id]) == 1
    assert state_db.load_store("pipeline", config)["deals"][0]["stage"] == "Lead"
    assert state_db.get_pending_action(config, action_id)["state"] in {"approved", "failed"}


# ---------------------------------------------------------------- wiki


def test_wiki_page_is_queued_and_republished_as_update(config):
    wiki = config_loader.get_project_root(config) / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "onboarding.md").write_text("# Onboarding\n\nStep one.\n")

    action = nm.queue_wiki_page(config, wiki / "onboarding.md")
    assert action["type"] == "notion.page.create"
    assert action["payload"]["parent_page_id"] == "page-wiki"
    assert action["payload"]["title"] == "Onboarding"
    _execute_agent(config, action["id"], {"page_id": "wiki-1"})

    assert nm.queue_wiki_page(config, wiki / "onboarding.md") is None  # unchanged: nothing to do
    (wiki / "onboarding.md").write_text("# Onboarding\n\nStep two.\n")
    update = nm.queue_wiki_page(config, wiki / "onboarding.md")
    assert update["type"] == "notion.page.update" and update["payload"]["page_id"] == "wiki-1"


def test_wiki_page_outside_the_wiki_is_refused(config, tmp_path):
    stray = tmp_path / "secret.md"
    stray.write_text("# nope")
    with pytest.raises(nm.NotionConfigError, match="wiki"):
        nm.queue_wiki_page(config, stray)


# ---------------------------------------------------------------- surfaces


def test_risk_levels():
    import action_risk

    assert action_risk.get_action_risk("notion.db.upsert") == "low"
    assert action_risk.get_action_risk("notion.page.create") == "medium"
    assert action_risk.get_action_risk("notion.pull") == "medium"
    assert action_risk.get_action_risk("notion.page.archive") == "high"


def test_claim_names_the_notion_account(config, capsys):
    import review_queue

    _, queued = _queue(config, "pipeline", [])
    action_id = queued["created"][0]["id"]
    state_db.approve_pending_action(config, action_id, approver="op", reason="ok")
    review_queue._main(["--config", str(config.source_path), "claim", "--action-id", action_id])
    assert json.loads(capsys.readouterr().out)["expected_account"] == "acme-workspace"


def test_capabilities_reports_notion(config):
    import capability_report

    report = capability_report.build_capability_report(config)
    assert report["notion"]["configured"] is True
    assert report["notion"]["domains"] == ["pipeline", "todos"]


def test_notion_skills_hidden_without_notion_config(tmp_path, monkeypatch):
    import doctor_base

    cfg = _config(tmp_path, notion="")
    monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(cfg.source_path))
    assert not {"notion-sync", "notion-wiki"} & set(doctor_base._filter_configured_skills(
        ["todo-list", "notion-sync", "notion-wiki"]))
    cfg = _config(tmp_path)
    assert {"notion-sync", "notion-wiki"} <= set(doctor_base._filter_configured_skills(
        ["todo-list", "notion-sync", "notion-wiki"]))


def test_sync_cli_plans_from_an_envelope(config, tmp_path, capsys):
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "skills" / "notion-sync" / "scripts" / "notion_sync.py"
    spec = importlib.util.spec_from_file_location("notion_sync_cli", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    envelope = tmp_path / "rows.json"
    envelope.write_text(json.dumps({"domain": "pipeline", "rows": []}))
    assert module.main(["--config", str(config.source_path), "plan", "--input", str(envelope)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["plan"]["push"][0]["record_id"] == "deal-001"
    assert out["queued"] is None  # plan alone queues nothing

    assert module.main(["--config", str(config.source_path), "plan", "--input", str(envelope), "--queue"]) == 0
    assert len(json.loads(capsys.readouterr().out)["queued"]["created"]) == 1


def test_approval_points_agent_path_actions_at_claim(config, capsys):
    import review_queue

    _queue(config, "pipeline", [])
    assert review_queue._main([
        "--config", str(config.source_path), "approve", "--all", "--risk", "low",
        "--type", "notion.db.upsert", "--approver", "op", "--reason", "sync", "--confirm-low-risk-bulk",
    ]) == 0
    out = capsys.readouterr().out
    assert "claim --action-id" in out and "execute --action-id" not in out
