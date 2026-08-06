#!/usr/bin/env python3
"""Tests for unified deadline engine."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from deadlines import categorize, compute_all_deadlines, filter_actionable, filter_overdue  # noqa: E402


def sample_config(tmp_path: Path) -> dict:
    today = date.today()
    return {
        "company": {
            "name": "Test Pte Ltd",
            "jurisdiction": "SG",
            "incorporation_date": "2024-01-15",
            "financial_year_end": "31 Dec",
            "currency": "SGD",
        },
        "google": {"service_account_path": "~/missing.json", "domain": "example.com", "delegate_email": "ops@example.com"},
        "paths": {"project_root": str(tmp_path), "wiki_path": str(tmp_path / "wiki"), "templates": str(tmp_path / "templates")},
        "delivery": {"channel": "local", "briefing_time": "08:00", "weekly_review_day": "friday", "weekly_review_time": "17:00", "timezone": "UTC"},
        "deadlines": {
            "custom": [
                {"name": "Tomorrow custom", "due": (today + timedelta(days=1)).isoformat(), "notes": "test"},
                {"name": "Yesterday custom", "due": (today - timedelta(days=1)).isoformat(), "notes": "test"},
            ]
        },
    }


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "company.yaml"
    path.write_text(yaml.safe_dump(sample_config(tmp_path), sort_keys=False), encoding="utf-8")
    return path


def test_sg_statutory_deadlines_computed(tmp_path):
    result = compute_all_deadlines(sample_config(tmp_path))
    statutory = [d for d in result if d["source"] == "statutory"]
    names = {d["name"] for d in statutory}
    assert "Annual Return filing" in names
    assert "Annual General Meeting (AGM)" in names
    assert all("due_date" in d and "days_until" in d and "category" in d for d in statutory)


def test_custom_deadlines_included(tmp_path):
    result = compute_all_deadlines(sample_config(tmp_path))
    names = {d["name"] for d in result}
    assert "Tomorrow custom" in names
    assert "Yesterday custom" in names


def test_categorization_correct():
    today = date.today()
    assert categorize(today - timedelta(days=1)) == "overdue"
    assert categorize(today + timedelta(days=3)) == "within_7"
    assert categorize(today + timedelta(days=20)) == "within_30"
    assert categorize(today + timedelta(days=60)) == "future"


def test_filter_actionable_and_overdue(tmp_path):
    result = compute_all_deadlines(sample_config(tmp_path))
    actionable = filter_actionable(result)
    overdue = filter_overdue(result)
    assert any(d["name"] == "Tomorrow custom" for d in actionable)
    assert any(d["name"] == "Yesterday custom" for d in overdue)
    assert all(d["days_until"] < 0 for d in overdue)


def test_status_done_excluded_from_filters(tmp_path):
    """Deadlines marked status: done must not appear in actionable or overdue lists."""
    today = date.today()
    config = sample_config(tmp_path)
    config["deadlines"]["custom"].append(
        {"name": "Completed RORC", "due": (today - timedelta(days=30)).isoformat(), "status": "done"}
    )
    config["deadlines"]["custom"].append(
        {"name": "Upcoming done", "due": (today + timedelta(days=5)).isoformat(), "status": "Done"}
    )
    result = compute_all_deadlines(config)
    actionable = filter_actionable(result)
    overdue = filter_overdue(result)
    assert not any(d["name"] == "Completed RORC" for d in overdue)
    assert not any(d["name"] == "Upcoming done" for d in actionable)
    assert not any(str(d.get("status", "")).lower() == "done" for d in overdue)
    assert not any(str(d.get("status", "")).lower() == "done" for d in actionable)


def test_cli_within_filter_json_valid(tmp_path):
    config_path = write_config(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "deadlines.py"), "--config", str(config_path), "--within", "30", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    data = json.loads(proc.stdout)
    assert isinstance(data, list)
    assert any(d["name"] == "Tomorrow custom" for d in data)
    assert all(0 <= d["days_until"] <= 30 for d in data)


def test_cli_overdue_filter_json_valid(tmp_path):
    config_path = write_config(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "deadlines.py"), "--config", str(config_path), "--overdue", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    data = json.loads(proc.stdout)
    assert isinstance(data, list)
    assert any(d["name"] == "Yesterday custom" for d in data)
    assert all(d["days_until"] < 0 for d in data)
