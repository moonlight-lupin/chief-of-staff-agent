#!/usr/bin/env python3
"""Tests for v0.3.4 — `logs` CLI surface and run lifecycle wiring.

Exercises `logs recent/show/diagnose (--run-id | --latest-failed)/prune/bundle`
against fabricated run directories in a temp project root, asserts the support
bundle contents EXACTLY match the allow-list (and that config_shape carries
types-not-values with no secret keys), that a readiness FAIL prints the diagnose
pointer, and that operational commands create+finish a run while `logs recent`
does not.
"""
import io
import json
import sys
import zipfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
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
        "sales_stages": ["Lead", "Qualified"],
        "stale_threshold_days": 14,
        # A deliberately secret-named leaf that must NEVER leak into a bundle.
        "api_client_secret": "super-secret-value",
    }
    import yaml
    config_path = project / "company.yaml"
    config_path.write_text(yaml.safe_dump(config))
    return config, project, config_path


def _now_id(offset_minutes=0):
    ts = datetime.now(timezone.utc) - timedelta(minutes=offset_minutes)
    return ts.strftime("%Y%m%dT%H%M%SZ") + "-" + format(abs(hash(str(offset_minutes))) % 0xffffff, "06x")


def _fab_run(project, run_id, events, summary=None):
    run_dir = project / ".runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.joinpath("events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events), encoding="utf-8"
    )
    if summary is not None:
        run_dir.joinpath("summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return run_dir


def _run(config_path, *args):
    import chief_of_staff
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = chief_of_staff.main(["--config", str(config_path), *args])
    return rc, buf.getvalue()


# ─── recent ──────────────────────────────────────────────────


class TestRecent:
    def test_recent_lists_runs_newest_first_and_incomplete(self, temp_project):
        _, project, config_path = temp_project
        old_id = "20260101T090000Z-aaaaaa"
        new_id = "20260101T100000Z-bbbbbb"
        _fab_run(project, old_id, [{"event": "run_started"}],
                 {"run_id": old_id, "command": "chief_of_staff daily", "outcome": "success",
                  "counts": {"error": 0, "warning": 1}})
        # No summary → shown as incomplete.
        _fab_run(project, new_id, [{"event": "run_started"}], summary=None)

        rc, out = _run(config_path, "logs", "recent")
        assert rc == 0
        lines = [l for l in out.splitlines() if "20260101" in l]
        assert lines[0].startswith(new_id)  # newest first
        assert "incomplete" in lines[0]
        assert old_id in out

    def test_recent_json(self, temp_project):
        _, project, config_path = temp_project
        rid = "20260101T100000Z-bbbbbb"
        _fab_run(project, rid, [{"event": "run_started"}],
                 {"run_id": rid, "command": "chief_of_staff doctor", "outcome": "degraded",
                  "counts": {"error": 0, "warning": 2}})
        rc, out = _run(config_path, "logs", "recent", "--json")
        assert rc == 0
        parsed = json.loads(out)
        assert parsed["mode"] == "logs.recent"
        entry = next(r for r in parsed["runs"] if r["run_id"] == rid)
        assert entry["outcome"] == "degraded"
        assert entry["warnings"] == 2


# ─── show ────────────────────────────────────────────────────


class TestShow:
    def test_show_prints_events(self, temp_project):
        _, project, config_path = temp_project
        rid = "20260101T100000Z-cccccc"
        _fab_run(project, rid, [
            {"event": "run_started", "level": "info", "command": "chief_of_staff daily"},
            {"event": "provider_request_failed", "level": "error", "status_code": 500,
             "message": "boom"},
        ], {"run_id": rid, "outcome": "failed"})
        rc, out = _run(config_path, "logs", "show", "--run-id", rid)
        assert rc == 0
        assert "provider_request_failed" in out
        assert "run_started" in out

    def test_show_level_filter(self, temp_project):
        _, project, config_path = temp_project
        rid = "20260101T100000Z-dddddd"
        _fab_run(project, rid, [
            {"event": "run_started", "level": "info"},
            {"event": "provider_request_failed", "level": "error", "message": "boom"},
        ], {"run_id": rid, "outcome": "failed"})
        rc, out = _run(config_path, "logs", "show", "--run-id", rid, "--level", "error")
        assert rc == 0
        assert "provider_request_failed" in out
        assert "run_started" not in out

    def test_show_missing_run(self, temp_project):
        _, _, config_path = temp_project
        rc, out = _run(config_path, "logs", "show", "--run-id", "20260101T100000Z-ffffff")
        assert rc == 1


# ─── diagnose ────────────────────────────────────────────────


class TestDiagnose:
    def test_diagnose_by_run_id(self, temp_project):
        _, project, config_path = temp_project
        rid = "20260101T100000Z-eeeeee"
        _fab_run(project, rid, [
            {"event": "provider_request_failed", "status_code": 401, "error_class": "auth",
             "message": "token expired, refresh required"},
        ], {"run_id": rid, "command": "chief_of_staff daily", "outcome": "failed"})
        rc, out = _run(config_path, "logs", "diagnose", "--run-id", rid)
        assert rc == 0
        assert "auth_expired" in out
        assert "Recommended action" in out

    def test_diagnose_json(self, temp_project):
        _, project, config_path = temp_project
        rid = "20260101T100000Z-e1e1e1"
        _fab_run(project, rid, [
            {"event": "ambiguous_write", "status_code": 0, "method": "POST"},
        ], {"run_id": rid, "outcome": "failed"})
        rc, out = _run(config_path, "logs", "diagnose", "--run-id", rid, "--json")
        assert rc == 0
        parsed = json.loads(out)
        assert parsed["primary"]["id"] == "ambiguous_write"
        assert parsed["primary"]["retry_safe"] is False

    def test_diagnose_latest_failed_picks_newest(self, temp_project):
        _, project, config_path = temp_project
        older = "20260101T080000Z-111111"
        newer = "20260101T090000Z-222222"
        ok = "20260101T095000Z-333333"
        _fab_run(project, older, [{"event": "provider_request_failed", "status_code": 429,
                                   "message": "throttled"}],
                 {"run_id": older, "outcome": "degraded"})
        _fab_run(project, newer, [{"event": "provider_request_failed", "status_code": 403,
                                   "error_class": "permission_denied", "message": "denied"}],
                 {"run_id": newer, "outcome": "failed"})
        # Newest overall but a SUCCESS — must be skipped by --latest-failed.
        _fab_run(project, ok, [{"event": "run_completed"}], {"run_id": ok, "outcome": "success"})

        rc, out = _run(config_path, "logs", "diagnose", "--latest-failed")
        assert rc == 0
        assert newer in out
        assert "permission_denied" in out

    def test_diagnose_latest_failed_none(self, temp_project):
        _, project, config_path = temp_project
        _fab_run(project, "20260101T090000Z-444444", [{"event": "run_completed"}],
                 {"run_id": "20260101T090000Z-444444", "outcome": "success"})
        rc, out = _run(config_path, "logs", "diagnose", "--latest-failed")
        assert rc == 0
        assert "No failed or degraded run" in out


# ─── prune ───────────────────────────────────────────────────


class TestPrune:
    def test_prune_removes_old_runs(self, temp_project):
        config, project, config_path = temp_project
        # An ancient run well beyond the 30-day default retention.
        old_id = "20200101T090000Z-999999"
        keep_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-aaaaaa"
        _fab_run(project, old_id, [{"event": "run_started"}], {"run_id": old_id, "outcome": "success"})
        _fab_run(project, keep_id, [{"event": "run_started"}], {"run_id": keep_id, "outcome": "success"})

        rc, out = _run(config_path, "logs", "prune", "--json")
        assert rc == 0
        parsed = json.loads(out)
        assert old_id in parsed["removed"]
        assert not (project / ".runs" / old_id).exists()
        assert (project / ".runs" / keep_id).exists()


# ─── bundle ──────────────────────────────────────────────────


class TestBundle:
    def _make_failed_run(self, project):
        rid = "20260101T100000Z-b0b0b0"
        _fab_run(project, rid, [
            {"event": "provider_request_failed", "status_code": 403,
             "error_class": "permission_denied", "message": "denied"},
        ], {"run_id": rid, "command": "chief_of_staff daily", "outcome": "failed",
            "counts": {"error": 1, "warning": 0}})
        return rid

    def test_bundle_contents_exact_allowlist(self, temp_project, tmp_path):
        _, project, config_path = temp_project
        rid = self._make_failed_run(project)
        out_zip = tmp_path / "cos-support.zip"
        rc, _out = _run(config_path, "logs", "bundle", "--run-id", rid, "--output", str(out_zip))
        assert rc == 0
        names = set(zipfile.ZipFile(out_zip).namelist())
        expected = {"events.jsonl", "summary.json", "diagnosis.json", "readiness.json",
                    "meta.json", "config_shape.json"}
        assert names == expected  # EXACTLY — no extra files

    def test_bundle_config_shape_types_not_values_no_secrets(self, temp_project, tmp_path):
        config, project, config_path = temp_project
        rid = self._make_failed_run(project)
        out_zip = tmp_path / "cos-support.zip"
        _run(config_path, "logs", "bundle", "--run-id", rid, "--output", str(out_zip))
        shape = json.loads(zipfile.ZipFile(out_zip).read("config_shape.json"))

        # No secret/token/password-named keys anywhere.
        def _no_secret_keys(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    assert "secret" not in k.lower()
                    assert "token" not in k.lower()
                    assert "password" not in k.lower()
                    _no_secret_keys(v)
            elif isinstance(node, list):
                for v in node:
                    _no_secret_keys(v)

        _no_secret_keys(shape)
        assert "api_client_secret" not in shape
        # Leaves are type names, not values.
        assert shape["company"]["name"] == "str"
        assert shape["stale_threshold_days"] == "int"
        # And the real secret value never appears in the serialized shape.
        assert "super-secret-value" not in json.dumps(shape)

    def test_bundle_meta_and_diagnosis(self, temp_project, tmp_path):
        _, project, config_path = temp_project
        rid = self._make_failed_run(project)
        out_zip = tmp_path / "cos-support.zip"
        _run(config_path, "logs", "bundle", "--run-id", rid, "--output", str(out_zip))
        zf = zipfile.ZipFile(out_zip)
        meta = json.loads(zf.read("meta.json"))
        assert meta["plugin_version"]
        assert meta["provider"] == "google_api"
        assert isinstance(meta["capability_report"], dict) and meta["capability_report"]
        diagnosis = json.loads(zf.read("diagnosis.json"))
        assert diagnosis["primary"]["id"] == "permission_denied"

    def test_bundle_latest_failed(self, temp_project, tmp_path):
        _, project, config_path = temp_project
        self._make_failed_run(project)
        out_zip = tmp_path / "cos-support.zip"
        rc, out = _run(config_path, "logs", "bundle", "--latest-failed", "--output", str(out_zip))
        assert rc == 0
        assert out_zip.exists()


# ─── readiness pointer ───────────────────────────────────────


class TestReadinessPointer:
    def test_readiness_fail_prints_diagnose_pointer(self, temp_project, monkeypatch):
        _, _, config_path = temp_project

        class _FakeVerify:
            def run_verification(self, config, include_writes=False):
                names = ["auth", "mail_read", "mail_folder_scoped", "mail_tags_list",
                         "calendar_read", "files_read", "mail_draft", "mail_tag_write",
                         "files_write", "mail_send", "calendar_write"]
                checks = {n: {"status": "pass", "detail": n} for n in names}
                checks["files_read"] = {"status": "fail", "detail": "drive list denied"}
                return {"provider": "google_api", "checks": checks,
                        "read_ready": False, "write_ready": "partial"}

        import chief_of_staff
        monkeypatch.setattr(chief_of_staff, "_get_workspace_verify", lambda: _FakeVerify())
        rc, out = _run(config_path, "readiness", "--summary")
        assert rc == 1
        assert "Run ID:" in out
        assert "logs diagnose --run-id" in out

    def test_readiness_json_has_run_id(self, temp_project, monkeypatch):
        _, _, config_path = temp_project
        import chief_of_staff
        monkeypatch.setattr(chief_of_staff, "_get_workspace_verify", lambda: None)
        rc, out = _run(config_path, "readiness", "--json")
        parsed = json.loads(out)
        assert "run_id" in parsed


# ─── lifecycle wiring ────────────────────────────────────────


class TestLifecycle:
    def test_operational_command_creates_run(self, temp_project):
        _, project, config_path = temp_project
        before = {p.name for p in (project / ".runs").iterdir()}
        rc, _out = _run(config_path, "daily", "--summary")
        after = {p.name for p in (project / ".runs").iterdir()}
        new_runs = after - before
        assert len(new_runs) == 1
        run_id = next(iter(new_runs))
        summary_path = project / ".runs" / run_id / "summary.json"
        assert summary_path.exists()
        summary = json.loads(summary_path.read_text())
        assert summary["outcome"] in {"success", "degraded", "failed"}
        assert summary["command"] == "chief_of_staff daily"

    def test_logs_command_does_not_create_run(self, temp_project):
        _, project, config_path = temp_project
        # Seed one run so `recent` has something to list.
        _fab_run(project, "20260101T100000Z-c0c0c0", [{"event": "run_started"}],
                 {"run_id": "20260101T100000Z-c0c0c0", "outcome": "success"})
        before = {p.name for p in (project / ".runs").iterdir()}
        rc, _out = _run(config_path, "logs", "recent")
        after = {p.name for p in (project / ".runs").iterdir()}
        assert rc == 0
        assert before == after  # no new run directory created
