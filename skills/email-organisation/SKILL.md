# Email Organisation

Email organisation onboarding and label policy management.

## Overview

This skill discovers the user's existing Gmail label/folder structure,
classifies labels into organisation categories, and generates a proposed
label policy — all read-only. No Gmail mutations occur during onboarding.

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