#!/usr/bin/env python3
"""v0.7.5 — doctor flags cron prompts that point at plugin paths or skills
that no longer exist.

Field report: after upgrades, headless cron jobs kept referencing a moved
script path and a ``chief-of-staff:`` skill that the plugin never shipped.
The runs broke silently; ``cron_jobs`` and ``cron_skill_files`` both stayed
green because neither reads the prompt text of the user's own jobs.

The check is read-only: it reads ``$HERMES_HOME/cron/jobs.json`` and never
edits a job.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "shared" / "scripts"))

import cron_prompts  # noqa: E402

CURRENT = "skills/daily-briefing/scripts/daily_briefing.py"


def _job(prompt: str, name: str = "Morning", **extra) -> dict:
    return {"id": "j1", "name": name, "prompt": prompt, **extra}


def _scan(*jobs):
    return cron_prompts.scan_jobs(list(jobs), plugin_root=PLUGIN_ROOT)


# ─── paths ───────────────────────────────────────────────────────────────────

def test_current_relative_path_is_clean():
    assert _scan(_job(f"Run .venv/bin/python {CURRENT} --input x.json")) == []


def test_current_absolute_path_is_clean():
    assert _scan(_job(f"Run {PLUGIN_ROOT / CURRENT}")) == []


def test_moved_relative_path_is_flagged_with_the_new_location():
    [finding] = _scan(_job("Run python scripts/daily_briefing.py then deliver."))
    assert finding["kind"] == "path"
    assert finding["job"] == "Morning"
    assert finding["ref"] == "scripts/daily_briefing.py"
    assert CURRENT in finding["hint"]


def test_moved_absolute_path_under_an_old_install_is_flagged(tmp_path):
    old = tmp_path / "plugins" / "chief-of-staff" / "scripts" / "daily_briefing.py"
    [finding] = _scan(_job(f"python3 {old}"))
    assert finding["kind"] == "path" and CURRENT in finding["hint"]


def test_missing_path_inside_the_plugin_is_flagged():
    gone = PLUGIN_ROOT / "shared" / "scripts" / "no_such_script_v075.py"
    [finding] = _scan(_job(f"python {gone}"))
    assert finding["kind"] == "path"


def test_path_relative_to_a_referenced_skill_is_clean():
    job = _job("Run scripts/daily_briefing.py", skills=["chief-of-staff:daily-briefing"])
    assert _scan(job) == []


def test_bare_plugin_script_name_is_clean():
    """Our own workflow crons say 'Then run: chief_of_staff.py workflows fire'."""
    assert _scan(_job("Then run: chief_of_staff.py workflows fire --schedule-id x")) == []


def test_users_own_scripts_are_not_our_business(tmp_path):
    assert _scan(_job(f"python {tmp_path / 'mine' / 'report.py'} and tools/sync.py")) == []


def test_trailing_punctuation_is_not_part_of_the_path():
    assert _scan(_job(f"Run `{CURRENT}`.")) == []
    assert _scan(_job(f"Run ({CURRENT}), then stop.")) == []


# ─── skills ──────────────────────────────────────────────────────────────────

def test_real_plugin_skill_is_clean():
    assert _scan(_job("brief", skills=["chief-of-staff:daily-briefing"])) == []
    assert _scan(_job("Load chief-of-staff:weekly-review and run it.")) == []


def test_unknown_namespaced_skill_is_flagged_with_scope_hint():
    job = _job("capture", name="Knowledge", skills=["chief-of-staff:workspace-knowledge-capture"])
    [finding] = _scan(job)
    assert finding["kind"] == "skill"
    assert finding["ref"] == "chief-of-staff:workspace-knowledge-capture"
    assert "not a chief-of-staff plugin skill" in finding["hint"]
    assert "prefix" in finding["hint"], "tell the user how to reference an agent-scope skill"


def test_unknown_skill_in_prompt_text_is_flagged():
    [finding] = _scan(_job("Load chief-of-staff:daily-briefings now"))
    assert finding["kind"] == "skill"
    assert "daily-briefing" in finding["hint"], "suggest the closest real skill"


def test_single_skill_field_is_read():
    [finding] = _scan(_job("x", skill="chief-of-staff:nope"))
    assert finding["ref"] == "chief-of-staff:nope"


def test_generated_workflow_skill_is_clean(tmp_path):
    root = tmp_path / "plugin"
    (root / "skills" / "daily-briefing").mkdir(parents=True)
    (root / "skills" / "daily-briefing" / "SKILL.md").write_text("x")
    (root / "skills.local" / "month-end").mkdir(parents=True)
    (root / "skills.local" / "month-end" / "SKILL.md").write_text("x")
    assert cron_prompts.scan_jobs([_job("x", skills=["chief-of-staff:month-end"])], plugin_root=root) == []


def test_other_namespaces_are_ignored():
    assert _scan(_job("x", skills=["productivity:google-workspace", "workspace-knowledge-capture"])) == []


def test_findings_are_deduplicated_per_job():
    job = _job("scripts/daily_briefing.py then again scripts/daily_briefing.py")
    assert len(_scan(job)) == 1


# ─── the doctor check ────────────────────────────────────────────────────────

def _write_jobs(home: Path, payload) -> None:
    (home / "cron").mkdir(parents=True, exist_ok=True)
    (home / "cron" / "jobs.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def hermes(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("CHIEF_OF_STAFF_HERMES_HOME", raising=False)
    return home


def test_check_passes_when_there_are_no_jobs(hermes):
    result = cron_prompts.check_cron_prompts(False, None, Path("company.yaml"))
    assert result.name == "cron_prompts" and result.status == "pass"


def test_check_warns_on_stale_references(hermes):
    _write_jobs(hermes, {"jobs": [
        _job("python scripts/daily_briefing.py", name="Morning"),
        _job("ok", name="Knowledge", skills=["chief-of-staff:workspace-knowledge-capture"]),
        _job(f"python {CURRENT}", name="Fine"),
    ]})
    result = cron_prompts.check_cron_prompts(False, None, Path("company.yaml"))
    assert result.status == "warn"
    assert "Morning" in result.detail and "Knowledge" in result.detail
    assert "Fine" not in result.detail
    assert CURRENT in result.detail


def test_check_accepts_a_bare_list(hermes):
    _write_jobs(hermes, [_job("python scripts/daily_briefing.py")])
    assert cron_prompts.check_cron_prompts(False, None, Path("c")).status == "warn"


def test_check_passes_on_clean_jobs(hermes):
    _write_jobs(hermes, {"jobs": [_job(f"python {CURRENT}")]})
    result = cron_prompts.check_cron_prompts(False, None, Path("c"))
    assert result.status == "pass" and "1" in result.detail


def test_check_warns_on_unreadable_store(hermes):
    (hermes / "cron").mkdir()
    (hermes / "cron" / "jobs.json").write_text("{not json", encoding="utf-8")
    assert cron_prompts.check_cron_prompts(False, None, Path("c")).status == "warn"


def test_fix_never_edits_the_jobs(hermes):
    _write_jobs(hermes, {"jobs": [_job("python scripts/daily_briefing.py")]})
    before = (hermes / "cron" / "jobs.json").read_bytes()
    result = cron_prompts.check_cron_prompts(True, None, Path("c"))
    assert result.fix_applied is False
    assert (hermes / "cron" / "jobs.json").read_bytes() == before


def test_detail_does_not_leak_prompt_bodies(hermes):
    _write_jobs(hermes, {"jobs": [_job("SECRET-CLIENT-NAME python scripts/daily_briefing.py")]})
    assert "SECRET-CLIENT-NAME" not in cron_prompts.check_cron_prompts(False, None, Path("c")).detail


def test_check_is_registered_with_doctor():
    import doctor_base
    names = [c.__name__ for c in doctor_base.CHECKS]
    assert "_check_cron_prompts" in names


def test_index_covers_plugin_code_only(tmp_path):
    """CI puts TMPDIR inside the checkout; scratch files there are not plugin scripts."""
    root = tmp_path / "plugin"
    (root / "skills" / "a" / "scripts").mkdir(parents=True)
    (root / "skills" / "a" / "scripts" / "real.py").write_text("")
    (root / "tmp" / "pytest-0").mkdir(parents=True)
    (root / "tmp" / "pytest-0" / "scratch.py").write_text("")
    index = cron_prompts._script_index(root)
    assert index == {"real.py": ["skills/a/scripts/real.py"]}
