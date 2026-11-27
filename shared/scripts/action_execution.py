#!/usr/bin/env python3
"""The one path from an approved action to a recorded outcome.

Every guarded executor (send_email, delete_actions, webhook_events) runs the
same sequence, and it lives here so a safety fix lands once:

1. **Claim** — ``mark_executing``: the approval is re-checked (expiry,
   integrity, signature, binding) and the action becomes exclusively ours.
2. **Account pre-flight** — the client acts as the account the action is bound
   to (``company_registry.preflight``).
3. **Capability gate** — the provider supports this action type.
4. **Execution grant** — a single-use, target-bound grant for this action
   (``workspace_guardrails.execution_grant``); the break-glass environment
   flags are set for the call only and restored afterwards.
5. **Call** — the executor's own provider call.
6. **Record** — ``mark_executed`` with the account check, or ``mark_failed``.

A refusal in steps 2–4, or a :class:`NotAttempted` raised by the call (before
any provider I/O, or by a provider that knows the server ran nothing — a 4xx or
a JSON-RPC error on Google's MCP servers), is recorded with ``attempted=False``:
nothing can have happened, so even a send stays retryable.

A :class:`ConfirmationRequired` — the workspace server paused the tool to ask
the user and ran nothing — is neither an attempt nor a retry:
``state_db.mark_awaiting_confirmation`` returns the action to approved without
touching ``retry_count`` (also on signed stores), and the prompt is kept in the
user's local store (``google_confirmation.record_prompt``) and returned on
``Execution.prompt`` for the caller to show (stage ``confirm``). The next run
claims afresh: approval, signature and binding are re-verified, and it gets a
new single-use grant. Only a human answers the prompt, through the
``confirmer`` the caller passes (a terminal prompt, or ``/cos confirm``).

Anything after the provider was called is a real attempt, and a failed send or
create is final (``state_db.final_on_failure``). A :class:`FinalFailure`
(Google's ``UserDeniedAction`` / ``AdminPolicyBlock``) is final for every
action type. The caller validates lookup, state and type before calling, and
prints the outcome its own way.
"""
from __future__ import annotations

import os
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping

from workspace_guardrails import ConfirmationRequired, FinalFailure, NotAttempted

__all__ = ["ConfirmationRequired", "Execution", "FinalFailure", "NotAttempted", "run_approved"]

_BREAK_GLASS = ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE")


@dataclass
class Execution:
    ok: bool
    stage: str                 # claim | preflight | capability | grant | refused | confirm | call | provider | done
    result: Any = None
    error: str = ""
    account_check: Any = None
    prompt: ConfirmationRequired | None = None   # stage "confirm": the server's question, unanswered


@contextmanager
def _approved_env() -> Iterator[None]:
    """The approval is the confirmation: open the guardrail's gates for this call only."""
    saved = {name: os.environ.get(name) for name in _BREAK_GLASS}
    try:
        for name in _BREAK_GLASS:
            os.environ[name] = "1"
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _approval_gate(cfg: Any) -> Any:
    """The env gate for an unsigned store; nothing for a signed one.

    On a store enrolled for signed approvals the execution grant (a
    contextvar) is what authorises the write, and ``confirm_action`` ignores
    the env flags. Setting them there would only leak them process-wide — in
    the long-lived Hermes gateway (``/cos confirm`` runs in-process) to every
    child it starts meanwhile — so they are never set.
    """
    try:
        from approval_signing import enforced

        signed = bool(enforced(cfg))
    except Exception:  # noqa: BLE001 — unknown means signed: never touch the env
        signed = True
    return nullcontext() if signed else _approved_env()


def run_approved(
    cfg: Any,
    action: Mapping[str, Any],
    client: Any,
    call: Callable[[Any, Mapping[str, Any]], Any],
    *,
    check_capability: bool = True,
    accept_non_dict: bool = False,
    confirmer: Callable[[ConfirmationRequired], Any] | None = None,
) -> Execution:
    """Claim, check, call and record one approved action.

    Refusals and provider errors come back as an :class:`Execution`; only a
    BaseException such as KeyboardInterrupt propagates (after the grant is
    released and the environment restored).

    ``confirmer`` is the human's answer to a workspace server's own prompt
    (``prompt -> choice id | None``). It is set on ``client.confirmer`` for
    this call only, when the client has one; it is never invented here.
    """
    from company_registry import preflight
    from state_db import mark_awaiting_confirmation, mark_executed, mark_executing, mark_failed

    action_id = action["id"]
    if not mark_executing(cfg, action_id):
        return Execution(False, "claim", error="cannot be executed (approval may have lapsed)")

    def refuse(stage: str, error: str) -> Execution:
        mark_failed(cfg, action_id, error, attempted=False)
        return Execution(False, stage, error=error)

    # Steps 2–4 do no provider I/O, so any error here is a refusal, never an
    # attempt: the action goes back to approved instead of sticking in executing.
    stage = "preflight"
    try:
        account_error, account_check = preflight(action, client, cfg)
        if account_error:
            return refuse("preflight", account_error)
        if check_capability:
            stage = "capability"
            from workspace_capabilities import require_capability

            unsupported = require_capability(client, action["type"], target=action.get("target", ""))
            if unsupported:
                return refuse("capability", unsupported.get("error") or f"{action['type']} not supported")
        stage = "grant"
        from workspace_guardrails import execution_grant

        grant = execution_grant(cfg, action_id)
        grant.__enter__()
    except Exception as exc:  # noqa: BLE001 — see above
        return refuse(stage, f"{type(exc).__name__}: {exc}" if not isinstance(exc, PermissionError) else str(exc))
    has_confirmer = confirmer is not None and hasattr(client, "confirmer")
    saved_confirmer = getattr(client, "confirmer", None) if has_confirmer else None
    try:
        if has_confirmer:
            client.confirmer = confirmer
        with _approval_gate(cfg):
            result = call(client, action)
    except ConfirmationRequired as exc:
        # The server asked the user and ran nothing: not an attempt and not a
        # retry, so the action returns to approved with its retry count
        # untouched. The error names the product and tool only — never the
        # message or arguments, which may hold recipients and a body.
        error = f"{exc.summary()}; the action was not attempted and stays approved"
        _record_prompt(cfg, action_id, exc)
        if mark_awaiting_confirmation(cfg, action_id, exc.summary()) is None:
            mark_failed(cfg, action_id, error, attempted=False)
        return Execution(False, "confirm", error=error, account_check=account_check, prompt=exc)
    except FinalFailure as exc:
        mark_failed(cfg, action_id, str(exc), final=True)
        return Execution(False, "call", error=str(exc), account_check=account_check)
    except NotAttempted as exc:
        return refuse("refused", str(exc))
    except Exception as exc:  # noqa: BLE001 — every provider error is recorded, never swallowed
        mark_failed(cfg, action_id, str(exc))
        return Execution(False, "call", error=str(exc), account_check=account_check)
    finally:
        if has_confirmer:
            client.confirmer = saved_confirmer
        grant.__exit__(None, None, None)

    if isinstance(result, dict):
        if not result.get("success", False):
            error = str(result.get("error") or "provider returned failure")
            if result.get("final"):
                mark_failed(cfg, action_id, error, final=True)
            elif result.get("not_attempted"):
                mark_failed(cfg, action_id, error, attempted=False)
            else:
                mark_failed(cfg, action_id, error)
            return Execution(False, "provider", result=result, error=error, account_check=account_check)
        mark_executed(cfg, action_id, {**result, "account_check": account_check})
    # None is never success: it says nothing about what happened. Other
    # non-dict results are kept as "raw" where the executor always did so.
    elif accept_non_dict and result is not None:
        mark_executed(cfg, action_id, {"raw": str(result), "account_check": account_check})
    else:
        mark_failed(cfg, action_id, "provider returned no structured result")
        return Execution(False, "provider", result=result, error="provider returned no structured result",
                         account_check=account_check)
    return Execution(True, "done", result=result, account_check=account_check)


def _record_prompt(cfg: Any, action_id: str, prompt: ConfirmationRequired) -> None:
    """Keep the prompt in the user's local store for ``/cos confirm`` and preview."""
    try:
        from google_confirmation import record_prompt

        record_prompt(cfg, action_id, prompt)
    except Exception:  # noqa: BLE001 — the record is a convenience; the state change is not
        pass
