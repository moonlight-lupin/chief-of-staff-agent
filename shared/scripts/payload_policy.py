"""Where an approved action may read or write local files (P0.3).

Drive actions carry local paths in their payload, and payloads can be shaped
by untrusted content. So a path is confined, not just shown to the approver:

* ``drive.download`` / ``files.download`` writes only inside the company's
  project root (never over plugin code, another company's store, or a
  dotfile);
* ``drive.upload`` / ``files.upload`` reads only from the project root or
  the configured ``paths.upload_roots``, and never a credential or the
  plugin's own state (``.env``, keys, ``~/.ssh``, ``~/.hermes``, ``state.db``).

Checked when the action is created and again just before execution, after
resolving symlinks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

DOWNLOAD_TYPES = frozenset({"drive.download", "files.download"})
UPLOAD_TYPES = frozenset({"drive.upload", "files.upload"})

_SECRET_NAMES = frozenset({".env", "state.db", "state.db-wal", "state.db-shm", "auth.json", "id_rsa",
                           "id_ed25519", "credentials", "application_default_credentials.json"})
_SECRET_DIRS = (".ssh", ".hermes", ".aws", ".gnupg", ".kube", ".config/gcloud", ".docker")
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


class PayloadRefused(ValueError):
    """An action's payload points somewhere it must not."""


def _resolve(raw: Any) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _project_root(config: Any) -> Path | None:
    try:
        from config_loader import get_project_root

        return Path(get_project_root(config)).expanduser().resolve(strict=False)
    except Exception:
        return None


def _is_secret(path: Path, root: Path | None = None) -> bool:
    """A credential, the plugin's state, or a hidden file. Inside an allowed
    root (which may itself live under ~/.hermes) only the path *below* the
    root is judged, so the project folder is never mistaken for a secret."""
    name = path.name.lower()
    if name in _SECRET_NAMES or name.startswith(".env") or name.endswith(_SECRET_SUFFIXES):
        return True
    if root is not None and _inside(path, root):
        return any(part.startswith(".") for part in path.relative_to(root).parts)
    text = path.as_posix()
    home = Path.home().resolve().as_posix()
    return any(text == f"{home}/{d}" or text.startswith(f"{home}/{d}/") for d in _SECRET_DIRS)


def check_payload(config: Any, action_type: str, payload: Mapping[str, Any] | None) -> str | None:
    """None when the payload's paths are allowed; otherwise why not."""
    payload = payload or {}
    if action_type in DOWNLOAD_TYPES:
        raw = payload.get("output_path", payload.get("path"))
        path, root = _resolve(raw), _project_root(config)
        if path is None:
            return None   # the provider refuses a missing path itself
        if root is None or not _inside(path, root) or path == root:
            return f"download path {raw!r} is outside the project root; save inside {root}"
        if _is_secret(path, root):
            return f"download path {raw!r} would overwrite a hidden or state file"
        return None
    if action_type in UPLOAD_TYPES:
        raw = payload.get("file_path", payload.get("path"))
        path = _resolve(raw)
        if path is None:
            return None
        roots = [r for r in [_project_root(config)] if r is not None]
        try:
            extra = (config.get("paths") or {}).get("upload_roots") or []
        except AttributeError:
            extra = []
        roots += [p for p in (_resolve(r) for r in extra) if p is not None]
        home_root = next((r for r in roots if _inside(path, r)), None)
        if _is_secret(path, home_root):
            return f"upload of {raw!r} refused: it is a credential or the plugin's own state"
        if home_root is None:
            return (f"upload of {raw!r} refused: files must come from the project root or "
                    f"paths.upload_roots ({', '.join(str(r) for r in roots) or 'none configured'})")
        return None
    return None


def enforce(config: Any, action_type: str, payload: Mapping[str, Any] | None) -> None:
    reason = check_payload(config, action_type, payload)
    if reason:
        raise PayloadRefused(reason)
