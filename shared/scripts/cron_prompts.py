#!/usr/bin/env python3
"""Doctor check: cron prompts that reference plugin paths or skills that no longer exist.

Headless cron jobs carry their own prompt text. After an upgrade moves a
script or renames a skill, those prompts keep pointing at the old location and
the scheduled run breaks silently: ``cron_jobs`` only checks that some job
mentions the briefing, and ``cron_skill_files`` only checks bindings installed
through ``workflows install``.

This reads ``$HERMES_HOME/cron/jobs.json`` and reports stale references. It is
read-only — editing a user's cron job is theirs to do — and it never echoes a
prompt body, only the offending reference.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from doctor_base import CheckResult  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
NAMESPACE = "chief-of-staff:"
CHECK_NAME = "cron_prompts"

_PATH_TOKEN = re.compile(r"[~\w./-]+\.(?:py|sh)(?!\w)")
_SKILL_TOKEN = re.compile(r"chief-of-staff:([a-z0-9][a-z0-9-]*)")
_PRUNE = {".git", ".venv", "venv", "node_modules", "__pycache__", "tests", ".pytest_cache"}
_PLUGIN_TOP_DIRS = {"skills", "shared"}
_CODE_DIRS = ("skills", "skills.local", "shared", ".claude/hooks")


def _script_index(plugin_root: Path) -> dict[str, list[str]]:
    """Map each plugin script basename to its current path(s), relative to the root."""
    index: dict[str, list[str]] = {}
    for top in (plugin_root, *(plugin_root / d for d in _CODE_DIRS)):
        if not top.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            if top == plugin_root:
                dirnames[:] = []  # only the root's own files; code dirs are walked below
            dirnames[:] = sorted(d for d in dirnames if d not in _PRUNE)
            for name in sorted(filenames):
                if name.endswith((".py", ".sh")):
                    rel = (Path(dirpath) / name).relative_to(plugin_root).as_posix()
                    index.setdefault(name, []).append(rel)
    return index


def _skill_dir(plugin_root: Path, name: str) -> Path | None:
    for base in ("skills", "skills.local"):
        path = plugin_root / base / name
        if (path / "SKILL.md").is_file():
            return path
    return None


def _plugin_skills(plugin_root: Path) -> list[str]:
    skills_dir = plugin_root / "skills"
    if not skills_dir.is_dir():
        return []
    return sorted(p.name for p in skills_dir.iterdir() if (p / "SKILL.md").is_file())


def _job_skill_refs(job: dict[str, Any]) -> list[str]:
    declared = job.get("skills")
    refs = [s for s in declared if isinstance(s, str)] if isinstance(declared, list) else []
    single = job.get("skill")
    if isinstance(single, str):
        refs.append(single)
    names = [r[len(NAMESPACE):] for r in refs if r.startswith(NAMESPACE)]
    prompt = job.get("prompt")
    if isinstance(prompt, str):
        names.extend(m.group(1).rstrip("-") for m in _SKILL_TOKEN.finditer(prompt))
    return list(dict.fromkeys(n for n in names if n))


def _skill_hint(name: str, known: list[str]) -> str:
    hint = "not a chief-of-staff plugin skill."
    close = difflib.get_close_matches(name, known, n=1)
    if close:
        hint += f" Did you mean {NAMESPACE}{close[0]}?"
    return hint + f" If it is one of your own agent skills, reference it without the '{NAMESPACE}' prefix."


def _path_finding(token: str, plugin_root: Path, skill_dirs: Iterable[Path],
                  index: dict[str, list[str]]) -> str | None:
    """Return a hint when ``token`` is a stale plugin path, else None."""
    if "/" not in token:
        return None  # a bare script name ("chief_of_staff.py") is not a path
    path = Path(token).expanduser()
    if path.is_absolute():
        if path.exists():
            return None
        ours = plugin_root in path.parents or path.name in index
    else:
        if (plugin_root / path).exists() or any((d / path).exists() for d in skill_dirs):
            return None
        ours = path.name in index or path.parts[0] in _PLUGIN_TOP_DIRS
    if not ours:
        return None  # the user's own scripts are not ours to judge
    current = index.get(path.name)
    if current:
        return "moved; now at " + ", ".join(current)
    return "no such file in the chief-of-staff plugin"


def _label(job: dict[str, Any], position: int) -> str:
    for key in ("name", "id"):
        value = job.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"job #{position + 1}"


def scan_jobs(jobs: list[Any], plugin_root: Path = PLUGIN_ROOT) -> list[dict[str, str]]:
    """Return one finding per stale reference per job: {job, kind, ref, hint}."""
    index = _script_index(plugin_root)
    known = _plugin_skills(plugin_root)
    findings: list[dict[str, str]] = []
    for position, job in enumerate(jobs):
        if not isinstance(job, dict):
            continue
        label = _label(job, position)
        seen: set[tuple[str, str]] = set()
        skill_dirs: list[Path] = []
        for name in _job_skill_refs(job):
            found = _skill_dir(plugin_root, name)
            if found is not None:
                skill_dirs.append(found)
            elif ("skill", name) not in seen:
                seen.add(("skill", name))
                findings.append({"job": label, "kind": "skill", "ref": NAMESPACE + name,
                                 "hint": _skill_hint(name, known)})
        prompt = job.get("prompt")
        if not isinstance(prompt, str):
            continue
        for match in _PATH_TOKEN.finditer(prompt):
            token = match.group(0)
            if ("path", token) in seen:
                continue
            hint = _path_finding(token, plugin_root, skill_dirs, index)
            if hint is not None:
                seen.add(("path", token))
                findings.append({"job": label, "kind": "path", "ref": token, "hint": hint})
    return findings


def _jobs_file() -> Path:
    from config_loader import get_hermes_home
    return get_hermes_home() / "cron" / "jobs.json"


def load_jobs(path: Path) -> list[Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("jobs", [])
    if not isinstance(data, list):
        raise ValueError("expected a list of jobs")
    return data


def check_cron_prompts(fix: bool, data: dict[str, Any] | None, config_path: Path) -> CheckResult:
    del fix, data, config_path  # read-only: never edits a user's cron job
    try:
        path = _jobs_file()
        if not path.is_file():
            return CheckResult(CHECK_NAME, "pass", "no Hermes cron jobs to check")
        jobs = load_jobs(path)
    except Exception as exc:
        return CheckResult(CHECK_NAME, "warn", f"cannot read cron jobs: {type(exc).__name__}: {exc}")
    findings = scan_jobs(jobs)
    if not findings:
        return CheckResult(CHECK_NAME, "pass", f"{len(jobs)} cron job(s) checked; no stale plugin references")
    affected = len({f["job"] for f in findings})
    listed = "; ".join(f"{f['job']}: {f['ref']} — {f['hint']}" for f in findings)
    return CheckResult(
        CHECK_NAME,
        "warn",
        f"{affected} cron job(s) reference stale plugin paths or skills: {listed} "
        "Update those job prompts; doctor does not edit cron jobs.",
    )
