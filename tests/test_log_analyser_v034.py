#!/usr/bin/env python3
"""Tests for v0.3.4 — deterministic rule-based run diagnosis (log_analyser).

One focused fixture per classification asserts the id, severity, retry_safe
flag, and that concrete remediation commands are attached. Also covers
primary-finding ordering (error beats warning), a clean run (no findings),
and the human-format section layout.
"""
import json
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

import log_analyser as la  # noqa: E402


# ─── Helpers ─────────────────────────────────────────────────


def _make_run(tmp_path, events, summary=None, run_id="20260101T120000Z-abc123"):
    run_dir = tmp_path / ".runs" / run_id
    run_dir.mkdir(parents=True)
    run_dir.joinpath("events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events), encoding="utf-8"
    )
    if summary is None:
        summary = {"run_id": run_id, "command": "chief_of_staff daily", "outcome": "failed"}
    run_dir.joinpath("summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return run_dir


# id -> (events, summary_override, expected_severity, expected_retry_safe)
CASES = {
    "auth_expired": (
        [{"event": "provider_request_failed", "status_code": 401, "error_class": "auth",
          "message": "access token expired; please refresh token"}],
        None, "error", True,
    ),
    "invalid_credentials": (
        [{"event": "provider_request_failed", "status_code": 401, "error_class": "auth",
          "message": "invalid_client: client secret has expired"}],
        None, "error", False,
    ),
    "admin_consent_missing": (
        [{"event": "provider_request_failed", "status_code": 403, "endpoint_category": "mail",
          "message": "admin consent required for Mail.Read"}],
        None, "error", False,
    ),
    "permission_denied": (
        [{"event": "provider_request_failed", "status_code": 403,
          "error_class": "permission_denied", "message": "Access is denied"}],
        None, "error", False,
    ),
    "mailbox_not_found": (
        [{"event": "provider_request_failed", "status_code": 404, "endpoint_category": "mail",
          "message": "ErrorInvalidUser /users lookup failed for user_principal"}],
        None, "error", False,
    ),
    "onedrive_not_provisioned": (
        [{"event": "provider_request_failed", "status_code": 403, "endpoint_category": "files",
          "message": "OneDrive is not provisioned (MySiteNotFound)"}],
        None, "error", False,
    ),
    "throttled": (
        [{"event": "provider_retry", "status_code": 429, "attempt": 1, "wait_s": 2.0,
          "reason": "throttled"}],
        None, "warning", True,
    ),
    "retry_deferred": (
        [{"event": "retry_deferred", "retry_after_s": 120}],
        None, "warning", True,
    ),
    "ambiguous_write": (
        [{"event": "ambiguous_write", "status_code": 0, "method": "POST"}],
        None, "error", False,
    ),
    "network_timeout": (
        [{"event": "provider_request_failed", "error_class": "network",
          "message": "ReadTimeout: connection timed out"}],
        None, "warning", True,
    ),
    "provider_unavailable": (
        [{"event": "provider_request_failed", "status_code": 503, "method": "GET",
          "message": "service unavailable"}],
        None, "warning", True,
    ),
    "invalid_configuration": (
        [{"event": "run_failed", "outcome": "failed"}],
        {"run_id": "20260101T120000Z-abc123", "command": "chief_of_staff daily",
         "outcome": "failed", "first_error": "ConfigError: could not load config company.yaml"},
        "error", False,
    ),
    "schema_validation_failed": (
        [{"event": "run_failed", "outcome": "failed"}],
        {"run_id": "20260101T120000Z-abc123", "command": "daily_briefing run",
         "outcome": "failed", "first_error": "SchemaError: payload does not match schema"},
        "error", False,
    ),
    "corrupt_yaml": (
        [{"event": "run_failed", "outcome": "failed"}],
        {"run_id": "20260101T120000Z-abc123", "command": "chief_of_staff daily",
         "outcome": "failed",
         "first_error": "yaml.scanner.ScannerError: mapping values are not allowed here"},
        "error", False,
    ),
    "file_lock_timeout": (
        [{"event": "run_failed", "outcome": "failed"}],
        {"run_id": "20260101T120000Z-abc123", "command": "chief_of_staff daily",
         "outcome": "failed",
         "first_error": "LockTimeout: could not acquire lock on .pending_actions.json"},
        "warning", True,
    ),
    "guardrail_blocked": (
        [{"event": "guardrail_blocked", "action": "mail.trash", "reason": "destructive action blocked"}],
        None, "warning", True,
    ),
    "pagination_truncated": (
        [{"event": "pagination_truncated", "cap": 100, "pages_followed": 5}],
        None, "warning", True,
    ),
    "audit_write_failed": (
        [{"event": "audit_write_failed", "reason": "disk full"}],
        None, "error", False,
    ),
}


def test_all_classifications_covered():
    """Every classification in the table has a focused fixture."""
    table_ids = {c["id"] for c in la.CLASSIFICATIONS}
    assert table_ids == set(CASES), table_ids.symmetric_difference(set(CASES))
    assert len(la.CLASSIFICATIONS) >= 18


@pytest.mark.parametrize("expected_id", list(CASES))
def test_classification_fixture(tmp_path, expected_id):
    events, summary, severity, retry_safe = CASES[expected_id]
    run_dir = _make_run(tmp_path, events, summary)
    result = la.analyse_run(run_dir)

    ids = [f["id"] for f in result["findings"]]
    assert result["primary"] is not None, f"{expected_id} produced no finding"
    assert result["primary"]["id"] == expected_id, f"got {ids}"

    finding = result["primary"]
    assert finding["severity"] == severity
    assert finding["retry_safe"] is retry_safe
    # Remediation must be present and concrete commands attached.
    assert finding["remediation"].strip()
    assert isinstance(finding["next_commands"], list) and finding["next_commands"]


def test_ambiguous_write_remediation_is_verify_first(tmp_path):
    run_dir = _make_run(tmp_path, [{"event": "ambiguous_write", "status_code": 0, "method": "POST"}])
    result = la.analyse_run(run_dir)
    primary = result["primary"]
    assert primary["id"] == "ambiguous_write"
    assert primary["retry_safe"] is False
    assert "verify" in primary["remediation"].lower()


def test_guardrail_remediation_names_env_var(tmp_path):
    run_dir = _make_run(
        tmp_path,
        [{"event": "guardrail_blocked", "action": "mail.trash", "reason": "destructive"}],
    )
    result = la.analyse_run(run_dir)
    assert "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE" in result["primary"]["remediation"]


def test_pagination_explains_cap(tmp_path):
    run_dir = _make_run(
        tmp_path, [{"event": "pagination_truncated", "cap": 250, "pages_followed": 10}]
    )
    result = la.analyse_run(run_dir)
    primary = result["primary"]
    assert primary["id"] == "pagination_truncated"
    assert primary["severity"] == "warning"
    assert any("250" in ev for ev in primary["evidence"])


# ─── Ordering ────────────────────────────────────────────────


def test_primary_error_beats_warning(tmp_path):
    """When both an error and a warning fire, the error is primary."""
    events = [
        {"event": "provider_retry", "status_code": 429, "wait_s": 1.0, "reason": "throttled"},
        {"event": "provider_request_failed", "status_code": 403,
         "error_class": "permission_denied", "message": "denied"},
    ]
    result = la.analyse_run(_make_run(tmp_path, events))
    ids = [f["id"] for f in result["findings"]]
    assert "permission_denied" in ids and "throttled" in ids
    assert result["primary"]["id"] == "permission_denied"
    assert result["primary"]["severity"] == "error"


def test_error_table_order_breaks_ties(tmp_path):
    """Two errors: the earlier table entry (invalid_configuration) wins."""
    events = [{"event": "provider_request_failed", "status_code": 403,
               "error_class": "permission_denied", "message": "denied"}]
    summary = {"run_id": "20260101T120000Z-abc123", "command": "chief_of_staff daily",
               "outcome": "failed", "first_error": "ConfigError: company.yaml missing"}
    result = la.analyse_run(_make_run(tmp_path, events, summary))
    ids = [f["id"] for f in result["findings"]]
    assert {"invalid_configuration", "permission_denied"} <= set(ids)
    assert result["primary"]["id"] == "invalid_configuration"


# ─── Clean run ───────────────────────────────────────────────


def test_clean_run_no_findings(tmp_path):
    events = [
        {"event": "run_started", "level": "info", "command": "chief_of_staff daily"},
        {"event": "provider_request_completed", "level": "info", "status_code": 200,
         "provider": "google_api", "operation": "mail_read"},
        {"event": "run_completed", "level": "info", "outcome": "success"},
    ]
    summary = {"run_id": "20260101T120000Z-abc123", "command": "chief_of_staff daily",
               "outcome": "success", "counts": {"error": 0, "warning": 0}}
    result = la.analyse_run(_make_run(tmp_path, events, summary))
    assert result["findings"] == []
    assert result["primary"] is None
    assert result["status"] == "ok"

    human = la.format_diagnosis(result, "human")
    assert "Clean bill of health" in human
    assert "outcome 'ok'" in human


# ─── Human format ────────────────────────────────────────────


def test_human_format_sections(tmp_path):
    events = [{"event": "provider_request_failed", "status_code": 401, "error_class": "auth",
               "message": "token expired, refresh required"}]
    result = la.analyse_run(_make_run(tmp_path, events))
    human = la.format_diagnosis(result, "human")
    assert human.startswith("Run: ")
    assert "Command: chief_of_staff daily" in human
    assert "Status: failed" in human
    assert "Primary finding: auth_expired" in human
    assert "Evidence:" in human
    assert "Likely cause:" in human
    assert "Recommended action:" in human
    # Concrete command surfaced under recommended action.
    assert "connect_workspace.py" in human


def test_json_and_markdown_formats(tmp_path):
    events = [{"event": "provider_request_failed", "status_code": 403,
               "error_class": "permission_denied", "message": "denied"}]
    result = la.analyse_run(_make_run(tmp_path, events))

    parsed = json.loads(la.format_diagnosis(result, "json"))
    assert parsed["primary"]["id"] == "permission_denied"
    assert parsed["run_id"] == "20260101T120000Z-abc123"

    md = la.format_diagnosis(result, "markdown")
    assert "# Run diagnosis" in md
    assert "Primary finding: permission_denied" in md


def test_next_commands_substitute_run_id(tmp_path):
    """<run-id> placeholders are filled with the actual run id."""
    events = [{"event": "ambiguous_write", "status_code": 0, "method": "POST"}]
    result = la.analyse_run(_make_run(tmp_path, events, run_id="20260101T120000Z-abc123"))
    joined = " ".join(result["primary"]["next_commands"])
    assert "<run-id>" not in joined
    assert "20260101T120000Z-abc123" in joined
