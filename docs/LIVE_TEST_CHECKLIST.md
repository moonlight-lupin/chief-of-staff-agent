# Live Test Checklist

Two providers are supported: **Composio MCP** and **Google service-account**.
Test the active provider configured in `company.yaml` under `integrations.workspace.provider`.

## Prerequisites (both providers)

- [ ] `company.yaml` exists and passes `python shared/scripts/doctor.py`
- [ ] `google.delegate_email` set in config
- [ ] `google.account_alias` set in config (for google_api provider)

---

## Section A: Composio MCP Live Test

### Prerequisites

- [ ] `COMPOSIO_MCP_KEY` set in `.env`
- [ ] `integrations.workspace.provider: composio` in `company.yaml`
- [ ] `integrations.workspace.mode: mcp`
- [ ] `integrations.workspace.user_id` set
- [ ] `integrations.workspace.mcp.endpoint` set to `https://connect.composio.dev/mcp`
- [ ] `integrations.workspace.mcp.key_env` set to `COMPOSIO_MCP_KEY`

### 1. MCP Initialize

```bash
python shared/scripts/connect_workspace.py --provider composio --mcp-url
```

Expected: Endpoint + key set + Initialized: ✅

### 2. Connect Toolkits

```bash
python shared/scripts/connect_workspace.py --provider composio --connect gmail
python shared/scripts/connect_workspace.py --provider composio --connect googlecalendar
python shared/scripts/connect_workspace.py --provider composio --connect googledrive
```

### 3. Check Connections

```bash
python shared/scripts/connect_workspace.py --provider composio --connections
```

Expected: gmail, googlecalendar, googledrive all connected.

### 4. MCP Info

```bash
python shared/scripts/connect_workspace.py --provider composio --mcp-info
```

### 5. Live Tool Tests

```bash
python shared/scripts/connect_workspace.py --provider composio --test gmail
python shared/scripts/connect_workspace.py --provider composio --test googlecalendar
python shared/scripts/connect_workspace.py --provider composio --test googledrive
```

### 6. Doctor (Composio mode)

```bash
python shared/scripts/doctor.py
```

Expected:
- ✅ workspace_provider: composio mcp
- ✅ COMPOSIO_MCP_KEY: set
- ✅ mcp_initialize: pass
- ⚠️ google_auth: skipped — active provider is composio

### 7. Skill Tests (Composio)

```bash
# Calendar scan (read)
python skills/calendar-manager/scripts/calendar_actions.py scan --today

# Drive search (read)
python skills/drive-filer/scripts/drive_file.py search --query "" --max 3

# Meeting prep gather (read)
python skills/meeting-prep/scripts/workspace_actions.py gmail-context --query "is:unread" --max 2

# Calendar create (write) — requires auto-approve
CHIEF_OF_STAFF_AUTO_APPROVE=1 python skills/calendar-manager/scripts/calendar_actions.py create \
  --title "Test Event" --start 2026-07-15 --end 2026-07-15

# Gmail draft (write) — requires auto-approve
CHIEF_OF_STAFF_AUTO_APPROVE=1 python skills/document-preparer/scripts/document_actions.py draft-email \
  --to test@example.com --subject "Test" --body "Test body"

# Document handoff (write) — requires auto-approve
CHIEF_OF_STAFF_AUTO_APPROVE=1 python skills/document-preparer/scripts/document_actions.py handoff \
  --file /tmp/test.docx --to client@example.com --subject "NDA" --body "Please review"

# Summary mode
CHIEF_OF_STAFF_AUTO_APPROVE=1 python skills/calendar-manager/scripts/calendar_actions.py --summary create \
  --title "Test" --start 2026-07-15 --end 2026-07-15
```

### 8. Guardrails (Composio)

```bash
# Safe write blocked without auto-approve (non-TTY)
python skills/calendar-manager/scripts/calendar_actions.py create \
  --title "Test" --start 2026-07-15 --end 2026-07-15
# Expected: ❌ cancelled by guardrail

# Destructive action blocked even with auto-approve
CHIEF_OF_STAFF_AUTO_APPROVE=1 python -c "
import sys; sys.path.insert(0, 'shared/scripts')
from workspace_guardrails import confirm_action
print(confirm_action('gmail.send'))
"
# Expected: False
```

---

## Section B: Google Service-Account Live Test

### Prerequisites

- [ ] `google.account_alias` set in `company.yaml` (e.g. "phronesis")
- [ ] `google.service_account_path` points to a valid JSON key file
- [ ] `google.delegate_email` set (the user to impersonate)
- [ ] External `google-workspace` skill installed with matching account alias
- [ ] No `integrations.workspace.provider` set (defaults to google_api), or set to `google_api`

Note: Google service-account mode requires the external google-workspace skill
and a working google_api.py account alias.

### 1. Doctor (Google mode)

```bash
python shared/scripts/doctor.py
```

Expected:
- ✅ workspace_provider: google_api direct
- ✅ google_workspace_skill: installed
- ✅ google_api_script: found
- ✅ google_service_account_file: found
- ✅ google_account_alias: phronesis
- ✅ google_delegate_email: menghuey@...
- ✅ google_auth: calendar list succeeded through --account phronesis --as menghuey@...

### 2. Calendar Scan (read)

```bash
python skills/calendar-manager/scripts/calendar_actions.py scan --today
```

Expected: JSON array of Meet-enabled events (or empty array if no events).

### 3. Drive Search (read)

```bash
python skills/drive-filer/scripts/drive_file.py search --query "test" --max 5
```

### 4. Calendar Create (write)

```bash
CHIEF_OF_STAFF_AUTO_APPROVE=1 python skills/calendar-manager/scripts/calendar_actions.py create \
  --title "Test Event" --start 2026-07-10 --end 2026-07-10
```

### 5. Gmail Draft — Not supported in Google service-account mode

Google service-account mode does not currently support Gmail draft creation
because the external google_api.py script has no draft subcommand.
Use Composio MCP for draft-email and document handoff workflows.

### 6. Summary Mode

```bash
CHIEF_OF_STAFF_AUTO_APPROVE=1 python skills/calendar-manager/scripts/calendar_actions.py --summary create \
  --title "Test Event" --start 2026-07-10 --end 2026-07-10
```

Expected:
```
✅ Calendar event created: Test Event
Provider: google_api
Audited: yes
```

### 7. Document Handoff — NOT supported (without --allow-partial)

Full document handoff (upload + Gmail draft) is not supported because
google_api.py has no draft subcommand. Use Composio MCP for full handoff.

With `--allow-partial`, Google can upload the file and return a clear
"draft unsupported" result:

```bash
CHIEF_OF_STAFF_AUTO_APPROVE=1 python skills/document-preparer/scripts/document_actions.py handoff \
  --file /tmp/test.docx --parent <folder_id> \
  --to client@example.com --subject "NDA" --body "Please review." --allow-partial
```

Note: Gmail draft and document handoff require Composio MCP provider.
Google service-account mode does not support Gmail draft creation.