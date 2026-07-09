#!/usr/bin/env python3
"""Install chief-of-staff cron jobs.

Usage:
    python install_cron.py --config company.yaml --dry-run
    python install_cron.py --config company.yaml --install
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    raise RuntimeError("PyYAML is required for install_cron.py") from exc


class CronInstallError(RuntimeError):
    """Raised when cron jobs cannot be generated or installed."""


DAYS = {
    "monday": 1, "mon": 1,
    "tuesday": 2, "tue": 2,
    "wednesday": 3, "wed": 3,
    "thursday": 4, "thu": 4,
    "friday": 5, "fri": 5,
    "saturday": 6, "sat": 6,
    "sunday": 0, "sun": 0,
}


def _load_config(path: str) -> dict[str, Any]:
    p = Path(path).expanduser()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise CronInstallError(f"Config must be a mapping: {p}")
    return data


def _time_to_cron(value: str, day: int | None = None) -> str:
    parts = str(value or "08:00").strip().split(":")
    if len(parts) < 2:
        raise CronInstallError(f"Invalid HH:MM time: {value!r}")
    hour = int(parts[0])
    minute = int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise CronInstallError(f"Invalid HH:MM time: {value!r}")
    dow = "*" if day is None else str(day)
    return f"{minute} {hour} * * {dow}"


def _prompt(task: str, config_path: Path) -> str:
    return (
        f"Run the Chief-of-Staff {task} workflow. This is a scheduled run; do not rely on conversation history. "
        f"Use configuration at {config_path}. Load the relevant chief-of-staff plugin skill, read current YAML state from the configured project root, "
        "perform only the workflow requested here, record idempotency in .runs, audit any state mutations, and deliver a concise result."
    )


def build_jobs(config_path: str) -> list[dict[str, Any]]:
    path = Path(config_path).expanduser().resolve()
    cfg = _load_config(str(path))
    delivery = cfg.get("delivery", {}) if isinstance(cfg.get("delivery"), Mapping) else {}
    backup = cfg.get("backup", {}) if isinstance(cfg.get("backup"), Mapping) else {}
    channel = str(delivery.get("channel") or "origin")
    if channel == "telegram" and delivery.get("chat_id"):
        deliver = f"telegram:{delivery['chat_id']}"
    else:
        deliver = channel
    weekly_day = DAYS.get(str(delivery.get("weekly_review_day") or "friday").lower(), 5)
    jobs = [
        {
            "name": "Chief-of-Staff Daily Briefing",
            "schedule": _time_to_cron(str(delivery.get("briefing_time") or "08:00")),
            "prompt": _prompt("daily briefing", path),
            "skills": ["chief-of-staff:daily-briefing"],
            "deliver": deliver,
        },
        {
            "name": "Chief-of-Staff Weekly Review",
            "schedule": _time_to_cron(str(delivery.get("weekly_review_time") or "17:00"), weekly_day),
            "prompt": _prompt("weekly review", path),
            "skills": ["chief-of-staff:weekly-review"],
            "deliver": deliver,
        },
        {
            "name": "Chief-of-Staff Deadline Scan",
            "schedule": "0 9 * * 5",
            "prompt": _prompt("deadline tracker scan", path),
            "skills": ["chief-of-staff:deadline-tracker"],
            "deliver": deliver,
        },
        {
            "name": "Chief-of-Staff Backup",
            "schedule": str(backup.get("schedule") or "0 3 * * 0"),
            "prompt": _prompt("backup", path),
            "skills": ["chief-of-staff:backup"],
            "deliver": deliver,
        },
        {
            "name": "Chief-of-Staff Calendar Scan",
            "schedule": "0 6 * * *",
            "prompt": _prompt("calendar scan for meeting reminders", path),
            "skills": ["chief-of-staff:calendar-manager", "chief-of-staff:meeting-prep"],
            "deliver": deliver,
        },
    ]
    timezone = delivery.get("timezone")
    for job in jobs:
        job["timezone"] = timezone or "local"
    return jobs


def install_job(job: Mapping[str, Any]) -> dict[str, Any]:
    cmd = ["hermes", "cron", "create", str(job["schedule"]), str(job["prompt"]), "--name", str(job["name"]), "--deliver", str(job["deliver"])]
    for skill in job.get("skills", []):
        cmd.extend(["--skill", str(skill)])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    return {"job": job["name"], "command": cmd, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install Chief-of-Staff cron jobs from company.yaml")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config" / "company.yaml"), help="Path to company.yaml")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Print jobs without creating them")
    mode.add_argument("--install", action="store_true", help="Create jobs via hermes cron create")
    parser.add_argument("--json", action="store_true", help="Print JSON")
    args = parser.parse_args(argv)
    jobs = build_jobs(args.config)
    if args.dry_run:
        if args.json:
            print(json.dumps(jobs, indent=2))
        else:
            for job in jobs:
                print(f"{job['name']}: {job['schedule']} ({job['timezone']}) -> {job['deliver']}")
                print(f"  skills: {', '.join(job['skills'])}")
                print(f"  prompt: {job['prompt']}")
        return 0
    results = [install_job(job) for job in jobs]
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for result in results:
            status = "OK" if result["returncode"] == 0 else "FAILED"
            print(f"{status}: {result['job']}")
            if result["stdout"]:
                print(result["stdout"].strip())
            if result["stderr"]:
                print(result["stderr"].strip())
    return 1 if any(r["returncode"] != 0 for r in results) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
