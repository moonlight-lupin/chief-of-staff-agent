---
name: deadline-tracker
description: "Use when tracking statutory or business deadlines for the Chief-of-Staff plugin. Loads company.yaml and jurisdiction packs, computes due dates, categorizes urgency, and feeds briefings/reviews."
version: 0.1.0
author: moonlight-lupin
license: MIT
metadata:
  hermes:
    tags: [chief-of-staff, compliance, deadlines, statutory, briefing]
    related_skills: [todo-list, daily-briefing, weekly-review]
---

# Deadline Tracker

## Overview

Deadline Tracker is the compliance and hard-deadline source of truth for the Chief-of-Staff plugin. It combines:

1. A company-specific `company.yaml` file.
2. A jurisdiction pack in `shared/config/jurisdictions/{jurisdiction}.yaml`.
3. Optional custom deadlines under `deadlines.custom[]`.

It does **not** replace legal, tax, or corporate-secretarial advice. Its job is operational vigilance: surface what is overdue or approaching, explain the authority/penalty context, and spawn follow-up to-dos when the user confirms.

## When to Use

Use this skill when the user asks:

- "What deadlines are coming up?"
- "What's due for compliance?"
- "Are any filings overdue?"
- "Show statutory deadlines for this company."
- "Add this business deadline."
- "Create reminders for upcoming filings."

Also use it for scheduled weekly scans and whenever Daily Briefing or Weekly Review needs a deadline feed.

Do **not** use this skill for flexible internal tasks with no hard date; use `todo-list` for those. Do not claim a filing is complete unless a source record or user confirmation says it is complete.

## Storage and Config

- Central config: `shared/config/company.yaml`
- Example config: `shared/config/company.yaml.example`
- Jurisdiction packs: `shared/config/jurisdictions/{sg,hk,us,uk}.yaml`
- Custom deadlines: `company.yaml` → `deadlines.custom[]`
- Generated reminders/actions: `todo-list` writes to `{project_root}/todos.yaml`

The config loader is `shared/scripts/config_loader.py`:

```python
from config_loader import load_config, get_project_root
config = load_config()  # or load_config('/path/to/company.yaml')
project_root = get_project_root(config)
```

The date engine is `shared/scripts/date_utils.py` and exposes:

- `days_until(date_str)`
- `categorize_deadline(deadline_date)` → `overdue | within_7 | within_30 | future`
- `compute_statutory_deadline(requirement, company_info)`
- `is_business_day(date_str, jurisdiction)`
- `next_business_day(date_str, jurisdiction)`

## Workflow

1. **Load config.** Use `load_config(path)` if the user supplied a path, otherwise the default `shared/config/company.yaml`. Completion criterion: config loaded successfully and `company.name`, `company.jurisdiction`, `company.incorporation_date`, `company.financial_year_end`, and `paths.project_root` are available.
2. **Load jurisdiction pack.** Read `shared/config/jurisdictions/{company.jurisdiction.lower()}.yaml`. Completion criterion: every requirement in `statutory[]` has `name`, `frequency`, `trigger`, `authority`, `penalty`, and `notes`.
3. **Compute statutory deadlines.** For each statutory requirement, call `compute_statutory_deadline(requirement, config.company)`. If a requirement is event-driven and no event date exists, include it as `needs_event_date` rather than inventing a due date.
4. **Merge custom deadlines.** Add `company.yaml` → `deadlines.custom[]`, preserving name, due date, authority, owner, and notes. Completion criterion: both statutory and custom items are represented in one normalized list.
5. **Categorize urgency.** Use `categorize_deadline(due)` for every concrete date. Completion criterion: each dated item has `days_remaining` and exactly one category: `overdue`, `within_7`, `within_30`, or `future`.
6. **Sort and filter for action.** Sort by due date ascending. Show overdue and due within 30 days by default; include future items when the user asks for a full calendar.
7. **Output a categorized table.** Include enough context for action: requirement, due date, days, authority, penalty/impact, source, and next action.
8. **Offer to spawn to-dos.** For overdue or within-7-day items, ask before writing to `todos.yaml`. Use `source: deadline-tracker` and tags such as `compliance`, jurisdiction code, and authority.

## `scan_deadlines.py` Interface

The implementation script is expected at:

```text
skills/deadline-tracker/scripts/scan_deadlines.py
```

Interface contract:

```bash
python scan_deadlines.py --config shared/config/company.yaml \
  --jurisdiction-dir shared/config/jurisdictions/ \
  [--full] [--json]
```

Expected JSON shape:

```json
{
  "company": "Acme Advisory Pte Ltd",
  "reference_date": "2026-07-09",
  "jurisdiction": "SG",
  "categories": {
    "overdue": [],
    "within_7": [],
    "within_30": [],
    "future": []
  },
  "summary": {
    "overdue": 0,
    "within_7": 1,
    "within_30": 2,
    "future": 8,
    "needs_event_date": 1
  }
}
```

Each item should include:

```yaml
name: Annual Return filing
due: "2026-07-31"
days_remaining: 22
category: within_30
authority: ACRA
penalty: "Late lodgement fees..."
source: statutory
notes: "Filed via BizFile+."
needs_event_date: false
```

If the script is not present, perform the workflow directly using `config_loader.py`, the jurisdiction YAML, and `date_utils.py`; do not fail the user request solely because the helper script is absent.

## Output Format

Use this table shape for human-facing replies:

```text
⏰ Deadlines — {Company Name} ({Jurisdiction}) as of {YYYY-MM-DD}

🔴 Overdue
| Due | Days | Requirement | Authority | Impact | Next action |
|---|---:|---|---|---|---|
| 2026-07-01 | -8 | GST return | IRAS | Late penalties | File F5 / confirm GST status |

🟡 Within 7 days
| Due | Days | Requirement | Authority | Impact | Next action |
|---|---:|---|---|---|---|

🟠 Within 30 days
| Due | Days | Requirement | Authority | Impact | Next action |
|---|---:|---|---|---|---|

⚪ Future
Shown only when requested.
```

If there are no actionable deadlines, say: `No overdue or next-30-day deadlines found.` Then include the next future statutory due date as situational awareness.

## Integrations

- **Daily Briefing:** consumes overdue, within-7, and within-30 categories. Daily Briefing is read-only and should not mark anything complete.
- **Weekly Review:** summarizes deadlines completed this week, upcoming next week, and risks that remained open.
- **To-Do List:** Deadline Tracker can spawn implementation tasks such as "Prepare ECI filing documents". Keep the statutory deadline itself in Deadline Tracker; keep the work item in To-Do List.
- **Drive Filer:** compliance documents and proof of filing should be filed under `01_Secretarial/` or `04_Finance/Tax/` according to `drive-map.yaml`.
- **Bookkeeper:** tax and GST deadlines may require invoice/expense data, but Bookkeeper remains the finance record source.

## Rules

- Treat statutory packs as default operational calendars, not legal advice.
- Never suppress an item because it is conditional; mark it `conditional` and state the condition.
- Event-driven items without event dates must be shown as `needs_event_date` when relevant.
- Roll filing dates that fall on weekends/public holidays to the next business day when `date_utils` supports the jurisdiction.
- Preserve the authority and penalty text from the jurisdiction pack in summaries.
- Ask before writing to-dos, editing config, or marking anything complete.
- Use ISO dates (`YYYY-MM-DD`) in data files. Human replies may additionally show readable dates.

## Common Pitfalls

1. **Confusing deadlines with tasks.** "File Annual Return by 31 July" is a deadline; "collect director signatures" is a to-do.
2. **Inventing event dates.** RORC and similar change-driven requirements need a trigger date. If none exists, ask or show `needs_event_date`.
3. **Ignoring first-period exceptions.** First AGM/accounts deadlines may differ. Use incorporation date and config overrides where known.
4. **Dropping conditional filings.** GST, ECI waivers, employer returns, and VAT/PAYE-style filings often depend on status. Show the condition.
5. **Assuming jurisdiction packs are exhaustive forever.** If an official notice has a concrete date, custom deadline beats the generic pack.
6. **Forgetting briefing consumers.** Keep machine-readable categories stable so Daily Briefing and Weekly Review do not need to re-interpret prose.

## Verification Checklist

- [ ] Config loaded or a clear missing-config error was reported.
- [ ] Correct jurisdiction pack loaded for `company.jurisdiction`.
- [ ] Statutory and custom deadlines both included.
- [ ] Every dated item has `due`, `days_remaining`, and category.
- [ ] Conditional and event-driven items are not silently dropped.
- [ ] Output is sorted by due date and grouped by urgency.
- [ ] Any proposed writes to `todos.yaml` were confirmed by the user.
