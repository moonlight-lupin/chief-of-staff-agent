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

A refusal in steps 2–4, or a :class:`NotAttempted` raised by the call before
any provider I/O, is recorded with ``attempted=False``: nothing can have
happened, so even a send stays retryable. Anything after the provider was
called is a real attempt, and a failed send or create is final
(``state_db.final_on_failure``). The caller validates lookup, state and type
before calling, and prints the outcome its own way.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping

_BREAK_GLASS = ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE")


class NotAttempted(Exception):
    """Raised by a call that refuses before any provider I/O."""


@dataclass
class Execution:
    ok: bool
    stage: str                 # claim | preflight | capability | grant | refused | call | provider | done
    result: Any = None
    error: str = ""
    account_check: Any = None


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


def run_approved(
    cfg: Any,
    action: Mapping[str, Any],
    client: Any,
    call: Callable[[Any, Mapping[str, Any]], Any],
    *,
    check_capability: bool = True,
    accept_non_dict: bool = False,
) -> Execution:
    """Claim, check, call and record one approved action.

    Refusals and provider errors come back as an :class:`Execution`; only a
    BaseException such as KeyboardInterrupt propagates (after the grant is
    released and the environment restored).
    """
    from company_registry import preflight
    from state_db import mark_executed, mark_executing, mark_failed

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
    try:
        with _approved_env():
            result = call(client, action)
    except NotAttempted as exc:
        return refuse("refused", str(exc))
    except Exception as exc:  # noqa: BLE001 — every provider error is recorded, never swallowed
        mark_failed(cfg, action_id, str(exc))
        return Execution(False, "call", error=str(exc), account_check=account_check)
    finally:
        grant.__exit__(None, None, None)

    if isinstance(result, dict):
        if not result.get("success", False):
            error = str(result.get("error") or "provider returned failure")
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
