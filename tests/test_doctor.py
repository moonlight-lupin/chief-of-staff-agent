#!/usr/bin/env python3
"""Tests for chief-of-staff doctor."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from doctor import _check_docuseal, run_checks  # noqa: E402


def minimal_config(tmp_path: Path, project_root: Path | None = None) -> Path:
    root = project_root or (tmp_path / "project")
    path = tmp_path / "company.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "company": {
                    "name": "Test Pte Ltd",
                    "jurisdiction": "SG",
                    "incorporation_date": "2024-01-01",
                    "financial_year_end": "31 Dec",
                    "currency": "SGD",
                },
                "google": {"service_account_path": "~/missing.json", "domain": "example.com", "delegate_email": "ops@example.com"},
                "paths": {"project_root": str(root), "wiki_path": str(root / "wiki"), "templates": str(tmp_path / "templates")},
                "delivery": {"channel": "local", "briefing_time": "08:00", "weekly_review_day": "friday", "weekly_review_time": "17:00", "timezone": "UTC"},
                "backup": {"schedule": "0 3 * * 0"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_all_checks_run(tmp_path):
    config = minimal_config(tmp_path)
    report = run_checks(fix=False, config=str(config))
    assert len(report) >= 18
    names = {r.name for r in report}
    assert "plugin_root" in names
    assert "python_compile" in names
    assert "audit_runs_dirs" in names


def test_json_output_valid(tmp_path):
    config = minimal_config(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "doctor.py"), "--config", str(config), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    data = json.loads(proc.stdout)
    assert isinstance(data, list)
    assert all("name" in row and "status" in row and "detail" in row for row in data)


def test_fix_creates_missing_dirs(tmp_path):
    project = tmp_path / "missing-project"
    config = minimal_config(tmp_path, project_root=project)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "doctor.py"), "--config", str(config), "--fix", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(proc.stdout)
    assert project.exists()
    for name in ("pipeline", "invoices", "expenses", "todos"):
        assert (project / f"{name}.yaml").exists()
    assert (project / ".audit").exists()
    assert (project / ".runs").exists()
    assert (project / "wiki" / "purpose.md").exists()
    assert any(row["fix_applied"] for row in report)


def test_reports_missing_config(tmp_path):
    missing = tmp_path / "no-company.yaml"
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "doctor.py"), "--config", str(missing), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    data = json.loads(proc.stdout)
    company = next(row for row in data if row["name"] == "company_yaml")
    assert company["status"] == "fail"
    assert "missing" in company["detail"] or "invalid" in company["detail"]


def test_assistant_name_warns_when_missing(tmp_path):
    """Named CoS triggers need assistant.name — doctor must surface the gap."""
    config = minimal_config(tmp_path)
    report = run_checks(fix=False, config=str(config))
    row = next(r for r in report if r.name == "assistant_name")
    assert row.status == "warn"
    assert "assistant.name" in row.detail
    assert "named" in row.detail.lower() or "trigger" in row.detail.lower()


def test_assistant_name_passes_when_set(tmp_path):
    config = minimal_config(tmp_path)
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["assistant"] = {"name": "Ada"}
    config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    report = run_checks(fix=False, config=str(config))
    row = next(r for r in report if r.name == "assistant_name")
    assert row.status == "pass"
    assert "Ada" in row.detail


def test_assistant_name_passes_generic_default_with_guidance(tmp_path):
    config = minimal_config(tmp_path)
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["assistant"] = {"name": "Chief of Staff"}
    config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    report = run_checks(fix=False, config=str(config))
    row = next(r for r in report if r.name == "assistant_name")
    assert row.status == "pass"
    assert "Chief of Staff" in row.detail
    assert "distinctive" in row.detail.lower() or "default" in row.detail.lower()


def test_assistant_name_warns_on_blank(tmp_path):
    config = minimal_config(tmp_path)
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["assistant"] = {"name": "   "}
    config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    report = run_checks(fix=False, config=str(config))
    row = next(r for r in report if r.name == "assistant_name")
    assert row.status == "warn"


def test_doctor_refuses_non_https_docuseal_url(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCUSEAL_API_KEY", "secret")

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("unsafe DocuSeal URL must not be opened")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    result = _check_docuseal(
        False,
        {"esign": {"provider": "docuseal", "url": "http://docuseal.example.com", "domain": "docuseal.example.com"}},
        tmp_path / "company.yaml",
    )

    assert result.status == "fail"
    assert "https" in result.detail.lower()


def test_doctor_refuses_metadata_ip(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCUSEAL_API_KEY", "secret")

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("metadata DocuSeal URL must not be opened")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    result = _check_docuseal(
        False,
        {"esign": {"provider": "docuseal", "url": "https://169.254.169.254/latest/meta-data"}},
        tmp_path / "company.yaml",
    )

    assert result.status == "fail"
    assert "metadata" in result.detail.lower() or "link-local" in result.detail.lower()


def test_doctor_refuses_loopback_ip(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCUSEAL_API_KEY", "secret")

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("loopback DocuSeal URL must not be opened")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    result = _check_docuseal(
        False,
        {"esign": {"provider": "docuseal", "url": "https://127.0.0.1:3000"}},
        tmp_path / "company.yaml",
    )
    assert result.status == "fail"
    assert "link-local" in result.detail.lower() or "metadata" in result.detail.lower()


def test_doctor_refuses_private_ip(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCUSEAL_API_KEY", "secret")

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("private DocuSeal URL must not be opened")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    result = _check_docuseal(
        False,
        {"esign": {"provider": "docuseal", "url": "https://10.0.0.1:3000"}},
        tmp_path / "company.yaml",
    )
    assert result.status == "fail"
    assert "link-local" in result.detail.lower() or "metadata" in result.detail.lower()


def test_doctor_probes_https_matching_domain(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCUSEAL_API_KEY", "secret")
    monkeypatch.delenv("DOCUSEAL_MCP_TOKEN", raising=False)
    opened: list[urllib.request.Request] = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req, timeout=0):
        opened.append(req)
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = _check_docuseal(
        False,
        {
            "esign": {
                "provider": "docuseal",
                "url": "https://docuseal.example.com",
                "domain": "docuseal.example.com",
                "auth_mode": "pro_api_only",
            }
        },
        tmp_path / "company.yaml",
    )

    assert result.status == "pass"
    assert [req.full_url for req in opened] == [
        "https://docuseal.example.com",
        "https://docuseal.example.com/api/templates?limit=1",
    ]
    assert opened[1].get_header("X-auth-token") == "secret"
