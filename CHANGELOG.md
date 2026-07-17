# Changelog

## v0.3.17 — document.handoff polish + Drive files.untrash

### Features

- **`document.handoff` readiness.** Preflight/dry-run/error copy no longer claims
  only Composio can draft; `google_api` (SA REST draft) and Composio Microsoft are
  first-class. Upload gaps fail closed before side effects; `--allow-partial`
  covers draft-only gaps. Google Composio uploads normalize `id` + `webViewLink` /
  `link` for draft body linking. Docs (`LIVE_TEST_CHECKLIST`, `APPROVAL_RUNBOOK`)
  and `PROVIDER_RECOMMENDATIONS` updated.
- **`files.untrash` soft-delete restore symmetry (Google).** New capability
  `files.untrash` (legacy `drive.untrash`) with ABC + `drive_untrash` alias.
  - `google_api`: Drive REST `files.update` `trashed=False` (SA + delegate)
  - Composio Google: `GOOGLEDRIVE_UNTRASH_FILE`
  - `delete_actions.py restore` for executed `drive.trash` → `drive_untrash`
  - Guardrails: `files.untrash` in `WRITE_ACTIONS` / `SAFE_WRITE_ACTIONS`
- **OneDrive untrash wired but capability False.** Methods call
  `ONE_DRIVE_RESTORE_DRIVE_ITEM` (Composio MS) / Graph `POST …/restore` (`m365`);
  kept False with Personal-only reason until Business/SharePoint is verified.
- **Beta daily-loop notes.** `BETA_DAILY_LOOP.md` / `BETA_READINESS_CHECKLIST.md`
  refreshed for Google-first beta; Outlook email-org E2E deferred without Entra.

## v0.3.16 — Keyless binary file upload via MCP sandbox staging

### Features

- **Composio binary file upload without `COMPOSIO_API_KEY`.** Google Drive and
  OneDrive binary uploads (`.pdf`/`.docx`) previously required a project
  `COMPOSIO_API_KEY` to stage a `FileUploadable` through the Files REST API (the
  MCP key 401s). New `composio_files.stage_file_uploadable_via_sandbox()` stages
  the local file into Composio's object store over the **MCP meta-tools**
  (`COMPOSIO_REMOTE_BASH_TOOL` base64-pipes the bytes into the remote sandbox with
  an md5 integrity check; `COMPOSIO_REMOTE_WORKBENCH.upload_local_file()` returns
  the `s3key`), needing **only `COMPOSIO_MCP_KEY`**. The provider's `files_upload`
  (both families) routes binary through this path; text is unchanged
  (`GOOGLEDRIVE_CREATE_FILE_FROM_TEXT` / `ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE`).
  The REST stager stays available for keyed setups but is no longer the default.
- **`files.upload` → True** for `composio`, `composio:mcp`, `composio_microsoft`,
  `composio_microsoft:mcp`; the `COMPOSIO_API_KEY` `UNSUPPORTED_REASONS` entries
  are removed. `--verify-writes` now exercises `files_write` instead of skipping
  it. **Execution-verified 2026-07-17** (only `COMPOSIO_MCP_KEY`): a throwaway
  `.pdf` uploaded to Google Drive and OneDrive and was trashed (Drive confirmed in
  Trash).

### Cleanup semantics

- Local temp file (caller-unlinked) and the sandbox working copy (removed in the
  stager's `finally`; sandbox is session-TTL reclaimed) are cleaned up. The
  intermediate Composio **S3 staged object** is **access-revoked** (its presigned
  URL 403s immediately) and **reclaimed on the tool-router session TTL** — there
  is no MCP delete for it, so it is not purged on demand (an explicit delete would
  need the Files REST API). Documented in the `files_upload` CLEANUP note.

### Constraints

- Composio upload tools cap `FileUploadable` at 5 MB (the stager rejects larger);
  base64 inflates the transfer ~1.33× (chunked ~700 KB/round-trip via heredoc,
  bypassing `MAX_ARG_STRLEN`).

## v0.3.15 — Google Drive trash, google_api drafts, Outlook email-org

### Features

- **Google Composio Drive files**: text uploads now use
  `GOOGLEDRIVE_CREATE_FILE_FROM_TEXT` (`file_name`+`text_content`, MCP-native — no
  Files-API staging, no `COMPOSIO_API_KEY`); binary uploads use
  `GOOGLEDRIVE_UPLOAD_FILE` with a staged `file_to_upload` (the raw `file_path`
  was silently ignored — fixed). **`files.trash` → True (execution-verified
  2026-07-16):** a text file created via `CREATE_FILE_FROM_TEXT` was trashed via
  `GOOGLEDRIVE_TRASH_FILE` and confirmed in Drive Trash. **`files.upload` stays
  False** (mirrors OneDrive): text works over MCP, but binary document filing
  needs `COMPOSIO_API_KEY` — or use the `google_api` service-account provider,
  which uploads binary to Drive directly with no Composio key.
- **`google_api` `mail.draft`**: create drafts via Gmail REST
  (`users.drafts.create`) with service-account domain-wide delegation when
  `google_api.py` has no draft CLI. Uses the `gmail.modify` scope (already in
  the provider's standard SCOPES, so no new admin delegation) and surfaces the
  message id as `id` (keeps `draft_id`). **Execution-verified 2026-07-16** — the
  draft landed in the delegate's Drafts folder. Unlocks `document.handoff` on
  google_api.
- **email-organisation Composio Microsoft**: classify → suggest → prepare path
  hardened for Outlook categories (displayName as tag id, Outlook message
  shape, category-aware suggestion copy). Live checklist covers prepare →
  review_queue execute.

## v0.3.14 — Google cleanup hardening + Outlook inspect-labels

### Fixes / hardening

- **Google Composio mail.tag / archive / unarchive / trash / untrash**:
  - Reject Gmail draft ids (`r-…`) on label/trash paths (tools need hex message ids).
  - Resolve label display names → `Label_…` ids before `GMAIL_ADD_LABEL_TO_EMAIL`.
  - `mail_create_tag` reuses an existing label id on 409/already-exists (verify
    path no longer falls back to the bare display name).
  - `workspace_verify` looks up tag ids via `mail_list_tags` on reuse.
  - **Execution-verified 2026-07-16 (live Gmail):** with the hardened path,
    `--verify-writes` on `family: google` ran green (`write_ready: yes`) — a full
    archive→unarchive→trash→untrash cycle plus tag apply on real hex message ids,
    no id-shape errors. These five capabilities are now **True** for
    `composio` / `composio:mcp`.

### Features

- **email-organisation `inspect-labels` (Composio Microsoft)**: Outlook-aware
  summary (`Outlook Category Inspection`, `tag_surface: outlook_categories`);
  `parse_labels` accepts `displayName` / missing `type` for master categories.

## v0.3.13 — Hermes Composio reads + Google Composio cleanup/tags/send

### Features

- **Hermes Composio MCP as read front-end**: document the fetch/compute split when
  Hermes already has Composio MCP connected — agent fetches reads →
  `schemas.py` envelope → `--input`; writes stay on `get_workspace_client`
  (`@guarded` + audit). Updated `agent` provider guidance, daily-briefing /
  weekly-review / meeting-prep Workspace Access, and `docs/SETUP.md`.
- **Google Composio parity** (catalog-wired from docs.composio.dev/toolkits/gmail):
  - `mail_list_tags` → `GMAIL_LIST_LABELS`
  - `mail_create_tag` → `GMAIL_CREATE_LABEL`
  - `mail_tag` / archive / unarchive → `GMAIL_ADD_LABEL_TO_EMAIL`
  - `mail_trash` / `mail_untrash` → `GMAIL_MOVE_TO_TRASH` / `GMAIL_UNTRASH_MESSAGE`
  - `mail_send` → `GMAIL_SEND_EMAIL` (approval-gated, same model as MS)
  - **Execution-verified 2026-07-16 (live Gmail):** `mail.list_tags`
    (`GMAIL_LIST_LABELS`), `mail.create_tag` (`GMAIL_CREATE_LABEL`), `mail.send`
    (`GMAIL_SEND_EMAIL`, sent + received) → capabilities True.
  - **Wired but NOT yet verified → False:** `mail.tag` / `mail.archive` /
    `mail.unarchive` / `mail.trash` / `mail.untrash`. The live probe rejected a
    Gmail draft id where a hex message id is required; `mail_create_draft` now
    surfaces the underlying `message.id` (the fix), and these flip True once
    `--verify-writes` re-runs green on `family: google`.
  - Still False: `mail.list_folders` / `mail.move`, `calendar.cancel`, `files.trash`
- **email-organisation**: skill + live checklist cover Composio Microsoft
  Outlook categories (Phase 4) and the CoS-only write path.

### Unchanged on purpose

- `calendar.cancel` remains False (no restore-path parity).
- Composio Microsoft `files.upload` remains False until `COMPOSIO_API_KEY`
  enables binary filing (text `CREATE_TEXT_FILE` still works when called).

## v0.3.12 — Composio Microsoft Phase 4 categories + MCP-native OneDrive text upload

### Features

- **Outlook categories (Phase 4)**:
  - `mail_list_tags` → `OUTLOOK_GET_MASTER_CATEGORIES`
  - `mail_create_tag` → `OUTLOOK_CREATE_USER_MASTER_CATEGORY`
  - `mail_tag` → `OUTLOOK_GET_MESSAGE` (current categories) +
    `OUTLOOK_UPDATE_EMAIL` (append category displayName)
  - Tag id IS the category `displayName` (same contract as native m365)
  - Capabilities: `mail.list_tags` / `mail.tag` / `mail.create_tag` True for
    `composio_microsoft` / `:mcp`
- **OneDrive text uploads without Files API**:
  - Plain-text files use `ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE` (`name` +
    `content` + optional `folder`) over Connect MCP — no
    `COMPOSIO_API_KEY` / FileUploadable staging
  - Binary files still use `ONE_DRIVE_ONEDRIVE_UPLOAD_FILE` with staged
    `{name,mimetype,s3key}` (project `x-api-key`) or a public `source_url`
  - Files staging retries `x-consumer-api-key` when `x-api-key` returns 401/403
  - Capabilities: `files.download` / `files.trash` True (execution-verified
    2026-07-16). `files.upload` stays **False**: the text path works over MCP,
    but binary document filing (`.pdf`/`.docx`) needs `COMPOSIO_API_KEY`, and a
    coarse boolean must not over-promise it (set the key to enable)

### Docs

- `docs/SETUP.md` clarifies text vs binary OneDrive paths and Phase 4 tags
  (see also https://composio.dev/toolkits/one_drive).

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