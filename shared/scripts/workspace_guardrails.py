#!/usr/bin/env python3
"""Safe action guardrails for workspace write operations.

Provides:
- is_write_action(action) → bool
- requires_confirmation(action) → bool
- confirm_action(action, **details) → bool (checks env for auto-approve)
- ActionResult dataclass for standardized return objects

Environment:
- CHIEF_OF_STAFF_AUTO_APPROVE=1  → skip confirmation prompts (CI/automation)
- CHIEF_OF_STAFF_AUDIT_STRICT=pipeline,invoices  → fail on audit errors for those skills
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any

# Actions that modify external state (Gmail, Calendar, Drive)
WRITE_ACTIONS: frozenset[str] = frozenset({
    "gmail.draft",
    "gmail.send",
    "calendar.create",
    "calendar.update",
    "calendar.delete",
    "drive.upload",
    "drive.download",
    "drive.delete",
})

# Actions that are destructive and should always require explicit confirmation
DESTRUCTIVE_ACTIONS: frozenset[str] = frozenset({
    "gmail.send",
    "calendar.delete",
    "drive.delete",
})

# Actions that create new objects but don't destroy existing ones
# These are safe to auto-approve in most workflows
SAFE_WRITE_ACTIONS: frozenset[str] = frozenset({
    "gmail.draft",      # draft is not sent
    "calendar.create",  # creates new event, doesn't modify existing
    "drive.upload",     # uploads new file
    "drive.download",   # read-only (downloads to local)
})


def is_write_action(action: str) -> bool:
    """Check if an action modifies external state."""
    return action in WRITE_ACTIONS


def requires_confirmation(action: str) -> bool:
    """Check if an action requires explicit confirmation before executing.

    Destructive actions always require confirmation.
    Safe write actions only require confirmation if auto-approve is not set.
    """
    if action not in WRITE_ACTIONS:
        return False

    if action in DESTRUCTIVE_ACTIONS:
        return True  # always confirm destructive actions

    if action in SAFE_WRITE_ACTIONS:
        return not _is_auto_approved()

    return not _is_auto_approved()


def _is_auto_approved() -> bool:
    """Check if auto-approve env var is set."""
    return os.getenv("CHIEF_OF_STAFF_AUTO_APPROVE", "").strip() in ("1", "true", "yes")


def confirm_action(action: str, **details: Any) -> bool:
    """Check if an action should proceed.

    Returns True if:
    - The action is not a write action (always proceed)
    - Auto-approve is enabled and the action is not destructive
    - The user confirms interactively (if stdin is available)

    Returns False if:
    - The action is destructive and auto-approve is set (still blocks — use CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1)
    - The user declines
    - stdin is not available and auto-approve is not set
    """
    if not is_write_action(action):
        return True

    if action in DESTRUCTIVE_ACTIONS:
        if os.getenv("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", "").strip() not in ("1", "true", "yes"):
            print(f"⚠️  Destructive action '{action}' requires CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1", file=sys.stderr)
            return False
        # If destructive is allowed, still print warning
        print(f"⚠️  Proceeding with destructive action: {action}", file=sys.stderr)

    if not requires_confirmation(action):
        return True

    # Non-interactive: only proceed if auto-approved
    if not sys.stdin.isatty():
        if _is_auto_approved():
            return True
        print(f"⚠️  Action '{action}' requires confirmation but no TTY available. "
              f"Set CHIEF_OF_STAFF_AUTO_APPROVE=1 to proceed.", file=sys.stderr)
        return False

    # Interactive: ask user
    detail_str = ", ".join(f"{k}={v}" for k, v in details.items() if v)
    prompt = f"Proceed with {action}"
    if detail_str:
        prompt += f" ({detail_str})"
    prompt += "? [y/N] "

    try:
        response = input(prompt).strip().lower()
        return response in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


@dataclass
class ActionResult:
    """Standardized result object for all workspace write actions.

    Fields:
        success: bool — whether the action completed
        action: str — the action name (e.g. "gmail.draft")
        provider: str — the provider that executed (e.g. "composio:mcp")
        tool_slug: str — the Composio tool slug used (if applicable)
        target: str — a human-readable identifier for the target (email, event title, file name)
        data: dict — raw response data from the provider
        error: str | None — error message if not successful
        audited: bool — whether the action was written to the audit log
    """
    success: bool
    action: str = ""
    provider: str = ""
    tool_slug: str = ""
    target: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    audited: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "action": self.action,
            "provider": self.provider,
            "tool_slug": self.tool_slug,
            "target": self.target,
            "data": self.data,
            "error": self.error,
            "audited": self.audited,
        }