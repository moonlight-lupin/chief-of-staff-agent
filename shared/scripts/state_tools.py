#!/usr/bin/env python3
"""Inspect, back up, and repair Chief-of-Staff local state files.

Usage:
    python shared/scripts/state_tools.py inspect
    python shared/scripts/state_tools.py backup --json
    python shared/scripts/state_tools.py repair --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure shared/scripts is importable when run as a standalone script.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from config_loader import get_hermes_home, load_config

STATE_ITEMS: list[dict[str, object]] = [
    {"name": ".events.json", "kind": "file", "json": True, "description": "event store"},
    {"name": ".pending_actions.json", "kind": "file", "json": True, "description": "pending actions queue"},
    {"name": ".email_organisation_policy.json", "kind": "file", "json": True, "description": "email organisation policy"},
    {"name": ".email_organisation_policy.proposal.json", "kind": "file", "json": True, "description": "email organisation policy proposal"},
    {"name": ".email_organisation_classifications.json", "kind": "file", "json": True, "description": "email organisation classifications"},
    {"name": ".email_organisation_suggestions.json", "kind": "file", "json": True, "description": "email organisation suggestions"},
    {"name": ".audit", "kind": "dir", "json": False, "description": "audit logs"},
    {"name": ".runs", "kind": "dir", "json": False, "description": "run logs"},
    {"name": ".webhook_replay_cache.json", "kind": "file", "json": True, "description": "delivery ID replay cache"},
]

JSON_ITEM_NAMES = [str(item["name"]) for item in STATE_ITEMS if item.get("json")]
REQUIRED_DIRECTORIES = [".audit", ".runs"]
REPLAY_CACHE_NAME = ".webhook_replay_cache.json"
PENDING_ACTIONS_NAME = ".pending_actions.json"
REPLAY_STALE_HOURS = 48


def _get_default_project_root_fallback() -> Path:
    """Default project root for fallback paths."""
    env = os.getenv("CHIEF_OF_STAFF_HERMES_HOME") or os.getenv("HERMES_HOME")
    home = Path(env).expanduser() if env else Path.home() / ".hermes"
    return home / "projects" / "default"


def _project_root(config: object) -> Path:
    """Get project root from config, env, or the standard fallback."""
    root: object | None = None
    try:
        paths = config.get("paths", {})  # type: ignore[attr-defined]
        if hasattr(paths, "get"):
            root = paths.get("project_root")  # type: ignore[attr-defined]
    except Exception:
        root = None
    if not root:
        root = os.getenv("CHIEF_OF_STAFF_PROJECT_ROOT", str(_get_default_project_root_fallback()))
    return Path(str(root)).expanduser()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _load_config_for_cli(json_output: bool) -> tuple[object | None, Path | None, int]:
    config = load_config()
    if config is None:
        message = "Failed to load Chief-of-Staff config"
        if json_output:
            print(json.dumps({"ok": False, "error": message}, indent=2))
        else:
            print(message, file=sys.stderr)
        return None, None, 1
    return config, _project_root(config), 0


def _state_path(project_root: Path, item: dict[str, object]) -> Path:
    return project_root / str(item["name"])


def _format_dt_from_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        total = 0
        for child in path.rglob("*"):
            if child.is_file():
                try:
                    total += child.stat().st_size
                except OSError:
                    pass
        return total
    return 0


def _dir_file_count(path: Path) -> int:
    count = 0
    if path.is_dir():
        for child in path.rglob("*"):
            if child.is_file():
                count += 1
    return count


def _load_json_file(path: Path) -> tuple[object | None, str | None]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh), None
    except json.JSONDecodeError as exc:
        return None, f"line {exc.lineno}, column {exc.colno}: {exc.msg}"
    except OSError as exc:
        return None, str(exc)


def _write_json_file(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
        fh.write("\n")
    os.replace(tmp, path)


def _json_entry_count(data: object) -> int | None:
    if isinstance(data, dict):
        for key in ("events", "actions", "entries", "classifications", "suggestions", "items", "labels", "categories"):
            value = data.get(key)
            if isinstance(value, (dict, list)):
                return len(value)
        return len(data)
    if isinstance(data, list):
        return len(data)
    return None


def _inspect_item(project_root: Path, item: dict[str, object]) -> dict[str, object]:
    path = _state_path(project_root, item)
    exists = path.exists()
    summary: dict[str, object] = {
        "name": str(item["name"]),
        "description": str(item.get("description", "")),
        "kind": str(item["kind"]),
        "path": str(path),
        "exists": exists,
        "size_bytes": None,
        "entry_count": None,
        "last_modified": None,
        "json_error": None,
    }
    if not exists:
        return summary

    try:
        summary["size_bytes"] = _path_size(path)
        summary["last_modified"] = _format_dt_from_timestamp(path.stat().st_mtime)
    except OSError as exc:
        summary["json_error"] = str(exc)
        return summary

    if str(item["kind"]) == "dir":
        summary["entry_count"] = _dir_file_count(path)
    elif item.get("json"):
        data, error = _load_json_file(path)
        if error:
            summary["json_error"] = error
        else:
            summary["entry_count"] = _json_entry_count(data)
    return summary


def _inspect(project_root: Path) -> dict[str, object]:
    return {
        "ok": True,
        "project_root": str(project_root),
        "items": [_inspect_item(project_root, item) for item in STATE_ITEMS],
    }


def _safe_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _next_version(value: object) -> int:
    return _safe_int(value, default=0) + 1


def _human_size(value: object) -> str:
    if value is None:
        return "-"
    size = _safe_int(value, default=-1)
    if size < 0:
        return "-"
    units = ["B", "KB", "MB", "GB"]
    amount = float(size)
    idx = 0
    while amount >= 1024 and idx < len(units) - 1:
        amount /= 1024
        idx += 1
    if idx == 0:
        return f"{size} B"
    return f"{amount:.1f} {units[idx]}"


def _print_inspect_human(result: dict[str, object]) -> None:
    print(f"Project root: {result['project_root']}")
    rows = []
    for item in result["items"]:  # type: ignore[index]
        row = item  # type: ignore[assignment]
        exists = "yes" if row["exists"] else "no"  # type: ignore[index]
        entries = row["entry_count"] if row["entry_count"] is not None else "-"  # type: ignore[index]
        modified = row["last_modified"] or "-"  # type: ignore[index]
        notes = row["json_error"] or row["description"]  # type: ignore[index]
        rows.append([
            str(row["name"]),  # type: ignore[index]
            exists,
            _human_size(row["size_bytes"]),  # type: ignore[index]
            str(entries),
            str(modified),
            str(notes),
        ])
    headers = ["Name", "Exists", "Size", "Entries", "Last Modified", "Notes"]
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, value in enumerate(row):
            widths[idx] = max(widths[idx], len(value))
    fmt = "  ".join(f"{{:<{width}}}" for width in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * width for width in widths]))
    for row in rows:
        print(fmt.format(*row))


def _unique_backup_dir(base: Path) -> Path:
    if not base.exists():
        return base
    for idx in range(1, 100):
        candidate = Path(f"{base}-{idx}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not allocate unique backup directory under {base.parent}")


def _backup(project_root: Path) -> dict[str, object]:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = _unique_backup_dir(get_hermes_home() / "backups" / f"state-{timestamp}")
    backup_dir.mkdir(parents=True, exist_ok=False)

    backed_up: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    for item in STATE_ITEMS:
        src = _state_path(project_root, item)
        dst = backup_dir / str(item["name"])
        if not src.exists():
            skipped.append({"name": str(item["name"]), "reason": "missing"})
            continue
        try:
            if src.is_dir():
                shutil.copytree(src, dst, copy_function=shutil.copy2)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            backed_up.append({
                "name": str(item["name"]),
                "kind": str(item["kind"]),
                "source": str(src),
                "destination": str(dst),
                "size_bytes": _path_size(src),
                "file_count": _dir_file_count(src) if src.is_dir() else 1,
            })
        except OSError as exc:
            skipped.append({"name": str(item["name"]), "reason": str(exc)})

    return {
        "ok": True,
        "project_root": str(project_root),
        "backup_dir": str(backup_dir),
        "backed_up": backed_up,
        "skipped": skipped,
    }


def _print_backup_human(result: dict[str, object]) -> None:
    backed_up_obj = result["backed_up"]
    skipped_obj = result["skipped"]
    backed_up = backed_up_obj if isinstance(backed_up_obj, list) else []
    skipped = skipped_obj if isinstance(skipped_obj, list) else []
    print(f"Backup directory: {result['backup_dir']}")
    print(f"Backed up {len(backed_up)} state item(s); skipped {len(skipped)} missing/failed item(s).")
    for item in backed_up:
        if isinstance(item, dict):
            print(f"  ✓ {item['name']} -> {item['destination']} ({_human_size(item['size_bytes'])})")
    for item in skipped:
        if isinstance(item, dict):
            print(f"  - {item['name']}: {item['reason']}")


def _find_malformed_json(project_root: Path) -> list[dict[str, object]]:
    malformed: list[dict[str, object]] = []
    for name in JSON_ITEM_NAMES:
        path = project_root / name
        if not path.exists():
            continue
        _data, error = _load_json_file(path)
        if error:
            malformed.append({"name": name, "path": str(path), "error": error})
    return malformed


def _pending_actions_data(project_root: Path, malformed: list[dict[str, object]]) -> tuple[dict[str, object] | None, Path]:
    path = project_root / PENDING_ACTIONS_NAME
    if not path.exists():
        return None, path
    if any(item.get("name") == PENDING_ACTIONS_NAME for item in malformed):
        return None, path
    data, error = _load_json_file(path)
    if error or not isinstance(data, dict):
        return None, path
    return data, path


def _find_executing_actions(data: dict[str, object] | None, min_age_minutes: int = 0) -> list[dict[str, object]]:
    """Find executing actions, optionally filtered by minimum age.

    Returns list of dicts with id, type, target, summary, executing_at, age_status.
    age_status is one of: 'stale' (older than min_age_minutes),
    'fresh' (younger), 'no_ts' (missing/invalid executing_at).
    """
    if not data:
        return []
    actions = data.get("actions")
    if not isinstance(actions, dict):
        return []
    found: list[dict[str, object]] = []
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(minutes=min_age_minutes) if min_age_minutes > 0 else now
    for action_id, action in actions.items():
        if isinstance(action, dict) and action.get("state") == "executing":
            ts_str = action.get("executing_at")
            age_status = "no_ts"
            if ts_str:
                try:
                    ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    age_status = "stale" if ts < threshold else "fresh"
                except (ValueError, TypeError):
                    age_status = "no_ts"
            found.append({
                "id": str(action.get("id") or action_id),
                "type": str(action.get("type", "")),
                "target": str(action.get("target", "")),
                "summary": str(action.get("summary", "")),
                "executing_at": str(ts_str or ""),
                "age_status": age_status,
            })
    return found


def _reset_executing_actions(data: dict[str, object], path: Path,
                              min_age_minutes: int = 0, force: bool = False) -> list[str]:
    """Reset executing actions to approved. Only resets stale ones unless force=True."""
    actions = data.get("actions")
    if not isinstance(actions, dict):
        return []
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(minutes=min_age_minutes) if min_age_minutes > 0 else now
    reset_ids: list[str] = []
    note = f"Reset from orphaned executing state by state_tools repair at {_now_iso()}; verify before executing."
    for action_id, action in actions.items():
        if isinstance(action, dict) and action.get("state") == "executing":
            if not force:
                ts_str = action.get("executing_at")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts >= threshold:
                            continue  # fresh, skip
                    except (ValueError, TypeError):
                        continue  # invalid timestamp, skip unless force
                else:
                    continue  # no timestamp, skip unless force
            action["state"] = "approved"
            previous = action.get("last_error")
            action["last_error"] = f"{previous}\n{note}" if previous else note
            reset_ids.append(str(action.get("id") or action_id))
    if reset_ids:
        data["_version"] = _next_version(data.get("_version", 0))
        _write_json_file(path, data)
    return reset_ids


def _parse_replay_timestamp(value: object) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _replay_cache_data(project_root: Path, malformed: list[dict[str, object]]) -> tuple[dict[str, object] | None, Path]:
    path = project_root / REPLAY_CACHE_NAME
    if not path.exists():
        return None, path
    if any(item.get("name") == REPLAY_CACHE_NAME for item in malformed):
        return None, path
    data, error = _load_json_file(path)
    if error or not isinstance(data, dict):
        return None, path
    return data, path


def _find_stale_replay_entries(data: dict[str, object] | None) -> list[dict[str, object]]:
    if not data:
        return []
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return []
    cutoff = _now_utc() - timedelta(hours=REPLAY_STALE_HOURS)
    stale: list[dict[str, object]] = []
    for delivery_id, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        ts = _parse_replay_timestamp(entry.get("ts"))
        if ts and ts < cutoff:
            stale.append({
                "delivery_id": str(delivery_id),
                "state": str(entry.get("state", "")),
                "timestamp": ts.isoformat(),
            })
    return stale


def _clean_stale_replay_entries(data: dict[str, object], path: Path, stale: list[dict[str, object]]) -> list[str]:
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return []
    stale_ids = [str(item["delivery_id"]) for item in stale]
    removed: list[str] = []
    for delivery_id in stale_ids:
        if delivery_id in entries:
            del entries[delivery_id]
            removed.append(delivery_id)
    if removed:
        data["_version"] = _next_version(data.get("_version", 0))
        data["entries"] = entries
        _write_json_file(path, data)
    return removed


def _repair(project_root: Path, dry_run: bool, min_executing_minutes: int = 15, force_reset: bool = False) -> dict[str, object]:
    malformed = _find_malformed_json(project_root)
    pending_data, pending_path = _pending_actions_data(project_root, malformed)
    executing_actions = _find_executing_actions(pending_data, min_age_minutes=min_executing_minutes)
    missing_dirs = []
    for name in REQUIRED_DIRECTORIES:
        path = project_root / name
        if not path.exists():
            missing_dirs.append({"name": name, "path": str(path)})
    replay_data, replay_path = _replay_cache_data(project_root, malformed)
    stale_replay = _find_stale_replay_entries(replay_data)

    fixes: dict[str, object] = {
        "reset_actions": [],
        "created_directories": [],
        "removed_replay_cache_entries": [],
        "skipped_malformed_json": malformed,
    }

    if not dry_run:
        if pending_data is not None:
            fixes["reset_actions"] = _reset_executing_actions(
                pending_data, pending_path,
                min_age_minutes=min_executing_minutes, force=force_reset)
        created_dirs: list[str] = []
        for item in missing_dirs:
            path = Path(str(item["path"]))
            path.mkdir(parents=True, exist_ok=True)
            created_dirs.append(str(path))
        fixes["created_directories"] = created_dirs
        if replay_data is not None:
            fixes["removed_replay_cache_entries"] = _clean_stale_replay_entries(replay_data, replay_path, stale_replay)

    # Count only stale + no_ts as issues (fresh executing = not an issue)
    stale_count = sum(1 for a in executing_actions if a.get("age_status") == "stale")
    no_ts_count = sum(1 for a in executing_actions if a.get("age_status") == "no_ts")
    issue_count = len(malformed) + stale_count + no_ts_count + len(missing_dirs) + len(stale_replay)
    return {
        "ok": True,
        "project_root": str(project_root),
        "dry_run": dry_run,
        "issue_count": issue_count,
        "issues": {
            "malformed_json": malformed,
            "executing_actions": executing_actions,
            "missing_directories": missing_dirs,
            "stale_replay_cache_entries": stale_replay,
        },
        "fixes": fixes,
    }


def _print_repair_human(result: dict[str, object], min_executing_minutes: int = 15) -> None:
    dry_run = bool(result["dry_run"])
    verb = "would fix" if dry_run else "fixed"
    print(f"Project root: {result['project_root']}")
    print(f"Repair mode: {'dry-run' if dry_run else 'apply'}")
    print(f"Issues found: {result['issue_count']}")
    issues = result["issues"]  # type: ignore[index]

    malformed = issues["malformed_json"]  # type: ignore[index]
    if malformed:
        print("Malformed JSON files (reported only; not deleted):")
        for item in malformed:
            print(f"  ! {item['name']}: {item['error']}")  # type: ignore[index]

    executing = issues["executing_actions"]  # type: ignore[index]
    if executing:
        stale_items = [a for a in executing if a.get("age_status") == "stale"]
        fresh_items = [a for a in executing if a.get("age_status") == "fresh"]
        no_ts_items = [a for a in executing if a.get("age_status") == "no_ts"]
        if stale_items:
            print(f"Stale executing actions (>{min_executing_minutes}min old, {verb} by resetting to approved):")
            for item in stale_items:
                print(f"  - {item['id']} {item['type']} {item['target']} [{item.get('executing_at', '')}]")
        if fresh_items:
            print(f"Fresh executing actions (still running, skipped):")
            for item in fresh_items:
                print(f"  - {item['id']} {item['type']} {item['target']} [{item.get('executing_at', '')}]")
        if no_ts_items:
            print(f"Executing actions with missing/invalid executing_at (reported, not auto-reset):")
            for item in no_ts_items:
                print(f"  - {item['id']} {item['type']} {item['target']}")
            print("  Use --force-reset-executing to reset these (risk: may still be running)")

    missing_dirs = issues["missing_directories"]  # type: ignore[index]
    if missing_dirs:
        print(f"Missing directories ({verb} by creating):")
        for item in missing_dirs:
            print(f"  - {item['path']}")  # type: ignore[index]

    stale = issues["stale_replay_cache_entries"]  # type: ignore[index]
    if stale:
        print(f"Stale replay cache entries older than {REPLAY_STALE_HOURS}h ({verb} by removing):")
        for item in stale:
            print(f"  - {item['delivery_id']} {item['state']} {item['timestamp']}")  # type: ignore[index]

    if not any([malformed, executing, missing_dirs, stale]):
        print("No repair issues found.")
    if not dry_run:
        fixes = result["fixes"]  # type: ignore[index]
        print("Applied fixes:")
        print(f"  reset_actions: {len(fixes['reset_actions'])}")  # type: ignore[index]
        print(f"  created_directories: {len(fixes['created_directories'])}")  # type: ignore[index]
        print(f"  removed_replay_cache_entries: {len(fixes['removed_replay_cache_entries'])}")  # type: ignore[index]


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", dest="json_output", default=argparse.SUPPRESS,
                        help="Print machine-readable JSON output")

    parser = argparse.ArgumentParser(description="Back up, inspect, and repair Chief-of-Staff local state files")
    parser.add_argument("--json", action="store_true", dest="json_output", default=False,
                        help="Print machine-readable JSON output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("backup", parents=[common], help="Copy existing state files to a timestamped backup directory")
    sub.add_parser("inspect", parents=[common], help="Print a summary table of local state files")
    repair = sub.add_parser("repair", parents=[common], help="Check and repair local state file issues")
    repair.add_argument("--dry-run", action="store_true", help="Report what would be fixed without making changes")
    repair.add_argument("--min-executing-age-minutes", type=int, default=15,
                        help="Minimum age in minutes before an executing action is considered stale (default: 15)")
    repair.add_argument("--force-reset-executing", action="store_true",
                        help="Reset ALL executing actions regardless of age (use with caution: risk of duplicate execution)")
    return parser


def _main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    json_output = bool(args.json_output)

    _config, project_root, rc = _load_config_for_cli(json_output)
    if rc:
        return rc
    if project_root is None:
        return 1

    if args.command == "backup":
        result = _backup(project_root)
        if json_output:
            print(json.dumps(result, indent=2, default=str))
        else:
            _print_backup_human(result)
        return 0

    if args.command == "inspect":
        result = _inspect(project_root)
        if json_output:
            print(json.dumps(result, indent=2, default=str))
        else:
            _print_inspect_human(result)
        return 0

    if args.command == "repair":
        min_min = getattr(args, "min_executing_age_minutes", 15)
        force = bool(getattr(args, "force_reset_executing", False))
        result = _repair(project_root, dry_run=bool(args.dry_run),
                         min_executing_minutes=min_min, force_reset=force)
        if json_output:
            print(json.dumps(result, indent=2, default=str))
        else:
            _print_repair_human(result, min_executing_minutes=min_min)
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
