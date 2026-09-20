#!/usr/bin/env python3
"""Interview → workflow YAML draft. Project-local artifact write only.

propose_workflow maps structured-interview answers onto the workflows.py loader
shape. write_workflow is the only serializer. Neither function calls the review
queue, connectors, or cron — install is a separate ``workflows install`` step.

``now=`` is accepted so callers can inject a frozen clock; it must not appear in
the YAML (identical answers must rewrite a byte-identical file).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

from workflows import (  # noqa: E402
    DESCRIPTION_MAX,
    SIGNAL_KEYS,
    STEP_ALLOWED,
    WorkflowValidationError,
    validate_workflow,
)

_TRIGGER_KEYS = frozenset({"message", "schedule"})


def propose_workflow(
    answers: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None,
    now: Any = None,
) -> dict[str, Any]:
    """Map interview answers to a dict ``validate_workflow`` accepts.

    Invalid answers raise ``WorkflowValidationError``. Does not write files.
    ``config`` and ``now`` are accepted for the caller contract; they do not
    mutate the draft (no timestamps, no connector I/O).
    """
    del config, now
    if not isinstance(answers, Mapping):
        raise WorkflowValidationError("answers: must be a mapping")

    trigger = answers["trigger"] if "trigger" in answers else None
    if not isinstance(trigger, Mapping) or not trigger:
        raise WorkflowValidationError("trigger: required")
    if not any(key in trigger for key in _TRIGGER_KEYS):
        raise WorkflowValidationError("trigger: required")

    steps_in = answers.get("steps")
    if not isinstance(steps_in, list) or not steps_in:
        raise WorkflowValidationError("steps: must be a non-empty list")

    step_ids: list[str] = []
    steps: list[dict[str, Any]] = []
    for raw_step in steps_in:
        step = _map_step(raw_step)
        step_ids.append(str(step.get("id") or ""))
        steps.append(step)

    delivery = _require_delivery_target(answers.get("delivery_target"))
    failure_policy = _require_failure_policy(answers.get("failure_policy"), step_ids)

    draft: dict[str, Any] = {
        "name": answers.get("name"),
        "description": _compose_description(answers),
        "triggers": dict(trigger),
        "steps": steps,
        "delivery": delivery,
    }
    if failure_policy is not None:
        draft["failure_policy"] = failure_policy
    return validate_workflow(draft)


def write_workflow(
    draft: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None,
    *,
    confirm: bool = False,
) -> dict[str, Any]:
    """Serialize a validated draft to ``<project_root>/workflows/<name>.yaml``.

    ``confirm=False`` is a dry-run: return the proposal, write nothing.
    Invalid drafts raise ``WorkflowValidationError`` and never write. The
    destination is derived from the validated kebab-case name only — path
    escapes in ``name`` cannot leave ``project_root``.
    """
    if not isinstance(draft, Mapping):
        raise WorkflowValidationError("workflow: must be a mapping")
    validated = validate_workflow(draft)
    if not confirm:
        return validated

    root = _project_root(config)
    name = validated["name"]
    dest = (root / "workflows" / f"{name}.yaml").resolve()
    if not dest.is_relative_to(root):
        raise WorkflowValidationError("name: must stay within the project root")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_dump_yaml(validated), encoding="utf-8")
    return validated


def _map_step(raw_step: Any) -> dict[str, Any]:
    if not isinstance(raw_step, Mapping):
        raise WorkflowValidationError("steps: each step must be a mapping")
    step = {key: raw_step[key] for key in STEP_ALLOWED if key in raw_step}
    if not any(key in step for key in SIGNAL_KEYS):
        step["manual"] = True
    return step


def _require_delivery_target(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WorkflowValidationError("delivery_target: must be a mapping with channel and target")
    channel = raw.get("channel")
    target = raw.get("target")
    if not isinstance(channel, str) or not channel.strip():
        raise WorkflowValidationError("delivery_target.channel: required non-empty string")
    if not isinstance(target, str) or not target.strip():
        raise WorkflowValidationError("delivery_target.target: required non-empty string")
    delivery = dict(raw)
    delivery["channel"] = channel.strip()
    delivery["target"] = target.strip()
    return delivery


def _require_failure_policy(raw: Any, step_ids: list[str]) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise WorkflowValidationError("failure_policy: must be a mapping")
    step = raw.get("step")
    if step is not None and step not in step_ids:
        raise WorkflowValidationError("failure_policy.step: must name an interview step id")
    return dict(raw)


def _compose_description(answers: Mapping[str, Any]) -> str:
    explicit = answers.get("description")
    if isinstance(explicit, str) and explicit.strip():
        text = explicit.strip()
    else:
        inputs = answers.get("inputs")
        outputs = answers.get("outputs")
        in_text = inputs.strip() if isinstance(inputs, str) else ""
        out_text = outputs.strip() if isinstance(outputs, str) else ""
        if in_text and out_text:
            text = f"{out_text} from {in_text}"
        elif out_text:
            text = out_text
        elif in_text:
            text = in_text
        else:
            name = answers.get("name") or "workflow"
            text = f"Workflow {name}"
    if len(text) > DESCRIPTION_MAX:
        text = text[:DESCRIPTION_MAX].rstrip()
    return text


def _project_root(config: Mapping[str, Any] | None) -> Path:
    if not isinstance(config, Mapping):
        raise WorkflowValidationError("paths.project_root: required")
    paths = config.get("paths")
    if not isinstance(paths, Mapping) or not paths.get("project_root"):
        raise WorkflowValidationError("paths.project_root: required")
    return Path(str(paths["project_root"])).expanduser().resolve()


def _dump_yaml(draft: Mapping[str, Any]) -> str:
    return yaml.safe_dump(
        dict(draft),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )


def _load_json_mapping(path: str) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise WorkflowValidationError(f"{path}: must be a JSON object")
    return raw


def _load_config(path: str | None) -> Mapping[str, Any]:
    from config_loader import load_config

    config = load_config(path)
    if config is None:
        raise SystemExit("could not load company.yaml; pass --config or set CHIEF_OF_STAFF_CONFIG")
    return config


def _emit(payload: Any) -> int:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Propose and write project-local workflow YAML (no install, no cron)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    propose_p = sub.add_parser("propose", help="Map interview answers to a loader-shape draft")
    propose_p.add_argument("--answers-json", required=True, help="Path to interview answers JSON")
    propose_p.add_argument("--config", default=None, help="Path to company.yaml")
    propose_p.add_argument(
        "--write",
        action="store_true",
        help="Also call write_workflow (still requires --confirm to persist)",
    )
    propose_p.add_argument(
        "--confirm",
        action="store_true",
        help="Operator confirmation: persist <project_root>/workflows/<name>.yaml",
    )

    write_p = sub.add_parser("write", help="Write a draft YAML after operator confirmation")
    write_p.add_argument("--draft-json", required=True, help="Path to a loader-shape draft JSON")
    write_p.add_argument("--config", default=None, help="Path to company.yaml")
    write_p.add_argument(
        "--confirm",
        action="store_true",
        help="Operator confirmation: persist <project_root>/workflows/<name>.yaml",
    )

    args = parser.parse_args(argv)
    try:
        config = _load_config(args.config)
        if args.cmd == "propose":
            answers = _load_json_mapping(args.answers_json)
            draft = propose_workflow(answers, config)
            written = False
            path = None
            if args.write:
                draft = write_workflow(draft, config, confirm=args.confirm)
                written = bool(args.confirm)
                if written:
                    path = str(_project_root(config) / "workflows" / f"{draft['name']}.yaml")
            payload: dict[str, Any] = {"draft": draft, "written": written}
            if path:
                payload["path"] = path
            return _emit(payload)

        draft = _load_json_mapping(args.draft_json)
        result = write_workflow(draft, config, confirm=args.confirm)
        payload = {"draft": result, "written": bool(args.confirm)}
        if args.confirm:
            payload["path"] = str(_project_root(config) / "workflows" / f"{result['name']}.yaml")
        return _emit(payload)
    except WorkflowValidationError as exc:
        print(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
