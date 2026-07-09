#!/usr/bin/env python3
"""Provider capability matrix.

Each provider declares which workspace actions it supports.
Skills can check client.supports("drive.upload") before calling.
"""
from __future__ import annotations

CAPABILITIES: dict[str, dict[str, bool]] = {
    "google_api": {
        "gmail.search": True,
        "gmail.draft": True,
        "gmail.send": True,
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