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

### Option 3: Microsoft 365 (Graph API)

Talks to Outlook mail, Outlook calendar, and OneDrive over the Microsoft Graph
REST API v1.0 using `requests` directly. Auth uses `msal`.

1. Register an app in **Microsoft Entra ID** (Azure AD):
   - Azure Portal → **Microsoft Entra ID** → **App registrations** → **New registration**.
   - Note the **Application (client) ID** and **Directory (tenant) ID**.
2. Grant **application** permissions (for `client_credentials`):
   - App registration → **API permissions** → **Add a permission** → **Microsoft Graph** → **Application permissions**:
     - `Mail.ReadWrite`
     - `Calendars.ReadWrite`
     - `Files.ReadWrite.All`
     - `User.Read.All`
   - Click **Grant admin consent** for your tenant (an admin must approve).
   - (For `device_code` mode use the equivalent **Delegated** permissions instead; no admin consent secret is required.)
3. Create a **client secret** (`client_credentials` only):
   - App registration → **Certificates & secrets** → **New client secret**.
   - Copy the secret **value** and export it as an env var (default `M365_CLIENT_SECRET`).
4. Install the auth dependency:
   ```bash
   pip install msal
   ```
5. Configure `company.yaml`:
   ```yaml
   integrations:
     workspace:
       provider: m365

   m365:
     tenant_id: "<directory-tenant-guid>"
     client_id: "<application-client-guid>"
     client_secret_env: "M365_CLIENT_SECRET"   # env var holding the secret (default)
     auth: "client_credentials"                # or "device_code"
     user_principal: "cos@yourtenant.com"      # mailbox UPN — REQUIRED for client_credentials
     token_cache_path: "~/.hermes/secrets/m365-token-cache.json"  # optional
   ```
   - `client_credentials` (app-only): operates on `/users/{user_principal}/...`; `user_principal` is required.
   - `device_code`: interactive delegated sign-in; a code + URL are printed to stderr on first token request; operates on `/me/...`.
6. Set the secret in your environment:
   ```bash
   export M365_CLIENT_SECRET="<the-secret-value>"
   ```
7. Verify:
   ```bash
   python shared/scripts/connect_workspace.py --provider m365 --status
   python shared/scripts/connect_workspace.py --provider m365 --connect   # connect guidance
   python shared/scripts/doctor.py
   ```

**Provider notes:**
- **Drafts are supported** (`POST /messages`).
- **Sending is destructive** and env-gated identically to Gmail send — set `CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1` to send.
- **Tags = Outlook categories.** The tag id IS the category `displayName` (there is no separate opaque id). Applying a tag fetches the message's current categories and appends.
- **Immutable ids.** Every Graph request sends `Prefer: IdType="ImmutableId"`. Graph message ids normally CHANGE when a message moves folders (archive/trash); immutable ids keep them stable so the soft-delete restore flow (which restores by the original/persisted id) still resolves after a move. As belt-and-braces, `mail_archive`/`mail_trash` also return a `restore_target` (the post-move id) in their result, and the generic restore flow prefers that persisted value over the original action target.
- **`calendar.cancel` is NOT supported for m365.** Graph cannot reinstate a cancelled event and the recreate-event workflow is not implemented, so cancel has no restore path and must not be offered behind the reversible soft-delete promise. The capability is `False`, `calendar_cancel` returns a failure `ActionResult` (it does not raise) explaining to cancel via Outlook or delete+recreate, and the generic execute path refuses approved m365 `calendar.cancel` actions pre-execution via `require_capability`. (`calendar_uncancel` also raises `NotImplementedError`.)
- **Upload is simple upload only** (`PUT .../content`), limited to files **< 4 MB**; larger files need an upload session (deferred).
- **Polling only** — Graph change-notification webhooks are deliberately deferred for this phase.

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
├── ComposioMCPWorkspaceClient  (routes through connect.composio.dev/mcp)
│   └── MCPClient (JSON-RPC over SSE)
│       ├── COMPOSIO_MANAGE_CONNECTIONS  (connect toolkits)
│       └── COMPOSIO_MULTI_EXECUTE_TOOL  (execute tool by slug)
└── M365GraphClient  (Microsoft Graph REST v1.0 via requests + msal)
    ├── _get_token()  (msal ConfidentialClientApplication / device flow)
    └── _request()    (single HTTP seam — Outlook mail, calendar, OneDrive)
```

Skills call `get_workspace_client(config)` → get the right backend automatically.

## Provider differences

**Google service-account:**
- Good for read/search, Calendar actions, Drive actions.
- Gmail send exists but is destructive and blocked by default (`CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1`).
- Gmail draft is not currently supported (google_api.py has no draft subcommand).
- Best for: enterprise Google ops with service-account delegation.

**Composio MCP:**
- Supports Gmail search and draft creation.
- Supports Calendar and Drive actions.
- Recommended provider for document handoff workflows (upload + draft email).
- Best for: managed-auth workflows that need Gmail drafts.

**Microsoft 365 (Graph):**
- Outlook mail search/draft/send/archive/trash, categories (tags), calendar, and OneDrive files.
- Drafts supported; send is destructive and blocked by default (`CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1`).
- No calendar uncancel (recreate the event). Simple upload only (< 4 MB). Polling only (no Graph webhooks yet).
- **Request resilience:** Graph calls transparently retry on throttling (HTTP 429) and the retryable 503/504 up to 3 times, honoring the server's `Retry-After` header (falling back to 1s/2s/4s exponential backoff, capped at 30s per wait). A 401 triggers a one-time token refresh and retry. List/search results are paginated automatically via `@odata.nextLink`, bounded by your `max_results` (mail/file search) or internal caps of 500 items / 10 pages (calendar and tag listings); when a cap truncates results a warning is emitted. No configuration required — always on.
- Best for: Microsoft 365 / Outlook / OneDrive tenants.

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

