#!/usr/bin/env python3
"""File-level advisory locking using fcntl for POSIX systems.

Usage:
    from file_lock import with_lock
    with with_lock("/path/to/pipeline.yaml"):
        # safe read-modify-write
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import os
import time
from pathlib import Path
from typing import Iterator


class FileLockError(RuntimeError):
    """Raised when an advisory lock cannot be acquired or released safely."""


def _lock_path(path: str | os.PathLike[str]) -> Path:
    return Path(f"{Path(path).expanduser()}.lock")


def _open_lock_file(path: str | os.PathLike[str]):
    lock_path = _lock_path(path)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        return lock_path.open("a+")
    except OSError as exc:
        raise FileLockError(f"Cannot create/open lock file {lock_path}: {exc}") from exc


@contextlib.contextmanager
def with_lock(path: str | os.PathLike[str], timeout: float | None = None) -> Iterator[Path]:
    """Acquire an exclusive advisory lock for ``path`` and release on exit.

    The lock is taken on ``{path}.lock``. The lock file is created if missing and
    intentionally left in place after release so future callers can reuse it.

    Args:
        path: Original data file path being protected.
        timeout: Optional seconds to wait. ``None`` waits indefinitely.

    Yields:
        The lock-file path.

    Raises:
        FileLockError: if the lock file cannot be created, the lock times out, or
        the OS reports an unexpected locking error.
    """

    fh = _open_lock_file(path)
    lock_path = _lock_path(path)
    start = time.monotonic()
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise FileLockError(f"Broken lock for {lock_path}: {exc}") from exc
                if timeout is not None and (time.monotonic() - start) >= timeout:
                    raise FileLockError(f"Timed out after {timeout}s waiting for lock {lock_path}") from exc
                time.sleep(0.05)
        yield lock_path
    finally:
        if acquired:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                raise FileLockError(f"Failed to release lock {lock_path}: {exc}") from exc
        fh.close()


def try_lock(path: str | os.PathLike[str], timeout: float = 5) -> bool:
    """Return True if an exclusive lock can be acquired within ``timeout``.

    This is a probe: on success the lock is released before returning. Use
    ``with_lock`` to hold a lock while doing work.
    """

    try:
        with with_lock(path, timeout=timeout):
            return True
    except FileLockError:
        return False


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe POSIX advisory file locks")
    parser.add_argument("path", help="Path whose {path}.lock should be probed")
    parser.add_argument("--timeout", type=float, default=5, help="Seconds to wait for the lock")
    args = parser.parse_args(argv)
    ok = try_lock(args.path, timeout=args.timeout)
    print("available" if ok else "locked")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
