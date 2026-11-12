---
name: notion-sync
description: "Use when mirroring the Chief-of-Staff pipeline (deals) or todos to a Notion database, or bringing Notion-side edits and new Notion tasks back into the plugin for approval. The plugin store is the source of truth; Notion is a mirror."
version: 0.1.0
author: moonlight-lupin
license: Apache-2.0
metadata:
  hermes:
    tags: [chief-of-staff, notion, sync, pipeline, todos]
    related_skills: [pipeline-manager, todo-list, notion-wiki]
---

# Notion Sync

## Overview

Keeps a Notion database in step with the plugin's pipeline or todos. The
plugin's store is the source of truth. Notion is a mirror that people can
also edit. Nothing moves in either direction without an approved action.

| Situation since last sync | What the plan proposes |
|---|---|
| Store changed, Notion did not | `notion.db.upsert`: you write it to Notion (agent path) |
| Notion changed, store did not | `notion.pull`: applied to the store after approval (Python path) |
| Both changed | Conflict: no action; ask the user which side is right |
| Record removed from the store | `notion.page.archive`: high risk, needs explicit approval |
| New Notion row with no `CoS ID` | `notion.pull` create: a new deal or task, after approval |

Only these Notion edits can come back. Pipeline: Stage, Value, Contact,
Email. Todos: Name, Status, Priority, Due. Edits to any other field are
reported as `ignored` and left alone. Values the store would reject (an
unknown stage, a bad status) are reported as `rejected`.

## Setup

`company.yaml`:

```yaml
notion:
  databases:
    pipeline: <database id>
    todos: <database id>
accounts:
  notion: <the Notion workspace your connector should act as>
```

Each database needs a `CoS ID` text property, plus the columns in
`shared/scripts/notion_mirror.py` → `PROPERTY_MAP`.

## Workflow

1. **Read** the Notion database with your own Notion connector. Write an
   envelope with each row's `page_id`, `last_edited_time` and its properties
   as plain values (text, number, ISO date, list of strings):

   ```json
   {"domain": "pipeline", "rows": [{"page_id": "…", "last_edited_time": "…",
     "properties": {"CoS ID": "deal-001", "Name": "…", "Stage": "Lead"}}]}
   ```

2. **Plan**, which is read-only. Show the user the summary:

   ```bash
   .venv/bin/python skills/notion-sync/scripts/notion_sync.py plan --input rows.json --summary
   ```

3. **Queue** once the user agrees, by adding `--queue`. This creates pending
   actions only. Re-running it never duplicates an open action.

4. **Approve.** Upserts are low risk, so the user may bulk-approve them with
   `review_queue.py approve --all --risk low --type notion.db.upsert --approver "<who>"
   --confirm-low-risk-bulk --reason "…"`. Pulls and archives are approved one
   at a time.

5. **Execute.**
   - `notion.pull`: `review_queue.py execute --action-id <id>`.
   - `notion.db.upsert` / `notion.page.archive`: `review_queue.py claim`, then
     do it with your Notion connector, then `record-execution --result-json
     '{"page_id": "…", "last_edited_time": "…"}' --executor-account <account>`.
     **Always include `page_id`.** Without it the row is not mirrored and the
     next sync proposes it again.
   - Before an update, check the page's `last_edited_time` against the
     payload's `expected_last_edited`. If it moved, someone edited the page
     after planning. Record a failure (`--status failure --error "Notion
     changed since plan"`) and plan again rather than overwriting their edit.

## Rules

- Never write to Notion without a claimed action, and never edit the store
  directly to "fix" a conflict. Show the user both values and let them choose.
- Under multiple companies, sync only the active company's databases
  (`--company <slug>`).
