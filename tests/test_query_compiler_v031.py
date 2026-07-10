#!/usr/bin/env python3
"""Tests for query_compiler.compile_query — pure, no I/O."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

from query_compiler import compile_query, QueryTranslationWarning  # noqa: E402

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


# ── Gmail dialect ──────────────────────────────────────────────────────

class TestGmailDialect:
    def test_string_model_passthrough(self):
        assert compile_query("is:unread newer_than:3d", "gmail") == "is:unread newer_than:3d"

    def test_dict_full(self):
        q = compile_query(
            {"unread": True, "from_sender": "x@y.com", "newer_than_days": 2,
             "has_attachment": True},
            "gmail",
        )
        assert q == "is:unread from:x@y.com newer_than:2d has:attachment"

    def test_older_than_and_tag_and_folder(self):
        q = compile_query(
            {"older_than_days": 30, "tag": "Finance", "folder": "inbox"}, "gmail"
        )
        assert q == "older_than:30d label:Finance in:inbox"

    def test_subject_with_space_is_quoted(self):
        q = compile_query({"subject_contains": "status update"}, "gmail")
        assert q == 'subject:"status update"'

    def test_raw_override_wins(self):
        q = compile_query(
            {"unread": True, "raw": {"gmail": "in:sent to:boss@x.com"}}, "gmail"
        )
        assert q == "in:sent to:boss@x.com"


# ── m365 dialect ───────────────────────────────────────────────────────

class TestM365Dialect:
    def test_string_model_passthrough_to_search(self):
        assert compile_query("hello", "m365") == {
            "folder": None, "filter": None, "search": "hello"}

    def test_filter_only(self):
        r = compile_query({"unread": True, "from_sender": "a@b.com"}, "m365", now=NOW)
        assert r["search"] is None
        assert r["filter"] == "isRead eq false and from/emailAddress/address eq 'a@b.com'"

    def test_search_only(self):
        r = compile_query({"subject_contains": "invoice"}, "m365", now=NOW)
        assert r["filter"] is None
        assert r["search"] == "subject:invoice"

    def test_newer_than_days_uses_injected_now(self):
        r = compile_query({"newer_than_days": 2}, "m365", now=NOW)
        # 2026-07-10 minus 2 days = 2026-07-08
        assert r["filter"] == "receivedDateTime ge 2026-07-08T12:00:00Z"

    def test_older_than_days(self):
        r = compile_query({"older_than_days": 10}, "m365", now=NOW)
        assert r["filter"] == "receivedDateTime le 2026-06-30T12:00:00Z"

    def test_has_attachment_and_tag_are_filter(self):
        r = compile_query({"has_attachment": True, "tag": "Legal"}, "m365", now=NOW)
        assert r["search"] is None
        assert r["filter"] == "hasAttachments eq true and categories/any(c:c eq 'Legal')"

    def test_filter_and_search_fold_to_kql(self):
        # unread (filter) + subject_contains (search) -> must fold, filter None
        r = compile_query(
            {"unread": True, "from_sender": "a@b.com", "newer_than_days": 2,
             "has_attachment": True, "subject_contains": "invoice"},
            "m365", now=NOW,
        )
        assert r["filter"] is None
        assert r["search"] == (
            "isread:false from:a@b.com received>=2026-07-08 "
            "hasattachments:true subject:invoice"
        )

    def test_quote_escaping_in_odata(self):
        r = compile_query({"from_sender": "o'brien@x.com"}, "m365", now=NOW)
        assert r["filter"] == "from/emailAddress/address eq 'o''brien@x.com'"

    def test_raw_dict_override_wins(self):
        r = compile_query(
            {"unread": True, "raw": {"m365": {"filter": "importance eq 'high'", "search": None}}},
            "m365", now=NOW,
        )
        assert r == {"folder": None, "filter": "importance eq 'high'", "search": None}

    def test_raw_string_override_is_search(self):
        r = compile_query({"raw": {"m365": "from:ceo"}}, "m365", now=NOW)
        assert r == {"folder": None, "filter": None, "search": "from:ceo"}

    def test_domain_goes_to_search(self):
        r = compile_query({"domain": "acme.com"}, "m365", now=NOW)
        assert r == {"folder": None, "filter": None, "search": "from:acme.com"}

    # ── Folder scope carried out-of-band (not a parentFolderId $filter) ──

    def test_in_inbox_is_out_of_band_folder(self):
        assert compile_query("in:inbox", "m365", now=NOW) == {
            "folder": "inbox", "filter": None, "search": None}

    def test_in_scope_well_known_folder_map(self):
        cases = {
            "in:sent": "sentitems",
            "in:drafts": "drafts",
            "in:draft": "drafts",
            "in:trash": "deleteditems",
            "in:spam": "junkemail",
        }
        for query, expected in cases.items():
            out = compile_query(query, "m365", now=NOW)
            assert out == {"folder": expected, "filter": None, "search": None}, query

    def test_in_anywhere_is_no_scope(self):
        # in:anywhere is an explicit whole-mailbox scope -> folder None, and with
        # nothing else it is an empty translation (ValueError).
        with pytest.raises(ValueError):
            compile_query("in:anywhere", "m365", now=NOW)

    def test_folder_survives_kql_fold(self):
        # in:inbox + a filter term + a search term -> fold, but folder is kept.
        out = compile_query("in:inbox is:unread subject:invoice", "m365", now=NOW)
        assert out["folder"] == "inbox"
        assert out["filter"] is None
        assert out["search"] == "isread:false subject:invoice"

    def test_dict_folder_field_maps_to_well_known(self):
        out = compile_query({"folder": "sent", "unread": True}, "m365", now=NOW)
        assert out["folder"] == "sentitems"
        assert out["filter"] == "isRead eq false"
        assert out["search"] is None

    # ── System labels map to folders/flags, NOT categories ──────────────

    def test_system_label_inbox_maps_to_folder_not_category(self):
        out = compile_query("label:INBOX", "m365", now=NOW)
        assert out == {"folder": "inbox", "filter": None, "search": None}

    def test_system_label_is_case_insensitive(self):
        out = compile_query("label:sent", "m365", now=NOW)
        assert out == {"folder": "sentitems", "filter": None, "search": None}

    def test_system_label_unread_sets_unread_flag(self):
        out = compile_query("label:UNREAD", "m365", now=NOW)
        assert out == {"folder": None, "filter": "isRead eq false", "search": None}

    def test_system_label_starred_carried_as_search_with_warning(self):
        with pytest.warns(QueryTranslationWarning):
            out = compile_query("label:STARRED", "m365", now=NOW)
        assert out["folder"] is None
        assert out["filter"] is None
        assert out["search"] == "STARRED"

    def test_non_system_label_is_category_filter(self):
        out = compile_query("label:Clients", "m365", now=NOW)
        assert out == {
            "folder": None,
            "filter": "categories/any(c:c eq 'Clients')",
            "search": None,
        }

    def test_dict_tag_system_label_maps_to_folder(self):
        out = compile_query({"tag": "INBOX"}, "m365", now=NOW)
        assert out == {"folder": "inbox", "filter": None, "search": None}

    def test_folder_conflict_keeps_first_and_warns(self):
        # in:inbox (folder inbox) + label:SENT (system -> sentitems) conflict.
        with pytest.warns(QueryTranslationWarning):
            out = compile_query("in:inbox label:SENT", "m365", now=NOW)
        assert out["folder"] == "inbox"


def test_unknown_dialect_raises():
    with pytest.raises(ValueError):
        compile_query({"unread": True}, "imap")
