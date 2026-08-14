---
name: bookkeeper
description: "Use when tracking Chief-of-Staff invoices, expenses, AR/AP, overdue bills, and monthly P&L from invoices.yaml and expenses.yaml without becoming a full accounting system."
version: 0.1.0
author: moonlight-lupin
license: Apache-2.0
metadata:
  hermes:
    tags: [chief-of-staff, bookkeeping, invoices, expenses, finance, pnl]
    related_skills: [pipeline-manager, daily-briefing, weekly-review, drive-filer, document-preparer]
---

# Bookkeeper

## Overview

Bookkeeper provides simple operational finance visibility for the Chief-of-Staff plugin: invoices sent/received, expenses, overdue AR/AP, and basic cash-basis monthly P&L. It is **not** a full accounting ledger, tax engine, or substitute for professional accounting software.

Use it to answer: Who owes us? Who do we owe? What was this month's paid revenue and expenses? Which invoices are overdue?

## When to Use

Use this skill when the user asks to:

- Add or update an invoice record.
- Add or update an expense.
- Show outstanding accounts receivable (AR) or accounts payable (AP).
- List overdue invoices or bills.
- Produce a monthly P&L snapshot.
- Link an invoice to a pipeline deal.
- Capture finance metadata for a filed invoice/receipt.

Do not use this skill for double-entry accounting, tax computation, payroll, bank reconciliation, or statutory filings beyond operational reminders.

## Storage

Bookkeeper data lives in the configured project root:

```text
{project_root}/invoices.yaml
{project_root}/expenses.yaml
```

Resolve `project_root` from `shared/config/company.yaml`. If missing, initialize after confirmation:

```yaml
# invoices.yaml
invoices: []
```

```yaml
# expenses.yaml
expenses: []
```

## Invoice Schema

```yaml
invoices:
  - id: INV-001
    direction: sent              # sent (AR) | received (AP)
    counterparty: "Acme Corp"
    deal_id: deal-001            # optional Pipeline Manager link
    amount: 4500
    currency: SGD
    issue_date: 2026-07-01
    due_date: 2026-07-15
    status: sent                 # draft | sent | received | paid | overdue | cancelled
    paid_date: null
    document_path: "04_Finance/Invoices_Sent/INV-001.pdf"
    notes: ""
```

Rules:

- `direction: sent` means accounts receivable: the company expects payment.
- `direction: received` means accounts payable: the company owes someone else.
- `amount` is numeric. Do not include currency symbols.
- `status: overdue` may be materialized for convenience, but overdue can always be computed from unpaid status and `due_date`.
- `paid_date` is required when status is `paid`.

## Expense Schema

```yaml
expenses:
  - id: EXP-001
    category: "software"
    vendor: "Google"
    amount: 12
    currency: SGD
    date: 2026-07-01
    status: paid                 # draft | submitted | approved | paid | reimbursed | cancelled
    document_path: "04_Finance/Receipts/EXP-001.pdf"
    recurring: monthly           # one-time | monthly | quarterly | yearly | null
    notes: ""
```

Recommended categories: `software`, `rent`, `utilities`, `travel`, `meals`, `professional`, `equipment`, `tax`, `other`. Use configured categories from `company.yaml` when present.

## Operations

### Add Invoice

1. Determine direction: sent (AR) or received (AP).
2. Capture counterparty, amount, currency, issue date, due date, status, optional `deal_id`, document path, and notes.
3. Generate a unique ID (`INV-###` unless user supplied one).
4. Validate `deal_id` against `pipeline.yaml` when available.
5. Write to `invoices.yaml`.
6. Offer Drive Filer if `document_path` is local/unfiled.

Completion criterion: the invoice appears once, has a unique ID, and direction classifies it as AR or AP.

### Update Invoice

Common updates: mark paid, change due date, add document path, add note, link deal. When marking paid, set `paid_date` and status `paid`. Invoices are cancelled, never removed: set `status: cancelled` and retain the record.

### Add Expense

Capture category, vendor, amount, currency, date, status, document path, recurrence, and notes. If the expense is travel-related, tag/link it for Travel Itinerary context when possible.

- Completion criterion: the expense has a unique ID, valid date, and currency is a valid ISO-4217 code recorded verbatim (no coercion into an existing currency).

### Reports

- **Monthly P&L:** paid sent invoices as revenue, paid expenses by category, net.
  Completion criterion: expenses classified to a category; per-currency net equals paid revenue minus paid expenses; no cross-currency sums.
- **Outstanding AR:** unpaid sent invoices not cancelled.
- **Outstanding AP:** unpaid received invoices not cancelled.
- **Overdue invoices:** unpaid invoices with due date before today.
- **Expense breakdown:** by category for a month, quarter, or year.

Completion criterion: every currency present in source records has its own labeled bucket.

The P&L helper script is:

```text
skills/bookkeeper/scripts/pl_report.py
```

Usage:

```bash
python3 skills/bookkeeper/scripts/pl_report.py --config shared/config/company.yaml --month 2026-07
```

Output is a formatted text report suitable for Daily Briefing, Weekly Review, or direct user replies.

## Integrations

- **Pipeline Manager:** invoices may include `deal_id`; deal detail can show invoice/payment status.
- **Daily Briefing:** pulls overdue invoices and outstanding AR total.
- **Weekly Review:** pulls P&L snapshot, AR/AP totals, payments received, invoices sent, and expense highlights.
- **Drive Filer:** files invoice PDFs, bills, and receipts to `04_Finance/` and vendor/client folders.
- **Document Preparer:** invoice document generation should create or update an invoice record.
- **Deadline Tracker:** GST/tax deadlines may require finance data, but tax filing dates remain in Deadline Tracker.

## P&L Policy

The default report is cash-basis:

- Revenue = `direction: sent`, `status: paid`, `paid_date` in the report month.
- Expenses = `expenses[]` with `status: paid` and `date` in the report month.
- Outstanding AR = `direction: sent`, not paid/cancelled, regardless of month.
- Outstanding AP = `direction: received`, not paid/cancelled, regardless of month.

If the user wants accrual reporting, state that v1 reports cash-basis and offer to add an explicit config option before changing behavior.

## Rules

- Ask before writing or changing financial records unless the user directly requested the write.
- Use `Decimal`-safe thinking for money; avoid floating-point-looking rounding in reports.
- Keep currencies visible. Do not combine currencies without labeling them.
- Never mark an invoice paid without a payment date or explicit confirmation.
- Keep source documents linked via `document_path` whenever possible.
- Do not infer payment from a friendly email unless the user confirms or a payment record exists.
- Avoid storing bank account details or secrets in YAML notes.

## Common Pitfalls

1. **Mixing AR and AP.** Direction controls whether money is owed to or by the company.
2. **Counting unpaid invoices as revenue.** P&L is cash-basis by default; only paid sent invoices count as revenue.
3. **Forgetting AP invoices.** Received invoices are not expenses until paid in the P&L, but they matter for outstanding AP.
4. **Losing deal links.** Preserve `deal_id` because Pipeline Manager uses it for client context.
5. **Combining currencies silently.** Report separate currency buckets if records are mixed.

## Verification Checklist

- [ ] `company.yaml` loaded and `project_root` resolved.
- [ ] `invoices.yaml` and `expenses.yaml` were read or initialized after confirmation.
- [ ] Records conform to schemas and dates are ISO/null.
- [ ] Paid invoices have `paid_date`.
- [ ] AR/AP classification follows `direction`.
- [ ] P&L report month matches `YYYY-MM` requested.
- [ ] Daily Briefing and Weekly Review can consume overdue and summary data.
