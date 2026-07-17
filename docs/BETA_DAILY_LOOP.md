# Beta Daily Loop Guide (v0.3.18)

## Overview

`chief_of_staff.py daily` is the canonical Google-first beta morning command.
It is **read-only**: it never approves, executes, sends, or mutates provider /
local business state. It now also collects **live** Gmail + Calendar (and local
deadlines / pipeline / todos / invoices) via `daily_briefing.collect` without
recording delivery or writing `.last_briefing`.

## Beta readiness notes (v0.3.18)

Use this loop with **Google** (`google_api` or Composio Google). Microsoft /
Outlook live E2E (including email-org) is deferred when Entra ID is unavailable.

| Path | Status |
|---|---|
| Daily summary (live mail/calendar reads + local panels) | Ready (read-only) |
| Doctor / smoke-test / readiness | Ready |
| `document.handoff` (upload + draft) | Ready on `google_api`, Composio Google, Composio Microsoft |
| Soft-delete restore for Drive trash | Ready on Google (`files.untrash` / `drive_untrash`) |
| OneDrive `files.untrash` | Capability False — wired (Personal Graph + Business SharePoint recycle bin) but not live-verified |
| Live Outlook email-org E2E | Deferred (needs Entra ID) |

## Recommended first beta day (Google operator)

```bash
# Prefer python3 when `python` is not on PATH
python3 shared/scripts/chief_of_staff.py doctor --summary
python3 shared/scripts/chief_of_staff.py readiness --summary
python3 shared/scripts/chief_of_staff.py smoke-test --summary
python3 shared/scripts/chief_of_staff.py daily --summary
python3 shared/scripts/review_queue.py list --state requested
```

Google SA prerequisites (when `integrations.workspace.provider` is `google_api`
or unset):

- External `google-workspace` skill / `google_api.py` (or `GOOGLE_WORKSPACE_API`)
- `google.service_account_path` pointing at a real JSON key
- `google.account_alias` and `google.delegate_email`
- `queries.yaml` beside `company.yaml` or under `shared/config/`

Composio Google alternative: `COMPOSIO_MCP_KEY`, `integrations.workspace.user_id`,
and connected Gmail / Google Calendar / Google Drive toolkits.

## The daily command

```bash
# Human-readable summary (default)
python3 shared/scripts/chief_of_staff.py daily --summary

# Machine-readable JSON
python3 shared/scripts/chief_of_staff.py daily --json

# Markdown formatted
python3 shared/scripts/chief_of_staff.py daily --markdown
```

Full standalone briefing text (also read-only with `--dry-run`):

```bash
python3 skills/daily-briefing/scripts/daily_briefing.py --dry-run --render
```

## What it shows

1. **System health** — config loaded, paths resolved, state files readable
2. **Briefing** — live Gmail/Calendar/local source statuses + urgent items,
   plus local email-org / suggestion counts
3. **Needs review** — pending actions by state and risk
4. **Pipeline / CRM** — active deals, stale deals, recently moved
5. **Bookkeeper** — invoice candidates, duplicates, pending actions
6. **Knowledge** — memory records, wiki pages, lint warnings
7. **State safety** — stuck executing actions, malformed files, missing files
8. **Recommended next commands** — 3-7 prioritized commands

## What it does NOT do

- ❌ Approve or execute actions
- ❌ Send email or create drafts
- ❌ Mutate Gmail/Calendar/Drive (reads only)
- ❌ Write invoices.yaml or pipeline.yaml
- ❌ Delete or merge wiki pages
- ❌ Mark memory facts as confirmed
- ❌ Reset stuck executing actions
- ❌ Record briefing delivery / write `.last_briefing`

All mutations remain in specialized commands or approved Review Queue execution.

## How to read the output

### System health
```
1. System health
  config: ok
  project root: ok
  workspace provider: google_api
  state files: ok
```

### Briefing (live sources)
```
2. Briefing
  recent events (24h): 4
  live sources (2026-07-17):
    gmail: ok (3)
    calendar: ok (2)
    deadlines: ok (1)
    pipeline: ok (0)
    todos: ok (2)
    invoices: ok (0)
    email_org: ok (1)
  urgent items: 1
    - [high] Todo: Send NDA
  email classified: 12
  active suggestions: 2
```

When credentials or `google_api.py` are missing, Gmail/Calendar show
`failed` / `unavailable` but the rest of the daily panels still render.
`readiness` marks the daily loop row as **WARN** in that case (still
read-only-ready).

### Needs review
```
3. Needs review
  Requested: 3 (1 high, 2 medium)
  Approved: 1 (waiting for execution)
  Failed: 0
  → python3 shared/scripts/review_queue.py list --state requested
```

### Pipeline
```
4. Pipeline / CRM
  Active deals: 8
  Stale deals: 2
  Oldest stale: deal-acme — Proposal Sent, 21 days inactive
  Contract Signed without invoice: 1
```

### Bookkeeper
```
5. Bookkeeper
  Invoice candidates: 4
  Candidates needing review: 2
  Duplicate warnings: 1
```

### Knowledge
```
6. Knowledge maintenance
  Memory records: 15 total
  Stale records: 3
  Low-confidence: 1
  Broken wiki links: 2
```

## How to act on what you see

### Review pending actions
```bash
python3 shared/scripts/review_queue.py list --state requested
python3 shared/scripts/review_queue.py preview --action-id <ID>
python3 shared/scripts/review_queue.py approve --action-id <ID> --approver "MH" --reason "Checked"
python3 shared/scripts/review_queue.py execute --action-id <ID>
```

### Review invoice candidates
```bash
python3 skills/bookkeeper/scripts/invoice_ingest.py candidates --summary
python3 skills/bookkeeper/scripts/invoice_ingest.py validate
```

### Review stale deals
```bash
python3 skills/pipeline-manager/scripts/pipeline.py stale --summary
python3 skills/pipeline-manager/scripts/pipeline.py list --summary
```

### Check knowledge quality
```bash
python3 shared/scripts/memory.py lint --summary
python3 skills/note-taker/scripts/wiki_curator.py lint --summary
python3 shared/scripts/memory.py backup
```

## Subsystem summaries

Each subsystem can be queried individually:

```bash
python3 shared/scripts/chief_of_staff.py review --summary
python3 shared/scripts/chief_of_staff.py pipeline --summary
python3 shared/scripts/chief_of_staff.py bookkeeper --summary
python3 shared/scripts/chief_of_staff.py knowledge --summary
python3 shared/scripts/chief_of_staff.py doctor --summary
```

## Smoke test

```bash
python3 shared/scripts/chief_of_staff.py smoke-test --summary
```

Verifies all subsystems can render without crashing and that watched state /
business / wiki files are not written (including `pipeline.yaml`,
`invoices.yaml`, `todos.yaml`, and wiki pages).

## JSON schema

```json
{
  "version": "0.3.18",
  "generated_at": "2026-07-17T08:00:00+00:00",
  "mode": "daily",
  "safety": {
    "read_only": true,
    "approved_actions_executed": 0,
    "provider_writes": 0,
    "pipeline_mutations": 0,
    "invoice_writes": 0
  },
  "sections": {
    "system_health": {},
    "briefing": {
      "live": {
        "available": true,
        "sources": {
          "gmail": {"status": "ok", "count": 3},
          "calendar": {"status": "ok", "count": 2}
        },
        "urgent_count": 0
      }
    },
    "review_queue": {},
    "pipeline": {},
    "bookkeeper": {},
    "knowledge": {},
    "state": {},
    "recommended_commands": []
  }
}
```
