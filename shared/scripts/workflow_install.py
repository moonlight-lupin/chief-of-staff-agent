#!/usr/bin/env python3
"""Install / uninstall a workflow as a skills.local overlay skill (and optional cron).

Also owns the nested ``chief_of_staff.py workflows`` CLI (logs-style parsers).
Writes generated SKILL.md under PLUGIN_ROOT/skills.local/<name>/ — never the
git-tracked skills/ tree, and never the review-queue store.
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

PLUGIN_ROOT = Path(__file__).resolve().parents[2]

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

from cos_helpers import _json_dump, _resolve_project_root, _safe_load_config  # noqa: E402
from workflow_cron import get_cron_binding, install_workflow_cron, uninstall_workflow_cron  # noqa: E402
from workflows import generate_skill_md, validate_workflow  # noqa: E402


class WorkflowInstallError(Exception):
    """Refused install/uninstall (missing YAML, invalid schema, not installed)."""


def _project_root(config: Mapping[str, Any] | None) -> Path:
    if isinstance(config, Mapping):
        paths = config.get("paths")
        if isinstance(paths, Mapping) and paths.get("project_root"):
            return Path(str(paths["project_root"])).expanduser()
    raise WorkflowInstallError("project_root is not configured")


def _overlay_dir(name: str) -> Path:
    return PLUGIN_ROOT / "skills.local" / name


def _overlay_skill(name: str) -> Path:
    return _overlay_dir(name) / "SKILL.md"


def _yaml_path(config: Mapping[str, Any] | None, name: str) -> Path:
    return _project_root(config) / "workflows" / f"{name}.yaml"


def _has_schedule(workflow: Mapping[str, Any]) -> bool:
    triggers = workflow.get("triggers")
    if not isinstance(triggers, Mapping):
        return False
    schedule = triggers.get("schedule")
    return isinstance(schedule, Mapping) and bool(str(schedule.get("cron") or "").strip())


def install_workflow(
    name: str,
    *,
    config: Mapping[str, Any] | None,
    session_id: str = "",
    now: Any = None,
) -> dict[str, Any]:
    """Validate YAML, write skills.local overlay SKILL.md, install cron if scheduled."""
    if not isinstance(name, str) or not name.strip():
        raise WorkflowInstallError("name: must be a non-empty string")
    name = name.strip()
    if yaml is None:
        raise WorkflowInstallError("PyYAML is required to install a workflow")
    path = _yaml_path(config, name)
    if not path.is_file():
        raise WorkflowInstallError(f"workflow YAML not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise WorkflowInstallError(str(exc)) from exc
    workflow = validate_workflow(raw)
    if workflow["name"] != name:
        raise WorkflowInstallError(
            f"workflow name {workflow['name']!r} does not match install name {name!r}"
        )
    markdown = generate_skill_md(workflow)
    skill_path = _overlay_skill(name)
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(markdown, encoding="utf-8")
    cron_installed = False
    if _has_schedule(workflow):
        install_workflow_cron(
            name,
            workflow,
            config,
            now=now,
            session_id=session_id,
        )
        cron_installed = True
    return {
        "name": name,
        "skill_path": str(skill_path),
        "cron_installed": cron_installed,
    }


def uninstall_workflow(
    name: str,
    *,
    config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Remove skills.local/<name>/ and any cron binding. Missing install is an error."""
    if not isinstance(name, str) or not name.strip():
        raise WorkflowInstallError("name: must be a non-empty string")
    name = name.strip()
    overlay = _overlay_dir(name)
    had_overlay = overlay.is_dir()
    had_cron = get_cron_binding(name, config=config) is not None
    if not had_overlay and not had_cron:
        raise WorkflowInstallError(f"workflow {name} is not installed")
    if had_overlay:
        shutil.rmtree(overlay)
    if had_cron:
        uninstall_workflow_cron(name, config)
    return {"name": name, "removed": True}


def _cli_error(exc: BaseException) -> int:
    print(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False))
    return 1


def _emit(payload: Any, args: argparse.Namespace) -> int:
    del args
    print(_json_dump(payload))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    if yaml is None:
        return _cli_error(WorkflowInstallError("PyYAML is required to list workflows"))
    config = _safe_load_config(getattr(args, "config", None))
    root = _resolve_project_root(config)
    rows: list[dict[str, Any]] = []
    wf_dir = (root / "workflows") if root is not None else None
    if wf_dir is not None and wf_dir.is_dir():
        for path in sorted(wf_dir.glob("*.yaml")):
            name = path.stem
            error: str | None = None
            valid = False
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(raw, Mapping) and raw.get("name"):
                    name = str(raw["name"])
                workflow = validate_workflow(raw)
                name = str(workflow["name"])
                valid = True
            except Exception as exc:
                error = str(exc)
            row: dict[str, Any] = {"name": name, "valid": valid}
            if error:
                row["error"] = error
            rows.append(row)
    payload = {"workflows": rows}
    if getattr(args, "summary", False):
        if not rows:
            print("No workflows")
            return 0
        print("NAME                              VALID")
        print("--------------------------------  -----")
        for row in rows:
            flag = "yes" if row.get("valid") else "no"
            print(f"{str(row.get('name') or ''):<32}  {flag}")
        return 0
    print(_json_dump(payload))
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    import workflow_runs
    return workflow_runs.cmd_runs(args)


def cmd_start(args: argparse.Namespace) -> int:
    import workflow_runs
    if not hasattr(args, "trigger"):
        args.trigger = "message"
    return workflow_runs.cmd_start(args)


def cmd_advance(args: argparse.Namespace) -> int:
    import workflow_runs
    config = _safe_load_config(getattr(args, "config", None))
    run_id = str(getattr(args, "run_id", "") or "")
    try:
        run = workflow_runs.get_run(run_id, config=config)
        if run is None:
            raise workflow_runs.WorkflowRunError(f"run not found: {run_id}")
        index = int(run.get("current_step_index") or 0)
        result = workflow_runs.advance_run(run_id, index, config=config)
    except Exception as exc:
        return _cli_error(exc)
    return _emit(result, args)


def _advanced_step_ids(before: Mapping[str, Any], after: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(after, Mapping):
        return []
    before_status = list(before.get("step_status") or [])
    after_status = list(after.get("step_status") or [])
    definition = after.get("definition") or before.get("definition") or {}
    steps = definition.get("steps") if isinstance(definition, Mapping) else []
    if not isinstance(steps, list):
        return []
    advanced: list[str] = []
    for index, step in enumerate(steps):
        prev = before_status[index] if index < len(before_status) else None
        nxt = after_status[index] if index < len(after_status) else None
        if prev == nxt or nxt not in ("completed", "skipped"):
            continue
        if isinstance(step, Mapping) and step.get("id"):
            advanced.append(str(step["id"]))
    return advanced


def cmd_sync(args: argparse.Namespace) -> int:
    import workflow_hooks
    import workflow_runs
    config = _safe_load_config(getattr(args, "config", None))
    workflow_name = str(getattr(args, "workflow", "") or "")
    active = None
    for run in workflow_runs.list_runs(config=config):
        if run.get("workflow_name") == workflow_name and run.get("state") in workflow_runs.ACTIVE_STATES:
            active = run
            break
    observed_at = datetime.now(timezone.utc).isoformat()
    if active is None:
        return _emit({"run_id": None, "advanced": [], "observed_at": observed_at}, args)
    run_id = str(active.get("workflow_run_id") or "")
    try:
        updated = workflow_hooks.observe_and_advance(run_id, config)
    except Exception as exc:
        return _cli_error(exc)
    after = updated if isinstance(updated, Mapping) else workflow_runs.get_run(run_id, config=config)
    payload = {
        "run_id": run_id,
        "advanced": _advanced_step_ids(active, after),
        "observed_at": observed_at,
    }
    return _emit(payload, args)


def cmd_resume(args: argparse.Namespace) -> int:
    import workflow_runs
    return workflow_runs.cmd_resume(args)


def cmd_abort(args: argparse.Namespace) -> int:
    import workflow_runs
    return workflow_runs.cmd_abort(args)


def cmd_install(args: argparse.Namespace) -> int:
    try:
        config = _safe_load_config(getattr(args, "config", None))
        result = install_workflow(
            str(args.name),
            config=config,
            session_id=str(getattr(args, "session_id", "") or ""),
        )
    except Exception as exc:
        return _cli_error(exc)
    return _emit(result, args)


def cmd_uninstall(args: argparse.Namespace) -> int:
    try:
        config = _safe_load_config(getattr(args, "config", None))
        result = uninstall_workflow(str(args.name), config=config)
    except Exception as exc:
        return _cli_error(exc)
    return _emit(result, args)


def _add_summary(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--summary", action="store_true", help="Human-readable table instead of JSON")


def add_workflows_parser(sub: argparse._SubParsersAction) -> None:
    """Attach nested ``workflows`` verbs to the chief_of_staff root parser."""
    workflows = sub.add_parser("workflows", help="Workflow orchestrator (runs, install, sync)")
    wf_sub = workflows.add_subparsers(dest="workflows_command", required=True)

    wf_list = wf_sub.add_parser("list", help="List project workflow YAML files (JSON default)")
    _add_summary(wf_list)
    wf_list.set_defaults(func=cmd_list)

    wf_runs = wf_sub.add_parser("runs", help="List workflow runs (JSON default)")
    _add_summary(wf_runs)
    wf_runs.set_defaults(func=cmd_runs)

    wf_start = wf_sub.add_parser("start", help="Start a run from a validated workflow YAML file")
    wf_start.add_argument("--file", required=True, help="Path to workflow YAML")
    wf_start.add_argument("--session-id", required=True, help="Owning session id")
    wf_start.add_argument("--trigger", default="message", help="Trigger source (message or cron)")
    _add_summary(wf_start)
    wf_start.set_defaults(func=cmd_start)

    wf_advance = wf_sub.add_parser("advance", help="Manually complete the current step of a run")
    wf_advance.add_argument("--run-id", required=True, help="workflow_run_id to advance")
    _add_summary(wf_advance)
    wf_advance.set_defaults(func=cmd_advance)

    wf_sync = wf_sub.add_parser("sync", help="Re-observe the active run for a workflow")
    wf_sync.add_argument("--workflow", required=True, help="Workflow name whose active run to sync")
    _add_summary(wf_sync)
    wf_sync.set_defaults(func=cmd_sync)

    wf_resume = wf_sub.add_parser("resume", help="Rebind the owning session and resume a run")
    wf_resume.add_argument("--run-id", required=True, help="workflow_run_id to resume")
    wf_resume.add_argument("--session-id", required=True, help="New owning session id")
    _add_summary(wf_resume)
    wf_resume.set_defaults(func=cmd_resume)

    wf_abort = wf_sub.add_parser("abort", help="Abort an active run")
    wf_abort.add_argument("--run-id", required=True, help="workflow_run_id to abort")
    _add_summary(wf_abort)
    wf_abort.set_defaults(func=cmd_abort)

    wf_install = wf_sub.add_parser("install", help="Validate YAML, write skills.local overlay, optional cron")
    wf_install.add_argument("name", help="Workflow name (workflows/<name>.yaml)")
    wf_install.add_argument("--session-id", default="", help="Session id for scheduled cron install")
    _add_summary(wf_install)
    wf_install.set_defaults(func=cmd_install)

    wf_uninstall = wf_sub.add_parser("uninstall", help="Remove skills.local overlay and cron binding")
    wf_uninstall.add_argument("name", help="Workflow name to uninstall")
    _add_summary(wf_uninstall)
    wf_uninstall.set_defaults(func=cmd_uninstall)
