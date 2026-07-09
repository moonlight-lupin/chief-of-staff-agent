#!/usr/bin/env python3
"""Provider capability matrix.

Each provider declares which workspace actions it supports.
Skills can check client.supports("drive.upload") before calling.
Workflow-level capabilities are derived from base action capabilities.
"""
from __future__ import annotations
from typing import Any

CAPABILITIES: dict[str, dict[str, bool]] = {
    "google_api": {
        "gmail.search": True,
        "gmail.draft": False,       # google_api.py has no draft subcommand
        "gmail.send": True,         # supported but destructive / guardrailed
        "gmail.archive": True,      # via gmail modify --remove-labels INBOX
        "gmail.trash": True,        # via gmail modify --add-labels TRASH
        "calendar.list": True,
        "calendar.create": True,
        "calendar.update": True,
        "calendar.cancel": True,    # via calendar update --status cancelled
        "drive.search": True,
        "drive.upload": True,
        "drive.download": True,
        "drive.trash": True,        # via drive delete (default is trash, reversible)
    },
    "composio": {
        "gmail.search": True,
        "gmail.draft": True,
        "gmail.send": False,
        "gmail.archive": False,     # not exposed via Composio MCP
        "gmail.trash": False,       # not exposed via Composio MCP
        "calendar.list": True,
        "calendar.create": True,
        "calendar.update": True,
        "calendar.cancel": False,   # not exposed via Composio MCP
        "drive.search": True,
        "drive.upload": True,
        "drive.download": True,
        "drive.trash": False,       # not exposed via Composio MCP
    },
    "composio:mcp": {
        "gmail.search": True,
        "gmail.draft": True,
        "gmail.send": False,
        "gmail.archive": False,
        "gmail.trash": False,
        "calendar.list": True,
        "calendar.create": True,
        "calendar.update": True,
        "calendar.cancel": False,
        "drive.search": True,
        "drive.upload": True,
        "drive.download": True,
        "drive.trash": False,
    },
}

# Derived workflow capabilities — a workflow is supported if all base actions are supported.
WORKFLOW_REQUIREMENTS: dict[str, list[str]] = {
    "document.handoff": ["drive.upload", "gmail.draft"],
    "meeting.gather": ["calendar.list", "gmail.search", "drive.search"],
    "weekly.collect": ["calendar.list", "gmail.search", "drive.search"],
}

# Human-readable reasons for why a specific provider doesn't support an action.
UNSUPPORTED_REASONS: dict[tuple[str, str], str] = {
    ("google_api", "gmail.draft"): "google_api.py has no draft subcommand",
    ("google_api", "document.handoff"): "document.handoff requires gmail.draft, which google_api does not support",
    ("composio", "gmail.send"): "sending email is intentionally disabled for Composio MCP",
    ("composio:mcp", "gmail.send"): "sending email is intentionally disabled for Composio MCP",
}

# Provider recommendations for each action/workflow.
PROVIDER_RECOMMENDATIONS: dict[str, str] = {
    "gmail.draft": "composio",
    "document.handoff": "composio",
    "gmail.send": "google_api",
    "calendar.create": "google_api or composio",
    "calendar.update": "google_api or composio",
    "drive.upload": "google_api or composio",
    "drive.download": "google_api or composio",
    "meeting.gather": "google_api or composio",
    "weekly.collect": "google_api or composio",
}


def get_capabilities(provider: str) -> dict[str, bool]:
    """Return capability dict for a provider. Unknown providers get empty dict."""
    return dict(CAPABILITIES.get(provider, {}))


def supports(provider: str, action: str) -> bool:
    """Check if a provider supports a specific action."""
    return CAPABILITIES.get(provider, {}).get(action, False)


def unsupported_actions(provider: str) -> list[str]:
    """Return list of actions this provider does NOT support."""
    caps = CAPABILITIES.get(provider, {})
    return [action for action, supported in caps.items() if not supported]


def all_actions() -> list[str]:
    """Return all known action keys."""
    actions: set[str] = set()
    for caps in CAPABILITIES.values():
        actions.update(caps.keys())
    return sorted(actions)


def recommend_provider_for(action: str) -> str:
    """Return a provider recommendation for a given action or workflow."""
    return PROVIDER_RECOMMENDATIONS.get(action, "google_api or composio")


def get_unsupported_reason(provider: str, action: str) -> str:
    """Return a human-readable reason for why a provider doesn't support an action."""
    return UNSUPPORTED_REASONS.get((provider, action), f"{action} is not supported by {provider}")


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