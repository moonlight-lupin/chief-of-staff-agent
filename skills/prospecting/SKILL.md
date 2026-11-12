---
name: prospecting
description: "Use when finding and qualifying new sales prospects for the business (ideal-customer search, company and contact enrichment) using paid data endpoints such as Monid, within an approved spend budget, and turning chosen prospects into pipeline deals for approval."
version: 0.1.0
author: moonlight-lupin
license: Apache-2.0
metadata:
  hermes:
    tags: [chief-of-staff, prospecting, leads, monid, sales, pdpa]
    related_skills: [pipeline-manager, entity-research, seo-audit, competitor-watch]
---

# Prospecting

## Overview

This skill finds companies and business contacts that fit the ideal-customer
profile (ICP), keeps a clean prospect list, and proposes the best ones as
pipeline deals. Paid data calls run only against an **approved spend
envelope**. The prospect list keeps business contact data only, records where
every lead came from, and never contacts anyone by itself.

## Setup

```yaml
spend:
  currency: USD
  monid:
    monthly_cap: 100          # hard ceiling across all budgets this month
prospecting:
  icp:
    industries: [fintech, payments]
    countries: [SG]
    min_employees: 10
    max_employees: 500
```

The skill appears only when `spend:` has a provider section.

## Workflow

1. **Agree the target.** Restate the ICP and how many leads the user wants.
   Agree a topic slug, e.g. `sg-fintech-cfos`.
2. **Price it for free.** Use `monid_discover`, then `monid_inspect`, to find
   suitable endpoints and their prices. Discovering and inspecting cost
   nothing.
3. **Get a budget.** Check `spend.py status`. If no approved envelope covers
   this topic, propose one sized to the estimate, then stop and ask the user
   to approve it (`review_queue.py preview`, then `approve`):

   ```bash
   .venv/bin/python shared/scripts/spend.py propose --provider monid \
     --purpose "Find SG fintech CFOs for Q4 outreach" --topic sg-fintech-cfos --cap 20 --currency USD
   ```

4. **Before every paid call, reserve the cost:**

   ```bash
   .venv/bin/python shared/scripts/spend.py reserve --envelope <id> --topic sg-fintech-cfos \
     --estimate <price from inspect> --endpoint <endpoint> --note "page 1"
   ```

   - **Exit 0:** make the call with `monid_run`, poll if it runs async, then
     record what it cost: `spend.py record --reservation <id> --actual <cost>
     --run-id <runId>`. If the call fails, use `--status failure` with the
     amount actually charged (often 0).
   - **Exit 3, or any refusal:** do **not** make the call. Tell the user why:
     over budget, a different topic, or the envelope has expired. Propose a
     new envelope if they want to continue.
   - Broken or useless results: `monid_dispute`, as Monid's instructions
     describe.

5. **Ingest** the results with full provenance:

   ```bash
   .venv/bin/python skills/prospecting/scripts/prospects.py ingest --input leads.json
   ```

   `leads.json` holds `{"source": {"provider": "monid", "endpoint": "…",
   "run_id": "…", "fetched_at": "…", "envelope_id": "…"}, "purpose": "…",
   "leads": [{"company", "website", "contact_name", "contact_role", "email",
   "phone", "linkedin", "industry", "country", "city", "employees"}]}`. Any
   other field is dropped and reported by name. Personal mailbox addresses
   (Gmail and similar) are dropped. Leads already in the pipeline, already
   listed, or opted out are skipped.

6. **Review** with `prospects.py list`, which shows no contact details unless
   you pass `--full`, ranked by `icp_fit`. Agree with the user which ones go
   forward.

7. **Propose** the chosen prospects: `prospects.py propose <id> …` queues
   `pipeline.deal.add` actions for the user to approve.

## Outreach rules

- Outreach is a separate, approved `mail.send` per recipient. Never bulk-send,
  and never send from this skill.
- Run `prospects.py can-contact <id> --channel email|phone|sms` before
  drafting. Phone and SMS stay blocked until the number has been checked
  against the Singapore Do Not Call Registry and recorded with
  `prospects.py dnc <id> --state clear|registered`.
- Every message says who you are and how to opt out. When someone opts out,
  run `prospects.py opt-out --email … [--domain …]` immediately. The address
  is suppressed permanently, even after purges.
- Retention: `prospects.py purge --older-than-days 180` shows what would be
  deleted. Pass `--confirm` only when the user asks for it.

## Confidentiality

Never paste contact details or lead lists into chat beyond what the user
needs to decide. Refer to prospects by id and company.
