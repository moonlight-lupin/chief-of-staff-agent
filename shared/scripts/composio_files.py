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

import hashlib
import mimetypes
import os
from pathlib import Path
from typing import Any, Mapping

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

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

    Prefer ``COMPOSIO_API_KEY`` when set; otherwise reuse the MCP key env
    (same project key is typically accepted as ``x-api-key``).
    """
    for env_name in ("COMPOSIO_API_KEY", key_env):
        val = os.getenv(env_name, "").strip()
        if val:
            return val
    raise ComposioFileError(
        f"No Composio API key found. Set COMPOSIO_API_KEY or {key_env} "
        "to stage files for OneDrive upload."
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
    headers = {
        "x-api-key": key,
        "Content-Type": "application/json",
    }

    meta: dict[str, Any] | None = None
    last_err = ""
    for rel in _UPLOAD_REQUEST_PATHS:
        url = f"{_backend_base()}{rel}"
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=30)
        except requests.RequestException as exc:
            last_err = str(exc)
            continue
        if resp.status_code == 404:
            last_err = f"HTTP 404 for {rel}"
            continue
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

    if not isinstance(meta, Mapping):
        raise ComposioFileError(
            f"Composio file upload request failed: {last_err or 'no response'}"
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
