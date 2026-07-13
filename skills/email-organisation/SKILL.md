---
name: email-organisation
description: "Use when inspecting Gmail labels, proposing or saving a label policy, or when the operator addresses '{assistant_name}' (the CoS assistant name) to check email (e.g. 'Ask {assistant_name} to check my email'). Route all Gmail operations through the company workspace account configured in company.yaml for {company_name}, NOT the agent's personal email."
version: 0.1.0
author: moonlight-lupin
license: Apache-2.0
metadata:
  hermes:
    tags: [chief-of-staff, email, gmail, labels, organisation]
    related_skills: [daily-briefing, calendar-manager, meeting-prep]
---

# Email Organisation

Email organisation onboarding and label policy management.

## Overview

This skill discovers the user's existing Gmail label/folder structure,
classifies labels into organisation categories, and generates a proposed
label policy — all read-only. No Gmail mutations occur during onboarding.

## When to Use

Use this skill when the operator wants to inspect their Gmail labels, propose a
label policy, or review/save an approved organisation policy.

Also use when the operator addresses their Chief of Staff by its configured name (`assistant.name` in company.yaml), e.g. "Ask <name> to check my email" / "<name>, what's on today?".

## Core Principle

**Use existing labels first.** Create new labels only when there is a
repeated, unmet need (future versions, with approval).

## Commands

```bash
# Discover existing Gmail labels and show structure
python scripts/email_organisation.py inspect-labels --summary

# Generate a proposed label policy from existing labels
python scripts/email_organisation.py propose-policy --summary

# Show current approved policy
python scripts/email_organisation.py show-policy --summary

# Save a proposal as an approved policy
python scripts/email_organisation.py save-policy --from .email_organisation_policy.proposal.json --approved-by "MH"

# Validate a policy file
python scripts/email_organisation.py validate-policy
```

## Safety

- No Gmail label creation
- No Gmail label application
- No archiving, trashing, or sending
- No pending actions created
- Only read-only label listing and local JSON writes

## Files

- `.email_organisation_policy.proposal.json` — proposed policy (not approved)
- `.email_organisation_policy.json` — approved policy
- `shared/scripts/email_label_policy.py` — reusable policy logic
