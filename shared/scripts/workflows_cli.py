"""Workflows CLI wiring — optional attachment of the `workflows` command.

Extracted from chief_of_staff.py so the CLI god-file stays under the
decomposition contract (<2500 lines). The wiring is fail-soft: import or
registration failures produce a stderr diagnostic and never raise.
"""

from __future__ import annotations

import argparse
import sys


def attach_workflows_command(sub: argparse._SubParsersAction) -> None:
    """Attach the `workflows` subcommand tree, fail-soft with diagnostics."""
    try:
        from workflow_install import add_workflows_parser
    except Exception as exc:
        print(
            f"Warning: workflow_install unavailable ({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
        return
    try:
        add_workflows_parser(sub)
    except Exception as exc:
        _drop_subparser(sub, "workflows")
        print(
            f"Warning: workflows command registration failed ({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )


def _drop_subparser(sub: argparse._SubParsersAction, name: str) -> None:
    """Remove a partially registered nested command so a failed attach cannot linger."""
    name_map = getattr(sub, "_name_parser_map", None)
    if isinstance(name_map, dict):
        name_map.pop(name, None)
    choices = getattr(sub, "choices", None)
    if isinstance(choices, dict):
        choices.pop(name, None)
    actions = getattr(sub, "_choices_actions", None)
    if isinstance(actions, list):
        sub._choices_actions = [
            action for action in actions if getattr(action, "dest", None) != name
        ]