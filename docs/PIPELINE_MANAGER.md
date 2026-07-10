# Pipeline Manager Guide (v0.2.8)

## Overview

The Pipeline Manager is the lightweight CRM layer for Chief-of-Staff. It tracks deals in `pipeline.yaml`, detects stale opportunities, surfaces CRM state in the Daily Briefing, and routes risky changes through the Review Queue.

## Commands

### Read-only

```bash
# List all deals with summary
python skills/pipeline-manager/scripts/pipeline.py list --summary

# Show deal details
python skills/pipeline-manager/scripts/pipeline.py show --deal-id deal-001

# Validate all deals
python skills/pipeline-manager/scripts/pipeline.py validate

# Detect stale deals
python skills/pipeline-manager/scripts/pipeline.py stale --summary
```

### Operator changes (explicit CLI)

```bash
# Add a new deal
python skills/pipeline-manager/scripts/pipeline.py add \
  --client "Acme Corp" \
  --contact "Jane Tan" \
  --email "jane@acme.example" \
  --value 4500 \
  --currency SGD \
  --stage "Lead"

# Move deal stage (with audit note)
python skills/pipeline-manager/scripts/pipeline.py move \
  --deal-id deal-001 \
  --stage "Proposal Sent" \
  --note "Proposal sent for review"

# Add a note
python skills/pipeline-manager/scripts/pipeline.py note \
  --deal-id deal-001 \
  --text "Client asked for revised timeline"

# Add archival note (doesn't update last_activity)
python skills/pipeline-manager/scripts/pipeline.py note \
  --deal-id deal-001 \
  --text "Annual review completed" \
  --archival

# Link a document
python skills/pipeline-manager/scripts/pipeline.py link-doc \
  --deal-id deal-001 \
  --type Proposal \
  --path "02_Clients/Acme Corp/Proposals/Proposal_v1.pdf" \
  --status sent
```

### Review Queue flow (autonomous suggestions)

```bash
# Preview
python shared/scripts/review_queue.py preview --action-id pa_001

# Approve
python shared/scripts/review_queue.py approve --action-id pa_001 --approver "MH" --reason "CRM update checked"

# Execute
python shared/scripts/review_queue.py execute --action-id pa_001
```

## Risk classification

| Action | Risk |
|---|---|
| `pipeline.deal.add` | 🟡 Medium |
| `pipeline.deal.move_stage` | 🟡 Medium |
| `pipeline.deal.add_note` | 🟢 Low |
| `pipeline.deal.link_document` | 🟢 Low |
| `pipeline.deal.delete` | 🔴 High (unsupported) |

## Stale deal detection

A deal is stale when `today - last_activity > stale_threshold_days` (default 14). Terminal stages (Paid, Lost, Cancelled) are excluded.

Recommended actions by stage:
- Lead → qualify or close
- Proposal Sent → follow up
- NDA Signed → prepare diligence / next meeting
- Contract Signed → create invoice candidate
- Invoiced → check payment status

## Safety

- `pipeline.yaml` only written from explicit CLI or approved pending actions
- No Gmail/Drive/Calendar calls
- Deal deletion is unsupported — use Lost/Cancelled stage instead
- Stage moves preserve audit notes and stage history
- Autonomous flows can suggest but not mutate pipeline

## Daily Briefing

The Daily Briefing includes a Pipeline section showing:
- Active deals count
- Stale deals count + oldest stale deal
- Recently moved deals
- Pending CRM actions
- Contract Signed deals without invoices
- Invoiced deals not yet paid