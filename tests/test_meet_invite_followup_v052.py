#!/usr/bin/env python3
"""Meet-invite follow-up email on calendar.create (v0.5.2).

Contract under test: the google_api provider's calendar_create must, when
attendees are present:
  1. insert the event via Calendar REST with conferenceDataVersion=1 so a
     Google Meet link is generated (google_api.py CLI cannot do this);
  2. send a follow-up Gmail invite containing the Meet link to every
     attendee, because service-account-created events do NOT reliably
     generate Google's own invitation email (verified live 2026-08-29);
  3. return the hangoutLink and invite-email id in the result;
  4. skip the follow-up email when the event has no attendees;
  5. propagate failure. If the follow-up email fails after the event was
     created, the result reports partial success with the error, not a
     silent drop.

These are orchestrator-written contract tests. Builders must NOT modify
this file.
"""

import base64
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


class _FakeCreds:
    token = "fake-token"


@pytest.fixture
def google_config():
    return {
        "google": {
            "service_account_path": "~/.hermes/test.json",
            "domain": "test.com",
            "delegate_email": "founder@test.com",
            "account_alias": "",
        },
        "integrations": {"workspace": {"provider": "google_api", "mode": "direct"}},
    }


@pytest.fixture(autouse=True)
def _auto_approve(monkeypatch):
    """Guardrail confirmation needs a TTY; tests set AUTO_APPROVE like other suites."""
    monkeypatch.setenv("CHIEF_OF_STAFF_AUTO_APPROVE", "1")


@pytest.fixture
def client(google_config):
    from providers.google_workspace import GoogleWorkspaceClient

    with patch(
        "providers.google_workspace._find_google_api_script",
        return_value=Path("/fake/google_api.py"),
    ):
        return GoogleWorkspaceClient(google_config)


def _post_registry(insert_response, send_response):
    """Callable recording REST POSTs; dispatches per-URL responses."""
    calls = []

    def _post(url, headers=None, params=None, json=None, timeout=None):
        calls.append({"url": url, "params": params or {}, "json": json or {}})
        if url.startswith(CALENDAR_EVENTS_URL):
            resp = MagicMock(status_code=200, text="")
            resp.json.return_value = insert_response
            return resp
        if url == GMAIL_SEND_URL:
            resp = MagicMock(status_code=200, text="")
            resp.json.return_value = send_response
            return resp
        raise AssertionError(f"unexpected URL: {url}")

    return calls, _post


def _decode_raw(raw: str) -> str:
    return base64.urlsafe_b64decode(raw + "===").decode("utf-8")


class TestMeetInviteFollowUp:
    def test_create_with_attendees_generates_meet_link_and_sends_invite(
        self, client
    ):
        """calendar.create + attendees => Meet link generated AND invite
        email sent even though Google's own invite email is unreliable
        under service-account delegation."""
        insert_response = {
            "id": "evt_123",
            "htmlLink": "https://calendar.google.com/evt_123",
            "hangoutLink": "https://meet.google.com/abc-defg-hij",
            "conferenceData": {"conferenceId": "abc-defg-hij"},
        }
        send_response = {"id": "msg_789", "threadId": "msg_789", "labelIds": ["SENT"]}
        calls, poster = _post_registry(insert_response, send_response)

        with patch(
            "providers.google_workspace._sa_credentials", return_value=_FakeCreds()
        ), patch("requests.post", side_effect=poster):
            result = client.calendar_create(
                "Hermes handover",
                "2026-08-29T10:30:00+08:00",
                "2026-08-29T11:30:00+08:00",
                attendees=["cliftonteo@example.com"],
            )

        insert_calls = [c for c in calls if c["url"].startswith(CALENDAR_EVENTS_URL)]
        assert len(insert_calls) == 1
        body = insert_calls[0]["json"]
        assert body["summary"] == "Hermes handover"
        assert body["attendees"] == [{"email": "cliftonteo@example.com"}]
        assert (
            body["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"]
            == "hangoutsMeet"
        )
        assert insert_calls[0]["params"].get("conferenceDataVersion") == 1

        send_calls = [c for c in calls if c["url"] == GMAIL_SEND_URL]
        assert len(send_calls) == 1
        raw = send_calls[0]["json"]["raw"]  # messages.send takes the Message resource directly
        mime_text = _decode_raw(raw)
        assert "https://meet.google.com/abc-defg-hij" in mime_text
        assert "cliftonteo@example.com" in mime_text
        assert "2026-08-29T10:30" in mime_text

        # Result surfaces the link + email id
        data = result.get("data") or result
        assert data.get("hangoutLink") == "https://meet.google.com/abc-defg-hij"

    def test_create_without_attendees_skips_follow_up_email(self, client):
        insert_response = {
            "id": "evt_solo",
            "hangoutLink": "https://meet.google.com/solo-lonely-1",
        }
        calls, poster = _post_registry(insert_response, {})
        with patch(
            "providers.google_workspace._sa_credentials", return_value=_FakeCreds()
        ), patch("requests.post", side_effect=poster):
            result = client.calendar_create(
                "Focus block",
                "2026-08-30T09:00:00+08:00",
                "2026-08-30T10:00:00+08:00",
                attendees=None,
            )

        assert [c["url"] for c in calls if c["url"] == GMAIL_SEND_URL] == []
        data = result.get("data") or result
        assert data.get("hangoutLink") == "https://meet.google.com/solo-lonely-1"

    def test_invite_email_failure_reports_partial_success_not_crdash(self, client):
        """Event created but Gmail send 500 → result must surface the
        partial state, not raise away the created event id/link.
        """
        insert_response = {
            "id": "evt_part",
            "hangoutLink": "https://meet.google.com/part-ial-tie",
        }

        calls = []

        def poster(url, headers=None, params=None, json=None, timeout=None):
            calls.append(url)
            if url.startswith(CALENDAR_EVENTS_URL):
                resp = MagicMock(status_code=200, text="")
                resp.json.return_value = insert_response
                return resp
            resp = MagicMock(status_code=500, text="backend error")
            return resp

        with patch(
            "providers.google_workspace._sa_credentials", return_value=_FakeCreds()
        ), patch("requests.post", side_effect=poster):
            result = client.calendar_create(
                "Partial", "2026-08-31T10:00:00+08:00", "2026-08-31T11:00:00+08:00",
                attendees=["a@example.com"],
            )

        assert result.get("success") is True  # event exists; email failed
        data = result.get("data") or result
        assert data.get("hangoutLink") == "https://meet.google.com/part-ial-tie"
        err = (result.get("error") or "")
        assert "invite" in err.lower() or "email" in err.lower()

    def test_create_failure_propagates_error(self, client):
        calls, poster = _post_registry({}, {})

        def failing_post(url, headers=None, params=None, json=None, timeout=None):
            resp = MagicMock(status_code=403, text="Forbidden")
            return resp

        with patch(
            "providers.google_workspace._sa_credentials", return_value=_FakeCreds()
        ), patch("requests.post", side_effect=failing_post):
            with pytest.raises(RuntimeError) as excinfo:
                client.calendar_create(
                    "Denied", "2026-08-31T10:00:00+08:00",
                    "2026-08-31T11:00:00+08:00",
                    attendees=["a@example.com"],
                )
        assert "403" in str(excinfo.value)

    def test_invite_email_contains_event_and_meet_details(self, client):
        """The follow-up email is a usable standalone invite: title, time,
        Meet link present in the decoded MIME body."""
        insert_response = {
            "id": "evt_body",
            "hangoutLink": "https://meet.google.com/bdy-link-x1",
        }
        send_response = {"id": "msg_body"}
        calls, poster = _post_registry(insert_response, send_response)
        with patch(
            "providers.google_workspace._sa_credentials", return_value=_FakeCreds()
        ), patch("requests.post", side_effect=poster):
            client.calendar_create(
                "Spec review",
                "2026-09-01T14:00:00+08:00",
                "2026-09-01T15:00:00+08:00",
                attendees=["x@example.com", "y@example.com"],
            )
        send_calls = [c for c in calls if c["url"] == GMAIL_SEND_URL]
        assert len(send_calls) == 1
        raw = send_calls[0]["json"]["raw"]  # messages.send takes the Message resource directly
        body = _decode_raw(raw)
        assert "Spec review" in body
        assert "https://meet.google.com/bdy-link-x1" in body
        assert "2026-09-01T14:00" in body or "2026-09-01 14:00" in body or "14:00" in body