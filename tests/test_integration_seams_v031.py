"""v0.3.1 cross-seam integration tests.

The v0.3.1 refactor introduced two contracts built independently:
- shared/scripts/schemas.py: canonical workspace record shapes (message/event/file)
  and the --input envelope consumed by the aggregate skills.
- shared/scripts/providers/m365_graph.py: Microsoft Graph responses normalized
  to those same shapes.

These tests pin the seam: whatever the M365 provider emits must validate
against the schemas contract, so agent-fetched data and provider-fetched data
stay interchangeable in the compute pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT / "shared" / "scripts", REPO_ROOT / "shared" / "scripts" / "providers"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import schemas  # noqa: E402
from m365_graph import M365GraphClient  # noqa: E402


GRAPH_MESSAGE = {
    "id": "AAMkAG=",
    "conversationId": "conv1",
    "subject": "Invoice due",
    "from": {"emailAddress": {"address": "billing@acme.com", "name": "Acme"}},
    "receivedDateTime": "2026-07-09T08:15:00Z",
    "bodyPreview": "Please pay...",
    "categories": ["AR"],
    "hasAttachments": True,
    "webLink": "https://outlook.office365.com/x",
}

GRAPH_EVENT = {
    "id": "evt1",
    "subject": "Board sync",
    "start": {"dateTime": "2026-07-10T09:00:00.0000000", "timeZone": "UTC"},
    "end": {"dateTime": "2026-07-10T10:00:00.0000000", "timeZone": "UTC"},
    "attendees": [{"emailAddress": {"address": "a@x.com"}}],
    "organizer": {"emailAddress": {"address": "b@x.com"}},
    "location": {"displayName": "Teams"},
    "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/j"},
    "showAs": "busy",
}

GRAPH_FILE = {
    "id": "f1",
    "name": "NDA.docx",
    "file": {"mimeType": "application/vnd.openxmlformats"},
    "lastModifiedDateTime": "2026-07-01T00:00:00Z",
    "webUrl": "https://onedrive/x",
    "parentReference": {"id": "p1"},
}


class TestM365NormalizationMatchesSchemas:
    def test_message_conforms(self):
        rec = M365GraphClient._normalize_message(GRAPH_MESSAGE)
        schemas.validate_message(rec)  # raises SchemaError on drift
        assert rec["sender"] == "billing@acme.com"
        assert rec["source"] == "outlook"
        assert rec["tags"] == ["AR"]

    def test_event_conforms(self):
        rec = M365GraphClient._normalize_event(GRAPH_EVENT)
        schemas.validate_event(rec)
        assert rec["conference_link"] == "https://teams.microsoft.com/j"
        assert rec["attendees"] == ["a@x.com"]

    def test_file_conforms(self):
        rec = M365GraphClient._normalize_file(GRAPH_FILE)
        schemas.validate_file(rec)
        assert rec["source"] == "onedrive"
        assert rec["parents"] == ["p1"]

    def test_envelope_of_normalized_records_conforms(self):
        payload = {
            "source": "outlook",
            "messages": [M365GraphClient._normalize_message(GRAPH_MESSAGE)],
            "events": [M365GraphClient._normalize_event(GRAPH_EVENT)],
            "files": [M365GraphClient._normalize_file(GRAPH_FILE)],
        }
        schemas.validate_workspace_payload(payload)

    def test_sparse_graph_records_still_conform(self):
        """Graph objects with minimal fields must still normalize to valid records."""
        msg = M365GraphClient._normalize_message(
            {"id": "m1", "receivedDateTime": "2026-07-09T00:00:00Z"}  # no from, no subject
        )
        schemas.validate_message(msg)
        assert msg["sender"] == "unknown"
        assert msg["subject"] == "(no subject)"
        evt = M365GraphClient._normalize_event(
            {
                "id": "e1",
                "subject": "t",
                "start": {"dateTime": "2026-07-10T09:00:00"},
                "end": {"dateTime": "2026-07-10T10:00:00"},
            }
        )
        schemas.validate_event(evt)
        fil = M365GraphClient._normalize_file({"id": "f1", "name": "n"})
        schemas.validate_file(fil)


class TestNeutralGuardrailClassification:
    """The neutral action ids must gate exactly like their legacy twins."""

    def test_neutral_ids_classified(self):
        import workspace_guardrails as wg

        assert "mail.send" in wg.WRITE_ACTIONS
        assert "mail.send" in wg.DESTRUCTIVE_ACTIONS
        assert "mail.draft" in wg.SAFE_WRITE_ACTIONS
        assert "files.upload" in wg.SAFE_WRITE_ACTIONS
        assert "files.download" in wg.SAFE_WRITE_ACTIONS

    def test_neutral_mirrors_legacy(self):
        import workspace_guardrails as wg

        for legacy, neutral in (
            ("gmail.send", "mail.send"),
            ("gmail.draft", "mail.draft"),
            ("drive.upload", "files.upload"),
            ("drive.download", "files.download"),
        ):
            assert (legacy in wg.DESTRUCTIVE_ACTIONS) == (neutral in wg.DESTRUCTIVE_ACTIONS), (legacy, neutral)
            assert (legacy in wg.SAFE_WRITE_ACTIONS) == (neutral in wg.SAFE_WRITE_ACTIONS), (legacy, neutral)
            assert (legacy in wg.WRITE_ACTIONS) == (neutral in wg.WRITE_ACTIONS), (legacy, neutral)

    def test_every_m365_mutating_id_is_gated(self):
        """Every neutral action id emitted by an m365 @guarded method (or the
        m365 calendar_cancel gate) MUST be a WRITE_ACTION, else confirm_action()
        (which permits anything NOT in WRITE_ACTIONS) would let it run ungated.

        This mirrors the exact set of ids used in providers/m365_graph.py.
        """
        import workspace_guardrails as wg

        # id -> expected classification: "destructive" or "safe".
        m365_mutations = {
            "mail.draft": "safe",
            "mail.send": "destructive",
            "mail.archive": "safe",
            "mail.unarchive": "safe",
            "mail.trash": "safe",
            "mail.untrash": "safe",
            "mail.tag": "safe",
            "mail.create_tag": "safe",
            "calendar.create": "safe",
            "calendar.update": "safe",   # WRITE but not SAFE-auto nor destructive
            "calendar.cancel": "safe",
            "files.upload": "safe",
            "files.download": "safe",
            "files.trash": "safe",
        }
        for action_id, kind in m365_mutations.items():
            assert action_id in wg.WRITE_ACTIONS, f"{action_id} must be a WRITE_ACTION"
            if kind == "destructive":
                assert action_id in wg.DESTRUCTIVE_ACTIONS, action_id
                assert action_id not in wg.SAFE_WRITE_ACTIONS, action_id
            else:
                assert action_id not in wg.DESTRUCTIVE_ACTIONS, action_id

        # The reversible-by-design mutations are explicitly SAFE_WRITE so they
        # gate behind auto-approve, never behind CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE.
        for reversible in ("mail.archive", "mail.unarchive", "mail.trash",
                           "mail.untrash", "mail.tag", "mail.create_tag",
                           "calendar.cancel", "files.trash"):
            assert reversible in wg.SAFE_WRITE_ACTIONS, reversible

    def test_legacy_soft_delete_ids_are_gated(self):
        """Legacy Google/Composio archive/trash spellings must be WRITE_ACTIONS.

        Previously they were left out so confirm_action() default-allowed them.
        They are now gated the same way as their neutral twins.
        """
        import workspace_guardrails as wg

        for legacy in ("gmail.archive", "gmail.trash", "drive.trash"):
            assert legacy in wg.WRITE_ACTIONS, legacy
            assert legacy in wg.SAFE_WRITE_ACTIONS, legacy
