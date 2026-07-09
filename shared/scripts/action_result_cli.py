#!/usr/bin/env python3
"""Shared CLI result printer for ActionResult-shaped dicts and workflow results.

Provides one consistent way to print action results across all skill scripts.
JSON is default for machine-readability; --summary gives human-readable output.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Ensure shared/scripts is importable
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))


def print_json(result: Any) -> None:
    """Print result as JSON."""
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


def summarize_result(result: dict[str, Any], label: str | None = None) -> str:
    """Return a human-readable summary string for an ActionResult or workflow dict."""
    lines: list[str] = []
    success = result.get("success", False)
    provider = result.get("provider", "?")
    audited = "yes" if result.get("audited") else "no"
    target = result.get("target", "")
    action = result.get("action", "")
    error = result.get("error")
    data = result.get("data", {})

    # Determine icon and label
    # Check for partial completion: steps exist, some succeeded, some didn't
    is_partial = False
    steps = result.get("steps")
    if steps and not success:
        has_success = any(s and s.get("success") for s in steps.values() if isinstance(s, dict))
        has_failure = any(s is None or (isinstance(s, dict) and not s.get("success")) for s in steps.values())
        is_partial = has_success and has_failure

    if is_partial:
        icon = "⚠️"
        lines.append(f"{icon} {label or action} partially completed")
    elif error and "not supported" in str(error).lower():
        icon = "❌"
        # If label already mentions "not supported" or "not available", use it directly
        if label and ("not supported" in label.lower() or "not available" in label.lower()):
            lines.append(f"{icon} {label} for provider {provider}")
        elif label:
            lines.append(f"{icon} {label} not available for provider {provider}")
        else:
            lines.append(f"{icon} {action} not available for provider {provider}")
    elif success:
        icon = "✅"
        lines.append(f"{icon} {label or action}" + (f": {target}" if target else ""))
    else:
        icon = "❌"
        lines.append(f"{icon} {label or action} failed" + (f": {target}" if target else ""))

    lines.append(f"Provider: {provider}")

    # Handle workflow steps (document.handoff)
    if steps:
        for step_name, step_result in steps.items():
            if step_result is None:
                lines.append(f"{step_name}: not attempted")
            elif isinstance(step_result, str):
                lines.append(f"{step_name}: {step_result}")
            elif isinstance(step_result, dict) and step_result.get("success"):
                lines.append(f"{step_name}: ✅ completed")
            elif isinstance(step_result, dict) and step_result.get("error") and "not supported" in str(step_result.get("error", "")).lower():
                lines.append(f"{step_name}: ❌ unsupported")
            elif isinstance(step_result, dict):
                lines.append(f"{step_name}: ❌ failed")
            else:
                lines.append(f"{step_name}: {step_result}")
        if not success and error and "not supported" in str(error).lower():
            from workspace_capabilities import recommend_provider_for
            rec = recommend_provider_for(action)
            lines.append(f"Recommended provider for full {action}: {rec}")
    else:
        # Single action result
        if audited == "yes":
            lines.append(f"Audited: yes")
        # Extract useful data fields
        for key in ("id", "path", "webViewLink", "htmlLink", "display_url"):
            if key in data:
                lines.append(f"{key}: {data[key]}")
        if error and "not supported" in str(error).lower():
            from workspace_capabilities import recommend_provider_for
            rec = recommend_provider_for(action)
            lines.append(f"Recommended provider: {rec}")
            # Extract reason from error — text between "because" and ". Use provider"
            reason = str(error)
            if "because" in reason:
                reason_part = reason.split("because", 1)[1]
                reason_part = reason_part.split(". Use provider")[0].strip()
                lines.append(f"Reason: {reason_part}")

    if error and "not supported" not in str(error).lower() and not steps:
        lines.append(f"Error: {error}")

    return "\n".join(lines)


def print_result(result: dict[str, Any], summary: bool = False, label: str | None = None) -> None:
    """Print a result dict as JSON (default) or human-readable summary."""
    if summary:
        print(summarize_result(result, label))
    else:
        print_json(result)