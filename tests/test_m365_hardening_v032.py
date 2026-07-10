#!/usr/bin/env python3
"""v0.3.2 — M365 Graph Tier 1 operational hardening.

Exercises the three hardening features added to ``M365GraphClient``:

  1. 429/503/504 throttle backoff inside :meth:`_request` (retry up to
     MAX_RETRIES, honouring Retry-After, exponential fallback, 30s cap).
  2. ``@odata.nextLink`` pagination in the read methods (max_results /
     MAX_ITEMS / MAX_PAGES caps, absolute-URL passthrough, non-silent
     truncation).
  3. 401 token-refresh retry (clear token, re-acquire, retry once).

The retry tests drive a scripted fake ``_send`` (the raw-HTTP seam that
``_request`` delegates to) so the real throttle/refresh control flow runs
without network or ``msal``; ``_sleep`` is stubbed so nothing actually blocks.
The pagination tests patch ``_request`` directly (pagination lives above it).
No network, no msal — same posture as tests/test_m365_graph_v031.py.
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


# ── Fixtures / helpers ─────────────────────────────────────────────────

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
        "paths": {"project_root": "/tmp/test-m365-hardening"},
    }


@pytest.fixture
def client(m365_config):
    from providers.m365_graph import M365GraphClient
    c = M365GraphClient(m365_config)
    c._get_token = MagicMock(return_value="fake-token")
    # Record every wait instead of sleeping.
    c._slept: list[float] = []
    c._sleep = lambda s: c._slept.append(s)
    return c


@pytest.fixture
def approve_env():
    """AUTO_APPROVE + a real tmp project root so guarded writes execute and
    audit to disk (mirrors tests/test_m365_graph_v031.py::approve_env)."""
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
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status, *, json_body=None, headers=None, content=None):
        self.status_code = status
        self._json = {} if json_body is None else json_body
        self.headers = headers or {}
        self.text = ""
        if content is not None:
            self.content = content
        else:
            self.content = b'{"_": 1}'  # non-empty => success path calls json()

    def json(self):
        return self._json


def scripted_send(responses):
    """Return a fake _send(method, url, **kwargs) that yields ``responses`` in
    order and records each call on ``.calls``."""
    seq = list(responses)

    def _send(method, url, **kwargs):
        _send.calls.append((method, url, kwargs))
        return seq.pop(0)

    _send.calls = []
    return _send


# ── (1) Throttle backoff ───────────────────────────────────────────────

class TestThrottleBackoff:
    def test_429_with_retry_after_then_success(self, client):
        send = scripted_send([
            FakeResp(429, headers={"Retry-After": "7"}),
            FakeResp(200, json_body={"ok": True}),
        ])
        client._send = send
        out = client._request("GET", "/users/cos@acme.com/messages")
        assert out == {"ok": True}
        assert len(send.calls) == 2          # exactly one retry
        assert client._slept == [7.0]        # honoured the header

    def test_503_is_retried_with_exponential_backoff(self, client):
        send = scripted_send([
            FakeResp(503),
            FakeResp(200, json_body={"ok": 1}),
        ])
        client._send = send
        out = client._request("GET", "/x")
        assert out == {"ok": 1}
        assert len(send.calls) == 2
        assert client._slept == [1.0]        # 2**0, no Retry-After header

    def test_retry_after_over_budget_defers_no_retry(self, client):
        # REWORKED (was test_retry_after_capped_at_30s, which asserted the old
        # wrong behaviour of sleeping a SHORTENED 30s and retrying). Policy now:
        # a Retry-After that exceeds the 30s budget is DEFERRED — no sleep, no
        # retry, raise naming the server-requested wait.
        send = scripted_send([
            FakeResp(429, headers={"Retry-After": "600"}),
            FakeResp(200, json_body={"ok": 1}),  # must never be reached
        ])
        client._send = send
        with pytest.raises(RuntimeError) as exc:
            client._request("GET", "/x")
        assert "600" in str(exc.value)
        assert "defer" in str(exc.value).lower()
        assert len(send.calls) == 1          # attempted exactly once
        assert client._slept == []           # never slept a shortened wait

    def test_retry_after_exactly_at_budget_is_slept_full(self, client):
        # Boundary: wait == RETRY_MAX_WAIT_S (30) is within budget -> full sleep.
        send = scripted_send([
            FakeResp(429, headers={"Retry-After": "30"}),
            FakeResp(200, json_body={"ok": 1}),
        ])
        client._send = send
        out = client._request("GET", "/x")
        assert out == {"ok": 1}
        assert client._slept == [30.0]
        assert len(send.calls) == 2

    def test_post_429_sleeps_full_retry_after_and_retries(self, client):
        # 429 retries ALL methods (Graph docs: throttled request NOT processed).
        # The small Retry-After is honoured in FULL, then the POST is retried.
        send = scripted_send([
            FakeResp(429, headers={"Retry-After": "5"}),
            FakeResp(200, json_body={"ok": 1}),
        ])
        client._send = send
        out = client._request("POST", "/users/cos@acme.com/sendMail",
                              json_body={"message": {}})
        assert out == {"ok": 1}
        assert client._slept == [5.0]        # full header value, not shortened
        assert len(send.calls) == 2

    @pytest.mark.parametrize("bad", ["-5", "NaN", "inf", "not-a-number"])
    def test_invalid_retry_after_falls_back_to_exponential(self, client, bad):
        # Negative / NaN / infinite / garbage Retry-After -> treat as absent and
        # fall back to finite exponential backoff. Never a NaN/negative sleep.
        send = scripted_send([
            FakeResp(503, headers={"Retry-After": bad}),
            FakeResp(200, json_body={"ok": 1}),
        ])
        client._send = send
        out = client._request("GET", "/x")   # GET is idempotent -> 503 retries
        assert out == {"ok": 1}
        assert client._slept == [1.0]         # 2**0 fallback, finite & positive
        assert all(s >= 0.0 and s == s and s != float("inf") for s in client._slept)
        assert len(send.calls) == 2

    # ── Method-aware 503/504 (non-idempotent writes must NOT auto-retry) ────

    def test_post_sendmail_504_not_retried_raises_verify_first(self, client):
        # 504 on a POST is ambiguous — sendMail may have gone out. No retry;
        # raise with verify-first guidance. Attempted exactly once.
        send = scripted_send([
            FakeResp(504),
            FakeResp(200, json_body={"ok": 1}),  # must never be reached
        ])
        client._send = send
        with pytest.raises(RuntimeError) as exc:
            client._request("POST", "/users/cos@acme.com/sendMail",
                           json_body={"message": {}})
        assert "may have completed" in str(exc.value)
        assert "504" in str(exc.value)
        assert len(send.calls) == 1           # attempted exactly once
        assert client._slept == []            # no backoff sleep

    def test_patch_503_not_retried_raises_verify_first(self, client):
        # PATCH is non-idempotent for this policy (categories/event updates).
        send = scripted_send([FakeResp(503), FakeResp(200, json_body={"ok": 1})])
        client._send = send
        with pytest.raises(RuntimeError) as exc:
            client._request("PATCH", "/users/cos@acme.com/messages/x",
                           json_body={"categories": []})
        assert "may have completed" in str(exc.value)
        assert len(send.calls) == 1

    def test_put_503_is_retried_idempotent(self, client):
        # PUT (upload) is idempotent by HTTP semantics -> 503 auto-retries.
        send = scripted_send([FakeResp(503), FakeResp(200, json_body={"ok": 1})])
        client._send = send
        out = client._request("PUT", "/users/cos@acme.com/drive/root:/f:/content",
                             content=b"x")
        assert out == {"ok": 1}
        assert client._slept == [1.0]
        assert len(send.calls) == 2

    def test_delete_504_is_retried_idempotent(self, client):
        # DELETE is idempotent -> 504 auto-retries.
        send = scripted_send([FakeResp(504), FakeResp(200, json_body={"ok": 1})])
        client._send = send
        out = client._request("DELETE", "/users/cos@acme.com/drive/items/x")
        assert out == {"ok": 1}
        assert client._slept == [1.0]
        assert len(send.calls) == 2

    def test_post_504_guarded_write_is_ambiguous_audited_failure(self, client, approve_env):
        # POST calendar_create + 503 via the guarded path: attempted exactly
        # once, audited-failure ActionResult carrying the ambiguous message.
        send = scripted_send([FakeResp(503)])
        client._send = send
        result = client.calendar_create("Sync", "2026-07-10", "2026-07-10")
        assert result["success"] is False
        assert result["audited"] is True
        assert "may have completed" in result["error"]
        assert "503" in result["error"]
        assert len(send.calls) == 1           # NOT retried

    def test_429_exhausted_read_returns_empty(self, client):
        # Four 429s: initial + 3 retries, then behaves like any non-2xx.
        send = scripted_send([FakeResp(429) for _ in range(4)])
        client._send = send
        with pytest.warns(UserWarning):
            out = client.mail_search("is:unread")
        assert out == []
        assert len(send.calls) == 4          # MAX_RETRIES=3 -> 4 total
        assert client._slept == [1.0, 2.0, 4.0]

    def test_429_exhausted_guarded_write_is_audited_failure(self, client, approve_env):
        send = scripted_send([FakeResp(429) for _ in range(4)])
        client._send = send
        result = client.calendar_create("Sync", "2026-07-10", "2026-07-10")
        assert result["success"] is False
        assert result["audited"] is True
        assert "429" in result["error"]
        assert len(send.calls) == 4


# ── (2) Pagination ─────────────────────────────────────────────────────

ABS_NEXT = "https://graph.microsoft.com/v1.0/users/cos@acme.com/messages?$skiptoken=OPAQUE123"


def _patch_request_sequence(client, payloads):
    """Patch client._request to record calls and return ``payloads`` in order."""
    seq = list(payloads)
    calls: list[tuple] = []

    def _fake(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return seq.pop(0)

    client._request = _fake
    return calls


class TestPagination:
    def test_two_pages_concatenated_and_absolute_url_used_verbatim(self, client):
        page1 = {"value": [{"id": "m1"}, {"id": "m2"}], "@odata.nextLink": ABS_NEXT}
        page2 = {"value": [{"id": "m3"}, {"id": "m4"}]}
        calls = _patch_request_sequence(client, [page1, page2])
        out = client.mail_search("is:unread", max_results=10)
        assert [m["id"] for m in out] == ["m1", "m2", "m3", "m4"]
        # First page sized with $top; second page uses the absolute nextLink
        # verbatim with NO params re-applied.
        assert calls[0][2]["params"]["$top"] == 10
        assert calls[1][1] == ABS_NEXT
        assert "params" not in calls[1][2]

    def test_respects_max_results_mid_page(self, client):
        page1 = {"value": [{"id": "m1"}, {"id": "m2"}], "@odata.nextLink": ABS_NEXT}
        page2 = {"value": [{"id": "m3"}, {"id": "m4"}]}
        _patch_request_sequence(client, [page1, page2])
        with pytest.warns(UserWarning, match="max_results=3"):
            out = client.mail_search("is:unread", max_results=3)
        assert [m["id"] for m in out] == ["m1", "m2", "m3"]

    def test_max_pages_cap_emits_warning(self, client):
        import providers.m365_graph as m

        # _request always returns one event and another nextLink -> unbounded
        # were it not for MAX_PAGES.
        calls: list[tuple] = []

        def _fake(method, path, **kwargs):
            calls.append((method, path))
            n = len(calls)
            return {"value": [{"id": f"e{n}"}],
                    "@odata.nextLink": f"https://graph.microsoft.com/v1.0/next?p={n}"}

        client._request = _fake
        with pytest.warns(UserWarning, match=f"MAX_PAGES={m.MAX_PAGES}"):
            out = client.calendar_list("2026-07-10", "2026-07-11")
        assert len(calls) == m.MAX_PAGES
        assert len(out) == m.MAX_PAGES

    def test_single_page_no_nextlink_does_not_warn(self, client, recwarn):
        _patch_request_sequence(client, [{"value": [{"id": "m1"}]}])
        out = client.mail_search("is:unread", max_results=10)
        assert [m["id"] for m in out] == ["m1"]
        assert len(recwarn) == 0

    # ── nextLink origin check (never send the bearer token off-host) ────────

    @pytest.mark.parametrize("bad_link", [
        "http://graph.microsoft.com/v1.0/users/cos@acme.com/messages?$skiptoken=X",
        "https://evil.example/v1.0/users/cos@acme.com/messages?$skiptoken=X",
    ])
    def test_offhost_or_insecure_nextlink_stops_with_warning(self, client, bad_link):
        # page1 offers a nextLink that is either http:// or on a foreign host;
        # pagination must STOP after page1, warn, and never follow the link.
        page1 = {"value": [{"id": "m1"}, {"id": "m2"}], "@odata.nextLink": bad_link}
        page2 = {"value": [{"id": "m3"}]}  # must never be requested
        calls = _patch_request_sequence(client, [page1, page2])
        with pytest.warns(UserWarning, match="refusing to follow"):
            out = client.mail_search("is:unread", max_results=10)
        assert [m["id"] for m in out] == ["m1", "m2"]   # page-1 items only
        assert len(calls) == 1                          # page2 never fetched

    def test_valid_graph_nextlink_is_followed(self, client):
        # A well-formed https graph.microsoft.com nextLink IS followed.
        page1 = {"value": [{"id": "m1"}], "@odata.nextLink": ABS_NEXT}
        page2 = {"value": [{"id": "m2"}]}
        calls = _patch_request_sequence(client, [page1, page2])
        out = client.mail_search("is:unread", max_results=10)
        assert [m["id"] for m in out] == ["m1", "m2"]
        assert calls[1][1] == ABS_NEXT


# ── (3) 401 token-refresh retry ────────────────────────────────────────

class TestTokenRefreshRetry:
    def test_401_then_success_after_reacquire(self, client):
        client._token = "stale-token"           # simulate a cached token
        get_token = MagicMock(return_value="fresh-token")
        client._get_token = get_token
        send = scripted_send([
            FakeResp(401, json_body={"error": {"message": "expired"}}),
            FakeResp(200, json_body={"ok": 1}),
        ])
        client._send = send
        out = client._request("GET", "/x")
        assert out == {"ok": 1}
        assert len(send.calls) == 2
        assert get_token.call_count == 2        # re-acquired once
        assert client._token is None            # cache was cleared on the 401
        assert client._slept == []              # refresh is not a throttle wait

    def test_401_twice_fails(self, client):
        get_token = MagicMock(return_value="fresh-token")
        client._get_token = get_token
        send = scripted_send([
            FakeResp(401, json_body={"error": {"message": "expired"}}),
            FakeResp(401, json_body={"error": {"message": "still expired"}}),
        ])
        client._send = send
        with pytest.raises(RuntimeError) as exc:
            client._request("GET", "/x")
        assert "401" in str(exc.value)
        assert len(send.calls) == 2             # one refresh retry, no more
        assert get_token.call_count == 2


# ── Composition: 401 refresh + 429 throttle, independent budgets ────────

class TestComposition:
    def test_401_refresh_then_429_retry_then_success(self, client):
        get_token = MagicMock(return_value="fresh-token")
        client._get_token = get_token
        send = scripted_send([
            FakeResp(401, json_body={"error": {"message": "expired"}}),
            FakeResp(429, headers={"Retry-After": "3"}),
            FakeResp(200, json_body={"ok": 1}),
        ])
        client._send = send
        out = client._request("GET", "/x")
        assert out == {"ok": 1}
        assert len(send.calls) == 3
        # 401 refresh did NOT consume the throttle budget: exactly one throttle
        # wait, honouring Retry-After.
        assert client._slept == [3.0]
        # get_token: iter1, iter2 (post-clear), iter3.
        assert get_token.call_count == 3
