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

**OneDrive restore (`composio_microsoft` — v0.3.20):**
- **Personal** — `ONE_DRIVE_RESTORE_DRIVE_ITEM` by drive item id
- **Business / work** — SharePoint recycle-bin restore (Personal Graph returns
  *"Operation not supported"* on work accounts). Connect the **SharePoint**
  toolkit (`share_point` is in the microsoft bootstrap toolkits). Optionally set
  `integrations.workspace.sharepoint_site_name` to your OneDrive personal path
  (e.g. `/personal/user_contoso_com`); otherwise it is derived from the item
  `webUrl`. `files_trash` persists the recycle-bin GUID as `restore_target`;
  `delete_actions.py restore` prefers that GUID. Same-session
  `files_untrash(drive_item_id)` falls back to LeafName lookup after Personal
  Graph fails.
- **`m365`** — SharePoint REST `RecycleBin/RestoreByIds` is wired but capability
  stays **False** until a live Entra run (needs SharePoint
  `Sites.ReadWrite.All` + host-scoped SPO token)

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
| drive.untrash / files.untrash | ✅ (SA REST) | ✅ | ✅ Composio MS (Business: connect `share_point`); ❌ m365 (wired, not live-verified) |
| document.handoff | ✅ | ✅ | ✅ |

Destructive / gated writes still require: prepare → approve → execute (or
`CHIEF_OF_STAFF_AUTO_APPROVE=1` for safe writes in non-interactive contexts).
