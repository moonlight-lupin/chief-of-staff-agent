# Live Test Checklist

Use this checklist to verify the Chief of Staff plugin works end-to-end with Composio MCP.

## Prerequisites

- [ ] `COMPOSIO_MCP_KEY` set in `.env`
- [ ] `integrations.workspace.provider: composio` in `company.yaml`
- [ ] `integrations.workspace.mode: mcp` in `company.yaml`
- [ ] `integrations.workspace.user_id` set in `company.yaml`
- [ ] `integrations.workspace.mcp.endpoint` set to `https://connect.composio.dev/mcp`
- [ ] `integrations.workspace.mcp.key_env` set to `COMPOSIO_MCP_KEY`

## 1. MCP Initialize

```bash
python shared/scripts/connect_workspace.py --provider composio --mcp-url
```

Expected:
- ✅ Endpoint: `https://connect.composio.dev/mcp`
- ✅ Key set: ✅
- ✅ Initialized: ✅ (session: UUID)

## 2. Connect Toolkits

```bash
python shared/scripts/connect_workspace.py --provider composio --connect gmail
python shared/scripts/connect_workspace.py --provider composio --connect googlecalendar
python shared/scripts/connect_workspace.py --provider composio --connect googledrive
```

Expected: Connect link printed for each. Open in browser to authorize.

## 3. Check Connections

```bash
python shared/scripts/connect_workspace.py --provider composio --connections
```

Expected:
- ✅ gmail: connected
- ✅ googlecalendar: connected
- ✅ googledrive: connected

## 4. MCP Info

```bash
python shared/scripts/connect_workspace.py --provider composio --mcp-info
```

Expected:
- Provider: composio
- Mode: mcp
- MCP init: ✅
- Meta tools: COMPOSIO_MANAGE_CONNECTIONS, COMPOSIO_MULTI_EXECUTE_TOOL
- Enabled tools listed per toolkit

## 5. Debug Tool (Payload Validation)

```bash
python shared/scripts/connect_workspace.py --provider composio --debug-tool gmail
python shared/scripts/connect_workspace.py --provider composio --debug-tool googlecalendar
python shared/scripts/connect_workspace.py --provider composio --debug-tool googledrive
```

Expected for each:
- MCP session established
- Meta tools listed
- Tool slug tested with payload `{"tools": [{"tool_slug": "...", "input": {...}}]}`
- Response keys and normalized result shown

## 6. Live Tool Tests

```bash
python shared/scripts/connect_workspace.py --provider composio --test gmail
python shared/scripts/connect_workspace.py --provider composio --test googlecalendar
python shared/scripts/connect_workspace.py --provider composio --test googledrive
```

Expected:
- ✅ Gmail: got N unread emails
- ✅ Calendar: got N events in next 7 days
- ✅ Drive: got N files

## 7. Doctor

```bash
python shared/scripts/doctor.py
```

Expected:
- ✅ composio: mode: mcp; COMPOSIO_MCP_KEY: set; mcp_initialize: pass; meta_tools: ...
- ✅ gmail: connected
- ✅ googlecalendar: connected
- ✅ googledrive: connected
- Capabilities reported as `composio:mcp`

## 8. Guardrails

```bash
# Safe write action (draft) should proceed with auto-approve
CHIEF_OF_STAFF_AUTO_APPROVE=1 python -c "
import sys; sys.path.insert(0, 'shared/scripts')
from workspace_guardrails import confirm_action
print(confirm_action('gmail.draft', to='test@example.com'))
"
# Expected: True

# Destructive action should be blocked even with auto-approve
CHIEF_OF_STAFF_AUTO_APPROVE=1 python -c "
import sys; sys.path.insert(0, 'shared/scripts')
from workspace_guardrails import confirm_action
print(confirm_action('gmail.send'))
"
# Expected: False (requires CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1)
```

## 9. Daily Briefing

```bash
python shared/scripts/doctor.py  # verify all green
# Trigger the briefing cron or run directly:
python skills/daily-briefing/scripts/daily_briefing.py
```

Expected: Briefing includes Gmail unread count and Calendar events for today.