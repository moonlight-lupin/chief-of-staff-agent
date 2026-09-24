#!/usr/bin/env python3
"""Git-backed state sync — keep ``project_root`` alive across cloud sessions.

A hosted Claude Code session runs on an ephemeral VM: anything under
``paths.project_root`` is lost at teardown. When project_root is a clone of a
*private* data repository, this module makes it durable:

    chief_of_staff.py sync status     # git-backed? dirty? ahead of the remote?
    chief_of_staff.py sync pull       # fast-forward only
    chief_of_staff.py sync push       # checkpoint state.db, commit, push

Guarantees, each with a test in ``tests/test_state_sync.py``:

* Data never goes to the plugin repository (which may be public): a root inside
  the plugin checkout, or a data remote equal to the plugin's own remote, is
  refused.
* ``.env`` and SQLite ``-wal``/``-shm`` sidecars are never committed, and a
  data repo that already tracks ``.env`` is refused outright.
* ``state.db`` is WAL-checkpointed before commit so the committed file holds
  every committed transaction.
* Pull is fast-forward only. A dirty or diverged tree is refused, never merged —
  ``state.db`` is binary and a textual merge would corrupt it.
* Credentials embedded in a remote URL are never echoed.

Stdlib only, so the Stop hook can run it with any Python 3.11+.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[2]

# Always excluded from the data repo. Secrets live in the plugin-root .env and
# must never be copied here; the sidecars are transient and checkpointed away.
# .runs/ holds per-run operational logs: rewritten on every command, so tracking
# it would leave the tree permanently dirty.
GITIGNORE_ENTRIES = (".env", ".env.*", "*.db-wal", "*.db-shm", "*.db-journal", "__pycache__/", ".runs/")

_FALLBACK_IDENTITY = ("-c", "user.name=Chief of Staff", "-c", "user.email=chief-of-staff@localhost")
_REMEDY = "Run: .venv/bin/python shared/scripts/chief_of_staff.py sync push"


class SyncError(Exception):
    """A refused or failed sync operation. The message is safe to show."""


# ─── git plumbing ────────────────────────────────────────────────────────────

def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and proc.returncode != 0:
        detail = redact_url((proc.stderr or proc.stdout).strip().splitlines()[-1:] or [""])
        raise SyncError(f"git {args[0]} failed: {detail}")
    return proc


def _toplevel(path: Path) -> Path | None:
    if not path.is_dir():
        return None
    proc = _git(path, "rev-parse", "--show-toplevel", check=False)
    return Path(proc.stdout.strip()).resolve() if proc.returncode == 0 else None


def _origin(root: Path) -> str:
    proc = _git(root, "remote", "get-url", "origin", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _branch(root: Path) -> str:
    return _git(root, "symbolic-ref", "--short", "HEAD", check=False).stdout.strip() or "main"


def _has_commits(root: Path) -> bool:
    return _git(root, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0


def _dirty(root: Path) -> list[str]:
    out = _git(root, "status", "--porcelain", "--untracked-files=all").stdout
    return [line[3:] for line in out.splitlines() if line.strip()]


def _ahead_behind(root: Path) -> tuple[int, int] | None:
    """Commits (ahead, behind) of the upstream, or None when there is none."""
    upstream = f"origin/{_branch(root)}"
    if _git(root, "rev-parse", "--verify", "-q", upstream, check=False).returncode != 0:
        return None
    out = _git(root, "rev-list", "--left-right", "--count", f"HEAD...{upstream}", check=False).stdout.split()
    return (int(out[0]), int(out[1])) if len(out) == 2 else None


def redact_url(value: Any) -> str:
    """Strip ``user:token@`` credentials from any URL inside ``value``."""
    text = " ".join(value) if isinstance(value, list) else str(value)
    return re.sub(r"(\w+://)[^/@\s]+@", r"\1", text)


def _normalise_remote(url: str) -> str:
    url = redact_url(url).strip().lower().rstrip("/")
    url = re.sub(r"\.git$", "", url)
    url = re.sub(r"^git@([^:]+):", r"https://\1/", url)
    return re.sub(r"^\w+://", "", url)


# ─── guards ──────────────────────────────────────────────────────────────────

def _require_repo(root: Path) -> Path:
    top = _toplevel(root)
    if top is None:
        raise SyncError(
            f"{root} is not a git repository. Clone your private data repo there "
            "(see docs/CLAUDE_CODE.md) before syncing."
        )
    plugin_top = _toplevel(Path(PLUGIN_ROOT))
    if plugin_top is not None and top == plugin_top:
        raise SyncError(
            "project_root is inside the plugin checkout; syncing would commit your "
            "data into the plugin repository. Use a separate private data repo."
        )
    plugin_origin = _origin(plugin_top) if plugin_top is not None else ""
    if plugin_origin and _normalise_remote(plugin_origin) == _normalise_remote(_origin(top)):
        raise SyncError(
            "The data repo's remote is the plugin repository itself. Point it at a "
            "separate private repo before syncing."
        )
    return top


def _ensure_gitignore(root: Path) -> None:
    path = root / ".gitignore"
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    missing = [e for e in GITIGNORE_ENTRIES if e not in existing]
    if missing:
        body = "\n".join(existing + missing).strip("\n") + "\n"
        path.write_text(body, encoding="utf-8")


def _refuse_tracked_secrets(root: Path) -> None:
    tracked = _git(root, "ls-files").stdout.splitlines()
    leaked = [f for f in tracked if Path(f).name == ".env" or Path(f).name.startswith(".env.")]
    if leaked:
        raise SyncError(
            f"The data repo already tracks secrets ({', '.join(leaked)}). Remove them "
            "from history and rotate the credentials before syncing again."
        )


def _checkpoint_databases(root: Path) -> list[str]:
    """Fold WAL into every *.db and return warnings about in-flight actions."""
    warnings: list[str] = []
    for db in sorted(root.rglob("*.db")):
        if ".git" in db.parts:
            continue
        try:
            conn = sqlite3.connect(str(db), timeout=10)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                has_actions = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_actions'"
                ).fetchone()
                if has_actions:
                    (n,) = conn.execute(
                        "SELECT COUNT(*) FROM pending_actions WHERE state = 'executing'"
                    ).fetchone()
                    if n:
                        warnings.append(
                            f"{n} action(s) still executing in {db.name}: close each with "
                            "review_queue.py record-execution before the session ends."
                        )
            finally:
                conn.close()
        except sqlite3.Error as exc:
            warnings.append(f"could not checkpoint {db.name}: {exc}")
    return warnings


# ─── public API ──────────────────────────────────────────────────────────────

def sync_status(root: Path | str) -> dict[str, Any]:
    root = Path(root).expanduser()
    top = _toplevel(root)
    if top is None:
        return {
            "project_root": str(root),
            "git_backed": False,
            "has_remote": False,
            "remote": "",
            "dirty": [],
            "ahead": 0,
            "behind": 0,
            "sync_refusal": "",
            "note": "project_root is not a git repository; state is not synced anywhere.",
        }
    remote = _origin(top)
    counts = _ahead_behind(top) if _has_commits(top) else None
    dirty = _dirty(top)
    ahead, behind = counts or (0, 0)
    if counts is None and _has_commits(top):
        ahead = int(_git(top, "rev-list", "--count", "HEAD").stdout.strip() or 0)
    in_sync = not dirty and ahead == 0
    try:
        _require_repo(top)
        refusal = ""
    except SyncError as exc:
        refusal = str(exc)
    return {
        "project_root": str(root),
        "git_backed": True,
        "has_remote": bool(remote),
        "remote": redact_url(remote),
        "branch": _branch(top),
        "dirty": dirty,
        "ahead": ahead,
        "behind": behind,
        "sync_refusal": refusal,
        "note": refusal or ("in sync with the remote." if in_sync else f"unsynced changes. {_REMEDY}"),
    }


def sync_pull(root: Path | str) -> dict[str, Any]:
    top = _require_repo(Path(root).expanduser())
    if not _origin(top):
        raise SyncError("The data repo has no 'origin' remote to pull from.")
    if _dirty(top):
        raise SyncError("The data repo has uncommitted changes; run `sync push` first, then pull.")
    branch = _branch(top)
    _git(top, "fetch", "origin")
    if _git(top, "rev-parse", "--verify", "-q", f"origin/{branch}", check=False).returncode != 0:
        return {"updated": False, "note": "remote has no history yet."}
    before = _git(top, "rev-parse", "HEAD", check=False).stdout.strip()
    if not _has_commits(top):
        _git(top, "reset", "--hard", f"origin/{branch}")
    else:
        counts = _ahead_behind(top)
        if counts and counts[0] and counts[1]:
            raise SyncError(
                f"Local and remote history have diverged ({counts[0]} local, {counts[1]} "
                "remote commit(s)). Resolve by hand; state.db cannot be merged."
            )
        _git(top, "merge", "--ff-only", f"origin/{branch}")
    after = _git(top, "rev-parse", "HEAD").stdout.strip()
    return {"updated": before != after, "head": after[:12]}


def sync_push(root: Path | str, message: str | None = None) -> dict[str, Any]:
    top = _require_repo(Path(root).expanduser())
    _refuse_tracked_secrets(top)
    if not _origin(top):
        raise SyncError("The data repo has no 'origin' remote to push to.")
    _ensure_gitignore(top)
    warnings = _checkpoint_databases(top)

    committed = False
    _git(top, "add", "-A")
    if _git(top, "diff", "--cached", "--quiet", check=False).returncode != 0:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        msg = message or f"state: sync {stamp}"
        identity = () if _git(top, "config", "user.email", check=False).stdout.strip() else _FALLBACK_IDENTITY
        _git(top, *identity, "commit", "-q", "-m", msg)
        committed = True

    counts = _ahead_behind(top)
    pushed = False
    if counts is None or counts[0]:
        _git(top, "push", "-u", "origin", f"HEAD:{_branch(top)}")
        pushed = True
    return {"committed": committed, "pushed": pushed, "warnings": warnings}


def stop_check(root: Path | str) -> tuple[int, str]:
    """(exit code, message) for the Stop hook: 2 blocks the stop with a remedy."""
    try:
        status = sync_status(root)
    except SyncError:
        return 0, ""
    if not status["git_backed"] or not status["has_remote"] or status["sync_refusal"]:
        return 0, ""
    if status["dirty"] or status["ahead"]:
        return 2, (
            "Chief-of-Staff state has changes that are not in the data repo yet "
            f"({len(status['dirty'])} uncommitted file(s), {status['ahead']} unpushed "
            f"commit(s)). This cloud VM is ephemeral. {_REMEDY}"
        )
    return 0, ""


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _resolve_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.getenv("CHIEF_OF_STAFF_PROJECT_ROOT")
    if env:
        return Path(env).expanduser()
    try:
        from config_loader import get_project_root, load_config

        root = get_project_root(load_config(quiet=True))
        if root is not None:
            return root
    except Exception:
        pass
    raise SyncError("No project root: pass --project-root or set paths.project_root.")


def cmd_sync(args: argparse.Namespace) -> int:
    try:
        root = _resolve_root(getattr(args, "project_root", None))
        action = args.sync_command
        if action == "status":
            result = sync_status(root)
        elif action == "pull":
            result = sync_pull(root)
        elif action == "push":
            result = sync_push(root, getattr(args, "message", None))
        else:  # stop-check
            code, msg = stop_check(root)
            if msg:
                print(msg, file=sys.stderr)
            return code
    except SyncError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    if getattr(args, "summary", False):
        for key, value in result.items():
            print(f"  {key:<14} {value}")
    else:
        print(json.dumps({"ok": True, **result}, indent=2))
    return 0


def add_sync_parser(sub: argparse._SubParsersAction) -> None:
    sync = sub.add_parser("sync", help="Git-backed project state: status / pull / push (cloud sessions)")
    sync_sub = sync.add_subparsers(dest="sync_command", required=True)
    for name, help_text in (
        ("status", "Is project_root git-backed, and is it in sync?"),
        ("pull", "Fast-forward project_root from its remote"),
        ("push", "Checkpoint state.db, commit and push project_root"),
        ("stop-check", "Exit 2 when state is unsynced (Stop hook)"),
    ):
        p = sync_sub.add_parser(name, help=help_text)
        p.add_argument("--project-root", help="Override paths.project_root")
        p.add_argument("--summary", action="store_true", help="Human-readable output")
        if name == "push":
            p.add_argument("--message", "-m", help="Commit message")
    sync.set_defaults(func=cmd_sync)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_sync_parser(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main(["sync", *sys.argv[1:]]))
