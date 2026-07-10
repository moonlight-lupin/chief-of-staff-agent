#!/usr/bin/env python3
"""Deterministic, rule-based diagnosis of Chief-of-Staff operational runs (v0.3.4).

This module reads the JSONL event stream and ``summary.json`` produced by
``runtime_log.py`` and maps them, through a table of hand-written rules, to
plain-English findings with concrete remediation. It performs **no** LLM or
network calls — every classification is a pure predicate over the parsed
events and the run summary, so the same run always yields the same diagnosis.

Design
------
``CLASSIFICATIONS`` is an ordered list. Each entry declares:

    id            stable identifier (also used by tests / bundles)
    severity      "error" | "warning" | "info"
    matcher       callable(events, summary) -> list[str] of evidence strings;
                  a non-empty return means "this rule fired" and the strings
                  become the finding's evidence.
    explanation   plain-English "likely cause"
    remediation   safe next steps a human can take
    next_commands exact commands to run (list[str])
    retry_safe    True when re-running the same operation is safe
    config_change True when the fix requires editing configuration/credentials
                  (drives the "No configuration change is currently indicated."
                  line — shown only when the primary finding does NOT need one)

``analyse_run`` returns a stable dict; ``format_diagnosis`` renders it as
human text, JSON, or Markdown.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

# ─── Severity ordering ───────────────────────────────────────────────────────

_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}


def _sev_rank(severity: str) -> int:
    return _SEVERITY_RANK.get(str(severity), 3)


# ─── Parsing ─────────────────────────────────────────────────────────────────


def _read_events(run_dir: Path) -> list[dict[str, Any]]:
    """Parse ``events.jsonl`` — tolerant of blank/corrupt lines."""
    events: list[dict[str, Any]] = []
    path = run_dir / "events.jsonl"
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return events
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _read_summary(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "summary.json"
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


# ─── Matcher helpers ─────────────────────────────────────────────────────────

# Field names that may carry human-facing text (hints, error messages, reasons).
_TEXT_FIELDS = (
    "message",
    "reason",
    "error",
    "detail",
    "hint",
    "command_line",
    "first_error",
)


def _event_text(ev: Mapping[str, Any]) -> str:
    """Concatenate all string field values of an event, lowercased."""
    parts: list[str] = []
    for value in ev.values():
        if isinstance(value, str):
            parts.append(value)
    return " ".join(parts).lower()


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _status(ev: Mapping[str, Any]) -> int | None:
    return _int(ev.get("status_code"))


def _failed_events(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [e for e in events if e.get("event") in {"provider_request_failed", "action_failed"}]


def _events_named(events: Sequence[Mapping[str, Any]], name: str) -> list[Mapping[str, Any]]:
    return [e for e in events if e.get("event") == name]


def _all_text(events: Sequence[Mapping[str, Any]], summary: Mapping[str, Any]) -> str:
    """Every text fragment across events + summary (first_error, warnings)."""
    parts: list[str] = [_event_text(e) for e in events]
    fe = summary.get("first_error")
    if isinstance(fe, str):
        parts.append(fe.lower())
    warns = summary.get("warnings")
    if isinstance(warns, list):
        parts.extend(str(w).lower() for w in warns)
    return " \n ".join(parts)


def _describe(ev: Mapping[str, Any]) -> str:
    """One-line, human-readable evidence string for a provider/action event."""
    bits: list[str] = [str(ev.get("event", "event"))]
    for key in ("provider", "operation", "action_type", "method", "endpoint_category"):
        val = ev.get(key)
        if val:
            bits.append(str(val))
    status = ev.get("status_code")
    if status is not None:
        bits.append(f"status {status}")
    for key in ("error_class", "reason", "message", "error"):
        val = ev.get(key)
        if val:
            bits.append(str(val))
            break
    return " ".join(bits)


# Hint/marker vocabularies (lowercased substring tests).
_REFRESH_MARKERS = ("refresh", "token expired", "expired token", "access token", "reauth", "re-authenticate")
_CREDENTIAL_MARKERS = (
    "invalid_client",
    "expired secret",
    "client secret",
    "credentials rejected",
    "invalid credentials",
    "credential",
    "aadsts7000215",
    "aadsts700",
    "unauthorized_client",
)
_ADMIN_CONSENT_MARKERS = ("admin consent", "admin-consent", "adminconsent", "consent required")
_MAILBOX_MARKERS = ("/users", "user_principal", "user principal", "mailboxnotfound", "resourcenotfound", "recipientnotfound")
_ONEDRIVE_MARKERS = ("onedrive", "drive provision", "provisioned", "provisioning", "mysitenotfound", "not provisioned")
_TIMEOUT_MARKERS = (
    "timeout",
    "timed out",
    "readtimeout",
    "connecttimeout",
    "connection reset",
    "connection aborted",
    "connectionerror",
    "max retries",
    "network is unreachable",
    "temporarily unavailable",
)
_CONFIG_MARKERS = ("company.yaml", "config not loaded", "configerror", "could not load config", "configuration error", "invalid configuration", "missing config")
_SCHEMA_MARKERS = ("schemaerror", "schema validation", "schema error", "does not match schema", "failed validation", "invalid payload")
_YAML_MARKERS = ("yamlerror", "yaml parse", "scannererror", "parsererror", "mapping values are not allowed", "could not find expected", "while parsing a", "yaml.")
_LOCK_MARKERS = ("lock timeout", "could not acquire lock", "locktimeout", "lock held", "failed to acquire lock", "file lock")


def _has_any(text: str, markers: Sequence[str]) -> bool:
    return any(m in text for m in markers)


# ─── Individual matchers ─────────────────────────────────────────────────────


def _m_auth_expired(events, summary):
    ev_text = _all_text(events, summary)
    out: list[str] = []
    for e in _failed_events(events):
        if _status(e) == 401 or e.get("error_class") == "auth":
            text = _event_text(e)
            if _has_any(text, _CREDENTIAL_MARKERS):
                continue  # credential rejection → invalid_credentials, not expiry
            if _status(e) == 401 and _has_any(text, _REFRESH_MARKERS):
                out.append(_describe(e))
    if not out and _has_any(ev_text, _REFRESH_MARKERS) and "401" in ev_text and not _has_any(ev_text, _CREDENTIAL_MARKERS):
        out.append("401 with token-refresh hint in run output")
    return out


def _m_invalid_credentials(events, summary):
    out: list[str] = []
    for e in _failed_events(events):
        text = _event_text(e)
        if _has_any(text, _CREDENTIAL_MARKERS) and (_status(e) in (401, 403) or e.get("error_class") == "auth"):
            out.append(_describe(e))
    return out


def _m_admin_consent_missing(events, summary):
    out: list[str] = []
    for e in _failed_events(events):
        if _status(e) == 403 and _has_any(_event_text(e), _ADMIN_CONSENT_MARKERS):
            out.append(_describe(e))
    return out


def _m_permission_denied(events, summary):
    out: list[str] = []
    for e in _failed_events(events):
        if _status(e) != 403:
            continue
        text = _event_text(e)
        if _has_any(text, _ADMIN_CONSENT_MARKERS) or _has_any(text, _ONEDRIVE_MARKERS):
            continue  # handled by more specific rules
        if _status(e) == 403 or e.get("error_class") == "permission_denied":
            out.append(_describe(e))
    return out


def _m_mailbox_not_found(events, summary):
    out: list[str] = []
    for e in _failed_events(events):
        if _status(e) != 404:
            continue
        text = _event_text(e)
        cat = str(e.get("endpoint_category", "")).lower()
        if _has_any(text, _MAILBOX_MARKERS) or cat in {"mail", "users"}:
            out.append(_describe(e))
    return out


def _m_onedrive_not_provisioned(events, summary):
    out: list[str] = []
    for e in _failed_events(events):
        if _status(e) == 403 and _has_any(_event_text(e), _ONEDRIVE_MARKERS):
            out.append(_describe(e))
    return out


def _m_throttled(events, summary):
    out: list[str] = []
    for e in events:
        if e.get("event") == "provider_retry" or e.get("error_class") == "throttled" or _status(e) == 429:
            if e.get("event") in {"provider_retry", "provider_request_failed", "provider_request_completed", "provider_request_started"}:
                out.append(_describe(e))
    return out


def _m_retry_deferred(events, summary):
    return [_describe(e) for e in _events_named(events, "retry_deferred")]


def _m_ambiguous_write(events, summary):
    return [_describe(e) for e in _events_named(events, "ambiguous_write")]


def _m_network_timeout(events, summary):
    out: list[str] = []
    for e in _failed_events(events):
        text = _event_text(e)
        if e.get("error_class") == "network" or _has_any(text, _TIMEOUT_MARKERS):
            out.append(_describe(e))
    return out


def _m_provider_unavailable(events, summary):
    out: list[str] = []
    for e in _failed_events(events):
        if _status(e) in (503, 504):
            method = str(e.get("method", "")).upper()
            cat = str(e.get("endpoint_category", "")).lower()
            # Idempotent paths: GET / HEAD or a read endpoint category.
            if method in {"", "GET", "HEAD"} or cat in {"read", "mail", "calendar", "files"}:
                out.append(_describe(e))
    return out


def _m_invalid_configuration(events, summary):
    text = _all_text(events, summary)
    if _has_any(text, _CONFIG_MARKERS):
        fe = summary.get("first_error")
        return [str(fe)] if fe else ["configuration-loading error in run output"]
    return []


def _m_schema_validation_failed(events, summary):
    text = _all_text(events, summary)
    if _has_any(text, _SCHEMA_MARKERS):
        fe = summary.get("first_error")
        return [str(fe)] if fe and _has_any(str(fe).lower(), _SCHEMA_MARKERS) else ["schema validation error in run output"]
    return []


def _m_corrupt_yaml(events, summary):
    text = _all_text(events, summary)
    if _has_any(text, _YAML_MARKERS):
        fe = summary.get("first_error")
        return [str(fe)] if fe and _has_any(str(fe).lower(), _YAML_MARKERS) else ["YAML parse error in run output"]
    return []


def _m_file_lock_timeout(events, summary):
    text = _all_text(events, summary)
    if _has_any(text, _LOCK_MARKERS):
        return ["file-lock timeout while writing a state store"]
    return []


def _m_guardrail_blocked(events, summary):
    return [_describe(e) for e in _events_named(events, "guardrail_blocked")]


def _m_pagination_truncated(events, summary):
    out: list[str] = []
    for e in _events_named(events, "pagination_truncated"):
        cap = e.get("cap")
        pages = e.get("pages_followed")
        out.append(f"pagination_truncated cap={cap} pages_followed={pages}")
    return out


def _m_audit_write_failed(events, summary):
    return [_describe(e) for e in _events_named(events, "audit_write_failed")]


# ─── Classification table ────────────────────────────────────────────────────

CLASSIFICATIONS: list[dict[str, Any]] = [
    {
        "id": "invalid_configuration",
        "severity": "error",
        "matcher": _m_invalid_configuration,
        "explanation": "company.yaml could not be loaded or is missing required fields, so the run had no usable configuration.",
        "remediation": "Validate and repair the configuration, then re-run the doctor to confirm it loads.",
        "next_commands": [
            "python shared/scripts/doctor.py --summary",
            "python shared/scripts/chief_of_staff.py doctor --summary",
        ],
        "retry_safe": False,
        "config_change": True,
    },
    {
        "id": "schema_validation_failed",
        "severity": "error",
        "matcher": _m_schema_validation_failed,
        "explanation": "An input payload failed schema validation (SchemaError); the data shape did not match what the schema requires.",
        "remediation": "Correct the offending input to match shared/scripts/schemas.py, then re-run.",
        "next_commands": [
            "python shared/scripts/doctor.py --summary",
        ],
        "retry_safe": False,
        "config_change": True,
    },
    {
        "id": "corrupt_yaml",
        "severity": "error",
        "matcher": _m_corrupt_yaml,
        "explanation": "A YAML file could not be parsed (syntax/indentation error), so the source could not be read.",
        "remediation": "Fix the YAML syntax (check indentation and quoting) and re-run the doctor to confirm it parses.",
        "next_commands": [
            "python shared/scripts/doctor.py --summary",
        ],
        "retry_safe": False,
        "config_change": True,
    },
    {
        "id": "invalid_credentials",
        "severity": "error",
        "matcher": _m_invalid_credentials,
        "explanation": "The provider rejected the credentials themselves (e.g. an expired client secret or invalid client) — a refresh will not fix this.",
        "remediation": "Rotate the credential / client secret and reconnect the workspace, then verify authentication.",
        "next_commands": [
            "python shared/scripts/connect_workspace.py --reconnect",
            "python shared/scripts/chief_of_staff.py readiness --summary",
        ],
        "retry_safe": False,
        "config_change": True,
    },
    {
        "id": "auth_expired",
        "severity": "error",
        "matcher": _m_auth_expired,
        "explanation": "The access token was expired or rejected (HTTP 401) and a token refresh is indicated before the operation can succeed.",
        "remediation": "Refresh/reconnect the workspace credentials, then re-run the operation.",
        "next_commands": [
            "python shared/scripts/connect_workspace.py --reconnect",
            "python shared/scripts/chief_of_staff.py readiness --summary",
        ],
        "retry_safe": True,
        "config_change": False,
    },
    {
        "id": "admin_consent_missing",
        "severity": "error",
        "matcher": _m_admin_consent_missing,
        "explanation": "The provider returned HTTP 403 because the app is missing tenant admin consent for the requested scope.",
        "remediation": "Have a workspace administrator grant admin consent for the required Graph permissions, then re-verify.",
        "next_commands": [
            "python shared/scripts/chief_of_staff.py readiness --summary",
        ],
        "retry_safe": False,
        "config_change": True,
    },
    {
        "id": "onedrive_not_provisioned",
        "severity": "error",
        "matcher": _m_onedrive_not_provisioned,
        "explanation": "Files access failed with HTTP 403 because the user's OneDrive/drive has not been provisioned yet.",
        "remediation": "Provision the user's OneDrive (open it once in the browser or provision via admin), then re-run files checks.",
        "next_commands": [
            "python shared/scripts/chief_of_staff.py readiness --summary",
        ],
        "retry_safe": False,
        "config_change": True,
    },
    {
        "id": "mailbox_not_found",
        "severity": "error",
        "matcher": _m_mailbox_not_found,
        "explanation": "The provider returned HTTP 404 for the mailbox/user — the configured user principal or delegate address is wrong or unlicensed.",
        "remediation": "Correct the delegate/user principal in company.yaml (and confirm the mailbox is licensed), then re-verify.",
        "next_commands": [
            "python shared/scripts/chief_of_staff.py readiness --summary",
        ],
        "retry_safe": False,
        "config_change": True,
    },
    {
        "id": "permission_denied",
        "severity": "error",
        "matcher": _m_permission_denied,
        "explanation": "The provider returned HTTP 403 (permission denied) without an admin-consent hint — the granted scopes do not cover this operation.",
        "remediation": "Review the granted API scopes/roles for the connected account and grant the missing permission, then re-verify.",
        "next_commands": [
            "python shared/scripts/chief_of_staff.py readiness --summary",
        ],
        "retry_safe": False,
        "config_change": True,
    },
    {
        "id": "ambiguous_write",
        "severity": "error",
        "matcher": _m_ambiguous_write,
        "explanation": "A write may or may not have been applied (the provider connection dropped mid-write), so its outcome is unknown.",
        "remediation": "Do NOT blindly retry. Verify in the external system whether the change landed; only retry if it did not.",
        "next_commands": [
            "python shared/scripts/chief_of_staff.py logs show --run-id <run-id>",
        ],
        "retry_safe": False,
        "config_change": False,
    },
    {
        "id": "audit_write_failed",
        "severity": "error",
        "matcher": _m_audit_write_failed,
        "explanation": "The audit trail could not be written — a change may have occurred without a durable audit record.",
        "remediation": "Check the audit directory permissions/disk space and confirm the audit store is writable before further writes.",
        "next_commands": [
            "python shared/scripts/chief_of_staff.py doctor --summary",
        ],
        "retry_safe": False,
        "config_change": False,
    },
    {
        "id": "provider_unavailable",
        "severity": "warning",
        "matcher": _m_provider_unavailable,
        "explanation": "The provider returned HTTP 503/504 on a read/idempotent path — a transient server-side outage.",
        "remediation": "This is transient. Wait and re-run; the read operation is idempotent and safe to retry.",
        "next_commands": [
            "python shared/scripts/chief_of_staff.py readiness --summary",
        ],
        "retry_safe": True,
        "config_change": False,
    },
    {
        "id": "network_timeout",
        "severity": "warning",
        "matcher": _m_network_timeout,
        "explanation": "A network timeout or connection error interrupted a provider request.",
        "remediation": "Check connectivity and re-run; the request timed out rather than being rejected.",
        "next_commands": [
            "python shared/scripts/chief_of_staff.py readiness --summary",
        ],
        "retry_safe": True,
        "config_change": False,
    },
    {
        "id": "throttled",
        "severity": "warning",
        "matcher": _m_throttled,
        "explanation": "The provider throttled the run (HTTP 429) and the client backed off/retried per the Retry-After hint.",
        "remediation": "This is transient rate-limiting. Re-run later, or reduce request volume/concurrency.",
        "next_commands": [
            "python shared/scripts/chief_of_staff.py logs show --run-id <run-id> --level warning",
        ],
        "retry_safe": True,
        "config_change": False,
    },
    {
        "id": "retry_deferred",
        "severity": "warning",
        "matcher": _m_retry_deferred,
        "explanation": "A retry was deferred because the provider's Retry-After exceeded the in-run budget; the operation was left for a later run.",
        "remediation": "Re-run the operation once the Retry-After window has elapsed.",
        "next_commands": [
            "python shared/scripts/chief_of_staff.py logs show --run-id <run-id> --level warning",
        ],
        "retry_safe": True,
        "config_change": False,
    },
    {
        "id": "file_lock_timeout",
        "severity": "warning",
        "matcher": _m_file_lock_timeout,
        "explanation": "A state store could not be written because a file lock could not be acquired in time (another process held it).",
        "remediation": "Ensure no other run is in progress, then re-run; if it persists, check for a stale lock file.",
        "next_commands": [
            "python shared/scripts/chief_of_staff.py doctor --summary",
        ],
        "retry_safe": True,
        "config_change": False,
    },
    {
        "id": "guardrail_blocked",
        "severity": "warning",
        "matcher": _m_guardrail_blocked,
        "explanation": "A safety guardrail blocked an action (e.g. a destructive or auto-approve gate). This is the safety system working as intended.",
        "remediation": (
            "If the action was intended, set the corresponding gate env var "
            "(CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1 for destructive actions, "
            "CHIEF_OF_STAFF_AUTO_APPROVE=1 for auto-approval) for that invocation and re-run."
        ),
        "next_commands": [
            "python shared/scripts/chief_of_staff.py review --summary",
        ],
        "retry_safe": True,
        "config_change": False,
    },
    {
        "id": "pagination_truncated",
        "severity": "warning",
        "matcher": _m_pagination_truncated,
        "explanation": "A provider listing hit the pagination cap, so results are incomplete (only the first N pages were followed).",
        "remediation": "Results are partial. Narrow the query (folder/filter) or raise the pagination cap if you need the full set.",
        "next_commands": [
            "python shared/scripts/chief_of_staff.py logs show --run-id <run-id>",
        ],
        "retry_safe": True,
        "config_change": False,
    },
]

# Fast lookup for tests / callers.
CLASSIFICATION_BY_ID = {c["id"]: c for c in CLASSIFICATIONS}
_ORDER = {c["id"]: i for i, c in enumerate(CLASSIFICATIONS)}


# ─── Status derivation ───────────────────────────────────────────────────────


def _derive_status(summary: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> str:
    outcome = str(summary.get("outcome", "")).lower()
    if outcome in {"success"}:
        return "ok"
    if outcome in {"degraded"}:
        return "degraded"
    if outcome in {"failed"}:
        return "failed"
    # No summary — infer from events.
    if _events_named(events, "run_failed"):
        return "failed"
    counts = summary.get("counts") if isinstance(summary.get("counts"), Mapping) else {}
    if int(counts.get("error", 0) or 0) or _failed_events(events):
        return "failed"
    if int(counts.get("warning", 0) or 0) or any(e.get("level") == "warning" for e in events):
        return "degraded"
    return "ok"


# ─── Public API ──────────────────────────────────────────────────────────────


def analyse_run(run_dir: Path) -> dict[str, Any]:
    """Diagnose a single run directory. Never raises; returns a stable dict."""
    run_dir = Path(run_dir)
    events = _read_events(run_dir)
    summary = _read_summary(run_dir)

    run_id = summary.get("run_id") or run_dir.name
    command = summary.get("command") or _first_command(events)
    status = _derive_status(summary, events)

    findings: list[dict[str, Any]] = []
    for entry in CLASSIFICATIONS:
        matcher: Callable[..., list[str]] = entry["matcher"]
        try:
            evidence = matcher(events, summary)
        except Exception:
            evidence = []
        if not evidence:
            continue
        findings.append(
            {
                "id": entry["id"],
                "severity": entry["severity"],
                "explanation": entry["explanation"],
                "evidence": [str(x) for x in evidence][:8],
                "remediation": entry["remediation"],
                "next_commands": _fill_commands(entry["next_commands"], str(run_id)),
                "retry_safe": bool(entry["retry_safe"]),
                "config_change": bool(entry.get("config_change", False)),
            }
        )

    findings.sort(key=lambda f: (_sev_rank(f["severity"]), _ORDER.get(f["id"], 999)))
    primary = findings[0] if findings else None

    return {
        "run_id": str(run_id),
        "command": str(command) if command else None,
        "status": status,
        "findings": findings,
        "primary": primary,
    }


def _first_command(events: Sequence[Mapping[str, Any]]) -> str | None:
    for e in events:
        cmd = e.get("command")
        if cmd:
            return str(cmd)
    return None


def _fill_commands(commands: Sequence[str], run_id: str) -> list[str]:
    return [c.replace("<run-id>", run_id) for c in commands]


# ─── Rendering ───────────────────────────────────────────────────────────────


def _render_human(result: Mapping[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"Run: {result.get('run_id', '?')}")
    lines.append(f"Command: {result.get('command') or '(unknown)'}")
    lines.append(f"Status: {result.get('status', 'ok')}")
    lines.append("")

    findings = result.get("findings") or []
    primary = result.get("primary")
    if not findings or primary is None:
        outcome = result.get("status", "ok")
        lines.append("Primary finding: none")
        lines.append(
            f"No problems detected — the run finished with outcome '{outcome}'. "
            "Clean bill of health."
        )
        return "\n".join(lines)

    lines.append(f"Primary finding: {primary['id']} ({primary['severity']})")
    lines.append("")
    lines.append("Evidence:")
    for ev in primary.get("evidence", []) or ["(no discrete evidence captured)"]:
        lines.append(f"  - {ev}")
    lines.append("")
    lines.append(f"Likely cause: {primary['explanation']}")
    lines.append("")
    lines.append("Recommended action:")
    lines.append(f"  {primary['remediation']}")
    for cmd in primary.get("next_commands", []):
        lines.append(f"    $ {cmd}")
    lines.append(f"  Retry safe: {'yes' if primary.get('retry_safe') else 'no'}")
    if not primary.get("config_change", False):
        lines.append("")
        lines.append("No configuration change is currently indicated.")

    others = [f for f in findings if f is not primary]
    if others:
        lines.append("")
        lines.append("Additional findings:")
        for f in others:
            lines.append(f"  - {f['id']} ({f['severity']}): {f['explanation']}")

    return "\n".join(lines)


def _render_markdown(result: Mapping[str, Any]) -> str:
    lines: list[str] = ["# Run diagnosis", ""]
    lines.append(f"- **Run:** {result.get('run_id', '?')}")
    lines.append(f"- **Command:** {result.get('command') or '(unknown)'}")
    lines.append(f"- **Status:** {result.get('status', 'ok')}")
    lines.append("")

    findings = result.get("findings") or []
    primary = result.get("primary")
    if not findings or primary is None:
        lines.append("## Primary finding")
        lines.append("")
        lines.append(
            f"None — the run finished with outcome `{result.get('status', 'ok')}`. Clean bill of health."
        )
        return "\n".join(lines)

    lines.append(f"## Primary finding: {primary['id']} ({primary['severity']})")
    lines.append("")
    lines.append("**Evidence:**")
    lines.append("")
    for ev in primary.get("evidence", []) or ["(no discrete evidence captured)"]:
        lines.append(f"- {ev}")
    lines.append("")
    lines.append(f"**Likely cause:** {primary['explanation']}")
    lines.append("")
    lines.append("**Recommended action:**")
    lines.append("")
    lines.append(primary["remediation"])
    lines.append("")
    for cmd in primary.get("next_commands", []):
        lines.append(f"```\n{cmd}\n```")
    lines.append("")
    lines.append(f"_Retry safe: {'yes' if primary.get('retry_safe') else 'no'}_")
    if not primary.get("config_change", False):
        lines.append("")
        lines.append("No configuration change is currently indicated.")

    others = [f for f in findings if f is not primary]
    if others:
        lines.append("")
        lines.append("## Additional findings")
        lines.append("")
        for f in others:
            lines.append(f"- **{f['id']}** ({f['severity']}): {f['explanation']}")

    return "\n".join(lines)


def format_diagnosis(result: Mapping[str, Any], fmt: str = "human") -> str:
    """Render an ``analyse_run`` result as ``human``, ``json``, or ``markdown``."""
    fmt = (fmt or "human").lower()
    if fmt == "json":
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)
    if fmt == "markdown":
        return _render_markdown(result)
    return _render_human(result)


__all__ = [
    "CLASSIFICATIONS",
    "CLASSIFICATION_BY_ID",
    "analyse_run",
    "format_diagnosis",
]
