#!/usr/bin/env python3
"""RED-phase tests: Workflow Orchestrator Batch 1 pure core (spec v3.2, WF-1..WF-6).

Targets shared/scripts/workflows.py (validate_workflow, generate_skill_md,
check_skill_name_free) plus the shipped examples/workflows/sample-invoice-chase.yaml.
Every test is expected to FAIL until the GREEN-phase implementation lands.
"""
import re
import sys
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SAMPLE_PATH = PLUGIN_ROOT / "examples" / "workflows" / "sample-invoice-chase.yaml"
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


def _wf():
    """Import the workflows module or fail with an explicit RED message."""
    try:
        import workflows
    except ImportError as e:
        pytest.fail(f"RED: shared/scripts/workflows.py not implemented yet ({e})")
    return workflows


def _step(step_id="step-one", name="Do the thing", signal=None, **extra):
    """Build a step dict; signal is a one-key {signal_type: payload} mapping."""
    step = {
        "id": step_id,
        "name": name,
        "description": "Does the thing end to end.",
    }
    if signal is not None:
        step.update(signal)
    step.update(extra)
    return step


def _base_workflow(name="invoice-chase"):
    """Minimal valid workflow used as the base for most cases."""
    return {
        "name": name,
        "description": "Chases unpaid invoices until they are settled.",
        "steps": [
            _step(
                step_id="list-overdue",
                name="List overdue invoices",
                signal={"command": {"pattern": "show overdue invoices"}},
            )
        ],
    }


def _load_sample():
    """Load the shipped sample workflow or fail with an explicit RED message."""
    if not SAMPLE_PATH.exists():
        pytest.fail("RED: examples/workflows/sample-invoice-chase.yaml not created yet")
    return yaml.safe_load(SAMPLE_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# WF-1: workflow schema validation
# ---------------------------------------------------------------------------


def test_wf1_valid_minimal_workflow_normalizes_defaults():
    """WF-1: a minimal valid workflow validates; defaults are applied."""
    wf = _wf()
    result = wf.validate_workflow(_base_workflow())
    assert result["name"] == "invoice-chase"
    step = result["steps"][0]
    assert step["required"] is True
    assert step["requires_approval"] is False


def test_wf1_missing_required_fields_rejected_with_field_prefix():
    """WF-1: missing name/description/steps raise errors naming the field."""
    wf = _wf()
    for field in ("name", "description", "steps"):
        data = _base_workflow()
        del data[field]
        with pytest.raises(wf.WorkflowValidationError) as excinfo:
            wf.validate_workflow(data)
        assert field in str(excinfo.value)


def test_wf1_empty_steps_list_rejected():
    """WF-1: steps must be a non-empty list."""
    wf = _wf()
    data = _base_workflow()
    data["steps"] = []
    with pytest.raises(wf.WorkflowValidationError):
        wf.validate_workflow(data)


def test_wf1_invalid_name_pattern_rejected():
    """WF-1: workflow name must be lowercase kebab-case."""
    wf = _wf()
    for bad in ("Bad_Name", "-bad", "bad-", "UPPER", "bad name"):
        with pytest.raises(wf.WorkflowValidationError):
            wf.validate_workflow(_base_workflow(name=bad))


def test_wf1_duplicate_step_id_rejected():
    """WF-1: step ids must be unique within the workflow."""
    wf = _wf()
    data = _base_workflow()
    data["steps"] = [
        _step(step_id="dup", signal={"command": {"pattern": "first"}}),
        _step(
            step_id="dup",
            name="Second thing",
            signal={"command": {"pattern": "second"}},
        ),
    ]
    with pytest.raises(wf.WorkflowValidationError):
        wf.validate_workflow(data)


def test_wf1_step_multiple_signals_rejected():
    """WF-1: a step with more than one completion signal is invalid."""
    wf = _wf()
    data = _base_workflow()
    data["steps"] = [
        {
            "id": "over-signalled",
            "name": "Over signalled",
            "description": "Has two signals.",
            "command": {"pattern": "do it"},
            "file": {"path": "out/done.txt"},
        }
    ]
    with pytest.raises(wf.WorkflowValidationError):
        wf.validate_workflow(data)


def test_wf1_step_zero_signals_rejected():
    """WF-1: a step with no completion signal is invalid."""
    wf = _wf()
    data = _base_workflow()
    data["steps"] = [
        {"id": "no-signal", "name": "No signal", "description": "No signal here."}
    ]
    with pytest.raises(wf.WorkflowValidationError):
        wf.validate_workflow(data)


def test_wf1_requires_approval_without_review_queue_rejected():
    """WF-1: requires_approval true demands a review_queue signal."""
    wf = _wf()
    data = _base_workflow()
    data["steps"] = [
        _step(
            step_id="needs-approval",
            name="Needs approval",
            signal={"command": {"pattern": "do it"}},
            requires_approval=True,
        )
    ]
    with pytest.raises(wf.WorkflowValidationError):
        wf.validate_workflow(data)


def test_wf1_unknown_top_level_key_rejected():
    """WF-1: strict schema — unknown top-level keys are errors."""
    wf = _wf()
    data = _base_workflow()
    data["priority"] = "high"
    with pytest.raises(wf.WorkflowValidationError):
        wf.validate_workflow(data)


def test_wf1_unknown_step_key_rejected():
    """WF-1: strict schema — unknown step keys are errors."""
    wf = _wf()
    data = _base_workflow()
    data["steps"][0]["timeout"] = "30s"
    with pytest.raises(wf.WorkflowValidationError):
        wf.validate_workflow(data)


def test_wf1_file_signal_absolute_path_rejected():
    """WF-1: file signal paths must be relative (no leading slash)."""
    wf = _wf()
    bad = _base_workflow()
    bad["steps"] = [
        _step(
            step_id="abs-path",
            name="Abs path",
            signal={"file": {"path": "/tmp/report.txt"}},
        )
    ]
    with pytest.raises(wf.WorkflowValidationError):
        wf.validate_workflow(bad)
    good = _base_workflow()
    good["steps"] = [
        _step(
            step_id="rel-path",
            name="Rel path",
            signal={"file": {"path": "reports/report.txt"}},
        )
    ]
    wf.validate_workflow(good)


def test_wf1_steps_limit_twenty_enforced():
    """WF-1: at most 20 steps — 20 passes, 21 fails."""
    wf = _wf()

    def many(n):
        data = _base_workflow()
        data["steps"] = [
            _step(
                step_id=f"step-{i:02d}",
                name=f"Step {i:02d}",
                signal={"command": {"pattern": f"run {i:02d}"}},
            )
            for i in range(1, n + 1)
        ]
        return data

    wf.validate_workflow(many(20))
    with pytest.raises(wf.WorkflowValidationError):
        wf.validate_workflow(many(21))


def test_wf1_schedule_timezone_absent_recorded_none():
    """WF-1: schedule without timezone records timezone as None (operator-local)."""
    wf = _wf()
    data = _base_workflow()
    data["triggers"] = {"schedule": {"cron": "0 9 * * 1"}}
    result = wf.validate_workflow(data)
    assert "triggers" in result, "normalized workflow must keep triggers"
    schedule = result["triggers"]["schedule"]
    assert schedule["cron"] == "0 9 * * 1"
    assert schedule["timezone"] is None


# ---------------------------------------------------------------------------
# WF-2: skill markdown generation
# ---------------------------------------------------------------------------


def test_wf2_deterministic_byte_identical():
    """WF-2: generate_skill_md is deterministic — same dict, identical bytes."""
    wf = _wf()
    validated = wf.validate_workflow(_base_workflow())
    assert wf.generate_skill_md(validated) == wf.generate_skill_md(validated)


def test_wf2_key_order_independent():
    """WF-2: generation sorts keys canonically — insertion order must not matter."""
    wf = _wf()
    forward = {
        "name": "key-order-check",
        "description": "Key order must not leak into the skill markdown.",
        "steps": [
            {
                "id": "alpha",
                "name": "Alpha step",
                "description": "First step.",
                "command": {"pattern": "run alpha"},
                "required": True,
                "requires_approval": False,
            }
        ],
    }
    reverse = {
        "steps": [
            {
                "requires_approval": False,
                "required": True,
                "command": {"pattern": "run alpha"},
                "description": "First step.",
                "name": "Alpha step",
                "id": "alpha",
            }
        ],
        "description": "Key order must not leak into the skill markdown.",
        "name": "key-order-check",
    }
    md_forward = wf.generate_skill_md(wf.validate_workflow(forward))
    md_reverse = wf.generate_skill_md(wf.validate_workflow(reverse))
    assert md_forward == md_reverse


def test_wf2_renders_h1_description_and_steps():
    """WF-2: output has the name in an H1, the description, a numbered step list."""
    wf = _wf()
    data = _base_workflow()
    data["steps"].append(
        _step(
            step_id="send-reminder",
            name="Send reminder",
            signal={"command": {"pattern": "send reminders"}},
        )
    )
    md = wf.generate_skill_md(wf.validate_workflow(data))
    h1_lines = [ln for ln in md.splitlines() if ln.startswith("# ")]
    assert any("invoice-chase" in ln for ln in h1_lines)
    assert "Chases unpaid invoices until they are settled." in md
    assert "List overdue invoices" in md
    assert "Send reminder" in md
    assert "1." in md and "2." in md


def test_wf2_no_generation_timestamp():
    """WF-2: generation must not embed a timestamp (determinism)."""
    wf = _wf()
    md = wf.generate_skill_md(wf.validate_workflow(_base_workflow()))
    assert re.search(r"20\d{2}-\d{2}-\d{2}", md) is None
    lowered = md.lower()
    assert "generated at" not in lowered
    assert "timestamp" not in lowered


def test_wf2_approval_and_degradation_notices():
    """WF-2: approval notice for requires_approval; degradation note for optional."""
    wf = _wf()
    data = _base_workflow()
    data["steps"] = [
        _step(
            step_id="approve-plan",
            name="Approve the plan",
            signal={"review_queue": {"action_type": "approve-plan"}},
            requires_approval=True,
        ),
        _step(
            step_id="optional-followup",
            name="Optional followup",
            signal={"command": {"pattern": "follow up"}},
            required=False,
        ),
    ]
    md = wf.generate_skill_md(wf.validate_workflow(data))
    lowered = md.lower()
    assert "approval" in lowered
    assert "degrad" in lowered or "optional" in lowered


def test_wf2_signal_rendering_per_type():
    """WF-2: each signal type renders pattern, path, action type, advance command."""
    wf = _wf()
    data = _base_workflow()
    data["steps"] = [
        _step(
            step_id="cmd-step",
            name="Command step",
            signal={"command": {"pattern": "run the ledger sweep"}},
        ),
        _step(
            step_id="file-step",
            name="File step",
            signal={"file": {"path": "reports/weekly-summary.md"}},
        ),
        _step(
            step_id="queue-step",
            name="Queue step",
            signal={"review_queue": {"action_type": "approve-expense"}},
        ),
        _step(
            step_id="manual-step",
            name="Manual step",
            signal={"manual": True},
        ),
    ]
    md = wf.generate_skill_md(wf.validate_workflow(data))
    assert "run the ledger sweep" in md
    assert "reports/weekly-summary.md" in md
    assert "approve-expense" in md
    lowered = md.lower()
    assert "claim" in lowered or "record-execution" in lowered
    assert "workflows advance" in md


def test_wf2_header_includes_regeneration_command():
    """WF-2: header comment carries the source YAML path and regeneration command."""
    wf = _wf()
    md = wf.generate_skill_md(wf.validate_workflow(_base_workflow()))
    assert "<!--" in md
    assert "regenerat" in md.lower()
    assert ".yaml" in md.lower() or ".yml" in md.lower()


# ---------------------------------------------------------------------------
# WF-3: YAML round-trip byte stability
# ---------------------------------------------------------------------------


def test_wf3_yaml_roundtrip_byte_stable(tmp_path):
    """WF-3: generate(validate(load(yaml))) is byte-stable across two calls."""
    wf = _wf()
    data = _base_workflow()
    data["triggers"] = {"message": ["chase invoices", "run the invoice chase"]}
    data["steps"].append(
        _step(
            step_id="send-reminder",
            name="Send reminder",
            signal={"file": {"path": "out/reminders.md"}},
        )
    )
    yaml_path = tmp_path / "invoice-chase.yaml"
    yaml_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    md1 = wf.generate_skill_md(wf.validate_workflow(loaded))
    md2 = wf.generate_skill_md(wf.validate_workflow(loaded))
    assert md1
    assert md1 == md2


# ---------------------------------------------------------------------------
# WF-4: bundled-skill shadow refusal
# ---------------------------------------------------------------------------


def test_wf4_bundled_skill_name_conflict_raises():
    """WF-4: a workflow name shadowing bundled skill daily-briefing is refused."""
    wf = _wf()
    with pytest.raises(wf.WorkflowNameConflict):
        wf.check_skill_name_free("daily-briefing", PLUGIN_ROOT)


def test_wf4_free_name_does_not_raise():
    """WF-4: a name with no bundled or local skill counterpart passes the check."""
    wf = _wf()
    wf.check_skill_name_free("test-wf-nonexistent", PLUGIN_ROOT)


def test_wf4_skills_local_conflict_raises(tmp_path):
    """WF-4: names shadowing a skills.local entry are refused too."""
    wf = _wf()
    (tmp_path / "skills.local" / "local-only-skill").mkdir(parents=True)
    with pytest.raises(wf.WorkflowNameConflict):
        wf.check_skill_name_free("local-only-skill", tmp_path)


# ---------------------------------------------------------------------------
# WF-5: sample workflow ships and validates
# ---------------------------------------------------------------------------


def test_wf5_sample_exists_validates_and_name_wellformed():
    """WF-5: the sample exists, validates, and its name fits the kebab pattern."""
    wf = _wf()
    data = _load_sample()
    wf.validate_workflow(data)
    name = data["name"]
    assert isinstance(name, str)
    assert len(name) <= 32
    assert NAME_PATTERN.match(name)


def test_wf5_sample_structure_requirements():
    """WF-5: sample has >=3 steps, approval, an optional step, delivery, 2 signals."""
    wf = _wf()
    data = _load_sample()
    wf.validate_workflow(data)
    steps = data["steps"]
    assert len(steps) >= 3
    assert any(step.get("requires_approval") is True for step in steps)
    assert any(step.get("required") is False for step in steps)
    assert "delivery" in data or "run_log_delivery" in data
    signal_types = set()
    for step in steps:
        for signal in ("command", "file", "review_queue", "manual"):
            if signal in step:
                signal_types.add(signal)
    assert len(signal_types) >= 2


def test_wf5_sample_skill_md_byte_stable():
    """WF-5: generate_skill_md on the sample does not raise and is byte-stable."""
    wf = _wf()
    validated = wf.validate_workflow(_load_sample())
    md1 = wf.generate_skill_md(validated)
    md2 = wf.generate_skill_md(validated)
    assert md1
    assert md1 == md2


# ---------------------------------------------------------------------------
# WF-6: name-length bound enforcement
# ---------------------------------------------------------------------------


def test_wf6_workflow_name_length_boundary():
    """WF-6: workflow name of exactly 32 chars passes; 33 fails."""
    wf = _wf()
    ok = _base_workflow(name="w" * 32)
    wf.validate_workflow(ok)
    bad = _base_workflow(name="w" * 33)
    with pytest.raises(wf.WorkflowValidationError):
        wf.validate_workflow(bad)


def test_wf6_step_name_length_boundary():
    """WF-6: step name of exactly 24 chars passes; 25 fails."""
    wf = _wf()
    ok = _base_workflow()
    ok["steps"] = [
        _step(step_id="ok-name", name="s" * 24, signal={"command": {"pattern": "run it"}})
    ]
    wf.validate_workflow(ok)
    bad = _base_workflow()
    bad["steps"] = [
        _step(step_id="bad-name", name="s" * 25, signal={"command": {"pattern": "run it"}})
    ]
    with pytest.raises(wf.WorkflowValidationError):
        wf.validate_workflow(bad)


def test_wf6_step_id_length_boundary():
    """WF-6: step id of exactly 24 chars passes; 25 fails."""
    wf = _wf()
    ok = _base_workflow()
    ok["steps"] = [
        _step(step_id="i" * 24, name="Ok id", signal={"command": {"pattern": "run it"}})
    ]
    wf.validate_workflow(ok)
    bad = _base_workflow()
    bad["steps"] = [
        _step(step_id="i" * 25, name="Bad id", signal={"command": {"pattern": "run it"}})
    ]
    with pytest.raises(wf.WorkflowValidationError):
        wf.validate_workflow(bad)