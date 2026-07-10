#!/usr/bin/env python3
"""Risk classification helpers for pending Chief-of-Staff actions.

The daily briefing and review queue both use these helpers to present
operator-facing risk levels consistently before any action is executed.
"""
from __future__ import annotations


HIGH_RISK_TYPES: set[str] = {
    "gmail.send",
    "gmail.trash",
    "drive.trash",
    "calendar.cancel",
}

MEDIUM_RISK_TYPES: set[str] = {
    "calendar.create",
    "calendar.update",
    "drive.upload",
    "gmail.archive",
}

LOW_RISK_TYPES: set[str] = {
    "gmail.label",
    "gmail.create_label",
    "drive.search",
    "gmail.search",
    "drive.download",
}

ACTION_RISK_MAP: dict[str, str] = {
    **{action_type: "high" for action_type in HIGH_RISK_TYPES},
    **{action_type: "medium" for action_type in MEDIUM_RISK_TYPES},
    **{action_type: "low" for action_type in LOW_RISK_TYPES},
}

RISK_LEVELS: tuple[str, str, str] = ("high", "medium", "low")

_RISK_ICONS: dict[str, str] = {
    "high": "🔴",
    "medium": "🟡",
    "low": "🟢",
}

_RISK_EXPLANATIONS: dict[str, str] = {
    "high": "This action can change or remove user-visible data and should be reviewed before execution.",
    "medium": "This action changes workspace state and should be checked for accuracy before execution.",
    "low": "This action is read-only or low-impact and is generally safe to run.",
}

_ACTION_EXPLANATIONS: dict[str, str] = {
    "gmail.send": "Sending email can contact external recipients and cannot be fully undone.",
    "gmail.trash": "Trashing email removes it from normal mailbox views and may hide important correspondence.",
    "drive.trash": "Trashing Drive files removes them from normal file views and can disrupt shared work.",
    "calendar.cancel": "Cancelling calendar events can notify attendees and remove scheduled commitments.",
    "calendar.create": "Creating calendar events changes schedules and may notify invited attendees.",
    "calendar.update": "Updating calendar events changes schedules and may notify invited attendees.",
    "drive.upload": "Uploading files changes Drive contents and may expose documents to configured sharing rules.",
    "gmail.archive": "Archiving email removes it from the inbox and may make follow-up easier to miss.",
    "gmail.label": "Applying Gmail labels organizes messages without changing their contents or recipients.",
    "gmail.create_label": "Creating Gmail labels changes mailbox organization without affecting existing message contents.",
    "drive.search": "Searching Drive reads metadata or file listings without changing workspace data.",
    "gmail.search": "Searching Gmail reads message listings without changing mailbox data.",
    "drive.download": "Downloading Drive files reads existing data without modifying the workspace.",
}


def get_action_risk(action_type: str) -> str:
    """Return the risk level for an action type, defaulting unknown actions to low."""
    return ACTION_RISK_MAP.get(action_type, "low")


def get_risk_icon(risk: str) -> str:
    """Return the display icon for a risk level."""
    return _RISK_ICONS.get(risk, "🟢")


def group_actions_by_risk(actions: list[dict]) -> dict[str, list[dict]]:
    """Group action dictionaries by risk level.

    Each action is expected to contain an ``action_type`` key. Missing or unknown
    action types are grouped as low risk.
    """
    grouped: dict[str, list[dict]] = {risk: [] for risk in RISK_LEVELS}
    for action in actions:
        action_type = str(action.get("action_type", ""))
        risk = get_action_risk(action_type)
        grouped[risk].append(action)
    return grouped


def get_risk_explanation(action_type: str, risk: str) -> str:
    """Return a deterministic explanation for why an action's risk matters."""
    if action_type in _ACTION_EXPLANATIONS:
        return _ACTION_EXPLANATIONS[action_type]
    return _RISK_EXPLANATIONS.get(risk, _RISK_EXPLANATIONS["low"])
