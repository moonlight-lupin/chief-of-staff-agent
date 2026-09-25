#!/usr/bin/env python3
"""Capability report — one machine-readable answer to "what may I do here?".

An agent (or an operator) driving Chief of Staff otherwise has to infer its
operating envelope from prose spread across README.md, docs/SETUP.md and the
capability tables. This module returns the same facts the code already holds,
as a single JSON-serialisable object.

Kept out of chief_of_staff.py so the entrypoint stays within its size budget.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import state_sync  # noqa: E402
import workspace_guardrails  # noqa: E402

# Providers whose write paths have been exercised against a live account.
# Native m365 is code-complete but has never run against a real Entra tenant,
# and saying so out loud is cheaper than someone discovering it in production.
_LIVE_VERIFIED_PROVIDERS = frozenset({
    "google_api", "composio", "composio_google", "composio_microsoft", "agent",
})

_M365_VERIFICATION_NOTE = (
    "Native Microsoft 365 Graph is code-complete but has never been live-verified "
    "against a real Entra tenant. Capability flags for it are conservative and "
    "reads should be treated as unproven until a canary run passes. See the M365 "
    "section of docs/PRODUCTION_ROADMAP.md."
)


def build_capability_report(config: Any, version: str = "") -> dict[str, Any]:
    """Describe this installation's operating envelope in one JSON object.

    An agent driving Chief of Staff otherwise has to infer what it may do from
    prose spread across README, SETUP and the capability tables. This returns
    the same information the code already holds: the active provider, which
    actions it supports, which it refuses and *why*, whether the provider has
    ever been live-verified, where state lives, and whether that state will
    survive the session.
    """
    integrations = config.get("integrations", {}) if isinstance(config, dict) else {}
    workspace = integrations.get("workspace", {}) if isinstance(integrations, dict) else {}
    provider = str(workspace.get("provider", "google_api") or "google_api")

    supported: list[str] = []
    unsupported: list[str] = []
    unsupported_reasons: dict[str, str] = {}
    try:
        import workspace_capabilities as _caps
        from doctor_base import _capability_provider_for_workspace

        # composio splits into composio_google / composio_microsoft by mode.
        cap_provider = _capability_provider_for_workspace(workspace)
        for action in _caps.all_actions():
            if _caps.supports(cap_provider, action):
                supported.append(action)
            else:
                unsupported.append(action)
                reason = _caps.get_unsupported_reason(cap_provider, action)
                if reason:
                    unsupported_reasons[action] = reason
    except Exception as exc:  # pragma: no cover - capability table unavailable
        unsupported_reasons["_error"] = f"capability table unavailable: {exc}"

    project_root = ""
    paths = config.get("paths", {}) if isinstance(config, dict) else {}
    if isinstance(paths, dict):
        project_root = str(paths.get("project_root") or "")

    hosted = workspace_guardrails.in_hosted_session()
    refusal = workspace_guardrails.hosted_session_refusal(provider)

    # A git-backed project_root outlives the VM once pushed, so a hosted
    # session with one is durable — on the condition the note spells out.
    sync = state_sync.sync_status(project_root) if project_root else {"git_backed": False}
    # storage.mode is the operator's onboarding choice; None means never asked,
    # in which case a syncable clone is detected rather than required.
    mode = state_sync.storage_mode(config)
    sync["mode"] = mode
    git_durable = mode != "local" and bool(
        sync.get("git_backed") and sync.get("has_remote") and not sync.get("sync_refusal")
    )
    if mode == "git" and not git_durable:
        state_note = (
            "storage.mode is git but project_root is not a syncable clone of a data repo "
            f"({sync.get('sync_refusal') or sync.get('note')}). Re-run bootstrap with "
            "--storage git --data-repo <owner/name>."
        )
    elif git_durable:
        state_note = (
            f"State is git-backed ({sync.get('remote')}) and survives only once pushed: "
            "run chief_of_staff.py sync push before the session ends. "
            f"Currently: {sync.get('note')}"
        )
    elif hosted:
        state_note = (
            "State lives on an ephemeral cloud VM and will NOT survive this "
            "session. Anything worth keeping must be committed and pushed, or "
            "written back through a connector, before the session ends. Make "
            "project_root a clone of a private data repo to keep it (docs/CLAUDE_CODE.md)."
        )
    else:
        state_note = f"State persists on local disk under {project_root or '<project_root>'}."

    verified = provider in _LIVE_VERIFIED_PROVIDERS
    return {
        "version": version,
        "provider": provider,
        "provider_verified": verified,
        "provider_verification_note": "" if verified else _M365_VERIFICATION_NOTE,
        "supported": sorted(supported),
        "unsupported": sorted(unsupported),
        "unsupported_reasons": unsupported_reasons,
        "project_root": project_root,
        "hosted_session": hosted,
        "hosted_session_refusal": refusal or "",
        "state_persistent": (not hosted) or git_durable,
        "state_note": state_note,
        "state_sync": sync,
        "execution_seam": (
            "Guarded Python path: review_queue.py execute. Agent path (agent "
            "provider): review_queue.py claim → perform the action with your own "
            "tools → review_queue.py record-execution. Never call connector write "
            "tools for a Chief-of-Staff action without claiming it first."
        ),
    }


