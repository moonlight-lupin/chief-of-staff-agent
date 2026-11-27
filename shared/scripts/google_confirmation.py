#!/usr/bin/env python3
"""Google's own confirmation prompt: who may answer it, and where it waits.

Google's Workspace MCP servers can pause a tool call and ask the user
(``input_required``). That prompt is a second gate after the plugin's own
approval, and it belongs to the user: the plugin never answers it. This module
holds the pieces around it:

* :func:`allow_persistent` — the operator's opt-in for "Always allow" choices
  (``integrations.workspace.google_mcp.allow_persistent_choices``, default
  false). Without it only ``ALLOW_ONCE`` and ``DENY_ONCE`` are ever offered.
* :func:`record_prompt` / :func:`get_prompt` / :func:`clear_prompt` — the
  pending prompt, kept in the user's own state store (kv
  ``google_confirmations``) so ``/cos confirm`` and ``review_queue.py
  preview`` can show it. ``state_db`` clears it when the action completes,
  fails finally, or is cancelled.
* :func:`terminal_confirmer` / :func:`default_confirmer` — a numbered prompt
  at a terminal. Never under Hermes, never without a TTY, and never on a store
  enrolled for signed approvals: there ``/cos confirm`` in the gateway is the
  only answer path. A TTY can be faked (a pty, an unset ``HERMES_SESSION_*``),
  so on an unsigned standalone install the terminal prompt is a convenience,
  not proof that a human answered — the same "not a sandbox" limit as the
  other TTY prompts. It never offers or accepts a persistent ``*_ALWAYS``
  choice, even when the operator opted in; those go only through
  ``/cos confirm``.
* :func:`next_step` — what to tell the user when nobody could be asked.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import plugin_secrets  # noqa: E402
from workspace_guardrails import ConfirmationRequired, is_persistent_choice  # noqa: E402

STORE = "google_confirmations"      # == state_db.CONFIRMATIONS_STORE
_MAX_TERMINAL_TRIES = 3

Confirmer = Callable[[ConfirmationRequired], "str | None"]


# ── operator policy ─────────────────────────────────────────────────────────

def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping) or hasattr(obj, "get"):
        try:
            return obj.get(key)
        except Exception:  # noqa: BLE001
            return None
    return None


def allow_persistent(config: Any) -> bool:
    """True only when the operator set ``allow_persistent_choices: true``."""
    workspace = _get(_get(config, "integrations") or {}, "workspace") or {}
    settings = _get(workspace, "google_mcp") or {}
    return _get(settings, "allow_persistent_choices") is True


# ── the stored prompt ───────────────────────────────────────────────────────

def _record(prompt: ConfirmationRequired, action_id: str = "") -> dict[str, Any]:
    return {
        "action_id": action_id,
        "server": prompt.server,
        "product": prompt.product,
        "tool": prompt.tool,
        "title": prompt.title,
        "message": prompt.message,
        "choices": [{"id": cid, "label": label} for cid, label in prompt.offered],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def record_prompt(config: Any, action_id: str, prompt: ConfirmationRequired) -> dict[str, Any]:
    """Store the pending prompt for ``action_id`` (replacing any earlier one).

    Kept in the user's own local store, so the message (recipients, excerpts)
    may be kept; the retry binding (``requestState``) and the tool arguments
    are not.
    """
    from state_db import _open_db

    rec = _record(prompt, action_id)

    def put(data: dict[str, Any]) -> None:
        prompts = data.setdefault("prompts", {})
        prompts[action_id] = rec

    with _open_db(config) as db:
        db.mutate_kv(STORE, put)
    return rec


def get_prompt(config: Any, action_id: str) -> dict[str, Any] | None:
    from state_db import _open_db

    try:
        with _open_db(config) as db:
            data = db.get_kv(STORE) or {}
    except Exception:  # noqa: BLE001 — no store yet means no prompt
        return None
    rec = (data.get("prompts") or {}).get(action_id)
    return dict(rec) if isinstance(rec, Mapping) else None


def clear_prompt(config: Any, action_id: str) -> None:
    from state_db import _open_db

    with _open_db(config) as db:
        db.clear_confirmation(action_id)


def same_prompt(record: Mapping[str, Any] | None, prompt: ConfirmationRequired) -> bool:
    """Is ``prompt`` the very question in ``record`` (server, tool, title, message, choice ids)?"""
    if not record:
        return False
    stored = {"server": record.get("server"), "tool": record.get("tool"), "title": record.get("title"),
              "message": record.get("message"),
              "choice_ids": [c.get("id") for c in record.get("choices") or [] if isinstance(c, Mapping)]}
    return stored == prompt.fingerprint()


# ── showing it ──────────────────────────────────────────────────────────────

def _fields(item: ConfirmationRequired | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(item, ConfirmationRequired):
        return _record(item)
    return dict(item)


def render(item: ConfirmationRequired | Mapping[str, Any], *, footer: str = "") -> str:
    """Google's product, server, tool, message and question with numbered choices."""
    f = _fields(item)
    lines = [f"=== {f.get('product') or 'Google Workspace'} ({f.get('tool')}) ===",
             f"Server: {f.get('server')}"]
    if f.get("message"):
        lines.append(str(f["message"]))
    lines.append(str(f.get("title") or "Confirm this action?"))
    for i, choice in enumerate(f.get("choices") or [], start=1):
        lines.append(f"  [{i}] {choice.get('label')} ({choice.get('id')})")
    if footer:
        lines.append(footer)
    return "\n".join(lines)


def signing_enforced(config: Any) -> bool:
    """True when the store is enrolled for signed approvals (fails closed)."""
    try:
        from approval_signing import enforced

        return bool(enforced(config))
    except Exception:  # noqa: BLE001 — unknown means enforced: no terminal answers
        return True


def next_step(action_id: str, config: Any = None) -> str:
    """What the user does when nobody could be asked here. Never done for them."""
    lead = f"Google is waiting for your confirmation; nothing was run and {action_id} stays approved. "
    hermes = f"`/<assistant name> confirm {action_id}` (also `/cos confirm {action_id}`) in Hermes"
    if config is not None and signing_enforced(config):
        return lead + f"This install uses signed approvals, so the only way to answer it is {hermes}."
    return (lead + f"Answer it yourself: run `review_queue.py execute --action-id {action_id}` in your own "
            f"terminal, or {hermes}.")


DIRECT_NEXT_STEP = ("Google is waiting for your confirmation; nothing was run. Run this command again "
                    "yourself in a terminal to answer Google's question, or queue it as an action "
                    "and answer with `/cos confirm <id>` in Hermes.")


def paused_result(prompt: ConfirmationRequired, *, action: str, provider: str, target: str = "",
                  note: str = "") -> dict[str, Any]:
    """An ActionResult-shaped failure for a direct CLI. Carries no message or arguments."""
    error = f"{prompt.summary()}. {note + ' ' if note else ''}{DIRECT_NEXT_STEP}"
    return {"success": False, "action": action, "provider": provider, "target": target,
            "data": {}, "error": error, "audited": True, "confirmation_required": True,
            "prompt": prompt.to_dict()}


# ── asking a human at a terminal ────────────────────────────────────────────

def terminal_confirmer(config: Any = None, *, input_fn: Callable[[str], str] = input,
                       out: Any = None) -> Confirmer:
    """A confirmer that shows the prompt and returns the typed numbered choice.

    Only the offered one-off choices are listed: a persistent ``*_ALWAYS``
    choice is never shown or returned here, whatever the opt-in. ``c``, an
    empty line, end of input or three answers that are not a listed number
    return None (nothing is sent).
    """

    def confirm(prompt: ConfirmationRequired) -> str | None:
        stream = out if out is not None else sys.stdout
        choices = [c for c in prompt.offered if not is_persistent_choice(c[0])]
        shown = {**_record(prompt), "choices": [{"id": cid, "label": label} for cid, label in choices]}
        print(render(shown), file=stream, flush=True)
        if not choices:
            return None
        for _ in range(_MAX_TERMINAL_TRIES):
            try:
                raw = input_fn(f"Enter choice (1-{len(choices)}, or c to cancel): ")
            except (EOFError, KeyboardInterrupt):
                return None
            raw = str(raw or "").strip().lower()
            if raw in ("", "c", "cancel", "q"):
                return None
            if raw.isdigit() and 1 <= int(raw) <= len(choices):
                choice = choices[int(raw) - 1][0]
                return None if is_persistent_choice(choice) else choice
            print(f"Choose a number from 1 to {len(choices)}, or c to cancel.", file=stream, flush=True)
        return None

    return confirm


def _interactive() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:  # noqa: BLE001 — a closed or replaced stream is not a terminal
        return False


def default_confirmer(config: Any) -> Confirmer | None:
    """A terminal confirmer at a TTY outside Hermes on an unsigned store, else None.

    On a store enrolled for signed approvals there is never a terminal
    confirmer: a pty and an unset ``HERMES_SESSION_*`` fake the other two
    checks, so ``/cos confirm`` in the gateway is the only answer path.
    """
    if signing_enforced(config) or not _interactive() or plugin_secrets.under_hermes():
        return None
    return terminal_confirmer(config)


def attach_confirmer(client: Any, config: Any) -> None:
    """For direct CLIs: let the human at this terminal answer Google's prompt."""
    if not hasattr(client, "confirmer"):
        return
    confirmer = default_confirmer(config)
    if confirmer is not None:
        client.confirmer = confirmer
