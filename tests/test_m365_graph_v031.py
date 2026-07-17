#!/usr/bin/env python3
"""Tests for M365GraphClient — fake transport, no network, no msal required."""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


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
        "paths": {"project_root": "/tmp/test-m365-workspace"},
    }


@pytest.fixture
def client(m365_config):
    from providers.m365_graph import M365GraphClient
    c = M365GraphClient(m365_config)
    # Never hit real auth in tests.
    c._get_token = MagicMock(return_value="fake-token")
    return c


@pytest.fixture
def approve_env():
    old = {k: os.environ.get(k) for k in
           ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE",
            "CHIEF_OF_STAFF_PROJECT_ROOT")}
    os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
    os.environ["CHIEF_OF_STAFF_PROJECT_ROOT"] = tempfile.mkdtemp()
    yield
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ── Factory ────────────────────────────────────────────────────────────

class TestFactory:
    def test_factory_resolves_m365(self, m365_config):
        from workspace_client import get_workspace_client
        from providers.m365_graph import M365GraphClient
        c = get_workspace_client(m365_config)
        assert isinstance(c, M365GraphClient)
        assert c.provider_name == "m365"


# ── Reads / normalisation ──────────────────────────────────────────────

class TestMailSearch:
    def test_mail_search_normalization(self, client):
        payload = {"value": [{
            "id": "AAMk123",
            "from": {"emailAddress": {"name": "Jane", "address": "jane@acme.com"}},
            "subject": "Q3 Invoice",
            "receivedDateTime": "2026-07-09T08:30:00Z",
            "conversationId": "conv-1",
            "bodyPreview": "Please find attached",
            "categories": ["Finance"],
            "hasAttachments": True,
            "webLink": "https://outlook.office365.com/mail/x",
        }]}
        with patch.object(client, "_request", return_value=payload) as req:
            out = client.mail_search({"unread": True}, max_results=5)
        assert len(out) == 1
        m = out[0]
        assert m == {
            "id": "AAMk123",
            "sender": "jane@acme.com",
            "subject": "Q3 Invoice",
            "date": "2026-07-09T08:30:00Z",
            "source": "outlook",
            "thread_id": "conv-1",
            "snippet": "Please find attached",
            "tags": ["Finance"],
            "has_attachments": True,
            "link": "https://outlook.office365.com/mail/x",
        }
        # $filter built from query model, $top passed
        _, kwargs = req.call_args
        assert kwargs["params"]["$top"] == 5
        assert kwargs["params"]["$filter"] == "isRead eq false"

    def test_mail_search_returns_empty_on_error(self, client):
        with patch.object(client, "_request", side_effect=RuntimeError("boom")):
            with pytest.warns(UserWarning):
                out = client.mail_search("is:unread")
        assert out == []

    def _record_request(self, client):
        """Patch _request to record (method, path, params) and return an empty
        Graph value payload. Returns the calls list."""
        calls: list[tuple] = []

        def _fake(method, path, **kwargs):
            calls.append((method, path, kwargs.get("params") or {}))
            return {"value": []}

        client._request = _fake
        return calls

    def test_mail_search_folder_scope_uses_mailfolders_path(self, client):
        # in:inbox -> folder scope applied via URL path, NOT a $filter.
        calls = self._record_request(client)
        client.mail_search("in:inbox is:unread")
        method, path, params = calls[-1]
        assert method == "GET"
        assert "/mailFolders/inbox/messages" in path
        # folder is out-of-band: no parentFolderId leaks into $filter.
        assert "parentFolderId" not in params.get("$filter", "")
        assert params.get("$filter") == "isRead eq false"

    def test_mail_search_no_folder_uses_plain_messages_path(self, client):
        calls = self._record_request(client)
        client.mail_search("is:unread")
        method, path, params = calls[-1]
        assert path.endswith("/messages")
        assert "mailFolders" not in path
        assert params.get("$filter") == "isRead eq false"

    def test_mail_search_system_label_inbox_uses_mailfolders_path(self, client):
        # label:INBOX is a Gmail SYSTEM label -> folder scope, not a category.
        calls = self._record_request(client)
        client.mail_search("label:INBOX")
        _, path, params = calls[-1]
        assert "/mailFolders/inbox/messages" in path
        assert "categories" not in params.get("$filter", "")

    def test_mail_search_non_system_label_stays_on_messages_with_category(self, client):
        # label:Clients is a NON-system label -> Outlook category filter, no folder.
        calls = self._record_request(client)
        client.mail_search("label:Clients")
        _, path, params = calls[-1]
        assert path.endswith("/messages")
        assert "mailFolders" not in path
        assert params.get("$filter") == "categories/any(c:c eq 'Clients')"


class TestCalendarList:
    def test_calendar_list_normalization_with_joinurl(self, client):
        payload = {"value": [{
            "id": "evt1",
            "subject": "Standup",
            "start": {"dateTime": "2026-07-10T09:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-07-10T09:30:00.0000000", "timeZone": "UTC"},
            "attendees": [{"emailAddress": {"address": "a@acme.com"}},
                          {"emailAddress": {"address": "b@acme.com"}}],
            "organizer": {"emailAddress": {"address": "boss@acme.com"}},
            "location": {"displayName": "Room 1"},
            "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup/xyz"},
            "isCancelled": False,
        }]}
        with patch.object(client, "_request", return_value=payload):
            out = client.calendar_list("2026-07-10", "2026-07-11")
        e = out[0]
        assert e["title"] == "Standup"
        assert e["start"] == "2026-07-10T09:00:00.0000000"
        assert e["attendees"] == ["a@acme.com", "b@acme.com"]
        assert e["organizer"] == "boss@acme.com"
        assert e["conference_link"] == "https://teams.microsoft.com/l/meetup/xyz"
        assert e["source"] == "outlook"
        assert e["status"] == "confirmed"

    def test_calendar_list_pads_bare_dates(self, client):
        with patch.object(client, "_request", return_value={"value": []}) as req:
            client.calendar_list("2026-07-10", "2026-07-11")
        _, kwargs = req.call_args
        assert kwargs["params"]["startDateTime"] == "2026-07-10T00:00:00Z"
        assert kwargs["params"]["endDateTime"] == "2026-07-11T23:59:59Z"


class TestFilesSearch:
    def test_files_search_normalization(self, client):
        payload = {"value": [{
            "id": "file1",
            "name": "NDA.pdf",
            "file": {"mimeType": "application/pdf"},
            "lastModifiedDateTime": "2026-07-01T10:00:00Z",
            "webUrl": "https://acme-my.sharepoint.com/x",
            "parentReference": {"id": "parent1"},
        }]}
        with patch.object(client, "_request", return_value=payload) as req:
            out = client.files_search("NDA", max_results=5)
        f = out[0]
        assert f == {
            "id": "file1", "name": "NDA.pdf", "source": "onedrive",
            "mime_type": "application/pdf", "modified": "2026-07-01T10:00:00Z",
            "link": "https://acme-my.sharepoint.com/x", "parents": ["parent1"],
        }
        args, _ = req.call_args
        assert "search(q='NDA')" in args[1]


# ── Guarded writes ─────────────────────────────────────────────────────

class TestGuardedWrites:
    def test_draft_success(self, client, approve_env):
        with patch.object(client, "_request", return_value={"id": "draft1"}) as req:
            result = client.mail_create_draft("to@x.com", "Hi", "Body", cc="cc@x.com")
        assert result["success"] is True
        assert result["action"] == "mail.draft"
        assert result["provider"] == "m365"
        assert result["audited"] is True
        assert result["data"]["id"] == "draft1"
        _, kwargs = req.call_args
        body = kwargs["json_body"]
        assert body["toRecipients"] == [{"emailAddress": {"address": "to@x.com"}}]
        assert body["ccRecipients"] == [{"emailAddress": {"address": "cc@x.com"}}]

    def test_send_blocked_without_allow_destructive(self, client, approve_env):
        # AUTO_APPROVE set, ALLOW_DESTRUCTIVE not set -> blocked, body never runs.
        with patch.object(client, "_request") as req:
            result = client.mail_send("a@b.com", "S", "B")
        assert result["success"] is False
        assert result["action"] == "mail.send"
        assert "destructive" in result["error"].lower() or "guardrail" in result["error"].lower()
        req.assert_not_called()

    def test_send_proceeds_with_allow_destructive(self, client, approve_env):
        os.environ["CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"] = "1"
        try:
            with patch.object(client, "_request", return_value={}) as req:
                result = client.mail_send("a@b.com", "S", "B")
            assert result["success"] is True
            assert result["action"] == "mail.send"
            assert result["audited"] is True
            args, kwargs = req.call_args
            assert args[1].endswith("/sendMail")
            assert kwargs["json_body"]["saveToSentItems"] is True
        finally:
            os.environ.pop("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", None)

    def test_http_error_becomes_audited_failure(self, client, approve_env):
        with patch.object(client, "_request", side_effect=RuntimeError("Graph API 404: not found")):
            result = client.calendar_create("Sync", "2026-07-10", "2026-07-10")
        assert result["success"] is False
        assert result["audited"] is True
        assert "404" in result["error"]

    def test_archive_move_destination(self, client, approve_env):
        with patch.object(client, "_request", return_value={"id": "m1"}) as req:
            result = client.mail_archive("m1")
        assert result["success"] is True
        args, kwargs = req.call_args
        assert args[1] == "/users/cos@acme.com/messages/m1/move"
        assert kwargs["json_body"]["destinationId"] == "archive"

    def test_trash_move_destination(self, client, approve_env):
        with patch.object(client, "_request", return_value={"id": "m1"}) as req:
            client.mail_trash("m1")
        _, kwargs = req.call_args
        assert kwargs["json_body"]["destinationId"] == "deleteditems"

    def test_mail_tag_appends_categories(self, client, approve_env):
        # First GET returns existing categories, then PATCH.
        responses = [{"categories": ["Existing"]}, {}]
        with patch.object(client, "_request", side_effect=responses) as req:
            result = client.mail_tag("m1", "Legal")
        assert result["success"] is True
        assert result["data"]["categories"] == ["Existing", "Legal"]
        patch_call = req.call_args_list[-1]
        assert patch_call.args[0] == "PATCH"
        assert patch_call.kwargs["json_body"]["categories"] == ["Existing", "Legal"]

    def test_files_upload_path_construction(self, client, approve_env):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(b"hello")
            fpath = tf.name
        try:
            with patch.object(client, "_request", return_value={"id": "up1"}) as req:
                client.files_upload(fpath, parent_id="folderA")
            args, _ = req.call_args
            name = Path(fpath).name
            assert args[1] == f"/users/cos@acme.com/drive/items/folderA:/{name}:/content"

            with patch.object(client, "_request", return_value={"id": "up2"}) as req2:
                client.files_upload(fpath)
            args2, _ = req2.call_args
            assert args2[1] == f"/users/cos@acme.com/drive/root:/{name}:/content"
        finally:
            os.unlink(fpath)


# ── Immutable ids + restore_target (blocker #3) ────────────────────────

class TestImmutableIdHeader:
    def test_request_sends_immutable_id_prefer_header(self, client):
        """Every Graph request must carry Prefer: IdType="ImmutableId" so ids
        stay stable across archive/trash moves (restore-flow correctness)."""
        import providers.m365_graph as m

        captured: dict = {}

        class _FakeResp:
            status_code = 200
            content = b'{"ok": true}'

            def json(self):
                return {"ok": True}

        def _fake_request(method, url, **kwargs):
            captured["headers"] = kwargs.get("headers") or {}
            return _FakeResp()

        with patch.object(m.requests, "request", side_effect=_fake_request):
            client._request("GET", "/users/cos@acme.com/messages")
        assert captured["headers"].get("Prefer") == 'IdType="ImmutableId"'

    def test_extra_headers_do_not_drop_prefer(self, client):
        import providers.m365_graph as m

        captured: dict = {}

        class _FakeResp:
            status_code = 200
            content = b"{}"

            def json(self):
                return {}

        def _fake_request(method, url, **kwargs):
            captured["headers"] = kwargs.get("headers") or {}
            return _FakeResp()

        with patch.object(m.requests, "request", side_effect=_fake_request):
            client._request("PUT", "/x", headers={"Content-Type": "application/octet-stream"})
        assert captured["headers"].get("Prefer") == 'IdType="ImmutableId"'
        assert captured["headers"].get("Content-Type") == "application/octet-stream"


class TestRestoreTarget:
    def test_archive_returns_restore_target(self, client, approve_env):
        # Post-move id differs from the original target; restore_target carries it.
        with patch.object(client, "_request", return_value={"id": "moved-1"}):
            result = client.mail_archive("orig-1")
        assert result["success"] is True
        assert result["data"]["id"] == "moved-1"
        assert result["data"]["restore_target"] == "moved-1"

    def test_trash_returns_restore_target(self, client, approve_env):
        with patch.object(client, "_request", return_value={"id": "moved-2"}):
            result = client.mail_trash("orig-2")
        assert result["data"]["restore_target"] == "moved-2"
        assert result["data"]["reversible"] is True

    def test_archive_restore_target_falls_back_to_message_id(self, client, approve_env):
        # If Graph returns no id (e.g. immutable id equals original), fall back.
        with patch.object(client, "_request", return_value={}):
            result = client.mail_archive("orig-3")
        assert result["data"]["restore_target"] == "orig-3"


# ── OneDrive files.untrash (Business SharePoint recycle bin) ───────────

class TestFilesUntrash:
    def test_files_trash_persists_recycle_bin_restore_target(self, client, approve_env):
        guid = "5d625d33-338c-4a77-a98a-3e287116440c"

        def _req(method, path, **kwargs):
            if method == "GET" and path.endswith("/drive/items/file-1"):
                return {"id": "file-1", "name": "notes.txt"}
            if method == "DELETE":
                return {}
            raise AssertionError(f"unexpected Graph call {method} {path}")

        client._sleep = lambda _s: None
        with patch.object(client, "_request", side_effect=_req), \
             patch.object(client, "_drive_web_url",
                          return_value="https://contoso-my.sharepoint.com/personal/u_contoso_com/Documents"), \
             patch.object(client, "_spo_request", return_value={
                 "value": [{"Id": guid, "LeafName": "notes.txt", "Title": "notes.txt"}],
             }) as spo:
            result = client.files_trash("file-1")
        assert result["success"] is True
        assert result["data"]["name"] == "notes.txt"
        assert result["data"]["restore_target"] == guid
        assert spo.called

    def test_files_untrash_guid_uses_sharepoint_restore(self, client, approve_env):
        guid = "5d625d33-338c-4a77-a98a-3e287116440c"
        with patch.object(client, "_drive_web_url",
                          return_value="https://contoso-my.sharepoint.com/personal/u_contoso_com/Documents"), \
             patch.object(client, "_spo_request", return_value={}) as spo:
            result = client.files_untrash(guid)
        assert result["success"] is True
        assert result["data"]["restore_path"] == "sharepoint_recycle_bin"
        assert spo.call_args.args[0] == "POST"
        assert "RestoreByIds" in spo.call_args.args[1]
        assert spo.call_args.kwargs["json_body"] == {"ids": [guid]}

    def test_files_untrash_personal_graph_path(self, client, approve_env):
        with patch.object(client, "_request", return_value={"id": "D464!1", "name": "a.txt"}) as req:
            result = client.files_untrash("D464!1")
        assert result["success"] is True
        assert result["data"]["restore_path"] == "graph_personal"
        assert req.call_args.args[0] == "POST"
        assert req.call_args.args[1].endswith("/drive/items/D464!1/restore")

    def test_files_untrash_business_fallback_after_personal_reject(self, client, approve_env):
        guid = "11111111-2222-3333-4444-555555555555"

        def _req(method, path, **kwargs):
            if method == "POST" and path.endswith("/restore"):
                raise RuntimeError("Graph API 400: not supported for work accounts")
            raise AssertionError(f"unexpected {method} {path}")

        client._sleep = lambda _s: None
        with patch.object(client, "_request", side_effect=_req), \
             patch.object(client, "_resolve_recycle_bin_id", return_value=guid), \
             patch.object(client, "_restore_via_sharepoint_recycle_bin",
                          return_value={
                              "id": guid, "restore_target": guid, "reversible": True,
                              "trashed": False, "restore_path": "sharepoint_recycle_bin",
                          }) as restore:
            result = client.files_untrash("01BUSINESSITEMIDXXXX")
        assert result["success"] is True
        assert result["data"]["restore_path"] == "sharepoint_recycle_bin"
        restore.assert_called_once_with(guid)

    def test_personal_site_base_from_drive_web_url(self, client):
        base = client._personal_site_base(
            "https://contoso-my.sharepoint.com/personal/user_contoso_com/Documents"
        )
        assert base == "https://contoso-my.sharepoint.com/personal/user_contoso_com"


# ── msal-missing ───────────────────────────────────────────────────────

class TestMsalMissing:
    def test_missing_msal_raises_clear_runtimeerror(self, m365_config, monkeypatch):
        from providers.m365_graph import M365GraphClient
        c = M365GraphClient(m365_config)
        # Force `import msal` to raise ImportError.
        monkeypatch.setitem(sys.modules, "msal", None)
        with pytest.raises(RuntimeError) as exc:
            c._get_token()
        assert "msal" in str(exc.value).lower()
        assert "pip install msal" in str(exc.value)
