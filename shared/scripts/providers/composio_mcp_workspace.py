#!/usr/bin/env python3
"""Strict-response facade for the Composio MCP workspace backend.

The implementation remains in ``composio_mcp_workspace_base``; this module
adds operation-specific validation so malformed successful envelopes cannot be
normalised into an empty successful read.
"""
from __future__ import annotations

import sys
from typing import Any, Mapping

try:  # package import
    from . import composio_mcp_workspace_base as _impl
except ImportError:  # direct path import used by some tests/tools
    import composio_mcp_workspace_base as _impl  # type: ignore


def _mapping_has_list(data: Any, keys: tuple[str, ...]) -> bool:
    """Return True only when DATA contains a recognised record list.

    Empty lists are valid successful reads. Missing keys, ``None``, scalar
    values, and the outer MCP ``data.results`` envelope are not.
    """
    if isinstance(data, list):
        return True
    if not isinstance(data, Mapping):
        return False
    for key in keys:
        if key in data:
            return isinstance(data.get(key), list)
    for key in ("response_data", "data", "result"):
        nested = data.get(key)
        if isinstance(nested, (Mapping, list)) and _mapping_has_list(nested, keys):
            return True
    return False


def _validate_read_payload(client: Any, operation: str, slug: str, data: Any) -> None:
    """Validate a successful tool payload before normalisation.

    The provider's public read methods wrap these ``ValueError`` instances in
    ``ComposioReadError``, causing callers such as daily briefing to mark the
    source unavailable instead of reporting an empty mailbox/calendar/drive.
    """
    if getattr(client, "family", "google") == "microsoft":
        keys = {
            "mail_search": ("value", "messages"),
            "calendar_list": ("value", "events"),
            "files_search": ("value", "files", "items"),
        }.get(operation, ())
        if keys and not _mapping_has_list(data, keys):
            raise ValueError(
                f"malformed Composio Microsoft response for {operation}: "
                f"expected one of {', '.join(keys)} to contain a list"
            )
        return

    # Preserve the exact, live-verified Google response contracts. Custom slugs
    # may return a bare list or a common operation-specific list key.
    expected: tuple[str, ...] = ()
    if operation == "mail_search":
        expected = ("messages",)
    elif operation == "calendar_list":
        event_data = data.get("event_data") if isinstance(data, Mapping) else None
        if isinstance(event_data, Mapping) and isinstance(event_data.get("event_data"), list):
            return
        expected = ("events",)
    elif operation == "files_search":
        expected = ("files", "items")

    if expected and not _mapping_has_list(data, expected):
        if operation == "mail_search" and isinstance(data, Mapping):
            preview = data.get("data_preview")
            preview_messages = (
                preview.get("messages") if isinstance(preview, Mapping) else None
            )
            if preview or (isinstance(preview_messages, list) and preview_messages):
                raise ValueError(
                    "Composio offloaded the mail_search response to data_preview "
                    "(result set too large for inline payload); refetch with leaner "
                    "args (include_payload=False, verbose=False)"
                )
        raise ValueError(
            f"malformed Composio Google response for {operation} via {slug!r}: "
            f"expected a recognised record list"
        )


_original_normalize_records = _impl.ComposioMCPWorkspaceClient._normalize_records


def _strict_normalize_records(self: Any, operation: str, slug: str, data: Any) -> Any:
    _validate_read_payload(self, operation, slug, data)
    return _original_normalize_records(self, operation, slug, data)


_impl.ComposioMCPWorkspaceClient._normalize_records = _strict_normalize_records
_impl._validate_read_payload = _validate_read_payload
_impl._mapping_has_list = _mapping_has_list

# Make imports and monkeypatching behave exactly as before: callers receive the
# implementation module object, now patched with strict validation.
sys.modules[__name__] = _impl
