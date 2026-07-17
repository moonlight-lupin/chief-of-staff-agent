# Operator Runbook — Approval Queue and Soft-Delete

## Overview

Chief-of-Staff gates all destructive actions through an approval queue.
No user-facing workflow calls provider methods directly.

## State Machine

```
                    ┌──────────┐
                    │ requested │ ← prepare creates this
                    └─────┬─────┘
                          │
           ┌──────────────┼──────────────┐
           │              │              │
     approve           cancel         expires
           │              │           (72h stale)
           ▼              ▼
    ┌──────────┐   ┌───────────┐
    │ approved │   │ cancelled  │
    └─────┬────┘   └───────────┘
          │
     mark_executing
     (checks expiry
      BEFORE provider)
          │
     ┌────┴─────┐
     │          │
  success    failure
     │          │
     ▼          ▼
┌──────────┐  ┌──────────┐
│ executed │  │ approved │ ← back to approved for retry
└────┬─────┘  └──────────┘
     │
  restore
 (where available)
     │
     ▼
 action reversed
```

## Timeouts

| State | Expiry |
|---|---|
| `requested` → `expired` | 72 hours after creation |
| `approved` → `expired` | 24 hours after approval (if not executed) |

Expired actions cannot be approved or executed. They must be re-prepared.

## Commands

### Gmail Send (approval-gated)

```bash
# 1. Prepare — creates pending action
python send_email.py prepare --to client@x.com --subject "NDA" --body "Please sign..."

# 2. Preview — safe view (no execution)
python send_email.py preview --action-id <id>

# 3. Approve — explicit confirmation with metadata
python send_email.py approve --action-id <id> --approver "MH" --reason "Client confirmed"

# 4. Execute — sends the email (requires approved state)
python send_email.py execute --action-id <id>

# List pending actions
python send_email.py list --state requested
python send_email.py list --state approved

# Cancel
python send_email.py cancel --action-id <id> --reason "Wrong recipient"

# Summary — counts by state + high-risk items
python send_email.py summary
```

### Soft-Delete Actions (approval-gated)

```bash
# Prepare — reason is required
python delete_actions.py prepare --action-type gmail.archive --target <msg_id> --reason "Old email"
python delete_actions.py prepare --action-type gmail.trash --target <msg_id> --reason "Spam"
python delete_actions.py prepare --action-type drive.trash --target <file_id> --reason "Outdated"
python delete_actions.py prepare --action-type calendar.cancel --target <event_id> --reason "Meeting cancelled"

# Dry-run and preflight
python delete_actions.py prepare --action-type gmail.trash --target <id> --reason "Test" --dry-run
python delete_actions.py prepare --action-type drive.trash --target <id> --reason "Test" --preflight

# Approve, execute, cancel — same as send_email
python delete_actions.py approve --action-id <id> --approver "MH" --reason "Confirmed"
python delete_actions.py execute --action-id <id>
python delete_actions.py cancel --action-id <id> --reason "Changed mind"

# Restore — reverse a previously executed soft-delete
python delete_actions.py restore --action-id <id>

# List and summary
python delete_actions.py list --state executed
python delete_actions.py summary
```

### Restore Capabilities

| Action | Restore method | Status |
|---|---|---|
| `gmail.archive` | `gmail_unarchive()` — add INBOX label back | ✅ Wired |
| `gmail.trash` | `gmail_untrash()` — remove TRASH label | ✅ Wired |
| `calendar.cancel` | `calendar_uncancel()` — set status confirmed | ✅ Wired (google_api only) |
| `drive.trash` | `drive_untrash()` / `files_untrash()` | ✅ Wired for Google + Composio Microsoft; `m365` wired but capability **False** |

**Google Drive restore:** `files.untrash` calls Drive REST `files.update`
(`trashed=False`) on `google_api` (SA + delegate; no `google_api.py` CLI) and
`GOOGLEDRIVE_UNTRASH_FILE` on Composio Google. Soft-delete restore is available
via `delete_actions.py restore` for executed `drive.trash` actions.

**OneDrive restore — wired but capability False (NOT live-verified):**
`files.untrash` stays **False** for `composio_microsoft` and `m365`. A
2026-07-17 live probe on a real OneDrive-for-Business account — **with the
SharePoint toolkit connected and a correct `/personal/…` `site_name`** — still
failed end-to-end: the `ONE_DRIVE_DELETE_ITEM`'d file never surfaced in
`SHARE_POINT_LIST_RECYCLE_BIN_ITEMS` (0 items after 45s), so `files_trash`
captured no `restore_target` and `files_untrash` returned `success=False`
(Personal Graph *"Operation not supported"*, recycle-bin fallback empty). The
OneDrive recycle bin and the queried SharePoint recycle bin do not line up. Flip
to True only after a live run actually restores a file.
- **Personal** — `ONE_DRIVE_RESTORE_DRIVE_ITEM` by drive item id (wired)
- **Business / work** (`composio_microsoft`) — SharePoint recycle-bin restore
  scoped to the OneDrive `/personal/…` site (from `webUrl` or
  `integrations.workspace.sharepoint_site_name`). `files_trash` is meant to
  persist the recycle-bin GUID as `restore_target`, but the live probe showed
  the deleted item is not visible to the SharePoint listing — wired, not working.
- **`m365`** — SharePoint REST `RecycleBin/RestoreByIds` wired (needs SharePoint
  `Sites.ReadWrite.All` + host-scoped SPO token); never run live

### Handling Failed Actions

When a provider call fails (exception or error), the action transitions:
`executing → approved` (back to approved for retry).

```bash
# Check if any actions failed (look for last_error and retry_count)
python send_email.py list --state approved

# Retry by executing again
python send_email.py execute --action-id <id>

# Or cancel if the issue is permanent
python send_email.py cancel --action-id <id> --reason "Provider unavailable"
```

The 24-hour approval expiry still applies to retried actions. If the approval
has lapsed since the original approval, `mark_executing()` will reject the
retry and mark the action as expired.

### Pending Action Maintenance

Old executed/cancelled/expired actions accumulate in `.pending_actions.json`.

**CLI (recommended):**
```bash
# Remove actions older than 30 days
python skills/document-preparer/scripts/delete_actions.py cleanup --days 30

# Summary mode
python skills/document-preparer/scripts/delete_actions.py --summary cleanup --days 30
```

**Python API (advanced):**
```python
from pending_actions import cleanup_old_actions
cleanup_old_actions(config, days=30)  # removes actions > 30 days old
```

## Risk Classification

Email recipients are classified on prepare:
- **internal** — same domain as company/Google workspace (no warning)
- **external** — different domain (⚠️ warning: "verify recipient before approving")
- **unknown** — invalid email

Risk is shown in `list`, `preview`, and `summary` output.

## Audit Trail

All state transitions are audited via `workspace_audit.py`:

| Transition | Audit status |
|---|---|
| prepare | `requested` |
| approve | `approved` (with approver + reason) |
| cancel | `cancelled` (with reason) |
| mark_executing | `executing` |
| mark_executed | `executed` (with result) |
| mark_failed | `failed` (with error) |
| expired | `expired` (with reason: `approval_lapsed` or stale) |

## Provider Capability Matrix

| Action | Google SA | Composio Google | Composio Microsoft |
|---|---|---|---|
| gmail.search / mail.search | ✅ | ✅ | ✅ |
| gmail.draft / mail.draft | ✅ (SA REST) | ✅ | ✅ |
| gmail.send / mail.send | ✅ (gated) | ✅ (gated) | ✅ (gated) |
| gmail.archive / mail.archive | ✅ | ✅ | ✅ |
| gmail.trash / mail.trash | ✅ | ✅ | ✅ |
| calendar.list | ✅ | ✅ | ✅ |
| calendar.create | ✅ | ✅ | ✅ |
| calendar.update | ✅ | ✅ | ✅ |
| calendar.cancel | ✅ (gated) | ❌ | ❌ |
| drive.search / files.search | ✅ | ✅ | ✅ |
| drive.upload / files.upload | ✅ | ✅ (text + binary) | ✅ (text + binary) |
| drive.download / files.download | ✅ | ✅ | ✅ |
| drive.trash / files.trash | ✅ | ✅ | ✅ |
| drive.untrash / files.untrash | ✅ (SA REST) | ✅ | ❌ Composio MS + m365 (wired Personal Graph + Business SharePoint recycle bin, but not live-verified) |
| document.handoff | ✅ | ✅ | ✅ |

Destructive / gated writes still require: prepare → approve → execute (or
`CHIEF_OF_STAFF_AUTO_APPROVE=1` for safe writes in non-interactive contexts).
