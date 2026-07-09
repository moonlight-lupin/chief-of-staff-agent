#!/usr/bin/env python3
"""Scan Google Calendar for today's/tomorrow's Meet-enabled events."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

try:
    from config_loader import load_config  # type: ignore
except Exception as exc:  # pragma: no cover
    print(
        f"Chief-of-Staff bootstrap incomplete: cannot import config_loader from {SHARED_SCRIPTS}: {exc}. "
        "Run plugin bootstrap first.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def google_api_script() -> Path:
    candidates = [
        PLUGIN_ROOT / "shared" / "scripts" / "google_api.py",
        Path.home() / ".hermes" / "skills" / "productivity" / "google-workspace" / "scripts" / "google_api.py",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError("google_api.py not found; install/configure google-workspace skill")


def configure(path: str | None) -> Any:
    if path:
        os.environ["CHIEF_OF_STAFF_CONFIG"] = path
    cfg = load_config(path)
    if cfg is None:
        raise RuntimeError("Could not load company.yaml; pass --config or set CHIEF_OF_STAFF_CONFIG")
    return cfg


def extract_meet_link(event: dict[str, Any]) -> str | None:
    for key in ("hangoutLink", "meet_link", "meetLink"):
        if event.get(key):
            return str(event[key])
    conf = event.get("conferenceData") or {}
    if isinstance(conf, dict):
        for entry in conf.get("entryPoints", []) or []:
            if isinstance(entry, dict) and entry.get("entryPointType") == "video" and entry.get("uri"):
                return str(entry["uri"])
    text = json.dumps(event, default=str)
    import re

    m = re.search(r"https://meet\.google\.com/[a-z0-9-]+", text, re.IGNORECASE)
    return m.group(0) if m else None


def normalize_event(event: dict[str, Any]) -> dict[str, Any] | None:
    link = extract_meet_link(event)
    if not link:
        return None
    attendees = []
    for attendee in event.get("attendees", []) or []:
        if isinstance(attendee, dict):
            attendees.append(attendee.get("email") or attendee.get("displayName") or attendee)
        else:
            attendees.append(attendee)
    start = event.get("start")
    end = event.get("end")
    if isinstance(start, dict):
        start = start.get("dateTime") or start.get("date")
    if isinstance(end, dict):
        end = end.get("dateTime") or end.get("date")
    return {
        "id": event.get("id"),
        "title": event.get("summary") or event.get("title") or "Untitled event",
        "start": start,
        "end": end,
        "meet_link": link,
        "attendees": attendees,
    }


def scan(args: argparse.Namespace) -> list[dict[str, Any]]:
    cfg = configure(args.config)
    script = google_api_script()
    day = date.today() + (timedelta(days=1) if args.tomorrow else timedelta(days=0))
    start = day.isoformat()
    end = (day + timedelta(days=1)).isoformat()
    delegate = str(cfg.get("google", {}).get("delegate_email", ""))
    cmd = [sys.executable, str(script)]
    if delegate:
        cmd.extend(["--as", delegate])
    cmd.extend(["calendar", "list", "--start", start, "--end", end])
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=45)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"google_api.py exited {proc.returncode}")
    loaded = json.loads(proc.stdout or "[]")
    events = loaded if isinstance(loaded, list) else loaded.get("items", []) if isinstance(loaded, dict) else []
    result = []
    for event in events:
        if isinstance(event, dict):
            normalized = normalize_event(event)
            if normalized:
                result.append(normalized)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan Calendar for Google Meet events")
    day = parser.add_mutually_exclusive_group(required=True)
    day.add_argument("--today", action="store_true")
    day.add_argument("--tomorrow", action="store_true")
    parser.add_argument("--config", help="Path to company.yaml (or CHIEF_OF_STAFF_CONFIG)")
    parser.add_argument("--json", action="store_true", default=True, help="Print JSON output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        print(json.dumps(scan(args), indent=2, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        print(f"calendar_scan.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
