# Chief of Staff Plugin — Setup Guide

## Quick Start

### Option 1: Google Service Account (advanced, self-hosted)

1. Install the plugin:
```bash
cd ~/.hermes/plugins
git clone https://github.com/moonlight-lupin/chief-of-staff-agent.git chief-of-staff
```

2. Run bootstrap:
```bash
python shared/scripts/bootstrap.py --company "Your Company" --jurisdiction SG --operator you@yourcompany.com
```

3. Set up Google service account:
   - Create a service account in [GCP Console](https://console.cloud.google.com/iam-admin/serviceaccounts)
   - Enable domain-wide delegation
   - Authorize scopes in [Google Workspace Admin Console](https://admin.google.com/ac/owl)
   - Download the JSON key file

4. Update `shared/config/company.yaml`:
```yaml
google:
  service_account_path: "~/.hermes/your_service_account.json"
  domain: "yourcompany.com"
  delegate_email: "you@yourcompany.com"
  account_alias: "yourcompany"  # matches --account flag in google_api.py

integrations:
  workspace:
    provider: google_api
    mode: direct
```

5. Verify:
```bash
python shared/scripts/doctor.py
python shared/scripts/connect_workspace.py --provider google_api
```

### Option 2: Composio MCP (easier onboarding, managed auth)

1. Install the plugin:
```bash
cd ~/.hermes/plugins
git clone https://github.com/moonlight-lupin/chief-of-staff-agent.git chief-of-staff
pip install requests
```

2. Get your Composio MCP key from [connect.composio.dev](https://connect.composio.dev)

3. Set it in `.env`:
```
COMPOSIO_MCP_KEY=your_key_here
```

4. Run bootstrap:
```bash
python shared/scripts/bootstrap.py --company "Your Company" --jurisdiction SG --operator you@yourcompany.com
```

5. Update `shared/config/company.yaml`:
```yaml
integrations:
  workspace:
    provider: composio
    mode: mcp
    mcp:
      endpoint: "https://connect.composio.dev/mcp"
      key_env: "COMPOSIO_MCP_KEY"
    user_id: "your-stable-user-id"  # e.g. "acme-alicia"
    toolkits:
      - gmail
      - googlecalendar
      - googledrive
    tools_allowlist:
      gmail:
        read:
          - GMAIL_FETCH_EMAILS
        write_safe:
          - GMAIL_CREATE_EMAIL_DRAFT
      googlecalendar:
        read:
          - GOOGLECALENDAR_FIND_EVENT
        write_safe:
          - GOOGLECALENDAR_CREATE_EVENT
      googledrive:
        read:
          - GOOGLEDRIVE_FIND_FILE
        write_safe:
          - GOOGLEDRIVE_UPLOAD_FILE
```

6. Connect Gmail and Calendar:
```bash
python shared/scripts/connect_workspace.py --provider composio --connect gmail
# Open the printed Connect Link in your browser

python shared/scripts/connect_workspace.py --provider composio --connect googlecalendar
# Open the printed Connect Link in your browser
```

7. Verify:
```bash
python shared/scripts/doctor.py
python shared/scripts/connect_workspace.py --status
```

8. Test the daily briefing:
```bash
python skills/daily-briefing/scripts/daily_briefing.py --dry-run --json
```

## Switching Providers

To switch from Google to Composio (or vice versa), just change `integrations.workspace.provider` in `company.yaml`. The Daily Briefing and all skills that use `WorkspaceClient` will automatically use the new provider.

## Doctor

Run `python shared/scripts/doctor.py` to check all components:

```
✅ workspace_provider: pass — composio mcp
✅ COMPOSIO_MCP_KEY: set
✅ mcp_initialize: pass
✅ meta_tools: COMPOSIO_MANAGE_CONNECTIONS, COMPOSIO_MULTI_EXECUTE_TOOL
✅ composio_user_id: acme-alicia
⚠️ gmail: not connected — run connect_workspace.py --provider composio --connect gmail
```

## Architecture

```
WorkspaceClient (ABC)
├── GoogleWorkspaceClient  (wraps google_api.py subprocess)
└── ComposioMCPWorkspaceClient  (routes through connect.composio.dev/mcp)
    └── MCPClient (JSON-RPC over SSE)
        ├── COMPOSIO_MANAGE_CONNECTIONS  (connect toolkits)
        └── COMPOSIO_MULTI_EXECUTE_TOOL  (execute tool by slug)
```

Skills call `get_workspace_client(config)` → get the right backend automatically.
## Migration from SDK mode (v0.1.8 and earlier)

If you previously used `provider: composio, mode: sdk`:

1. Change `mode` to `mcp` in `company.yaml`
2. Set `COMPOSIO_MCP_KEY` in `.env` (from https://connect.composio.dev)
3. Add the `mcp` section to your config:
   ```yaml
   mcp:
     endpoint: "https://connect.composio.dev/mcp"
     key_env: "COMPOSIO_MCP_KEY"
   ```
4. Run: `python connect_workspace.py --provider composio --connections`
5. Remove `composio-core` from your pip dependencies (no longer needed)

