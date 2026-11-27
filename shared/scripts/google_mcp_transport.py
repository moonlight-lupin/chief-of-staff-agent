#!/usr/bin/env python3
"""Stateless JSON-RPC client for Google's Workspace MCP servers.

Google's Gmail, Calendar and Drive MCP servers speak *stateless* Streamable
HTTP (Google Workspace guide "MCP form elicitation", retrieved 2026-11-27):

* no ``initialize`` handshake and no session id — every POST stands alone,
  with ``Authorization``, ``Content-Type``, ``Accept`` and
  ``MCP-Protocol-Version`` headers;
* every ``tools/call`` carries ``params._meta`` with the protocol version, the
  client's name and version, and the ``elicitation.form`` capability;
* the body comes back either as JSON or as Server-Sent Events (``data:``
  lines), so both are parsed;
* a call that needs the user's approval returns ``resultType:
  "input_required"`` with an approval form and an opaque ``requestState``.
  The retry re-sends the identical name and arguments plus ``requestState``
  and ``inputResponses`` as top-level ``params`` fields.

This module never answers that form. :func:`call_tool_confirmed` raises
:class:`GoogleConfirmationRequired` unless the caller passed a ``confirmer``,
and refuses any answer outside a snapshot of the choices offered to the human
(persistent ``*_ALWAYS`` choices only when the operator opted in).

Retries: a read may be re-sent once on HTTP 429/503 (the server refused
before doing anything). A write is never re-sent automatically — after a
timeout, a 408 or a 5xx the outcome is unknown, and the error says so. Where
the server certainly ran nothing (any other 4xx including 429, a JSON-RPC
error, a non-form elicitation, a "retry without inputResponses" refusal) the
error is also a :class:`workspace_guardrails.NotAttempted`, so an executor
keeps the action approved. ``UserDeniedAction`` and ``AdminPolicyBlock`` are
:class:`workspace_guardrails.FinalFailure`: never retried.

The access token comes from a callable on each request and is never stored
here, logged, or written to disk.
"""
from __future__ import annotations

import copy
import itertools
import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

try:
    import requests
except ImportError:  # pragma: no cover - requests is a declared dependency
    requests = None  # type: ignore

from workspace_guardrails import (  # noqa: E402
    ConfirmationRequired,
    FinalFailure,
    NotAttempted,
    is_persistent_choice,
)

PROTOCOL_VERSION = "2026-07-28"
CLIENT_NAME = "Chief of Staff"

ENDPOINTS: dict[str, str] = {
    "gmail": "https://gmailmcp.googleapis.com/mcp/v1",
    "calendar": "https://calendarmcp.googleapis.com/mcp/v1",
    "drive": "https://drivemcp.googleapis.com/mcp/v1",
}
PRODUCTS: dict[str, str] = {
    "gmail": "Gmail",
    "calendar": "Google Calendar",
    "drive": "Google Drive",
}

MAX_ELICITATION_ROUNDS = 3          # the same bound as Google's reference clients
READ_RETRY_STATUS = frozenset({429, 503})
MISSING_CLIENT_CAPABILITY = -32021
_ERROR_TEXT_LIMIT = 300

_DENIED_TEXT = "declined the confirmation request"
_BLOCKED_TEXT = "administrator has blocked"
_REBIND_TEXT = "without inputresponses"


# ── errors ──────────────────────────────────────────────────────────────────

class GoogleMCPError(RuntimeError):
    """Any failure talking to a Google Workspace MCP server."""

    def __init__(self, message: str, *, server: str = "", tool: str = "") -> None:
        super().__init__(message)
        self.server = server
        self.tool = tool


class GoogleMCPTransportError(GoogleMCPError):
    """HTTP-level failure. ``outcome_unknown`` is True when a write may have run."""

    def __init__(self, message: str, *, status: int | None = None, outcome_unknown: bool = False,
                 server: str = "", tool: str = "") -> None:
        super().__init__(message, server=server, tool=tool)
        self.status = status
        self.outcome_unknown = outcome_unknown


class GoogleMCPRequestRefused(GoogleMCPTransportError, NotAttempted):
    """HTTP 4xx (not 408): the server refused the request and ran nothing."""


class GoogleMCPInputUnsupported(GoogleMCPError, NotAttempted):
    """The server asked for input this client cannot show (not a form). Nothing ran."""


class GoogleMCPRPCError(GoogleMCPError, NotAttempted):
    """A top-level JSON-RPC ``error`` object: the call was refused, nothing ran."""

    def __init__(self, message: str, *, code: int | None = None, data: Any = None,
                 server: str = "", tool: str = "") -> None:
        super().__init__(message, server=server, tool=tool)
        self.code = code
        self.data = data


class MissingClientCapability(GoogleMCPRPCError):
    """JSON-RPC -32021: the call needs form elicitation the request did not declare."""


class GoogleMCPToolError(GoogleMCPError):
    """The tool ran to completion with ``isError: true``."""

    def __init__(self, message: str, *, reason: str = "", server: str = "", tool: str = "") -> None:
        super().__init__(message, server=server, tool=tool)
        self.reason = reason


class UserDeniedAction(GoogleMCPToolError, FinalFailure):
    """The user chose Don't allow (DENY_ONCE), declined or cancelled the prompt. Final."""


class AdminPolicyBlock(GoogleMCPToolError, FinalFailure):
    """An administrator policy blocks third-party agents from this action. Final."""


class ApprovalBindingRefused(GoogleMCPToolError, NotAttempted):
    """The server refused the retry's approval binding ("retry without inputResponses")."""


class GoogleConfirmationRequired(ConfirmationRequired):
    """Google's approval form for one tool call (``resultType: input_required``)."""


class ConfirmationChoiceRefused(GoogleConfirmationRequired):
    """The confirmer answered with a choice the server did not offer. Nothing was sent."""

    def __init__(self, prompt: GoogleConfirmationRequired, choice: Any,
                 offered: list[tuple[str, str]] | None = None) -> None:
        self.choice = choice
        super().__init__(server=prompt.server, product=prompt.product, tool=prompt.tool,
                         message=prompt.message, title=prompt.title, choices=prompt.choices,
                         request_state=prompt.request_state, arguments=prompt.arguments,
                         request_key=prompt.request_key, field_key=prompt.field_key,
                         offered=list(offered) if offered is not None else prompt.offered)

    def summary(self) -> str:
        offered = ", ".join(self.offered_ids) or "none"
        return (f"confirmer answer {self.choice!r} is not one of the choices {self.product} offered "
                f"for {self.tool} ({offered}); nothing was sent")


# ── parsing ─────────────────────────────────────────────────────────────────

def _sse_messages(text: str) -> list[Any]:
    """JSON payloads of each SSE event (``data:`` lines joined per event)."""
    out: list[Any] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            raw = "\n".join(buf)
            buf.clear()
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                pass

    for line in text.splitlines():
        if not line.strip():
            flush()
        elif line.startswith("data:"):
            buf.append(line[5:].lstrip(" "))
    flush()
    return out


def parse_body(text: str, content_type: str = "", request_id: Any = None) -> dict[str, Any]:
    """The JSON-RPC response message in a JSON or SSE body.

    For SSE, notifications and requests are skipped and the message answering
    ``request_id`` is returned. A stream that never answers ``request_id`` is
    an error: another message's result is never taken for ours. Without a
    ``request_id`` only a stream with exactly one response is accepted.
    """
    body = text or ""
    if "text/event-stream" in (content_type or "").lower() or body.lstrip().startswith(("data:", "event:", ":")):
        candidates = [m for m in _sse_messages(body)
                      if isinstance(m, Mapping) and ("result" in m or "error" in m)]
        if not candidates:
            raise GoogleMCPError("no JSON-RPC response in the event stream")
        if request_id is None:
            if len(candidates) == 1:
                return dict(candidates[0])
            raise GoogleMCPError("several JSON-RPC responses in the event stream and no request id")
        for m in candidates:
            if m.get("id") == request_id:
                return dict(m)
        raise GoogleMCPError(f"no JSON-RPC response for request {request_id} in the event stream")
    try:
        msg = json.loads(body)
    except json.JSONDecodeError as exc:
        raise GoogleMCPError(f"unparseable MCP response ({content_type or 'no content type'})") from exc
    if not isinstance(msg, Mapping):
        raise GoogleMCPError("MCP response is not a JSON-RPC object")
    return dict(msg)


def tool_payload(result: Mapping[str, Any]) -> Any:
    """The data a completed tool returned: ``structuredContent``, else its JSON text."""
    structured = result.get("structuredContent")
    if structured is not None:
        return structured
    texts = [str(item.get("text") or "") for item in result.get("content") or []
             if isinstance(item, Mapping) and item.get("type") == "text"]
    joined = "".join(texts).strip()
    if not joined:
        return {}
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return {"text": joined}


def _error_reason(result: Mapping[str, Any]) -> str:
    meta = result.get("_meta")
    err = meta.get("google.com/error") if isinstance(meta, Mapping) else None
    if isinstance(err, Mapping):
        for detail in err.get("details") or []:
            if isinstance(detail, Mapping) and detail.get("reason"):
                return str(detail["reason"])
    return ""


def raise_for_tool_error(result: Mapping[str, Any], *, server: str, tool: str) -> None:
    """Raise a named error for an ``isError`` result (UserDeniedAction, AdminPolicyBlock, …)."""
    if not result.get("isError"):
        return
    payload = tool_payload(result)
    text = payload.get("text") if isinstance(payload, Mapping) and "text" in payload else json.dumps(payload)
    text = str(text or "tool returned an error")[:_ERROR_TEXT_LIMIT]
    reason = _error_reason(result)
    lowered = text.lower()
    if not reason and _DENIED_TEXT in lowered:
        reason = "UserDeniedAction"
    elif not reason and _BLOCKED_TEXT in lowered:
        reason = "AdminPolicyBlock"
    product = PRODUCTS.get(server, server)
    label = f" [{reason}]" if reason else ""
    message = f"{product} {tool} failed{label}: {text}"
    cls = {"UserDeniedAction": UserDeniedAction, "AdminPolicyBlock": AdminPolicyBlock}.get(reason)
    if cls is None:
        cls = ApprovalBindingRefused if _REBIND_TEXT in lowered else GoogleMCPToolError
    raise cls(message, reason=reason, server=server, tool=tool)


def prompt_from_result(result: Mapping[str, Any], *, server: str, tool: str,
                       arguments: Mapping[str, Any], host: str = "",
                       allow_persistent: bool = False) -> GoogleConfirmationRequired | None:
    """Build the approval prompt from an ``input_required`` result, or None if it has no form.

    ``offered`` is what may be put to the human: every choice when
    ``allow_persistent``, else only the one-off ones (ALLOW_ONCE, DENY_ONCE).
    """
    requests_ = result.get("inputRequests")
    if not isinstance(requests_, Mapping):
        return None
    for key, req in requests_.items():
        if not isinstance(req, Mapping) or req.get("method") != "elicitation/create":
            continue
        params = req.get("params") if isinstance(req.get("params"), Mapping) else {}
        if params.get("mode", "form") != "form":
            continue
        schema = params.get("requestedSchema") if isinstance(params.get("requestedSchema"), Mapping) else {}
        properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
        if not properties:
            continue
        field_key, field = next(iter(properties.items()))
        field = field if isinstance(field, Mapping) else {}
        choices: list[tuple[str, str]] = []
        if isinstance(field.get("enum"), list):
            names = field.get("enumNames") if isinstance(field.get("enumNames"), list) else []
            choices = [(str(v), str(names[i]) if i < len(names) else str(v)) for i, v in enumerate(field["enum"])]
        elif isinstance(field.get("oneOf"), list):
            choices = [(str(o.get("const")), str(o.get("title") or o.get("const")))
                       for o in field["oneOf"] if isinstance(o, Mapping) and "const" in o]
        meta = result.get("_meta") if isinstance(result.get("_meta"), Mapping) else {}
        server_info = meta.get("io.modelcontextprotocol/serverInfo") if isinstance(meta, Mapping) else None
        server_info = server_info if isinstance(server_info, Mapping) else {}
        product = (str(server_info.get("title") or "") or PRODUCTS.get(server, "") or "Google Workspace")
        return GoogleConfirmationRequired(
            server=host or server, product=product, tool=tool,
            message=str(params.get("message") or "Confirm this action."),
            title=str(field.get("title") or "Confirm this action?"),
            choices=choices, request_state=result.get("requestState"),
            arguments=copy.deepcopy(dict(arguments)), request_key=str(key), field_key=str(field_key),
            offered=[c for c in choices if allow_persistent or not is_persistent_choice(c[0])],
        )
    return None


# ── transport ───────────────────────────────────────────────────────────────

class GoogleMCPTransport:
    """POSTs one JSON-RPC message per request to a Google Workspace MCP server."""

    def __init__(self, token_provider: Callable[[], str], *, client_version: str,
                 timeout: float = 60.0, accept_language: str | None = None,
                 endpoints: Mapping[str, str] | None = None) -> None:
        self._token_provider = token_provider
        self.client_version = str(client_version)
        self.timeout = timeout
        self.accept_language = accept_language
        self.endpoints = dict(endpoints or ENDPOINTS)
        self._ids = itertools.count(1)

    def endpoint(self, server: str) -> str:
        try:
            return self.endpoints[server]
        except KeyError as exc:
            raise GoogleMCPError(f"unknown Google MCP server {server!r}") from exc

    def host(self, server: str) -> str:
        return urlparse(self.endpoint(server)).hostname or server

    def base_meta(self) -> dict[str, Any]:
        return {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientInfo": {"name": CLIENT_NAME, "version": self.client_version},
            "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}},
        }

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token_provider()}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.accept_language:
            headers["Accept-Language"] = self.accept_language
        return headers

    def rpc(self, server: str, method: str, params: Mapping[str, Any], *, read_only: bool,
            tool: str = "") -> dict[str, Any]:
        """Send one request and return its JSON-RPC response message.

        ``read_only`` allows one re-send on 429/503. A write is sent once.
        """
        if requests is None:  # pragma: no cover
            raise GoogleMCPError("the requests package is required for the google_mcp provider")
        url = self.endpoint(server)
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": dict(params)}
        attempts = 2 if read_only else 1
        what = f"{PRODUCTS.get(server, server)} {tool or method}"
        for attempt in range(attempts):
            try:
                resp = requests.post(url, headers=self._headers(), json=payload, timeout=self.timeout)
            except requests.Timeout as exc:
                raise GoogleMCPTransportError(
                    f"{what} timed out; it may or may not have run — check before trying again",
                    outcome_unknown=not read_only, server=server, tool=tool) from exc
            except requests.RequestException as exc:
                raise GoogleMCPTransportError(
                    f"{what} failed to connect ({type(exc).__name__}); it may or may not have run",
                    outcome_unknown=not read_only, server=server, tool=tool) from exc
            status = int(getattr(resp, "status_code", 0) or 0)
            if status == 200:
                content_type = str((getattr(resp, "headers", None) or {}).get("Content-Type", ""))
                msg = parse_body(getattr(resp, "text", "") or "", content_type, payload["id"])
                if "error" in msg and msg["error"] is not None:
                    err = msg["error"] if isinstance(msg["error"], Mapping) else {"message": str(msg["error"])}
                    code = err.get("code")
                    text = str(err.get("message") or "JSON-RPC error")[:_ERROR_TEXT_LIMIT]
                    if code == MISSING_CLIENT_CAPABILITY:
                        raise MissingClientCapability(
                            f"{what} refused [MissingRequiredClientCapability -32021]: {text}",
                            code=code, data=err.get("data"), server=server, tool=tool)
                    raise GoogleMCPRPCError(f"{what} JSON-RPC error {code}: {text}",
                                            code=code, data=err.get("data"), server=server, tool=tool)
                return msg
            if read_only and status in READ_RETRY_STATUS and attempt + 1 < attempts:
                continue
            body = str(getattr(resp, "text", "") or "")[:_ERROR_TEXT_LIMIT]
            # A 4xx (but not 408 Request Timeout) is a refusal: nothing ran.
            # Anything else on a write — 408, 5xx, an unexpected status — may
            # have run.
            refused = 400 <= status < 500 and status != 408
            if refused:
                raise GoogleMCPRequestRefused(f"{what} refused (HTTP {status}): {body}; nothing was run",
                                              status=status, outcome_unknown=False, server=server, tool=tool)
            unknown = not read_only
            suffix = "; it may or may not have run — check before trying again" if unknown else ""
            raise GoogleMCPTransportError(f"{what} failed (HTTP {status}): {body}{suffix}",
                                          status=status, outcome_unknown=unknown, server=server, tool=tool)
        raise GoogleMCPTransportError(f"{what} failed", server=server, tool=tool)  # pragma: no cover

    def discover(self, server: str) -> dict[str, Any]:
        """``server/discover``: the server's metadata (stateless, no session)."""
        msg = self.rpc(server, "server/discover", {}, read_only=True, tool="server/discover")
        result = msg.get("result") if isinstance(msg.get("result"), Mapping) else {}
        meta = result.get("_meta") if isinstance(result.get("_meta"), Mapping) else {}
        info = meta.get("io.modelcontextprotocol/serverInfo") if isinstance(meta, Mapping) else {}
        return dict(info) if isinstance(info, Mapping) else {}

    def call_tool(self, server: str, name: str, arguments: Mapping[str, Any], *, read_only: bool,
                  request_state: Any = None, input_responses: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """One ``tools/call``. Returns the raw ``result`` object."""
        params: dict[str, Any] = {"name": name, "arguments": copy.deepcopy(dict(arguments)),
                                  "_meta": self.base_meta()}
        if request_state is not None:
            params["requestState"] = request_state
        if input_responses is not None:
            params["inputResponses"] = copy.deepcopy(dict(input_responses))
        msg = self.rpc(server, "tools/call", params, read_only=read_only, tool=name)
        result = msg.get("result")
        if not isinstance(result, Mapping):
            raise GoogleMCPError(f"{PRODUCTS.get(server, server)} {name} returned no result",
                                 server=server, tool=name)
        return dict(result)


Confirmer = Callable[[GoogleConfirmationRequired], Any]


def call_tool_confirmed(transport: GoogleMCPTransport, server: str, name: str, arguments: Mapping[str, Any], *,
                        read_only: bool, confirmer: Confirmer | None = None,
                        allow_persistent: bool = False) -> Any:
    """Call a tool, handle Google's approval form, and return the tool's data.

    The form is answered only by ``confirmer(prompt)``; with no confirmer, or a
    ``None`` answer, :class:`GoogleConfirmationRequired` is raised. The answer
    is checked against a snapshot of the offered choice ids taken *before* the
    confirmer runs, so a confirmer cannot widen them; a persistent
    ``*_ALWAYS`` choice is offered only with ``allow_persistent``. Anything
    else raises :class:`ConfirmationChoiceRefused` and nothing is sent. The
    retry is the identical name and arguments plus ``requestState`` and
    ``inputResponses``.
    """
    frozen = copy.deepcopy(dict(arguments))
    result = transport.call_tool(server, name, frozen, read_only=read_only)
    for round_no in range(1, MAX_ELICITATION_ROUNDS + 1):
        if result.get("resultType") != "input_required":
            break
        prompt = prompt_from_result(result, server=server, tool=name, arguments=frozen,
                                    host=transport.host(server), allow_persistent=allow_persistent)
        if prompt is None:
            raise GoogleMCPInputUnsupported(f"{PRODUCTS.get(server, server)} {name} asked for input this "
                                            "client cannot show (not a form); nothing was run",
                                            server=server, tool=name)
        if confirmer is None or round_no == MAX_ELICITATION_ROUNDS:
            raise prompt
        offered_snapshot = tuple(prompt.offered)
        offered_ids = frozenset(cid for cid, _ in offered_snapshot)
        choice = confirmer(prompt)
        if choice is None:
            raise prompt
        if (not isinstance(choice, str) or choice not in offered_ids
                or (is_persistent_choice(choice) and not allow_persistent)):
            raise ConfirmationChoiceRefused(prompt, choice, offered=list(offered_snapshot))
        responses = {prompt.request_key: {"action": "accept", "content": {prompt.field_key: choice}}}
        result = transport.call_tool(server, name, frozen, read_only=read_only,
                                     request_state=prompt.request_state, input_responses=responses)
    raise_for_tool_error(result, server=server, tool=name)
    return tool_payload(result)
