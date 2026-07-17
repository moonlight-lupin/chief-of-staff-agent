#!/usr/bin/env python3
"""Composio file-store staging for FileUploadable tool arguments.

``ONE_DRIVE_ONEDRIVE_UPLOAD_FILE`` (and similar tools) expect a Composio
``FileUploadable`` ``{name, mimetype, s3key}`` — not a local path. The Python
SDK stages files via:

  1. POST ``/api/v3.1/files/upload/request`` (presigned URL + object key)
  2. PUT file bytes to the presigned URL
  3. Pass ``{name, mimetype, s3key: key}`` into the tool execute call

This module implements that flow with ``requests`` only (no ``composio`` SDK),
so the raw-MCP workspace provider can upload local files.
"""
from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Mapping

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

# Composio's upload tools (GOOGLEDRIVE_UPLOAD_FILE / ONE_DRIVE_ONEDRIVE_UPLOAD_FILE)
# cap FileUploadable payloads at 5 MB.
_MAX_STAGE_BYTES = 5 * 1024 * 1024
# Base64 text appended to the sandbox per COMPOSIO_REMOTE_BASH_TOOL call. Sent via
# a quoted heredoc (NOT a shell argument) so it bypasses MAX_ARG_STRLEN (128 KB);
# the only ceiling is the JSON-RPC body size, so ~700 KB per round-trip is safe.
_SANDBOX_B64_CHUNK = 700_000
_SANDBOX_MOUNT = "/mnt/files"

# Prefer the documented v3.1 path; fall back to the SDK's older v3 path.
_UPLOAD_REQUEST_PATHS = (
    "/api/v3.1/files/upload/request",
    "/api/v3/files/upload/request",
)
_DEFAULT_BACKEND = "https://backend.composio.dev"
_CHUNK = 1024 * 1024


class ComposioFileError(RuntimeError):
    """Raised when Composio file staging or download fails."""


def _backend_base() -> str:
    return (
        os.getenv("COMPOSIO_BACKEND_URL", "").strip().rstrip("/")
        or _DEFAULT_BACKEND
    )


def resolve_composio_api_key(key_env: str = "COMPOSIO_MCP_KEY") -> str:
    """Resolve an API key for Composio's REST backend (Files API).

    The Files upload-request endpoint authenticates with header ``x-api-key``
    (project API key from the Composio dashboard). Connect MCP at
    ``connect.composio.dev`` often uses the AI Clients key as
    ``x-consumer-api-key`` / Bearer — that value *may* work as ``x-api-key``
    here, but a 401 means you need the project API key as ``COMPOSIO_API_KEY``.

    Prefer ``COMPOSIO_API_KEY`` when set; otherwise fall back to ``key_env``.
    Note: text OneDrive uploads can skip this entirely via
    ``ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE`` (name+content over MCP).
    """
    for env_name in ("COMPOSIO_API_KEY", key_env):
        val = os.getenv(env_name, "").strip()
        if val:
            return val
    raise ComposioFileError(
        f"No Composio API key found. Set COMPOSIO_API_KEY (project x-api-key) "
        f"or {key_env} to stage binary FileUploadable uploads. For plain-text "
        "files prefer ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE over MCP (no staging)."
    )


def file_md5(path: Path) -> str:
    """MD5 hex digest — required by Composio's upload-request API for dedup."""
    h = hashlib.md5()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def guess_mimetype(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def stage_file_uploadable(
    file_path: str | Path,
    *,
    tool_slug: str,
    toolkit_slug: str,
    key_env: str = "COMPOSIO_MCP_KEY",
    api_key: str | None = None,
) -> dict[str, str]:
    """Stage a local file into Composio's object store; return FileUploadable dict.

    Returns ``{"name", "mimetype", "s3key"}`` suitable for tool arguments.
    """
    if requests is None:
        raise ComposioFileError("requests package required for Composio file staging")

    path = Path(file_path).expanduser()
    if not path.is_file():
        raise ComposioFileError(f"File not found or not a regular file: {path}")
    if not os.access(path, os.R_OK):
        raise ComposioFileError(f"File not readable: {path}")

    key = api_key or resolve_composio_api_key(key_env)
    mimetype = guess_mimetype(path)
    body = {
        "toolkit_slug": toolkit_slug,
        "tool_slug": tool_slug,
        "filename": path.name,
        "mimetype": mimetype,
        "md5": file_md5(path),
    }
    # Auth header variants: Files docs use x-api-key; Connect AI Clients keys
    # are documented as x-consumer-api-key. Try both with the same secret.
    auth_headers = (
        {"x-api-key": key},
        {"x-consumer-api-key": key},
    )

    meta: dict[str, Any] | None = None
    last_err = ""
    for rel in _UPLOAD_REQUEST_PATHS:
        url = f"{_backend_base()}{rel}"
        for auth in auth_headers:
            headers = {"Content-Type": "application/json", **auth}
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=30)
            except requests.RequestException as exc:
                last_err = str(exc)
                continue
            if resp.status_code == 404:
                last_err = f"HTTP 404 for {rel}"
                break  # path missing — try next path, not next auth
            if resp.status_code in (401, 403):
                last_err = (
                    f"HTTP {resp.status_code} with {next(iter(auth))} "
                    f"for {rel}: {(resp.text or '')[:200]}"
                )
                continue  # try alternate auth header
            if resp.status_code >= 400:
                raise ComposioFileError(
                    f"Composio file upload request failed (HTTP {resp.status_code}): "
                    f"{resp.text[:300]}"
                )
            try:
                meta = resp.json()
            except ValueError as exc:
                raise ComposioFileError(
                    f"Composio file upload request returned non-JSON: {exc}"
                ) from exc
            break
        if isinstance(meta, Mapping):
            break

    if not isinstance(meta, Mapping):
        raise ComposioFileError(
            f"Composio file upload request failed: {last_err or 'no response'}. "
            "Binary uploads need a project API key as x-api-key (COMPOSIO_API_KEY). "
            "For plain-text files use ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE over MCP instead."
        )

    s3key = str(meta.get("key") or "").strip()
    if not s3key:
        raise ComposioFileError(
            f"Composio file upload request missing object key: {list(meta.keys())}"
        )

    presigned = (
        str(meta.get("new_presigned_url") or meta.get("newPresignedUrl") or "").strip()
    )
    # Dedup hit: key returned without a new upload URL — reuse existing object.
    if presigned:
        put_headers: dict[str, str] = {}
        storage = (meta.get("metadata") or {}) if isinstance(meta.get("metadata"), Mapping) else {}
        if str(storage.get("storage_backend", "")).lower() == "azure_blob_storage":
            put_headers["x-ms-blob-type"] = "BlockBlob"
        try:
            with path.open("rb") as fh:
                put = requests.put(
                    presigned, data=fh, headers=put_headers, timeout=120,
                )
        except requests.RequestException as exc:
            raise ComposioFileError(f"Composio presigned PUT failed: {exc}") from exc
        # SDK historically treated some 403s as success for certain backends;
        # require 2xx here for honesty.
        if put.status_code < 200 or put.status_code >= 300:
            raise ComposioFileError(
                f"Composio presigned PUT failed (HTTP {put.status_code}): "
                f"{(put.text or '')[:200]}"
            )

    return {
        "name": path.name,
        "mimetype": mimetype,
        "s3key": s3key,
    }


# ── MCP-native sandbox staging (no COMPOSIO_API_KEY) ───────────────────────────
# The Files REST path above needs a project x-api-key. Composio's MCP meta-tools
# (COMPOSIO_REMOTE_BASH_TOOL + COMPOSIO_REMOTE_WORKBENCH) can stage a local file
# into the same object store using ONLY the MCP key:
#   1. base64-pipe the local bytes into the remote sandbox (/mnt/files) — the
#      file content travels over MCP's encrypted JSON-RPC channel, never a public
#      URL, no IP allow-listing.
#   2. call the sandbox helper upload_local_file() → returns an s3key.
#   3. the caller passes {name, mimetype, s3key} to the upload tool.
# The local sandbox copy is removed in step 4; the STAGED S3 object itself cannot
# be force-deleted over MCP (no delete helper) — its presigned URL is revoked
# immediately and it is reclaimed with the tool-router session TTL. See the
# CLEANUP note in providers/composio_mcp_workspace_base.files_upload.

_S3KEY_MARKER = "COS_S3KEY="
_STAGE_ERR_MARKER = "COS_STAGE_ERROR="
_MD5_MARKER = "COS_MD5="
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_sandbox_name(name: str) -> str:
    """A filesystem/shell-safe sandbox filename that keeps the real extension.

    The destination filename is set explicitly on the FileUploadable ``name``
    field, so the sandbox name only needs to be safe + preserve the suffix (some
    Composio helpers sniff mimetype from the extension)."""
    suffix = "".join(Path(name).suffixes)[-16:]
    suffix = _SAFE_NAME_RE.sub("", suffix)
    return f"cos_stage_{os.urandom(6).hex()}{suffix}"


def _meta_call(mcp_client: Any, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke a Composio MCP meta-tool and return its ``data`` mapping.

    Raises ``ComposioFileError`` on a non-successful envelope so a broken step
    never silently yields a bad s3key.
    """
    resp = mcp_client.call_tool(tool, arguments)
    if not isinstance(resp, Mapping):
        raise ComposioFileError(f"{tool}: unexpected response type {type(resp).__name__}")
    if resp.get("successful") is False and resp.get("error"):
        raise ComposioFileError(f"{tool} failed: {str(resp.get('error'))[:300]}")
    data = resp.get("data")
    return data if isinstance(data, Mapping) else {}


_RC_MARKER = "COS_RC="


def _sandbox_bash(mcp_client: Any, command: str, session_id: str | None) -> dict[str, Any]:
    # Gate on the command's EXIT STATUS, not on any stderr — bash tools may emit
    # benign warnings to stderr. Append a exit-code sentinel and fail only on a
    # missing or non-zero code (stderr is surfaced in the error for diagnosis).
    wrapped = f"{command}\nprintf '{_RC_MARKER}%s\\n' \"$?\""
    args: dict[str, Any] = {"command": wrapped}
    if session_id:
        args["session_id"] = session_id
    data = _meta_call(mcp_client, "COMPOSIO_REMOTE_BASH_TOOL", args)
    stdout = str(data.get("stdout") or "")
    rc: str | None = None
    for line in stdout.splitlines():
        if line.startswith(_RC_MARKER):
            rc = line[len(_RC_MARKER):].strip()
    if rc != "0":
        stderr = str(data.get("stderr") or "")
        detail = (stderr.strip() or stdout.strip() or "no output")[:300]
        raise ComposioFileError(
            f"sandbox bash exited with status {rc if rc is not None else '<none>'}: {detail}"
        )
    return data


def _sandbox_python(mcp_client: Any, code: str, session_id: str | None) -> str:
    args: dict[str, Any] = {"code_to_execute": code, "thought": "stage file for upload"}
    if session_id:
        args["session_id"] = session_id
    data = _meta_call(mcp_client, "COMPOSIO_REMOTE_WORKBENCH", args)
    return str(data.get("stdout") or "")


def stage_file_uploadable_via_sandbox(
    file_path: str | Path,
    *,
    mcp_client: Any,
    mount_dir: str = _SANDBOX_MOUNT,
) -> dict[str, str]:
    """Stage a local file into Composio's object store over MCP (no API key).

    Returns a ``{"name", "mimetype", "s3key"}`` FileUploadable dict, mirroring
    :func:`stage_file_uploadable` but authenticating with only the MCP key via
    the remote sandbox. The sandbox working copy is deleted before returning.
    """
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise ComposioFileError(f"File not found or not a regular file: {path}")
    if not os.access(path, os.R_OK):
        raise ComposioFileError(f"File not readable: {path}")
    raw = path.read_bytes()
    if len(raw) > _MAX_STAGE_BYTES:
        raise ComposioFileError(
            f"file {path.name} is {len(raw)} bytes; Composio upload tools cap "
            f"FileUploadable at {_MAX_STAGE_BYTES} bytes (5 MB)"
        )
    mimetype = guess_mimetype(path)
    expected_md5 = file_md5(path)

    # 76-col-wrapped base64 (base64 -d tolerates the newlines).
    b64 = base64.b64encode(raw).decode("ascii")
    wrapped = "\n".join(b64[i : i + 76] for i in range(0, len(b64), 76))

    sbx_name = _safe_sandbox_name(path.name)
    sbx = f"{mount_dir}/{sbx_name}"
    b64_path = f"{sbx}.b64"

    session: str | None = None
    try:
        # 1. Fresh scratch files in the sandbox.
        data = _sandbox_bash(
            mcp_client,
            f"mkdir -p {mount_dir} && rm -f '{b64_path}' '{sbx}' && echo staged-reset",
            session,
        )
        session = str(data.get("sandbox_id_suffix") or "") or None

        # 2. Append the base64 in heredoc chunks (bypasses MAX_ARG_STRLEN).
        for start in range(0, len(wrapped), _SANDBOX_B64_CHUNK):
            chunk = wrapped[start : start + _SANDBOX_B64_CHUNK]
            _sandbox_bash(
                mcp_client,
                f"cat >> '{b64_path}' <<'COS_B64_EOF'\n{chunk}\nCOS_B64_EOF",
                session,
            )

        # 3. Decode + integrity-check against the local md5.
        data = _sandbox_bash(
            mcp_client,
            f"base64 -d '{b64_path}' > '{sbx}' && rm -f '{b64_path}' && "
            f"printf '{_MD5_MARKER}%s\\n' \"$(md5sum '{sbx}' | cut -d' ' -f1)\"",
            session,
        )
        got_md5 = ""
        for line in str(data.get("stdout") or "").splitlines():
            if line.startswith(_MD5_MARKER):
                got_md5 = line[len(_MD5_MARKER):].strip()
        if got_md5 != expected_md5:
            raise ComposioFileError(
                f"sandbox file integrity check failed for {path.name}: "
                f"local md5 {expected_md5}, sandbox md5 {got_md5 or '<none>'}"
            )

        # 4. Stage to the object store via the sandbox helper → s3key.
        code = (
            "try:\n"
            f"    _m, _ = upload_local_file({sbx!r})\n"
            f"    print({_S3KEY_MARKER!r} + str(_m.get('s3key','')))\n"
            "except Exception as _e:\n"
            f"    print({_STAGE_ERR_MARKER!r} + repr(_e))\n"
        )
        stdout = _sandbox_python(mcp_client, code, session)
        s3key = ""
        for line in stdout.splitlines():
            if line.startswith(_S3KEY_MARKER):
                s3key = line[len(_S3KEY_MARKER):].strip()
            elif line.startswith(_STAGE_ERR_MARKER):
                raise ComposioFileError(
                    f"sandbox upload_local_file failed: {line[len(_STAGE_ERR_MARKER):][:300]}"
                )
        if not s3key:
            raise ComposioFileError(
                f"sandbox staging returned no s3key for {path.name}; stdout: {stdout[:300]}"
            )
        return {"name": path.name, "mimetype": mimetype, "s3key": s3key}
    finally:
        # Remove the sandbox working copy (best-effort; sandbox is ephemeral too).
        try:
            _sandbox_bash(mcp_client, f"rm -f '{sbx}' '{b64_path}'", session)
        except Exception:  # noqa: BLE001 — cleanup must never mask the real result
            pass


def download_s3url(url: str, output_path: str | Path, *, timeout: int = 120) -> Path:
    """Download bytes from a Composio ``s3url`` (or any HTTPS URL) to ``output_path``."""
    if requests is None:
        raise ComposioFileError("requests package required for Composio file download")
    if not url:
        raise ComposioFileError("empty download URL")
    out = Path(output_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = requests.get(url, stream=True, timeout=timeout)
    except requests.RequestException as exc:
        raise ComposioFileError(f"download failed: {exc}") from exc
    if resp.status_code >= 400:
        raise ComposioFileError(
            f"download failed (HTTP {resp.status_code}): {(resp.text or '')[:200]}"
        )
    with out.open("wb") as fh:
        for chunk in resp.iter_content(chunk_size=_CHUNK):
            if chunk:
                fh.write(chunk)
    return out


def find_s3url(payload: Any) -> str | None:
    """Extract a Composio download ``s3url`` from a nested tool payload."""
    if isinstance(payload, Mapping):
        for key in ("s3url", "s3_url", "url"):
            val = payload.get(key)
            if isinstance(val, str) and val.startswith(("http://", "https://")):
                # Prefer explicit s3url keys; bare "url" only when nested under content.
                if key in ("s3url", "s3_url"):
                    return val
        content = payload.get("content")
        if isinstance(content, Mapping):
            found = find_s3url(content)
            if found:
                return found
        for nest_key in ("data", "response_data", "result", "attachment", "file"):
            nested = payload.get(nest_key)
            if isinstance(nested, (Mapping, list)):
                found = find_s3url(nested)
                if found:
                    return found
        # Last resort: nested dict with a https url under content-like shapes.
        for nest_key in ("content",):
            nested = payload.get(nest_key)
            if isinstance(nested, Mapping):
                val = nested.get("url")
                if isinstance(val, str) and val.startswith(("http://", "https://")):
                    return val
    elif isinstance(payload, list):
        for item in payload:
            found = find_s3url(item)
            if found:
                return found
    return None
