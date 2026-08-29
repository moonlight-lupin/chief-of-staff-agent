# Workspace Access

Shared ladder for Chief of Staff skills that read or write mail, calendar, or files. Skills state *what* to obtain; this file states *how*.

Normalize every record to the canonical shapes in `shared/scripts/schemas.py` (`normalize_workspace_payload` / `validate_workspace_payload` — the normalize-to-schemas step):

- Mail → `message`: `{id, sender, subject, date, thread_id?, snippet?, tags?, has_attachments?, link?, source?}`
- Calendar → `event`: `{id, title, start, end, attendees?, organizer?, location?, conference_link?, status?, source?}`
- Files → `file`: `{id, name, mime_type?, modified?, link?, parents?, source?}`

Resolve account/delegate identity from `company.yaml` (`google.account`, `google.delegate_email`) when the chosen path needs it. **All connector and provider paths must resolve to the `company.yaml` workspace account — never brief, read, or write from the agent's personal mailbox.**

## Access ladder

Use the first available path:

1. **Agent-side connectors** — native Gmail / Google Calendar / Drive / Microsoft 365 connectors, **or an already-authed Hermes Composio MCP session** (`GMAIL_FETCH_EMAILS` / `OUTLOOK_QUERY_EMAILS`, `GOOGLECALENDAR_FIND_EVENT` / `OUTLOOK_GET_CALENDAR_VIEW`, and the equivalent file-search tools). Map results to the canonical shapes. When the Composio slugs are already known (pinned in `tools_allowlist` in `company.yaml`), call `COMPOSIO_MULTI_EXECUTE_TOOL` directly with those slugs, batching independent reads into one call — do NOT route through `COMPOSIO_SEARCH_TOOLS` discovery first; it returns ~54 KB of schemas and adds a session-ID failure mode with no benefit when slugs are known. Use `COMPOSIO_SEARCH_TOOLS` only as a fallback when a slug errors as unknown/renamed (the provider surfaces this well via `ComposioToolError`, which names the slug and the `tool_slugs` override path). Prefer this path when Hermes already has Composio connected: use it as a **read front-end only**; CoS writes still go through `get_workspace_client`.
2. **The configured workspace provider** via `shared/scripts/workspace_client.py`: `get_workspace_client(config)` then `.mail_search(query, max_results=...)`, `.calendar_list(start, end)`, `.files_search(...)`, and the matching write methods (`.calendar_create` / `.calendar_update` / `.calendar_cancel`, `.files_upload` / `.files_download` / `.files_trash`, mail draft/send/archive/tag). The provider is selected by `integrations.workspace.provider` in `company.yaml` (`google_api` | `composio` | `m365` | `agent`); all non-`agent` providers expose the same neutral method surface. Use this path for any **writes** so `@guarded` + audit apply.
3. **Pre-fetched data via `--input`** — when the agent has already gathered workspace data with path 1, hand it to the skill script as a `schemas.py` workspace envelope (`{generated_at?, source?, messages: [...], events: [...], files: [...]}`). Set `source` to `"agent"` or `"composio-mcp"` as appropriate.

Skill wrappers `daily_briefing.py`, `workspace_actions.py`, and `workspace_collect.py` accept a pre-fetched `--input` envelope. Wrappers `calendar_actions.py` and `drive_file.py` route through `WorkspaceClient` directly and do not accept `--input`.

## Gmail-dialect note

Query templates in `shared/config/queries.yaml` are written in the **Gmail search dialect**. Native Gmail connectors and `google_api` accept them as-is; the `m365` provider translates the same intent to Microsoft Graph (`$search`/`$filter`); native connectors may take natural-language equivalents. Preserve the *intent* of each template regardless of provider.

File-store search examples use the Google Drive query dialect; the `m365` provider translates the same intent to Microsoft Graph, and native connectors accept natural-language or structured queries.

## Source-trust note

When `chief_of_staff.py daily` marks a live source `unavailable` while a direct Composio read of the same source succeeds, trust the direct records and disclose the adapter gap in the brief — do not fail closed or redirect the agent to a different connector.
