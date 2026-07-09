#!/usr/bin/env python3
"""Provider capability matrix.

Each provider declares which workspace actions it supports.
Skills can check client.supports("drive.upload") before calling.
"""
from __future__ import annotations
from typing import Any

CAPABILITIES: dict[str, dict[str, bool]] = {
    "google_api": {
        "gmail.search": True,
        "gmail.draft": False,       # google_api.py has no draft subcommand
        "gmail.send": True,         # supported but destructive / guardrailed
        "calendar.list": True,
        "calendar.create": True,
        "calendar.update": True,
        "drive.search": True,
        "drive.upload": True,
        "drive.download": True,
    },
    "composio": {
        "gmail.search": True,
        "gmail.draft": True,
        "gmail.send": False,
        "calendar.list": True,
        "calendar.create": True,
        "calendar.update": True,
        "drive.search": True,
        "drive.upload": True,
        "drive.download": True,
    },
    "composio:mcp": {
        "gmail.search": True,
        "gmail.draft": True,
        "gmail.send": False,
        "calendar.list": True,
        "calendar.create": True,
        "calendar.update": True,
        "drive.search": True,
        "drive.upload": True,
        "drive.download": True,
    },
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
    """Return a provider recommendation for a given action."""
    if action in ("gmail.draft", "document.handoff"):
        return "composio"
    return "google_api or composio"


def require_capability(client: Any, action: str, target: str | None = None) -> dict[str, Any] | None:
    """Check if a client supports an action. Return None if supported,
    or an ActionResult-shaped error dict if not supported."""
    if not client.supports(action):
        return {
            "success": False,
            "action": action,
            "provider": client.provider_name,
            "target": target or "",
            "data": {},
            "error": f"{action} is not supported by provider {client.provider_name}. "
                     f"Use provider={recommend_provider_for(action)} for this workflow.",
            "audited": False,
        }
    return None