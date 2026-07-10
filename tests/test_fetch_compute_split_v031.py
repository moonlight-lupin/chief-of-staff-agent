#!/usr/bin/env python3
"""Tests for v0.3.1 — Phase 1 fetch/compute split.

Covers:
1. Workspace record schema validation (message/event/file) happy + sad paths.
2. Envelope validation + normalize_workspace_payload defaults.
3. daily_briefing --input end-to-end (legacy JSON path) from a fixture envelope.
4. daily_briefing --input "-" (stdin).
5. daily_briefing --input invalid JSON / schema violation → non-zero exit.
6. AgentWorkspaceClient raises with guidance, health_check True, factory resolves.
7. daily_briefing run --input feeds calendar_deadlines.
"""
import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))
for skill in ("daily-briefing", "weekly-review", "meeting-prep"):
    d = PLUGIN_ROOT / "skills" / skill / "scripts"
    if d.exists() and str(d) not in sys.path:
        sys.path.insert(0, str(d))


# ── Sample records ─────────────────────────────────────────────────────────

def _msg(**over):
    base = {"id": "m1", "sender": "a@x.com", "subject": "Hello", "date": "2026-07-10T08:00:00Z"}
    base.update(over)
    return base


def _evt(**over):
    base = {"id": "e1", "title": "Standup", "start": "2026-07-10T09:00:00Z",
            "end": "2026-07-10T09:15:00Z"}
    base.update(over)
    return base


def _file(**over):
    base = {"id": "f1", "name": "NDA.pdf"}
    base.update(over)
    return base


# ── Schema: message ────────────────────────────────────────────────────────

class TestMessageSchema:
    def test_valid(self):
        from schemas import validate_message
        validate_message(_msg())
        validate_message(_msg(thread_id="t1", snippet="hi", tags=["INBOX"],
                              has_attachments=True, link="http://x", source="gmail"))

    def test_missing_required(self):
        from schemas import validate_message, SchemaError
        for field in ("id", "sender", "subject", "date"):
            bad = _msg()
            bad.pop(field)
            with pytest.raises(SchemaError):
                validate_message(bad)

    def test_bad_date(self):
        from schemas import validate_message, SchemaError
        with pytest.raises(SchemaError):
            validate_message(_msg(date="not-a-date"))

    def test_bad_tags(self):
        from schemas import validate_message, SchemaError
        with pytest.raises(SchemaError):
            validate_message(_msg(tags="INBOX"))
        with pytest.raises(SchemaError):
            validate_message(_msg(tags=[1, 2]))

    def test_bad_has_attachments(self):
        from schemas import validate_message, SchemaError
        with pytest.raises(SchemaError):
            validate_message(_msg(has_attachments="yes"))


# ── Schema: event ──────────────────────────────────────────────────────────

class TestEventSchema:
    def test_valid(self):
        from schemas import validate_event
        validate_event(_evt())
        validate_event(_evt(attendees=["a@x.com"], organizer="o@x.com",
                            location="Room", conference_link="http://z",
                            status="confirmed", source="outlook"))

    def test_missing_required(self):
        from schemas import validate_event, SchemaError
        for field in ("id", "title", "start", "end"):
            bad = _evt()
            bad.pop(field)
            with pytest.raises(SchemaError):
                validate_event(bad)

    def test_bad_start(self):
        from schemas import validate_event, SchemaError
        with pytest.raises(SchemaError):
            validate_event(_evt(start="yesterday"))

    def test_bad_attendees(self):
        from schemas import validate_event, SchemaError
        with pytest.raises(SchemaError):
            validate_event(_evt(attendees="a@x.com"))


# ── Schema: file ───────────────────────────────────────────────────────────

class TestFileSchema:
    def test_valid(self):
        from schemas import validate_file
        validate_file(_file())
        validate_file(_file(mime_type="application/pdf", modified="2026-07-01T00:00:00Z",
                           link="http://d", parents=["root"], source="agent"))

    def test_missing_required(self):
        from schemas import validate_file, SchemaError
        for field in ("id", "name"):
            bad = _file()
            bad.pop(field)
            with pytest.raises(SchemaError):
                validate_file(bad)

    def test_bad_modified(self):
        from schemas import validate_file, SchemaError
        with pytest.raises(SchemaError):
            validate_file(_file(modified="whenever"))

    def test_bad_parents(self):
        from schemas import validate_file, SchemaError
        with pytest.raises(SchemaError):
            validate_file(_file(parents="root"))


# ── Schema: envelope ───────────────────────────────────────────────────────

class TestEnvelope:
    def test_valid_full(self):
        from schemas import validate_workspace_payload
        validate_workspace_payload({
            "generated_at": "2026-07-10T08:00:00Z", "source": "agent",
            "messages": [_msg()], "events": [_evt()], "files": [_file()],
        })

    def test_empty_ok(self):
        from schemas import validate_workspace_payload
        validate_workspace_payload({})

    def test_not_mapping(self):
        from schemas import validate_workspace_payload, SchemaError
        with pytest.raises(SchemaError):
            validate_workspace_payload([_msg()])

    def test_list_must_be_list(self):
        from schemas import validate_workspace_payload, SchemaError
        with pytest.raises(SchemaError):
            validate_workspace_payload({"messages": {"id": "m1"}})

    def test_bad_generated_at(self):
        from schemas import validate_workspace_payload, SchemaError
        with pytest.raises(SchemaError):
            validate_workspace_payload({"generated_at": "soon"})

    def test_bad_source_type(self):
        from schemas import validate_workspace_payload, SchemaError
        with pytest.raises(SchemaError):
            validate_workspace_payload({"source": 123})

    def test_indexed_error_message(self):
        from schemas import validate_workspace_payload, SchemaError
        with pytest.raises(SchemaError) as exc:
            validate_workspace_payload({"messages": [_msg(), _msg(id="")]})
        assert "messages[1]" in str(exc.value)

    def test_normalize_fills_defaults(self):
        from schemas import normalize_workspace_payload
        out = normalize_workspace_payload({"messages": [_msg()], "events": [_evt()],
                                           "files": [_file()]})
        assert out["messages"][0]["tags"] == []
        assert out["events"][0]["attendees"] == []
        assert out["files"][0]["parents"] == []
        assert out["generated_at"] is None
        # Does not mutate input record defaults
        assert "tags" not in _msg()

    def test_normalize_rejects_invalid(self):
        from schemas import normalize_workspace_payload, SchemaError
        with pytest.raises(SchemaError):
            normalize_workspace_payload({"messages": [{"id": "m1"}]})


# ── AgentWorkspaceClient ───────────────────────────────────────────────────

class TestAgentWorkspaceClient:
    def test_health_check_true_and_name(self):
        from providers.agent_workspace import AgentWorkspaceClient
        client = AgentWorkspaceClient({})
        assert client.health_check() is True
        assert client.provider_name == "agent"

    def test_read_methods_raise_with_guidance(self):
        from providers.agent_workspace import AgentWorkspaceClient
        client = AgentWorkspaceClient({})
        for call in (lambda: client.mail_search("q"),
                     lambda: client.calendar_list("a", "b"),
                     lambda: client.files_search("q")):
            with pytest.raises(NotImplementedError) as exc:
                call()
            msg = str(exc.value)
            assert "--input" in msg
            assert "schemas.py" in msg

    def test_write_methods_raise(self):
        from providers.agent_workspace import AgentWorkspaceClient
        client = AgentWorkspaceClient({})
        with pytest.raises(NotImplementedError):
            client.mail_send("a@x.com", "s", "b")
        with pytest.raises(NotImplementedError):
            client.files_upload("/tmp/x")

    def test_factory_resolves_agent(self):
        from workspace_client import get_workspace_client
        client = get_workspace_client({"integrations": {"workspace": {"provider": "agent"}}})
        assert client.provider_name == "agent"
        assert client.health_check() is True


# ── daily_briefing --input ─────────────────────────────────────────────────

def _write_config(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = {
        "company": {"name": "Test Co", "jurisdiction": "SG", "currency": "SGD",
                     "incorporation_date": "2026-01-01", "financial_year_end": "31 Dec",
                     "business_type": "professional_services"},
        "google": {"delegate_email": "t@test.com", "account_alias": "test",
                    "domain": "test.com", "service_account_path": "/tmp/sa.json"},
        "paths": {"project_root": str(project), "wiki_path": str(project / "wiki"),
                   "templates": str(PLUGIN_ROOT / "shared" / "templates")},
        "delivery": {"channel": "telegram", "briefing_time": "08:00",
                      "weekly_review_day": "friday", "weekly_review_time": "17:00",
                      "timezone": "Asia/Singapore"},
        "integrations": {"workspace": {"provider": "agent"}},
        "sales_stages": ["Lead", "Proposal Sent", "NDA Signed", "Contract Signed",
                         "Invoiced", "Paid"],
    }
    import yaml
    config_path = project / "company.yaml"
    config_path.write_text(yaml.safe_dump(config))
    return config_path, project


def _envelope():
    return {
        "generated_at": "2026-07-10T08:00:00Z", "source": "agent",
        "messages": [_msg(), _msg(id="m2", subject="Invoice")],
        "events": [_evt(title="Board meeting")],
        "files": [_file()],
    }


class TestDailyBriefingInput:
    def test_legacy_json_from_input_file(self, tmp_path, monkeypatch):
        config_path, _ = _write_config(tmp_path)
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))
        env_file = tmp_path / "workspace.json"
        env_file.write_text(json.dumps(_envelope()))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = daily_briefing.main(["--config", str(config_path), "--json",
                                      "--input", str(env_file)])
        assert rc == 0
        data = json.loads(buf.getvalue())
        gmail = data["sources"]["gmail"]
        assert gmail["status"] == "ok"
        assert len(gmail["items"][0]["result"]) == 2
        cal = data["sources"]["calendar"]
        assert cal["status"] == "ok"
        assert cal["items"][0]["title"] == "Board meeting"

    def test_input_via_stdin(self, tmp_path, monkeypatch):
        config_path, _ = _write_config(tmp_path)
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_envelope())))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = daily_briefing.main(["--config", str(config_path), "--json", "--input", "-"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["sources"]["calendar"]["status"] == "ok"

    def test_no_client_constructed_with_input(self, tmp_path, monkeypatch):
        """With --input the agent provider (which raises on fetch) must not be called."""
        config_path, _ = _write_config(tmp_path)
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))
        env_file = tmp_path / "workspace.json"
        env_file.write_text(json.dumps(_envelope()))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = daily_briefing.main(["--config", str(config_path), "--json",
                                      "--input", str(env_file)])
        assert rc == 0
        data = json.loads(buf.getvalue())
        # agent provider raises NotImplementedError on fetch; if it had been used
        # gmail/calendar would be 'failed'. They are 'ok' because --input bypassed it.
        assert data["sources"]["gmail"]["status"] == "ok"
        assert data["sources"]["calendar"]["status"] == "ok"

    def test_missing_input_file(self, tmp_path, monkeypatch):
        config_path, _ = _write_config(tmp_path)
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))
        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            rc = daily_briefing.main(["--config", str(config_path), "--json",
                                      "--input", str(tmp_path / "nope.json")])
        assert rc != 0

    def test_invalid_json(self, tmp_path, monkeypatch):
        config_path, _ = _write_config(tmp_path)
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))
        env_file = tmp_path / "bad.json"
        env_file.write_text("{not json")
        import daily_briefing
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = daily_briefing.main(["--config", str(config_path), "--json",
                                      "--input", str(env_file)])
        assert rc != 0

    def test_schema_violation(self, tmp_path, monkeypatch):
        config_path, _ = _write_config(tmp_path)
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))
        env_file = tmp_path / "bad_schema.json"
        env_file.write_text(json.dumps({"events": [{"id": "e1"}]}))  # missing title/start/end
        import daily_briefing
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = daily_briefing.main(["--config", str(config_path), "--json",
                                      "--input", str(env_file)])
        assert rc != 0

    def test_run_subcommand_input_populates_calendar(self, tmp_path, monkeypatch):
        config_path, _ = _write_config(tmp_path)
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))
        env_file = tmp_path / "workspace.json"
        env_file.write_text(json.dumps(_envelope()))
        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = daily_briefing.main(["run", "--config", str(config_path), "--json",
                                      "--dry-run", "--input", str(env_file)])
        assert rc == 0
        data = json.loads(buf.getvalue())
        cal = data["sections"]["calendar_deadlines"]
        assert any(item.get("summary") == "Board meeting" for item in cal)


# ── weekly-review workspace_collect --input ────────────────────────────────

class TestWorkspaceCollectInput:
    def test_all_from_input(self, tmp_path, monkeypatch):
        env_file = tmp_path / "ws.json"
        env_file.write_text(json.dumps(_envelope()))
        import workspace_collect
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = workspace_collect.main(["--input", str(env_file), "all",
                                         "--week-start", "2026-07-06"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert len(data["gmail_unread"]) == 2
        assert len(data["calendar_events"]) == 1
        assert len(data["drive_recent"]) == 1

    def test_invalid_input_nonzero(self, tmp_path):
        env_file = tmp_path / "ws.json"
        env_file.write_text(json.dumps({"events": [{"id": "e1"}]}))
        import workspace_collect
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = workspace_collect.main(["--input", str(env_file), "all"])
        assert rc != 0


# ── meeting-prep workspace_actions --input ─────────────────────────────────

class TestWorkspaceActionsInput:
    def test_gather_from_input(self, tmp_path):
        env_file = tmp_path / "ws.json"
        env_file.write_text(json.dumps(_envelope()))
        import workspace_actions
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = workspace_actions.main(["--input", str(env_file), "gather",
                                         "--event-id", "e1"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["event"]["id"] == "e1"
        assert len(data["drive_files"]) == 1
        assert data["gmail_context"][0]["messages"]

    def test_calendar_context_from_input(self, tmp_path):
        env_file = tmp_path / "ws.json"
        env_file.write_text(json.dumps(_envelope()))
        import workspace_actions
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = workspace_actions.main(["--input", str(env_file), "calendar-context",
                                         "--start", "2026-07-06", "--end", "2026-07-16"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert len(data) == 1
