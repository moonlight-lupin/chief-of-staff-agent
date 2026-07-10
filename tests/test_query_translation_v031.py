#!/usr/bin/env python3
"""Bundled-query translation coverage for query_compiler.

Proves that every Gmail-syntax template in queries.yaml.example — and the
hardcoded runtime literals — translate to non-empty, correct Microsoft 365
Graph queries, and that no untranslatable token is dropped silently.
"""
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

from query_compiler import (  # noqa: E402
    QueryTranslationWarning,
    compile_query,
    parse_gmail_query,
)

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)

QUERIES_YAML = PLUGIN_ROOT / "shared" / "config" / "queries.yaml.example"

# Dummy values for the template placeholders that appear in the bundle.
PLACEHOLDERS = {
    "{client_name}": "Acme",
    "{delegate_email}": "delegate@example.com",
    "{contact_email}": "contact@example.com",
    "{domain}": "example.com",
    "{days}": "7",
    "{invoice_id}": "INV-1",
}


def _render(query: str) -> str:
    for key, val in PLACEHOLDERS.items():
        query = query.replace(key, val)
    return query


def _load_templates() -> list[dict]:
    data = yaml.safe_load(QUERIES_YAML.read_text())
    return list(data.get("queries", []))


def _rendered_by_name() -> dict[str, str]:
    return {q["name"]: _render(q["query"]) for q in _load_templates()}


# ── Every bundled template compiles for both dialects ──────────────────

class TestBundleCoverage:
    def test_yaml_loads_and_has_queries(self):
        templates = _load_templates()
        assert len(templates) >= 8
        assert all("query" in q and "name" in q for q in templates)

    @pytest.mark.parametrize("tmpl", _load_templates(), ids=lambda t: t["name"])
    def test_gmail_dialect_is_passthrough(self, tmpl):
        rendered = _render(tmpl["query"])
        # For the gmail dialect a string passes through unchanged.
        assert compile_query(rendered, "gmail", now=NOW) == rendered

    @pytest.mark.parametrize("tmpl", _load_templates(), ids=lambda t: t["name"])
    def test_m365_dialect_never_empty(self, tmpl):
        rendered = _render(tmpl["query"])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", QueryTranslationWarning)
            out = compile_query(rendered, "m365", now=NOW)
        assert isinstance(out, dict)
        assert out.get("filter") or out.get("search"), (
            f"{tmpl['name']} translated to an EMPTY m365 query: {out}"
        )


# ── Exact m365 output for a representative sample ──────────────────────

class TestM365ExactTranslation:
    """The task names briefing_unread_priority / documents_for_signature /
    invoices_received; those names are not present in this bundle, so we assert
    the equivalent representative templates that exercise the same operators:
    top-level OR group, subject:(A OR B), newer_than, subject word + free text,
    and an all-$filter (no fold) case."""

    def setup_method(self):
        self.q = _rendered_by_name()

    def test_unread_priority_top_level_or_group_folds(self):
        # in:inbox is:unread newer_than:14d ("urgent" OR ... OR "deadline")
        out = compile_query(self.q["unread_priority"], "m365", now=NOW)
        assert out["filter"] is None
        assert out["search"] == (
            "isread:false received>=2026-06-26 "
            '(urgent OR approval OR "please review" OR '
            '"action required" OR deadline)'
        )

    def test_engagement_threads_subject_or_group_folds(self):
        # subject:(proposal OR NDA OR ... OR "statement of work") newer_than:14d
        out = compile_query(self.q["engagement_threads"], "m365", now=NOW)
        assert out["filter"] is None
        assert out["search"] == (
            "received>=2026-06-26 "
            "(subject:proposal OR subject:NDA OR subject:SOW OR "
            "subject:invoice OR subject:contract OR subject:engagement OR "
            'subject:"statement of work")'
        )

    def test_invoice_payment_subject_word_plus_text_folds(self):
        # subject:invoice Acme newer_than:30d
        out = compile_query(self.q["invoice_payment"], "m365", now=NOW)
        assert out["filter"] is None
        assert out["search"] == "received>=2026-06-10 subject:invoice Acme"

    def test_recent_unread_all_filter_no_fold(self):
        # is:unread newer_than:3d label:INBOX -> pure $filter, no $search
        out = compile_query(self.q["recent_unread"], "m365", now=NOW)
        assert out["search"] is None
        assert out["filter"] == (
            "isRead eq false and receivedDateTime ge 2026-07-07T12:00:00Z "
            "and categories/any(c:c eq 'INBOX')"
        )

    def test_client_documents_has_attachment_and_newer(self):
        out = compile_query(self.q["client_documents"], "m365", now=NOW)
        assert out["search"] is None
        assert out["filter"] == (
            "receivedDateTime ge 2026-07-03T12:00:00Z and hasAttachments eq true"
        )

    def test_acra_iras_from_or_group_folds(self):
        out = compile_query(self.q["acra_iras"], "m365", now=NOW)
        assert out["filter"] is None
        assert out["search"] == (
            "(from:@acra.gov.sg OR from:@iras.gov.sg OR from:@bizfile.gov.sg) "
            "received>=2026-06-10"
        )

    def test_unread_business_to_and_unread_folds(self):
        out = compile_query(self.q["unread_business"], "m365", now=NOW)
        assert out["filter"] is None
        assert out["search"] == "isread:false to:delegate@example.com"

    def test_sent_followup_from_and_newer_all_filter(self):
        out = compile_query(self.q["sent_followup"], "m365", now=NOW)
        assert out["search"] is None
        assert out["filter"] == (
            "from/emailAddress/address eq 'delegate@example.com' "
            "and receivedDateTime ge 2026-07-03T12:00:00Z"
        )


# ── Runtime literals (inventoried from call sites) ─────────────────────

class TestRuntimeLiterals:
    """Literals passed to client.mail_search(...) across skills/ and shared/."""

    @pytest.mark.parametrize("literal", [
        "in:inbox",                 # email_organisation.py, poll_events.py
        "is:unread",                # workspace_collect.py default, connect_workspace
        "from:person@test.com",     # meeting-prep workspace_actions.py
        "from:external@client.com",
    ])
    def test_literal_compiles_non_empty_m365(self, literal):
        out = compile_query(literal, "m365", now=NOW)
        assert out.get("filter") or out.get("search"), literal

    def test_in_inbox_maps_to_folder_filter(self):
        assert compile_query("in:inbox", "m365", now=NOW) == {
            "filter": "parentFolderId eq 'inbox'",
            "search": None,
        }

    def test_from_literal_is_exact_address_filter(self):
        out = compile_query("from:person@test.com", "m365", now=NOW)
        assert out["filter"] == "from/emailAddress/address eq 'person@test.com'"
        assert out["search"] is None


# ── No silent emptiness / warnings policy ──────────────────────────────

class TestNoSilentEmptiness:
    def test_unknown_operator_warns_but_stays_usable(self):
        with pytest.warns(QueryTranslationWarning, match="weirdop:x"):
            out = compile_query("weirdop:x is:unread", "m365", now=NOW)
        # The unknown token is carried as free-text search, not dropped.
        assert out["search"] == "isread:false weirdop:x"

    def test_unknown_operator_alone_is_carried_as_search(self):
        with pytest.warns(QueryTranslationWarning):
            out = compile_query("weirdop:value", "m365", now=NOW)
        assert out["filter"] is None
        assert out["search"] == "weirdop:value"

    def test_unknown_is_value_warns(self):
        with pytest.warns(QueryTranslationWarning, match="is:starred"):
            out = compile_query("is:starred from:a@b.com", "m365", now=NOW)
        assert out.get("filter") or out.get("search")

    def test_empty_translation_raises_value_error(self):
        # in:anywhere carries no folder filter and nothing else -> empty.
        with pytest.raises(ValueError):
            compile_query("in:anywhere", "m365", now=NOW)

    def test_empty_string_is_allowed_empty(self):
        assert compile_query("", "m365", now=NOW) == {"filter": None, "search": None}

    def test_non_empty_never_returns_double_none(self):
        for tmpl in _load_templates():
            rendered = _render(tmpl["query"])
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", QueryTranslationWarning)
                out = compile_query(rendered, "m365", now=NOW)
            assert not (out["filter"] is None and out["search"] is None)


# ── Parser-level unit checks for operators in the bundle ───────────────

class TestParser:
    def test_duration_units(self):
        assert parse_gmail_query("newer_than:2w")["newer_than_days"] == 14
        assert parse_gmail_query("newer_than:3m")["newer_than_days"] == 90
        assert parse_gmail_query("newer_than:1y")["newer_than_days"] == 365
        assert parse_gmail_query("older_than:5d")["older_than_days"] == 5

    def test_is_read_unread(self):
        assert parse_gmail_query("is:unread").get("unread") is True
        assert parse_gmail_query("is:read").get("read") is True
        # negation flips the flag
        assert parse_gmail_query("-is:unread").get("read") is True

    def test_from_domain_vs_address(self):
        assert parse_gmail_query("from:@acme.com").get("domain") == "@acme.com"
        assert parse_gmail_query("from:a@acme.com").get("from_sender") == "a@acme.com"

    def test_subject_group_vs_word(self):
        assert parse_gmail_query("subject:invoice").get("subject_contains") == "invoice"
        assert parse_gmail_query(
            "subject:(a OR b)"
        ).get("subject_any") == ["a", "b"]

    def test_has_attachment_and_filename_and_label(self):
        m = parse_gmail_query("has:attachment filename:pdf label:Finance")
        assert m.get("has_attachment") is True
        assert m.get("filename") == "pdf"
        assert m.get("tag") == "Finance"

    def test_quoted_phrase_and_top_level_or_group(self):
        m = parse_gmail_query('"hello world" (a OR b)')
        assert m.get("text") == "hello world"
        assert m.get("any_terms") == ["a", "b"]

    def test_negation_of_operator_becomes_search_not(self):
        out = compile_query("is:unread -has:attachment", "m365", now=NOW)
        # unread(filter) + NOT(search) -> fold
        assert out["filter"] is None
        assert "NOT hasattachments:true" in out["search"]
