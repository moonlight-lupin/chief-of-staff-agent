#!/usr/bin/env python3
"""v0.3.21 — Daily briefing suppresses inbound mail already replied in-thread."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
DAILY_BRIEFING = PLUGIN_ROOT / "skills" / "daily-briefing" / "scripts"
for p in (SHARED_SCRIPTS, DAILY_BRIEFING, PLUGIN_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


@pytest.fixture
def tmp_project(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEF_OF_STAFF_PROJECT_ROOT", str(tmp_path))
    return tmp_path


class TestReplyHelpers:
    def test_reply_index_and_filter(self):
        import daily_briefing as db

        operator = "me@example.com"
        inbound = {
            "id": "m1",
            "sender": "client@acme.com",
            "subject": "Need approval",
            "date": "2026-07-10T09:00:00Z",
            "thread_id": "t1",
        }
        reply = {
            "id": "m2",
            "sender": "me@example.com",
            "subject": "Re: Need approval",
            "date": "2026-07-10T11:00:00Z",
            "thread_id": "t1",
        }
        other = {
            "id": "m3",
            "sender": "other@acme.com",
            "subject": "Still open",
            "date": "2026-07-11T09:00:00Z",
            "thread_id": "t2",
        }
        index = db._reply_index_from_messages([inbound, reply, other], operator)
        assert "t1" in index
        assert "t2" not in index

        kept, suppressed = db._filter_replied_messages(
            [inbound, other], index, operator,
        )
        assert suppressed == 1
        assert kept == [other]

    def test_no_suppress_without_thread_id(self):
        import daily_briefing as db

        msg = {
            "id": "m1",
            "sender": "client@acme.com",
            "subject": "Hi",
            "date": "2026-07-10T09:00:00Z",
        }
        index = {"some-other": db.datetime.fromisoformat("2026-07-10T12:00:00+00:00")}
        kept, suppressed = db._filter_replied_messages([msg], index, "me@example.com")
        assert suppressed == 0
        assert kept == [msg]

    def test_sent_side_query_detection(self):
        import daily_briefing as db

        assert db._is_sent_side_query("sent_followup", "from:me@x.com newer_than:7d", "me@x.com")
        assert db._is_sent_side_query("x", "in:sent newer_than:7d", "me@x.com")
        assert not db._is_sent_side_query(
            "unread_priority", 'in:inbox is:unread newer_than:3d', "me@x.com",
        )


class TestCollectGmailReplyAwareness:
    def test_collect_gmail_suppresses_replied_threads(
        self, tmp_project, monkeypatch,
    ):
        import daily_briefing as db

        queries_path = tmp_project / "queries.yaml"
        queries_path.write_text(
            "queries:\n"
            "  - name: unread_priority\n"
            "    query: 'in:inbox is:unread newer_than:3d'\n"
            "    max: 10\n"
            "  - name: sent_followup\n"
            "    query: 'from:{delegate_email} newer_than:7d'\n"
            "    max: 10\n",
            encoding="utf-8",
        )

        inbound = {
            "id": "in1",
            "sender": "client@acme.com",
            "subject": "Please review",
            "date": "2026-07-10T08:00:00Z",
            "thread_id": "thr-1",
        }
        still_open = {
            "id": "in2",
            "sender": "other@acme.com",
            "subject": "Urgent",
            "date": "2026-07-11T08:00:00Z",
            "thread_id": "thr-2",
        }
        sent_reply = {
            "id": "out1",
            "sender": "me@example.com",
            "subject": "Re: Please review",
            "date": "2026-07-10T12:00:00Z",
            "thread_id": "thr-1",
        }

        client = MagicMock()

        def _search(query, max_results=10):
            q = query.lower()
            if "in:sent" in q:
                return [sent_reply]
            if "from:me@example.com" in q:
                return [sent_reply]
            if "in:inbox" in q or "is:unread" in q:
                return [inbound, still_open]
            return []

        client.mail_search.side_effect = _search

        config = {
            "integrations": {"workspace": {"provider": "google_api"}},
            "google": {"delegate_email": "me@example.com"},
            "paths": {"project_root": str(tmp_project)},
        }
        monkeypatch.setattr(db, "_get_workspace_client", lambda _cfg: client)
        monkeypatch.setattr(db, "ensure_workspace_config", lambda _cfg: None)
        monkeypatch.setattr(db, "sibling_or_shared", lambda _cfg, _name: queries_path)

        result = db.collect_gmail(config, tmp_project)
        assert isinstance(result, dict)
        assert result["suppressed_replied"] == 1
        unread = next(i for i in result["items"] if i["name"] == "unread_priority")
        assert [m["id"] for m in unread["result"]] == ["in2"]
        assert unread["suppressed_replied"] == 1
        # sent_followup is not filtered as inbound outstanding
        sent = next(i for i in result["items"] if i["name"] == "sent_followup")
        assert len(sent["result"]) == 1

    def test_agent_input_filters_replied(self):
        import daily_briefing as db

        messages = [
            {
                "id": "m1",
                "sender": "client@acme.com",
                "subject": "Need this",
                "date": "2026-07-10T08:00:00Z",
                "thread_id": "t9",
            },
            {
                "id": "m2",
                "sender": "me@example.com",
                "subject": "Re: Need this",
                "date": "2026-07-10T09:00:00Z",
                "thread_id": "t9",
            },
            {
                "id": "m3",
                "sender": "open@acme.com",
                "subject": "Still waiting",
                "date": "2026-07-11T08:00:00Z",
                "thread_id": "t10",
            },
        ]
        out = db._messages_to_gmail_items(messages, operator="me@example.com")
        assert isinstance(out, dict)
        assert out["suppressed_replied"] == 2  # inbound replied + operator msg
        kept_ids = [m["id"] for m in out["items"][0]["result"]]
        assert kept_ids == ["m3"]
