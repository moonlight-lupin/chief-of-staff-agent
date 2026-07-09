#!/usr/bin/env python3
"""Tests for v0.1.2 atomic state-store foundation."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from audit_log import append_audit, read_audit  # noqa: E402
from file_lock import try_lock  # noqa: E402
from schemas import SchemaError, validate_store  # noqa: E402
from state_store import load_store, save_store_atomic  # noqa: E402


def cfg(root: Path) -> dict:
    return {"paths": {"project_root": str(root)}, "sales_stages": ["Lead", "Proposal Sent", "Paid"]}


def test_empty_store_returns_correct_template(tmp_path):
    data = load_store("pipeline", config=cfg(tmp_path))
    assert data == {"deals": []}
    assert (tmp_path / "pipeline.yaml").exists()


def test_atomic_write_creates_backup(tmp_path):
    config = cfg(tmp_path)
    before = load_store("pipeline", config=config)
    first = {"deals": [{"id": "deal-1", "client_name": "Acme", "stage": "Lead"}]}
    save_store_atomic("pipeline", first, action="seed", before=before, after=first, config=config)
    second = {"deals": [{"id": "deal-1", "client_name": "Acme", "stage": "Proposal Sent"}]}
    save_store_atomic("pipeline", second, action="move", before=first, after=second, config=config)
    assert yaml.safe_load((tmp_path / "pipeline.yaml").read_text()) == second
    backups = sorted((tmp_path / ".backups").glob("pipeline.*.yaml"))
    assert backups, "expected backup-before-write"
    assert yaml.safe_load(backups[-1].read_text()) == first


def test_audit_log_appended_correctly(tmp_path):
    config = cfg(tmp_path)
    append_audit("pipeline", action="move_stage", before={"stage": "Lead"}, after={"stage": "Paid"}, actor="agent", config=config)
    entries = read_audit("pipeline", limit=10, config=config)
    assert len(entries) == 1
    assert entries[0]["store"] == "pipeline"
    assert entries[0]["action"] == "move_stage"
    assert entries[0]["actor"] == "agent"
    assert entries[0]["before"] == {"stage": "Lead"}


def test_lock_timeout_works(tmp_path):
    target = tmp_path / "pipeline.yaml"
    code = (
        "import sys,time\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from file_lock import with_lock\n"
        f"with with_lock({str(target)!r}):\n"
        "    time.sleep(1.5)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code])
    try:
        time.sleep(0.2)
        assert try_lock(target, timeout=0.2) is False
    finally:
        proc.wait(timeout=5)
    assert try_lock(target, timeout=1) is True


def test_schema_validation_catches_bad_data(tmp_path):
    bad = {"deals": [{"id": "deal-1", "client_name": "Acme", "stage": "Bogus"}]}
    with pytest.raises(SchemaError, match="stage"):
        validate_store("pipeline", bad, config=cfg(tmp_path))
    with pytest.raises(Exception, match="stage"):
        save_store_atomic("pipeline", bad, config=cfg(tmp_path))


def test_concurrent_write_does_not_corrupt(tmp_path):
    config = cfg(tmp_path)
    load_store("pipeline", config=config)
    worker = SCRIPTS / "state_store.py"
    snippets = []
    for i in range(6):
        snippets.append(
            "import sys; "
            f"sys.path.insert(0, {str(SCRIPTS)!r}); "
            "from state_store import save_store_atomic; "
            f"save_store_atomic('pipeline', {{'deals':[{{'id':'deal-{i}','client_name':'Client {i}','stage':'Lead'}}]}}, config={{'paths':{{'project_root':{str(tmp_path)!r}}}, 'sales_stages':['Lead','Proposal Sent','Paid']}})"
        )
    procs = [subprocess.Popen([sys.executable, "-c", code]) for code in snippets]
    for proc in procs:
        assert proc.wait(timeout=10) == 0
    parsed = yaml.safe_load((tmp_path / "pipeline.yaml").read_text())
    validate_store("pipeline", parsed, config=config)
    assert isinstance(parsed["deals"], list)


def test_cli_help_works():
    proc = subprocess.run([sys.executable, str(SCRIPTS / "state_store.py"), "--help"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "--store" in proc.stdout
