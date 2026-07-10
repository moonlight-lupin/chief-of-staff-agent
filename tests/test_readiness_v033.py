#!/usr/bin/env python3
"""Tests for v0.3.3 — generated readiness (go/no-go) report.

The workspace verification module (shared/scripts/workspace_verify.py) is
authored concurrently; these tests inject a fake via the lazy import hook
(chief_of_staff._get_workspace_verify) so readiness can be exercised in
isolation regardless of whether the real module is present.
"""
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
SKILLS_SCRIPTS = PLUGIN_ROOT / "skills"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))
for skill in ("daily-briefing", "note-taker", "pipeline-manager", "bookkeeper", "document-preparer"):
    d = SKILLS_SCRIPTS / skill / "scripts"
    if d.exists() and str(d) not in sys.path:
        sys.path.insert(0, str(d))


# ─── Fixtures ────────────────────────────────────────────────

@pytest.fixture
def temp_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".audit").mkdir()
    (project / ".runs").mkdir()
    (project / ".knowledge").mkdir()
    config = {
        "company": {"name": "Test Co", "jurisdiction": "SG", "currency": "SGD",
                    "incorporation_date": "2026-01-01", "financial_year_end": "31 Dec",
                    "business_type": "professional_services"},
        "google": {"delegate_email": "test@test.com", "account_alias": "test",
                   "domain": "test.com", "service_account_path": "/tmp/sa.json"},
        "paths": {"project_root": str(project), "wiki_path": str(project / "wiki"),
                  "templates": str(PLUGIN_ROOT / "shared" / "templates")},
        "delivery": {"channel": "telegram", "briefing_time": "08:00",
                     "weekly_review_day": "friday", "weekly_review_time": "17:00",
                     "timezone": "Asia/Singapore"},
        "integrations": {"workspace": {"provider": "google_api"}},
        "sales_stages": ["Lead", "Qualified", "Proposal Sent", "NDA Signed",
                         "Contract Signed", "Invoiced", "Paid", "Lost"],
        "stale_threshold_days": 14,
    }
    import yaml
    config_path = project / "company.yaml"
    config_path.write_text(yaml.safe_dump(config))
    return config, project, config_path


class _FakeVerify:
    """Stand-in for shared/scripts/workspace_verify."""

    def __init__(self, checks, read_ready, write_ready, provider="google_api"):
        self._checks = checks
        self._read_ready = read_ready
        self._write_ready = write_ready
        self._provider = provider
        self.calls = []

    def run_verification(self, config, include_writes=False):
        self.calls.append({"config": config, "include_writes": include_writes})
        return {
            "provider": self._provider,
            "checks": dict(self._checks),
            "read_ready": self._read_ready,
            "write_ready": self._write_ready,
        }


def _all(status):
    names = ["auth", "mail_read", "mail_folder_scoped", "mail_tags_list",
             "calendar_read", "files_read", "mail_draft", "mail_tag_write",
             "files_write", "mail_send", "calendar_write"]
    return {n: {"status": status, "detail": f"{n} {status}"} for n in names}


def _inject(monkeypatch, fake):
    import chief_of_staff
    monkeypatch.setattr(chief_of_staff, "_get_workspace_verify", lambda: fake)
    return chief_of_staff


def _run(cos, config_path, *flags):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cos.main(["--config", str(config_path), "readiness", *flags])
    return rc, buf.getvalue()


# ─── All-pass path ───────────────────────────────────────────

class TestAllPass:
    def test_all_pass_writes_partial_yields_partial(self, temp_project, monkeypatch):
        """All reads pass, writes not tested → read-only YES, execution PARTIAL, exit 0."""
        _, _, config_path = temp_project
        fake = _FakeVerify(_all("pass"), read_ready=True, write_ready="partial")
        cos = _inject(monkeypatch, fake)
        rc, out = _run(cos, config_path, "--summary")
        assert rc == 0
        assert "Ready for daily read-only operation: YES" in out
        assert "Ready for approved execution: PARTIAL" in out
        # verification was requested read-only
        assert fake.calls and fake.calls[0]["include_writes"] is False

    def test_writes_yes_yields_execution_yes(self, temp_project, monkeypatch):
        """All reads pass and writes pass → approved execution YES."""
        _, _, config_path = temp_project
        fake = _FakeVerify(_all("pass"), read_ready=True, write_ready="yes")
        cos = _inject(monkeypatch, fake)
        rc, out = _run(cos, config_path, "--summary")
        assert rc == 0
        assert "Ready for daily read-only operation: YES" in out
        assert "Ready for approved execution: YES" in out

    def test_writes_no_yields_execution_no(self, temp_project, monkeypatch):
        """All reads pass but writes fail → approved execution NO (read-only still YES)."""
        _, _, config_path = temp_project
        fake = _FakeVerify(_all("pass"), read_ready=True, write_ready="no")
        cos = _inject(monkeypatch, fake)
        rc, out = _run(cos, config_path, "--summary")
        assert rc == 0  # read-only still YES
        assert "Ready for daily read-only operation: YES" in out
        assert "Ready for approved execution: NO" in out


# ─── Failure paths ───────────────────────────────────────────

class TestFailures:
    def test_files_read_fail_blocks_read_only(self, temp_project, monkeypatch):
        """files_read is in rows 1–5, so its failure → read-only NO, exit 1."""
        _, _, config_path = temp_project
        checks = _all("pass")
        checks["files_read"] = {"status": "fail", "detail": "drive list denied"}
        fake = _FakeVerify(checks, read_ready=False, write_ready="partial")
        cos = _inject(monkeypatch, fake)
        rc, out = _run(cos, config_path, "--summary")
        assert rc == 1
        assert "Ready for daily read-only operation: NO" in out
        # Files read row shows FAIL
        assert "Files read" in out
        assert "FAIL" in out
        # execution cannot be ready if read-only fails
        assert "Ready for approved execution: NO" in out

    def test_folder_scoped_fail_blocks_read_only(self, temp_project, monkeypatch):
        """mail_folder_scoped is REQUIRED → its failure makes Mail read FAIL and
        blocks read-only (NO, exit 1)."""
        _, _, config_path = temp_project
        checks = _all("pass")
        checks["mail_folder_scoped"] = {"status": "fail", "detail": "no scoped folder"}
        fake = _FakeVerify(checks, read_ready=False, write_ready="partial")
        cos = _inject(monkeypatch, fake)
        rc, out = _run(cos, config_path, "--summary")
        assert rc == 1
        mail_line = next(l for l in out.splitlines() if "Mail read" in l)
        assert "FAIL" in mail_line
        assert "mail_folder_scoped" in mail_line
        assert "Ready for daily read-only operation: NO" in out
        assert "Ready for approved execution: NO" in out

    def test_tags_only_fail_warns_still_ready(self, temp_project, monkeypatch):
        """mail_tags_list is OPTIONAL → its failure only warns Mail read (with the
        degraded wording) and read-only stays YES (exit 0)."""
        _, _, config_path = temp_project
        checks = _all("pass")
        checks["mail_tags_list"] = {"status": "fail", "detail": "categories blocked"}
        fake = _FakeVerify(checks, read_ready=True, write_ready="partial")
        cos = _inject(monkeypatch, fake)
        rc, out = _run(cos, config_path, "--summary")
        assert rc == 0
        mail_line = next(l for l in out.splitlines() if "Mail read" in l)
        assert "WARN" in mail_line
        assert "email organisation features will be degraded" in mail_line
        assert "Ready for daily read-only operation: YES" in out


# ─── Missing config ──────────────────────────────────────────

class TestMissingConfig:
    def test_missing_config_degrades(self, tmp_path, monkeypatch):
        """Missing config → row 1 FAIL, workspace NOT TESTED, exit 1, no traceback."""
        # Fake still present, but config is None so verification is skipped.
        fake = _FakeVerify(_all("pass"), read_ready=True, write_ready="yes")
        import chief_of_staff
        monkeypatch.setattr(chief_of_staff, "_get_workspace_verify", lambda: fake)
        missing = tmp_path / "does_not_exist.yaml"
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = chief_of_staff.main(["--config", str(missing), "readiness", "--summary"])
        out = buf.getvalue()
        assert rc == 1
        assert "Core configuration" in out
        # Row 1 must be FAIL
        core_line = next(l for l in out.splitlines() if "Core configuration" in l)
        assert "FAIL" in core_line
        # Workspace rows NOT TESTED (verification skipped because config not loaded)
        auth_line = next(l for l in out.splitlines() if "Workspace authentication" in l)
        assert "NOT TESTED" in auth_line
        assert "Ready for daily read-only operation: NO" in out
        # Fake must not have been called (config was None)
        assert fake.calls == []


# ─── Module absent ───────────────────────────────────────────

class TestModuleAbsent:
    def test_missing_module_marks_not_tested(self, temp_project, monkeypatch):
        """workspace_verify absent → workspace rows NOT TESTED, no crash."""
        _, _, config_path = temp_project
        cos = _inject(monkeypatch, None)  # simulate ImportError result
        rc, out = _run(cos, config_path, "--summary")
        # No traceback; graceful.
        assert rc == 1  # auth NOT TESTED → read-only NO
        for label in ("Workspace authentication", "Mail read", "Calendar read", "Files read"):
            line = next(l for l in out.splitlines() if label in l)
            assert "NOT TESTED" in line


# ─── Output formats ──────────────────────────────────────────

class TestOutputFormats:
    def test_json_schema(self, temp_project, monkeypatch):
        """--json exposes rows, verdicts, and the raw verification report."""
        _, _, config_path = temp_project
        fake = _FakeVerify(_all("pass"), read_ready=True, write_ready="partial")
        cos = _inject(monkeypatch, fake)
        rc, out = _run(cos, config_path, "--json")
        assert rc == 0
        parsed = json.loads(out)
        assert parsed["mode"] == "readiness"
        assert parsed["safety"]["read_only"] is True
        assert isinstance(parsed["rows"], list) and len(parsed["rows"]) == 8
        keys = {r["key"] for r in parsed["rows"]}
        assert {"core_config", "workspace_auth", "mail_read", "calendar_read",
                "files_read", "review_queue", "daily_loop", "optional_writes"} <= keys
        for row in parsed["rows"]:
            assert row["status"] in {"PASS", "FAIL", "WARN", "NOT TESTED"}
            assert "detail" in row
        assert parsed["verdicts"]["read_only_ready"] == "YES"
        assert parsed["verdicts"]["approved_execution_ready"] == "PARTIAL"
        # Raw verification report is echoed
        assert parsed["verification"]["provider"] == "google_api"
        assert "checks" in parsed["verification"]

    def test_markdown_contains_verdict_lines(self, temp_project, monkeypatch):
        """--markdown includes the two verdict lines."""
        _, _, config_path = temp_project
        fake = _FakeVerify(_all("pass"), read_ready=True, write_ready="yes")
        cos = _inject(monkeypatch, fake)
        rc, out = _run(cos, config_path, "--markdown")
        assert rc == 0
        assert "# Chief of Staff Readiness" in out
        assert "Ready for daily read-only operation:" in out
        assert "Ready for approved execution:" in out
        assert "YES" in out

    def test_summary_layout_labels(self, temp_project, monkeypatch):
        """--summary renders the expected row labels and the writes pointer."""
        _, _, config_path = temp_project
        fake = _FakeVerify(_all("pass"), read_ready=True, write_ready="partial",
                           provider="m365")
        cos = _inject(monkeypatch, fake)
        rc, out = _run(cos, config_path, "--summary")
        assert rc == 0
        assert out.splitlines()[0] == "Chief of Staff Readiness"
        for label in ("Core configuration", "Workspace authentication", "Mail read",
                      "Calendar read", "Files read", "Review queue", "Daily loop",
                      "Optional writes"):
            assert label in out
        # Optional-writes pointer names the provider and the verify-writes flag.
        writes_line = next(l for l in out.splitlines() if "Optional writes" in l)
        assert "NOT TESTED" in writes_line
        assert "--verify-writes" in writes_line
        assert "--provider m365" in writes_line
