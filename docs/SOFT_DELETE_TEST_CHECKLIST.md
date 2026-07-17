# Live Test Checklist — Soft-Delete and Restore

## Prerequisites

- Google service account configured (`account_alias`, `delegate_email`)
- `CHIEF_OF_STAFF_AUTO_APPROVE=1` environment variable set
- Config at `~/.hermes/plugins/chief-of-staff/shared/config/company.yaml`

## Section A: Gmail Archive → Restore

### A1. Prepare archive
```bash
python skills/document-preparer/scripts/delete_actions.py --config <CONFIG> prepare \
  --action-type gmail.archive --target <message_id> --reason "Live test: archive"
```
Expected: JSON with `state: "requested"`, `type: "gmail.archive"`

### A2. Preview
```bash
python skills/document-preparer/scripts/delete_actions.py --config <CONFIG> preview \
  --action-id <id>
```
Expected: Safe view with `Reversible: True`, `Restore: Remove archive label...`

### A3. Approve
```bash
python skills/document-preparer/scripts/delete_actions.py --config <CONFIG> approve \
  --action-id <id> --approver "test" --reason "Live test"
```
Expected: `state: "approved"`

### A4. Execute
```bash
python skills/document-preparer/scripts/delete_actions.py --config <CONFIG> execute \
  --action-id <id>
```
Expected: `success: true`, `action: "gmail.archive"`, `audited: true`

### A5. Restore
```bash
python skills/document-preparer/scripts/delete_actions.py --config <CONFIG> restore \
  --action-id <id>
```
Expected: `success: true`, `action: "gmail.unarchive"`
Verify in Gmail: message should be back in INBOX.

## Section B: Gmail Trash → Restore

### B1-B4. Same as A1-A4 but with `--action-type gmail.trash`
### B5. Restore
```bash
python skills/document-preparer/scripts/delete_actions.py --config <CONFIG> restore \
  --action-id <id>
```
Expected: `success: true`, `action: "gmail.untrash"`
Verify in Gmail: message should be out of TRASH.

## Section C: Calendar Cancel → Restore

### C1. Prepare
```bash
python skills/document-preparer/scripts/delete_actions.py --config <CONFIG> prepare \
  --action-type calendar.cancel --target <event_id> --reason "Live test: cancel"
```

### C2-C4. Preview, Approve, Execute (same pattern as A)

### C5. Restore
```bash
python skills/document-preparer/scripts/delete_actions.py --config <CONFIG> restore \
  --action-id <id>
```
Expected: `success: true`, `action: "calendar.uncancel"`
Verify in Google Calendar: event status should be back to confirmed.

## Section D: Drive / OneDrive Trash + Restore

Provider: `google_api`, Composio Google (`composio` / `composio:mcp`), or
Composio Microsoft (`composio_microsoft` — v0.3.20).
OneDrive Business restore requires the **SharePoint** toolkit connected and
preferably `sharepoint_site_name: /personal/…` (auto-derived from `webUrl`
when unset). Native `m365` untrash stays capability False until an Entra live
run (`Sites.ReadWrite.All` + host-scoped SPO token).

### D1. Prepare
```bash
python skills/document-preparer/scripts/delete_actions.py --config <CONFIG> prepare \
  --action-type drive.trash --target <file_id> --reason "Live test: trash"
```

### D2-D4. Preview, Approve, Execute (same pattern as A)

### D5. Restore
```bash
python skills/document-preparer/scripts/delete_actions.py --config <CONFIG> restore \
  --action-id <id>
```
Expected: `success: true`, restore via `drive_untrash` / `files_untrash`.
- Google: Drive REST `trashed=False` / `GOOGLEDRIVE_UNTRASH_FILE`
- OneDrive Personal: Graph / `ONE_DRIVE_RESTORE_DRIVE_ITEM`
- OneDrive Business: SharePoint recycle-bin GUID (`restore_target` from trash)

Verify in Drive/OneDrive: file is no longer in Trash / Recycle bin.

## Section E: Expiry and Failed Actions

### E1. Test approved expiry (simulated)
1. Prepare and approve an action
2. Wait 24+ hours (or manually age `approved_at` in `.pending_actions.json`)
3. Try to execute:
```bash
python skills/document-preparer/scripts/delete_actions.py --config <CONFIG> execute \
  --action-id <id>
```
Expected: `rc: 1`, "cannot be executed (approval may have lapsed)"
Verify: action state should be `expired`, provider method NEVER called.

### E2. Test failed action retry
1. Prepare, approve, and execute an action with a bad target ID
2. Check state — should be back to `approved` with `last_error` set
3. Retry:
```bash
python skills/document-preparer/scripts/delete_actions.py --config <CONFIG> execute \
  --action-id <id>
```
Expected: Retry attempts execution again (if approval not expired).

## Section F: Cleanup

### F1. Clean up old actions
```python
from pending_actions import cleanup_old_actions
cleanup_old_actions(config, days=30)
```
Expected: Returns count of removed actions (executed/cancelled/expired > 30 days old).
