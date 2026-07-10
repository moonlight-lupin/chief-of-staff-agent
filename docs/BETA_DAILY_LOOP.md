# Beta Daily Loop Guide (v0.3.0)

## Overview

The `chief_of_staff.py` entrypoint is the friendly front door to all Chief-of-Staff subsystems. One command gives you a complete read-only picture of your operating state.

## The daily command

```bash
# Human-readable summary (default)
python shared/scripts/chief_of_staff.py daily --summary

# Machine-readable JSON
python shared/scripts/chief_of_staff.py daily --json

# Markdown formatted
python shared/scripts/chief_of_staff.py daily --markdown
```

## What it shows

1. **System health** — config loaded, paths resolved, state files readable
2. **Briefing** — recent events, email activity, calendar
3. **Needs review** — pending actions by state and risk
4. **Pipeline / CRM** — active deals, stale deals, recently moved
5. **Bookkeeper** — invoice candidates, duplicates, pending actions
6. **Knowledge** — memory records, wiki pages, lint warnings
7. **State safety** — stuck executing actions, malformed files, missing files
8. **Recommended next commands** — 3-7 prioritized commands

## What it does NOT do

- ❌ Approve or execute actions
- ❌ Send email or draft email
- ❌ Mutate Gmail/Calendar/Drive
- ❌ Write invoices.yaml or pipeline.yaml
- ❌ Delete or merge wiki pages
- ❌ Mark memory facts as confirmed
- ❌ Reset stuck executing actions
- ❌ Any provider writes

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

### Needs review
```
3. Needs review
  Requested: 3 (1 high, 2 medium)
  Approved: 1 (waiting for execution)
  Failed: 0
  → python shared/scripts/review_queue.py list --state requested
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
python shared/scripts/review_queue.py list --state requested
python shared/scripts/review_queue.py preview --action-id <ID>
python shared/scripts/review_queue.py approve --action-id <ID> --approver "MH" --reason "Checked"
python shared/scripts/review_queue.py execute --action-id <ID>
```

### Review invoice candidates
```bash
python skills/bookkeeper/scripts/invoice_ingest.py candidates --summary
python skills/bookkeeper/scripts/invoice_ingest.py validate
```

### Review stale deals
```bash
python skills/pipeline-manager/scripts/pipeline.py stale --summary
python skills/pipeline-manager/scripts/pipeline.py list --summary
```

### Check knowledge quality
```bash
python shared/scripts/memory.py lint --summary
python skills/note-taker/scripts/wiki_curator.py lint --summary
python shared/scripts/memory.py backup
```

## Subsystem summaries

Each subsystem can be queried individually:

```bash
python shared/scripts/chief_of_staff.py review --summary
python shared/scripts/chief_of_staff.py pipeline --summary
python shared/scripts/chief_of_staff.py bookkeeper --summary
python shared/scripts/chief_of_staff.py knowledge --summary
python shared/scripts/chief_of_staff.py doctor --summary
```

## Smoke test

```bash
python shared/scripts/chief_of_staff.py smoke-test --summary
```

Verifies all subsystems can render without crashing and no writes occur.

## JSON schema

```json
{
  "version": "0.3.0",
  "generated_at": "2026-07-10T08:00:00+08:00",
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
    "briefing": {},
    "review_queue": {},
    "pipeline": {},
    "bookkeeper": {},
    "knowledge": {},
    "state": {},
    "recommended_commands": []
  }
}
```