---
name: pipeline-manager
description: "Use when managing the Chief-of-Staff CRM pipeline: add deals, move stages, list/show deal details, add notes, link documents, and detect stale opportunities from project YAML storage."
version: 0.1.0
author: moonlight-lupin
license: Apache-2.0
metadata:
  hermes:
    tags: [chief-of-staff, crm, pipeline, sales, yaml]
    related_skills: [document-preparer, bookkeeper, meeting-prep, drive-filer, entity-research]
---

# Pipeline Manager

## Overview

Pipeline Manager is the lightweight CRM for the Chief-of-Staff plugin. It tracks prospects and clients through configurable sales stages, links documents and invoices to deals, and exposes stale-deal signals for Daily Briefing and Weekly Review.

It uses human-readable YAML, not a database. Keep records tidy, stable, and auditable because other skills treat `pipeline.yaml` as a foundation data source.

## When to Use

Use this skill when the user asks to:

- Add a lead, prospect, deal, or opportunity.
- Move a deal to another stage.
- Show or list active deals.
- Find stale proposals or opportunities.
- Add notes to a deal.
- Link a proposal, NDA, contract, invoice, or dossier to a deal.
- Retrieve client context for meetings, document prep, or billing.

Do not use this skill for general tasks; use `todo-list`. Do not use it as the accounting ledger; use `bookkeeper` for invoices and expenses.

## Storage

Pipeline data lives in:

```text
{project_root}/pipeline.yaml
```

Resolve `project_root` from `shared/config/company.yaml` via `shared/scripts/config_loader.py`.

If the file does not exist, create it with:

```yaml
deals: []
```

Use UTF-8, ISO dates, and stable IDs. Avoid reformatting unrelated records when making small changes.

## Config

Read these fields from `company.yaml`:

```yaml
sales_stages: [Lead, Qualified, Proposal Sent, NDA Signed, Contract Signed, Invoiced, Paid, Lost]
stale_threshold_days: 14
company:
  currency: SGD
paths:
  project_root: "~/.hermes/projects/acme/"
```

Rules:

- `sales_stages` defines allowed active stages and ordering.
- `stale_threshold_days` is the default maximum days without activity in a stage.
- Use `company.currency` as the default currency when a deal value is supplied without currency.
- Preserve historical stage names in old records if the config changes; normalize only when the user confirms.

## Schema

```yaml
deals:
  - id: deal-001
    client_name: "Acme Corp"
    contact_name: "John Tan"
    contact_email: "john@acme.example"
    stage: "Proposal Sent"
    value: 4500
    currency: SGD
    created: 2026-06-15
    last_activity: 2026-07-01
    documents:
      - type: NDA
        path: "02_Clients/Acme Corp/NDA/NDA_signed.pdf"
        status: signed
        added: 2026-07-01
      - type: Proposal
        path: "02_Clients/Acme Corp/Proposals/Proposal_v1.pdf"
        status: sent
        added: 2026-07-02
    notes:
      - date: 2026-07-09
        author: chief-of-staff
        text: "Follow up after board meeting."
```

Required fields for each deal:

| Field | Requirement |
|---|---|
| `id` | Stable unique ID, e.g. `deal-001` or `deal-acme-20260709` |
| `client_name` | Organization/person buying the service |
| `contact_name` | Primary contact; may be empty only if unknown |
| `contact_email` | Primary email; may be empty only if unknown |
| `stage` | One of `sales_stages` unless preserving legacy data |
| `value` | Numeric expected value; use `0` if unknown |
| `currency` | ISO-style code such as SGD, USD, GBP |
| `created` | ISO date |
| `last_activity` | ISO date updated on meaningful activity |
| `documents` | List, initially `[]` |
| `notes` | List of note objects; migrate legacy string notes to list when editing that deal |

## Operations

### Add Deal

1. Collect `client_name`, contact details, value/currency, and initial stage.
2. Default stage to the first configured `sales_stages` item.
3. Generate a unique ID by incrementing existing numeric IDs or using a slug+date.
4. Set `created` and `last_activity` to today.
5. Write the new record to `pipeline.yaml`.
6. Confirm with the deal ID and current stage.

Completion criterion: the new deal is present exactly once and can be shown by ID.

### Move Stage

1. Identify the deal by exact ID, exact client name, or unambiguous fuzzy match.
2. Validate the target stage against `sales_stages`.
3. Update `stage` and `last_activity` to today.
4. Append a note recording the stage movement: `Moved from X to Y`.
5. Trigger integration suggestions:
   - Proposal stage → offer Document Preparer.
   - NDA Signed → offer Entity Research.
   - Contract Signed → offer invoice prep / Bookkeeper.
   - Invoiced/Paid → ensure linked invoice exists.

Completion criterion: stage change and audit note are both saved.

### List Deals

Support filters:

- all active deals
- by stage
- by client name
- stale only
- closed/won (`Paid` or configured final stage)
- lost/inactive

Default list columns:

| ID | Client | Stage | Value | Last activity | Stale? | Next suggested action |

### Show Detail

Show every field plus linked documents and notes in reverse chronological order. Include integrations: invoice status from Bookkeeper if `deal_id` appears in `invoices.yaml`, and meeting context if requested.

### Add Note

Append a note object:

```yaml
- date: 2026-07-09
  author: chief-of-staff
  text: "Client asked for revised scope by Friday."
```

Update `last_activity` unless the user explicitly says the note is archival.

### Link Document

Append a document object:

```yaml
- type: Contract
  path: "02_Clients/Acme Corp/Contracts_SOW/MSA_signed.pdf"
  status: signed
  added: 2026-07-09
```

Prefer Drive-relative paths when available; absolute local paths are allowed for unfiled documents but should trigger a Drive Filer suggestion.

## Stale Detection

A deal is stale when:

```text
today - last_activity > stale_threshold_days
```

Exclude terminal stages such as `Paid`, `Lost`, `Cancelled`, or any stage configured as terminal in future config. For stale deals, include:

- days inactive
- current stage
- last note/document activity
- recommended next action

Daily Briefing should show stale deals that need attention. Weekly Review should summarize stale count, oldest stale deal, and deals that moved this week.

## Integrations

- **Document Preparer:** uses stage and client info to generate NDAs, proposals, SOWs, contracts, and invoices.
- **Bookkeeper:** invoices link back with `deal_id`. Pipeline should not duplicate invoice status except as a brief display.
- **Meeting Prep:** pulls deal stage, value, documents, notes, and open invoices for attendees.
- **Drive Filer:** files linked documents under `02_Clients/{client}/...` and can update document paths after filing.
- **Entity Research:** can be triggered when a deal reaches `NDA Signed` or another due-diligence stage.
- **Daily Briefing / Weekly Review:** consume stale deals, recent stage movements, and notable new deals.

## Rules

- Ask before creating, modifying, or deleting records.
- Never delete deals during normal use; move to a terminal stage or add a note.
- Use exact configured stage names in data files.
- Preserve audit history in notes when changing meaningful fields.
- Treat `pipeline.yaml` as authoritative for CRM state, not email threads.
- Do not store secrets, private credentials, or full legal document text in notes.
- Keep dates in ISO format and values as numbers, not formatted strings.

## Common Pitfalls

1. **Using stale email activity as pipeline activity without evidence.** Only update `last_activity` when the user confirms or a concrete event is recorded.
2. **Breaking downstream links by renaming IDs.** IDs are referenced by invoices and documents. Never change an ID casually.
3. **Overwriting notes.** Always append notes; do not replace history unless explicitly cleaning malformed data.
4. **Ignoring configured stages.** Do not invent stage names in the data file; ask to update `company.yaml` if the process changed.
5. **Mixing AR/AP into deals.** Link invoice IDs and summarize status, but keep finance details in Bookkeeper.

## Verification Checklist

- [ ] `company.yaml` loaded and `project_root` resolved.
- [ ] `pipeline.yaml` exists or was initialized after confirmation.
- [ ] Deal IDs are unique and stable.
- [ ] Stage values match `sales_stages` unless legacy data is intentionally preserved.
- [ ] Writes preserve unrelated records.
- [ ] Stale detection excludes terminal stages.
- [ ] Integrations were suggested only when relevant.
