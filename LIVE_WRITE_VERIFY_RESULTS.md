# Live WRITE Verification — Composio Microsoft 365 (PR #6, Phase 1+2)

**Date:** 2026-07-16
**Branch:** `cursor/composio-ms-cleanup-phase1-34d1`
**Provider:** `composio_microsoft:mcp` (family `microsoft`, mode `mcp`)
**Endpoint:** `https://connect.composio.dev/mcp`
**Key:** `COMPOSIO_MCP_KEY` — shell env only, **REDACTED**, never written to config/commit/log.
**Config:** `/tmp/live/company.yaml` (not committed; `paths.project_root` under `/tmp`).

**Safety honored:** No mail was ever sent (no `mail_send` / `OUTLOOK_SEND_EMAIL`). Only
conservative writes were performed: create+trash a draft, and (opt-in) create→update→delete
ONE marked event. No permanent deletes (draft → Deleted Items; event → calendar Deleted
Items, both recoverable). Every artifact was cleaned up and a post-run scan confirms **0
leftover verification drafts and 0 leftover verification events**.

> Redaction: counts and shapes only. No real subjects, addresses, file names, or event
> titles. Returned ids are not reproduced. The connected mailbox owner identity is redacted.

---

## 1. Connections (both active)

| Toolkit | Active | Entity (redacted owner) |
|---|---|---|
| `outlook`   | ✅ active default | `outlook_serio-vealer` |
| `one_drive` | ✅ active default | `one_drive_stanly-jarry` |

Reads sanity (`--verify`): **read_ready: yes** (auth, mail_read, mail_folder_scoped,
calendar_read, files_read all pass; `mail_tags_list` optional-fail as before).

---

## 2. Per-write result (what actually EXECUTED live)

| Capability | Composio slug | Verified args | Result |
|---|---|---|---|
| `mail.draft`      | `OUTLOOK_CREATE_DRAFT`         | `subject, body, is_html, to_recipients[]` | ✅ **executed** — draft created (id returned) |
| `mail.trash`      | `OUTLOOK_MOVE_MESSAGE`         | `message_id, destination_id="deleteditems"` | ✅ **executed** — draft moved to Deleted Items (cleanup) |
| `calendar.create` | `OUTLOOK_CALENDAR_CREATE_EVENT`| `subject, start_datetime, end_datetime, time_zone` | ✅ **executed** — event created (id returned) |
| `calendar.update` | `OUTLOOK_UPDATE_CALENDAR_EVENT`| `event_id, subject` | ✅ **executed** — event updated |
| `calendar.delete` | `OUTLOOK_DELETE_CALENDAR_EVENT`| `event_id` | ✅ **executed** — event deleted (recoverable) |
| `files.upload`    | `ONE_DRIVE_ONEDRIVE_UPLOAD_FILE` | `file` (local path) | ❌ **FAILED — hard blocker** (see §3) |
| `files.trash`     | `ONE_DRIVE_DELETE_ITEM`       | `item_id` | ⏭ **not executed** — no self-created item (blocked by upload) |
| `files.download`  | `ONE_DRIVE_DOWNLOAD_FILE`     | `item_id, file_name` | ⏭ **not executed** — no self-created item (blocked by upload) |
| `mail.archive` / `mail.unarchive` / `mail.untrash` | `OUTLOOK_MOVE_MESSAGE` (dest `archive`/`inbox`) | — | ⏭ **not exercised** — outside the conservative write list; only the `deleteditems` destination ran (via `mail.trash`) |

Official harness runs (final, with honest capabilities):
- `--verify-writes` → `Write ready: yes`, exit 0: `mail_draft ✓`; `files_write —` (skipped,
  "provider does not support files.upload"); `mail_send —`.
- `--verify-calendar-writes` → `Write ready: yes`, exit 0: `calendar_write ✓
  created/updated/deleted`.

---

## 3. Slug / argument findings

**No slug or argument corrections were needed in the provider this run.** The Phase 1+2
slugs and Composio-shaped args in `composio_mcp_workspace_base.py` were already correct for
every write that executed (draft, mail-move→deleteditems, calendar create/update/delete all
succeeded as-coded). The one failure was NOT a slug/arg typo:

**`files.upload` — confirmed HARD BLOCKER (not fixable by arg renaming):**
- Live error: `Invalid request data provided — Input should be a valid dictionary or
  instance of FileUploadable on parameter 'file'`.
- `ONE_DRIVE_ONEDRIVE_UPLOAD_FILE`'s `file` parameter is a Composio **FileUploadable**
  object `{name, mimetype, s3key}` (`additionalProperties:false`), OR a public `source_url`.
  The `s3key` must reference a file already in Composio's S3 store (per the schema:
  "typically returned from a previous download action"). A **local temp file cannot be
  turned into an s3key over the raw MCP transport** — that requires Composio's SDK-side file
  upload, which is not reachable via `COMPOSIO_MULTI_EXECUTE_TOOL`, and no `composio` SDK /
  file-store helper exists in this repo. There is no inline-content field (schema forbids
  extra keys). Therefore local-file upload is not achievable by the raw-MCP provider, and
  `files.download` / `files.trash` have no safe self-created artifact to act on.

This is exactly the "confirmed hard blocker" branch of the fix loop: the capability is set
False with a specific `UNSUPPORTED_REASONS` entry rather than left as an unverified True.

The Google family was **not touched** (byte-for-byte unchanged).

---

## 4. Capability matrix changes (`workspace_capabilities.py`)

`composio_microsoft` and `composio_microsoft:mcp` were both updated so each write is True
ONLY if it executed successfully live:

| Capability | Before (optimistic) | After (live-verified) | Why |
|---|---|---|---|
| `mail.draft`      | True  | **True**  | executed ✅ |
| `mail.trash`      | True  | **True**  | executed ✅ (move→deleteditems) |
| `calendar.create` | True  | **True**  | executed ✅ |
| `calendar.update` | True  | **True**  | executed ✅ |
| `calendar.delete` | True  | **True**  | executed ✅ |
| `mail.archive`    | True  | **False** | destination not exercised (only deleteditems ran) |
| `mail.unarchive`  | True  | **False** | destination not exercised |
| `mail.untrash`    | True  | **False** | destination not exercised |
| `files.upload`    | True  | **False** | live FAIL — FileUploadable/s3key blocker |
| `files.download`  | True  | **False** | not executed — blocked by upload |
| `files.trash`     | True  | **False** | not executed — no self-created artifact |
| `mail.send`       | False | **False** | policy (never send) |
| `calendar.cancel` | False | **False** | policy (no restore path) |
| `mail.tag*` / `mail.list_tags` | False | **False** | not exposed via Composio MCP |
| reads (`mail.search`, `calendar.list`, `files.search`) | True | **True** | execution-verified (v0.3.7 read run) |

Each newly-False write has a specific `UNSUPPORTED_REASONS` entry (both neutral and legacy
`drive.*`/`gmail.*` keys) citing this 2026-07-16 run; none fall back to the generic
"is not supported by ..." string.

---

## 5. Tests updated (to match LIVE-VERIFIED reality, not flipped blindly)

- `tests/test_workspace_capabilities.py` — renamed
  `test_composio_microsoft_writes_disabled_until_execution_verified` →
  `test_composio_microsoft_writes_reflect_live_execution`: asserts the executed writes are
  True and the blocked ones are False with a specific reason.
- `tests/test_doctor.py::test_doctor_microsoft_capability_key` — `mail.draft` /
  `calendar.create` now appear under **supported**; `files.upload` under **unsupported**.
- `tests/test_onboarding_fixes_v036.py::test_verify_cli_resolves_m365_via_env` — real break:
  `run_verification` gained `include_calendar_writes`; the `fake_run` mock signature now
  accepts it.
- `tests/test_composio_microsoft_v037.py::TestCapabilities` (3 tests) — updated to the
  live-verified supported/unsupported sets.
- `tests/test_composio_microsoft_cleanup_v039.py` (`TestCapabilitiesPhase1And2`,
  `TestVerifyWritesPhase2`) — caps reflect live reality; the mocked `--verify-writes` test
  now asserts `files_write` is `not_tested` and the OneDrive slugs are never called.

**Full suite:** `python -m pytest -q` → **1714 passed, 0 failed**. (The `_cffi_backend`
import error that was breaking the JWT/webhook/e2e crypto tests in this container was fixed
by reinstalling `cffi`; those tests are unrelated to this change and now pass.)

---

## 6. Handoff / recommendations

1. **OneDrive writes (upload/download/trash)** are the remaining gap. To support them the
   provider needs a Composio file-store upload step (obtain an `s3key`, or accept a
   `source_url`) — a real feature, out of scope for this verification. Until then they are
   honestly `False`.
2. **mail.archive / unarchive / untrash** share the execution-verified `OUTLOOK_MOVE_MESSAGE`
   slug (only the well-known destination differs). They stay `False` because the
   `archive`/`inbox` destinations weren't exercised in this conservative run; a follow-up
   that moves a throwaway draft through those folders (reversible, cleaned up) would let
   them be flipped True honestly.
3. **Executed & safe today:** mail draft, mail trash (soft), and the full calendar
   create/update/delete lifecycle — all cleaned up, no residue.
