#!/usr/bin/env python3
"""v0.7.8 — Hermes home resolution must stay inside the pytest sandbox."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "shared" / "scripts"))


def test_doctor_google_workspace_skill_does_not_read_path_home_hermes(tmp_path, monkeypatch):
    """Regression: _check_google_workspace used HERMES_HOME-or-~/.hermes only.

    With HERMES_HOME pointed at an empty sandbox, a skill installed only under
    Path.home()/.hermes must not make doctor report installed.
    """
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    real_skill = (
        fake_home
        / ".hermes"
        / "skills"
        / "productivity"
        / "google-workspace"
        / "SKILL.md"
    )
    real_skill.parent.mkdir(parents=True)
    real_skill.write_text("---\nname: google-workspace\n---\n", encoding="utf-8")

    sandbox = tmp_path / "sandbox-hermes"
    sandbox.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("HERMES_HOME", str(sandbox))
    monkeypatch.setenv("CHIEF_OF_STAFF_HERMES_HOME", str(sandbox))

    from doctor_base import _check_google_workspace

    result = _check_google_workspace(False, None, Path("company.yaml"))
    assert result.name == "google_workspace_skill"
    assert result.status == "warn"
    assert "not found" in result.detail


def _assert_google_api_script_uses_sandbox_hermes(
    tmp_path,
    monkeypatch,
    *,
    skill_dir: str,
    module_name: str,
) -> None:
    """google_api_script() must resolve via get_hermes_home(), not Path.home()/.hermes."""
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    decoy = (
        fake_home
        / ".hermes"
        / "skills"
        / "productivity"
        / "google-workspace"
        / "scripts"
        / "google_api.py"
    )
    decoy.parent.mkdir(parents=True)
    decoy.write_text("# decoy — must not be chosen\n", encoding="utf-8")

    sandbox = tmp_path / "sandbox-hermes"
    sandbox_script = (
        sandbox
        / "skills"
        / "productivity"
        / "google-workspace"
        / "scripts"
        / "google_api.py"
    )
    sandbox_script.parent.mkdir(parents=True)
    sandbox_script.write_text("# sandbox\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("HERMES_HOME", str(sandbox))
    monkeypatch.setenv("CHIEF_OF_STAFF_HERMES_HOME", str(sandbox))

    scripts = PLUGIN_ROOT / "skills" / skill_dir / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    mod = importlib.import_module(module_name)
    importlib.reload(mod)
    assert mod.google_api_script() == sandbox_script


def test_daily_briefing_google_api_script_uses_sandbox_hermes(tmp_path, monkeypatch):
    _assert_google_api_script_uses_sandbox_hermes(
        tmp_path,
        monkeypatch,
        skill_dir="daily-briefing",
        module_name="daily_briefing",
    )


def test_calendar_scan_google_api_script_uses_sandbox_hermes(tmp_path, monkeypatch):
    _assert_google_api_script_uses_sandbox_hermes(
        tmp_path,
        monkeypatch,
        skill_dir="calendar-manager",
        module_name="calendar_scan",
    )
