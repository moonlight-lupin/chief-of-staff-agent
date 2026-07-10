# Review Queue Guide

## Overview

The review queue is the operator's control surface for Chief-of-Staff. It unifies pending actions and suggestions into one CLI for listing, previewing, approving, dismissing, executing, and auditing.

## Commands

### Morning review

```bash
# Get a summary of what needs attention
python shared/scripts/review_queue.py summary

# List everything needing review
python shared/scripts/review_queue.py list --state requested
```

### Preview and approve

```bash
# Preview a specific action
python shared/scripts/review_queue.py preview --action-id pa_001

# Approve after review
python shared/scripts/review_queue.py approve --action-id pa_001 --approver "MH" --reason "Recipient and content checked"

# Execute after approval
python shared/scripts/review_queue.py execute --action-id pa_001
```

### Dismiss

```bash
# Dismiss an action (reason required)
python shared/scripts/review_queue.py dismiss --action-id pa_002 --reason "No longer needed"
```

### Filter

```bash
# Filter by risk
python shared/scripts/review_queue.py list --risk high

# Filter by type
python shared/scripts/review_queue.py list --type gmail.send

# Filter by state
python shared/scripts/review_queue.py list --state approved
```

### Bulk approve (low-risk only)

```bash
# Bulk approve low-risk label actions
python shared/scripts/review_queue.py approve --all --risk low --type gmail.label --reason "Low-risk label cleanup reviewed" --confirm-low-risk-bulk
```

Bulk approval is restricted to **low-risk actions only** and requires the `--confirm-low-risk-bulk` flag. High-risk and medium-risk bulk approval is rejected.

### Audit trail

```bash
# Show recent state transitions
python shared/scripts/review_queue.py audit --limit 20
```

## Risk classification

| Risk | Action types | Icon |
|------|-------------|------|
| High | gmail.send, gmail.trash, drive.trash, calendar.cancel | 🔴 |
| Medium | calendar.create, calendar.update, drive.upload, gmail.archive | 🟡 |
| Low | gmail.label, gmail.create_label, drive.search, gmail.search, drive.download | 🟢 |

Unknown write actions (e.g., `custom.delete`) are classified as **high risk** — not silently defaulted to low.

## States

| State | Description |
|-------|-------------|
| requested | Action created, awaiting operator review |
| approved | Operator approved, ready for execution |
| executing | Execution in progress |
| executed | Execution completed successfully |
| failed | Execution failed, can retry |
| dismissed | Operator dismissed (not needed) |
| cancelled | System/expiry cancellation |
| expired | Approval window lapsed |

## Safety

- The review queue may approve, dismiss, and execute through existing paths
- It must NOT execute unapproved actions
- It must NOT bulk approve high-risk or medium-risk actions
- It must NOT send email, modify Gmail/Calendar/Drive directly
- It must NOT bypass provider guardrails
- Dismissed items remain in audit history (not deleted)

## Daily workflow

1. **Morning:** `review_queue.py summary` → see what needs attention
2. **Review:** `review_queue.py list --state requested` → see pending items
3. **Preview:** `review_queue.py preview --action-id <id>` → inspect details
4. **Approve:** `review_queue.py approve --action-id <id> --approver MH --reason "..."`
5. **Execute:** `review_queue.py execute --action-id <id>`
6. **Dismiss:** `review_queue.py dismiss --action-id <id> --reason "..."`
7. **Audit:** `review_queue.py audit --limit 20` → review history