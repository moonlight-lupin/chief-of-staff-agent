#!/usr/bin/env python3
"""v0.7.5 — the test suite must never resolve to the operator's real state.

Field report: on a production-like install, test_build_trends_section_never_raises
(test_trends_v057.py) passed config=None, so StateDB fell back to
CHIEF_OF_STAFF_PROJECT_ROOT / load_config() and opened the operator's live
state.db. With trend snapshots present the test failed; on an empty root it
passed but left a state.db behind. The suite now pins every resolution path
to a per-test temporary directory.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "shared" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def _run_trends_tests(operator_root: Path, hermes_home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CHIEF_OF_STAFF_PROJECT_ROOT"] = str(operator_root)
    env["HERMES_HOME"] = str(hermes_home)
    env["CHIEF_OF_STAFF_HERMES_HOME"] = str(hermes_home)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(PLUGIN_ROOT / "tests" / "test_trends_v057.py")],
        cwd=str(PLUGIN_ROOT), env=env, capture_output=True, text=True, timeout=300,
    )


def _seed_live_root(root: Path) -> None:
    from test_trends_v057 import _summary_briefing, cfg
    from trend_history import capture_snapshot
    assert capture_snapshot(_summary_briefing(), cfg(root)) is not None


def test_trend_tests_pass_against_a_live_operator_root(tmp_path):
    operator_root = tmp_path / "live"
    operator_root.mkdir()
    _seed_live_root(operator_root)
    before = _tree(operator_root)
    proc = _run_trends_tests(operator_root, tmp_path / "hermes")
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert _tree(operator_root) == before, "the suite modified the operator's state"


def test_trend_tests_leave_an_empty_operator_root_empty(tmp_path):
    operator_root = tmp_path / "live"
    operator_root.mkdir()
    hermes = tmp_path / "hermes"
    proc = _run_trends_tests(operator_root, hermes)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert list(operator_root.iterdir()) == [], "the suite created files in the operator's project root"
    assert not hermes.exists(), "the suite wrote into the operator's Hermes home"


def test_unconfigured_state_has_no_ambient_root():
    """In-process: with no config, there is no project root to fall back to."""
    import pytest
    from state_db import _db_root
    with pytest.raises(Exception):
        _db_root(None)


def test_default_config_resolves_inside_the_test_sandbox(tmp_path_factory):
    from config_loader import _default_config_path, load_config
    assert tmp_path_factory.getbasetemp().resolve() in _default_config_path().resolve().parents
    assert load_config(quiet=True) is None


def test_hermes_home_resolves_inside_the_test_sandbox(tmp_path_factory):
    from config_loader import get_hermes_home
    assert tmp_path_factory.getbasetemp().resolve() in get_hermes_home().resolve().parents
