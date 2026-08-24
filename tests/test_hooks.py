#!/usr/bin/env python3
"""Tests for hooks.py — all 9 quality hooks."""

import sys
import os
import json
import tempfile
from pathlib import Path
from datetime import date, datetime, timedelta

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

# Import hooks module
HOOKS_PATH = PLUGIN_ROOT
if str(HOOKS_PATH) not in sys.path:
    sys.path.insert(0, str(HOOKS_PATH))

from hooks import (
    company_context_primer,
    yaml_integrity_checker,
    stale_briefing_detector,
    pipeline_stage_validator,
    format_enforcer,
    self_sign_guard,
    deadline_urgency_injection,
    _load_company_yaml,
    _cos_skills_loaded,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def cos_context():
    """Context dict with CoS skills loaded."""
    return {"loaded_skills": ["daily-briefing", "pipeline-manager", "chief-of-staff:bookkeeper"]}

@pytest.fixture
def no_cos_context():
    """Context dict with no CoS skills."""
    return {"loaded_skills": ["some-other-skill"]}

@pytest.fixture
def empty_context():
    """Empty context (no skill info)."""
    return {}

@pytest.fixture
def tmp_config(tmp_path):
    """Create a temporary company.yaml with test data."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    config = config_dir / "company.yaml"
    config.write_text(f"""\
company:
  name: "Test Co Pte Ltd"
  jurisdiction: SG
  currency: SGD
  business_type: professional_services

google:
  domain: "test.com"
  delegate_email: "founder@test.com"

paths:
  project_root: "{project_dir}"
  wiki_path: "{project_dir}/wiki/"

delivery:
  channel: telegram
  briefing_time: "20:00"
  timezone: "Asia/Singapore"

sales_stages: [Lead, Proposal Sent, NDA Signed, Contract Signed, Invoiced, Paid]
stale_threshold_days: 14

deadlines:
  custom:
    - name: "Annual Return"
      due: "{(date.today() - timedelta(days=5)).isoformat()}"
      notes: "ACRA filing"
    - name: "Tax Filing"
      due: "{(date.today() + timedelta(days=60)).isoformat()}"
      notes: "IRAS"
""")

    # Create pipeline.yaml with test deals
    (project_dir / "pipeline.yaml").write_text("""\
deals:
  - id: deal-001
    client_name: Acme Corp
    stage: Proposal Sent
    value: 4500
    currency: SGD
    created: "2026-06-15"
    last_activity: "2026-07-01"
    documents: []
    notes: ""
  - id: deal-002
    client_name: Beta Ltd
    stage: Lead
    value: 2000
    currency: SGD
    created: "2026-07-05"
    last_activity: "2026-07-05"
    documents: []
    notes: ""
""")

    # Create invoices.yaml
    (project_dir / "invoices.yaml").write_text("""\
invoices:
  - id: INV-001
    direction: sent
    counterparty: Acme Corp
    amount: 4500
    currency: SGD
    issue_date: "2026-07-01"
    due_date: "2026-07-15"
    status: sent
    paid_date: null
""")

    # Set env var to point to our test config
    old_val = os.environ.get("CHIEF_OF_STAFF_CONFIG")
    os.environ["CHIEF_OF_STAFF_CONFIG"] = str(config)
    yield {"config": config, "project": project_dir}
    if old_val:
        os.environ["CHIEF_OF_STAFF_CONFIG"] = old_val
    else:
        del os.environ["CHIEF_OF_STAFF_CONFIG"]


# ── 1. Company Context Primer ────────────────────────────────────────────────

class TestCompanyContextPrimer:
    def test_returns_string_with_cos_skills(self, tmp_config, cos_context):
        result = company_context_primer(cos_context)
        assert result is not None
        assert "[CoS Context]" in result
        assert "Test Co Pte Ltd" in result

    def test_returns_none_without_cos_skills(self, tmp_config, no_cos_context):
        result = company_context_primer(no_cos_context)
        assert result is None

    def test_includes_company_name(self, tmp_config, cos_context):
        result = company_context_primer(cos_context)
        assert "Test Co Pte Ltd" in result
        assert "SG" in result

    def test_includes_pipeline_info(self, tmp_config, cos_context):
        result = company_context_primer(cos_context)
        assert "Pipeline" in result

    def test_includes_ar_outstanding(self, tmp_config, cos_context):
        result = company_context_primer(cos_context)
        assert "AR outstanding" in result
        assert "4,500" in result

    def test_includes_overdue_deadline(self, tmp_config, cos_context):
        result = company_context_primer(cos_context)
        assert "OVERDUE" in result
        assert "Annual Return" in result

    def test_returns_none_when_no_config(self, cos_context, monkeypatch):
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", "/nonexistent/path.yaml")
        result = company_context_primer(cos_context)
        assert result is None


# ── 2. YAML Integrity Checker ────────────────────────────────────────────────

class TestYAMLIntegrityChecker:
    def test_valid_yaml_no_warning(self, tmp_config, cos_context):
        result = yaml_integrity_checker(
            "terminal",
            {"command": "echo 'test' > pipeline.yaml"},
            "done",
            cos_context,
        )
        # pipeline.yaml is valid in our fixture, so no error
        # But the command didn't actually write to the real file
        # The hook checks if the file *currently* parses, so it should pass
        assert result is None or "integrity" not in result.lower()

    def test_no_yaml_in_command_returns_none(self, tmp_config, cos_context):
        result = yaml_integrity_checker(
            "terminal",
            {"command": "ls -la /tmp"},
            "done",
            cos_context,
        )
        assert result is None

    def test_returns_none_without_cos_skills(self, tmp_config, no_cos_context):
        result = yaml_integrity_checker(
            "terminal",
            {"command": "echo test > pipeline.yaml"},
            "done",
            no_cos_context,
        )
        assert result is None

    def test_detects_corrupted_yaml(self, tmp_config, cos_context):
        """Write a broken YAML file and verify the hook catches it."""
        root = tmp_config["project"]
        # Corrupt the pipeline.yaml
        (root / "pipeline.yaml").write_text("deals:\n  - id: broken\n    bad: : : value\n")
        result = yaml_integrity_checker(
            "terminal",
            {"command": "echo test >> pipeline.yaml"},
            "done",
            cos_context,
        )
        assert result is not None
        assert "integrity" in result.lower() or "pipeline" in result.lower()


# ── 3. Stale Briefing Detector ───────────────────────────────────────────────

class TestStaleBriefingDetector:
    def test_no_marker_no_data_returns_none(self, tmp_path, cos_context, monkeypatch):
        # Point to empty project dir
        config = tmp_path / "company.yaml"
        config.write_text(f"""\
company:
  name: Test
  jurisdiction: SG
paths:
  project_root: "{tmp_path}"
""")
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config))
        result = stale_briefing_detector(cos_context)
        # No pipeline.yaml, no marker — should return None
        assert result is None

    def test_no_marker_with_data_suggests_briefing(self, tmp_config, cos_context):
        # Remove the marker if it exists
        marker = tmp_config["project"] / ".last_briefing"
        if marker.exists():
            marker.unlink()
        result = stale_briefing_detector(cos_context)
        assert result is not None
        assert "briefing" in result.lower()

    def test_fresh_briefing_no_warning(self, tmp_config, cos_context):
        marker = tmp_config["project"] / ".last_briefing"
        marker.write_text(datetime.now().isoformat())
        result = stale_briefing_detector(cos_context)
        assert result is None

    def test_stale_briefing_warns(self, tmp_config, cos_context):
        marker = tmp_config["project"] / ".last_briefing"
        old_time = datetime.now() - timedelta(hours=48)
        marker.write_text(old_time.isoformat())
        result = stale_briefing_detector(cos_context)
        assert result is not None
        assert "2" in result  # 2 days ago

    def test_returns_none_without_cos_skills(self, tmp_config, no_cos_context):
        result = stale_briefing_detector(no_cos_context)
        assert result is None


# ── 4. Pipeline Stage Validator ──────────────────────────────────────────────

class TestPipelineStageValidator:
    def test_valid_stage_no_warning(self, tmp_config, cos_context):
        result = pipeline_stage_validator(
            "terminal",
            {"command": 'python script.py --stage "Proposal Sent" --file pipeline.yaml'},
            cos_context,
        )
        assert result is None

    def test_invalid_stage_warns(self, tmp_config, cos_context):
        result = pipeline_stage_validator(
            "terminal",
            {"command": 'python script.py --stage "Proposal Send" --file pipeline.yaml'},
            cos_context,
        )
        assert result is not None
        assert "Proposal Send" in result
        assert "not a configured" in result.lower() or "valid stages" in result.lower()

    def test_no_pipeline_in_command_returns_none(self, tmp_config, cos_context):
        result = pipeline_stage_validator(
            "terminal",
            {"command": "ls -la /tmp"},
            cos_context,
        )
        assert result is None

    def test_case_insensitive_match(self, tmp_config, cos_context):
        result = pipeline_stage_validator(
            "terminal",
            {"command": '--stage "proposal sent" pipeline.yaml'},
            cos_context,
        )
        # "proposal sent" (lowercase) should match "Proposal Sent" case-insensitively
        assert result is None


# ── 5. Format Enforcer ───────────────────────────────────────────────────────

class TestFormatEnforcer:
    def test_briefing_with_all_markers_passes(self, cos_context):
        response = "📋 Daily Briefing\n📅 Calendar\n⏰ Deadlines\n📧 Inbox"
        result = format_enforcer(response, cos_context)
        assert result is None

    def test_briefing_missing_markers_warns(self, cos_context):
        response = "Here's your briefing: nothing much today."
        result = format_enforcer(response, cos_context)
        assert result is not None
        assert "Briefing" in result
        assert "missing" in result.lower()

    def test_weekly_review_with_all_markers_passes(self):
        ctx = {"loaded_skills": ["weekly-review"]}
        response = "📊 Weekly Review\n✅ Completed\n📅 Next Week\n⚠️ Attention"
        result = format_enforcer(response, ctx)
        assert result is None

    def test_weekly_review_missing_markers_warns(self):
        ctx = {"loaded_skills": ["weekly-review"]}
        response = "This week was productive."
        result = format_enforcer(response, ctx)
        assert result is not None

    def test_no_briefing_skill_returns_none(self, no_cos_context):
        result = format_enforcer("some response", no_cos_context)
        assert result is None


# ── 6. Self-Sign Guard ───────────────────────────────────────────────────────

class TestSelfSignGuard:
    def test_multiple_locations_warns(self, cos_context):
        result_str = json.dumps([
            {"page": 1, "matched_text": "Signature:", "party_context": "Client"},
            {"page": 1, "matched_text": "Signature:", "party_context": "Service Provider"},
        ])
        result = self_sign_guard(
            "terminal",
            {"command": "python sign_detector.py doc.pdf"},
            result_str,
            cos_context,
        )
        assert result is not None
        assert "2 signature" in result
        assert "ALL" in result

    def test_single_location_no_warning(self, cos_context):
        result_str = json.dumps([
            {"page": 1, "matched_text": "Signature:", "party_context": "Service Provider"},
        ])
        result = self_sign_guard(
            "terminal",
            {"command": "python sign_detector.py doc.pdf"},
            result_str,
            cos_context,
        )
        assert result is None

    def test_no_sign_detector_in_command(self, cos_context):
        result = self_sign_guard(
            "terminal",
            {"command": "ls -la"},
            "output",
            cos_context,
        )
        assert result is None

    def test_malformed_result_returns_none(self, cos_context):
        result = self_sign_guard(
            "terminal",
            {"command": "python sign_detector.py doc.pdf"},
            "not json at all",
            cos_context,
        )
        assert result is None

    def test_returns_none_without_cos_skills(self, no_cos_context):
        result = self_sign_guard(
            "terminal",
            {"command": "python sign_detector.py doc.pdf"},
            '[{"page": 1}]',
            no_cos_context,
        )
        assert result is None


# ── 7. Deadline Urgency Injection ────────────────────────────────────────────

class TestDeadlineUrgencyInjection:
    def test_overdue_deadline_injected(self, tmp_config, cos_context):
        result = deadline_urgency_injection(cos_context)
        assert result is not None
        assert "OVERDUE" in result
        assert "Annual Return" in result

    def test_status_done_not_injected(self, tmp_path, cos_context, monkeypatch):
        """A deadline marked status: done must not trigger the overdue alarm."""
        config = tmp_path / "company.yaml"
        config.write_text(f"""\
company:
  name: Test
  jurisdiction: SG
paths:
  project_root: "{tmp_path}"
deadlines:
  custom:
    - name: "Annual Return"
      due: "{(date.today() - timedelta(days=5)).isoformat()}"
      notes: "ACRA filing"
      status: done
""")
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config))
        result = deadline_urgency_injection(cos_context)
        assert result is None

    def test_future_deadline_not_injected(self, tmp_path, cos_context, monkeypatch):
        config = tmp_path / "company.yaml"
        config.write_text(f"""\
company:
  name: Test
  jurisdiction: SG
paths:
  project_root: "{tmp_path}"
deadlines:
  custom:
    - name: "Future Thing"
      due: "{(date.today() + timedelta(days=90)).isoformat()}"
""")
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config))
        result = deadline_urgency_injection(cos_context)
        assert result is None

    def test_no_deadlines_returns_none(self, tmp_path, cos_context, monkeypatch):
        config = tmp_path / "company.yaml"
        config.write_text(f"""\
company:
  name: Test
  jurisdiction: SG
paths:
  project_root: "{tmp_path}"
""")
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config))
        result = deadline_urgency_injection(cos_context)
        assert result is None

    def test_returns_none_without_cos_skills(self, tmp_config, no_cos_context):
        result = deadline_urgency_injection(no_cos_context)
        assert result is None


# ── Helper function tests ────────────────────────────────────────────────────

class TestHelpers:
    def test_cos_skills_loaded_with_cos_skill(self, cos_context):
        assert _cos_skills_loaded(cos_context) is True

    def test_cos_skills_loaded_without_cos_skill(self, no_cos_context):
        assert _cos_skills_loaded(no_cos_context) is False

    def test_cos_skills_loaded_empty_context(self, empty_context):
        # Empty context defaults to False — persona only when CoS skill confirmed
        assert _cos_skills_loaded(empty_context) is False

    def test_load_company_yaml_returns_dict(self, tmp_config):
        config = _load_company_yaml()
        assert config is not None
        assert config["company"]["name"] == "Test Co Pte Ltd"

    def test_load_company_yaml_missing_returns_none(self, monkeypatch):
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", "/nonexistent.yaml")
        assert _load_company_yaml() is None


# ── Registration test ────────────────────────────────────────────────────────

class TestHookRegistration:
    def test_all_hooks_defined(self):
        from hooks import ALL_HOOKS
        total = sum(len(hooks) for hooks in ALL_HOOKS.values())
        assert total == 10

    def test_all_events_covered(self):
        from hooks import ALL_HOOKS
        assert "pre_llm_call" in ALL_HOOKS
        assert "post_tool_call" in ALL_HOOKS
        assert "on_session_start" in ALL_HOOKS
        assert "pre_tool_call" in ALL_HOOKS
        assert "post_llm_call" in ALL_HOOKS

    def test_pre_llm_call_has_two_hooks(self):
        from hooks import ALL_HOOKS
        assert len(ALL_HOOKS["pre_llm_call"]) == 3

    def test_post_tool_call_has_two_hooks(self):
        from hooks import ALL_HOOKS
        assert len(ALL_HOOKS["post_tool_call"]) == 2