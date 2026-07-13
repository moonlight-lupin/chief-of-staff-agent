#!/usr/bin/env python3
"""Shared Composio toolkit-family resolution for workspace config.

Single source of truth for ``integrations.workspace.family`` resolution used by
``providers.composio_mcp_workspace`` and ``connect_workspace``.
"""
from __future__ import annotations

import warnings
from typing import Any, Mapping, Sequence

VALID_FAMILIES = ("google", "microsoft")

# Toolkit names that imply the microsoft family when family is not set explicitly.
MICROSOFT_TOOLKITS = frozenset({"outlook", "one_drive", "onedrive"})

# Toolkit names that imply the google family (used for mismatch warnings).
GOOGLE_TOOLKITS = frozenset({
    "gmail",
    "googlecalendar",
    "googledrive",
    "google_calendar",
    "google_drive",
})


def _normalize_toolkits(toolkits: Sequence[Any] | None) -> list[str]:
    if not isinstance(toolkits, (list, tuple)):
        return []
    return [str(t).strip().lower() for t in toolkits if t is not None and str(t).strip()]


def _resolve_composio_family(
    workspace: Mapping[str, Any] | None,
    *,
    toolkits: Sequence[Any] | None = None,
    warn: bool = True,
) -> str:
    """Resolve the Composio toolkit family from workspace config.

    Explicit ``family`` wins when it is one of :data:`VALID_FAMILIES`. Invalid
    explicit values default to ``google`` (and warn). When family is unset,
    microsoft toolkits in the list imply ``microsoft`` (and warn). Otherwise
    ``google``.
    """
    if not isinstance(workspace, Mapping):
        return "google"

    tk = _normalize_toolkits(
        toolkits if toolkits is not None else workspace.get("toolkits")
    )

    explicit = workspace.get("family")
    if explicit is not None and str(explicit).strip():
        fam = str(explicit).strip().lower()
        if fam in VALID_FAMILIES:
            return fam
        if warn:
            warnings.warn(
                f"integrations.workspace.family={explicit!r} is not one of "
                f"{VALID_FAMILIES}; defaulting to 'google'",
                UserWarning,
                stacklevel=2,
            )
        return "google"

    if any(t in MICROSOFT_TOOLKITS for t in tk):
        if warn:
            warnings.warn(
                "integrations.workspace.family not set but toolkits contain "
                "outlook/one_drive — inferring family='microsoft'. Set family "
                "explicitly in company.yaml to silence this warning.",
                UserWarning,
                stacklevel=2,
            )
        return "microsoft"
    return "google"


def warn_family_toolkit_mismatch(
    family: str,
    toolkits: Sequence[Any] | None,
) -> None:
    """Warn when configured family and toolkit names disagree."""
    fam = str(family or "").strip().lower()
    tk = set(_normalize_toolkits(toolkits))
    if not tk:
        return
    if fam == "microsoft" and tk & GOOGLE_TOOLKITS:
        warnings.warn(
            f"integrations.workspace.family='microsoft' but toolkits include "
            f"Google toolkit(s) {sorted(tk & GOOGLE_TOOLKITS)}; "
            "use outlook/one_drive for Microsoft or set family='google'.",
            UserWarning,
            stacklevel=2,
        )
    elif fam == "google" and tk & MICROSOFT_TOOLKITS:
        warnings.warn(
            f"integrations.workspace.family='google' but toolkits include "
            f"Microsoft toolkit(s) {sorted(tk & MICROSOFT_TOOLKITS)}; "
            "use gmail/googlecalendar/googledrive for Google or set "
            "family='microsoft'.",
            UserWarning,
            stacklevel=2,
        )
