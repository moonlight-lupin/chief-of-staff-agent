#!/usr/bin/env python3
"""Fix-round A RED→GREEN: W2 refusals, H4 US-8 strip wording, BLOCK-2 path traversal."""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from workflows import validate_workflow  # noqa: E402

FROZEN = datetime(2026, 9, 20, 6, 17, tzinfo=timezone.utc)
US8_BOUND = "bound — await operator approval"


def _step(step_id, name, signal, **extra):
    step = {
        "id": step_id,
        "name": name,
        "description": f"{name} end to end.",
    }
    step.update(signal)
    step.update(extra)
    return step


def _raw_workflow(name="invoice-chase", steps=None):
    if steps is None:
        steps = [
            _step(
                "propose-send",
                "Propose send",
                {"review_queue": {"action_type": "gmail.send"}},
            ),
        ]
    return {
        "name": name,
        "description": "Chases unpaid invoices until they are settled.",
        "steps": steps,
    }


def _validated(name="invoice-chase", steps=None):
    return validate_workflow(_raw_workflow(name=name, steps=steps))


@pytest.fixture
def temp_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".audit").mkdir()
    (project / ".runs").mkdir()
    (project / "workflows").mkdir()
    config = {
        "company": {
            "name": "Test Co",
            "jurisdiction": "SG",
            "currency": "SGD",
            "incorporation_date": "2026-01-01",
            "financial_year_end": "31 Dec",
            "business_type": "professional_services",
        },
        "google": {
            "delegate_email": "test@test.com",
            "account_alias": "test",
            "domain": "test.com",
            "service_account_path": "/tmp/sa.json",
        },
        "paths": {
            "project_root": str(project),
            "wiki_path": str(project / "wiki"),
            "templates": str(PLUGIN_ROOT / "shared" / "templates"),
        },
        "delivery": {
            "channel": "telegram",
            "briefing_time": "08:00",
            "weekly_review_day": "friday",
            "weekly_review_time": "17:00",
            "timezone": "Asia/Singapore",
        },
        "integrations": {"workspace": {"provider": "google_api"}},
        "sales_stages": ["Lead", "Proposal Sent", "Paid"],
    }
    config_path = project / "company.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))
    return config, project, config_path


def _cos(config_path, *argv):
    import chief_of_staff

    buf = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        rc = chief_of_staff.main(["--config", str(config_path), *argv])
    return rc, buf.getvalue(), err.getvalue()


def _create_action(config, action_type="gmail.send"):
    from state_db import create_pending_action

    return create_pending_action(
        config=config,
        action_type=action_type,
        provider="google_api",
        target="billing@example.com",
        payload={"to": "billing@example.com", "subject": "nudge", "body": "please pay"},
        summary=f"Test {action_type}",
    )


def _land_executed(config, action_id, *, success=True):
    from state_db import approve_pending_action, mark_executed, mark_executing

    approve_pending_action(config, action_id, approver="MH", reason="Reviewed")
    mark_executing(config, action_id)
    return mark_executed(config, action_id, {"success": success})


def test_advance_run_refuses_awaiting_approval_until_bound_action_executed(temp_project):
    """W2: advance_run refuses awaiting-approval unless bound action is executed+success."""
    import workflow_runs

    config, _project, _config_path = temp_project
    run = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=_validated(),
        config=config,
        now=FROZEN,
    )
    action = _create_action(config)
    workflow_runs.bind_action(
        run["workflow_run_id"], "propose-send", action["id"], config=config
    )
    parked = workflow_runs.get_run(run["workflow_run_id"], config=config)
    assert parked["state"] == "awaiting-approval"
    with pytest.raises(workflow_runs.WorkflowRunError, match="approval"):
        workflow_runs.advance_run(run["workflow_run_id"], 0, config=config)
    still = workflow_runs.get_run(run["workflow_run_id"], config=config)
    assert still["state"] == "awaiting-approval"
    assert still["current_step_index"] == 0

    _land_executed(config, action["id"], success=True)
    advanced = workflow_runs.advance_run(run["workflow_run_id"], 0, config=config)
    assert advanced["step_status"][0] == "completed"


def test_cmd_advance_refuses_non_manual_current_step(temp_project):
    """W2: workflows advance refuses when the current step's signal is not manual."""
    import workflow_runs

    config, _project, config_path = temp_project
    run = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=_validated(
            steps=[
                _step(
                    "list-overdue",
                    "List overdue",
                    {"command": {"pattern": "show overdue invoices"}},
                ),
            ]
        ),
        config=config,
        now=FROZEN,
    )
    rc, out, err = _cos(
        config_path,
        "workflows",
        "advance",
        "--run-id",
        run["workflow_run_id"],
    )
    assert rc != 0
    blob = (out.strip() or err.strip())
    payload = json.loads(blob)
    assert payload.get("error")
    assert "manual" in str(payload.get("error")).lower()
    fetched = workflow_runs.get_run(run["workflow_run_id"], config=config)
    assert fetched["current_step_index"] == 0
    assert fetched["step_status"][0] == "pending"


def test_uninstall_refuses_path_traversal_bookkeeper_intact(temp_project):
    """BLOCK-2: workflows uninstall ../skills/bookkeeper is refused; bundled skill intact."""
    import workflow_install

    _config, _project, config_path = temp_project
    bookkeeper = PLUGIN_ROOT / "skills" / "bookkeeper"
    skill = bookkeeper / "SKILL.md"
    assert bookkeeper.is_dir()
    sentinel = skill.read_text(encoding="utf-8")
    rc, out, err = _cos(config_path, "workflows", "uninstall", "../skills/bookkeeper")
    assert rc != 0
    blob = (out.strip() or err.strip())
    payload = json.loads(blob)
    assert payload.get("error")
    assert bookkeeper.is_dir()
    assert skill.read_text(encoding="utf-8") == sentinel
    with pytest.raises(workflow_install.WorkflowInstallError):
        workflow_install.uninstall_workflow("../skills/bookkeeper", config=_config)


def test_pointer_strip_bound_approval_uses_us8_wording(temp_project):
    """H4: bound approval strip renders US-8 wording, not a bare self-approve imperative."""
    import workflow_hooks
    import workflow_runs

    config, _project, _config_path = temp_project
    run = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=_validated(),
        config=config,
        now=FROZEN,
    )
    action = _create_action(config)
    action_id = action["id"]
    workflow_runs.bind_action(
        run["workflow_run_id"], "propose-send", action_id, config=config
    )
    strip = workflow_hooks.pointer_strip(
        {"session_id": "sess-1"}, config=config, now=FROZEN
    )
    assert isinstance(strip, str)
    assert f"action {action_id} {US8_BOUND}" in strip
    assert "review_queue.py approve --action-id" in strip
    approve_at = strip.lower().find("review_queue.py approve")
    operator_at = strip.lower().find("operator:")
    assert operator_at != -1
    assert operator_at < approve_at
    assert len(strip) <= 200


def test_start_run_audit_carries_action_actor_and_run_id(temp_project):
    """W3: mutate_kv audit for start includes workflow.start, operator, workflow_run_id."""
    from audit_log import read_audit
    import workflow_runs

    config, _project, _config_path = temp_project
    run = workflow_runs.start_run(
        "invoice-chase",
        "message",
        "sess-1",
        workflow=_validated(),
        config=config,
        now=FROZEN,
    )
    entries = read_audit("workflow_runs", limit=20, config=config)
    assert entries
    last = entries[-1]
    assert last.get("action") == "workflow.start"
    assert last.get("actor") == "operator"
    after = last.get("after") or {}
    assert after.get("workflow_run_id") == run["workflow_run_id"]
