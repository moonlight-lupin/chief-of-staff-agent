---
name: todo-list
description: "Use when managing internal Chief-of-Staff tasks in todos.yaml: add, list, update, complete, defer, cancel, and surface open or overdue action items for briefings and reviews."
version: 0.1.0
author: moonlight-lupin
license: MIT
metadata:
  hermes:
    tags: [chief-of-staff, tasks, todos, productivity, yaml]
    related_skills: [deadline-tracker, daily-briefing, weekly-review, meeting-prep]
---

# To-Do List

## Overview

To-Do List is the internal task layer for the Chief-of-Staff plugin. It tracks flexible work items generated manually or by other skills. It is intentionally separate from Deadline Tracker:

- **Deadline Tracker** = external obligations with hard due dates.
- **To-Do List** = internal actions required to move work forward.

Examples: follow up with a prospect, prepare filing documents, draft a proposal, review an invoice, send meeting notes.

## When to Use

Use this skill when the user asks to:

- Add a to-do or action item.
- Show what is open, overdue, high priority, or tagged.
- Mark a task done.
- Defer, cancel, or update a task.
- Capture action items from a meeting.
- Create implementation tasks from deadlines or pipeline events.

Do not use this skill to represent statutory deadlines themselves; store those in Deadline Tracker and create supporting tasks here.

## Storage

To-dos live in:

```text
{project_root}/todos.yaml
```

Resolve `project_root` from `shared/config/company.yaml`. If the file does not exist, create it with:

```yaml
todos: []
```

Use ISO dates and preserve unrelated records when editing.

## Schema

```yaml
todos:
  - id: todo-001
    title: "Follow up with John re: proposal"
    priority: high          # high | medium | low
    due: 2026-07-12         # ISO date or null
    status: open            # open | done | deferred | cancelled
    source: briefing        # manual | briefing | meeting | weekly-review | deadline-tracker | pipeline-manager
    tags: [sales, acme-corp]
    created: 2026-07-09
    completed: null
```

Required fields:

| Field | Notes |
|---|---|
| `id` | Stable unique ID such as `todo-001` |
| `title` | Verb-led action; specific enough to execute |
| `priority` | `high`, `medium`, or `low` |
| `due` | ISO date or `null`; do not invent if unknown |
| `status` | `open`, `done`, `deferred`, or `cancelled` |
| `source` | Where the task came from |
| `tags` | List of lowercase tags for filtering |
| `created` | ISO date |
| `completed` | ISO date when done, otherwise `null` |

Optional fields may be added as needed: `related_deal_id`, `deadline_name`, `document_path`, `meeting_id`, `owner`, `notes`.

## Operations

### Add

1. Capture title, priority, due date if known, source, and tags.
2. Normalize title to an action phrase.
3. Generate the next unique ID.
4. Default priority to `medium`, status to `open`, source to `manual`, tags to `[]`, created to today, completed to `null`.
5. Write to `todos.yaml`.
6. Confirm with ID, due date, and priority.

Completion criterion: the new to-do appears once and is listable.

### List

Supported filters:

- by priority: high/medium/low
- by tag
- by status
- by due date
- overdue only
- due today
- due within N days
- source, e.g. `deadline-tracker` or `meeting-prep`

Default sort:

1. overdue open items
2. due today
3. high priority
4. nearest due date
5. oldest created date

Default columns:

| ID | Priority | Due | Status | Title | Tags | Source |

### Update

Update title, priority, due date, tags, source, or notes. Preserve `id`, `created`, and completed history unless explicitly correcting malformed data. When changing due date, state whether the item was deferred or simply corrected.

### Mark Done

1. Identify exact task.
2. Set `status: done`.
3. Set `completed` to today.
4. Preserve due date for review metrics.
5. Report completion.

### Defer

1. Identify exact task.
2. Require a new due date or explicit `due: null`.
3. Set `status: deferred` only if the task is intentionally paused; otherwise keep `open` with the new due date.
4. Add/append a note if a notes field exists or if the deferral rationale matters.

### Cancel

1. Confirm cancellation if the task is not obviously obsolete.
2. Set `status: cancelled`.
3. Keep `completed: null`.
4. Preserve the record for audit and Weekly Review.

## Integrations

- **Daily Briefing:** pulls open items, overdue items, and high-priority items due soon.
- **Weekly Review:** computes completion rate, carry-over items, cancelled items, and tasks created this week.
- **Meeting Prep:** converts meeting action items into to-dos tagged with attendee/client names.
- **Deadline Tracker:** can spawn to-dos for preparation steps before statutory deadlines.
- **Pipeline Manager:** can spawn sales follow-ups and proposal/contract next actions.
- **Bookkeeper:** can spawn collection/payment tasks for overdue invoices.

## Metrics for Weekly Review

Compute:

- created this week
- completed this week
- overdue open count
- completion rate = completed this week / (completed this week + still-open tasks due this week)
- carry-over items from prior weeks
- high-priority unresolved items

Do not game the metrics by deleting tasks; use `done` or `cancelled`.

## Rules

- Ask before writing unless the user directly asked to add/update/complete a task.
- Use concise, action-oriented titles.
- Do not duplicate existing open tasks; if a likely duplicate exists, ask whether to update it.
- Keep tags lowercase and hyphenated (`acme-corp`, `compliance`, `sales`).
- Use `null` for unknown due dates, not fake dates.
- Never delete tasks during normal operation.
- Avoid storing sensitive full email bodies or document contents in task titles/notes.

## Common Pitfalls

1. **Turning every reminder into a deadline.** Internal prep belongs here; external due dates belong in Deadline Tracker.
2. **Ambiguous completion.** "Follow up" is weak; "Email John Tan about revised proposal" is actionable.
3. **Duplicate action items from meetings and briefings.** Search open tasks by title/client tags before adding.
4. **Marking deferred tasks as done.** Defer means still not done; done means completed.
5. **Forgetting source.** Source drives Weekly Review attribution and debugging.

## Verification Checklist

- [ ] `todos.yaml` loaded or initialized after confirmation.
- [ ] IDs are unique.
- [ ] New/updated records conform to schema.
- [ ] Dates are ISO or `null`.
- [ ] Open, overdue, and high-priority filters work for Daily Briefing.
- [ ] Weekly Review can calculate completion metrics from statuses and dates.
