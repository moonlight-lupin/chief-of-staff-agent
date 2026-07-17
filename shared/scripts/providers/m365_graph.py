#!/usr/bin/env python3
"""Microsoft 365 (Graph API) backend for WorkspaceClient.

Implements the provider-neutral WorkspaceClient surface (mail_*, calendar_*,
files_*) over the Microsoft Graph REST API v1.0 using ``requests`` directly —
no Graph SDK.  Authentication uses ``msal``, imported LAZILY inside the auth
path so this module imports fine even when msal is not installed; a clear
RuntimeError naming ``pip install msal`` is raised only when a token is actually
requested.

Config (under the top-level ``m365`` key, with
``integrations.workspace.provider: m365``):

    m365:
      tenant_id: "<entra-tenant-guid>"
      client_id: "<app-registration-client-id>"
      client_secret_env: "M365_CLIENT_SECRET"   # env var holding the secret (default)
      auth: "client_credentials"                # or "device_code"
      user_principal: "user@tenant.com"         # mailbox UPN, REQUIRED for client_credentials
      token_cache_path: "~/.hermes/secrets/m365-token-cache.json"  # optional

Auth modes:
  * ``client_credentials`` (default) -> msal.ConfidentialClientApplication,
    scope ``["https://graph.microsoft.com/.default"]``, endpoints operate on
    ``/users/{user_principal}/...``.
  * ``device_code`` -> msal.PublicClientApplication with the device flow message
    printed to stderr, endpoints operate on ``/me/...``.

All HTTP is routed through :meth:`_request` and all token acquisition through
:meth:`_get_token` so both can be monkeypatched in unit tests (no network, no
msal required).  Non-2xx responses raise RuntimeError carrying the Graph status
and error message; the ``@guarded`` wrapper converts that into an audited
failure ActionResult.  Read methods (mail_search, calendar_list, files_search,
mail_list_tags) follow the Google provider's pattern: warn + return ``[]`` on
failure.

Operational behaviour (Tier 1 hardening):
  * **Method-aware throttle backoff.**  :meth:`_request` retries a request up to
    ``MAX_RETRIES`` (3) times, but the policy depends on both the status and the
    HTTP method (see :data:`IDEMPOTENT_METHODS`):
      - ``429`` retries for ALL methods (Graph documents a throttled request as
        NOT processed, so a retry cannot duplicate a write).
      - ``503`` / ``504`` auto-retry ONLY idempotent methods
        (``GET`` / ``PUT`` / ``DELETE``).  A ``504`` is ambiguous — the upstream
        may have completed a ``POST``/``PATCH`` write (sendMail, draft, event,
        move, category) even though the gateway timed out — so those methods are
        NOT retried; :meth:`_request` raises a ``RuntimeError`` carrying the
        status and verify-first guidance instead (guarded writes surface this as
        an audited-failure ActionResult, which is the desired UX).
      - ``401`` still retries once for all methods after a token refresh.
    **Retry-After is honoured, never shortened.**  A valid ``Retry-After`` (delta
    seconds, or an HTTP-date parsed to seconds-from-now) that is
    ``<= RETRY_MAX_WAIT_S`` (30) is slept in FULL and retried; one that EXCEEDS
    the 30s budget is DEFERRED — :meth:`_request` raises rather than sleep a
    shortened wait and keep counting against the throttle limit.  When the header
    is absent or invalid (non-numeric/non-date, negative, NaN, or infinite) the
    wait falls back to exponential ``1s / 2s / 4s``; only that fallback is capped
    at ``RETRY_MAX_WAIT_S``.  Sleeping goes through the injectable ``self._sleep``
    so tests never actually block.  The raw HTTP call lives below
    :meth:`_request` in :meth:`_send` (the monkeypatch seam for retry tests);
    once the retry budget is exhausted the request behaves exactly like any other
    non-2xx (reads warn + ``[]``, guarded writes → audited failure).
  * **Pagination.**  The list/read methods follow ``@odata.nextLink`` (an
    ABSOLUTE Graph URL, passed through verbatim — never re-prefixed with
    ``base_url``) until the collection is exhausted, the caller's
    ``max_results`` is reached (mail_search / files_search), or an internal cap
    is hit.  calendar_list / mail_list_tags cap at ``MAX_ITEMS`` (500); no read
    follows more than ``MAX_PAGES`` (10) links.  ``$top`` is still sent on the
    first request, sized to what is needed.  Stopping at a cap while a nextLink
    remains is never silent — it emits a ``warnings.warn`` naming the cap.  Each
    ``@odata.nextLink`` is origin-checked before it is followed: it must be
    ``https`` and its host must equal the configured Graph host, else pagination
    stops with a warning (the bearer token is never sent off-host).
  * **Token-refresh retry.**  On a ``401`` :meth:`_request` clears the cached
    token (``self._token = None``), re-acquires via :meth:`_get_token`, and
    retries the request ONCE.  A second ``401`` fails as before.  This refresh
    is independent of the throttle-retry budget above (a 401 refresh does not
    consume a throttle retry, and vice-versa).

Immutable ids:
  Every Graph request sent through :meth:`_request` carries the
  ``Prefer: IdType="ImmutableId"`` header.  By default Graph message ids CHANGE
  when a message moves folders (archive/trash); requesting immutable ids keeps
  them stable across moves so the generic soft-delete restore flow (which
  restores by the original/persisted id) still resolves after a move.  As a
  belt-and-braces measure, :meth:`mail_archive`/:meth:`mail_trash` also return a
  ``restore_target`` key (the post-move id) in their ActionResult data.

Provider differences vs Google/Composio:
  * Drafts ARE supported (POST /messages).
  * Sending is destructive and env-gated identically to gmail.send.
  * There is no calendar "uncancel" — Graph cannot reinstate a cancelled event.
    Because there is no restore path, ``calendar.cancel`` is NOT supported for
    m365: the capability is False (see workspace_capabilities.py) and
    :meth:`calendar_cancel` returns a failure ActionResult explaining to cancel
    via Outlook or delete+recreate the event (it does not raise).
    :meth:`calendar_uncancel` likewise raises NotImplementedError.
  * Change-notification webhooks are intentionally deferred — polling only.
"""
from __future__ import annotations

import math
import sys
import time
import warnings
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import requests

_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from workspace_client import WorkspaceClient
from workspace_guardrails import guarded
from query_compiler import compile_query

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
DEFAULT_SCOPE = ["https://graph.microsoft.com/.default"]

# ── Operational hardening constants (Tier 1) ──────────────────────────────
# Method-aware throttle backoff (see _request):
#   * 429   -> retry ALL methods (Graph documents a throttled request as NOT
#              processed), subject to the Retry-After rules below.
#   * 503/504 -> auto-retry ONLY idempotent methods (GET/PUT/DELETE). A 504 is
#              ambiguous — the upstream may have completed the write even though
#              the gateway timed out — so POST/PATCH are NOT retried; they raise
#              with verify-first guidance instead.
# Retry-After is honoured, never shortened: a valid header <= RETRY_MAX_WAIT_S is
# slept in FULL and retried; a valid header > RETRY_MAX_WAIT_S is DEFERRED (raise,
# never sleep a shortened wait). Only the exponential fallback (used when the
# header is absent/invalid) is capped at RETRY_MAX_WAIT_S.
RETRYABLE_STATUS = (429, 503, 504)
MAX_RETRIES = 3
RETRY_MAX_WAIT_S = 30
# HTTP methods that are safe to auto-retry on an ambiguous 503/504: PUT (upload)
# and DELETE are idempotent by HTTP semantics; POST and PATCH are not.
IDEMPOTENT_METHODS = frozenset({"GET", "PUT", "DELETE"})
# Surfaced when a non-idempotent write (POST/PATCH) hits an ambiguous 503/504:
# the gateway timed out but the upstream may already have applied the write.
AMBIGUOUS_WRITE_GUIDANCE = (
    "The request may have completed, but confirmation was not received. "
    "Do not retry automatically; verify the external system first."
)
# Pagination safety caps (follow @odata.nextLink). max_results-bearing reads
# stop at the caller's max_results; the internal reads (calendar_list,
# mail_list_tags) stop at MAX_ITEMS. No read follows more than MAX_PAGES links.
# Hitting a cap while a nextLink remains is never silent — it warns.
MAX_PAGES = 10
MAX_ITEMS = 500
DEVICE_SCOPE = [
    "https://graph.microsoft.com/Mail.ReadWrite",
    "https://graph.microsoft.com/Calendars.ReadWrite",
    "https://graph.microsoft.com/Files.ReadWrite.All",
    "https://graph.microsoft.com/User.Read",
]


# Neutral mail.*/files.* guardrail action ids used by this provider
# (mail.send destructive; mail.draft / calendar.create|update / files.upload|download
# safe writes; archive/trash/tag/cancel ungated, mirroring the Google provider)
# are classified directly in workspace_guardrails.py WRITE/DESTRUCTIVE/SAFE sets.


# ── Operational instrumentation helpers (v0.3.4) ──────────────────────────
# These emit into the structured runtime log (shared/scripts/runtime_log.py).
# They are unconditional and zero-cost-safe: log_event silently no-ops when no
# run is active, and this wrapper swallows any import/logging error so provider
# behaviour is never affected. Request bodies/params are NEVER passed in.
def _log_event(event: str, **fields: Any) -> None:
    """Best-effort structured runtime log. No-ops when runtime_log is
    unavailable or no run is active; never raises into the caller."""
    try:
        from runtime_log import log_event
        log_event(event, **fields)
    except Exception:  # pragma: no cover - logging must never break the caller
        pass


def _endpoint_category(path: str) -> str:
    """Coarse Graph endpoint bucket derived from the request path only (never
    the query string): mail | calendar | files | categories | user | other."""
    p = str(path or "").lower()
    if "mastercategories" in p:
        return "categories"
    if "/messages" in p or "/mailfolders" in p or "/sendmail" in p:
        return "mail"
    if "/calendarview" in p or "/events" in p or "/calendar" in p:
        return "calendar"
    if "/drive" in p:
        return "files"
    if "/users/" in p or p.rstrip("/").endswith("/me") or "/me/" in p:
        return "user"
    return "other"


def _operation_from_path(method: str, path: str) -> str:
    """Best-effort operation label from an HTTP method + Graph path, used when a
    caller supplies no explicit operation. Query strings are stripped so no
    filter/search text (which can contain client names) is ever logged."""
    raw = str(path or "")
    if raw.startswith("http"):
        try:
            raw = urlparse(raw).path
        except (ValueError, TypeError):
            raw = ""
    raw = raw.split("?", 1)[0]
    seg = raw.rstrip("/").rsplit("/", 1)[-1]
    return f"{str(method).upper()} {seg}" if seg else str(method).upper()


def _error_class_from_status(status: int | None, code: str | None = None) -> str:
    """Map a Graph (status, error-code) to a diagnostics-aligned error_class."""
    if code and str(code).lower() == "erroraccessdenied":
        return "permission_denied"
    if status == 429:
        return "throttled"
    if status == 401:
        return "auth"
    if status == 403:
        return "permission_denied"
    if status == 404:
        return "not_found"
    if status in (503, 504):
        # A 503/504 is a server-side OUTAGE, not client rate-limiting; mislabelling
        # it "throttled" produces misleading rate-limit diagnoses for real outages.
        return "provider_unavailable"
    if status is not None and status >= 500:
        return "network"
    return "other"


def _split_addrs(value: str | None) -> list[str]:
    if not value:
        return []
    return [a.strip() for a in str(value).split(",") if a.strip()]


def _recipients(value: str | None) -> list[dict[str, Any]]:
    return [{"emailAddress": {"address": a}} for a in _split_addrs(value)]


def _graph_datetime(value: str, default_time: str = "10:00:00") -> str:
    """Normalise a date/datetime into a Graph dateTime (no trailing Z).

    Graph's event dateTime is paired with a separate timeZone, so a trailing
    ``Z`` must be stripped.  Bare dates are padded with ``default_time``.
    """
    v = str(value)
    if "T" not in v:
        v = f"{v}T{default_time}"
    if v.endswith("Z"):
        v = v[:-1]
    return v


class M365GraphClient(WorkspaceClient):
    """Microsoft 365 provider over Graph REST v1.0."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self._provider_name = "m365"
        self.base_url = GRAPH_BASE

        m365 = config.get("m365", {}) if isinstance(config, Mapping) else {}
        if not isinstance(m365, Mapping):
            m365 = {}
        self.tenant_id = str(m365.get("tenant_id", "") or "")
        self.client_id = str(m365.get("client_id", "") or "")
        self.client_secret_env = str(m365.get("client_secret_env", "M365_CLIENT_SECRET") or "M365_CLIENT_SECRET")
        self.auth_mode = str(m365.get("auth", "client_credentials") or "client_credentials")
        self.user_principal = str(m365.get("user_principal", "") or "")
        tcp = m365.get("token_cache_path")
        self.token_cache_path = str(tcp) if tcp else ""

        self._token: str | None = None
        self._spo_tokens: dict[str, str] = {}
        self._msal_app = None
        # Injectable so retry/backoff tests don't actually block. Overridden in
        # tests with a fake that just records the requested wait.
        self._sleep = time.sleep

    # ── User base path ────────────────────────────────────────────────

    def _user_base(self) -> str:
        """Return the Graph path prefix for the active identity."""
        if self.auth_mode == "device_code":
            return "/me"
        if not self.user_principal:
            raise RuntimeError(
                "m365 client_credentials auth requires m365.user_principal "
                "(the mailbox UPN, e.g. user@tenant.com)"
            )
        return f"/users/{self.user_principal}"

    # ── Auth (msal imported lazily) ───────────────────────────────────

    def _get_token(self) -> str:
        """Acquire a Graph access token. Raises RuntimeError on any failure."""
        if self._token:
            return self._token
        try:
            import msal  # noqa: F401  — lazy: import only when auth is attempted
        except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
            raise RuntimeError(
                "Microsoft 365 provider requires the 'msal' package. "
                "Install it with: pip install msal"
            ) from exc

        import os

        authority = f"https://login.microsoftonline.com/{self.tenant_id}"

        if self.auth_mode == "device_code":
            app = msal.PublicClientApplication(self.client_id, authority=authority)
            flow = app.initiate_device_flow(scopes=DEVICE_SCOPE)
            if "user_code" not in flow:
                raise RuntimeError(f"Failed to start device flow: {flow.get('error_description', flow)}")
            print(flow.get("message", "Complete device sign-in in your browser."), file=sys.stderr)
            result = app.acquire_token_by_device_flow(flow)
        else:
            secret = os.getenv(self.client_secret_env, "")
            if not secret:
                raise RuntimeError(
                    f"m365 client_credentials auth requires the client secret in "
                    f"env var {self.client_secret_env}"
                )
            if not self.tenant_id or not self.client_id:
                raise RuntimeError("m365 config requires tenant_id and client_id")
            app = msal.ConfidentialClientApplication(
                self.client_id, authority=authority, client_credential=secret
            )
            result = app.acquire_token_for_client(scopes=DEFAULT_SCOPE)

        token = result.get("access_token") if isinstance(result, Mapping) else None
        if not token:
            err = result.get("error_description") if isinstance(result, Mapping) else result
            raise RuntimeError(f"Failed to acquire Microsoft Graph token: {err}")
        self._token = token
        return token

    # ── HTTP (single seam) ────────────────────────────────────────────

    @staticmethod
    def _error_body(resp: "requests.Response") -> tuple[str | None, str | None]:
        """Return ``(message, code)`` parsed from a Graph error payload
        (``{"error": {"code": ..., "message": ...}}``); ``(None, None)`` when the
        body is absent or not JSON."""
        try:
            payload = resp.json()
        except Exception:
            return None, None
        err = payload.get("error", {}) if isinstance(payload, Mapping) else {}
        if not isinstance(err, Mapping):
            return None, None
        return err.get("message"), err.get("code")

    @staticmethod
    def _error_message(resp: "requests.Response") -> str:
        msg, _ = M365GraphClient._error_body(resp)
        return f"Graph API {resp.status_code}: {msg or (getattr(resp, 'text', '') or '').strip()[:300]}"

    # ── Permission-specific error diagnosis ───────────────────────────
    @staticmethod
    def _permission_hint(
        status: int,
        path: str,
        error_code: str | None,
        secret_env: str = "M365_CLIENT_SECRET",
    ) -> str | None:
        """Map a Graph failure ``(status, request path, Graph error code)`` to an
        actionable, provider-specific remediation hint, or ``None`` when nothing
        specific applies.

        Pure and deterministic given its inputs (``path`` matching is
        case-insensitive substring). ``secret_env`` names the env var that holds
        the client secret so the 401 hint can point at the right variable.

        On a real Entra tenant the most common failure is a partially-granted or
        un-consented application permission, so these hints steer the operator to
        the exact API-permission / admin-consent / provisioning fix. The caller
        APPENDS the hint to the existing ``Graph API {status}: {message}`` text —
        it never replaces it.
        """
        p = str(path or "").lower()
        code = str(error_code or "")
        access_denied = code.lower() == "erroraccessdenied"

        # Calendar: 403 (or ErrorAccessDenied) on calendarView/events.
        if (status == 403 or access_denied) and ("/calendarview" in p or "/events" in p):
            return ("hint: Calendars.ReadWrite permission or mailbox calendar "
                    "access is missing.")
        # Mail: 403 on the messages / mailFolders surfaces.
        if status == 403 and ("/messages" in p or "/mailfolders" in p):
            return ("hint: Mail.Read/Mail.ReadWrite application permission may be "
                    "missing, or admin consent has not been granted (Entra: App "
                    "registration → API permissions → Grant admin consent).")
        # Files: 403 on any OneDrive/drive surface.
        if status == 403 and "/drive" in p:
            return ("hint: Files.ReadWrite.All permission may be missing, or "
                    "OneDrive is not provisioned for this user (the user may need "
                    "to open OneDrive once).")
        # User lookup: 404 on a /users/ path (wrong UPN or unreadable user).
        if status == 404 and "/users/" in p:
            return ("hint: m365.user_principal may be incorrect, or the app cannot "
                    "read this user (check the UPN and User.Read.All permission).")
        # Credentials rejected: a 401 only reaches the raise/warn path AFTER the
        # single token-refresh retry has already failed (the first 401 is
        # intercepted and retried in _request), so any 401 seen here is a genuine
        # credential/consent failure.
        if status == 401:
            return ("hint: credentials rejected — check tenant_id/client_id and "
                    f"that the client secret in {secret_env} is current "
                    "(secrets expire).")
        return None

    def _raise_for_status(self, resp: "requests.Response", url: str) -> None:
        """Raise a ``RuntimeError`` for a non-2xx Graph response, APPENDING a
        permission-specific remediation hint when one applies.

        The base ``Graph API {status}: {message}`` text is preserved verbatim
        (many callers/tests assert on it); the hint is only ever appended. Reads
        that warn+return ``[]`` and guarded writes that convert the exception into
        an audited-failure ``ActionResult`` both inherit the hint automatically,
        since they surface this exception's text.
        """
        base = self._error_message(resp)
        _, code = self._error_body(resp)
        hint = self._permission_hint(resp.status_code, url, code, self.client_secret_env)
        raise RuntimeError(f"{base} {hint}" if hint else base)

    def _send(self, method: str, url: str, **kwargs: Any) -> "requests.Response":
        """Lowest HTTP seam: the single raw ``requests.request`` call.

        Kept as its own method so the throttle/refresh retry loop in
        :meth:`_request` can drive it, and so retry tests can monkeypatch
        ``self._send`` with a scripted sequence of canned responses while the
        OLD tests that patch ``requests.request`` (via this call) or patch
        ``_request`` wholesale continue to work unchanged.
        """
        return requests.request(method, url, **kwargs)

    @staticmethod
    def _parse_retry_after(resp: "requests.Response") -> float | None:
        """Parse and sanitize the ``Retry-After`` response header.

        Returns the server-requested wait in seconds (finite, ``>= 0``) when the
        header is present and VALID; returns ``None`` when the header is absent
        or INVALID, signalling the caller to fall back to exponential backoff.

        Accepted forms:
          * delta-seconds — an integer/float number of seconds; and
          * HTTP-date (RFC 7231) — parsed via
            :func:`email.utils.parsedate_to_datetime` and converted to
            seconds-from-now, clamped to ``>= 0`` (a date already in the past
            means "retry now").

        Treated as INVALID (→ ``None`` → exponential fallback): a value that is
        neither numeric nor a parseable HTTP-date, a NEGATIVE delta-seconds, or a
        NaN/infinite value.  We never sleep a NaN/negative wait, and we never
        shorten a valid wait here — the ``> RETRY_MAX_WAIT_S`` defer decision is
        made by the caller so it can raise instead of retry.
        """
        headers = getattr(resp, "headers", None) or {}
        raw = headers.get("Retry-After")
        if raw is None:
            return None
        # Prefer the delta-seconds form.
        parsed: float | None
        try:
            parsed = float(raw)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            # Numeric: reject NaN/inf and negative deltas (→ fallback).
            if not math.isfinite(parsed) or parsed < 0:
                return None
            return parsed
        # Not numeric: try the HTTP-date form.
        try:
            dt = parsedate_to_datetime(str(raw))
        except (TypeError, ValueError):
            dt = None
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        if not math.isfinite(delta):
            return None
        return max(0.0, delta)

    def _is_same_graph_origin(self, url: str) -> bool:
        """True iff ``url`` is https and its host equals the configured Graph
        host (parsed from ``self.base_url``).

        Used to gate ``@odata.nextLink`` following so the bearer token is never
        sent off-host (or over cleartext http).
        """
        try:
            target = urlparse(url)
            base = urlparse(self.base_url)
        except (ValueError, TypeError):
            return False
        return target.scheme == "https" and bool(target.hostname) and \
            target.hostname == base.hostname

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        raw: bool = False,
        timeout: int = 30,
        operation: str | None = None,
        degrade: bool = False,
    ) -> Any:
        """Perform one Graph request. Returns parsed JSON dict (or raw bytes if
        ``raw=True``). Raises RuntimeError on non-2xx responses.

        Wraps the raw call (:meth:`_send`) with two orthogonal retry policies:
          * throttle backoff on 429/503/504 (up to ``MAX_RETRIES``), and
          * a single token-refresh retry on 401.
        The two budgets are independent — a 401 refresh does not consume a
        throttle retry. ``path`` may be a relative Graph path (prefixed with
        ``base_url``) OR an absolute ``https://`` URL (an ``@odata.nextLink``),
        which is used verbatim.
        """
        # nextLink is an absolute URL — do NOT re-prefix base_url.
        url = path if str(path).startswith("http") else f"{self.base_url}{path}"
        m = str(method).upper()
        op = operation or _operation_from_path(method, path)
        category = _endpoint_category(path)
        provider = self._provider_name
        # Operating-boundary log: what we are ATTEMPTING (no bodies/params).
        _log_event(
            "provider_request_started", level="debug", component="m365",
            provider=provider, operation=op, method=m, endpoint_category=category,
        )
        token_refreshed = False
        throttle_attempts = 0
        attempt = 0
        while True:
            attempt += 1
            token = self._get_token()
            # Prefer immutable ids on EVERY Graph request. By default Graph
            # message ids change when a message moves folders (archive/trash),
            # which would break the generic restore flow that restores by the
            # original target id. Requesting IdType="ImmutableId" makes ids
            # stable across moves so a persisted id/restore_target still
            # resolves after archive/trash.
            hdrs = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Prefer": 'IdType="ImmutableId"',
            }
            if headers:
                hdrs.update(headers)
            _start = time.monotonic()
            # Wrap the raw transport so requests.Timeout / ConnectionError / TLS
            # errors surface a provider_request_failed (error_class=network) event
            # before they propagate — otherwise the analyser's network_timeout rule
            # could never fire on the real path. Behaviour is unchanged: the
            # exception is re-raised exactly as before (reads warn+[]; guarded
            # writes → audited failure).
            try:
                resp = self._send(
                    method, url, params=params, json=json_body, data=content,
                    headers=hdrs, timeout=timeout,
                )
            except requests.RequestException as exc:
                duration_ms = int((time.monotonic() - _start) * 1000)
                _log_event(
                    "provider_request_failed",
                    level="warning" if degrade else "error", component="m365",
                    provider=provider, operation=op, method=m,
                    endpoint_category=category, duration_ms=duration_ms,
                    attempt=attempt, error_class="network",
                    exception_type=type(exc).__name__,
                    message=str(exc)[:200],
                )
                raise
            duration_ms = int((time.monotonic() - _start) * 1000)
            status = resp.status_code

            # 401: refresh the token once, then retry (independent of throttle).
            if status == 401 and not token_refreshed:
                token_refreshed = True
                self._token = None
                _log_event(
                    "provider_retry", level="warning", component="m365",
                    provider=provider, operation=op, status_code=401,
                    attempt=attempt, wait_s=0, reason="token_refresh",
                )
                continue

            # 429 / 503 / 504: method-aware throttle backoff, honouring
            # Retry-After (never shortened).
            if status in RETRYABLE_STATUS:
                is_idempotent = m in IDEMPOTENT_METHODS
                # 503/504 on a non-idempotent method (POST/PATCH) is AMBIGUOUS —
                # the write may have completed. Never auto-retry; raise with
                # verify-first guidance (guarded writes → audited failure).
                if status in (503, 504) and not is_idempotent:
                    _log_event(
                        "ambiguous_write", level="error", component="m365",
                        provider=provider, operation=op, status_code=status,
                        method=m,
                    )
                    raise RuntimeError(
                        f"Graph API {status}: {AMBIGUOUS_WRITE_GUIDANCE}"
                    )
                # 429 (any method) and 503/504 (idempotent) are retryable.
                if throttle_attempts < MAX_RETRIES:
                    wait = self._parse_retry_after(resp)
                    if wait is not None:
                        # Valid Retry-After: honour it in full, or DEFER if it
                        # exceeds the auto-retry budget (never shorten + retry).
                        if wait > RETRY_MAX_WAIT_S:
                            _log_event(
                                "retry_deferred", level="warning",
                                component="m365", provider=provider,
                                operation=op, status_code=status,
                                retry_after_s=wait,
                            )
                            raise RuntimeError(
                                f"Graph API {status}: Graph requested "
                                f"Retry-After={wait:g}s which exceeds the "
                                f"{RETRY_MAX_WAIT_S}s auto-retry budget; "
                                f"deferred — retry later"
                            )
                        reason = "retry_after_header"
                    else:
                        # Header absent/invalid: exponential 1s/2s/4s, capped.
                        wait = min(float(2 ** throttle_attempts),
                                   float(RETRY_MAX_WAIT_S))
                        reason = "exponential_backoff"
                    _log_event(
                        "provider_retry", level="warning", component="m365",
                        provider=provider, operation=op, status_code=status,
                        attempt=attempt, wait_s=wait, reason=reason,
                    )
                    self._sleep(wait)
                    throttle_attempts += 1
                    continue

            if not (200 <= status < 300):
                _msg, _code = self._error_body(resp)
                _log_event(
                    "provider_request_failed",
                    level="warning" if degrade else "error", component="m365",
                    provider=provider, operation=op, method=m,
                    endpoint_category=category, status_code=status,
                    duration_ms=duration_ms, attempt=attempt,
                    error_class=_error_class_from_status(status, _code),
                    message=(str(_msg) if _msg else "")[:200],
                )
                self._raise_for_status(resp, url)

            # Success (2xx). result_count only when the collection is cheaply
            # available (a "value" list); omitted otherwise.
            result_count = None
            if not raw and not (status == 204 or not (resp.content or b"")):
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if isinstance(data, Mapping) and isinstance(data.get("value"), list):
                    result_count = len(data["value"])
            else:
                data = None
            _log_event(
                "provider_request_completed", level="debug", component="m365",
                provider=provider, operation=op, method=m,
                endpoint_category=category, status_code=status,
                duration_ms=duration_ms, attempt=attempt,
                result_count=result_count,
            )
            if raw:
                return resp.content
            if status == 204 or not (resp.content or b""):
                return {}
            if data is None:
                return {}
            return data

    def _paged_values(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        max_items: int | None,
        context: str,
    ) -> list[dict[str, Any]]:
        """Follow ``@odata.nextLink`` collecting the ``value`` arrays.

        Stops when the collection is exhausted, ``max_items`` is reached, or
        ``MAX_PAGES`` links have been followed. ``nextLink`` is an absolute URL
        passed straight to :meth:`_request` (no params) — but only after an
        origin check (:meth:`_is_same_graph_origin`): a nextLink that is not
        https on the configured Graph host warns and STOPS pagination so the
        bearer token is never sent off-host. Hitting a cap while a nextLink
        still remains warns (never silent truncation). ``context`` names the
        calling method for the warning text.
        """
        items: list[dict[str, Any]] = []
        next_link: str | None = None
        for page in range(MAX_PAGES):
            if next_link:
                data = self._request(method, next_link, operation=context, degrade=True)
            else:
                data = self._request(method, path, params=params, operation=context, degrade=True)
            value = data.get("value", []) if isinstance(data, Mapping) else []
            items.extend(v for v in value if isinstance(v, Mapping))
            next_link = data.get("@odata.nextLink") if isinstance(data, Mapping) else None

            # Origin-check the absolute nextLink before following it: it must be
            # https AND on the configured Graph host, else we would leak the
            # bearer token off-host/over cleartext. On violation, warn and STOP
            # (return what we have) — treat it as "no more pages".
            if next_link and not self._is_same_graph_origin(next_link):
                warnings.warn(
                    f"m365 {context}: refusing to follow @odata.nextLink "
                    f"{next_link!r} — not https on the Graph host; stopping "
                    f"pagination"
                )
                return items[:max_items] if max_items is not None else items

            if max_items is not None and len(items) >= max_items:
                had_more = bool(next_link) or len(items) > max_items
                if had_more:
                    warnings.warn(
                        f"m365 {context}: truncated at max_results={max_items} "
                        f"cap; more results were available"
                    )
                    _log_event(
                        "pagination_truncated", level="warning", component="m365",
                        provider=self._provider_name, operation=context,
                        cap=max_items, pages_followed=page + 1,
                    )
                return items[:max_items]
            if not next_link:
                return items
        # Fell out of the loop => MAX_PAGES followed with a nextLink still set.
        if next_link:
            warnings.warn(
                f"m365 {context}: stopped after MAX_PAGES={MAX_PAGES} pages; "
                f"more results were available"
            )
            _log_event(
                "pagination_truncated", level="warning", component="m365",
                provider=self._provider_name, operation=context,
                cap=MAX_PAGES, pages_followed=MAX_PAGES,
            )
        return items

    # ── Normalisers ───────────────────────────────────────────────────

    @staticmethod
    def _normalize_message(m: Mapping[str, Any]) -> dict[str, Any]:
        sender = ""
        frm = m.get("from") or {}
        if isinstance(frm, Mapping):
            addr = frm.get("emailAddress", {}) or {}
            sender = addr.get("address", "") or addr.get("name", "") or ""
        out: dict[str, Any] = {
            "id": m.get("id"),
            # schemas.validate_message requires non-empty sender/subject/date;
            # Graph drafts and some system notifications omit "from", subjects
            # can be blank, and drafts have no receivedDateTime.
            "sender": sender or "unknown",
            "subject": m.get("subject") or "(no subject)",
            "date": m.get("receivedDateTime") or m.get("sentDateTime") or m.get("createdDateTime") or "",
            "source": "outlook",
        }
        if m.get("conversationId"):
            out["thread_id"] = m.get("conversationId")
        if m.get("bodyPreview") is not None:
            out["snippet"] = m.get("bodyPreview")
        if m.get("categories") is not None:
            out["tags"] = list(m.get("categories") or [])
        if m.get("hasAttachments") is not None:
            out["has_attachments"] = bool(m.get("hasAttachments"))
        if m.get("webLink"):
            out["link"] = m.get("webLink")
        return out

    @staticmethod
    def _normalize_event(e: Mapping[str, Any]) -> dict[str, Any]:
        start = (e.get("start", {}) or {}).get("dateTime")
        end = (e.get("end", {}) or {}).get("dateTime")
        attendees = []
        for a in e.get("attendees", []) or []:
            addr = ((a.get("emailAddress", {}) or {}).get("address")) if isinstance(a, Mapping) else None
            if addr:
                attendees.append(addr)
        organizer = ((e.get("organizer", {}) or {}).get("emailAddress", {}) or {}).get("address")
        location = (e.get("location", {}) or {}).get("displayName")
        conference_link = ((e.get("onlineMeeting", {}) or {}).get("joinUrl")) or location
        out: dict[str, Any] = {
            "id": e.get("id"),
            "title": e.get("subject", "") or "",
            "start": start,
            "end": end,
            "source": "outlook",
        }
        if attendees:
            out["attendees"] = attendees
        if organizer:
            out["organizer"] = organizer
        if location:
            out["location"] = location
        if conference_link:
            out["conference_link"] = conference_link
        if e.get("isCancelled") is not None:
            out["status"] = "cancelled" if e.get("isCancelled") else (e.get("showAs") or "confirmed")
        elif e.get("showAs"):
            out["status"] = e.get("showAs")
        return out

    @staticmethod
    def _normalize_file(f: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": f.get("id"),
            "name": f.get("name"),
            "source": "onedrive",
        }
        file_facet = f.get("file") or {}
        if isinstance(file_facet, Mapping) and file_facet.get("mimeType"):
            out["mime_type"] = file_facet.get("mimeType")
        if f.get("lastModifiedDateTime"):
            out["modified"] = f.get("lastModifiedDateTime")
        if f.get("webUrl"):
            out["link"] = f.get("webUrl")
        parent = f.get("parentReference") or {}
        if isinstance(parent, Mapping) and parent.get("id"):
            out["parents"] = [parent.get("id")]
        return out

    # ── Mail: reads ───────────────────────────────────────────────────

    def mail_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        try:
            compiled = compile_query(query, "m365")
            params: dict[str, Any] = {"$top": max_results}
            if compiled.get("filter"):
                params["$filter"] = compiled["filter"]
            if compiled.get("search"):
                params["$search"] = f'"{compiled["search"]}"'
            # Folder scope is carried out-of-band: Graph scopes folders by URL
            # path (/mailFolders/{well-known-or-id}/messages), NOT by $filter.
            folder = compiled.get("folder")
            if folder:
                path = f"{self._user_base()}/mailFolders/{folder}/messages"
            else:
                path = f"{self._user_base()}/messages"
            value = self._paged_values(
                "GET", path, params=params, max_items=max_results,
                context="mail_search",
            )
            return [self._normalize_message(m) for m in value if isinstance(m, Mapping)]
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"m365 mail_search failed: {exc}")
            return []

    def mail_list_tags(self) -> list[dict[str, Any]]:
        """List Outlook master categories. Read-only. For m365 the tag id IS the
        category displayName."""
        try:
            value = self._paged_values(
                "GET", f"{self._user_base()}/outlook/masterCategories",
                max_items=MAX_ITEMS, context="mail_list_tags",
            )
            out = []
            for c in value:
                if not isinstance(c, Mapping):
                    continue
                name = c.get("displayName", "")
                out.append({
                    "id": name,                # tag id == displayName for m365
                    "name": name,
                    "displayName": name,
                    "color": c.get("color"),
                    "graph_id": c.get("id"),
                })
            return out
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"m365 mail_list_tags failed: {exc}")
            return []

    # ── Mail: writes ──────────────────────────────────────────────────

    @guarded("mail.draft", target_arg="to", audit_provider="m365", audit_tool="graph_api")
    def mail_create_draft(self, to: str, subject: str, body: str,
                          cc: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": _recipients(to),
        }
        if cc:
            payload["ccRecipients"] = _recipients(cc)
        return self._request("POST", f"{self._user_base()}/messages", json_body=payload)

    @guarded("mail.send", target_arg="to", audit_provider="m365", audit_tool="graph_api",
             block_error=(
                 "cancelled by guardrail (requires explicit user approval; "
                 "use send_email.py prepare→approve→execute or set "
                 "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1)"
             ))
    def mail_send(self, to: str, subject: str, body: str,
                  cc: str | None = None) -> dict[str, Any]:
        message: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": _recipients(to),
        }
        if cc:
            message["ccRecipients"] = _recipients(cc)
        self._request("POST", f"{self._user_base()}/sendMail",
                      json_body={"message": message, "saveToSentItems": True})
        return {"status": "sent", "to": to}

    def mail_list_folders(self, include_hidden: bool = False,
                          max_results: int = 100) -> list[dict[str, Any]]:
        """List top-level mail folders (GET /mailFolders)."""
        params: dict[str, Any] = {
            "$top": max_results,
            "$select": "id,displayName,parentFolderId,childFolderCount,"
                       "unreadItemCount,totalItemCount,isHidden",
        }
        # Graph hides isHidden=true folders unless includeHiddenFolders=true.
        if include_hidden:
            params["includeHiddenFolders"] = "true"
        data = self._request("GET", f"{self._user_base()}/mailFolders", params=params)
        value = data.get("value", []) if isinstance(data, Mapping) else []
        out: list[dict[str, Any]] = []
        for f in value or []:
            if not isinstance(f, Mapping):
                continue
            out.append({
                "id": f.get("id"),
                "name": f.get("displayName") or "",
                "parent_id": f.get("parentFolderId"),
                "unread": f.get("unreadItemCount"),
                "total": f.get("totalItemCount"),
                "child_count": f.get("childFolderCount"),
                "hidden": bool(f.get("isHidden")) if f.get("isHidden") is not None else None,
            })
        return out

    def _move(self, message_id: str, destination: str) -> dict[str, Any]:
        return self._request(
            "POST", f"{self._user_base()}/messages/{message_id}/move",
            json_body={"destinationId": destination},
        )

    @guarded("mail.move", target_arg="message_id", audit_provider="m365", audit_tool="graph_api")
    def mail_move_to_folder(self, message_id: str, folder_id: str) -> dict[str, Any]:
        """Move a message to any folder id or well-known name."""
        data = self._move(message_id, folder_id)
        moved_id = data.get("id", message_id) if isinstance(data, Mapping) else message_id
        return {
            "id": moved_id,
            "destination": folder_id,
            "restore_target": moved_id,
            "reversible": True,
        }

    @guarded("mail.archive", target_arg="message_id", audit_provider="m365", audit_tool="graph_api")
    def mail_archive(self, message_id: str) -> dict[str, Any]:
        data = self._move(message_id, "archive")
        # restore_target: the post-move id. With immutable ids this equals the
        # original id, but the generic restore flow prefers this persisted value
        # over the original action target for correctness across moves.
        moved_id = data.get("id", message_id)
        return {"id": moved_id, "destination": "archive", "restore_target": moved_id}

    @guarded("mail.unarchive", target_arg="message_id", audit_provider="m365", audit_tool="graph_api")
    def mail_unarchive(self, message_id: str) -> dict[str, Any]:
        data = self._move(message_id, "inbox")
        return {"id": data.get("id", message_id), "destination": "inbox"}

    @guarded("mail.trash", target_arg="message_id", audit_provider="m365", audit_tool="graph_api")
    def mail_trash(self, message_id: str) -> dict[str, Any]:
        data = self._move(message_id, "deleteditems")
        moved_id = data.get("id", message_id)
        return {"id": moved_id, "destination": "deleteditems", "reversible": True,
                "restore_target": moved_id}

    @guarded("mail.untrash", target_arg="message_id", audit_provider="m365", audit_tool="graph_api")
    def mail_untrash(self, message_id: str) -> dict[str, Any]:
        data = self._move(message_id, "inbox")
        return {"id": data.get("id", message_id), "destination": "inbox"}

    @guarded("mail.tag", target_arg="message_id", audit_provider="m365", audit_tool="graph_api")
    def mail_tag(self, message_id: str, tag_id: str) -> dict[str, Any]:
        """Apply an Outlook category. For m365 the tag_id IS the category
        displayName. Fetches current categories and appends."""
        current = self._request(
            "GET", f"{self._user_base()}/messages/{message_id}",
            params={"$select": "categories"},
        )
        existing = list(current.get("categories", []) or []) if isinstance(current, Mapping) else []
        if tag_id not in existing:
            existing.append(tag_id)
        self._request(
            "PATCH", f"{self._user_base()}/messages/{message_id}",
            json_body={"categories": existing},
        )
        return {"id": message_id, "categories": existing}

    @guarded("mail.create_tag", target_arg="name", audit_provider="m365", audit_tool="graph_api")
    def mail_create_tag(self, name: str) -> dict[str, Any]:
        data = self._request(
            "POST", f"{self._user_base()}/outlook/masterCategories",
            json_body={"displayName": name, "color": "preset0"},
        )
        return {"id": name, "name": name, "graph_id": data.get("id") if isinstance(data, Mapping) else None}

    # ── Calendar ──────────────────────────────────────────────────────

    def calendar_list(self, start: str, end: str) -> list[dict[str, Any]]:
        if "T" not in start:
            start = f"{start}T00:00:00Z"
        if "T" not in end:
            end = f"{end}T23:59:59Z"
        try:
            params = {"startDateTime": start, "endDateTime": end, "$top": 100}
            value = self._paged_values(
                "GET", f"{self._user_base()}/calendarView", params=params,
                max_items=MAX_ITEMS, context="calendar_list",
            )
            return [self._normalize_event(e) for e in value if isinstance(e, Mapping)]
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"m365 calendar_list failed: {exc}")
            return []

    @guarded("calendar.create", target_arg="title", audit_provider="m365", audit_tool="graph_api")
    def calendar_create(self, title: str, start: str, end: str,
                        attendees: list[str] | None = None,
                        description: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "subject": title,
            "start": {"dateTime": _graph_datetime(start, "10:00:00"), "timeZone": "UTC"},
            "end": {"dateTime": _graph_datetime(end, "11:00:00"), "timeZone": "UTC"},
        }
        if attendees:
            payload["attendees"] = [
                {"emailAddress": {"address": a}, "type": "required"} for a in attendees
            ]
        if description:
            payload["body"] = {"contentType": "Text", "content": description}
        return self._request("POST", f"{self._user_base()}/events", json_body=payload)

    @guarded("calendar.update", target_arg="event_id", audit_provider="m365", audit_tool="graph_api")
    def calendar_update(self, event_id: str, **fields: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in fields.items():
            if key in ("title", "summary", "subject"):
                payload["subject"] = value
            elif key == "start":
                payload["start"] = {"dateTime": _graph_datetime(str(value), "10:00:00"), "timeZone": "UTC"}
            elif key == "end":
                payload["end"] = {"dateTime": _graph_datetime(str(value), "11:00:00"), "timeZone": "UTC"}
            elif key == "description":
                payload["body"] = {"contentType": "Text", "content": value}
            elif key == "location":
                payload["location"] = {"displayName": value}
            else:
                payload[key] = value
        return self._request("PATCH", f"{self._user_base()}/events/{event_id}", json_body=payload)

    def calendar_cancel(self, event_id: str) -> dict[str, Any]:
        """calendar.cancel is NOT supported for m365 — there is no restore path.

        The generic soft-delete flow maps calendar.cancel -> calendar_uncancel
        (its reversible promise), but Microsoft Graph cannot reinstate a
        cancelled event, so honouring that promise is impossible. Rather than
        perform an irreversible cancel behind a "reversible" gate, this returns
        a failure ActionResult (it never raises). The guardrail is still applied
        first so a clean (unapproved) environment is blocked exactly like every
        other mutation; when the gate would allow it, we still refuse with an
        explanation. Capability "calendar.cancel" is also False for m365
        (workspace_capabilities.py), so the generic execute path refuses these
        actions pre-execution via require_capability.
        """
        from workspace_guardrails import ActionResult, confirm_action

        if not confirm_action("calendar.cancel", event_id=event_id):
            return ActionResult(
                success=False, action="calendar.cancel", provider=self._provider_name,
                target=event_id, error="cancelled by guardrail",
            ).to_dict()
        return ActionResult(
            success=False, action="calendar.cancel", provider=self._provider_name,
            target=event_id,
            error="calendar.cancel is not supported for m365: Microsoft Graph has no "
                  "uncancel/restore path, so a cancel cannot be honoured behind the "
                  "reversible soft-delete promise. Cancel the event via Outlook, or "
                  "delete and recreate it.",
        ).to_dict()

    def calendar_uncancel(self, event_id: str) -> dict[str, Any]:
        raise NotImplementedError("Graph has no uncancel; recreate the event")

    # ── Files (OneDrive) ──────────────────────────────────────────────

    def files_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        try:
            escaped = str(query).replace("'", "''")
            path = f"{self._user_base()}/drive/root/search(q='{escaped}')"
            value = self._paged_values(
                "GET", path, params={"$top": max_results},
                max_items=max_results, context="files_search",
            )
            return [self._normalize_file(f) for f in value if isinstance(f, Mapping)]
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"m365 files_search failed: {exc}")
            return []

    @guarded("files.upload", target_arg="file_path", audit_provider="m365", audit_tool="graph_api")
    def files_upload(self, file_path: str, parent_id: str | None = None) -> dict[str, Any]:
        # Simple upload — supports files < 4 MB only. Larger files need an upload
        # session (deferred).
        p = Path(file_path)
        name = p.name
        content = p.read_bytes()
        if parent_id:
            path = f"{self._user_base()}/drive/items/{parent_id}:/{name}:/content"
        else:
            path = f"{self._user_base()}/drive/root:/{name}:/content"
        return self._request(
            "PUT", path, content=content,
            headers={"Content-Type": "application/octet-stream"},
        )

    @guarded("files.download", target_arg="file_id", audit_provider="m365", audit_tool="graph_api")
    def files_download(self, file_id: str, output_path: str) -> dict[str, Any]:
        content = self._request(
            "GET", f"{self._user_base()}/drive/items/{file_id}/content",
            raw=True, timeout=120,
        )
        if isinstance(content, (bytes, bytearray)):
            Path(output_path).write_bytes(bytes(content))
        return {"path": output_path}

    @guarded("files.trash", target_arg="file_id", audit_provider="m365", audit_tool="graph_api")
    def files_trash(self, file_id: str) -> dict[str, Any]:
        """Soft-delete to the OneDrive recycle bin.

        Captures the item name before delete and, when possible, resolves the
        SharePoint recycle-bin GUID as ``restore_target`` so Business restores
        (which cannot use Graph ``driveItem/restore``) still work through
        ``files_untrash`` / ``delete_actions.py restore``.
        """
        name = ""
        try:
            meta = self._request(
                "GET", f"{self._user_base()}/drive/items/{file_id}",
                params={"$select": "id,name"},
            )
            if isinstance(meta, Mapping):
                name = str(meta.get("name") or "")
        except Exception:
            name = ""
        self._request("DELETE", f"{self._user_base()}/drive/items/{file_id}")
        out: dict[str, Any] = {"id": file_id, "reversible": True}
        if name:
            out["name"] = name
        recycle_id = self._resolve_recycle_bin_id(leaf_name=name) if name else ""
        if recycle_id:
            out["restore_target"] = recycle_id
        return out

    @guarded("files.untrash", target_arg="file_id", audit_provider="m365", audit_tool="graph_api")
    def files_untrash(self, file_id: str) -> dict[str, Any]:
        """Restore a recycled OneDrive item.

        Paths:
          1. If ``file_id`` is a recycle-bin GUID → SharePoint REST RestoreByIds
             (OneDrive for Business / work accounts).
          2. Else try Graph ``POST …/drive/items/{id}/restore`` (Personal only).
          3. On Personal-only / not-found errors, fall back to SharePoint REST
             recycle-bin restore (lookup by GUID or recent LeafName match).
        """
        target = (file_id or "").strip()
        if not target:
            raise RuntimeError("files.untrash requires a file_id or recycle-bin GUID")

        if _is_guid(target):
            return self._restore_via_sharepoint_recycle_bin(target)

        try:
            data = self._request(
                "POST", f"{self._user_base()}/drive/items/{target}/restore",
                json_body={},
            )
            out = dict(data) if isinstance(data, dict) else {}
            out.setdefault("id", target)
            out["reversible"] = True
            out["trashed"] = False
            out["restore_path"] = "graph_personal"
            return out
        except RuntimeError as exc:
            if not _is_personal_restore_unsupported(exc):
                raise
            # Business / work account — resolve recycle-bin GUID then restore.
            recycle_id = self._resolve_recycle_bin_id(drive_item_id=target)
            if not recycle_id:
                raise RuntimeError(
                    f"Graph driveItem restore is Personal-only and no SharePoint "
                    f"recycle-bin item matched id {target!r}. Trash via files_trash "
                    f"first (persists restore_target), or pass the recycle-bin GUID."
                ) from exc
            result = self._restore_via_sharepoint_recycle_bin(recycle_id)
            result["drive_item_id"] = target
            return result

    # ── OneDrive Business recycle-bin helpers (SharePoint REST) ────────

    def _drive_web_url(self) -> str:
        data = self._request(
            "GET", f"{self._user_base()}/drive",
            params={"$select": "id,webUrl"},
        )
        if not isinstance(data, Mapping):
            raise RuntimeError("drive metadata response was not a mapping")
        web = str(data.get("webUrl") or "").strip()
        if not web:
            raise RuntimeError("drive.webUrl missing — cannot resolve SharePoint site")
        return web

    def _personal_site_base(self, web_url: str) -> str:
        """Derive the SharePoint site base from a OneDrive drive webUrl."""
        parsed = urlparse(web_url)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0].lower() == "personal":
            base_path = "/" + "/".join(parts[:2])
            return f"{parsed.scheme}://{parsed.netloc}{base_path}"
        if parts:
            base_path = "/" + "/".join(parts[:-1])
            return f"{parsed.scheme}://{parsed.netloc}{base_path}"
        return f"{parsed.scheme}://{parsed.netloc}"

    def _get_spo_token(self, host: str) -> str:
        """Acquire a SharePoint-host token (audience = https://{host}/.default).

        Graph tokens cannot call ``*_api/web/recyclebin`` on ``*.sharepoint.com``.
        App registrations need SharePoint application permission
        ``Sites.ReadWrite.All`` (or equivalent) in addition to Graph Files scopes.
        """
        host = (host or "").strip().lower()
        if not host:
            raise RuntimeError("SharePoint host is empty")
        cached = self._spo_tokens.get(host)
        if cached:
            return cached
        try:
            import msal  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Microsoft 365 provider requires the 'msal' package. "
                "Install it with: pip install msal"
            ) from exc
        import os

        authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        scope = [f"https://{host}/.default"]
        if self.auth_mode == "device_code":
            # Device-code tokens are Graph-scoped; re-run device flow for SPO.
            app = msal.PublicClientApplication(self.client_id, authority=authority)
            flow = app.initiate_device_flow(scopes=scope)
            if "user_code" not in flow:
                raise RuntimeError(
                    f"Failed to start SharePoint device flow: "
                    f"{flow.get('error_description', flow)}"
                )
            print(
                flow.get("message", "Complete SharePoint device sign-in in your browser."),
                file=sys.stderr,
            )
            result = app.acquire_token_by_device_flow(flow)
        else:
            secret = os.getenv(self.client_secret_env, "")
            if not secret:
                raise RuntimeError(
                    f"m365 client_credentials auth requires the client secret in "
                    f"env var {self.client_secret_env}"
                )
            if not self.tenant_id or not self.client_id:
                raise RuntimeError("m365 config requires tenant_id and client_id")
            app = msal.ConfidentialClientApplication(
                self.client_id, authority=authority, client_credential=secret,
            )
            result = app.acquire_token_for_client(scopes=scope)
        token = result.get("access_token") if isinstance(result, Mapping) else None
        if not token:
            err = result.get("error_description") if isinstance(result, Mapping) else result
            raise RuntimeError(
                f"Failed to acquire SharePoint token for {host}: {err}. "
                "Grant the app SharePoint Sites.ReadWrite.All (application) "
                "and admin-consent it for OneDrive Business recycle-bin restore."
            )
        self._spo_tokens[host] = str(token)
        return str(token)

    def _spo_request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any | None = None,
        timeout: int = 45,
    ) -> Any:
        """SharePoint REST call with a host-scoped bearer token."""
        parsed = urlparse(url)
        host = parsed.hostname or ""
        token = self._get_spo_token(host)
        hdrs = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json;odata=nometadata",
            "Content-Type": "application/json;odata=nometadata",
        }
        resp = self._send(
            method, url, json=json_body, headers=hdrs, timeout=timeout,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"SharePoint REST {resp.status_code} on {parsed.path}: "
                f"{(resp.text or '')[:500]}"
            )
        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    def _resolve_recycle_bin_id(
        self,
        *,
        leaf_name: str = "",
        drive_item_id: str = "",
    ) -> str:
        """Find a first-stage recycle-bin GUID by LeafName (preferred) or id hint."""
        try:
            web_url = self._drive_web_url()
            site = self._personal_site_base(web_url)
        except Exception:
            return ""
        list_url = (
            f"{site}/_api/web/RecycleBin"
            f"?$select=Id,Title,LeafName,DirName,ItemType,DeletedDate"
            f"&$orderby=DeletedDate desc&$top=50"
        )
        # Recycle-bin indexing can lag a moment after DELETE.
        data: Any = {}
        for attempt in range(3):
            try:
                data = self._spo_request("GET", list_url)
            except Exception:
                if attempt == 2:
                    return ""
                self._sleep(0.4 * (attempt + 1))
                continue
            items = []
            if isinstance(data, Mapping):
                value = data.get("value")
                if isinstance(value, list):
                    items = value
            if items or attempt == 2:
                break
            self._sleep(0.4 * (attempt + 1))
        leaf = (leaf_name or "").strip().lower()
        for item in items:
            if not isinstance(item, Mapping):
                continue
            item_leaf = str(item.get("LeafName") or item.get("Title") or "").strip()
            item_id = str(item.get("Id") or "").strip()
            if leaf and item_leaf.lower() == leaf and _is_guid(item_id):
                return item_id
        # No name match — only accept an explicit GUID drive_item_id (rare).
        if drive_item_id and _is_guid(drive_item_id):
            return drive_item_id.strip()
        return ""

    def _restore_via_sharepoint_recycle_bin(self, recycle_bin_id: str) -> dict[str, Any]:
        web_url = self._drive_web_url()
        site = self._personal_site_base(web_url)
        # Prefer site-collection RestoreByIds (works for OneDrive personal sites).
        restore_url = f"{site}/_api/site/RecycleBin/RestoreByIds"
        self._spo_request(
            "POST", restore_url,
            json_body={"ids": [recycle_bin_id]},
        )
        return {
            "id": recycle_bin_id,
            "restore_target": recycle_bin_id,
            "reversible": True,
            "trashed": False,
            "restore_path": "sharepoint_recycle_bin",
        }

    # ── Health ────────────────────────────────────────────────────────

    def health_check(self) -> bool:
        try:
            self._request("GET", self._user_base(), params={"$select": "id"})
            return True
        except Exception:
            return False


def _is_guid(value: str) -> bool:
    """True for SharePoint recycle-bin GUIDs (not OneDrive Business 01… ids)."""
    import re
    return bool(
        re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            (value or "").strip(),
        )
    )


def _is_personal_restore_unsupported(exc: BaseException) -> bool:
    msg = str(exc).lower()
    needles = (
        "personal",
        "not supported",
        "notsupported",
        "badrequest",
        "400",
        "404",
        "itemnotfound",
        "not found",
    )
    return any(n in msg for n in needles)
