#!/usr/bin/env python3
"""Tests for v0.3.6 — the readiness→diagnose loop must tell the truth, plus the
zero-credential ``demo`` first-value path.

Audit blocker (v0.3.6): with bad workspace credentials, ``readiness`` correctly
showed the error and printed a "Diagnose: … logs diagnose --run-id …" pointer,
but running that command printed "No problems detected … Clean bill of health"
for a FAILED run. Root cause: readiness/verify printed failures to stdout but
never emitted structured error events, so events.jsonl held only run_started /
run_failed and the analyser had nothing to match.

These tests exercise the real wiring end-to-end:
  * workspace_verify emits ``verify_check_failed`` error events on check failure;
  * readiness emits ``readiness_row_failed`` for every FAIL row and the diagnose
    pointer fires for BOTH the per-check (m365-style) and client-construction
    (google_api-style) credential-failure paths;
  * log_analyser classifies the credential failure (or at minimum surfaces the
    first error) and NEVER renders "Clean bill" / "No problems detected" for a
    failed/degraded run;
  * ``demo`` renders the daily briefing from bundled sample data with no config
    or credentials, banners the output, and touches nothing under examples/.
"""
import hashlib
import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr
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

import log_analyser as la  # noqa: E402


# An msal-style credential rejection carrying the exact phrasings the analyser
# classifies as invalid_credentials.
_MSAL_ERROR = (
    "ClientAuthenticationError: AADSTS90002: Tenant 'bogus-tenant' not found. "
    "Check your tenant name or GUID. credentials rejected"
)


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
        "integrations": {"workspace": {"provider": "m365"}},
        "sales_stages": ["Lead", "Qualified"],
        "stale_threshold_days": 14,
    }
    import yaml
    config_path = project / "company.yaml"
    config_path.write_text(yaml.safe_dump(config))
    return config, project, config_path


class _MsalStubClient:
    """Client whose every call fails with an msal-style credential error (the
    per-check / m365 path: construction succeeds, health_check() raises)."""

    provider_name = "m365"

    def health_check(self):
        raise RuntimeError(_MSAL_ERROR)

    def mail_search(self, *a, **k):
        raise RuntimeError(_MSAL_ERROR)

    def mail_list_tags(self, *a, **k):
        raise RuntimeError(_MSAL_ERROR)

    def calendar_list(self, *a, **k):
        raise RuntimeError(_MSAL_ERROR)

    def files_search(self, *a, **k):
        raise RuntimeError(_MSAL_ERROR)

    def supports(self, action):
        return False


def _run_cos(config_path, *args):
    import chief_of_staff
    buf = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        rc = chief_of_staff.main(["--config", str(config_path), *args])
    return rc, buf.getvalue()


def _only_run_dir(project):
    runs = [p for p in (project / ".runs").iterdir() if p.is_dir()]
    assert len(runs) == 1, f"expected exactly one run dir, got {runs}"
    return runs[0]


def _read_events(run_dir):
    text = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    return [json.loads(l) for l in text.splitlines() if l.strip()]


# ─── Per-check (m365-style) credential failure end-to-end ────────────────────


class TestPerCheckCredentialFailure:
    def test_verify_check_failed_events_emitted(self, temp_project, monkeypatch):
        _, project, config_path = temp_project
        import workspace_verify
        monkeypatch.setattr(workspace_verify, "get_workspace_client",
                            lambda cfg: _MsalStubClient())

        rc, out = _run_cos(config_path, "readiness", "--summary")
        assert rc == 1  # workspace auth failed → not read-ready

        run_dir = _only_run_dir(project)
        events = _read_events(run_dir)
        verify_fails = [e for e in events if e.get("event") == "verify_check_failed"]
        assert verify_fails, "expected verify_check_failed events in events.jsonl"
        assert all(e.get("level") == "error" for e in verify_fails)
        # The auth check failure must carry the msal detail.
        auth_fail = [e for e in verify_fails if e.get("check") == "auth"]
        assert auth_fail and "aadsts90002" in json.dumps(auth_fail).lower()

    def test_summary_first_error_non_null(self, temp_project, monkeypatch):
        _, project, config_path = temp_project
        import workspace_verify
        monkeypatch.setattr(workspace_verify, "get_workspace_client",
                            lambda cfg: _MsalStubClient())
        _run_cos(config_path, "readiness", "--summary")
        run_dir = _only_run_dir(project)
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        assert summary.get("outcome") == "failed"
        assert summary.get("first_error"), "summary.first_error must be non-null for a failed run"
        assert int(summary.get("counts", {}).get("error", 0)) >= 1

    def test_diagnose_classifies_and_never_clean_bill(self, temp_project, monkeypatch):
        _, project, config_path = temp_project
        import workspace_verify
        monkeypatch.setattr(workspace_verify, "get_workspace_client",
                            lambda cfg: _MsalStubClient())
        _run_cos(config_path, "readiness", "--summary")
        run_dir = _only_run_dir(project)
        rid = run_dir.name

        rc, out = _run_cos(config_path, "logs", "diagnose", "--run-id", rid)
        assert rc == 0
        # The lying strings must NEVER appear for a failed run.
        assert "Clean bill of health" not in out
        assert "No problems detected" not in out
        # Classified as invalid_credentials (the real matcher now sees the events).
        assert "invalid_credentials" in out
        assert "Status: failed" in out


# ─── Client-construction (google_api-style) failure: pointer unification ─────


class TestConstructionFailurePointer:
    def test_google_api_path_prints_run_id_and_diagnose_block(self, temp_project, monkeypatch):
        """When the client blows up at construction (google_api path), the
        workspace auth row must land FAIL so the Run ID + Diagnose block prints —
        the audit saw it for m365 but not google_api. Unify the two."""
        cfg, project, config_path = temp_project
        # google_api provider whose client construction raises (bad creds).
        import yaml
        cfg["integrations"]["workspace"]["provider"] = "google_api"
        config_path.write_text(yaml.safe_dump(cfg))

        import workspace_verify

        def _boom(_cfg):
            raise RuntimeError(_MSAL_ERROR)

        monkeypatch.setattr(workspace_verify, "get_workspace_client", _boom)

        rc, out = _run_cos(config_path, "readiness", "--summary")
        assert rc == 1
        assert "FAIL" in out
        assert "Run ID:" in out
        assert "logs diagnose --run-id" in out

        run_dir = _only_run_dir(project)
        events = _read_events(run_dir)
        row_fails = [e for e in events if e.get("event") == "readiness_row_failed"]
        assert any(e.get("row") == "workspace_auth" for e in row_fails)

        rid = run_dir.name
        rc2, diag = _run_cos(config_path, "logs", "diagnose", "--run-id", rid)
        assert rc2 == 0
        assert "Clean bill of health" not in diag
        assert "No problems detected" not in diag
        assert "invalid_credentials" in diag


# ─── Direct format_diagnosis unit test: no clean-bill for failed/degraded ────


def _make_run(tmp_path, events, summary):
    run_dir = tmp_path / ".runs" / summary.get("run_id", "20260101T120000Z-abc123")
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.joinpath("events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events), encoding="utf-8")
    run_dir.joinpath("summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return run_dir


class TestFormatDiagnosisNeverLies:
    def test_failed_run_no_findings_surfaces_first_error(self, tmp_path):
        events = [
            {"event": "run_started", "level": "info"},
            {"event": "novel_explosion", "level": "error",
             "message": "a totally unclassified boom happened"},
            {"event": "run_failed", "level": "info", "outcome": "failed"},
        ]
        summary = {"run_id": "20260101T120000Z-abc123", "outcome": "failed",
                   "counts": {"error": 1, "warning": 0},
                   "first_error": "a totally unclassified boom happened"}
        result = la.analyse_run(_make_run(tmp_path, events, summary))
        assert result["status"] == "failed"
        assert result["findings"] == []

        for fmt in ("human", "markdown"):
            text = la.format_diagnosis(result, fmt)
            assert "Clean bill of health" not in text
            assert "No problems detected" not in text
            assert "a totally unclassified boom happened" in text
            assert "logs show --run-id 20260101T120000Z-abc123 --level error" in text

        # JSON never carries the lying wording either.
        js = la.format_diagnosis(result, "json")
        assert "Clean bill" not in js and "No problems detected" not in js
        assert json.loads(js)["first_error"] == "a totally unclassified boom happened"

    def test_degraded_run_no_findings_wording(self, tmp_path):
        events = [
            {"event": "run_started", "level": "info"},
            {"event": "odd_warning", "level": "warning", "message": "something mildly off"},
            {"event": "run_completed", "level": "info", "outcome": "degraded"},
        ]
        summary = {"run_id": "20260101T120000Z-def456", "outcome": "degraded",
                   "counts": {"error": 0, "warning": 1},
                   "first_error": None, "warnings": ["something mildly off"]}
        result = la.analyse_run(_make_run(tmp_path, events, summary))
        assert result["status"] == "degraded"
        human = la.format_diagnosis(result, "human")
        assert "Clean bill of health" not in human
        assert "No problems detected" not in human
        assert "--level warning" in human

    def test_clean_success_still_says_clean_bill(self, tmp_path):
        events = [
            {"event": "run_started", "level": "info"},
            {"event": "run_completed", "level": "info", "outcome": "success"},
        ]
        summary = {"run_id": "20260101T120000Z-aaa000", "outcome": "success",
                   "counts": {"error": 0, "warning": 0}}
        result = la.analyse_run(_make_run(tmp_path, events, summary))
        human = la.format_diagnosis(result, "human")
        assert "Clean bill of health" in human


# ─── Structured-event matcher unit test ──────────────────────────────────────


def test_invalid_credentials_matcher_fires_on_structured_events():
    events = [
        {"event": "verify_check_failed", "level": "error", "component": "workspace_verify",
         "check": "auth", "provider": "m365", "message": _MSAL_ERROR},
        {"event": "readiness_row_failed", "level": "error", "component": "readiness",
         "row": "workspace_auth", "message": "verification error: " + _MSAL_ERROR},
    ]
    out = la._m_invalid_credentials(events, {})
    assert out, "invalid_credentials must fire on structured verify/readiness events"


# ─── demo subcommand ─────────────────────────────────────────────────────────


def _hash_dir(directory: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(directory.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(directory).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


class TestDemo:
    def test_demo_zero_config_banners_and_leaves_examples_untouched(self, monkeypatch):
        examples_dir = PLUGIN_ROOT / "examples"
        before = _hash_dir(examples_dir)

        # Zero config / zero credentials.
        monkeypatch.delenv("CHIEF_OF_STAFF_CONFIG", raising=False)
        monkeypatch.delenv("CHIEF_OF_STAFF_PROJECT_ROOT", raising=False)

        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            rc = chief_of_staff.main(["demo"])
        out = buf.getvalue()

        assert rc == 0
        # Banner top AND bottom.
        assert out.count("DEMO — sample data") >= 2
        # At least one sample deal artifact and one sample calendar/message artifact
        # (the envelope flowed through the real --input compute path).
        assert "deal-001" in out
        assert "Acme Corp — procurement sync" in out
        assert "Active deals:" in out

        # examples/ must be byte-for-byte unchanged (no writes, no .runs).
        assert _hash_dir(examples_dir) == before
        assert not (examples_dir / ".runs").exists()

    def test_demo_json_format(self, monkeypatch):
        monkeypatch.delenv("CHIEF_OF_STAFF_CONFIG", raising=False)
        monkeypatch.delenv("CHIEF_OF_STAFF_PROJECT_ROOT", raising=False)
        import chief_of_staff
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            rc = chief_of_staff.main(["demo", "--json"])
        out = buf.getvalue()
        assert rc == 0
        assert "DEMO — sample data" in out
        # The JSON briefing body sits between the banners.
        start = out.index("{")
        end = out.rindex("}") + 1
        parsed = json.loads(out[start:end])
        assert "sections" in parsed


# ─── recommended-commands surface registration ───────────────────────────────


def test_demo_registered_in_recommended_commands_for_fresh_operator():
    import chief_of_staff
    # A fresh operator with no config: the demo must be recommended.
    cmds = chief_of_staff.build_recommended_commands(
        system_health={"config_loaded": False},
        review_queue={}, pipeline={}, bookkeeper={}, knowledge={}, state={},
    )
    joined = " ".join(c["command"] for c in cmds)
    assert "chief_of_staff.py demo" in joined
