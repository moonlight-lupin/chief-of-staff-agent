"""Readiness report rendering — extracted from chief_of_staff.py.

Pure render + emission helpers for the `readiness` command. Keeps the CLI
god-file under the decomposition contract (<2500 lines). Behavior is
identical to the previous inline block; chief_of_staff re-exports the
public names so `cmd_readiness` and tests see the same API.
"""

from __future__ import annotations

import sys
from typing import Any, Mapping, Sequence


_R_FAIL = "FAIL"
_R_PASS = "PASS"


def _emit_readiness_row_failures(rows: Sequence[Mapping[str, Any]]) -> None:
    """Emit a ``readiness_row_failed`` error event for each FAIL row (no-op when
    runtime_log is absent or no run is active)."""
    try:
        import runtime_log as _runtime_log_mod
    except ImportError:
        _runtime_log_mod = None
    _runtime_log = getattr(_runtime_log_mod, "log_event", None)
    if _runtime_log is None:
        return
    sanitize_detail = getattr(
        _runtime_log_mod,
        "sanitize_provider_error_detail",
        lambda value: str(value or "").replace("\n", " ").replace("\r", " ")[:240],
    )
    for row in rows:
        if not isinstance(row, Mapping) or str(row.get("status")) != _R_FAIL:
            continue
        try:
            _runtime_log(
                "readiness_row_failed",
                level="error",
                component="readiness",
                row=str(row.get("key", "")),
                message=sanitize_detail(row.get("detail", "") or ""),
            )
        except Exception as exc:
            print(
                f"readiness_row_failed emission error for "
                f"{row.get('key', '')!r}: {exc}",
                file=sys.stderr,
            )


def readiness_diagnose_pointer(payload: Mapping[str, Any], prefix: str = "") -> list[str]:
    """When any readiness row FAILed, point the operator at logs diagnose."""
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    has_fail = any(isinstance(r, Mapping) and str(r.get("status")) == _R_FAIL for r in rows)
    if not has_fail:
        return []
    run_id = payload.get("run_id")
    if not run_id:
        return []
    return [
        f"{prefix}Run ID: {run_id}",
        f"{prefix}Diagnose:",
        f"{prefix}  python shared/scripts/chief_of_staff.py logs diagnose --run-id {run_id}",
    ]


def render_readiness_summary(payload: Mapping[str, Any]) -> str:
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    verdicts = payload.get("verdicts") if isinstance(payload.get("verdicts"), Mapping) else {}
    lines: list[str] = ["Chief of Staff Readiness"]
    label_w = 26
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        label = str(row.get("label", ""))
        status = str(row.get("status", ""))
        detail = str(row.get("detail", ""))
        line = f"  {label.ljust(label_w)}{status}"
        # Surface the reason (and pointers) for anything not fully passing.
        if detail and status != _R_PASS:
            line += f"  — {detail}"
        lines.append(line)
    lines.append(
        f"  Ready for daily read-only operation: {verdicts.get('read_only_ready', 'NO')}"
    )
    lines.append(
        f"  Ready for approved execution: {verdicts.get('approved_execution_ready', 'NO')}"
    )
    lines.extend(readiness_diagnose_pointer(payload, prefix="  "))
    return "\n".join(lines)


def render_readiness_markdown(payload: Mapping[str, Any]) -> str:
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    verdicts = payload.get("verdicts") if isinstance(payload.get("verdicts"), Mapping) else {}
    lines: list[str] = [
        "# Chief of Staff Readiness",
        "",
        "| Check | Status | Detail |",
        "| --- | --- | --- |",
    ]
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        label = str(row.get("label", ""))
        status = str(row.get("status", ""))
        detail = str(row.get("detail", "")).replace("|", "\\|")
        lines.append(f"| {label} | {status} | {detail} |")
    lines.append("")
    lines.append(
        f"**Ready for daily read-only operation:** {verdicts.get('read_only_ready', 'NO')}"
    )
    lines.append(
        f"**Ready for approved execution:** {verdicts.get('approved_execution_ready', 'NO')}"
    )
    pointer = readiness_diagnose_pointer(payload)
    if pointer:
        lines.append("")
        lines.append(f"**Run ID:** {payload.get('run_id')}")
        lines.append("**Diagnose:**")
        lines.append("")
        lines.append(
            f"    python shared/scripts/chief_of_staff.py logs diagnose --run-id {payload.get('run_id')}"
        )
    return "\n".join(lines)