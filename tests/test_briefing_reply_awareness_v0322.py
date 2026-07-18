#!/usr/bin/env python3
"""v0.3.22 — Follow-ups to v0.3.21 reply awareness.

Covers the review notes on PR #19:
- ``_is_from_operator`` matches the operator by parsed address, not a loose
  substring (no more false positives like "jo@x.com" ⊂ "jojo@x.com").
- The sent-mail scan window (lookback days / max messages) is configurable via
  an optional ``briefing`` config section so older replies can still suppress.
- Full ``collect_gmail`` unread→reply suppression exercised end-to-end with the
  real Composio field shape (``messageTimestamp`` + ``"Name <email>"`` sender +
  ``threadId``), closing the gap the live 0→5 index check left open.
"""
from __future__ import annotations

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


class TestIsFromOperatorMatching:
    def test_exact_plain_address(self):
        import daily_briefing as db

        assert db._is_from_operator({"sender": "me@example.com"}, "me@example.com")

    def test_name_and_angle_address(self):
        import daily_briefing as db

        assert db._is_from_operator(
            {"sender": "Op Name <me@example.com>"}, "me@example.com",
        )

    def test_substring_address_is_not_a_match(self):
        import daily_briefing as db

        # "me@example.com" is a substring of "jme@example.com" — the old
        # ``operator in sender`` check matched this; the parsed-address check
        # must not.
        assert not db._is_from_operator(
            {"sender": "jme@example.com"}, "me@example.com",
        )
        assert not db._is_from_operator(
            {"sender": "Someone Else <jme@example.com>"}, "me@example.com",
        )

    def test_address_only_in_display_name_is_not_a_match(self):
        import daily_briefing as db

        # Operator address echoed in the display name but a different real
        # address must not count as from the operator.
        assert not db._is_from_operator(
            {"sender": "me@example.com via list <bounce@list.acme.com>"},
            "me@example.com",
        )


class TestReplyScanLimits:
    def test_defaults(self):
        import daily_briefing as db

        assert db._reply_scan_limits({}) == (
            db._REPLY_LOOKBACK_DAYS,
            db._REPLY_SENT_MAX,
        )

    def test_config_override(self):
        import daily_briefing as db

        cfg = {"briefing": {"reply_lookback_days": 30, "reply_sent_max": 200}}
        assert db._reply_scan_limits(cfg) == (30, 200)

    def test_invalid_values_fall_back_to_defaults(self):
        import daily_briefing as db

        cfg = {"briefing": {"reply_lookback_days": "nope", "reply_sent_max": -5}}
        assert db._reply_scan_limits(cfg) == (
            db._REPLY_LOOKBACK_DAYS,
            db._REPLY_SENT_MAX,
        )

    def test_build_index_honours_configured_window(self):
        import daily_briefing as db

        client = MagicMock()
        client.mail_search.return_value = []
        db._build_operator_reply_index(
            client, "me@example.com", lookback_days=30, sent_max=200,
        )
        args, kwargs = client.mail_search.call_args
        assert "newer_than:30d" in args[0]
        assert kwargs.get("max_results") == 200


class TestCollectGmailLiveShapeEndToEnd:
    """Full collect_gmail suppress path with Composio's real field shape."""

    def _msg(self, thread, sender, ts, mid):
        return {
            "messageId": mid,
            "threadId": thread,
            "sender": sender,
            "subject": "s",
            "messageTimestamp": ts,
        }

    def test_unread_reply_cycle_suppresses_answered_thread(
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

        operator = "me@example.com"
        # Live shape: "Name <email>" sender, messageTimestamp, camelCase threadId.
        inbound = self._msg("thr-1", "Client <client@acme.com>",
                            "2026-07-10T08:00:00Z", "in1")
        still_open = self._msg("thr-2", "Other <other@acme.com>",
                              "2026-07-11T08:00:00Z", "in2")
        sent_reply = self._msg("thr-1", f"Op Name <{operator}>",
                              "2026-07-10T12:00:00Z", "out1")

        client = MagicMock()

        def _search(query, max_results=10):
            q = query.lower()
            if "in:sent" in q or f"from:{operator}" in q:
                return [sent_reply]
            if "in:inbox" in q or "is:unread" in q:
                return [inbound, still_open]
            return []

        client.mail_search.side_effect = _search

        config = {
            "integrations": {"workspace": {"provider": "google_api"}},
            "google": {"delegate_email": operator},
            "paths": {"project_root": str(tmp_project)},
        }
        monkeypatch.setattr(db, "_get_workspace_client", lambda _cfg: client)
        monkeypatch.setattr(db, "ensure_workspace_config", lambda _cfg: None)
        monkeypatch.setattr(db, "sibling_or_shared", lambda _cfg, _name: queries_path)

        result = db.collect_gmail(config, tmp_project)
        assert isinstance(result, dict)
        # thr-1 was answered → suppressed; thr-2 still open → kept.
        assert result["suppressed_replied"] == 1
        unread = next(i for i in result["items"] if i["name"] == "unread_priority")
        assert [m["messageId"] for m in unread["result"]] == ["in2"]
