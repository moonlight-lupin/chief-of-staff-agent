#!/usr/bin/env python3
"""v0.10.0 — the google_mcp provider (Google Workspace remote MCP servers).

Google's Workspace AI Tools Addendum (effective 2027-02-01) requires AI agents
to reach Workspace data through Google's MCP servers or CLI. These tests pin the
provider that talks to the Gmail, Calendar and Drive MCP servers directly:

* the stateless wire format (headers, per-request ``_meta``, JSON and SSE);
* each neutral method's tool name, arguments and normalised output;
* guarded writes (no grant, no request) and audit under ``google_mcp``;
* Google's own approval prompt (``input_required``): the client never answers
  it, a caller-supplied confirmer may, and only with an offered choice;
* denial, admin block and -32021 surface as named failures;
* writes are never retried on a timeout or 5xx;
* the access token stays in memory;
* wiring: registry, capabilities, hosted-session refusal, matrix, bootstrap,
  doctor, connect_workspace.

No network: ``requests.post`` and the service-account credentials are mocked.
"""
from __future__ import annotations

import base64
import contextlib
import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

import google_mcp_transport as gmt  # noqa: E402
import schemas  # noqa: E402
import state_db  # noqa: E402
import workspace_guardrails  # noqa: E402
from providers import google_mcp_workspace as gmw  # noqa: E402

TOKEN = "ya29.test-token-DO-NOT-PERSIST"
DELEGATE = "alicia@acme.example"


# ── fakes ───────────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status=200, body=None, text=None, content_type="application/json"):
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.text = text if text is not None else json.dumps(body)
        self.content = self.text.encode()


class FakeHTTP:
    """Stands in for ``requests.post``: records every call, replays a queue."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls: list[dict] = []

    def add(self, *responses):
        self.queue.extend(responses)

    def __call__(self, url, headers=None, json=None, timeout=None, **_kw):
        self.calls.append({"url": url, "headers": dict(headers or {}),
                           "json": copy.deepcopy(json), "timeout": timeout})
        if not self.queue:
            raise AssertionError(f"unexpected extra POST to {url}: {json}")
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def tool_calls(self):
        return [c["json"]["params"] for c in self.calls if c["json"].get("method") == "tools/call"]


def ok(payload=None, *, structured=True):
    result: dict = {"resultType": "complete", "content": []}
    if payload is not None:
        result["content"] = [{"type": "text", "text": json.dumps(payload)}]
        if structured:
            result["structuredContent"] = payload
    return FakeResponse(body={"jsonrpc": "2.0", "id": 1, "result": result})


def tool_error(text, reason=None):
    result: dict = {"resultType": "complete", "isError": True,
                    "content": [{"type": "text", "text": text}]}
    if reason:
        result["_meta"] = {"google.com/error": {
            "code": 400, "message": text, "status": "FAILED_PRECONDITION",
            "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                         "reason": reason, "domain": "mcp.googleapis.com"}]}}
    return FakeResponse(body={"jsonrpc": "2.0", "id": 1, "result": result})


ENUM = ["ALLOW_ONCE", "ALLOW_AUDIENCE_ALWAYS", "ALLOW_TOOL_ALWAYS", "DENY_ONCE"]
ENUM_NAMES = ["Allow once", "Always allow for these recipients", "Always allow this tool", "Don't allow"]
REQUEST_STATE = '{"elicitationId":"e-1","requestBinding":"b-1"}'


def input_required(enum=None, names=None):
    return FakeResponse(body={"jsonrpc": "2.0", "id": 2, "result": {
        "resultType": "input_required",
        "inputRequests": {"approval_choice": {
            "method": "elicitation/create",
            "params": {
                "mode": "form",
                "message": "**Quarterly numbers**  \nTo: bob@partner.example",
                "requestedSchema": {
                    "type": "object", "required": ["approval_choice"],
                    "properties": {"approval_choice": {
                        "type": "string",
                        "title": "Allow Chief of Staff to use send_message for these recipients?",
                        "enum": enum or ENUM, "enumNames": names or ENUM_NAMES}}}}}},
        "requestState": REQUEST_STATE}})


class FakeCreds:
    def __init__(self, token=TOKEN):
        self.token = token
        self.valid = True
        self.refreshed = 0

    def refresh(self, _request):
        self.refreshed += 1
        self.valid = True


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_break_glass(monkeypatch):
    for name in ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)


@pytest.fixture
def cfg(tmp_path):
    sa = tmp_path / "keys" / "sa.json"
    sa.parent.mkdir()
    sa.write_text("{}")
    project = tmp_path / "project"
    project.mkdir()
    return {
        "google": {"delegate_email": DELEGATE, "service_account_path": str(sa)},
        "integrations": {"workspace": {"provider": "google_mcp"}},
        "paths": {"project_root": str(project), "upload_roots": [str(tmp_path / "uploads")]},
    }


@pytest.fixture
def creds():
    c = FakeCreds()
    with patch.object(gmw, "_sa_credentials", return_value=c) as factory:
        c.factory = factory
        yield c


@pytest.fixture
def http(creds):
    fake = FakeHTTP()
    with patch("google_mcp_transport.requests.post", side_effect=fake) as post:
        fake.post = post
        yield fake


@pytest.fixture
def client(cfg, http):
    return gmw.GoogleMCPWorkspaceClient(cfg)


@contextlib.contextmanager
def granted(cfg, action_type, target, payload=None):
    """An approved, executing action and its single-use execution grant."""
    with state_db._open_db(cfg) as db:
        action_id = db.create_action(action_type, "google_mcp", target, payload or {"target": target})["id"]
    state_db.approve_pending_action(cfg, action_id, approver="op")
    assert state_db.mark_executing(cfg, action_id)
    with workspace_guardrails.execution_grant(cfg, action_id):
        yield action_id


def _audit_records(cfg):
    path = Path(cfg["paths"]["project_root"]) / ".audit" / "workspace.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


MSG = {"id": "m-1", "threadId": "t-1", "sender": "carol@client.example",
       "toRecipients": [DELEGATE], "subject": "Invoice 42", "snippet": "Please find",
       "date": "2026-11-20T09:15:00Z", "labelIds": ["INBOX", "UNREAD"],
       "viewUrl": "https://mail.google.com/mail/u/0/#all/m-1"}


# ── transport: request shape ────────────────────────────────────────────────

class TestRequestShape:
    def test_headers_on_every_post(self, client, http):
        http.add(ok({"labels": []}))
        client.mail_list_tags()
        call = http.calls[0]
        assert call["url"] == "https://gmailmcp.googleapis.com/mcp/v1"
        h = call["headers"]
        assert h["Authorization"] == f"Bearer {TOKEN}"
        assert h["Content-Type"] == "application/json"
        assert h["Accept"] == "application/json, text/event-stream"
        assert h["MCP-Protocol-Version"] == "2026-07-28"

    def test_endpoints_per_product(self, client, http):
        http.add(ok({"events": []}), ok({"files": []}))
        client.calendar_list("2026-11-27", "2026-11-28")
        client.files_search("", max_results=3)
        assert http.calls[0]["url"] == "https://calendarmcp.googleapis.com/mcp/v1"
        assert http.calls[1]["url"] == "https://drivemcp.googleapis.com/mcp/v1"

    def test_tools_call_carries_meta(self, client, http):
        import chief_of_staff

        http.add(ok({"labels": []}))
        client.mail_list_tags()
        body = http.calls[0]["json"]
        assert body["jsonrpc"] == "2.0" and body["method"] == "tools/call"
        assert "id" in body
        params = body["params"]
        assert params["name"] == "list_labels"
        assert params["arguments"] == {}
        meta = params["_meta"]
        assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
        assert meta["io.modelcontextprotocol/clientInfo"] == {
            "name": "Chief of Staff", "version": chief_of_staff.VERSION}
        assert meta["io.modelcontextprotocol/clientCapabilities"] == {"elicitation": {"form": {}}}
        # No stateful handshake: the first request is the tool call itself.
        assert all(c["json"]["method"] != "initialize" for c in http.calls)

    def test_server_discover_is_stateless(self, creds):
        fake = FakeHTTP(FakeResponse(body={"jsonrpc": "2.0", "id": 1, "result": {
            "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "gmail", "title": "Gmail"}}}}))
        with patch("google_mcp_transport.requests.post", side_effect=fake):
            transport = gmt.GoogleMCPTransport(lambda: TOKEN, client_version="9.9.9")
            info = transport.discover("gmail")
        assert fake.calls[0]["json"]["method"] == "server/discover"
        assert fake.calls[0]["json"]["params"] == {}
        assert info["title"] == "Gmail"


class TestResponseParsing:
    def test_plain_json(self):
        msg = gmt.parse_body('{"jsonrpc":"2.0","id":3,"result":{"x":1}}', "application/json")
        assert msg["result"] == {"x": 1}

    def test_sse_single_event(self):
        text = 'event: message\ndata: {"jsonrpc":"2.0","id":3,"result":{"x":2}}\n\n'
        assert gmt.parse_body(text, "text/event-stream")["result"] == {"x": 2}

    def test_sse_skips_notifications_and_takes_the_response(self):
        text = ('data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
                'data: {"jsonrpc":"2.0","id":7,\n'
                'data: "result":{"ok":true}}\n\n')
        assert gmt.parse_body(text, "text/event-stream")["result"] == {"ok": True}

    def test_sse_without_content_type_is_still_parsed(self):
        text = 'data: {"jsonrpc":"2.0","id":1,"result":{"y":3}}\n'
        assert gmt.parse_body(text, "")["result"] == {"y": 3}

    def test_garbage_raises(self):
        with pytest.raises(gmt.GoogleMCPError):
            gmt.parse_body("<html>oops</html>", "text/html")

    def test_sse_response_through_the_client(self, client, http):
        payload = {"labels": [{"labelId": "Label_1", "name": "Clients", "labelType": "USER"}]}
        body = {"jsonrpc": "2.0", "id": 1, "result": {
            "resultType": "complete", "content": [{"type": "text", "text": json.dumps(payload)}]}}
        http.add(FakeResponse(text=f"event: message\ndata: {json.dumps(body)}\n\n",
                              content_type="text/event-stream"))
        assert client.mail_list_tags() == [{"id": "Label_1", "name": "Clients", "type": "user"}]

    def test_text_content_without_structured_content(self, client, http):
        http.add(ok({"labels": [{"labelId": "INBOX", "name": "INBOX", "labelType": "SYSTEM"}]},
                    structured=False))
        assert client.mail_list_tags()[0]["type"] == "system"

    def test_empty_object_means_zero_items(self, client, http):
        http.add(ok({}))
        assert client.mail_list_tags() == []


class TestRpcErrors:
    def test_missing_client_capability(self, client, http):
        http.add(FakeResponse(body={"jsonrpc": "2.0", "id": 2, "error": {
            "code": -32021,
            "message": "Action could not be completed. This action requires user confirmation...",
            "data": {"requiredCapabilities": {"elicitation": {"form": {}}}}}}))
        with pytest.raises(gmt.MissingClientCapability) as exc:
            client.mail_list_tags()
        assert exc.value.code == -32021
        assert "MissingRequiredClientCapability" in str(exc.value)

    def test_other_rpc_error(self, client, http):
        http.add(FakeResponse(body={"jsonrpc": "2.0", "id": 2,
                                    "error": {"code": -32602, "message": "bad params"}}))
        with pytest.raises(gmt.GoogleMCPRPCError) as exc:
            client.mail_list_tags()
        assert exc.value.code == -32602 and "bad params" in str(exc.value)

    def test_http_401_is_not_retried(self, client, http):
        http.add(FakeResponse(status=401, text="unauthorized"))
        with pytest.raises(gmt.GoogleMCPTransportError) as exc:
            client.mail_list_tags()
        assert exc.value.status == 401
        assert len(http.calls) == 1


# ── retries ─────────────────────────────────────────────────────────────────

class TestRetryPolicy:
    @pytest.mark.parametrize("status", [429, 503])
    def test_read_retries_once_on_throttle(self, client, http, status):
        http.add(FakeResponse(status=status, text="slow down"), ok({"labels": []}))
        assert client.mail_list_tags() == []
        assert len(http.calls) == 2

    def test_read_retries_only_once(self, client, http):
        http.add(FakeResponse(status=503, text="x"), FakeResponse(status=503, text="x"))
        with pytest.raises(gmt.GoogleMCPTransportError):
            client.mail_list_tags()
        assert len(http.calls) == 2

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_transport_write_is_never_retried_on_status(self, creds, status):
        fake = FakeHTTP(FakeResponse(status=status, text="boom"), ok({}))
        with patch("google_mcp_transport.requests.post", side_effect=fake):
            transport = gmt.GoogleMCPTransport(lambda: TOKEN, client_version="1")
            with pytest.raises(gmt.GoogleMCPTransportError) as exc:
                transport.call_tool("gmail", "send_message", {"to": ["x@y.z"]}, read_only=False)
        assert len(fake.calls) == 1
        assert exc.value.outcome_unknown is (status >= 500)

    def test_transport_write_is_never_retried_on_timeout(self, creds):
        import requests

        fake = FakeHTTP(requests.Timeout("slow"), ok({}))
        with patch("google_mcp_transport.requests.post", side_effect=fake):
            transport = gmt.GoogleMCPTransport(lambda: TOKEN, client_version="1")
            with pytest.raises(gmt.GoogleMCPTransportError) as exc:
                transport.call_tool("gmail", "send_message", {"to": ["x@y.z"]}, read_only=False)
        assert len(fake.calls) == 1
        assert exc.value.outcome_unknown is True
        assert "may" in str(exc.value).lower()

    @pytest.mark.parametrize("failure", ["timeout", 500, 503, 504])
    def test_guarded_send_is_sent_once(self, cfg, client, http, failure):
        import requests

        http.add(requests.Timeout("slow") if failure == "timeout" else FakeResponse(status=failure, text="x"))
        with granted(cfg, "mail.send", "bob@partner.example"):
            out = client.mail_send("bob@partner.example", "Hi", "Body")
        assert out["success"] is False
        assert len(http.calls) == 1

    def test_timeout_on_a_read_is_not_retried_either(self, client, http):
        import requests

        http.add(requests.ConnectionError("reset"))
        with pytest.raises(gmt.GoogleMCPTransportError):
            client.mail_list_tags()
        assert len(http.calls) == 1


# ── reads ───────────────────────────────────────────────────────────────────

class TestMailSearch:
    def test_tool_and_arguments(self, client, http):
        http.add(ok({"threads": [{"id": "t-1", "messages": [MSG]}]}))
        client.mail_search("is:unread newer_than:2d", max_results=5)
        params = http.tool_calls[0]
        assert params["name"] == "search_threads"
        assert params["arguments"] == {"query": "is:unread newer_than:2d", "pageSize": 5}

    def test_normalised_shape(self, client, http):
        http.add(ok({"threads": [{"id": "t-1", "viewUrl": "https://mail/t-1", "messages": [MSG]}]}))
        [msg] = client.mail_search("is:unread")
        schemas.validate_message(msg)
        assert msg["id"] == "m-1" and msg["message_id"] == "m-1"
        assert msg["thread_id"] == "t-1" and msg["threadId"] == "t-1"
        assert msg["sender"] == msg["from"] == "carol@client.example"
        assert msg["subject"] == "Invoice 42"
        assert msg["snippet"] == "Please find"
        assert msg["date"] == "2026-11-20T09:15:00Z"
        assert msg["tags"] == msg["labelIds"] == ["INBOX", "UNREAD"]
        assert msg["link"] == MSG["viewUrl"]
        assert msg["source"] == "gmail"
        assert msg["to"] == [DELEGATE]

    def test_latest_message_of_each_thread(self, client, http):
        older = {**MSG, "id": "m-0", "date": "2026-11-19T08:00:00Z", "subject": "Re: Invoice 42"}
        http.add(ok({"threads": [{"id": "t-1", "messages": [older, MSG]}]}))
        [msg] = client.mail_search("x")
        assert msg["id"] == "m-1"

    def test_thread_without_messages_is_fetched(self, client, http):
        http.add(ok({"threads": [{"id": "t-9"}]}),
                 ok({"id": "t-9", "messages": [{**MSG, "id": "m-9", "threadId": "t-9"}]}))
        [msg] = client.mail_search("x")
        assert msg["id"] == "m-9"
        params = http.tool_calls[1]
        assert params["name"] == "get_thread"
        assert params["arguments"] == {"threadId": "t-9", "messageFormat": "MINIMAL"}

    def test_paginates_up_to_max_results(self, client, http):
        page1 = {"threads": [{"id": f"t-{i}", "messages": [{**MSG, "id": f"m-{i}"}]} for i in range(2)],
                 "nextPageToken": "p2"}
        page2 = {"threads": [{"id": f"t-{i}", "messages": [{**MSG, "id": f"m-{i}"}]} for i in range(2, 4)],
                 "nextPageToken": "p3"}
        http.add(ok(page1), ok(page2))
        out = client.mail_search("x", max_results=3)
        assert [m["id"] for m in out] == ["m-0", "m-1", "m-2"]
        assert http.tool_calls[1]["arguments"]["pageToken"] == "p2"
        assert len(http.calls) == 2

    def test_empty_query_is_omitted_and_page_size_capped(self, client, http):
        http.add(ok({}))
        assert client.mail_search("", max_results=500) == []
        assert http.tool_calls[0]["arguments"] == {"pageSize": 50}

    def test_internal_date_fallback(self, client, http):
        msg = {k: v for k, v in MSG.items() if k != "date"}
        msg["internalDate"] = "1763630100000"
        http.add(ok({"threads": [{"id": "t-1", "messages": [msg]}]}))
        [out] = client.mail_search("x")
        schemas.validate_message(out)
        assert out["date"].startswith("2025-11-20")

    def test_tool_error_is_raised_not_an_empty_list(self, client, http):
        http.add(tool_error("Quota exhausted"))
        with pytest.raises(gmt.GoogleMCPToolError):
            client.mail_search("x")


class TestOtherReads:
    def test_list_tags(self, client, http):
        http.add(ok({"labels": [
            {"labelId": "INBOX", "name": "INBOX", "labelType": "SYSTEM"},
            {"labelId": "Label_7", "name": "Finance/Invoices", "labelType": "USER"}]}))
        assert client.mail_list_tags() == [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "Label_7", "name": "Finance/Invoices", "type": "user"}]
        assert http.tool_calls[0]["name"] == "list_labels"

    def test_list_folders_is_labels(self, client, http):
        http.add(ok({"labels": [{"labelId": "Label_7", "name": "Clients", "labelType": "USER"}]}))
        assert client.mail_list_folders() == [{"id": "Label_7", "name": "Clients", "type": "user"}]
        assert http.tool_calls[0]["name"] == "list_labels"

    def test_calendar_list_dates(self, client, http):
        event = {"id": "e-1", "summary": "Board prep", "status": "confirmed",
                 "htmlLink": "https://calendar/e-1",
                 "start": {"dateTime": "2026-11-27T10:00:00+08:00"},
                 "end": {"dateTime": "2026-11-27T11:00:00+08:00"},
                 "attendees": [{"email": "dana@acme.example"}, {"email": DELEGATE, "self": True}],
                 "organizer": {"email": DELEGATE}, "location": "Room 4",
                 "conferenceUrl": "https://meet.google.com/abc"}
        http.add(ok({"events": [event]}))
        [out] = client.calendar_list("2026-11-27", "2026-11-28")
        params = http.tool_calls[0]
        assert params["name"] == "list_events"
        args = params["arguments"]
        assert args["calendarId"] == "primary"
        assert args["startTime"] == "2026-11-27T00:00:00"
        assert args["endTime"] == "2026-11-28T23:59:59"
        schemas.validate_event(out)
        assert out["id"] == "e-1" and out["title"] == "Board prep"
        assert out["start"] == "2026-11-27T10:00:00+08:00"
        assert out["attendees"] == ["dana@acme.example", DELEGATE]
        assert out["organizer"] == DELEGATE
        assert out["conference_link"] == "https://meet.google.com/abc"
        assert out["event_link"] == "https://calendar/e-1"
        assert out["status"] == "confirmed"

    def test_calendar_list_all_day_and_explicit_times(self, client, http):
        http.add(ok({"events": [{"id": "e-2", "summary": "Holiday",
                                 "start": {"date": "2026-12-25"}, "end": {"date": "2026-12-26"}}]}))
        [out] = client.calendar_list("2026-12-25T00:00:00Z", "2026-12-26T00:00:00Z")
        args = http.tool_calls[0]["arguments"]
        assert args["startTime"] == "2026-12-25T00:00:00Z"
        assert out["start"] == "2026-12-25"
        schemas.validate_event(out)

    def test_calendar_list_paginates(self, client, http):
        e = {"start": {"dateTime": "2026-11-27T10:00:00Z"}, "end": {"dateTime": "2026-11-27T11:00:00Z"}}
        http.add(ok({"events": [{**e, "id": "a", "summary": "A"}], "nextPageToken": "n"}),
                 ok({"events": [{**e, "id": "b", "summary": "B"}]}))
        out = client.calendar_list("2026-11-27", "2026-11-27")
        assert [x["id"] for x in out] == ["a", "b"]
        assert http.tool_calls[1]["arguments"]["pageToken"] == "n"

    @pytest.mark.parametrize("query, expected", [
        ("name = 'NDA'", "title = 'NDA'"),
        ("name contains 'Q3' and trashed = false", "title contains 'Q3' and trashed = false"),
        ("quarterly report", "fullText contains 'quarterly report'"),
        ("it's", "fullText contains 'it\\'s'"),
        ("mimeType = 'application/pdf'", "mimeType = 'application/pdf'"),
    ])
    def test_files_search_query_translation(self, client, http, query, expected):
        http.add(ok({"files": []}))
        client.files_search(query, max_results=7)
        params = http.tool_calls[0]
        assert params["name"] == "search_files"
        assert params["arguments"] == {"query": expected, "pageSize": 7, "excludeContentSnippets": True}

    def test_files_search_normalised(self, client, http):
        http.add(ok({"files": [{"id": "f-1", "title": "NDA.pdf", "mimeType": "application/pdf",
                                "modifiedTime": "2026-11-01T12:00:00Z", "viewUrl": "https://drive/f-1",
                                "parentId": "folder-1"}]}))
        [f] = client.files_search("")
        assert "query" not in http.tool_calls[0]["arguments"]
        schemas.validate_file(f)
        assert f == {"id": "f-1", "name": "NDA.pdf", "title": "NDA.pdf", "mime_type": "application/pdf",
                     "mimeType": "application/pdf", "modified": "2026-11-01T12:00:00Z",
                     "link": "https://drive/f-1", "parents": ["folder-1"], "source": "gdrive"}

    def test_health_check(self, client, http):
        http.add(ok({"labels": []}))
        assert client.health_check() is True
        http.add(FakeResponse(status=401, text="no"))
        assert client.health_check() is False


# ── writes ──────────────────────────────────────────────────────────────────

WRITES = [
    ("mail_send", ("bob@partner.example", "Hi", "Body"), {}),
    ("mail_create_draft", ("bob@partner.example", "Hi", "Body"), {}),
    ("mail_tag", ("m-1", "Label_7"), {}),
    ("mail_create_tag", ("Clients",), {}),
    ("mail_archive", ("m-1",), {}),
    ("mail_unarchive", ("m-1",), {}),
    ("mail_move_to_folder", ("m-1", "Label_7"), {}),
    ("mail_trash", ("m-1",), {}),
    ("mail_untrash", ("m-1",), {}),
    ("calendar_create", ("Review", "2026-12-01T10:00:00", "2026-12-01T11:00:00"), {}),
    ("calendar_update", ("e-1",), {"title": "New title"}),
    ("calendar_delete", ("e-1",), {}),
    ("files_trash", ("f-1",), {}),
]


class TestGuardedWrites:
    @pytest.mark.parametrize("method, args, kwargs", WRITES, ids=[w[0] for w in WRITES])
    def test_refused_without_a_grant_and_nothing_is_sent(self, client, http, method, args, kwargs):
        with patch("sys.stdin.isatty", return_value=False):
            out = getattr(client, method)(*args, **kwargs)
        assert out["success"] is False
        assert "guardrail" in out["error"]
        assert out["provider"] == "google_mcp"
        assert http.calls == []

    def test_blocked_write_is_audited_under_google_mcp(self, cfg, client, http):
        with patch("sys.stdin.isatty", return_value=False):
            client.mail_send("bob@partner.example", "Hi", "Body")
        records = _audit_records(cfg)
        assert any(r["provider"] == "google_mcp" and r["status"] == "blocked" for r in records)

    def test_grant_for_another_recipient_does_not_cover_this_one(self, cfg, client, http):
        with granted(cfg, "mail.send", "bob@partner.example"):
            with patch("sys.stdin.isatty", return_value=False):
                out = client.mail_send("eve@attacker.example", "Hi", "Body")
        assert out["success"] is False and http.calls == []

    def test_send(self, cfg, client, http):
        http.add(ok({"id": "m-new", "threadId": "t-new", "viewUrl": "https://mail/m-new"}))
        with granted(cfg, "mail.send", "bob@partner.example, ann@partner.example",
                     {"to": "bob@partner.example, ann@partner.example", "cc": "cc@acme.example"}):
            out = client.mail_send("bob@partner.example, ann@partner.example", "Hi", "Plain body",
                                   cc="cc@acme.example")
        assert out["success"] is True, out
        assert out["action"] == "mail.send" and out["provider"] == "google_mcp"
        params = http.tool_calls[0]
        assert params["name"] == "send_message"
        assert params["arguments"] == {"to": ["bob@partner.example", "ann@partner.example"],
                                       "cc": ["cc@acme.example"], "subject": "Hi", "body": "Plain body"}
        assert out["data"]["id"] == "m-new" and out["data"]["thread_id"] == "t-new"
        assert any(r["provider"] == "google_mcp" and r["operation"] == "mail.send"
                   and r["status"] == "success" for r in _audit_records(cfg))

    def test_send_rejects_header_injection(self, cfg, client, http):
        with granted(cfg, "mail.send", "bob@partner.example"):
            out = client.mail_send("bob@partner.example", "Hi\r\nBcc: eve@x.example", "Body")
        assert out["success"] is False and http.calls == []

    def test_draft(self, cfg, client, http):
        http.add(ok({"id": "r-123", "messageId": "m-d", "threadId": "t-d", "viewUrl": "https://mail/d"}))
        with granted(cfg, "mail.draft", "bob@partner.example"):
            out = client.mail_create_draft("bob@partner.example", "Hi", "Body")
        assert out["success"] is True, out
        params = http.tool_calls[0]
        assert params["name"] == "create_draft"
        assert params["arguments"] == {"to": ["bob@partner.example"], "subject": "Hi", "body": "Body"}
        assert out["data"]["id"] == "m-d" and out["data"]["draft_id"] == "r-123"

    def test_tag_by_id(self, cfg, client, http):
        http.add(ok({}))
        with granted(cfg, "mail.tag", "m-1"):
            out = client.mail_tag("m-1", "Label_7")
        assert out["success"] is True, out
        params = http.tool_calls[0]
        assert params == {**params, "name": "label_message",
                          "arguments": {"messageId": "m-1", "labelIds": ["Label_7"]}}
        assert out["data"]["label_id"] == "Label_7"

    def test_tag_by_name_resolves_the_label(self, cfg, client, http):
        http.add(ok({"labels": [{"labelId": "Label_7", "name": "Finance/Invoices", "labelType": "USER"}]}),
                 ok({}))
        with granted(cfg, "mail.tag", "m-1"):
            out = client.mail_tag("m-1", "finance/invoices")
        assert out["success"] is True, out
        assert http.tool_calls[1]["arguments"] == {"messageId": "m-1", "labelIds": ["Label_7"]}

    def test_tag_unknown_name_fails(self, cfg, client, http):
        http.add(ok({"labels": []}))
        with granted(cfg, "mail.tag", "m-1"):
            out = client.mail_tag("m-1", "Nope")
        assert out["success"] is False and "Nope" in out["error"]

    def test_create_tag(self, cfg, client, http):
        http.add(ok({"labelId": "Label_9", "name": "Clients"}))
        with granted(cfg, "mail.create_tag", "Clients"):
            out = client.mail_create_tag("Clients")
        assert out["success"] is True, out
        assert http.tool_calls[0]["name"] == "create_label"
        assert http.tool_calls[0]["arguments"] == {"displayName": "Clients"}
        assert out["data"]["id"] == "Label_9"

    @pytest.mark.parametrize("method, action, add, remove", [
        ("mail_archive", "mail.archive", None, ["INBOX"]),
        ("mail_unarchive", "mail.unarchive", ["INBOX"], None),
    ])
    def test_archive_unarchive(self, cfg, client, http, method, action, add, remove):
        http.add(ok({"messageId": "m-1", "labelIds": []}))
        with granted(cfg, action, "m-1"):
            out = getattr(client, method)("m-1")
        assert out["success"] is True, out
        params = http.tool_calls[0]
        assert params["name"] == "update_message_labels"
        expected = {"messageId": "m-1"}
        if add:
            expected["addLabelIds"] = add
        if remove:
            expected["removeLabelIds"] = remove
        assert params["arguments"] == expected
        assert out["data"]["reversible"] is True

    def test_move_to_label(self, cfg, client, http):
        http.add(ok({"messageId": "m-1", "labelIds": ["Label_7"]}))
        with granted(cfg, "mail.move", "m-1"):
            out = client.mail_move_to_folder("m-1", "Label_7")
        assert out["success"] is True, out
        assert http.tool_calls[0]["arguments"] == {
            "messageId": "m-1", "addLabelIds": ["Label_7"], "removeLabelIds": ["INBOX"]}
        assert out["data"]["undo_add_label_ids"] == ["INBOX"]
        assert out["data"]["undo_remove_label_ids"] == ["Label_7"]

    @pytest.mark.parametrize("method, action, tool", [
        ("mail_trash", "mail.trash", "trash_message"),
        ("mail_untrash", "mail.untrash", "untrash_message"),
    ])
    def test_trash_untrash(self, cfg, client, http, method, action, tool):
        http.add(ok())
        with granted(cfg, action, "m-1"):
            out = getattr(client, method)("m-1")
        assert out["success"] is True, out
        assert http.tool_calls[0]["name"] == tool
        assert http.tool_calls[0]["arguments"] == {"messageId": "m-1"}

    def test_draft_id_is_not_a_message_id(self, cfg, client, http):
        with granted(cfg, "mail.trash", "r-5544"):
            out = client.mail_trash("r-5544")
        assert out["success"] is False and http.calls == []

    def test_calendar_create(self, cfg, client, http):
        http.add(ok({"id": "e-new", "htmlLink": "https://cal/e-new",
                     "conferenceUrl": "https://meet.google.com/xyz"}))
        with granted(cfg, "calendar.create", "Review"):
            out = client.calendar_create("Review", "2026-12-01T10:00:00+08:00", "2026-12-01T11:00:00+08:00",
                                         attendees=["dana@acme.example"], description="Agenda")
        assert out["success"] is True, out
        params = http.tool_calls[0]
        assert params["name"] == "create_event"
        assert params["arguments"] == {
            "calendarId": "primary", "summary": "Review",
            "startTime": "2026-12-01T10:00:00+08:00", "endTime": "2026-12-01T11:00:00+08:00",
            "attendees": [{"email": "dana@acme.example"}], "description": "Agenda",
            "addGoogleMeetUrl": True}
        assert out["data"]["id"] == "e-new"
        assert out["data"]["conference_link"] == "https://meet.google.com/xyz"

    def test_calendar_create_date_only_defaults(self, cfg, client, http):
        http.add(ok({"id": "e-new"}))
        with granted(cfg, "calendar.create", "Review"):
            client.calendar_create("Review", "2026-12-01", "2026-12-01")
        args = http.tool_calls[0]["arguments"]
        assert args["startTime"] == "2026-12-01T10:00:00"
        assert args["endTime"] == "2026-12-01T11:00:00"
        assert "attendees" not in args

    def test_calendar_create_refuses_a_bad_attendee(self, cfg, client, http):
        with granted(cfg, "calendar.create", "Review"):
            out = client.calendar_create("Review", "2026-12-01", "2026-12-01", attendees=["not-an-email"])
        assert out["success"] is False and http.calls == []

    def test_calendar_update(self, cfg, client, http):
        http.add(ok({"id": "e-1"}))
        with granted(cfg, "calendar.update", "e-1"):
            out = client.calendar_update("e-1", title="New", start="2026-12-02T09:00:00",
                                         end="2026-12-02T10:00:00", description="d", location="L",
                                         attendees=["new@acme.example"])
        assert out["success"] is True, out
        params = http.tool_calls[0]
        assert params["name"] == "update_event"
        assert params["arguments"] == {
            "calendarId": "primary", "eventId": "e-1", "summary": "New",
            "startTime": "2026-12-02T09:00:00", "endTime": "2026-12-02T10:00:00",
            "description": "d", "location": "L", "addedAttendees": [{"email": "new@acme.example"}]}

    @pytest.mark.parametrize("field", ["status", "colour", "recurrence"])
    def test_calendar_update_refuses_unknown_fields(self, cfg, client, http, field):
        with granted(cfg, "calendar.update", "e-1"):
            out = client.calendar_update("e-1", **{field: "x"})
        assert out["success"] is False and field in out["error"]
        assert http.calls == []

    def test_calendar_delete(self, cfg, client, http):
        http.add(ok({"id": "e-1", "status": "cancelled"}))
        with granted(cfg, "calendar.delete", "e-1"):
            out = client.calendar_delete("e-1")
        assert out["success"] is True, out
        assert http.tool_calls[0]["name"] == "delete_event"
        assert http.tool_calls[0]["arguments"] == {"calendarId": "primary", "eventId": "e-1"}
        assert out["data"] == {"id": "e-1", "deleted": True}

    def test_calendar_cancel_is_unsupported(self, client):
        assert client.supports("calendar.cancel") is False
        assert client.supports("calendar.uncancel") is False
        with pytest.raises(NotImplementedError):
            client.calendar_cancel("e-1")

    def test_files_upload_base64(self, cfg, client, http, tmp_path):
        src = tmp_path / "uploads" / "deck.pdf"
        src.parent.mkdir()
        src.write_bytes(b"%PDF-1.7\x00\xff binary")
        http.add(ok({"id": "f-new", "title": "deck.pdf", "mimeType": "application/pdf",
                     "viewUrl": "https://drive/f-new"}))
        with granted(cfg, "files.upload", str(src), {"file_path": str(src)}):
            out = client.files_upload(str(src), parent_id="folder-1")
        assert out["success"] is True, out
        params = http.tool_calls[0]
        assert params["name"] == "create_file"
        args = params["arguments"]
        assert args == {"title": "deck.pdf", "contentMimeType": "application/pdf",
                        "base64Content": base64.b64encode(src.read_bytes()).decode("ascii"),
                        "disableConversionToGoogleType": True, "parentId": "folder-1"}
        assert out["data"]["id"] == "f-new" and out["data"]["link"] == "https://drive/f-new"

    def test_files_upload_outside_roots_is_refused(self, cfg, client, http, tmp_path):
        src = tmp_path / "elsewhere.txt"
        src.write_text("x")
        # The queue refuses such a payload outright; this pins the client's own
        # check for a direct call that holds a grant naming the path.
        with granted(cfg, "files.upload", str(src), {"target": str(src)}):
            out = client.files_upload(str(src))
        assert out["success"] is False and http.calls == []

    def test_files_download(self, cfg, client, http):
        dest = Path(cfg["paths"]["project_root"]) / "downloads" / "nda.pdf"
        data = b"%PDF binary \x00\x01"
        http.add(ok({"id": "f-1", "title": "nda.pdf", "mimeType": "application/pdf",
                     "content": base64.b64encode(data).decode()}))
        with granted(cfg, "files.download", "f-1", {"file_id": "f-1", "output_path": str(dest)}):
            out = client.files_download("f-1", str(dest))
        assert out["success"] is True, out
        assert http.tool_calls[0]["name"] == "download_file_content"
        assert http.tool_calls[0]["arguments"] == {"fileId": "f-1"}
        assert dest.read_bytes() == data
        assert out["data"]["path"] == str(dest) and out["data"]["bytes"] == len(data)
        assert "content" not in out["data"]

    def test_files_trash(self, cfg, client, http):
        http.add(ok())
        with granted(cfg, "files.trash", "f-1"):
            out = client.files_trash("f-1")
        assert out["success"] is True, out
        assert http.tool_calls[0]["name"] == "trash_file"
        assert http.tool_calls[0]["arguments"] == {"fileId": "f-1"}

    def test_unsupported_methods(self, client):
        for name, args in (("files_untrash", ("f-1",)), ("contacts_list", ()),
                           ("contacts_create", ()), ("contacts_delete", ("p",))):
            with pytest.raises(NotImplementedError):
                getattr(client, name)(*args)


# ── Google's approval prompt ────────────────────────────────────────────────

class TestConfirmation:
    def test_input_required_raises_with_the_prompt(self, cfg, client, http):
        http.add(input_required())
        with granted(cfg, "mail.send", "bob@partner.example"):
            with pytest.raises(gmt.GoogleConfirmationRequired) as exc:
                client.mail_send("bob@partner.example", "Hi", "Body")
        prompt = exc.value
        assert isinstance(prompt, workspace_guardrails.ConfirmationRequired)
        assert prompt.server == "gmailmcp.googleapis.com"
        assert prompt.product == "Gmail"
        assert prompt.tool == "send_message"
        assert prompt.message.startswith("**Quarterly numbers**")
        assert prompt.title.startswith("Allow Chief of Staff")
        assert prompt.choices == list(zip(ENUM, ENUM_NAMES))
        assert prompt.request_state == REQUEST_STATE
        assert prompt.arguments == {"to": ["bob@partner.example"], "subject": "Hi", "body": "Body"}
        assert len(http.calls) == 1

    def test_prompt_is_audited_as_confirmation_required(self, cfg, client, http):
        http.add(input_required())
        with granted(cfg, "mail.send", "bob@partner.example"):
            with pytest.raises(gmt.GoogleConfirmationRequired):
                client.mail_send("bob@partner.example", "Hi", "Body")
        assert any(r["status"] == "confirmation_required" and r["provider"] == "google_mcp"
                   for r in _audit_records(cfg))

    def test_prompt_dict_keeps_private_fields_out_by_default(self, cfg, client, http):
        http.add(input_required())
        with granted(cfg, "mail.send", "bob@partner.example"):
            with pytest.raises(gmt.GoogleConfirmationRequired) as exc:
                client.mail_send("bob@partner.example", "Hi", "Secret body text")
        public = exc.value.to_dict()
        assert public["tool"] == "send_message" and public["product"] == "Gmail"
        assert public["choices"] == [{"id": i, "label": n} for i, n in zip(ENUM, ENUM_NAMES)]
        assert "Secret body text" not in json.dumps(public)
        assert REQUEST_STATE not in json.dumps(public)
        assert "Secret body text" not in str(exc.value)
        private = exc.value.to_dict(include_private=True)
        assert private["arguments"]["body"] == "Secret body text"
        assert private["request_state"] == REQUEST_STATE

    def test_confirmer_retry_is_identical_plus_state_and_responses(self, cfg, client, http):
        http.add(input_required(), ok({"id": "m-new", "threadId": "t-new"}))
        seen = []

        def confirmer(prompt):
            seen.append(prompt)
            return "ALLOW_ONCE"

        client.confirmer = confirmer
        with granted(cfg, "mail.send", "bob@partner.example"):
            out = client.mail_send("bob@partner.example", "Hi", "Body")
        assert out["success"] is True, out
        assert len(seen) == 1 and isinstance(seen[0], gmt.GoogleConfirmationRequired)
        first, retry = http.tool_calls
        assert retry["name"] == first["name"] == "send_message"
        assert retry["arguments"] == first["arguments"]
        assert retry["requestState"] == REQUEST_STATE
        assert retry["inputResponses"] == {
            "approval_choice": {"action": "accept", "content": {"approval_choice": "ALLOW_ONCE"}}}
        assert retry["_meta"] == first["_meta"]
        assert "requestState" not in retry["_meta"] and "inputResponses" not in retry["_meta"]
        assert "requestState" not in first and "inputResponses" not in first
        assert http.calls[1]["url"] == http.calls[0]["url"]

    def test_confirmer_via_constructor(self, cfg, http):
        http.add(input_required(), ok({"id": "m-new"}))
        client = gmw.GoogleMCPWorkspaceClient(cfg, confirmer=lambda p: "ALLOW_ONCE")
        with granted(cfg, "mail.send", "bob@partner.example"):
            assert client.mail_send("bob@partner.example", "Hi", "Body")["success"] is True

    def test_no_confirmer_never_answers(self, cfg, client, http):
        http.add(input_required())
        with granted(cfg, "mail.send", "bob@partner.example"):
            with pytest.raises(gmt.GoogleConfirmationRequired):
                client.mail_send("bob@partner.example", "Hi", "Body")
        assert len(http.calls) == 1
        assert "inputResponses" not in http.tool_calls[0]

    def test_confirmer_returning_none_raises(self, cfg, client, http):
        http.add(input_required())
        client.confirmer = lambda prompt: None
        with granted(cfg, "mail.send", "bob@partner.example"):
            with pytest.raises(gmt.GoogleConfirmationRequired):
                client.mail_send("bob@partner.example", "Hi", "Body")
        assert len(http.calls) == 1

    @pytest.mark.parametrize("choice", ["ALLOW_ALWAYS", "allow_once", "", "ALLOW_RESOURCE_ALWAYS", 1])
    def test_choice_outside_the_offered_enum_is_refused(self, cfg, client, http, choice):
        http.add(input_required())
        client.confirmer = lambda prompt: choice
        with granted(cfg, "mail.send", "bob@partner.example"):
            with pytest.raises(gmt.ConfirmationChoiceRefused) as exc:
                client.mail_send("bob@partner.example", "Hi", "Body")
        assert len(http.calls) == 1
        assert "not one of" in str(exc.value)

    def test_deny_once_surfaces_user_denied(self, cfg, client, http):
        http.add(input_required(),
                 tool_error("Action canceled. The user declined the confirmation request.",
                            "UserDeniedAction"))
        client.confirmer = lambda prompt: "DENY_ONCE"
        with granted(cfg, "mail.send", "bob@partner.example"):
            out = client.mail_send("bob@partner.example", "Hi", "Body")
        assert out["success"] is False
        assert "UserDeniedAction" in out["error"]
        assert http.tool_calls[1]["inputResponses"]["approval_choice"]["content"] == {
            "approval_choice": "DENY_ONCE"}

    def test_admin_policy_block(self, cfg, client, http):
        http.add(tool_error("Action could not be completed. Your administrator has blocked "
                            "third-party agents from performing this action.", "AdminPolicyBlock"))
        with granted(cfg, "mail.send", "bob@partner.example"):
            out = client.mail_send("bob@partner.example", "Hi", "Body")
        assert out["success"] is False and "AdminPolicyBlock" in out["error"]
        assert len(http.calls) == 1

    def test_denial_without_structured_meta_falls_back_to_the_text(self, client, http):
        http.add(tool_error("Action canceled. The user declined the confirmation request."))
        with pytest.raises(gmt.UserDeniedAction) as exc:
            client.mail_list_tags()
        assert "declined" in str(exc.value)

    def test_read_prompt_raises_too(self, client, http):
        http.add(input_required(enum=["ALLOW_ONCE", "ALLOW_OWNER_ALWAYS", "DENY_ONCE"],
                                names=["Allow once", "Always allow for this owner", "Don't allow"]))
        with pytest.raises(gmt.GoogleConfirmationRequired) as exc:
            client.files_search("budget")
        assert exc.value.product == "Google Drive"

    def test_rounds_are_bounded(self, cfg, client, http):
        http.add(input_required(), input_required(), input_required(), input_required())
        calls = []
        client.confirmer = lambda prompt: calls.append(1) or "ALLOW_ONCE"
        with granted(cfg, "mail.send", "bob@partner.example"):
            with pytest.raises(gmt.GoogleConfirmationRequired):
                client.mail_send("bob@partner.example", "Hi", "Body")
        assert len(http.calls) == 3 and len(calls) == 2


# ── the shared execution path ───────────────────────────────────────────────

class TestRunApproved:
    def _approved(self, cfg):
        with state_db._open_db(cfg) as db:
            action = db.create_action("mail.send", "google_mcp", "bob@partner.example",
                                      {"to": "bob@partner.example", "subject": "Hi", "body": "Body"})
        state_db.approve_pending_action(cfg, action["id"], approver="op")
        return state_db.get_pending_action(cfg, action["id"])

    def _send(self, client, action):
        p = action["payload"]
        return client.mail_send(p["to"], p["subject"], p["body"])

    def test_google_prompt_leaves_the_action_approved(self, cfg, client, http):
        import action_execution as ax

        http.add(input_required())
        action = self._approved(cfg)
        with patch("company_registry.preflight", return_value=(None, {"checked": True})):
            out = ax.run_approved(cfg, action, client, self._send)
        assert out.ok is False and out.stage == "confirm"
        assert isinstance(out.prompt, gmt.GoogleConfirmationRequired)
        assert out.prompt.tool == "send_message"
        assert state_db.get_pending_action(cfg, action["id"])["state"] == "approved"
        assert len(http.calls) == 1
        assert "Body" not in out.error

    def test_confirmed_send_executes(self, cfg, client, http):
        import action_execution as ax

        http.add(input_required(), ok({"id": "m-new", "threadId": "t-new"}))
        client.confirmer = lambda prompt: "ALLOW_ONCE"
        action = self._approved(cfg)
        with patch("company_registry.preflight", return_value=(None, {"checked": True})):
            out = ax.run_approved(cfg, action, client, self._send)
        assert out.ok, out
        assert state_db.get_pending_action(cfg, action["id"])["state"] == "executed"

    def test_denied_send_is_final(self, cfg, client, http):
        import action_execution as ax

        http.add(tool_error("Action canceled.", "UserDeniedAction"))
        action = self._approved(cfg)
        with patch("company_registry.preflight", return_value=(None, {"checked": True})):
            out = ax.run_approved(cfg, action, client, self._send)
        assert out.stage == "provider" and "UserDeniedAction" in out.error
        assert state_db.get_pending_action(cfg, action["id"])["state"] == "failed"


# ── auth ────────────────────────────────────────────────────────────────────

class TestAuth:
    def test_scopes_and_subject(self, client, http, creds):
        http.add(ok({"labels": []}))
        client.mail_list_tags()
        kwargs = creds.factory.call_args.kwargs
        assert sorted(kwargs["scopes"]) == sorted([
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/drive"])
        assert kwargs["delegate_email"] == DELEGATE
        assert kwargs["service_account_path"].endswith("sa.json")

    def test_token_is_cached_in_memory(self, client, http, creds):
        http.add(ok({"labels": []}), ok({"labels": []}), ok({"files": []}))
        client.mail_list_tags()
        client.mail_list_tags()
        client.files_search("")
        assert creds.factory.call_count == 1

    def test_expired_token_is_refreshed_not_rebuilt(self, client, http, creds):
        http.add(ok({"labels": []}), ok({"labels": []}))
        client.mail_list_tags()
        creds.valid = False
        client.mail_list_tags()
        assert creds.refreshed == 1 and creds.factory.call_count == 1

    def test_constructor_needs_no_network(self, cfg):
        with patch.object(gmw, "_sa_credentials") as factory, \
             patch("google_mcp_transport.requests.post") as post:
            gmw.GoogleMCPWorkspaceClient(cfg)
        factory.assert_not_called()
        post.assert_not_called()

    def test_token_is_never_written_to_disk(self, cfg, client, http, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        before = {p for p in tmp_path.rglob("*") if p.is_file()}
        http.add(ok({"labels": []}), ok({"threads": [{"id": "t-1", "messages": [MSG]}]}),
                 ok({"id": "m-new"}), input_required())
        client.mail_list_tags()
        client.mail_search("x")
        with granted(cfg, "mail.send", "bob@partner.example"):
            client.mail_send("bob@partner.example", "Hi", "Body")
        with granted(cfg, "mail.send", "bob@partner.example"):
            with pytest.raises(gmt.GoogleConfirmationRequired):
                client.mail_send("bob@partner.example", "Hi", "Body")
        written = [p for p in tmp_path.rglob("*") if p.is_file()]
        assert set(written) - before, "the writes should at least have been audited"
        for path in written:
            assert TOKEN.encode() not in path.read_bytes(), f"token leaked into {path}"

    def test_missing_service_account_is_explained(self, cfg, http):
        cfg["google"].pop("service_account_path")
        client = gmw.GoogleMCPWorkspaceClient(cfg)
        with patch.object(gmw, "_sa_credentials") as factory:
            with pytest.raises(RuntimeError, match="service_account_path"):
                client.mail_list_tags()
        factory.assert_not_called()

    def test_hermes_secret_route(self, cfg, http, monkeypatch):
        import plugin_secrets

        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", '{"type": "service_account"}')
        client = gmw.GoogleMCPWorkspaceClient(cfg)
        with patch.object(gmw, "_sa_credentials", return_value=FakeCreds()) as factory:
            http.add(ok({"labels": []}))
            client.mail_list_tags()
        assert factory.call_args.kwargs["service_account_path"] == plugin_secrets.SA_SECRET_SOURCE

    def test_key_file_is_refused_under_hermes(self, cfg, monkeypatch):
        """The real credential builder (shared with google_api) refuses a key file under Hermes."""
        import plugin_secrets

        monkeypatch.setattr(plugin_secrets, "under_hermes", lambda: True)
        client = gmw.GoogleMCPWorkspaceClient(cfg)
        with patch("google_mcp_transport.requests.post") as post:
            with pytest.raises(RuntimeError, match="GOOGLE_SERVICE_ACCOUNT_JSON"):
                client.mail_list_tags()
        post.assert_not_called()

    def test_acting_identity(self, client):
        assert client.acting_identity("mail") == DELEGATE
        assert client.acting_identity("calendar") == DELEGATE

    def test_acting_identity_unset(self, cfg):
        cfg["google"]["delegate_email"] = ""
        assert gmw.GoogleMCPWorkspaceClient(cfg).acting_identity("mail") is None


# ── wiring ──────────────────────────────────────────────────────────────────

ACTION_METHODS = {
    "mail.search": "mail_search", "mail.draft": "mail_create_draft", "mail.send": "mail_send",
    "mail.list_folders": "mail_list_folders", "mail.move": "mail_move_to_folder",
    "mail.archive": "mail_archive", "mail.unarchive": "mail_unarchive", "mail.trash": "mail_trash",
    "mail.untrash": "mail_untrash", "mail.list_tags": "mail_list_tags", "mail.tag": "mail_tag",
    "mail.create_tag": "mail_create_tag", "calendar.list": "calendar_list",
    "calendar.create": "calendar_create", "calendar.update": "calendar_update",
    "calendar.cancel": "calendar_cancel", "calendar.uncancel": "calendar_uncancel",
    "calendar.delete": "calendar_delete", "files.search": "files_search",
    "files.upload": "files_upload", "files.download": "files_download", "files.trash": "files_trash",
    "files.untrash": "files_untrash", "contacts.list": "contacts_list",
    "contacts.create": "contacts_create", "contacts.update": "contacts_update",
    "contacts.delete": "contacts_delete",
}


class TestWiring:
    def test_registry(self, cfg):
        from workspace_client import get_workspace_client, registered_providers

        assert "google_mcp" in registered_providers()
        client = get_workspace_client(cfg)
        assert isinstance(client, gmw.GoogleMCPWorkspaceClient)
        assert client.provider_name == "google_mcp"

    def test_hosted_session_refusal(self, cfg, monkeypatch):
        from workspace_client import get_workspace_client

        assert "google_mcp" in workspace_guardrails.CREDENTIAL_PROVIDERS
        monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "session_x")
        with pytest.raises(workspace_guardrails.HostedSessionRefusal, match="google_mcp"):
            get_workspace_client(cfg)

    def test_capability_table_is_explicit(self):
        import workspace_capabilities as caps

        table = caps.CAPABILITIES["google_mcp"]
        assert set(ACTION_METHODS) <= set(table)
        off = sorted(a for a, v in table.items() if not v)
        assert off == ["calendar.cancel", "calendar.uncancel", "contacts.create", "contacts.delete",
                       "contacts.list", "contacts.update", "files.untrash"]
        for action in off:
            reason = caps.get_unsupported_reason("google_mcp", action)
            assert reason != f"{action} is not supported by google_mcp", action
        assert "untrash" in caps.get_unsupported_reason("google_mcp", "drive.untrash")

    def test_every_supported_action_is_implemented(self, client):
        import workspace_capabilities as caps
        from workspace_client import WorkspaceClient

        for action, supported in caps.CAPABILITIES["google_mcp"].items():
            method = ACTION_METHODS[action]
            own = getattr(type(client), method, None)
            base = getattr(WorkspaceClient, method, None)
            if supported:
                assert own is not None and own is not base, f"{action} supported but not implemented"

    def test_verification_matrix_is_catalog_only(self):
        import verification_matrix as vm
        import workspace_capabilities as caps

        assert vm.validate() == []
        evidence = vm.EVIDENCE["google_mcp"]
        supported = {a for a, ok in caps.CAPABILITIES["google_mcp"].items() if ok}
        assert set(evidence) == supported
        for action, entry in evidence.items():
            assert entry["level"] == "catalog", action
            assert "2026-11-27" in entry["source"] and "MCP" in entry["source"]
        summary = vm.provider_summary("google_mcp")
        assert summary["counts"]["live"] == 0
        assert summary["provider_verified"] is False

    def test_capability_report(self):
        import chief_of_staff

        report = chief_of_staff.build_capability_report(
            {"integrations": {"workspace": {"provider": "google_mcp"}}})
        assert report["provider"] == "google_mcp"
        assert report["provider_verified"] is False
        assert "mail.send" in report["supported"]
        assert "status field" in report["unsupported_reasons"]["calendar.cancel"]
        assert "untrash tool" in report["unsupported_reasons"]["files.untrash"]
        assert report["verification"]["actions"]["mail.send"]["level"] == "catalog"

    def test_configured_identity(self, cfg):
        import company_registry

        assert company_registry.configured_identity(cfg, "google_mcp") == DELEGATE

    def test_connect_workspace_knows_the_provider(self):
        import connect_workspace

        args = connect_workspace.build_parser().parse_args(["--provider", "google_mcp"])
        assert args.provider == "google_mcp"

    def test_connect_workspace_setup_check(self, cfg, capsys):
        import connect_workspace

        with patch.object(gmw.GoogleMCPWorkspaceClient, "health_check", return_value=True):
            assert connect_workspace.cmd_provider_google_mcp(cfg) == 0
        out = capsys.readouterr().out
        assert DELEGATE in out and "gmailmcp.googleapis.com" in out

    def test_connect_workspace_setup_check_fails_without_delegate(self, cfg, capsys):
        import connect_workspace

        cfg["google"]["delegate_email"] = ""
        assert connect_workspace.cmd_provider_google_mcp(cfg) == 1

    def test_doctor_check(self, cfg, tmp_path):
        import doctor_base

        result = doctor_base._check_google_mcp(False, cfg, tmp_path / "company.yaml")
        assert result.name == "google_mcp" and result.status == "pass", result.detail
        assert "delegate_email" in result.detail

        other = doctor_base._check_google_mcp(False, {"integrations": {"workspace": {"provider": "m365"}}},
                                              tmp_path / "company.yaml")
        assert other.status == "pass" and "skipped" in other.detail

        cfg["google"]["service_account_path"] = str(tmp_path / "missing.json")
        bad = doctor_base._check_google_mcp(False, cfg, tmp_path / "company.yaml")
        assert bad.status == "warn"
        assert doctor_base._check_google_mcp in doctor_base.CHECKS

    def test_doctor_google_auth_skips_google_mcp(self, cfg, tmp_path):
        import doctor_base

        result = doctor_base._check_google_auth(False, cfg, tmp_path / "company.yaml")
        assert "google_mcp" in result.detail

    def test_bootstrap_overlay(self):
        from types import SimpleNamespace

        import bootstrap

        assert "google_mcp" in bootstrap.WORKSPACE_PROVIDERS
        overlay, required, notices, nxt = bootstrap._provider_overlay(
            SimpleNamespace(workspace_provider="google_mcp", operator=DELEGATE))
        assert overlay == {"integrations": {"workspace": {"provider": "google_mcp"}}}
        assert required == []
        assert any("google.delegate_email" in n and "service_account_path" in n for n in notices)
        assert any("--provider google_mcp" in c for c in nxt)
        assert any("google_mcp" in s or "MCP" in s for s in bootstrap._next_steps("google_mcp"))

    def test_example_config_documents_it(self):
        text = (PLUGIN_ROOT / "shared" / "config" / "company.yaml.example").read_text()
        assert "google_mcp" in text

    def test_session_hook_context(self, cfg):
        sys.path.insert(0, str(PLUGIN_ROOT))
        import hooks

        line = hooks._workspace_routing(cfg)
        assert line and "google_mcp" in line and DELEGATE in line
