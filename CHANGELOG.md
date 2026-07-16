# Changelog

## v0.3.11 — Composio Microsoft Phase 3 folders + approval-gated mail.send

### Features

- **Folder-first Outlook organise (Phase 3)**:
  - `mail_list_folders` → `OUTLOOK_LIST_MAIL_FOLDERS` (also native m365
    `GET /mailFolders`)
  - `mail_move_to_folder` → `OUTLOOK_MOVE_MESSAGE` / Graph move with any folder
    id or well-known name
  - `mail_resolve_folder` helper (well-known names + display-name lookup)
  - Capabilities: `mail.list_folders` / `mail.move` True for
    `composio_microsoft` / `:mcp` and `m365`
- **Approval-gated `mail.send`** for Composio Microsoft via `OUTLOOK_SEND_EMAIL`:
  - Capability True (still destructive)
  - Preferred path: `send_email.py prepare → approve → execute` (works for any
    provider that supports `mail.send`, including m365 / google_api /
    composio_microsoft)
  - Direct calls require `CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1` (plus
    `CHIEF_OF_STAFF_AUTO_APPROVE=1` when approval already happened via the queue)
  - Guardrail messaging names the approve→execute path
- Google Composio `mail.send` remains intentionally disabled.

### Docs

- `docs/SETUP.md` updated for Phase 3 folders and approved send.

## v0.3.10 — Composio OneDrive FileUploadable staging + mail-move verify

### Features

- **Composio Files API staging** (`shared/scripts/composio_files.py`): local files
  are staged via `POST /api/v3.1/files/upload/request` (v3 fallback) + presigned
  PUT, then passed to `ONE_DRIVE_ONEDRIVE_UPLOAD_FILE` as
  `{name, mimetype, s3key}`. Azure Blob PUTs set `x-ms-blob-type: BlockBlob`.
- **OneDrive download persist** prefers Composio `s3url` fetch before inline/
  base64 fallbacks.
- **Capability matrix** for `composio_microsoft` / `:mcp`: `files.upload` /
  `files.download` / `files.trash` and `mail.archive` / `mail.unarchive` /
  `mail.untrash` are True (share execution-verified `OUTLOOK_MOVE_MESSAGE`).
- **Verify harness** adds `mail_move_write` (draft → archive → inbox → trash →
  inbox → trash) and runs `files_write` (upload → optional download → trash)
  when those capabilities are present.

### Docs

- `docs/SETUP.md` updated for FileUploadable staging and the mail-move probe.

## v0.3.9 — Composio Microsoft cleanup + content writes (Phase 1+2)

### Features

- **Composio Microsoft mail cleanup** via catalog slug `OUTLOOK_MOVE_MESSAGE`:
  `mail_archive` → `archive`, `mail_trash` → `deleteditems` (soft-delete),
  `mail_unarchive` / `mail_untrash` → `inbox`. Returns `restore_target` like
  native m365. Does **not** use permanent `OUTLOOK_DELETE_MESSAGE`.
- **Composio Microsoft OneDrive trash** via `ONE_DRIVE_DELETE_ITEM` (recycle bin,
  not `ONE_DRIVE_DELETE_ITEM_PERMANENTLY`).
- **Content writes (Phase 2)** capability-True with **Composio catalog arg shapes**:
  `mail.draft` (`OUTLOOK_CREATE_DRAFT`), `calendar.create` /
  `calendar.update`, `files.upload` / `files.download`. Args no longer send raw
  Graph JSON where the catalog expects Composio fields (`to_recipients`,
  `start_datetime`+`time_zone`, `folder`, `file_name`, …). Write payloads normalize
  a top-level `id` for `workspace_verify`.
- **`calendar_delete`** (`OUTLOOK_DELETE_CALENDAR_EVENT`) for verify cleanup;
  opt-in CLI `--verify-calendar-writes` (create→update→delete of a marked
  `[CoS verify]` event). Default `--verify-writes` still never creates events.
- **Verify draft cleanup without tags**: when `mail.tag` is unsupported, a
  successful draft probe still trashes the artefact (needed for Composio MS).
- **Capability matrix** for `composio_microsoft` / `:mcp`: cleanup + content
  writes True; `mail.send` / `calendar.cancel` / `mail.tag*` still False.
- Slugs overridable via `tool_slugs` (`mail_move`, `files_trash`,
  `calendar_delete`, …). Google Composio family still refuses MS-only cleanup
  methods.

### Docs

- `docs/SETUP.md` Composio Microsoft verification note updated for Phase 1+2.

## v0.3.8 — Code review fixes (v0.3.5→v0.3.7 review findings)

### Breaking changes

- **Composio Microsoft write capabilities** (`mail.draft`, `calendar.create/update`, `files.upload/download`) now report `False` (unsupported) until execution-verified. Write implementations and Graph-correct arg shapes remain in code. Use Google Composio or native M365 Graph for writes.
- **`esign-connector` skill** is no longer registered unless `esign.url` is configured in `company.yaml`. `self-sign` remains always registered.
- **`GmailClient.get_attachment`** restored to pre-v0.3.6 inline `gmail attachment` CLI verb. `download_attachment` is now a separate method using `tempfile.mkdtemp()` (0o700) instead of world-readable `/tmp`.
- **`connect_workspace`** config discovery logging is quiet by default; use `--verbose` or `CHIEF_OF_STAFF_DEBUG=1` for the resolved-path log.

### Security fixes

- **Doctor DocuSeal probe** refuses non-HTTPS URLs, metadata/link-local/loopback/private IPs, and host mismatch with `esign.domain` before attaching API key.
- **Runtime log scrubbing** extended: MSAL/Google token shapes (`ya29.*`, UUIDs, 48+ char tokens), JSON key-value secrets, URL-embedded tokens.
- **`sanitize_provider_error_detail()`** classifies/truncates auth failure blobs before logging to `events.jsonl`.
- **Assistant/company name validation** rejects newlines, double quotes, and >64 char names before YAML interpolation.
- **Dotenv parser** rejects suspicious process-control keys (`PATH`, `LD_PRELOAD`, `PYTHONPATH`, etc.) and strips inline comments.

### Correctness fixes

- **Soft Composio errors** (rate limits, auth failures) now raise `RuntimeError` instead of returning `{successful: False}` → no false `read_ready: true`.
- **Error classifier reordered**: connection errors checked before unknown-tool; bare `"not found"` removed from needles.
- **Identity scrub** completed: `google.domain`, `account_alias`, SA path, Drive root, company legal IDs, phones, `home_chat_id` all scrubbed on first install. Re-bootstrap preserves operator-edited values.
- **Shipped SKILL.md** descriptions now contain rendered defaults (`Chief of Staff` / `your organization`) — no more literal `{assistant_name}` placeholders.
- **Bootstrap overlay**: custom assistant names render to `skills.local/` (gitignored) instead of mutating tracked `skills/*/SKILL.md`.
- **`skills.local/` overlay** is now loaded by `register()` — named routing works end-to-end.
- **Microsoft calendar** date-only end times use `T23:59:59Z` (not zero-duration `T00:00:00Z`).
- **`@guarded` audit slugs** resolve dynamically from family — Microsoft audit records use `OUTLOOK_*` / `ONE_DRIVE_*` slugs.
- **`esign.admin_email`** legacy fallback: both `provider_email` (preferred) and `admin_email` (legacy) accepted.
- **Name-injection skip logic** unified via shared `is_default_assistant_name()` across bootstrap/doctor/hooks.
- **Family resolution** deduplicated into `composio_family.py`, shared by client and `connect_workspace`.
- **Query compile failure** now emits `warnings.warn()` before falling back to `{top: N}`.
- **Family/toolkit mismatch** warning on client init.
- **`list_attachments`** warns on unexpected shapes instead of silent `[]`.
- **`build_briefing()`** public API replaces `cmd_demo`'s private `_build_structured_briefing` call.

### Other

- MCP `clientInfo.version` updated to match plugin version.
- Freemail domain list expanded (`googlemail.com`, `live.com`, `icloud.com`, `proton.me`, etc.).
- README note: "Never paste API keys or secrets directly into chat logs."
- `.gitignore` entry for `skills.local/` overlay.