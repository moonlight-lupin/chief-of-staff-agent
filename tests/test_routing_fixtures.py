#!/usr/bin/env python3
"""Routing fixtures under skills/*/evals/routing-fixtures.json stay honest.

The fixtures are specs, not executed against a live model: each pairs a sample
request with the skill that should handle it and the output it must (and must
never) contain. This test keeps them well-formed and pointed at skills this
plugin actually ships — a fixture routing to a skill that does not exist
documents behaviour nobody can get. "none" means no Chief-of-Staff skill: the
agent answers directly.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILLS = PLUGIN_ROOT / "skills"
SHIPPED = {p.name for p in SKILLS.iterdir() if (p / "SKILL.md").exists()}
FIXTURE_FILES = sorted(SKILLS.glob("*/evals/routing-fixtures.json"))


def test_there_are_fixture_files():
    assert FIXTURE_FILES


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.parent.parent.name)
def test_fixture_file_is_well_formed(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["skill_name"] == path.parent.parent.name
    ids = [f["id"] for f in data["fixtures"]]
    assert len(ids) == len(set(ids)), "fixture ids must be unique"
    for fx in data["fixtures"]:
        assert fx["request"].strip()
        routing = fx["expected_routing"]
        assert routing["skill"] in SHIPPED | {"none"}, f"{fx['id']}: routes to unknown skill {routing['skill']!r}"
        assert routing["reason"].strip()
        unknown = set(routing.get("not", [])) - SHIPPED
        assert not unknown, f"{fx['id']}: 'not' lists unknown skills {sorted(unknown)}"
        assert routing["skill"] not in routing.get("not", [])
        assert fx["required_output_fields"]
        for pattern in fx.get("forbidden_patterns", []):
            re.compile(pattern)
