#!/usr/bin/env python3
"""Tests for state_store audit failure policy (non-strict + strict modes)."""

import sys
import os
import tempfile
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


@pytest.fixture
def tmp_project():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        project = root / "project"
        project.mkdir()
        config_dir = root / "config"
        config_dir.mkdir()
        config = config_dir / "company.yaml"
        config.write_text(f"""\
company:
  name: "Test Co"
  jurisdiction: SG
  incorporation_date: "2024-01-15"
  financial_year_end: "31 Dec"
  currency: SGD
  business_type: professional_services
google:
  service_account_path: "~/.hermes/test.json"
  domain: "test.com"
  delegate_email: "founder@test.com"
  account_alias: ""
paths:
  project_root: "{project}"
  wiki_path: "{project}/wiki/"
  templates: "{project}/templates/"
delivery:
  channel: telegram
  briefing_time: "20:00"
  weekly_review_day: friday
  weekly_review_time: "17:00"
  timezone: "Asia/Singapore"
sales_stages: [Lead, Proposal Sent, NDA Signed, Contract Signed, Invoiced, Paid]
stale_threshold_days: 14
calendar:
  reminder_minutes: 15
  auto_prep_brief: true
self_sign:
  signature_image: null
  auto_date: true
  output_format: pdf
  party_aliases:
    - "Service Provider"
backup:
  enabled: true
  schedule: "0 3 * * 0"
  drive_folder: "09_Backups/"
  exclude:
    - ".env"
""")
        (project / "pipeline.yaml").write_text("deals: []\n")
        os.environ["CHIEF_OF_STAFF_CONFIG"] = str(config)
        yield project
        os.environ.pop("CHIEF_OF_STAFF_CONFIG", None)
        os.environ.pop("CHIEF_OF_STAFF_AUDIT_STRICT", None)


def _make_deal(name="Test"):
    return {
        "id": f"deal-test-{name}", "client_name": name, "stage": "Lead",
        "status": "active", "created": "2026-07-09", "last_activity": "2026-07-09",
        "stage_history": [{"stage": "Lead", "at": "2026-07-09"}],
        "documents": [], "notes": [], "value": 0, "currency": "SGD",
    }


class TestAuditPolicy:
    def test_non_strict_succeeds_on_audit_failure(self, tmp_project):
        """Mutation succeeds even if audit log can't be written; no crash."""
        from state_db import load_store, save_store_atomic
        from audit_log import append_audit

        # Patch append_audit to always fail by making .audit a file instead of dir
        audit_path = tmp_project / ".audit"
        audit_path.mkdir()
        # Create a file at .audit/pipeline.log that blocks directory operations
        # Actually, make .audit itself a file to break the path
        import shutil
        shutil.rmtree(str(audit_path))
        audit_path.write_text("not a directory")  # .audit is a file, not a dir

        data = load_store("pipeline")
        data["deals"].append(_make_deal("NonStrict"))

        os.environ.pop("CHIEF_OF_STAFF_AUDIT_STRICT", None)
        # Should NOT raise — best-effort mode
        save_store_atomic("pipeline", data, action="add_deal",
                          before={"deals": []}, after=data)

        # Mutation should be on disk
        saved = yaml.safe_load((tmp_project / "pipeline.yaml").read_text())
        assert len(saved["deals"]) == 1
        assert saved["deals"][0]["client_name"] == "NonStrict"

    def test_strict_mode_raises_on_audit_failure(self, tmp_project):
        """In strict mode, audit failure causes save_store_atomic to raise."""
        from state_db import load_store, save_store_atomic, StateStoreError

        # Make .audit a file to force audit failure
        audit_path = tmp_project / ".audit"
        if audit_path.exists():
            import shutil
            if audit_path.is_dir():
                shutil.rmtree(str(audit_path))
            else:
                audit_path.unlink()
        audit_path.write_text("not a directory")

        data = load_store("pipeline")
        data["deals"].append(_make_deal("Strict"))

        os.environ["CHIEF_OF_STAFF_AUDIT_STRICT"] = "pipeline"
        with pytest.raises(StateStoreError, match="strict mode"):
            save_store_atomic("pipeline", data, action="add_deal",
                              before={"deals": []}, after=data)

        # Mutation still happened on disk (write succeeds before audit)
        saved = yaml.safe_load((tmp_project / "pipeline.yaml").read_text())
        assert len(saved["deals"]) == 1
        assert saved["deals"][0]["client_name"] == "Strict"

    def test_normal_audit_succeeds(self, tmp_project):
        """Normal operation: both mutation and audit succeed."""
        from state_db import load_store, save_store_atomic

        data = load_store("pipeline")
        data["deals"].append(_make_deal("Normal"))

        save_store_atomic("pipeline", data, action="add_deal",
                          before={"deals": []}, after=data)

        # Check audit log was written
        audit_log = tmp_project / ".audit" / "pipeline.log"
        assert audit_log.exists()
        content = audit_log.read_text()
        assert "add_deal" in content