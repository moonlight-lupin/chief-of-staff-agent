"""Where an approved action may read or write local files (P0.3).

Action payloads can be shaped by untrusted content, so the local paths they
carry are confined, not just shown to the approver:

* **Downloads** (``drive.download`` / ``files.download``) write a *new* file
  inside the company's project root. They never overwrite an existing file,
  and never write into the user's records (top-level ``*.yaml``/``*.yml``),
  ``wiki/``, ``workflows/``, ``skills.local/``, a hidden path or the store.
* **Uploads** (``drive.upload`` / ``files.upload``) and **attachments** on
  send actions read only from the project root or ``paths.upload_roots``
  (never ``/`` or the home folder itself), and never a credential, a key, the
  configured service-account file, the plugin's state, a hidden file or a
  hard-linked file.

Every path key a payload carries is checked, not just the one the executor
reads. Checked at proposal (``StateDB.create_action``), at ``claim``, and
again inside every guarded upload/download, after resolving symlinks.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

DOWNLOAD_TYPES = frozenset({"drive.download", "files.download"})
UPLOAD_TYPES = frozenset({"drive.upload", "files.upload"})
SEND_TYPES = frozenset({"gmail.send", "mail.send", "gmail.draft", "mail.draft"})

_DOWNLOAD_KEYS = ("output_path", "path", "destination", "dest", "local_path")
_UPLOAD_KEYS = ("file_path", "path", "source", "local_path")

_SECRET_PATTERNS = ("*secret*", "*credential*", "*token*.json", "*service*account*", "*.env", ".env*", "id_*",
                    "state.*", "*.sqlite", "*.sqlite3", "*.db", "*.db-*", "*.pem", "*.key", "*.p12", "*.pfx",
                    "*.keystore", "auth.json", "*.kdbx", "*.ovpn")
_SECRET_DIRS = (".ssh", ".hermes", ".aws", ".gnupg", ".kube", ".config/gcloud", ".docker", ".azure")
_PROTECTED_SUBTREES = ("wiki", "workflows", "skills.local")


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


def _upload_roots(config: Any) -> list[Path]:
    roots = [r for r in [_project_root(config)] if r is not None]
    try:
        extra = (config.get("paths") or {}).get("upload_roots") or []
    except AttributeError:
        extra = []
    home = Path.home().resolve()
    for raw in extra if isinstance(extra, (list, tuple)) else []:
        path = _resolve(raw)
        # "/" or the home folder itself would make the confinement meaningless.
        if path is not None and path != Path("/") and path != home and len(path.parts) > 1:
            roots.append(path)
    return roots


def _configured_key_file(config: Any) -> Path | None:
    try:
        return _resolve(((config.get("google") or {}).get("service_account_path")))
    except AttributeError:
        return None


def _secret_name(path: Path) -> bool:
    name = path.name.lower()
    return any(fnmatch.fnmatch(name, pattern) for pattern in _SECRET_PATTERNS)


def _secret_location(path: Path, root: Path | None) -> bool:
    """Hidden below the allowed root, or (outside any root) a known secret dir."""
    if root is not None and _inside(path, root):
        return any(part.startswith(".") for part in path.relative_to(root).parts)
    text = path.as_posix()
    home = Path.home().resolve().as_posix()
    return any(text == f"{home}/{d}" or text.startswith(f"{home}/{d}/") for d in _SECRET_DIRS)


def _hard_linked(path: Path) -> bool:
    try:
        return path.is_file() and os.stat(path).st_nlink > 1
    except OSError:
        return False


def _check_upload(config: Any, raw: Any) -> str | None:
    path = _resolve(raw)
    if path is None:
        return None
    roots = _upload_roots(config)
    home_root = next((r for r in roots if _inside(path, r)), None)
    key_file = _configured_key_file(config)
    if _secret_name(path) or _secret_location(path, home_root) or (key_file is not None and path == key_file):
        return f"file {raw!r} refused: it looks like a credential, a key or the plugin's own state"
    if home_root is None:
        return (f"file {raw!r} refused: files must come from the project root or paths.upload_roots "
                f"({', '.join(str(r) for r in roots) or 'none configured'})")
    if _hard_linked(path):
        return f"file {raw!r} refused: it is hard-linked, so it may be another file in disguise"
    return None


def _check_download(config: Any, raw: Any) -> str | None:
    path, root = _resolve(raw), _project_root(config)
    if path is None:
        return None
    if root is None or not _inside(path, root) or path == root:
        return f"download path {raw!r} is outside the project root; save inside {root}"
    rel = path.relative_to(root)
    if _secret_name(path) or _secret_location(path, root):
        return f"download path {raw!r} would write a hidden, credential or state file"
    if rel.parts and rel.parts[0] in _PROTECTED_SUBTREES:
        return f"download path {raw!r} would write into {rel.parts[0]}/, which holds your notes or instructions"
    if len(rel.parts) == 1 and path.suffix.lower() in {".yaml", ".yml", ".json", ".md"}:
        return f"download path {raw!r} would replace one of your records; save into a subfolder"
    if path.exists():
        return f"download path {raw!r} already exists; a download never overwrites a file"
    return None


def _values(payload: Mapping[str, Any], keys: Iterable[str]) -> list[Any]:
    return [payload.get(k) for k in keys if payload.get(k) not in (None, "")]


def check_payload(config: Any, action_type: str, payload: Mapping[str, Any] | None) -> str | None:
    """None when every local path in the payload is allowed; otherwise why not."""
    payload = payload or {}
    if action_type in DOWNLOAD_TYPES:
        for raw in _values(payload, _DOWNLOAD_KEYS):
            reason = _check_download(config, raw)
            if reason:
                return reason
    elif action_type in UPLOAD_TYPES:
        for raw in _values(payload, _UPLOAD_KEYS):
            reason = _check_upload(config, raw)
            if reason:
                return reason
    if action_type in SEND_TYPES or payload.get("attachments"):
        attachments = payload.get("attachments") or []
        for item in attachments if isinstance(attachments, (list, tuple)) else [attachments]:
            raw = (item.get("path") or item.get("file_path")) if isinstance(item, Mapping) else item
            reason = _check_upload(config, raw) if raw else None
            if reason:
                return f"attachment: {reason}"
    return None


def enforce(config: Any, action_type: str, payload: Mapping[str, Any] | None) -> None:
    reason = check_payload(config, action_type, payload)
    if reason:
        raise PayloadRefused(reason)
