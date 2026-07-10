#!/usr/bin/env python3
"""v0.3.3 — M365 permission-specific error diagnosis.

Covers the ``_permission_hint`` mapping (pure helper) exhaustively plus a few
end-to-end fakes proving the hint is APPENDED to the ``Graph API {status}:
{message}`` text and rides along every failure surface:

  * reads that warn + return ``[]`` (the warning carries the hint),
  * ``_request`` that raises (health-check path),
  * guarded writes that convert the RuntimeError into an audited-failure
    ActionResult (the ActionResult error carries the hint).

Same posture as tests/test_m365_graph_v031.py / test_m365_hardening_v032.py —
no network, no msal; a scripted ``_send`` drives the real control flow.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


# ── Fixtures / fakes ───────────────────────────────────────────────────

@pytest.fixture
def m365_config():
    return {
        "integrations": {"workspace": {"provider": "m365"}},
        "m365": {
            "tenant_id": "tenant-guid",
            "client_id": "client-guid",
            "client_secret_env": "M365_CLIENT_SECRET",
            "auth": "client_credentials",
            "user_principal": "cos@acme.com",
        },
        "paths": {"project_root": "/tmp/test-m365-diagnostics"},
    }


@pytest.fixture
def client(m365_config):
    from providers.m365_graph import M365GraphClient
    c = M365GraphClient(m365_config)
    c._get_token = MagicMock(return_value="fake-token")
    c._slept: list[float] = []
    c._sleep = lambda s: c._slept.append(s)
    return c


@pytest.fixture
def approve_env():
    keys = ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE",
            "CHIEF_OF_STAFF_PROJECT_ROOT")
    old = {k: os.environ.get(k) for k in keys}
    os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
    os.environ["CHIEF_OF_STAFF_PROJECT_ROOT"] = tempfile.mkdtemp()
    os.environ.pop("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", None)
    yield
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class FakeResp:
    def __init__(self, status, *, json_body=None, headers=None, text=""):
        self.status_code = status
        self._json = {} if json_body is None else json_body
        self.headers = headers or {}
        self.text = text
        self.content = b'{"_": 1}'

    def json(self):
        return self._json


def scripted_send(responses):
    seq = list(responses)

    def _send(method, url, **kwargs):
        _send.calls.append((method, url, kwargs))
        return seq.pop(0)

    _send.calls = []
    return _send


ADMIN_CONSENT = "Grant admin consent"
CAL_HINT = "Calendars.ReadWrite"
FILES_HINT = "Files.ReadWrite.All"
USER_HINT = "m365.user_principal may be incorrect"
CRED_HINT = "credentials rejected"


def _err(code, message="denied"):
    return {"error": {"code": code, "message": message}}


# ── Unit: _permission_hint exhaustive ──────────────────────────────────

class TestPermissionHintUnit:
    def test_403_messages_path_is_mail_hint(self):
        from providers.m365_graph import M365GraphClient as C
        h = C._permission_hint(403, "/users/cos@acme.com/messages", None)
        assert h is not None
        assert "Mail.Read/Mail.ReadWrite" in h
        assert ADMIN_CONSENT in h

    def test_403_mailfolders_path_is_mail_hint(self):
        from providers.m365_graph import M365GraphClient as C
        h = C._permission_hint(403, "/users/x/mailFolders/inbox/messages", None)
        assert h is not None and ADMIN_CONSENT in h

    def test_403_mailfolders_only_still_matches(self):
        from providers.m365_graph import M365GraphClient as C
        # A mailFolders path without a trailing /messages still maps to mail.
        h = C._permission_hint(403, "/users/x/mailFolders/archive", None)
        assert h is not None and "Mail.Read" in h

    def test_404_users_path_is_user_hint(self):
        from providers.m365_graph import M365GraphClient as C
        h = C._permission_hint(404, "/users/wrong@acme.com", None)
        assert h is not None and USER_HINT in h
        assert "User.Read.All" in h

    def test_403_drive_path_is_files_hint(self):
        from providers.m365_graph import M365GraphClient as C
        h = C._permission_hint(403, "/users/x/drive/root:/f:/content", None)
        assert h is not None and FILES_HINT in h
        assert "OneDrive" in h

    def test_403_calendarview_is_calendar_hint(self):
        from providers.m365_graph import M365GraphClient as C
        h = C._permission_hint(403, "/users/x/calendarView", None)
        assert h is not None and CAL_HINT in h

    def test_403_events_is_calendar_hint(self):
        from providers.m365_graph import M365GraphClient as C
        h = C._permission_hint(403, "/users/x/events", None)
        assert h is not None and CAL_HINT in h

    def test_erroraccessdenied_code_on_events_without_403(self):
        from providers.m365_graph import M365GraphClient as C
        # Even with a non-403 status, ErrorAccessDenied on a calendar path maps.
        h = C._permission_hint(500, "/users/x/events", "ErrorAccessDenied")
        assert h is not None and CAL_HINT in h

    def test_erroraccessdenied_case_insensitive(self):
        from providers.m365_graph import M365GraphClient as C
        h = C._permission_hint(500, "/users/x/calendarView", "erroraccessdenied")
        assert h is not None and CAL_HINT in h

    def test_401_default_secret_env(self):
        from providers.m365_graph import M365GraphClient as C
        h = C._permission_hint(401, "/users/x/messages", None)
        assert h is not None and CRED_HINT in h
        assert "M365_CLIENT_SECRET" in h

    def test_401_custom_secret_env(self):
        from providers.m365_graph import M365GraphClient as C
        h = C._permission_hint(401, "/anything", None, secret_env="ACME_SECRET")
        assert h is not None and "ACME_SECRET" in h

    def test_path_matching_is_case_insensitive(self):
        from providers.m365_graph import M365GraphClient as C
        h = C._permission_hint(403, "/USERS/X/MESSAGES", None)
        assert h is not None and "Mail.Read" in h

    @pytest.mark.parametrize("status,path,code", [
        (403, "/users/x/outlook/masterCategories", None),  # unknown 403 surface
        (500, "/users/x/messages", None),                  # not a 403
        (404, "/users/x/messages", None),                  # 404 but not /users/-only rule? still matches user
        (429, "/users/x/drive", None),                     # throttle, no hint
        (403, "", None),                                    # empty path
    ])
    def test_none_or_specific_cases(self, status, path, code):
        from providers.m365_graph import M365GraphClient as C
        h = C._permission_hint(status, path, code)
        # 404 on /users/... always yields the user hint (documented behaviour);
        # everything else in this table has no specific mapping.
        if status == 404 and "/users/" in path:
            assert h is not None and USER_HINT in h
        else:
            assert h is None

    def test_403_unknown_path_returns_none(self):
        from providers.m365_graph import M365GraphClient as C
        assert C._permission_hint(403, "/users/x/outlook/masterCategories", None) is None


# ── E2E: hint appended across raise / warn / guarded surfaces ──────────

class TestEndToEnd:
    def test_403_mail_read_warning_carries_admin_consent_hint(self, client):
        send = scripted_send([FakeResp(403, json_body=_err("ErrorAccessDenied",
                                                           "Access is denied"))])
        client._send = send
        with pytest.warns(UserWarning) as rec:
            out = client.mail_search("is:unread")
        assert out == []
        msg = "\n".join(str(w.message) for w in rec)
        assert "Graph API 403" in msg          # base message preserved
        assert ADMIN_CONSENT in msg            # hint appended
        assert "Mail.Read/Mail.ReadWrite" in msg

    def test_404_user_healthcheck_path_raises_user_hint(self, client):
        # Drive the health-check path (/users/{upn}) directly through _request so
        # we can observe the raised RuntimeError before health_check swallows it.
        send = scripted_send([FakeResp(404, json_body=_err("ErrorItemNotFound",
                                                           "not found"))])
        client._send = send
        with pytest.raises(RuntimeError) as exc:
            client._request("GET", client._user_base(), params={"$select": "id"})
        text = str(exc.value)
        assert "Graph API 404" in text
        assert USER_HINT in text
        # health_check itself still just returns False (swallows the error).
        client._send = scripted_send([FakeResp(404, json_body=_err("x", "y"))])
        assert client.health_check() is False

    def test_guarded_write_403_calendar_is_audited_failure_with_hint(self, client, approve_env):
        # POST /events 403 via the guarded calendar_create path -> audited-failure
        # ActionResult whose error carries the calendar permission hint.
        send = scripted_send([FakeResp(403, json_body=_err("ErrorAccessDenied",
                                                          "denied"))])
        client._send = send
        result = client.calendar_create("Sync", "2026-07-10", "2026-07-10")
        assert result["success"] is False
        assert result["audited"] is True
        assert "Graph API 403" in result["error"]
        assert CAL_HINT in result["error"]

    def test_401_after_refresh_raises_credentials_hint(self, client):
        # Two 401s: the first triggers the single token refresh, the second falls
        # through to the raise path -> credentials hint naming the secret env var.
        send = scripted_send([
            FakeResp(401, json_body=_err("InvalidAuthenticationToken", "expired")),
            FakeResp(401, json_body=_err("InvalidAuthenticationToken", "expired")),
        ])
        client._send = send
        with pytest.raises(RuntimeError) as exc:
            client._request("GET", "/users/cos@acme.com/messages")
        text = str(exc.value)
        assert "Graph API 401" in text
        assert CRED_HINT in text
        assert "M365_CLIENT_SECRET" in text

    def test_guarded_files_403_carries_files_hint(self, client, approve_env):
        # files_upload PUT -> 403 on a /drive path -> Files.ReadWrite.All hint.
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(b"hi")
            fpath = tf.name
        try:
            send = scripted_send([FakeResp(403, json_body=_err("accessDenied", "no"))])
            client._send = send
            result = client.files_upload(fpath)
        finally:
            os.unlink(fpath)
        assert result["success"] is False
        assert FILES_HINT in result["error"]
