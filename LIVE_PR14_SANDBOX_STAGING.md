# PR #14 — MCP-native binary upload via sandbox staging (no COMPOSIO_API_KEY)

**Date:** 2026-07-17
**Branch:** `claude/composio-sandbox-file-staging` (off `main` @ v0.3.15 / PR #13)
**Scope:** Composio Google Drive **and** OneDrive binary file upload with **only
`COMPOSIO_MCP_KEY`** — no `COMPOSIO_API_KEY`, no public URL, no service account.

## Problem
`GOOGLEDRIVE_UPLOAD_FILE` / `ONE_DRIVE_ONEDRIVE_UPLOAD_FILE` take a Composio
**FileUploadable** (`{name, mimetype, s3key}`), not a local path. Staging that
s3key via the Files REST API needs a project `COMPOSIO_API_KEY` (the MCP key
401s — verified in prior runs). So binary document filing (`.pdf`/`.docx`) was
capability-`False` for the Composio families. Text already worked MCP-native via
the create-from-text tools.

## Fix
Stage the local file into Composio's object store over the **MCP meta-tools**,
which authenticate with only the MCP key:

1. `COMPOSIO_REMOTE_BASH_TOOL` — base64-pipe the local bytes into the remote
   sandbox `/mnt/files/` (chunked heredocs; file content travels the encrypted
   JSON-RPC channel, never a public URL). An md5 integrity check compares the
   sandbox copy to the local file.
2. `COMPOSIO_REMOTE_WORKBENCH` — `upload_local_file()` → returns an `s3key`.
3. Pass `{name, mimetype, s3key}` to the upload tool.

Text uploads are unchanged (`GOOGLEDRIVE_CREATE_FILE_FROM_TEXT` /
`ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE`). The REST stager
(`composio_files.stage_file_uploadable`) is kept for keyed setups but is no
longer the default wiring.

### Files changed
- `shared/scripts/composio_files.py` — `stage_file_uploadable_via_sandbox()` +
  helpers (`_sandbox_bash`, `_sandbox_python`, chunking, md5 check, cleanup).
- `shared/scripts/providers/composio_mcp_workspace_base.py` — `_stage_file_uploadable()`
  (sandbox-only), both family binary paths route through it; CLEANUP note added.
- `shared/scripts/workspace_capabilities.py` — `files.upload` → `True` for
  `composio`, `composio:mcp`, `composio_microsoft`, `composio_microsoft:mcp`;
  removed the `COMPOSIO_API_KEY` `UNSUPPORTED_REASONS` entries and constants.
- Tests updated to the new reality + new sandbox-staging unit tests
  (mocked MCP: happy path, md5-mismatch, helper-error, oversize).

## Live verification (2026-07-17, only `COMPOSIO_MCP_KEY`, `COMPOSIO_API_KEY` unset)
Throwaway `.pdf` (binary) through the real provider `files_upload` → `files_trash`:

| Family | Upload | Trash | Confirm |
|---|---|---|---|
| Google Drive (`GOOGLEDRIVE_UPLOAD_FILE`) | ✅ file id returned | ✅ `GOOGLEDRIVE_TRASH_FILE` | active=0, in Trash=1 |
| OneDrive (`ONE_DRIVE_ONEDRIVE_UPLOAD_FILE`) | ✅ file id returned | ✅ `ONE_DRIVE_DELETE_ITEM` | trashed |

Sandbox `/mnt/files` swept clean afterward. **Unit suite: `python -m pytest -q` →
1769 passed, 0 failed.**

## Cleanup semantics (the 4 artifacts)
1. **Local temp file** — caller unlinks it (`try/finally`).
2. **Sandbox working copy** (`/mnt/files/…`) — removed in the stager's `finally`;
   the sandbox is ephemeral (session-TTL reclaimed).
3. **Composio S3 staged object** (the `s3key`) — **access-revoked + TTL-reclaimed,
   not purged on demand.** Its presigned URL 403s immediately and the key is
   scoped to the tool-router session, so Composio reclaims it on session TTL.
   There is **no MCP delete** for it; an explicit purge would need the Files REST
   API (`COMPOSIO_API_KEY`). This is an accepted limitation of the keyless path.
4. **Destination Drive/OneDrive file** — the deliverable; trash via `files_trash`
   when it is a throwaway/verification artefact.

## Constraints
- Composio upload tools cap FileUploadable at **5 MB**; the stager rejects larger.
- base64 inflates the payload ~1.33×; transfer is chunked (~700 KB/round-trip via
  heredoc, bypassing `MAX_ARG_STRLEN`).
- 3 MCP round-trips + per-chunk bash calls; all server-to-server. Fine for typical
  CoS documents (invoices/letters/reports).
