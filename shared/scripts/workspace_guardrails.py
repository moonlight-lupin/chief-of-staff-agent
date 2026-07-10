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

import functools
import inspect
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

# Actions that modify external state (Gmail, Calendar, Drive)
# Legacy gmail.*/drive.* ids are used by the Google/Composio providers;
# neutral mail.*/files.* ids are used by newer providers (m365). Semantics mirror
# each other exactly (e.g. mail.send gates like gmail.send).
WRITE_ACTIONS: frozenset[str] = frozenset({
    "gmail.draft",
    "gmail.send",
    "mail.draft",
    "mail.send",
    "calendar.create",
    "calendar.update",
    "calendar.delete",
    "drive.upload",
    "drive.download",
    "drive.delete",
    "files.upload",
    "files.download",
})

# Actions that are destructive and should always require explicit confirmation
DESTRUCTIVE_ACTIONS: frozenset[str] = frozenset({
    "gmail.send",
    "mail.send",
    "calendar.delete",
    "drive.delete",
})

# Actions that create new objects but don't destroy existing ones
# These are safe to auto-approve in most workflows
SAFE_WRITE_ACTIONS: frozenset[str] = frozenset({
    "gmail.draft",      # draft is not sent
    "mail.draft",
    "calendar.create",  # creates new event, doesn't modify existing
    "drive.upload",     # uploads new file
    "drive.download",   # read-only (downloads to local)
    "files.upload",
    "files.download",
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


def guarded(
    action_id: str,
    *,
    target_arg: str,
    audit_provider: str,
    audit_tool: str = "google_api.py",
    audit_operation: str | None = None,
    tool_slug: str = "",
    block_error: str = "cancelled by guardrail",
) -> Callable[[Callable[..., Any]], Callable[..., dict[str, Any]]]:
    """Factor out the confirm_action -> run -> audit -> ActionResult boilerplate.

    Decorate a provider write method whose body performs the raw work and either
    returns a data dict (success) or raises an exception (failure). The wrapper:

      1. Resolves the target from the ``target_arg`` parameter.
      2. Calls ``confirm_action(action_id, **{target_arg: target})``; if the
         guardrail refuses, returns an error ActionResult WITHOUT invoking the
         body (nothing is audited).
      3. Invokes the body. On success, audits the action and returns a success
         ActionResult wrapping the body's returned dict. On exception, audits a
         failure and returns an error ActionResult carrying ``str(exc)``.

    The action id is passed explicitly so existing providers keep emitting their
    LEGACY ids ("gmail.send", "drive.upload", "calendar.create", ...) — stored
    pending-action queues and tests depend on those exact strings. Future
    providers pass neutral ids ("mail.send", "files.upload", ...).

    Args:
        action_id: ActionResult/confirm action id (also the default audit op).
        target_arg: name of the wrapped-method parameter holding the target.
        audit_provider: provider string written to the audit log
            (e.g. "google_api", "composio").
        audit_tool: tool identifier for the audit log
            ("google_api.py" or a Composio tool slug).
        audit_operation: audit operation string; defaults to ``action_id``.
        tool_slug: ActionResult.tool_slug value (Composio slug or "").
        block_error: error message returned when the guardrail blocks the action.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., dict[str, Any]]:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
            # Import inside the wrapper so unittest.mock.patch of these module
            # attributes (workspace_guardrails.confirm_action /
            # workspace_audit.audit_workspace_action) is honoured at call time.
            from workspace_audit import audit_workspace_action
            from workspace_guardrails import ActionResult, confirm_action

            bound = sig.bind(self, *args, **kwargs)
            bound.apply_defaults()
            target = str(bound.arguments.get(target_arg, "") or "")
            provider = getattr(self, "provider_name", None) or getattr(self, "_provider_name", "unknown")
            operation = audit_operation or action_id

            if not confirm_action(action_id, **{target_arg: target}):
                return ActionResult(
                    success=False, action=action_id, provider=provider,
                    tool_slug=tool_slug, target=target, error=block_error,
                ).to_dict()

            try:
                data = fn(self, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — provider errors become ActionResult
                audit_workspace_action(
                    self.config, audit_provider, operation, audit_tool,
                    target=target, status="failed",
                )
                return ActionResult(
                    success=False, action=action_id, provider=provider,
                    tool_slug=tool_slug, target=target, error=str(exc), audited=True,
                ).to_dict()

            audit_workspace_action(
                self.config, audit_provider, operation, audit_tool, target=target,
            )
            return ActionResult(
                success=True, action=action_id, provider=provider,
                tool_slug=tool_slug, target=target,
                data=data if isinstance(data, dict) else {}, audited=True,
            ).to_dict()

        return wrapper

    return decorator