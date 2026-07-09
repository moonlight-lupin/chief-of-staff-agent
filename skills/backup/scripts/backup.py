#!/usr/bin/env python3
"""Create and upload Chief of Staff Hermes backups.

The script creates a timestamped tar.gz archive from configured Hermes paths,
uploads it through the google-workspace skill's google_api.py, then prunes old
Drive backups according to weekly/monthly retention settings.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tarfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_EXCLUDES = [".env", "auth.json", "state.db", "sessions/", "logs/"]
DEFAULT_GOOGLE_API = Path("~/.hermes/skills/productivity/google-workspace/scripts/google_api.py").expanduser()


class BackupError(RuntimeError):
    """Raised for user-facing backup failures."""


@dataclass
class BackupResult:
    archive_path: str
    size_bytes: int
    file_count: int
    included_roots: list[str]
    skipped_roots: list[str]
    elapsed_seconds: float


@dataclass
class UploadResult:
    elapsed_seconds: float
    command: list[str]
    response: Any


@dataclass
class PruneResult:
    kept: list[str]
    deleted: list[str]
    skipped: list[str]
    errors: list[str]


def _expand(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _slug(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "company"


def load_config(config_path: str | Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise BackupError("PyYAML is required to read company.yaml.") from exc
    path = Path(config_path).expanduser()
    if not path.exists():
        raise BackupError(f"Config file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise BackupError(f"Config must be a YAML mapping: {path}")
    data.setdefault("_config_path", str(path.resolve()))
    return data


def _hermes_home(config: dict[str, Any]) -> Path:
    explicit = config.get("hermes_home")
    if explicit:
        return _expand(explicit)
    paths = config.get("paths", {}) if isinstance(config.get("paths"), dict) else {}
    project_root = paths.get("project_root")
    if project_root:
        project_path = _expand(project_root)
        if project_path.parent.name == "projects":
            return project_path.parent.parent
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        return _expand(env_home)
    return _expand("~/.hermes")


def _company_slug(config: dict[str, Any]) -> str:
    company = config.get("company", {}) if isinstance(config.get("company"), dict) else {}
    return _slug(str(company.get("slug") or company.get("name") or "company"))


def _project_root(config: dict[str, Any]) -> Path:
    paths = config.get("paths", {}) if isinstance(config.get("paths"), dict) else {}
    if paths.get("project_root"):
        return _expand(paths["project_root"])
    return _hermes_home(config) / "projects" / _company_slug(config)


def _backup_config(config: dict[str, Any]) -> dict[str, Any]:
    backup = config.get("backup", {}) if isinstance(config.get("backup"), dict) else {}
    return {
        "enabled": backup.get("enabled", True),
        "schedule": backup.get("schedule", "0 3 * * 0"),
        "retention_weekly": int(backup.get("retention_weekly", 4)),
        "retention_monthly": int(backup.get("retention_monthly", 12)),
        "drive_folder": backup.get("drive_folder", "09_Backups/"),
        "drive_folder_id": backup.get("drive_folder_id") or backup.get("drive_folder" if str(backup.get("drive_folder", "")).startswith("id:") else "drive_folder_id"),
        "output_dir": backup.get("output_dir", "~/.hermes/backups"),
        "exclude": list(backup.get("exclude", DEFAULT_EXCLUDES) or []),
    }


def _included_paths(config: dict[str, Any]) -> list[Path]:
    hermes_home = _hermes_home(config)
    return [
        hermes_home / "config.yaml",
        hermes_home / "skills",
        _project_root(config),
        hermes_home / "cron",
    ]


def _normalize_excludes(excludes: list[str]) -> list[str]:
    normalized: list[str] = []
    for item in excludes:
        value = str(item).strip()
        if value:
            normalized.append(value)
    return normalized


def _is_excluded(path: Path, root: Path, excludes: list[str]) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path.name
    rel_str = str(rel).replace(os.sep, "/")
    parts = rel_str.split("/")
    for pattern in excludes:
        p = pattern.replace(os.sep, "/")
        p_no_slash = p.rstrip("/")
        if p.endswith("/"):
            if p_no_slash in parts or rel_str.startswith(p):
                return True
        if rel_str == p_no_slash or path.name == p_no_slash:
            return True
        if fnmatch.fnmatch(rel_str, p) or fnmatch.fnmatch(path.name, p):
            return True
    return False


def _iter_files(root: Path, excludes: list[str]) -> tuple[list[Path], list[str]]:
    if not root.exists():
        return [], [str(root)]
    if root.is_file():
        return ([] if _is_excluded(root, root.parent, excludes) else [root]), []
    files: list[Path] = []
    skipped: list[str] = []
    for current, dirs, filenames in os.walk(root):
        current_path = Path(current)
        kept_dirs = []
        for dirname in dirs:
            dir_path = current_path / dirname
            if _is_excluded(dir_path, root, excludes):
                skipped.append(str(dir_path))
            else:
                kept_dirs.append(dirname)
        dirs[:] = kept_dirs
        for filename in filenames:
            file_path = current_path / filename
            if _is_excluded(file_path, root, excludes):
                skipped.append(str(file_path))
            else:
                files.append(file_path)
    return files, skipped


def create_backup(config: dict[str, Any], output_path: str | Path) -> BackupResult:
    """Create a tar.gz backup of configured Hermes/Chief of Staff data."""
    start = time.monotonic()
    output = _expand(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    backup_cfg = _backup_config(config)
    excludes = _normalize_excludes(backup_cfg["exclude"])
    roots = _included_paths(config)
    skipped: list[str] = []
    file_count = 0
    included_roots: list[str] = []

    with tarfile.open(output, "w:gz") as tar:
        for root in roots:
            root = _expand(root)
            files, root_skipped = _iter_files(root, excludes)
            skipped.extend(root_skipped)
            if not root.exists():
                continue
            included_roots.append(str(root))
            if root.is_file() and files:
                arcname = root.relative_to(root.anchor) if root.is_absolute() else root
                tar.add(root, arcname=str(arcname), recursive=False)
                file_count += 1
                continue
            for file_path in files:
                arcname = file_path.relative_to(root.parent)
                tar.add(file_path, arcname=str(arcname), recursive=False)
                file_count += 1

    size = output.stat().st_size
    return BackupResult(
        archive_path=str(output),
        size_bytes=size,
        file_count=file_count,
        included_roots=included_roots,
        skipped_roots=skipped,
        elapsed_seconds=round(time.monotonic() - start, 3),
    )


def _google_identity(config: dict[str, Any]) -> tuple[str, str]:
    google = config.get("google", {}) if isinstance(config.get("google"), dict) else {}
    account = google.get("account") or google.get("service_account_path") or "default"
    delegate = google.get("delegate_email") or google.get("delegate") or google.get("as")
    if not delegate:
        raise BackupError("Missing google.delegate_email in company.yaml; cannot call google_api.py with --as.")
    return str(account), str(delegate)


def _run_google_api(config: dict[str, Any], service: str, command: str, args: list[str]) -> Any:
    script = Path(
        os.environ.get("GOOGLE_WORKSPACE_API")
        or config.get("google_api_script", "")
        or str(DEFAULT_GOOGLE_API)
    ).expanduser()
    if not script.exists():
        raise BackupError(f"google_api.py not found: {script}")
    account, delegate = _google_identity(config)
    cmd = [sys.executable, str(script), "--account", account, "--as", delegate, service, command, *args]
    completed = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise BackupError(
            f"google_api.py {service} {command} failed with exit {completed.returncode}: {completed.stderr.strip() or completed.stdout.strip()}"
        )
    stdout = completed.stdout.strip()
    if not stdout:
        return {"ok": True}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return stdout


def upload_backup(config: dict[str, Any], archive_path: str | Path, drive_folder_id: str) -> UploadResult:
    """Upload archive to Google Drive through google_api.py."""
    start = time.monotonic()
    args = ["--file", str(_expand(archive_path)), "--parent-id", drive_folder_id]
    response = _run_google_api(config, "drive", "upload", args)
    elapsed = round(time.monotonic() - start, 3)
    account, delegate = _google_identity(config)
    command = [
        sys.executable,
        str(DEFAULT_GOOGLE_API),
        "--account",
        account,
        "--as",
        delegate,
        "drive",
        "upload",
        *args,
    ]
    return UploadResult(elapsed_seconds=elapsed, command=command, response=response)


def _parse_drive_items(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, dict):
        if isinstance(response.get("files"), list):
            return [item for item in response["files"] if isinstance(item, dict)]
        if isinstance(response.get("items"), list):
            return [item for item in response["items"] if isinstance(item, dict)]
        if isinstance(response.get("data"), list):
            return [item for item in response["data"] if isinstance(item, dict)]
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    return []


def _item_datetime(item: dict[str, Any]) -> datetime:
    for key in ("createdTime", "modifiedTime", "created", "modified"):
        value = item.get(key)
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
    name = str(item.get("name") or "")
    match = re.search(r"(20\d{6})[-_T]?(\d{6})?", name)
    if match:
        date = match.group(1)
        tm = match.group(2) or "000000"
        try:
            return datetime.strptime(date + tm, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.fromtimestamp(0, tz=timezone.utc)


def _item_id(item: dict[str, Any]) -> Optional[str]:
    value = item.get("id") or item.get("file_id")
    return str(value) if value else None


def prune_old_backups(
    drive_folder_id: str,
    retention_weekly: int,
    retention_monthly: int,
    config: Optional[dict[str, Any]] = None,
    dry_run: bool = False,
) -> PruneResult:
    """Delete old backup archives from Drive according to retention policy.

    The newest N backups are kept as weekly backups. In addition, the newest
    backup per month is kept for the newest M months. All other files matching
    .tar.gz in the backup folder are candidates for deletion.
    """
    if config is None:
        raise BackupError("config is required so prune_old_backups can call google_api.py")
    response = _run_google_api(config, "drive", "list", ["--folder-id", drive_folder_id])
    items = [item for item in _parse_drive_items(response) if str(item.get("name", "")).endswith(".tar.gz")]
    items.sort(key=_item_datetime, reverse=True)

    keep_ids: set[str] = set()
    kept: list[str] = []
    for item in items[: max(0, retention_weekly)]:
        fid = _item_id(item)
        if fid:
            keep_ids.add(fid)
            kept.append(str(item.get("name") or fid))

    months_seen: set[str] = set()
    for item in items:
        dt = _item_datetime(item)
        month_key = dt.strftime("%Y-%m")
        if month_key in months_seen or len(months_seen) >= max(0, retention_monthly):
            continue
        months_seen.add(month_key)
        fid = _item_id(item)
        if fid:
            keep_ids.add(fid)
            name = str(item.get("name") or fid)
            if name not in kept:
                kept.append(name)

    deleted: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for item in items:
        fid = _item_id(item)
        name = str(item.get("name") or fid or "<unknown>")
        if not fid:
            skipped.append(f"{name} (missing file id)")
            continue
        if fid in keep_ids:
            continue
        if dry_run:
            skipped.append(f"{name} (dry-run would delete)")
            continue
        try:
            _run_google_api(config, "drive", "delete", ["--file-id", fid])
            deleted.append(name)
        except BackupError as exc:
            errors.append(f"{name}: {exc}")
    return PruneResult(kept=kept, deleted=deleted, skipped=skipped, errors=errors)


def _default_output_path(config: dict[str, Any]) -> Path:
    backup_cfg = _backup_config(config)
    output_dir = _expand(backup_cfg["output_dir"])
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return output_dir / f"chief-of-staff-{_company_slug(config)}-{timestamp}.tar.gz"


def _resolve_drive_folder_id(config: dict[str, Any]) -> str:
    backup_cfg = _backup_config(config)
    folder_id = backup_cfg.get("drive_folder_id")
    if folder_id and str(folder_id).startswith("id:"):
        folder_id = str(folder_id)[3:]
    if folder_id:
        return str(folder_id)
    drive = config.get("drive", {}) if isinstance(config.get("drive"), dict) else {}
    folders = drive.get("folders", {}) if isinstance(drive.get("folders"), dict) else {}
    for key in ("backups", "backup", "09_Backups"):
        if folders.get(key):
            return str(folders[key])
    raise BackupError(
        "No backup Drive folder ID found. Set backup.drive_folder_id or drive.folders.backups in company.yaml."
    )


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ["B", "KB", "MB", "GB"]:
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a tar.gz backup of Hermes Chief of Staff data and upload it to Google Drive.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", required=True, help="Path to company.yaml")
    parser.add_argument("--output", help="Archive output path; default is timestamped under backup.output_dir")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be backed up/uploaded/pruned without changing anything")
    parser.add_argument("--no-upload", action="store_true", help="Create local archive but skip Drive upload and pruning")
    parser.add_argument("--format", choices=["json", "text"], default="text", help="Report format")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        backup_cfg = _backup_config(config)
        if not backup_cfg["enabled"]:
            raise BackupError("backup.enabled is false in company.yaml")
        output_path = _expand(args.output) if args.output else _default_output_path(config)
        report: dict[str, Any] = {
            "config": str(Path(args.config).expanduser()),
            "dry_run": args.dry_run,
            "included_paths": [str(p) for p in _included_paths(config)],
            "excluded_patterns": backup_cfg["exclude"],
            "archive": None,
            "upload": None,
            "prune": None,
        }

        if args.dry_run:
            # Count files without writing archive.
            excludes = _normalize_excludes(backup_cfg["exclude"])
            total = 0
            skipped: list[str] = []
            for root in _included_paths(config):
                files, root_skipped = _iter_files(_expand(root), excludes)
                total += len(files)
                skipped.extend(root_skipped)
            report["archive"] = {
                "would_create": str(output_path),
                "file_count": total,
                "skipped": skipped,
            }
        else:
            result = create_backup(config, output_path)
            report["archive"] = asdict(result) | {"size_human": _human_size(result.size_bytes)}

        if not args.no_upload and not args.dry_run:
            drive_folder_id = _resolve_drive_folder_id(config)
            upload = upload_backup(config, output_path, drive_folder_id)
            report["upload"] = asdict(upload)
            prune = prune_old_backups(
                drive_folder_id,
                backup_cfg["retention_weekly"],
                backup_cfg["retention_monthly"],
                config=config,
                dry_run=False,
            )
            report["prune"] = asdict(prune)
        elif not args.no_upload and args.dry_run:
            try:
                report["upload"] = {"would_upload_to_folder_id": _resolve_drive_folder_id(config)}
                report["prune"] = {
                    "would_prune": True,
                    "retention_weekly": backup_cfg["retention_weekly"],
                    "retention_monthly": backup_cfg["retention_monthly"],
                }
            except BackupError as exc:
                report["upload"] = {"blocked": str(exc)}

        if args.format == "json":
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            archive = report.get("archive") or {}
            print("Chief of Staff backup report")
            print(f"Dry run: {args.dry_run}")
            if args.dry_run:
                print(f"Would create: {archive.get('would_create')}")
                print(f"File count: {archive.get('file_count')}")
            else:
                print(f"Archive: {archive.get('archive_path')}")
                print(f"Size: {archive.get('size_human')}")
                print(f"File count: {archive.get('file_count')}")
                print(f"Create time: {archive.get('elapsed_seconds')}s")
            if report.get("upload"):
                upload = report["upload"]
                if isinstance(upload, dict) and "elapsed_seconds" in upload:
                    print(f"Upload time: {upload['elapsed_seconds']}s")
                else:
                    print(f"Upload: {upload}")
            if report.get("prune"):
                prune = report["prune"]
                if isinstance(prune, dict):
                    print(f"Pruned: {len(prune.get('deleted', []))}; kept: {len(prune.get('kept', []))}; errors: {len(prune.get('errors', []))}")
        return 0
    except BackupError as exc:
        print(f"backup error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
