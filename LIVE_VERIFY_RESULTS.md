# Live Verify Results — Composio Microsoft 365 (Outlook + OneDrive)

**Date:** 2026-07-13
**Provider:** `composio_microsoft:mcp` (family `microsoft`, mode `mcp`)
**Endpoint:** `https://connect.composio.dev/mcp`
**Key:** `COMPOSIO_MCP_KEY` — supplied via shell env only, **REDACTED**, never written to
config or committed.
**Config used:** `/tmp/live/company.yaml` (not committed; `paths.project_root=/tmp/live/project_root`).
**Mode:** READ-ONLY. No writes attempted — `--verify-writes` was **not** run; no mail sent;
nothing created/modified in Outlook or OneDrive.

> Privacy note: this report contains **counts and shapes only**. No real email subjects,
> addresses, bodies, event titles, or file names from the mailbox/drive are included.
> The connected Outlook account's owner identity is redacted.

---

## 1. Outcome summary

| Verify check        | Result | Notes |
|---------------------|--------|-------|
| auth                | ✅ pass | MCP session initialized |
| mail_read           | ✅ pass | live Outlook data (count-only below) |
| mail_folder_scoped  | ✅ pass | `in:inbox` folder scope honored |
| calendar_read       | ✅ pass | live Outlook calendar data |
| files_read          | ⛔ fail | **hard blocker** — OneDrive not connected |
| mail_tags_list      | ⚠️ fail (optional) | provider has no Outlook categories/tags op; does NOT block read_ready |
| mail_draft / mail_tag_write / files_write | — not_tested | writes not requested (read-only run) |
| mail_send / calendar_write | — not_tested | verification never sends/creates |

`read_ready: false` **solely** because of the OneDrive hard blocker. The three target
read checks the task asked to get passing: **mail_search ✅, calendar_read ✅, files_read
⛔ (hard blocker, not a code defect)**. `write_ready: partial` (writes not requested).

---

## 2. Connection states (COMPOSIO_MANAGE_CONNECTIONS)

| Toolkit    | Active connection? | State |
|------------|--------------------|-------|
| `outlook`  | ✅ **yes**         | 1 default account `ACTIVE` (owner identity redacted); a couple of extra accounts left in `initiated`/`initializing` from prior link attempts |
| `one_drive`| ❌ **no**          | all accounts `initiated` / `initializing` (OAuth link pending — never completed); `active_connections: 0` |

`connect_workspace.py --status` (redacted):

```json
{
  "provider": "composio", "mode": "mcp", "mcp_key_set": true,
  "user_id": "serio-vealer", "family": "microsoft",
  "connections": { "outlook": {"status": "connected"},
                   "one_drive": {"status": "pending"} },
  "healthy": true
}
```

`user_id` was resolved by probing `COMPOSIO_MANAGE_CONNECTIONS` and taking the entity
behind the **active** Outlook connection (account handle `outlook_serio-vealer`).

---

## 3. Root cause found: catalog slug drift + wrong argument names

The v0.3.7 Microsoft `FAMILY_SLUGS` defaults used a **doubled toolkit prefix**
(`OUTLOOK_OUTLOOK_*`, `ONE_DRIVE_ONE_DRIVE_*`) that **no real Composio slug carries**,
and two operations also used the **wrong argument names**. Real slugs/args were
discovered from the live catalog via `COMPOSIO_SEARCH_TOOLS` +
`COMPOSIO_GET_TOOL_SCHEMAS`, and the reads were execution-verified against the active
Outlook connection.

### 3a. Slug corrections — `shared/scripts/providers/composio_mcp_workspace.py` (`FAMILY_SLUGS["microsoft"]`)

| Operation          | Before (broken)                     | After (real)                    | How verified |
|--------------------|-------------------------------------|---------------------------------|--------------|
| `mail_search`      | `OUTLOOK_OUTLOOK_LIST_MESSAGES`     | `OUTLOOK_QUERY_EMAILS`          | **executed** ✅ returned live messages |
| `calendar_list`    | `OUTLOOK_OUTLOOK_GET_CALENDAR_VIEW` | `OUTLOOK_GET_CALENDAR_VIEW`     | **executed** ✅ returned live events |
| `files_search`     | `ONE_DRIVE_ONE_DRIVE_FIND_FILE`     | `ONE_DRIVE_SEARCH_ITEMS`        | catalog (SEARCH_TOOLS primary); exec blocked by no OneDrive conn |
| `mail_create_draft`| `OUTLOOK_OUTLOOK_CREATE_DRAFT`      | `OUTLOOK_CREATE_DRAFT`          | schema (GET_TOOL_SCHEMAS) — not executed (read-only) |
| `calendar_create`  | `OUTLOOK_OUTLOOK_CREATE_EVENT`      | `OUTLOOK_CALENDAR_CREATE_EVENT` | catalog (SEARCH_TOOLS primary) — not executed |
| `calendar_update`  | `OUTLOOK_OUTLOOK_UPDATE_EVENT`      | `OUTLOOK_UPDATE_CALENDAR_EVENT` | catalog (SEARCH_TOOLS related) — not executed |
| `files_upload`     | `ONE_DRIVE_ONE_DRIVE_UPLOAD_FILE`   | `ONE_DRIVE_ONEDRIVE_UPLOAD_FILE`| catalog (SEARCH_TOOLS primary) — not executed |
| `files_download`   | `ONE_DRIVE_ONE_DRIVE_DOWNLOAD_FILE` | `ONE_DRIVE_DOWNLOAD_FILE`       | schema (GET_TOOL_SCHEMAS) — not executed |

### 3b. Argument-name corrections

| Operation       | Arg before → after | Reason (from live schema) |
|-----------------|--------------------|---------------------------|
| `calendar_list` | `startDateTime` → `start_datetime`, `endDateTime` → `end_datetime` | `OUTLOOK_GET_CALENDAR_VIEW` requires **snake_case** ISO datetimes; camelCase keys are unknown → would fail `required` validation. |
| `calendar_list` | dropped `max_results` | not a real arg; pagination is `top`. |
| `mail_search`   | dropped `max_results` (kept `top`) | `OUTLOOK_QUERY_EMAILS` paginates via `top`; `max_results` is not a real arg (tolerated/ignored, removed for cleanliness). |
| `files_search`  | `query` → `q`, `max_results` → `top` | `ONE_DRIVE_SEARCH_ITEMS` requires `q` (search text); `query`/`max_results` are unknown → missing-`q` failure. |

Google-family slugs and argument names are **untouched** (byte-for-byte); all google
compatibility tests still pass.

### 3c. Response-envelope extraction fix (the false-pass bug)

Before the fix `files_read` **falsely passed with 0 results** even though OneDrive is not
connected. Cause: `_execute_composio_tool` only inspected `results[0]["response"]["error"]`,
but a **batch-level** failure (e.g. no active connection) puts the error directly at
`results[0]["error"]` **with no `"response"` wrapper**. That real error was replaced by a
generic `"tool execution failed"` string and then normalized to `[]`, so the verifier saw
an empty-but-clean read and passed it.

Fix:
- `_execute_composio_tool` now reads the error from `results[0]["response"]["error"]`
  **or** `results[0]["error"]` **or** the batch-level `data.error`, so the real message is
  never masked.
- Added `_is_connection_error()` + `ComposioConnectionError`: a no-active-connection error
  is now **raised** (like the existing unknown-tool path), so reads warn (verification
  honestly **fails** the check) and guarded writes surface it as the `ActionResult` error —
  instead of silently certifying a capability the drive cannot serve.
- Generic/transient errors (e.g. rate-limit) still swallow to `[]` with no warning
  (unchanged; `test_non_unknown_error_is_not_enriched` still green).

---

## 4. Verified read shapes (counts/shapes only — no content)

- **Execution transport:** `COMPOSIO_MULTI_EXECUTE_TOOL` with
  `{"tools":[{"tool_slug": ..., "arguments": ...}]}`; success at
  `data.results[0].response.successful == true`, payload at `...response.data`.
- **mail_search** (`OUTLOOK_QUERY_EMAILS`): response envelope `{ "@odata.context",
  "@odata.nextLink", "next_page_token", "value": [ ... ] }`. Records Graph-shaped
  (fields: `id, from, subject, receivedDateTime, conversationId, bodyPreview, categories,
  hasAttachments, isRead, ...`). `mail_read` (`is:unread`) → **1** record; `mail_folder_scoped`
  (`in:inbox`) → **1** record. Normalized to canonical `schemas.validate_message` shape
  (`id, sender, subject, date, source="outlook", ...`).
- **calendar_read** (`OUTLOOK_GET_CALENDAR_VIEW`): same `{ "value": [ ... ] }` envelope.
  Today→tomorrow window → **6** events. Records Graph-shaped (`id, subject, start, end,
  attendees, organizer, location, onlineMeeting, showAs, isCancelled, ...`) → normalized to
  canonical `schemas.validate_event` shape.
- The `_ms_extract_records` extractor already handled the Graph `value` array, so no
  extractor change was needed for the two working reads.

---

## 5. Remaining failures (raw error text)

### 5a. `files_read` — HARD BLOCKER (OneDrive not connected)

Raw error surfaced by the fixed code:

```
Composio MCP files_search failed: Composio reports no active connection for the toolkit
backing slug 'ONE_DRIVE_SEARCH_ITEMS' (operation 'files_search'): No active connection
found for toolkit(s) 'one_drive' in this session. To fix this, call
COMPOSIO_MANAGE_CONNECTIONS with toolkits=['one_drive'] to establish a connection, then
retry this tool call.. Connect the toolkit (connect_workspace.py --provider composio
--connect <toolkit>) and wait for it to become active, then retry.
```

Underlying batch envelope observed live:

```json
{"data":{"results":[{"error":"No active connection found for toolkit(s) 'one_drive' in this session. ...","tool_slug":"ONE_DRIVE_SEARCH_ITEMS","index":0}],"total_count":1,"success_count":0,"error_count":1},"error":"1 out of 1 tools failed","successful":false}
```

**This is not a code defect.** The OneDrive slug (`ONE_DRIVE_SEARCH_ITEMS`) and args
(`q`, `top`) are now correct; the check will pass once OneDrive has an **active**
connection. Completing that requires the operator to finish the OAuth link
(`connect_workspace.py --provider composio --connect one_drive`, then complete the browser
flow) — outside the scope of a read-only, credential-only run.

### 5b. `mail_tags_list` — OPTIONAL, does not block read_ready

```
ComposioMCPWorkspaceClient does not support mail_list_tags; email organisation features
will be degraded
```

Expected/by-design: the Composio Microsoft provider exposes no Outlook categories/tags
list operation. `mail_tags_list` is explicitly optional in `workspace_verify` and does not
gate `read_ready`.

---

## 6. Tests

- Updated Microsoft-family expectations to the verified-real slugs/args in
  `tests/test_composio_microsoft_v037.py`:
  - `mail_search` slug → `OUTLOOK_QUERY_EMAILS`; asserts `top` present and `max_results` absent.
  - `calendar_list` slug → `OUTLOOK_GET_CALENDAR_VIEW`; asserts snake_case `start_datetime`/`end_datetime`, no `startDateTime`.
  - `files_search` slug → `ONE_DRIVE_SEARCH_ITEMS`; asserts `q == "NDA"`, no `query`.
  - Updated unknown-tool mocked slug strings to real slugs (`OUTLOOK_QUERY_EMAILS`, `OUTLOOK_CREATE_DRAFT`).
  - **New** `TestNoActiveConnection`: proves the batch-level no-connection envelope
    surfaces (read warns + returns `[]`; `_execute_composio_tool` raises
    `ComposioConnectionError`) instead of false-passing.

```
python -m pytest tests/test_composio_microsoft_v037.py tests/test_composio_mcp_workspace.py -q
=> 42 passed
```

- Full suite: `1589 passed, 20 failed`. **All 20 failures are pre-existing and unrelated**
  — they are JWT/OIDC/webhook crypto tests failing on `ModuleNotFoundError: No module named
  '_cffi_backend'` in this environment. Verified identical failures on the pristine baseline
  with my changes stashed. No composio/workspace/verify/query_compiler test regressed.

---

## 7. Files changed

- `shared/scripts/providers/composio_mcp_workspace.py` — Microsoft `FAMILY_SLUGS` defaults
  (8 slugs), `calendar_list`/`files_search`/`_ms_mail_search_args` argument names,
  response-envelope extraction + `ComposioConnectionError`/`_is_connection_error`.
- `tests/test_composio_microsoft_v037.py` — updated expectations to verified-real
  slugs/args + new no-active-connection coverage.
- `LIVE_VERIFY_RESULTS.md` — this report.

## 8. Handoff / next step for the operator

1. Connect OneDrive: `CHIEF_OF_STAFF_CONFIG=/tmp/live/company.yaml python
   shared/scripts/connect_workspace.py --provider composio --connect one_drive`, open the
   returned link, finish OAuth, wait for `active`.
2. Re-run `--verify`; `files_read` should then pass (slug/args are already correct) and
   `read_ready` should flip to `true`.
3. Outlook mail + calendar reads are already live-verified and require no further action.
