"""Onboarding an existing Notion database (follow-up to the Notion mirror).

Covers: column mapping, the schema check that sync requires, adding missing
columns as an approved high-risk action, the guard against duplicating an
unlinked database, the one-time link step, and conflict resolution.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import notion_mirror as nm
import state_db
from test_notion_mirror import DEAL, TODO, _config, _execute_agent, _row

PIPE_SCHEMA = {"CoS ID": "rich_text", "Name": "title", "Stage": "status", "Status": "select",
               "Value": "number", "Currency": "select", "Contact": "rich_text", "Email": "email",
               "Last activity": "date"}
# An existing team database: different names, no CoS ID, no Currency/Status/Last activity.
EXISTING_SCHEMA = {"Company": "title", "Deal stage": "status", "Amount": "number",
                   "Contact name": "rich_text", "Contact email": "email", "Notes": "rich_text"}
EXISTING_COLUMNS = """\
  columns:
    pipeline:
      Name: Company
      Stage: Deal stage
      Value: Amount
      Contact: Contact name
      Email: Contact email
      Status: null
      Currency: null
      Last activity: null
"""
NOTION = """\
notion:
  databases:
    pipeline: db-pipe
    todos: db-todo
"""


def _execute_local(config, action_id):
    import review_queue

    state_db.approve_pending_action(config, action_id, approver="op", reason="ok")
    return review_queue._main(["--config", str(config.source_path), "execute", "--action-id", action_id])


@pytest.fixture
def fresh(tmp_path):
    """Plugin has two deals; Notion database uses the documented columns."""
    cfg = _config(tmp_path, notion=NOTION)
    deals = [dict(DEAL), {**DEAL, "id": "deal-002", "client_name": "Other Ltd", "contact_email": "a@other.example"}]
    state_db.save_store_atomic("pipeline", {"deals": deals}, config=cfg)
    return cfg


@pytest.fixture
def existing(tmp_path):
    """Plugin has deals; an existing Notion database uses its own column names."""
    cfg = _config(tmp_path, notion=NOTION + EXISTING_COLUMNS)
    deals = [dict(DEAL), {**DEAL, "id": "deal-002", "client_name": "Other Ltd", "contact_email": "a@other.example"},
             {**DEAL, "id": "deal-003", "client_name": "Store Only Pte", "contact_email": "s@store.example"}]
    state_db.save_store_atomic("pipeline", {"deals": deals}, config=cfg)
    return cfg


def _existing_row(page_id, company, stage="Lead", amount=1000, email=None):
    return _row(page_id, {"Company": company, "Deal stage": stage, "Amount": amount,
                          "Contact name": "Jo", "Contact email": email})


# ---------------------------------------------------------------- column mapping


def test_column_map_defaults_to_the_documented_names(fresh):
    assert nm.column_map(fresh, "pipeline")["Stage"] == "Stage"


def test_column_map_reads_config(existing):
    cmap = nm.column_map(existing, "pipeline")
    assert cmap["Name"] == "Company" and cmap["Currency"] is None


@pytest.mark.parametrize("columns, match", [
    ("  columns:\n    pipeline:\n      Name: null\n", "Name"),
    ("  columns:\n    pipeline:\n      Colour: Hue\n", "Colour"),
])
def test_invalid_column_maps_are_refused(tmp_path, columns, match):
    cfg = _config(tmp_path, notion=NOTION + columns)
    with pytest.raises(nm.NotionConfigError, match=match):
        nm.column_map(cfg, "pipeline")


def test_mapped_columns_are_read_and_written_under_their_notion_names(existing):
    nm.check_schema(existing, "pipeline", {**EXISTING_SCHEMA, "CoS ID": "rich_text"})
    rows = [_existing_row("p1", "Client Co", email="jo@client.example")]
    nm.queue_link(existing, nm.plan_link(existing, "pipeline", rows))
    _execute_local(existing, state_db.list_pending_actions(existing, state="requested")[0]["id"])

    plan = nm.plan_sync(existing, "pipeline", rows)
    push = next(p for p in plan["push"] if p["record_id"] == "deal-001")
    assert push["properties"]["Company"] == "Client Co"
    assert push["properties"]["CoS ID"] == "deal-001"
    assert "Name" not in push["properties"] and "Currency" not in push["properties"]


# ---------------------------------------------------------------- schema check


def test_sync_requires_a_schema_check(fresh):
    with pytest.raises(nm.NotionNotReady, match="check"):
        nm.plan_sync(fresh, "pipeline", [])
    report = nm.check_schema(fresh, "pipeline", PIPE_SCHEMA)
    assert report["ready"] is True and report["key_column"] == "present"
    assert nm.plan_sync(fresh, "pipeline", [])["push"]


def test_check_suggests_a_mapping_for_an_existing_database(fresh):
    report = nm.check_schema(fresh, "pipeline", EXISTING_SCHEMA)
    assert report["ready"] is False
    assert report["key_column"] == "missing"
    by = {c["canonical"]: c for c in report["columns"]}
    assert by["Name"]["status"] == "missing" and by["Name"]["suggestion"] == "Company"
    assert by["Stage"]["suggestion"] == "Deal stage"
    assert by["Value"]["suggestion"] == "Amount"
    assert by["Email"]["suggestion"] == "Contact email"
    assert by["Currency"]["suggestion"] is None
    suggested = report["suggested_config"]["notion"]["columns"]["pipeline"]
    assert suggested["Name"] == "Company" and suggested["Currency"] is None
    with pytest.raises(nm.NotionNotReady, match="check"):
        nm.plan_sync(fresh, "pipeline", [])


def test_check_flags_wrong_column_types(fresh):
    report = nm.check_schema(fresh, "pipeline", {**PIPE_SCHEMA, "Value": "rich_text"})
    by = {c["canonical"]: c for c in report["columns"]}
    assert by["Value"]["status"] == "wrong_type" and report["ready"] is False


def test_changing_the_column_map_after_a_check_requires_a_new_check(tmp_path):
    cfg = _config(tmp_path, notion=NOTION)
    nm.check_schema(cfg, "pipeline", PIPE_SCHEMA)
    changed = _config(tmp_path, notion=NOTION + "  columns:\n    pipeline:\n      Currency: null\n")
    with pytest.raises(nm.NotionNotReady, match="check"):
        nm.plan_sync(changed, "pipeline", [])


# ---------------------------------------------------------------- add columns


def test_missing_key_column_is_added_only_after_approval(existing):
    report = nm.check_schema(existing, "pipeline", EXISTING_SCHEMA)
    (action,) = nm.queue_add_columns(existing, report, ["CoS ID"])
    assert action["type"] == "notion.db.add_property"
    assert action["provider"] == "agent" and action["state"] == "requested"
    assert action["payload"] == {"domain": "pipeline", "database_id": "db-pipe",
                                 "property": "CoS ID", "type": "rich_text"}

    import action_risk
    assert action_risk.get_action_risk("notion.db.add_property") == "high"

    _execute_agent(existing, action["id"], {})
    # The schema changed: the recorded check is stale and must be re-run.
    with pytest.raises(nm.NotionNotReady, match="check"):
        nm.plan_sync(existing, "pipeline", [])


def test_adding_a_column_that_is_not_missing_is_refused(fresh):
    report = nm.check_schema(fresh, "pipeline", PIPE_SCHEMA)
    with pytest.raises(nm.NotionConfigError, match="Stage"):
        nm.queue_add_columns(fresh, report, ["Stage"])


def test_add_uses_the_mapped_name_and_expected_type(fresh):
    report = nm.check_schema(fresh, "pipeline", {k: v for k, v in PIPE_SCHEMA.items() if k != "Last activity"})
    (action,) = nm.queue_add_columns(fresh, report, ["Last activity"])
    assert action["payload"]["property"] == "Last activity" and action["payload"]["type"] == "date"


# ---------------------------------------------------------------- link


def test_unlinked_existing_database_is_refused_rather_than_duplicated(existing):
    nm.check_schema(existing, "pipeline", {**EXISTING_SCHEMA, "CoS ID": "rich_text"})
    rows = [_existing_row("p1", "Client Co")]
    with pytest.raises(nm.NotionNotReady, match="link"):
        nm.plan_sync(existing, "pipeline", rows)


def test_link_plan_matches_by_name_and_reports_the_rest(existing):
    nm.check_schema(existing, "pipeline", {**EXISTING_SCHEMA, "CoS ID": "rich_text"})
    rows = [
        _existing_row("p1", "  client co ", email="jo@client.example"),   # case/space-insensitive
        _existing_row("p2", "Other Ltd", email="x@nowhere.example"),
        _existing_row("p3", "Other Ltd", email="a@other.example"),          # email breaks the tie
        _existing_row("p4", "Notion Only Inc"),
    ]
    plan = nm.plan_link(existing, "pipeline", rows)
    assert {(m["record_id"], m["page_id"]) for m in plan["matched"]} == {("deal-001", "p1"), ("deal-002", "p3")}
    assert plan["notion_only"] == ["p2", "p4"]
    assert plan["store_only"] == ["deal-003"]
    assert plan["ambiguous"] == []


def test_link_plan_reports_ambiguous_matches(existing):
    nm.check_schema(existing, "pipeline", {**EXISTING_SCHEMA, "CoS ID": "rich_text"})
    rows = [_existing_row("p1", "Client Co"), _existing_row("p2", "Client Co")]
    plan = nm.plan_link(existing, "pipeline", rows)
    assert plan["ambiguous"] == [{"record_id": "deal-001", "page_ids": ["p1", "p2"]}]
    assert all(m["record_id"] != "deal-001" for m in plan["matched"])


def test_approved_link_enables_sync_without_duplicates(existing):
    nm.check_schema(existing, "pipeline", {**EXISTING_SCHEMA, "CoS ID": "rich_text"})
    rows = [
        _existing_row("p1", "Client Co", email="jo@client.example"),          # same values
        _existing_row("p2", "Other Ltd", amount=999, email="a@other.example"),  # differs
        _existing_row("p4", "Notion Only Inc"),
    ]
    action = nm.queue_link(existing, nm.plan_link(existing, "pipeline", rows))
    assert action["type"] == "notion.link" and action["provider"] == "local"
    assert nm.plan_sync.__name__  # linking itself changes nothing until approved
    with pytest.raises(nm.NotionNotReady):
        nm.plan_sync(existing, "pipeline", rows)
    assert _execute_local(existing, action["id"]) == 0

    plan = nm.plan_sync(existing, "pipeline", rows)
    assert {p["record_id"] for p in plan["push"]} == {"deal-001", "deal-003"}  # fill CoS ID; create store-only
    assert [c["record_id"] for c in plan["conflicts"]] == ["deal-002"]          # unknown which side is right
    assert [(p["op"], p["page_id"]) for p in plan["pull"]] == [("create", "p4")]  # offered as import


# ---------------------------------------------------------------- resolve


def _linked_with_conflict(cfg):
    nm.check_schema(cfg, "pipeline", {**EXISTING_SCHEMA, "CoS ID": "rich_text"})
    rows = [_existing_row("p2", "Other Ltd", stage="Proposal Sent", email="a@other.example")]
    _execute_local(cfg, nm.queue_link(cfg, nm.plan_link(cfg, "pipeline", rows))["id"])
    assert nm.plan_sync(cfg, "pipeline", rows)["conflicts"]
    return rows


def test_resolve_keep_store_pushes_the_store_values(existing):
    rows = _linked_with_conflict(existing)
    action = nm.queue_resolve(existing, "pipeline", "deal-002", "store", rows)
    assert action["type"] == "notion.resolve"
    assert _execute_local(existing, action["id"]) == 0
    plan = nm.plan_sync(existing, "pipeline", rows)
    push = next(p for p in plan["push"] if p["record_id"] == "deal-002")
    assert push["page_id"] == "p2" and push["properties"]["Deal stage"] == "Lead"
    assert not plan["conflicts"]


def test_resolve_keep_notion_pulls_the_notion_values(existing):
    rows = _linked_with_conflict(existing)
    assert _execute_local(existing, nm.queue_resolve(existing, "pipeline", "deal-002", "notion", rows)["id"]) == 0
    plan = nm.plan_sync(existing, "pipeline", rows)
    assert [(p["record_id"], p["changes"]) for p in plan["pull"]] == [("deal-002", {"stage": "Proposal Sent"})]


def test_resolve_is_refused_if_the_store_changed_meanwhile(existing):
    rows = _linked_with_conflict(existing)
    action = nm.queue_resolve(existing, "pipeline", "deal-002", "notion", rows)
    deals = state_db.load_store("pipeline", existing)["deals"]
    for d in deals:
        if d["id"] == "deal-002":
            d["value"] = 1
    state_db.save_store_atomic("pipeline", {"deals": deals}, config=existing)
    assert _execute_local(existing, action["id"]) == 1


def test_resolve_needs_a_real_conflict(existing):
    rows = _linked_with_conflict(existing)
    with pytest.raises(nm.NotionConfigError, match="conflict"):
        nm.queue_resolve(existing, "pipeline", "deal-001", "store", rows)


# ---------------------------------------------------------------- inputs + CLI


def test_briefing_inputs_respect_the_column_map(tmp_path):
    import notion_inputs as ni
    from datetime import datetime, timezone

    cfg = _config(tmp_path, notion=NOTION + "  columns:\n    todos:\n      Name: Task\n      Due: Deadline\n")
    state_db.save_store_atomic("todos", {"todos": [dict(TODO)]}, config=cfg)
    rows = [_row("t1", {"Task": "Renew domain", "Status": "open", "Deadline": "2026-11-10"})]
    summary = ni.summarise(cfg, {"databases": {"todos": rows}},
                           now=datetime(2026, 11, 12, tzinfo=timezone.utc))
    assert [t["title"] for t in summary["notion_only_tasks"]] == ["Renew domain"]


def test_cli_check_link_and_add(existing, tmp_path, capsys):
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "skills" / "notion-sync" / "scripts" / "notion_sync.py"
    spec = importlib.util.spec_from_file_location("notion_sync_cli2", script)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    cfg = str(existing.source_path)

    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"domain": "pipeline", "properties": EXISTING_SCHEMA}))
    assert cli.main(["--config", cfg, "check", "--input", str(schema), "--add", "CoS ID"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["report"]["key_column"] == "missing"
    assert [a["type"] for a in out["queued"]] == ["notion.db.add_property"]

    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps({"domain": "pipeline", "rows": [_existing_row("p1", "Client Co")]}))
    # Linking waits until the column exists and the database passes a fresh check.
    assert cli.main(["--config", cfg, "link", "--input", str(rows)]) == 1
    capsys.readouterr()
    _execute_agent(existing, out["queued"][0]["id"], {})
    capsys.readouterr()
    schema.write_text(json.dumps({"domain": "pipeline", "properties": {**EXISTING_SCHEMA, "CoS ID": "rich_text"}}))
    assert cli.main(["--config", cfg, "check", "--input", str(schema)]) == 0
    assert json.loads(capsys.readouterr().out)["report"]["ready"] is True
    assert cli.main(["--config", cfg, "link", "--input", str(rows)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["plan"]["matched"][0]["record_id"] == "deal-001" and out["queued"] is None
