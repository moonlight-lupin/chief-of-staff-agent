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

# Actions that modify external state (Gmail, Calendar, Drive).
# Legacy gmail.*/drive.* ids are used by the Google/Composio providers;
# neutral mail.*/files.* ids are used by newer providers (m365). Semantics mirror
# each other exactly (e.g. mail.send gates like gmail.send).
#
# Both spellings MUST appear here. @guarded methods on Google/Composio emit the
# legacy ids (gmail.archive, gmail.trash, drive.trash, …); m365 emits the
# neutral ids. confirm_action() is default-deny: anything not in READ_ACTIONS
# or WRITE_ACTIONS is blocked, so omitting a mutation id is a bypass.
WRITE_ACTIONS: frozenset[str] = frozenset({
    "gmail.draft",
    "gmail.send",
    "mail.draft",
    "mail.send",
    "calendar.create",
    "calendar.update",
    "calendar.delete",
    "calendar.cancel",   # m365 gates here; google uses it too (approval-queue gated)
    "calendar.uncancel",
    "drive.upload",
    "drive.download",
    "drive.delete",
    "files.upload",
    "files.download",
    "files.trash",       # m365 OneDrive recycle-bin (reversible)
    "files.untrash",     # restore from Drive trash / OneDrive recycle bin
    "drive.trash",
    "drive.untrash",
    "mail.archive",      # m365 move -> Archive (reversible)
    "mail.unarchive",    # m365 move -> Inbox
    "mail.trash",        # m365 move -> Deleted Items (30-day recoverable)
    "mail.untrash",      # m365 move -> Inbox
    "mail.move",         # move to an arbitrary folder id / well-known name
    "mail.tag",          # m365 append Outlook category (trivially undoable)
    "mail.create_tag",   # m365 create Outlook master category
    "gmail.archive",
    "gmail.unarchive",
    "gmail.trash",
    "gmail.untrash",
    "gmail.label",
    "gmail.create_label",
})

# Explicit allowlist of read-only action IDs. confirm_action() permits these
# without a gate. Unknown IDs are denied (not treated as reads).
READ_ACTIONS: frozenset[str] = frozenset({
    "gmail.search",
    "gmail.read",
    "gmail.list",
    "gmail.labels.list",
    "calendar.list",
    "calendar.get",
    "calendar.search",
    "calendar.list_reminders",
    "drive.search",
    "drive.list",
    "drive.read",
    "mail.search",
    "mail.read",
    "mail.list_folders",
    "mail.list_tags",
    "files.search",
    "files.list",
    "files.read",
    # NOTE: "unknown" is intentionally NOT in READ_ACTIONS. Unknown action
    # IDs must be denied by default-deny, not allowed as reads. Event
    # classification's "unknown" category is separate from executable
    # action IDs and must not bypass the guardrail.
})

# Actions that are destructive and should always require explicit confirmation
DESTRUCTIVE_ACTIONS: frozenset[str] = frozenset({
    "gmail.send",
    "mail.send",
    "calendar.delete",
    "drive.delete",
})

# Actions that create new objects but don't destroy existing ones, OR whose
# effect is reversible by design (trash is 30-day recoverable; archive/tag/move
# are trivially undoable). These gate behind the auto-approve mechanism rather
# than executing unconditionally, and behind CHIEF_OF_STAFF_AUTO_APPROVE=1 in
# non-interactive contexts — they never require CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE.
SAFE_WRITE_ACTIONS: frozenset[str] = frozenset({
    "gmail.draft",      # draft is not sent
    "mail.draft",
    "calendar.create",  # creates new event, doesn't modify existing
    "drive.upload",     # uploads new file
    "drive.download",   # read-only (downloads to local)
    "files.upload",
    "files.download",
    # Reversible mutations (see WRITE_ACTIONS note above). Neutral and
    # legacy spellings are both listed so Google/Composio @guarded methods
    # gate the same way as m365.
    "files.trash",      # OneDrive recycle bin — reversible
    "files.untrash",    # restore from trash / recycle bin
    "drive.trash",
    "drive.untrash",
    "calendar.cancel",  # reversible on providers with an uncancel path
    "calendar.uncancel",
    "mail.archive",     # reversible: move back to Inbox
    "mail.unarchive",
    "mail.trash",       # reversible: 30-day Deleted Items recovery
    "mail.untrash",
    "mail.move",        # reversible: move back to previous folder
    "mail.tag",         # reversible: remove the category
    "mail.create_tag",  # reversible: delete the category
    "gmail.archive",
    "gmail.unarchive",
    "gmail.trash",
    "gmail.untrash",
    "gmail.label",
    "gmail.create_label",
})


def is_read_action(action: str) -> bool:
    """Check if an action is an explicit read-only allowlist member."""
    return action in READ_ACTIONS


def is_write_action(action: str) -> bool:
    """Check if an action modifies external state.

    Known writes (WRITE_ACTIONS) return True. Explicit reads return False.
    Unknown action IDs are treated as writes so default-deny cannot be
    bypassed by inventing a new mutation id.
    """
    if action in READ_ACTIONS:
        return False
    return True


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


def _log_guardrail_blocked(action: str, reason: str) -> None:
    """Emit a ``guardrail_blocked`` runtime event naming the action and the gate
    that refused it. Best-effort: no-ops when no run is active and never raises
    into the caller."""
    try:
        from runtime_log import log_event
        log_event(
            "guardrail_blocked", level="warning", component="guardrails",
            action=str(action), reason=str(reason),
        )
    except Exception:  # pragma: no cover - logging must never break the caller
        pass


def confirm_action(action: str, **details: Any) -> bool:
    """Check if an action should proceed.

    Returns True if:
    - The action is in READ_ACTIONS (reads always pass)
    - Auto-approve is enabled and the action is a non-destructive write
    - The user confirms interactively (if stdin is available)

    Returns False if:
    - The action is in neither READ_ACTIONS nor WRITE_ACTIONS (unknown = deny)
    - The action is destructive and auto-approve is set (still blocks — use CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1)
    - The user declines
    - stdin is not available and auto-approve is not set
    """
    if action in READ_ACTIONS:
        return True
    if action not in WRITE_ACTIONS:
        print(
            f"⚠️  Unknown action '{action}' is not in the read or write allowlists. "
            f"Blocked (default-deny).",
            file=sys.stderr,
        )
        _log_guardrail_blocked(action, "unknown_action")
        return False

    if action in DESTRUCTIVE_ACTIONS:
        if os.getenv("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", "").strip() not in ("1", "true", "yes"):
            print(
                f"⚠️  Destructive action '{action}' needs explicit user approval.\n"
                f"   Preferred: send_email.py prepare → approve → execute\n"
                f"   Or set CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1 after the user confirms,",
                file=sys.stderr,
            )
            _log_guardrail_blocked(action, "destructive_not_allowed")
            return False
        # Dual-gate: ALLOW_DESTRUCTIVE means the operator opted in; when
        # AUTO_APPROVE is also set (pending-action execute path), a human already
        # approved via the queue — skip a second interactive prompt.
        print(f"⚠️  Proceeding with destructive action: {action}", file=sys.stderr)
        if _is_auto_approved():
            return True

    if not requires_confirmation(action):
        return True

    # Non-interactive: only proceed if auto-approved
    if not sys.stdin.isatty():
        if _is_auto_approved():
            return True
        print(f"⚠️  Action '{action}' requires confirmation but no TTY available. "
              f"Set CHIEF_OF_STAFF_AUTO_APPROVE=1 to proceed.", file=sys.stderr)
        _log_guardrail_blocked(action, "no_tty_not_auto_approved")
        return False

    # Interactive: ask user
    detail_str = ", ".join(f"{k}={v}" for k, v in details.items() if v)
    prompt = f"Proceed with {action}"
    if detail_str:
        prompt += f" ({detail_str})"
    prompt += "? [y/N] "

    try:
        response = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        _log_guardrail_blocked(action, "interrupted")
        return False
    if response in ("y", "yes"):
        return True
    _log_guardrail_blocked(action, "user_declined")
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
    audit_tool: str | Callable[[Any], str] = "google_api.py",
    audit_operation: str | None = None,
    tool_slug: str | Callable[[Any], str] = "",
    block_error: str = "cancelled by guardrail",
) -> Callable[[Callable[..., Any]], Callable[..., dict[str, Any]]]:
    """Factor out the confirm_action -> run -> audit -> ActionResult boilerplate.

    Decorate a provider write method whose body performs the raw work and either
    returns a data dict (success) or raises an exception (failure). The wrapper:

      1. Resolves the target from the ``target_arg`` parameter.
      2. Calls ``confirm_action(action_id, **{target_arg: target})``; if the
         guardrail refuses, audits status="blocked" (best-effort) and returns
         an error ActionResult WITHOUT invoking the body.
      3. Invokes the body. On success, audits the action (audit failure is
         logged and ``audited=False`` — it never masks a successful mutation)
         and returns a success ActionResult wrapping the body's returned dict.
         On exception, audits a failure and returns an error ActionResult
         carrying ``str(exc)``.

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
            ("google_api.py" or a Composio tool slug). May be a callable that
            receives ``self`` for providers whose tool slug depends on runtime
            config, such as Composio toolkit family.
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
            resolved_audit_tool = audit_tool(self) if callable(audit_tool) else audit_tool
            resolved_tool_slug = tool_slug(self) if callable(tool_slug) else tool_slug

            if not confirm_action(action_id, **{target_arg: target}):
                # confirm_action already emitted guardrail_blocked with the
                # specific gate reason; emitting again here would duplicate the
                # event, so the decorator's block path stays silent. On success
                # nothing is logged either — the provider layer (_request)
                # covers the operational events. Denied writes ARE durably
                # audited (status="blocked") so they leave a trace even when
                # no run is active and runtime_log no-ops.
                try:
                    audit_workspace_action(
                        self.config, audit_provider, operation,
                        str(resolved_audit_tool), target=target, status="blocked",
                    )
                except Exception:  # pragma: no cover - audit must never mask the block
                    pass
                return ActionResult(
                    success=False, action=action_id, provider=provider,
                    tool_slug=str(resolved_tool_slug), target=target, error=block_error,
                ).to_dict()

            try:
                data = fn(self, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — provider errors become ActionResult
                audit_workspace_action(
                    self.config, audit_provider, operation, str(resolved_audit_tool),
                    target=target, status="failed",
                )
                return ActionResult(
                    success=False, action=action_id, provider=provider,
                    tool_slug=str(resolved_tool_slug), target=target, error=str(exc), audited=True,
                ).to_dict()

            # Audit the success — failure here must NOT mask the mutation.
            audit_ok = True
            try:
                audit_workspace_action(
                    self.config, audit_provider, operation, str(resolved_audit_tool),
                    target=target,
                )
            except Exception:
                audit_ok = False
                try:
                    from runtime_log import log_event
                    log_event(
                        "audit_failed", level="warning", component="guardrails",
                        action=str(action_id), operation=str(operation),
                        target=target,
                    )
                except Exception:  # pragma: no cover - logging must never break the caller
                    pass

            return ActionResult(
                success=True, action=action_id, provider=provider,
                tool_slug=str(resolved_tool_slug), target=target,
                data=data if isinstance(data, dict) else {}, audited=audit_ok,
            ).to_dict()

        return wrapper

    return decorator
