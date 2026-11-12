"""Notion as an input to existing skills: daily briefing, weekly review, meeting prep.

The agent reads Notion with its own connector and adds an optional ``notion``
block to the workspace envelope. Skills summarise it read-only.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config_loader
import notion_inputs as ni
import notion_mirror as nm
import schemas
import state_db
from test_notion_mirror import DEAL, TODO, _checked, _config, _row

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def _load_script(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def config(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(cfg.source_path))
    state_db.save_store_atomic("pipeline", {"deals": [dict(DEAL)]}, config=cfg)
    state_db.save_store_atomic("todos", {"todos": [dict(TODO)]}, config=cfg)
    return _checked(cfg)


NOTION_BLOCK = {
    "pages": [
        {"id": "p-old", "title": "Old plan", "last_edited_time": _iso(96)},
        {"id": "p-new", "title": "Q4 launch plan", "last_edited_time": _iso(2),
         "last_edited_by": "Sam", "url": "https://notion.so/p-new"},
        {"id": "p-client", "title": "Client Co — kickoff notes", "last_edited_time": _iso(5)},
    ],
    "comments": [
        {"id": "c1", "page_id": "p-new", "page_title": "Q4 launch plan", "author": "Sam",
         "created_time": _iso(1), "mentions_operator": True, "text": "SECRET BODY please review"},
        {"id": "c2", "page_id": "p-old", "page_title": "Old plan", "author": "Kim",
         "created_time": _iso(1), "mentions_operator": False},
    ],
}


# ---------------------------------------------------------------- envelope


def test_envelope_accepts_and_keeps_a_notion_block():
    payload = {"messages": [], "notion": NOTION_BLOCK}
    cleaned = schemas.normalize_workspace_payload(payload)
    assert cleaned["notion"]["pages"][1]["title"] == "Q4 launch plan"
    assert cleaned["notion"]["databases"] == {}


def test_envelope_without_notion_is_unchanged():
    cleaned = schemas.normalize_workspace_payload({"messages": []})
    assert "notion" not in cleaned


@pytest.mark.parametrize("bad, match", [
    ({"pages": [{"title": "x", "last_edited_time": _iso(1)}]}, "id"),
    ({"pages": [{"id": "p", "title": "x", "last_edited_time": "yesterday"}]}, "last_edited_time"),
    ({"comments": [{"id": "c", "page_id": "p", "created_time": _iso(1)}]}, "author"),
    ({"databases": {"todos": [{"properties": {}}]}}, "page_id"),
    ({"pages": "nope"}, "list"),
])
def test_envelope_rejects_malformed_notion_records(bad, match):
    with pytest.raises(schemas.SchemaError, match=match):
        schemas.normalize_workspace_payload({"notion": bad})


# ---------------------------------------------------------------- summary


def test_summary_lists_recent_pages_newest_first(config):
    summary = ni.summarise(config, NOTION_BLOCK, since_hours=24, now=NOW)
    assert [p["title"] for p in summary["recent_pages"]] == ["Q4 launch plan", "Client Co — kickoff notes"]


def test_mentions_need_attention_without_exposing_comment_text(config):
    summary = ni.summarise(config, NOTION_BLOCK, since_hours=24, now=NOW)
    assert [m["page_title"] for m in summary["mentions"]] == ["Q4 launch plan"]
    assert "SECRET" not in json.dumps(summary)
    assert any("Q4 launch plan" in item["title"] for item in summary["needs_attention"])


def test_notion_only_tasks_due_soon_are_surfaced(config):
    rows = [
        _row("t-1", {"Name": "Book venue", "Status": "open", "Due": "2026-11-10"}),   # overdue
        _row("t-2", {"Name": "Plan party", "Status": "open", "Due": "2026-12-30"}),   # not soon
        _row("t-3", {"Name": "Done thing", "Status": "done", "Due": "2026-11-01"}),   # done
        _row("t-4", nm.to_properties("todos", TODO)),                                 # already in store
    ]
    summary = ni.summarise(config, {"databases": {"todos": rows}}, since_hours=24, now=NOW)
    assert [t["title"] for t in summary["notion_only_tasks"]] == ["Book venue"]
    assert summary["notion_only_tasks"][0]["overdue"] is True


def test_sync_status_counts_edits_waiting_and_conflicts(config):
    from test_notion_mirror import _push_all

    _push_all(config, "pipeline")
    edited = {**nm.to_properties("pipeline", DEAL), "Stage": "Proposal Sent"}
    summary = ni.summarise(config, {"databases": {"pipeline": [_row("page-0", edited)]}}, now=NOW)
    status = summary["sync"]["pipeline"]
    assert status["notion_edits_to_review"] == 1 and status["already_queued"] == 0
    assert any("notion-sync" in a["title"] for a in summary["next_actions"])

    nm.queue_plan(config, nm.plan_sync(config, "pipeline", [_row("page-0", edited)]))
    status = ni.summarise(config, {"databases": {"pipeline": [_row("page-0", edited)]}}, now=NOW)["sync"]["pipeline"]
    assert status["already_queued"] == 1


def test_summary_is_read_only(config):
    from test_notion_mirror import _push_all

    _push_all(config, "pipeline")
    before = state_db.list_pending_actions(config)
    edited = {**nm.to_properties("pipeline", DEAL), "Stage": "Proposal Sent"}
    ni.summarise(config, {"databases": {"pipeline": [_row("page-0", edited)]}}, now=NOW)
    assert state_db.list_pending_actions(config) == before


def test_rows_for_unmapped_databases_are_noted_not_planned(tmp_path):
    cfg = _config(tmp_path, notion="notion:\n  databases:\n    todos: db-todo\n")
    summary = ni.summarise(cfg, {"databases": {"pipeline": [_row("x", {"Name": "A"})]}}, now=NOW)
    assert "pipeline" not in summary["sync"]
    assert "pipeline" in summary["notes"][0]


# ---------------------------------------------------------------- daily briefing


def test_daily_briefing_has_a_notion_section(config, monkeypatch):
    daily = _load_script("skills/daily-briefing/scripts/daily_briefing.py", "daily_briefing_notion")
    monkeypatch.setattr(ni, "_now", lambda: NOW)
    envelope = schemas.normalize_workspace_payload({"notion": NOTION_BLOCK})
    briefing = daily._build_structured_briefing(str(config.source_path), workspace_input=envelope)

    notion = briefing["sections"]["notion"]
    assert [p["title"] for p in notion["recent_pages"]][0] == "Q4 launch plan"
    assert briefing["summary"]["notion_items"] >= 1
    assert any("Notion" in item["title"] for item in briefing["sections"]["needs_attention"])

    from briefing_renderer import render

    for fmt in ("text", "markdown", "html"):
        rendered = render(briefing, fmt)
        assert "Notion" in rendered and "Q4 launch plan" in rendered
        assert "SECRET" not in rendered


def test_daily_briefing_without_notion_input_has_no_notion_section(config):
    daily = _load_script("skills/daily-briefing/scripts/daily_briefing.py", "daily_briefing_plain")
    briefing = daily._build_structured_briefing(str(config.source_path),
                                                workspace_input=schemas.normalize_workspace_payload({}))
    assert "notion" not in briefing["sections"]


# ---------------------------------------------------------------- weekly review


def test_weekly_collect_includes_notion(config, tmp_path, capsys, monkeypatch):
    weekly = _load_script("skills/weekly-review/scripts/workspace_collect.py", "workspace_collect_notion")
    monkeypatch.setattr(ni, "_now", lambda: NOW)
    envelope = tmp_path / "env.json"
    envelope.write_text(json.dumps({"notion": NOTION_BLOCK}))
    assert weekly.main(["--config", str(config.source_path), "--input", str(envelope), "all",
                        "--week-start", "2026-11-09"]) == 0
    out = json.loads(capsys.readouterr().out)
    titles = [p["title"] for p in out["notion"]["recent_pages"]]
    assert titles == ["Q4 launch plan", "Client Co — kickoff notes"]  # 7-day window from week start


# ---------------------------------------------------------------- meeting prep


def test_meeting_prep_finds_notion_context_for_attendees(config, tmp_path, capsys):
    prep = _load_script("skills/meeting-prep/scripts/workspace_actions.py", "workspace_actions_notion")
    event = {"id": "e1", "title": "Kickoff", "start": _iso(-2), "end": _iso(-3),
             "attendees": ["jo@client.example", "me@acme.example"]}
    rows = [_row("page-0", nm.to_properties("pipeline", DEAL))]
    envelope = tmp_path / "env.json"
    envelope.write_text(json.dumps({"events": [event], "notion": {**NOTION_BLOCK,
                                                                   "databases": {"pipeline": rows}}}))
    assert prep.main(["--config", str(config.source_path), "--input", str(envelope),
                      "gather", "--event-id", "e1"]) == 0
    out = json.loads(capsys.readouterr().out)
    matched = {c["title"]: c["matched"] for c in out["notion_context"]}
    assert "Client Co — kickoff notes" in matched          # page title names the attendee's client
    assert "Client Co" in matched                          # pipeline row for the attendee
    assert "Q4 launch plan" not in matched
