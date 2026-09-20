#!/usr/bin/env python3
"""RED-phase tests: workflow-architect skill (spec v3.1 US-1 + US-2 surface).

Target: skills/workflow-architect/SKILL.md + scripts/workflow_architect.py
(propose_workflow, write_workflow). Generation and install stay on the existing
pure core: workflows.validate_workflow / generate_skill_md and
workflow_install.install_workflow.

Draft-shape decision (pinned here, restated in /tmp/arch-red-report.md):
propose_workflow returns a dict in the workflows.py loader shape (a mapping
validate_workflow accepts). It does not return a YAML string. Invalid interview
answers raise workflows.WorkflowValidationError (a WorkflowError) — never an
{error: ...} dict, never WorkflowRunError (that exception is the run-store
layer). Invalid YAML is never written.

RED marker: missing skill → pytest.fail("RED: ... not implemented yet"), the
same ImportError helper as tests/test_workflow_orchestrator_batch1.py::_wf
and the missing-file helper as _load_sample. Tests collect; they fail at
call time.

Every test is expected to FAIL until the GREEN-phase skill lands.
Tests only — no skill implementation.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
ARCHITECT_SCRIPTS = PLUGIN_ROOT / "skills" / "workflow-architect" / "scripts"
SKILL_MD = PLUGIN_ROOT / "skills" / "workflow-architect" / "SKILL.md"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(ARCHITECT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(ARCHITECT_SCRIPTS))

from workflows import (  # noqa: E402
    WORKFLOW_NAME_MAX,
    WorkflowError,
    generate_skill_md,
    validate_workflow,
)

FROZEN = datetime(2026, 9, 20, 6, 17, tzinfo=timezone.utc)
SKILL_DESCRIPTION_MAX = 1024
OVERLAY_NAMES = ("arch-nudge", "arch-close")

# Frontmatter keys shared by skills/deadline-tracker/SKILL.md and
# skills/daily-briefing/SKILL.md (exact keys, not values).
SHIPPED_FRONTMATTER_KEYS = ("name", "description", "version", "author", "license", "metadata")

# Example-config employer / PII that a shipped skill must not copy in.
_PII_FRAGMENTS = (
    "Acme Advisory",
    "Alicia Tan",
    "202400001A",
    "M90000001A",
    "Raffles Place",
    "alicia@",
    "acme-advisory",
    "+65 6123 4567",
    "client_secret",
    "BEGIN PRIVATE",
    "sk-ant-",
    "sk-live",
    "api_key",
)


def _architect():
    """Import the skill script or fail with the batch-1 RED marker."""
    try:
        import workflow_architect
    except ImportError as e:
        pytest.fail(
            f"RED: skills/workflow-architect/scripts/workflow_architect.py not implemented yet ({e})"
        )
    return workflow_architect


def _skill_md_text() -> str:
    """Read SKILL.md or fail with the batch-1 missing-file RED marker."""
    if not SKILL_MD.is_file():
        pytest.fail("RED: skills/workflow-architect/SKILL.md not created yet")
    return SKILL_MD.read_text(encoding="utf-8")


def _overlay(name: str) -> Path:
    return PLUGIN_ROOT / "skills.local" / name


def _overlay_skill(name: str) -> Path:
    return _overlay(name) / "SKILL.md"


def _answers_two_step(**overrides):
    """Valid interview answers for a simple 2-step notification workflow.

    Second step has no command/file/review_queue signature so the architect
    must default it to manual (US-7).
    """
    answers = {
        "name": "arch-nudge",
        "trigger": {"message": ["run arch-nudge"]},
        "steps": [
            {
                "id": "draft-note",
                "name": "Draft note",
                "description": "Write the status note from daily bullets.",
                "file": {"path": "out/status-nudge.md"},
            },
            {
                "id": "notify-ops",
                "name": "Notify operator",
                "description": "Tell the operator the status note is ready.",
            },
        ],
        "inputs": "daily status bullets",
        "outputs": "status note at out/status-nudge.md",
        "delivery_target": {"channel": "briefing", "target": "operator"},
        "failure_policy": {"on_failure": "halt"},
    }
    answers.update(overrides)
    return answers


def _answers_three_step(**overrides):
    """Valid interview answers for a 3-step workflow with an approval gate + cron."""
    answers = {
        "name": "arch-close",
        "trigger": {
            "message": ["run arch-close"],
            "schedule": {"cron": "0 9 * * 1", "timezone": "UTC"},
        },
        "steps": [
            {
                "id": "list-open",
                "name": "List open",
                "description": "List items still open this week.",
                "command": {"pattern": "show open items"},
            },
            {
                "id": "propose-mail",
                "name": "Propose mail",
                "description": "Propose the close-out email for operator approval.",
                "review_queue": {"action_type": "gmail.send"},
            },
            {
                "id": "log-close",
                "name": "Log close",
                "description": "Record that the close-out mail was proposed.",
                "manual": True,
                "required": False,
            },
        ],
        "inputs": "open items list",
        "outputs": "approved close-out email",
        "delivery_target": {"channel": "briefing", "target": "operator"},
        "failure_policy": {"on_failure": "halt"},
    }
    answers.update(overrides)
    return answers


# Inline YAML-shaped drafts (fixtures, not written until round-trip). Each
# must pass validate_workflow today — they pin the loader shape the skill emits.
SAMPLE_TWO_STEP = {
    "name": "arch-nudge",
    "description": "Notify the operator with a two-step status note.",
    "triggers": {"message": ["run arch-nudge"]},
    "steps": [
        {
            "id": "draft-note",
            "name": "Draft note",
            "description": "Write the status note from daily bullets.",
            "file": {"path": "out/status-nudge.md"},
        },
        {
            "id": "notify-ops",
            "name": "Notify operator",
            "description": "Tell the operator the status note is ready.",
            "manual": True,
        },
    ],
    "delivery": {"channel": "briefing", "target": "operator"},
    "failure_policy": {"on_failure": "halt"},
}

SAMPLE_THREE_STEP = {
    "name": "arch-close",
    "description": "Weekly close with an approval-gated send.",
    "triggers": {
        "message": ["run arch-close"],
        "schedule": {"cron": "0 9 * * 1", "timezone": "UTC"},
    },
    "steps": [
        {
            "id": "list-open",
            "name": "List open",
            "description": "List items still open this week.",
            "command": {"pattern": "show open items"},
        },
        {
            "id": "propose-mail",
            "name": "Propose mail",
            "description": "Propose the close-out email for operator approval.",
            "review_queue": {"action_type": "gmail.send"},
        },
        {
            "id": "log-close",
            "name": "Log close",
            "description": "Record that the close-out mail was proposed.",
            "manual": True,
            "required": False,
        },
    ],
    "delivery": {"channel": "briefing", "target": "operator"},
    "failure_policy": {"on_failure": "halt"},
}

VALID_ANSWERS_FIXTURES = (_answers_two_step(), _answers_three_step())


def _assert_loader_draft(draft):
    """Pin: propose_workflow returns a dict validate_workflow will accept."""
    assert isinstance(draft, dict), (
        "propose_workflow must return a dict in the workflows.py loader shape, not YAML text"
    )
    assert "error" not in draft, "invalid drafts raise WorkflowValidationError; they do not return {error: ...}"
    assert "name" in draft and "steps" in draft


def _yaml_path(project: Path, name: str) -> Path:
    return project / "workflows" / f"{name}.yaml"


def _pending_ids(config):
    from state_db import list_pending_actions

    return [item.get("id") for item in (list_pending_actions(config, include_expired=True) or [])]


def _fake_subprocess_ok(monkeypatch, captured=None):
    """Mirror tests/test_workflow_orchestrator_batch4.py hermes-cron stub."""

    class _FakeProc:
        stdout = "ok"
        stderr = ""
        returncode = 0

    def _fake_run(argv, capture_output=True, text=True, timeout=60, check=False, **kwargs):
        if captured is not None:
            captured.append(list(argv))
        return _FakeProc()

    monkeypatch.setattr(subprocess, "run", _fake_run)


@pytest.fixture(autouse=True)
def _clean_architect_overlays():
    """install_workflow writes gitignored skills.local; never leave fixtures behind."""
    for name in OVERLAY_NAMES:
        shutil.rmtree(_overlay(name), ignore_errors=True)
    yield
    for name in OVERLAY_NAMES:
        shutil.rmtree(_overlay(name), ignore_errors=True)


@pytest.fixture
def temp_project(tmp_path, monkeypatch):
    """StateDB + CLI config on a temp project root (batch 2/5 fixture)."""
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


# ---------------------------------------------------------------------------
# Part 1: propose_workflow / write_workflow
# ---------------------------------------------------------------------------


def test_propose_workflow_valid_answers_always_validate(temp_project):
    """US-1: every valid-answers fixture yields a draft validate_workflow accepts."""
    config, project, _config_path = temp_project
    arch = _architect()
    for answers in VALID_ANSWERS_FIXTURES:
        draft = arch.propose_workflow(answers, config, now=FROZEN)
        _assert_loader_draft(draft)
        validate_workflow(draft)
        assert not list(project.joinpath("workflows").glob("*.yaml")), (
            "propose_workflow must not write YAML; write_workflow is the only writer"
        )


def test_propose_workflow_maps_interview_to_loader_shape(temp_project):
    """Interview keys map onto the schema; inputs/outputs are not top-level YAML keys."""
    config, _project, _config_path = temp_project
    arch = _architect()
    answers = _answers_two_step()
    draft = arch.propose_workflow(answers, config, now=FROZEN)
    _assert_loader_draft(draft)
    normalized = validate_workflow(draft)
    assert normalized["name"] == "arch-nudge"
    assert normalized["triggers"]["message"] == ["run arch-nudge"]
    assert [step["id"] for step in normalized["steps"]] == ["draft-note", "notify-ops"]
    assert normalized["delivery"]["channel"] == "briefing"
    assert normalized["delivery"]["target"] == "operator"
    assert normalized["failure_policy"]["on_failure"] == "halt"
    for leaked in ("inputs", "outputs", "trigger", "delivery_target"):
        assert leaked not in draft, f"{leaked} is an interview field, not a workflow YAML key"
    blob = " ".join(
        [str(normalized.get("description", ""))]
        + [str(step.get("description", "")) for step in normalized["steps"]]
    )
    assert "daily status bullets" in blob
    assert "status note" in blob


def test_propose_workflow_defaults_unsigned_step_to_manual(temp_project):
    """US-7: a step with no command/file/review_queue signature defaults to manual."""
    config, _project, _config_path = temp_project
    arch = _architect()
    draft = arch.propose_workflow(_answers_two_step(), config, now=FROZEN)
    normalized = validate_workflow(draft)
    notify = next(step for step in normalized["steps"] if step["id"] == "notify-ops")
    assert "manual" in notify
    assert "command" not in notify
    assert "file" not in notify
    assert "review_queue" not in notify


def test_propose_workflow_missing_trigger_raises(temp_project):
    """Interview requires a trigger even though triggers are optional on the schema."""
    config, _project, _config_path = temp_project
    arch = _architect()
    for trigger in (None, {}, ""):
        answers = _answers_two_step(trigger=trigger)
        with pytest.raises(WorkflowError):
            arch.propose_workflow(answers, config, now=FROZEN)
    answers = _answers_two_step()
    del answers["trigger"]
    with pytest.raises(WorkflowError):
        arch.propose_workflow(answers, config, now=FROZEN)


def test_propose_workflow_empty_steps_raises(temp_project):
    config, _project, _config_path = temp_project
    arch = _architect()
    with pytest.raises(WorkflowError):
        arch.propose_workflow(_answers_two_step(steps=[]), config, now=FROZEN)


def test_propose_workflow_bad_delivery_target_raises(temp_project):
    config, _project, _config_path = temp_project
    arch = _architect()
    bads = (
        None,
        "",
        "telegram",
        {"channel": "briefing"},
        {"channel": "briefing", "target": ""},
        {"channel": "briefing", "target": ["operator"]},
        {"channel": "briefing", "target": {"to": "operator"}},
    )
    for target in bads:
        with pytest.raises(WorkflowError):
            arch.propose_workflow(_answers_two_step(delivery_target=target), config, now=FROZEN)


def test_propose_workflow_failure_policy_unknown_step_raises(temp_project):
    """failure_policy.step must name an interview step id."""
    config, _project, _config_path = temp_project
    arch = _architect()
    with pytest.raises(WorkflowError):
        arch.propose_workflow(
            _answers_two_step(failure_policy={"on_failure": "skip", "step": "no-such-step"}),
            config,
            now=FROZEN,
        )


def test_propose_workflow_name_longer_than_32_raises(temp_project):
    config, _project, _config_path = temp_project
    arch = _architect()
    too_long = "a" * (WORKFLOW_NAME_MAX + 1)
    assert len(too_long) > WORKFLOW_NAME_MAX
    with pytest.raises(WorkflowError):
        arch.propose_workflow(_answers_two_step(name=too_long), config, now=FROZEN)


def test_propose_workflow_name_not_fs_safe_raises(temp_project):
    config, _project, _config_path = temp_project
    arch = _architect()
    for bad in ("Bad_Name", "-bad", "bad-", "UPPER", "bad name", "../escape", "/tmp/abs"):
        with pytest.raises(WorkflowError):
            arch.propose_workflow(_answers_two_step(name=bad), config, now=FROZEN)


def test_write_workflow_confirm_false_returns_proposal_writes_nothing(temp_project):
    """confirm=False is a dry-run: return the proposal, write nothing."""
    config, project, _config_path = temp_project
    arch = _architect()
    draft = arch.propose_workflow(_answers_two_step(), config, now=FROZEN)
    proposal = arch.write_workflow(draft, config, confirm=False)
    _assert_loader_draft(proposal)
    assert not _yaml_path(project, "arch-nudge").exists()
    assert list(project.joinpath("workflows").glob("*.yaml")) == []


def test_write_workflow_confirm_true_writes_project_local_yaml(temp_project):
    """US-1: confirmed draft lands at <project_root>/workflows/<name>.yaml only."""
    config, project, _config_path = temp_project
    arch = _architect()
    draft = arch.propose_workflow(_answers_two_step(), config, now=FROZEN)
    arch.write_workflow(draft, config, confirm=True)
    path = _yaml_path(project, "arch-nudge")
    assert path.is_file()
    assert path.resolve().is_relative_to(project.resolve())
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    validate_workflow(loaded)
    assert loaded["name"] == "arch-nudge"
    assert not _overlay("arch-nudge").exists(), "write_workflow must not install the overlay skill"


def test_write_workflow_invalid_draft_never_written(temp_project):
    """Invalid YAML is never written, even with confirm=True."""
    config, project, _config_path = temp_project
    arch = _architect()
    with pytest.raises(WorkflowError):
        arch.write_workflow({"name": "arch-nudge"}, config, confirm=True)
    assert not _yaml_path(project, "arch-nudge").exists()


def test_write_workflow_refuses_path_escaping_project_root(temp_project):
    """Name with ../ or an absolute path must not write outside project_root."""
    config, project, _config_path = temp_project
    arch = _architect()
    base = dict(SAMPLE_TWO_STEP)
    abs_hit = Path("/tmp/arch-abs.yaml")
    abs_existed = abs_hit.exists()
    before = {path.resolve() for path in project.rglob("*")}
    for bad_name in ("../escape", "/tmp/arch-abs", "foo/../../outside"):
        draft = dict(base)
        draft["name"] = bad_name
        with pytest.raises(WorkflowError):
            arch.write_workflow(draft, config, confirm=True)
    after = {path.resolve() for path in project.rglob("*")}
    assert after == before
    assert not (project / "escape.yaml").exists()
    assert not (project.parent / "outside.yaml").exists()
    if not abs_existed and abs_hit.exists():
        pytest.fail("write_workflow wrote an absolute-path workflow file")


def test_write_workflow_idempotent_rewrite_is_byte_identical(temp_project):
    """Same answers → byte-identical file (now= frozen; no timestamps)."""
    config, project, _config_path = temp_project
    arch = _architect()
    answers = _answers_two_step()
    first = arch.propose_workflow(answers, config, now=FROZEN)
    arch.write_workflow(first, config, confirm=True)
    path = _yaml_path(project, "arch-nudge")
    bytes_first = path.read_bytes()
    second = arch.propose_workflow(answers, config, now=FROZEN)
    arch.write_workflow(second, config, confirm=True)
    assert path.read_bytes() == bytes_first


def test_architect_does_not_touch_review_queue_or_cron(temp_project, monkeypatch):
    """US-1 last AC: capturing a workflow never mutates the review-queue store or cron."""
    import state_db
    import workflow_cron

    config, project, _config_path = temp_project
    arch = _architect()
    mutations: list[str] = []
    captured: list[list[str]] = []
    original_create = state_db.create_pending_action
    original_approve = state_db.approve_pending_action

    def _spy_create(*args, **kwargs):
        mutations.append("create_pending_action")
        return original_create(*args, **kwargs)

    def _spy_approve(*args, **kwargs):
        mutations.append("approve_pending_action")
        return original_approve(*args, **kwargs)

    monkeypatch.setattr(state_db, "create_pending_action", _spy_create)
    monkeypatch.setattr(state_db, "approve_pending_action", _spy_approve)
    _fake_subprocess_ok(monkeypatch, captured)
    before = _pending_ids(config)
    draft = arch.propose_workflow(_answers_three_step(), config, now=FROZEN)
    arch.write_workflow(draft, config, confirm=True)
    assert mutations == []
    assert _pending_ids(config) == before
    assert workflow_cron.get_cron_binding("arch-close", config=config) is None
    assert not any("cron" in cmd and "create" in cmd for cmd in captured)
    assert _yaml_path(project, "arch-close").is_file()


# ---------------------------------------------------------------------------
# Part 2: SKILL.md quality pins (mirror test_plugin_structure / test_phase4_notes)
# ---------------------------------------------------------------------------


def test_skill_md_exists_with_shipped_frontmatter_keys():
    """Frontmatter keys match deadline-tracker and daily-briefing exactly."""
    content = _skill_md_text()
    assert content.startswith("---"), "workflow-architect: no frontmatter"
    parts = content.split("---", 2)
    assert len(parts) >= 3, "workflow-architect: malformed frontmatter"
    fm = yaml.safe_load(parts[1])
    assert fm is not None
    for key in SHIPPED_FRONTMATTER_KEYS:
        assert key in fm, f"workflow-architect: missing {key!r} in frontmatter"
    hermes = (fm.get("metadata") or {}).get("hermes") or {}
    assert "tags" in hermes
    assert "related_skills" in hermes
    assert fm["name"] == "workflow-architect"
    assert str(fm["license"]) in ("Apache-2.0", "MIT")
    assert str(fm["version"]).strip()
    assert str(fm["author"]).strip()


def test_skill_md_name_and_description_bounds():
    """Hermes description bound: non-empty, ≤1024, 'Use when' like the two reference skills."""
    content = _skill_md_text()
    fm = yaml.safe_load(content.split("---", 2)[1])
    description = str(fm["description"]).strip()
    assert description
    assert len(description) <= SKILL_DESCRIPTION_MAX
    assert description.startswith("Use when")


def test_skill_md_interview_covers_required_anchors():
    """US-1: structured interview covers trigger/steps/inputs/outputs/delivery/failure-policy."""
    content = _skill_md_text()
    lower = content.lower()
    for anchor in ("trigger", "steps", "inputs", "outputs", "delivery"):
        assert anchor in lower, f"SKILL.md interview is missing {anchor!r}"
    assert "failure policy" in lower or "failure-policy" in lower


def test_skill_md_names_post_confirmation_commands():
    """Operator commands after confirmation: workflows install <name>; workflows list."""
    content = _skill_md_text()
    assert "workflows install" in content
    assert "chief_of_staff.py workflows list" in content


def test_skill_md_states_confirmation_and_review_queue_out_of_scope():
    """US-1: confirm-before-write, and the artifact write is out of review-queue scope."""
    lower = _skill_md_text().lower()
    assert "confirm" in lower
    assert "review-queue" in lower or "review queue" in lower
    assert any(
        phrase in lower
        for phrase in (
            "out of scope",
            "outside the review queue",
            "not a review-queue",
            "not review-queue approval",
            "visibility approval",
            "artifact write",
        )
    ), "SKILL.md must state the YAML write is not a review-queue connector mutation"


def test_skill_md_has_no_pii_secrets_or_employer_names():
    content = _skill_md_text()
    lower = content.lower()
    for fragment in _PII_FRAGMENTS:
        assert fragment.lower() not in lower, f"SKILL.md must not contain {fragment!r}"
    emails = re.findall(r"[A-Za-z0-9._%+-]+@(?!example\.com)[A-Za-z0-9.-]+\.[A-Za-z]{2,}", content)
    assert emails == [], f"SKILL.md must not contain non-example emails: {emails}"


# ---------------------------------------------------------------------------
# Part 3: round-trip (interview → propose → validate → generate → install)
# ---------------------------------------------------------------------------


def test_round_trip_two_step_notification(temp_project, monkeypatch):
    """2-step notification: answers → propose → validate → generate_skill_md → install."""
    from workflow_install import install_workflow

    config, project, _config_path = temp_project
    validate_workflow(SAMPLE_TWO_STEP)
    arch = _architect()
    _fake_subprocess_ok(monkeypatch)
    draft = arch.propose_workflow(_answers_two_step(), config, now=FROZEN)
    _assert_loader_draft(draft)
    validated = validate_workflow(draft)
    markdown = generate_skill_md(validated)
    assert isinstance(markdown, str) and markdown.startswith("---")
    arch.write_workflow(draft, config, confirm=True)
    path = _yaml_path(project, "arch-nudge")
    assert path.is_file()
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["name"] == "arch-nudge"
    result = install_workflow("arch-nudge", config=config, session_id="sess-1", now=FROZEN)
    assert result["name"] == "arch-nudge"
    assert result["cron_installed"] is False
    skill = _overlay_skill("arch-nudge")
    assert skill.is_file()
    assert skill.read_text(encoding="utf-8") == markdown
    assert not (PLUGIN_ROOT / "skills" / "arch-nudge").exists()


def test_round_trip_three_step_approval_gate_scheduled(temp_project, monkeypatch):
    """3-step approval-gate + schedule: install writes overlay; doctor cron-skill-files passes."""
    import workflow_cron
    from workflow_install import install_workflow

    config, project, config_path = temp_project
    validate_workflow(SAMPLE_THREE_STEP)
    arch = _architect()
    _fake_subprocess_ok(monkeypatch)
    draft = arch.propose_workflow(_answers_three_step(), config, now=FROZEN)
    validated = validate_workflow(draft)
    assert any(step.get("requires_approval") for step in validated["steps"])
    assert "schedule" in validated["triggers"]
    markdown = generate_skill_md(validated)
    arch.write_workflow(draft, config, confirm=True)
    assert _yaml_path(project, "arch-close").is_file()
    result = install_workflow("arch-close", config=config, session_id="sess-1", now=FROZEN)
    # US-9 (round-A C2): first scheduled install proposes a cron.create
    # review-queue action; the cron registers only after approve + execute.
    pending_id = result.get("pending_action_id")
    assert result["cron_installed"] is False
    assert pending_id
    from workflow_cron import execute_cron_create
    from state_db import approve_pending_action
    approve_pending_action(config, pending_id, "operator", "smoke round-trip")
    exec_result = execute_cron_create(config, pending_id, now=FROZEN)
    assert exec_result.get("schedule_id")
    # Post-approval reinstall now writes the real binding.
    result2 = install_workflow("arch-close", config=config, session_id="sess-1", now=FROZEN)
    assert result2["cron_installed"] is True
    skill = _overlay_skill("arch-close")
    assert skill.is_file()
    assert skill.read_text(encoding="utf-8") == markdown
    check = workflow_cron.check_cron_skill_files(False, config, config_path)
    assert check.status == "pass"
