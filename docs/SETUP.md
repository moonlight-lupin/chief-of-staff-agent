# Chief of Staff Plugin — Setup Guide

## Fastest path

```bash
# 1. Clone into the Hermes plugins directory
mkdir -p ~/.hermes/plugins && cd ~/.hermes/plugins
git clone https://github.com/moonlight-lupin/chief-of-staff-agent.git chief-of-staff
cd chief-of-staff

# 2. Install dependencies
python -m pip install -r requirements.txt

# 3. Try it first — a sample-data daily brief, zero credentials
python shared/scripts/chief_of_staff.py demo

# 4. Bootstrap: pick a workspace provider and name your assistant
python shared/scripts/bootstrap.py \
  --company "Your Company" --jurisdiction SG \
  --operator you@yourcompany.com \
  --workspace-provider m365 \
  --assistant-name "Ada"

# 5. Set secrets in .env (auto-loaded from the plugin root) or the shell env
echo 'M365_CLIENT_SECRET=...' >> .env      # shell env wins if both are set

# 6. Connect and verify the workspace (no --config needed — auto-discovered)
python shared/scripts/connect_workspace.py --connect-m365
python shared/scripts/connect_workspace.py --verify

# 7. Readiness go/no-go
python shared/scripts/chief_of_staff.py readiness --summary
```

The detailed, provider-by-provider reference follows below.

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

3. Set it in `.env` in the plugin root (auto-loaded on every run; a value already
   exported in your shell takes precedence):
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

#### Microsoft 365 via Composio (easiest M365 onboarding — no Entra admin)

The same Composio managed-OAuth path also drives **Microsoft 365** — Outlook mail,
Outlook calendar, and OneDrive — with **no Entra app registration and no admin
consent**. You connect Outlook/OneDrive with a Connect Link, exactly like Gmail.
Select the toolkit *family* with one config key.

1. Bootstrap with the microsoft family:
```bash
python shared/scripts/bootstrap.py --company "Your Company" --jurisdiction SG \
  --operator you@yourcompany.com \
  --workspace-provider composio --composio-family microsoft \
  --composio-user-id your-stable-user-id
```
This writes `family: microsoft` and `toolkits: [outlook, one_drive]` under
`integrations.workspace`. (You can also set these by hand — the config below.)

2. Config (`shared/config/company.yaml`):
```yaml
integrations:
  workspace:
    provider: composio
    mode: mcp
    family: microsoft          # Outlook mail/calendar + OneDrive
    user_id: "your-stable-user-id"
    toolkits:
      - outlook
      - one_drive
    mcp:
      endpoint: "https://connect.composio.dev/mcp"
      key_env: "COMPOSIO_MCP_KEY"
    # Optional: override a tool slug if Composio renames one in their catalog.
    # tool_slugs:
    #   mail_search: OUTLOOK_OUTLOOK_LIST_MESSAGES
```

3. Connect Outlook and OneDrive (open each printed Connect Link in your browser):
```bash
python shared/scripts/connect_workspace.py --provider composio --connect outlook
python shared/scripts/connect_workspace.py --provider composio --connect one_drive
```

4. Verify / readiness (read verification on a live connection):
```bash
python shared/scripts/connect_workspace.py --status          # shows family + per-toolkit state
python shared/scripts/connect_workspace.py --verify          # per-capability go/no-go
python shared/scripts/connect_workspace.py --provider composio --capabilities
```

> **Verification note.** The Composio Microsoft tool slugs were **verified against
> Composio's live catalog (2026-07-13)**, and the three reads — `mail_search`
> (`OUTLOOK_QUERY_EMAILS`), `calendar_list` (`OUTLOOK_GET_CALENDAR_VIEW`), and
> `files_search` (`ONE_DRIVE_SEARCH_ITEMS`) — are **execution-verified** against a
> live Outlook + OneDrive connection (`read_ready: true`).
>
> **Cleanup + writes (v0.3.9 Phase 1+2)** are capability-True for Composio
> Microsoft:
>
> - Cleanup: `mail.archive` / `mail.trash` / restore via `OUTLOOK_MOVE_MESSAGE`
>   (well-known `archive`, `deleteditems`, `inbox` — not permanent
>   `OUTLOOK_DELETE_MESSAGE`); `files.trash` via `ONE_DRIVE_DELETE_ITEM`.
> - Content writes use **Composio catalog arg shapes** (not raw Graph JSON):
>   `OUTLOOK_CREATE_DRAFT` (`to_recipients` / `body`+`is_html`),
>   `OUTLOOK_CALENDAR_CREATE_EVENT` (`start_datetime`/`time_zone`/`attendees_info`),
>   `OUTLOOK_UPDATE_CALENDAR_EVENT`, `ONE_DRIVE_ONEDRIVE_UPLOAD_FILE` (`file`/`folder`),
>   `ONE_DRIVE_DOWNLOAD_FILE` (`item_id`/`file_name`).
>
> Run write smoke after connect:
> ```bash
> python shared/scripts/connect_workspace.py --verify-writes
> # optional calendar create→update→delete of a marked [CoS verify] event:
> python shared/scripts/connect_workspace.py --verify-calendar-writes
> ```
> `--verify-writes` creates a draft and a tiny OneDrive file, then trashes both
> (tags stay unsupported until Phase 4 — the draft is still cleaned up). Calendar
> probe is opt-in because delete is destructive for the artefact just created.
>
> Every slug remains **config-overridable** via `integrations.workspace.tool_slugs`
> in case Composio renames one: a wrong slug reports *itself*, naming the failing
> slug and the exact `tool_slugs` key to fix. Gmail-syntax queries are translated
> to Outlook automatically
> (`in:inbox`, `is:unread`, `from:`, `newer_than:` …); a dict query with a
> `raw: {m365: {...}}` override is passed through verbatim. `mail.send` is
> intentionally disabled; categories/cancel are not yet exposed via Composio
> (capabilities report them honestly as unsupported).

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
5. Configure `company.yaml` by running bootstrap (writes the `integrations.workspace`
   and `m365` blocks for you — no hand-editing of YAML):
   ```bash
   python shared/scripts/bootstrap.py \
     --company "Your Company" --jurisdiction SG \
     --operator you@yourcompany.com \
     --workspace-provider m365 \
     --m365-auth client_credentials \
     --tenant-id "<directory-tenant-guid>" \
     --client-id "<application-client-guid>" \
     --user-principal "cos@yourtenant.com"
   ```
   - `--m365-auth client_credentials` (app-only): operates on `/users/{user_principal}/...`; `--user-principal` is required.
   - `--m365-auth device_code`: interactive delegated sign-in; a code + URL are printed to stderr on first token request; operates on `/me/...` (omit `--user-principal`).
   - Bootstrap never writes the secret to `company.yaml`; it only records the env-var
     name (default `M365_CLIENT_SECRET`, override with `--m365-secret-env`).
6. Set the secret. Put it in `.env` in the plugin root (auto-loaded on every run) or
   export it in your shell — **the shell environment wins if both are set**:
   ```bash
   echo 'M365_CLIENT_SECRET=<the-secret-value>' >> .env   # auto-loaded
   # or, for the current shell only:
   export M365_CLIENT_SECRET="<the-secret-value>"
   ```
7. Connect and verify (no `--config` needed — `company.yaml` is auto-discovered):
   ```bash
   python shared/scripts/connect_workspace.py --provider m365 --status
   python shared/scripts/connect_workspace.py --connect-m365   # connect guidance
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

## Verify your workspace

A single health check is not enough on real tenants: Entra permissions and
admin consent can be **partially** configured (mail reads fine while OneDrive
403s, drafts work while categories are blocked). `--verify` probes each
capability independently and reports `pass` / `fail` / `not_tested` per check.

```bash
# Read-only verification (safe; no writes)
python shared/scripts/connect_workspace.py --verify

# Read verification + opt-in non-destructive write smoke checks
python shared/scripts/connect_workspace.py --verify-writes

# Machine-readable JSON (either mode)
python shared/scripts/connect_workspace.py --verify --json
```

`--verify` and `--verify-writes` honour `--provider <name>` to check a specific
provider, and `--json` to emit the report as JSON (default is the human layout).

Sample output:

```
Workspace verification — provider: m365
Read ready:  no
Write ready: partial

Authentication
  ✓ auth — authenticated

Mail
  ✓ mail_read — 1 result(s)
  ✓ mail_folder_scoped — 1 result(s)
  ✓ mail_tags_list (optional) — 3 result(s)

Calendar
  ✓ calendar_read — 2 result(s)

OneDrive/Files
  ✗ files_read — m365 files_search failed: Graph API 403: Access denied

Writes
  — mail_draft — verification never sends mail
  — mail_tag_write — provider does not support mail tag write
  — files_write — provider does not support files.upload
  — mail_send — verification never sends mail
  — calendar_write — verification never creates calendar events
```

In the sample above the OneDrive read 403s, so `Read ready` is `no`: `files_read`
is one of the reads the product depends on.

The exit code is `0` when the provider is read-ready — that is, `auth`,
`mail_read`, **folder-scoped mail search** (`mail_folder_scoped`),
`calendar_read` and `files_read` all pass. `mail_tags_list` is **optional**: its
failure does not affect read-readiness (email organisation features are merely
degraded), and it is marked `(optional)` in the report. For `--verify-writes` the
exit code is `0` only if additionally no tested write (or its cleanup) failed.
Otherwise it is `1`.

Notes:
- **`--verify-writes` never sends mail and never creates calendar events.** It
  creates a draft, applies a tag, and uploads a tiny temp file, then trashes the
  draft and the file. `mail_send` and `calendar_write` are always reported
  `not_tested`.
- **Composio Microsoft write verification is unsupported today.** Its write
  slugs are catalog/schema-verified but not execution-verified, and the verifier
  skips write checks when cleanup capabilities are missing. Do not treat
  `--verify-writes` as Microsoft Composio write acceptance.
- **Writes are only attempted when they can be cleaned up.** `mail_draft` (and
  the tag write that tags it) is skipped as `not_tested` unless the provider
  supports `mail.trash`; `files_write` is skipped unless it supports
  `files.trash`. This avoids leaving verification artefacts behind. If a write
  succeeds but its cleanup fails, that check is reported `fail` (manual removal
  required) and write-readiness becomes `no`.
- **The `CoS-Verify` category/label persists.** The write smoke reuses one
  category across runs (it is not deleted); only the verification draft and
  uploaded file are cleaned up.

## ESign Connector (DocuSeal)

The eSign Connector skill sends documents for e-signature via a self-hosted
DocuSeal instance. It uses **two API tokens** — no browser login or admin
password required.

### Prerequisites (all three are required)

1. **Self-hosted DocuSeal instance, reachable by external signers.**
   - Deploy DocuSeal (Docker) on a server or NAS.
   - Expose it via a public domain or tunnel (e.g. Cloudflare Tunnel, ngrok,
     Tailscale Funnel) so signers outside your network can access signing links.
   - The instance URL (e.g. `https://sign.yourdomain.com`) must be reachable
     from both the plugin's host and the signer's browser.

2. **SMTP configured in DocuSeal.**
   - DocuSeal sends signing request emails to signers. Without SMTP, no emails
     are sent and the signing flow is broken.
   - Configure in DocuSeal Settings → Email → SMTP.
   - Common setup: Google Workspace SMTP relay (`smtp-relay.gmail.com:587`,
     STARTTLS, your workspace credentials).

3. **Two API tokens created in DocuSeal.**
   - **MCP token**: Settings → MCP Server → create token.
     Used for: `create_template`, `search_templates`, `search_documents`.
   - **API key**: Settings → API → create access token.
     Used for: `PATCH /api/templates/{id}` (field placement),
     `POST /api/submissions` (send to signers), `GET /api/submissions/{id}`
     (status), `DELETE /api/submissions/{id}` (cancel),
     `GET /api/submissions/{id}/documents` (download signed).
   - These are **different credentials** with different headers: MCP uses
     `Authorization: Bearer` on `/mcp`; API key uses `X-Auth-Token` on `/api/*`.

### Setup

1. Install DocuSeal (if not already running):
   ```bash
   # Docker example — see DocuSeal docs for full setup
   docker run -d --name docuseal \
     -p 3000:3000 \
     -e HOST=https://sign.yourdomain.com \
     docuseal/docuseal
   ```

2. Create the two tokens in DocuSeal Settings (see prerequisites above).

3. Add tokens to `.env` in the plugin root (never in company.yaml). The `.env` is
   auto-loaded on every run; a value already exported in your shell takes precedence:
   ```bash
   DOCUSEAL_MCP_TOKEN=your_mcp_token_here
   DOCUSEAL_API_KEY=your_api_key_here
   ```

4. Run bootstrap with `--esign-url`:
   ```bash
   python shared/scripts/bootstrap.py \
     --company "Your Company Pte Ltd" \
     --jurisdiction SG \
     --operator you@yourdomain.com \
     --esign-url https://sign.yourdomain.com
   ```

   Or manually edit `shared/config/company.yaml` → `esign` section:
   ```yaml
   esign:
     provider: docuseal
     url: "https://sign.yourdomain.com"
     domain: "sign.yourdomain.com"
     provider_email: "you@yourdomain.com"
     provider_role: "Service Provider"
     client_role: "Client"
     auth_mode: auto
     file_serving:
       mode: existing
       public_base_url: null
       cleanup_after_send: true
     defaults:
       signing_order: random
       cancel_before_resend: true
     field_detection:
       prefer: auto
       page_indexing: zero_based
   ```

   Migration note: older configs with `esign.admin_email` still work, but rename
   it to `esign.provider_email` when you next edit `company.yaml`.

5. Verify:
   ```bash
   python shared/scripts/doctor.py
   ```
   The doctor checks DocuSeal connectivity, verifies both tokens are present
   (based on `auth_mode`), and validates the API key against `GET /api/templates`.
   If the API key is invalid (HTTP 401/403), the doctor reports **fail**.
   If a required token is missing for the configured auth mode, it reports **fail**.

### How it works

```
Document (.docx from Document Preparer or external PDF)
  → LibreOffice headless → PDF (if .docx)
  → PyMuPDF merge (if multi-doc, e.g. T&Cs + SOW)
  → Serve PDF via HTTPS (file_serving config)
  → MCP create_template(name, url)           → empty template
  → GET /api/templates/{id}                  → extract attachment_uuid + submitter uuid
  → Coordinate extraction (sign_detector.py or ODL+pdfplumber)
  → Normalize to 0-1 top-down coordinates
  → PATCH /api/templates/{id} with fields    → (API key, X-Auth-Token)
  → Verify fields (count, uuids, unique names, submitter uuids)
  → POST /api/submissions                     → send to signers (API key)
  → GET /api/submissions/{id}                 → track status
  → GET /api/submissions/{id}/documents       → download signed PDF
  → Drive Filer files to client folder
```

### Self-hosted DocuSeal CE vs Pro

| Feature | CE (free) | Pro |
|---------|-----------|-----|
| `POST /api/templates/pdf` (create template with fields in one call) | ❌ Pro-gated | ✅ |
| MCP `create_template` (empty template) | ✅ | ✅ |
| `PATCH /api/templates/{id}` (add fields) | ✅ (API key) | ✅ |
| `POST /api/submissions` (send to signers) | ✅ (API key) | ✅ |
| `POST /api/submissions/pdf` (one-off from PDF) | ❌ Pro-gated | ✅ |

On CE free, the flow uses MCP to create the template + API key to PATCH fields
(two tokens). On Pro, you can use a single API key for everything via
`POST /templates/pdf` (set `auth_mode: pro_api_only` in company.yaml).

### Self-Sign vs eSign Connector

| User intent | Skill |
|---|---|
| "Sign this", "add my signature" | `self-sign` (offline, pure Python) |
| "Send to client/vendor/director for signature" | `esign-connector` (DocuSeal) |
| Generated doc where user signs locally only | `self-sign` |
| Generated doc requiring remote counterparty signatures | `esign-connector` |

Both skills share `self-sign/scripts/sign_detector.py` for signature location
detection. Self-sign places a local image; esign-connector normalizes to
DocuSeal coordinates and uploads.

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
- For the Microsoft Composio family, Outlook/OneDrive reads are
  execution-verified, but write capabilities are catalog/schema-verified only
  and currently reported unsupported.

**Microsoft 365 (Graph):**
- Outlook mail search/draft/send/archive/trash, categories (tags), calendar, and OneDrive files.
- Drafts supported; send is destructive and blocked by default (`CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1`).
- No calendar uncancel (recreate the event). Simple upload only (< 4 MB). Polling only (no Graph webhooks yet).
- **Request resilience:** Graph calls retry with a method-aware policy (up to 3 times). Throttling (HTTP 429) retries for every method, since Graph documents a throttled request as not processed. The retryable 503/504 auto-retry only for idempotent methods (GET/PUT/DELETE); a 504 on a non-idempotent write (POST/PATCH — send, draft, event create, move, category) is ambiguous (the write may have completed), so it is not retried automatically and surfaces a verify-first error (guarded writes become an audited-failure result). The server's `Retry-After` header is honored in full and never shortened: a value within the 30s budget is slept and retried, while a longer value is deferred (raised as a throttled/retry-later error) rather than slept short and retried. When the header is absent or invalid the wait falls back to 1s/2s/4s exponential backoff (that fallback alone is capped at 30s). A 401 triggers a one-time token refresh and retry. List/search results are paginated automatically via `@odata.nextLink`, bounded by your `max_results` (mail/file search) or internal caps of 500 items / 10 pages (calendar and tag listings); each nextLink is origin-checked (must be https on the Graph host) before it is followed, and a cap or a rejected link emits a warning. No configuration required — always on.
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
