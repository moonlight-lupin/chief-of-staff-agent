---
name: email-organisation
description: "Use when inspecting mail labels/categories, proposing or saving a label policy."
version: 0.1.3
author: moonlight-lupin
license: Apache-2.0
metadata:
  hermes:
    tags: [chief-of-staff, email, gmail, outlook, labels, categories, organisation]
    related_skills: [daily-briefing, calendar-manager, meeting-prep]
---

# Email Organisation

Email organisation onboarding and label/category policy management.

## Overview

This skill discovers the user's existing **Gmail labels** or **Outlook master
categories** (via `mail_list_tags`), classifies them into organisation
categories, and generates a proposed policy — all read-only during onboarding.
No mailbox mutations occur until suggestions are prepared → approved → executed
through the review queue.

Works with workspace providers that advertise `mail.list_tags`:

| Provider | Tag surface |
|---|---|
| `google_api` | Gmail labels |
| `composio` (family google) | Gmail labels via `GMAIL_LIST_LABELS` |
| `composio` (family microsoft) / `m365` | Outlook categories (tag id = displayName) |

## When to Use

Use this skill when the operator wants to inspect their labels/categories,
propose a label policy, or review/save an approved organisation policy.

Also use when the operator addresses their Chief of Staff by its configured name (`assistant.name` in company.yaml), e.g. "Ask <name> to check my email" / "<name>, what's on today?".

## Core Principle

**Use existing labels/categories first.** Create new ones only when there is a
repeated, unmet need (with approval).

## Commands

```bash
# Discover existing labels/categories and show structure
python skills/email-organisation/scripts/email_organisation.py inspect-labels --summary

# Generate a proposed policy from existing labels/categories
python skills/email-organisation/scripts/email_organisation.py propose-policy --summary

# Show current approved policy
python skills/email-organisation/scripts/email_organisation.py show-policy --summary

# Save a proposal as an approved policy
python skills/email-organisation/scripts/email_organisation.py save-policy \
  --from .email_organisation_policy.proposal.json --approved-by "MH"

# Validate a policy file
python skills/email-organisation/scripts/email_organisation.py validate-policy
```

### Composio Microsoft (Outlook categories) — Phase 4

With `integrations.workspace.family: microsoft` (and Outlook connected):

```bash
python skills/email-organisation/scripts/email_organisation.py \
  --config shared/config/company.yaml --summary inspect-labels

python skills/email-organisation/scripts/email_organisation.py \
  --config shared/config/company.yaml --summary propose-policy
```

`inspect-labels` uses `OUTLOOK_GET_MASTER_CATEGORIES` via `mail_list_tags`.
Summary titles the run **Outlook Category Inspection** and sets
`tag_surface: outlook_categories`. Categories appear as user tags
(id = displayName).

Classify / suggest / prepare use the same review-queue path as Gmail; suggestion
titles say “Apply category …” and `payload.label_id` is the category
displayName. Execute uses guarded `mail_tag` / `mail_archive` /
`mail_create_tag` on the CoS workspace client — not raw Hermes MCP write tools.

See `docs/EMAIL_ORG_LIVE_TEST_CHECKLIST.md` § Composio Microsoft.

## Safety (onboarding)

- No label/category creation
- No label/category application
- No archiving, trashing, or sending
- No pending actions created
- Only read-only tag listing and local JSON writes

## Files

- `.email_organisation_policy.proposal.json` — proposed policy (not approved)
- `.email_organisation_policy.json` — approved policy
- `shared/scripts/email_label_policy.py` — reusable policy logic

## Writes vs Hermes Composio MCP

If Hermes already has Composio MCP connected, you may **inspect** via CoS
(`inspect-labels` above) or by calling read tools and summarizing — but
**organise writes** (tag / archive / create label) must go through
`email_organisation.py prepare` → `review_queue.py approve/execute` so
guardrails and audit apply.
