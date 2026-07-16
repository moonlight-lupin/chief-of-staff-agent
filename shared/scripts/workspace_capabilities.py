#!/usr/bin/env python3
"""Provider capability matrix.

Each provider declares which workspace actions it supports.
Skills can check client.supports("files.upload") before calling.
Workflow-level capabilities are derived from base action capabilities.

Capability keys are PROVIDER-NEUTRAL (mail.*, files.*, calendar.*). Legacy
Gmail/Drive-flavored keys (gmail.*, drive.*) are still accepted everywhere via
LEGACY_ACTION_ALIASES so existing callers keep working. get_capabilities()
returns a MERGED dict containing both neutral and legacy keys.
"""
from __future__ import annotations
from typing import Any

# Legacy (Gmail/Drive-flavored) action key -> neutral action key.
# Gmail labels ≈ Outlook categories, so labels map to the neutral "tag" verbs.
LEGACY_ACTION_ALIASES: dict[str, str] = {
    "gmail.search": "mail.search",
    "gmail.draft": "mail.draft",
    "gmail.send": "mail.send",
    "gmail.archive": "mail.archive",
    "gmail.trash": "mail.trash",
    "gmail.labels.list": "mail.list_tags",
    "gmail.label": "mail.tag",
    "gmail.create_label": "mail.create_tag",
    "drive.search": "files.search",
    "drive.upload": "files.upload",
    "drive.download": "files.download",
    "drive.trash": "files.trash",
    # calendar.* keys are already neutral (unchanged).
}

# Canonical, provider-neutral capability keys stored per provider.
CAPABILITIES: dict[str, dict[str, bool]] = {
    "google_api": {
        "mail.search": True,
        "mail.draft": False,        # google_api.py has no draft subcommand
        "mail.send": True,          # supported but destructive / guardrailed
        "mail.list_folders": False, # Gmail uses labels, not Outlook folders
        "mail.move": False,         # no folder-move surface on google_api
        "mail.archive": True,       # via gmail modify --remove-labels INBOX
        "mail.trash": True,         # via gmail modify --add-labels TRASH
        "mail.list_tags": True,     # via gmail labels (read-only)
        "mail.tag": True,           # via gmail modify --add-labels (apply existing label)
        "mail.create_tag": True,    # via gmail labels --create (if supported)
        "calendar.list": True,
        "calendar.create": True,
        "calendar.update": True,
        "calendar.cancel": True,    # via calendar update --status cancelled
        "files.search": True,
        "files.upload": True,
        "files.download": True,
        "files.trash": True,        # via drive delete (default is trash, reversible)
    },
    # Google Composio family — v0.3.13 catalog-wired Gmail cleanup/tags/send
    # (docs.composio.dev/toolkits/gmail). Pending live --verify-writes.
    # mail.list_folders / mail.move stay False (Gmail uses labels, not Outlook
    # folders). calendar.cancel stays False. files.trash still unwired.
    "composio": {
        "mail.search": True,
        "mail.draft": True,
        "mail.send": True,          # GMAIL_SEND_EMAIL — destructive / approval-gated
        "mail.list_folders": False, # Gmail uses labels, not folder ids
        "mail.move": False,
        "mail.archive": True,       # GMAIL_ADD_LABEL_TO_EMAIL remove INBOX
        "mail.unarchive": True,     # add INBOX
        "mail.trash": True,         # GMAIL_MOVE_TO_TRASH
        "mail.untrash": True,       # GMAIL_UNTRASH_MESSAGE
        "mail.list_tags": True,     # GMAIL_LIST_LABELS
        "mail.tag": True,           # GMAIL_ADD_LABEL_TO_EMAIL (label id)
        "mail.create_tag": True,    # GMAIL_CREATE_LABEL
        "calendar.list": True,
        "calendar.create": True,
        "calendar.update": True,
        "calendar.cancel": False,   # leave unsupported (no restore path parity)
        "files.search": True,
        "files.upload": True,
        "files.download": True,
        "files.trash": False,       # not wired yet
    },
    "composio:mcp": {
        "mail.search": True,
        "mail.draft": True,
        "mail.send": True,
        "mail.list_folders": False,
        "mail.move": False,
        "mail.archive": True,
        "mail.unarchive": True,
        "mail.trash": True,
        "mail.untrash": True,
        "mail.list_tags": True,
        "mail.tag": True,
        "mail.create_tag": True,
        "calendar.list": True,
        "calendar.create": True,
        "calendar.update": True,
        "calendar.cancel": False,
        "files.search": True,
        "files.upload": True,
        "files.download": True,
        "files.trash": False,
    },
    # Composio Microsoft family (Outlook mail/calendar + OneDrive via managed
    # OAuth) — providers.composio_mcp_workspace with family=microsoft. The client
    # reports provider_name "composio_microsoft:mcp".
    #
    # v0.3.9 live-verified: reads, mail.draft, mail.trash (deleteditems),
    # calendar create/update/delete.
    # v0.3.10 live-verified 2026-07-16: mail.archive/unarchive/untrash — the
    # full archive→unarchive→trash→untrash→trash cycle executed on the shared
    # OUTLOOK_MOVE_MESSAGE slug and cleaned up.
    # v0.3.11 Phase 3 — execution-verified 2026-07-16 (live Outlook):
    # mail.list_folders, mail.move, mail.send (approval-gated).
    # v0.3.12 Phase 4 — execution-verified 2026-07-16 (live Outlook + OneDrive):
    # mail.list_tags/tag/create_tag (OUTLOOK_GET_MASTER_CATEGORIES /
    # CREATE_USER_MASTER_CATEGORY / UPDATE_EMAIL — CoS-Verify created + applied),
    # and files.download/trash (ONE_DRIVE_DOWNLOAD_FILE → ONE_DRIVE_DELETE_ITEM).
    # files.upload stays False: the TEXT path (CREATE_TEXT_FILE) is verified, but
    # the headline case is BINARY document filing (.pdf/.docx), which 401s without
    # COMPOSIO_API_KEY (confirmed 2026-07-16). We do not advertise general upload
    # until that key is set — see _FILES_UPLOAD_BINARY_REASON. calendar.cancel False.
    "composio_microsoft": {
        "mail.search": True,        # OUTLOOK_QUERY_EMAILS — execution-verified (read)
        "mail.draft": True,         # OUTLOOK_CREATE_DRAFT — execution-verified 2026-07-16
        "mail.send": True,          # OUTLOOK_SEND_EMAIL — execution-verified 2026-07-16 (approval-gated / destructive)
        "mail.list_folders": True,  # OUTLOOK_LIST_MAIL_FOLDERS — execution-verified 2026-07-16
        "mail.move": True,          # OUTLOOK_MOVE_MESSAGE → folder id / well-known — execution-verified 2026-07-16
        "mail.archive": True,       # OUTLOOK_MOVE_MESSAGE → archive — execution-verified 2026-07-16
        "mail.unarchive": True,     # OUTLOOK_MOVE_MESSAGE → inbox — execution-verified 2026-07-16
        "mail.trash": True,         # OUTLOOK_MOVE_MESSAGE → deleteditems — execution-verified 2026-07-16
        "mail.untrash": True,       # OUTLOOK_MOVE_MESSAGE → inbox — execution-verified 2026-07-16
        "mail.list_tags": True,     # OUTLOOK_GET_MASTER_CATEGORIES — execution-verified 2026-07-16
        "mail.tag": True,           # OUTLOOK_UPDATE_EMAIL categories append — execution-verified 2026-07-16
        "mail.create_tag": True,    # OUTLOOK_CREATE_USER_MASTER_CATEGORY — execution-verified 2026-07-16
        "calendar.list": True,      # OUTLOOK_GET_CALENDAR_VIEW — execution-verified (read)
        "calendar.create": True,    # OUTLOOK_CALENDAR_CREATE_EVENT — execution-verified 2026-07-16
        "calendar.update": True,    # OUTLOOK_UPDATE_CALENDAR_EVENT — execution-verified 2026-07-16
        "calendar.cancel": False,
        "calendar.delete": True,    # OUTLOOK_DELETE_CALENDAR_EVENT — execution-verified 2026-07-16
        "files.search": True,       # ONE_DRIVE_SEARCH_ITEMS — execution-verified (read)
        "files.upload": False,      # text works via CREATE_TEXT_FILE, but binary (.pdf/.docx) needs COMPOSIO_API_KEY — see _FILES_UPLOAD_BINARY_REASON
        "files.download": True,     # ONE_DRIVE_DOWNLOAD_FILE (+ s3url fetch) — execution-verified 2026-07-16
        "files.trash": True,        # ONE_DRIVE_DELETE_ITEM → recycle bin — execution-verified 2026-07-16
    },
    # Alias: composio_microsoft:mcp is the same capability set as composio_microsoft.
    # Kept as a separate key so callers using provider_name + ":mcp" resolve correctly.
    "composio_microsoft:mcp": {
        "mail.search": True,
        "mail.draft": True,
        "mail.send": True,
        "mail.list_folders": True,
        "mail.move": True,
        "mail.archive": True,
        "mail.unarchive": True,
        "mail.trash": True,
        "mail.untrash": True,
        "mail.list_tags": True,
        "mail.tag": True,
        "mail.create_tag": True,
        "calendar.list": True,
        "calendar.create": True,
        "calendar.update": True,
        "calendar.cancel": False,
        "calendar.delete": True,
        "files.search": True,
        "files.upload": False,      # text via CREATE_TEXT_FILE ok; binary needs COMPOSIO_API_KEY — see _FILES_UPLOAD_BINARY_REASON
        "files.download": True,
        "files.trash": True,
    },
    # Microsoft 365 (Graph) provider — providers.m365_graph.M365GraphClient.
    # Every neutral action below is implemented over Microsoft Graph REST v1.0.
    # mail.send is destructive (env-gated identically to gmail.send).
    # calendar.cancel is False: Graph cannot reinstate a cancelled event
    # (calendar_uncancel raises NotImplementedError and the recreate-event
    # workflow is not implemented), so cancel has no restore path and must not
    # be offered behind the reversible soft-delete promise.
    "m365": {
        "mail.search": True,        # GET /messages ($filter/$search)
        "mail.draft": True,         # POST /messages
        "mail.send": True,          # POST /sendMail — destructive / guardrailed
        "mail.list_folders": True,  # GET /mailFolders
        "mail.move": True,          # POST /messages/{id}/move
        "mail.archive": True,       # POST /messages/{id}/move -> archive
        "mail.unarchive": True,     # move -> inbox
        "mail.trash": True,         # move -> deleteditems (reversible)
        "mail.untrash": True,       # move -> inbox
        "mail.list_tags": True,     # GET /outlook/masterCategories
        "mail.tag": True,           # PATCH /messages/{id} categories (append)
        "mail.create_tag": True,    # POST /outlook/masterCategories
        "calendar.list": True,      # GET /calendarView
        "calendar.create": True,    # POST /events
        "calendar.update": True,    # PATCH /events/{id}
        "calendar.cancel": False,   # no uncancel/restore path in Graph — the
                                    # recreate-event workflow is not implemented,
                                    # so cancel cannot honour the reversible
                                    # soft-delete promise (see m365_graph.calendar_cancel)
        "files.search": True,       # GET /drive/root/search — OneDrive
        "files.upload": True,       # PUT /drive/root:/{name}:/content (<4MB)
        "files.download": True,     # GET /drive/items/{id}/content
        "files.trash": True,        # DELETE /drive/items/{id} (recycle bin)
    },
    # Claude-native agent provider — actions are performed by the agent/tools,
    # not by script-callable provider methods, so every script-callable action
    # is False here.
    "agent": {
        "mail.search": False,
        "mail.draft": False,
        "mail.send": False,
        "mail.list_folders": False,
        "mail.move": False,
        "mail.archive": False,
        "mail.trash": False,
        "mail.list_tags": False,
        "mail.tag": False,
        "mail.create_tag": False,
        "calendar.list": False,
        "calendar.create": False,
        "calendar.update": False,
        "calendar.cancel": False,
        "files.search": False,
        "files.upload": False,
        "files.download": False,
        "files.trash": False,
    },
}

# Derived workflow capabilities — a workflow is supported if all base actions
# are supported. Requirements use legacy keys (resolved transparently by
# client.supports); this keeps existing callers/tests stable.
WORKFLOW_REQUIREMENTS: dict[str, list[str]] = {
    "document.handoff": ["drive.upload", "gmail.draft"],
    "meeting.gather": ["calendar.list", "gmail.search", "drive.search"],
    "weekly.collect": ["calendar.list", "gmail.search", "drive.search"],
}

# OneDrive uploads over Composio MCP: plain-text files go through
# ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE (MCP-native, execution-verified 2026-07-16).
# BINARY files (.pdf/.docx — the headline document-filing case) must stage a
# FileUploadable through Composio's Files REST API, which needs a project
# COMPOSIO_API_KEY (the MCP key 401s, confirmed 2026-07-16); the source_url
# alternative only helps for files already at a public HTTPS URL, not local
# documents. So files.upload is reported unsupported until that key is set —
# the text path still works when called directly, it is just not advertised as
# general upload. files.download / files.trash remain supported (verified).
_FILES_UPLOAD_BINARY_REASON = (
    "OneDrive binary uploads (.pdf/.docx) stage through Composio's Files REST "
    "API, which needs a project COMPOSIO_API_KEY (the MCP key 401s; source_url "
    "only works for already-public URLs). Plain-text files upload over MCP via "
    "ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE. Set COMPOSIO_API_KEY to enable general "
    "file upload."
)

# Human-readable reasons for why a specific provider doesn't support an action.
# Keyed by legacy action ids (callers pass legacy ids today).
UNSUPPORTED_REASONS: dict[tuple[str, str], str] = {
    ("google_api", "gmail.draft"): "google_api.py has no draft subcommand",
    ("google_api", "document.handoff"): "document.handoff requires gmail.draft, which google_api does not support",
    ("composio", "calendar.cancel"): "calendar.cancel is not offered for Composio Google "
                                     "(no restore-path parity with the soft-delete promise)",
    ("composio:mcp", "calendar.cancel"): "calendar.cancel is not offered for Composio Google "
                                         "(no restore-path parity with the soft-delete promise)",
    ("composio", "files.trash"): "Google Drive trash is not wired for Composio MCP yet",
    ("composio:mcp", "files.trash"): "Google Drive trash is not wired for Composio MCP yet",
    ("composio_microsoft", "files.upload"): _FILES_UPLOAD_BINARY_REASON,
    ("composio_microsoft:mcp", "files.upload"): _FILES_UPLOAD_BINARY_REASON,
    ("m365", "calendar.cancel"): "Microsoft Graph has no uncancel/restore path and the "
                                 "recreate-event workflow is not implemented, so cancel cannot "
                                 "be honoured behind the reversible soft-delete promise — "
                                 "cancel the event via Outlook, or delete and recreate it",
}

# Provider recommendations for each action/workflow.
PROVIDER_RECOMMENDATIONS: dict[str, str] = {
    "gmail.draft": "composio",
    "document.handoff": "composio",
    "gmail.send": "google_api, composio, or composio_microsoft",
    "calendar.create": "google_api or composio",
    "calendar.update": "google_api or composio",
    "drive.upload": "google_api or composio",
    "drive.download": "google_api or composio",
    "meeting.gather": "google_api or composio",
    "weekly.collect": "google_api or composio",
}


def _resolve_action(action: str) -> str:
    """Resolve a (possibly legacy) action key to its neutral equivalent."""
    return LEGACY_ACTION_ALIASES.get(action, action)


def get_capabilities(provider: str) -> dict[str, bool]:
    """Return capability dict for a provider. Unknown providers get empty dict.

    The returned dict is MERGED: it contains the neutral keys plus every legacy
    alias key (pointing at the same value) so both `caps["mail.send"]` and
    `caps["gmail.send"]` resolve. This preserves back-compat for callers that
    still use Gmail/Drive-flavored keys.
    """
    neutral = CAPABILITIES.get(provider)
    if not neutral:
        return {}
    merged: dict[str, bool] = dict(neutral)
    for legacy, target in LEGACY_ACTION_ALIASES.items():
        if target in neutral:
            merged[legacy] = neutral[target]
    return merged


def supports(provider: str, action: str) -> bool:
    """Check if a provider supports a specific action (neutral or legacy key)."""
    neutral = CAPABILITIES.get(provider, {})
    if not neutral:
        return False
    return bool(neutral.get(_resolve_action(action), False))


def unsupported_actions(provider: str) -> list[str]:
    """Return list of actions this provider does NOT support.

    Includes both neutral and legacy key spellings so callers checking either
    form find their key.
    """
    caps = get_capabilities(provider)
    return [action for action, supported in caps.items() if not supported]


def all_actions() -> list[str]:
    """Return all known action keys (neutral and legacy spellings)."""
    actions: set[str] = set()
    for caps in CAPABILITIES.values():
        actions.update(caps.keys())
    actions.update(LEGACY_ACTION_ALIASES.keys())
    return sorted(actions)


def recommend_provider_for(action: str) -> str:
    """Return a provider recommendation for a given action or workflow."""
    if action in PROVIDER_RECOMMENDATIONS:
        return PROVIDER_RECOMMENDATIONS[action]
    # Fall back to the neutral spelling if a legacy key was passed.
    return PROVIDER_RECOMMENDATIONS.get(_resolve_action(action), "google_api or composio")


def get_unsupported_reason(provider: str, action: str) -> str:
    """Return a human-readable reason for why a provider doesn't support an action."""
    if (provider, action) in UNSUPPORTED_REASONS:
        return UNSUPPORTED_REASONS[(provider, action)]
    return f"{action} is not supported by {provider}"


def workflow_supported(client: Any, workflow: str) -> tuple[bool, list[str]]:
    """Check if a client supports a derived workflow.

    Returns (supported, missing_actions).
    """
    requirements = WORKFLOW_REQUIREMENTS.get(workflow)
    if requirements is None:
        return (False, [workflow])
    missing = [a for a in requirements if not client.supports(a)]
    return (not missing, missing)


def require_capability(client: Any, action: str, target: str | None = None) -> dict[str, Any] | None:
    """Check if a client supports an action. Return None if supported,
    or an ActionResult-shaped error dict if not supported.

    Error messages include a specific reason and provider recommendation.
    """
    if not client.supports(action):
        reason = get_unsupported_reason(client.provider_name, action)
        recommendation = recommend_provider_for(action)
        return {
            "success": False,
            "action": action,
            "provider": client.provider_name,
            "target": target or "",
            "data": {},
            "error": f"{action} is not supported by provider {client.provider_name} because {reason}. "
                     f"Use provider={recommendation} for this workflow.",
            "audited": False,
        }
    return None
