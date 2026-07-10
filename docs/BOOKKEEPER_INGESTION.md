# Bookkeeper Invoice Ingestion Guide

## Overview

The invoice ingestion module detects invoice-like material from local events, extracts structured candidates, validates them, checks for duplicates, and routes them through the review queue — all without writing to `invoices.yaml` until the operator approves.

## Workflow

```
Scan local events → Extract candidate → Validate → Check duplicates
→ Prepare review action → Operator approves → Execute (writes invoices.yaml)
```

## Commands

### Scan for invoice candidates

```bash
# Scan recent events for invoice-like content
python skills/bookkeeper/scripts/invoice_ingest.py scan --since 24h --summary

# Dry-run (report without writing candidates)
python skills/bookkeeper/scripts/invoice_ingest.py scan --dry-run
```

### Review candidates

```bash
# List all candidates
python skills/bookkeeper/scripts/invoice_ingest.py candidates --summary

# Preview a specific candidate
python skills/bookkeeper/scripts/invoice_ingest.py preview --candidate-id bic_001

# Validate all candidates
python skills/bookkeeper/scripts/invoice_ingest.py validate
```

### Prepare for approval

```bash
# Create a pending action (does NOT write invoices.yaml)
python skills/bookkeeper/scripts/invoice_ingest.py prepare --candidate-id bic_001
```

### Review and approve through review queue

```bash
# Preview in review queue
python shared/scripts/review_queue.py preview --action-id pa_001

# Approve
python shared/scripts/review_queue.py approve --action-id pa_001 --approver "MH" --reason "Invoice checked"

# Execute (writes to invoices.yaml)
python shared/scripts/review_queue.py execute --action-id pa_001
```

### Dismiss a candidate

```bash
python skills/bookkeeper/scripts/invoice_ingest.py dismiss --candidate-id bic_002 --reason "Not an invoice"
```

## Candidate states

| State | Description |
|-------|-------------|
| candidate | Newly detected, awaiting review |
| prepared | Pending action created, awaiting approval |
| recorded | Invoice written to invoices.yaml |
| dismissed | Operator dismissed (not an invoice) |

## Validation

| Status | Condition |
|--------|-----------|
| valid | All required fields present, no warnings |
| needs_review | Has warnings (missing fields, duplicate possible) |
| invalid | Required fields missing |

Required fields: direction, counterparty, amount, currency, issue_date, due_date

## Duplicate detection

| Score | Action |
|-------|--------|
| ≥ 0.85 | `duplicate_possible` warning added |
| ≥ 0.95 | `duplicate_likely` — blocks prepare/execution unless explicit override |

## Safety

- Invoice ingestion reads local events only — no Gmail/Drive/Calendar calls
- `invoices.yaml` is only written after approved execution
- Money stored as strings (not floats) to avoid precision issues
- No bank account details or secrets stored in candidates
- No invoice deletion or mark-paid in v0.2.6

## Daily briefing

The daily briefing includes a Bookkeeper section showing:
- Invoice candidates found
- Candidates needing review
- Duplicate warnings
- Pending invoice-record actions