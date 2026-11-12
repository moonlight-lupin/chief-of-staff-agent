"""assistant.name is also the Hermes slash command, so it is restricted.

Rule: start with a letter; then letters, digits, spaces, '-' or '_'; at most
32 characters. Every valid name maps cleanly to a chat command (Telegram
allows a-z, 0-9, '_', 32 characters).

Enforced where a name is set (bootstrap --assistant-name, skill rendering);
an existing invalid name only warns in doctor, and the slash command falls
back to /cos, so an old install keeps working.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "shared" / "scripts"))

from config_loader import validate_assistant_name  # noqa: E402

VALID = ["Ada", "Ada Lovelace", "JARVIS-2", "max_power", "Chief of Staff", "A" * 32]
INVALID = ["", "   ", "Ada!", "Ada & Co", "O'Brien", "小助", "Zoë", "1Ada", "-ada", "_ada", "a" * 33,
           "Ada\nBob", "ada/../x", "Ada 🚀", "Ada.Bot", 'Ada"']


@pytest.mark.parametrize("name", VALID)
def test_valid_names_pass(name):
    assert validate_assistant_name(name) == name.strip()


@pytest.mark.parametrize("name", INVALID)
def test_disallowed_characters_are_refused(name):
    with pytest.raises(ValueError, match="assistant.name"):
        validate_assistant_name(name)


@pytest.mark.parametrize("name", INVALID[2:])
def test_bootstrap_refuses_a_disallowed_name(name):
    import bootstrap_base

    args = argparse.Namespace(config=None, company=None, jurisdiction=None, operator=None, project_root=None,
                              business_type=None, assistant_name=name)
    with pytest.raises(bootstrap_base.BootstrapError, match="assistant.name"):
        bootstrap_base._merge_preset(args)


def test_bootstrap_accepts_a_valid_name():
    import bootstrap_base

    args = argparse.Namespace(config=None, company=None, jurisdiction=None, operator=None, project_root=None,
                              business_type=None, assistant_name="Ada Lovelace")
    assert bootstrap_base._merge_preset(args)["assistant"]["name"] == "Ada Lovelace"


def test_doctor_warns_about_an_existing_disallowed_name(tmp_path):
    import doctor_base

    result = doctor_base._check_assistant_name(False, {"assistant": {"name": "Ada & Co"}}, tmp_path / "c.yaml")
    assert result.status == "warn" and "/cos" in result.detail


@pytest.mark.parametrize("name", INVALID)
def test_a_disallowed_name_never_becomes_a_command(name):
    import hermes_surface

    assert hermes_surface.command_names({"assistant": {"name": name}}) == ["cos"]


@pytest.mark.parametrize("name", VALID)
def test_every_valid_name_is_a_legal_chat_command(name):
    import re

    import hermes_surface

    for command in hermes_surface.command_names({"assistant": {"name": name}}):
        assert re.fullmatch(r"[a-z][a-z0-9_]{0,31}", command)
