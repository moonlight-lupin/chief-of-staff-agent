---
name: weekly-review
description: "Use when producing the Chief-of-Staff Friday weekly review across deadlines, pipeline, bookkeeping, tasks, calendar, wiki, Drive filing, and self-sign activity."
version: 0.1.0
author: "Phronesis Applied"
license: MIT
metadata:
  hermes:
    tags: [chief-of-staff, weekly-review, reporting, pipeline, finance, knowledge-base]
    related_skills: [deadline-tracker, pipeline-manager, bookkeeper, todo-list, calendar-manager, note-taker, drive-filer, self-sign]
---

# Weekly Review

## Overview

Weekly Review is the Chief-of-Staff plugin's Friday aggregation layer. It answers: what got done, what moved, what money changed hands, what is coming next week, what needs attention, and what knowledge/assets were added.

The review pulls from all operational skills but remains **read-only by default**. It can recommend follow-up actions, carry-over tasks, or cleanup work, but it must not modify source files, Drive, calendar, invoices, documents, or cron jobs unless the user separately confirms through the relevant skill.

## When to Use

Use this skill when the user asks:

- "Weekly review"
- "How did this week go?"
- "What changed this week?"
- "What needs attention next week?"

Also use it for the scheduled Friday cron job at `delivery.weekly_review_time` from `company.yaml`.

Do **not** use this skill for real-time urgent command-center output; use `daily-briefing`.

## Source Contributions

| Source | What it contributes |
|---|---|
| Deadline Tracker | What was due this week, what is coming next week, what is overdue |
| Pipeline Manager | Deals moved, new leads, stale proposals, current active-stage distribution |
| Bookkeeper | Invoices sent, payments received, overdue/outstanding totals, P&L snapshot |
| To-Do List | Completion rate, completed items, carry-over items, overdue open tasks |
| Calendar | Meeting summary for this week, upcoming meetings next week |
| Note Taker | New wiki pages created/updated, knowledge-base growth |
| Drive Filer | Files filed this week, pending `00_Inbox`/unfiled items if known |
| Self-Sign | Documents signed this week, pending signature requests if known |
| Document Preparer | Documents generated this week if records/files indicate them |
| Deep Research / Entity Research | Research reports or dossiers created and filed this week |
| Travel Itinerary | Trips created/updated and upcoming travel if data exists |
| Backup | Backup status for the week if logs/Drive records exist |

Minimum required local files are resolved from `shared/config/company.yaml`:

```text
{project_root}/pipeline.yaml
{project_root}/todos.yaml
{project_root}/invoices.yaml
{project_root}/expenses.yaml
{wiki_path}/
```

Google Calendar access uses the `google-workspace` script. Drive filing and self-sign activity may be inferred from YAML links, wiki logs, document filenames, Drive listings, or skill logs if present.

## Time Windows

Use `delivery.timezone` from `company.yaml`.

- **This week:** Monday 00:00 through Friday review time by default; if run on a weekend, use the full Monday-Sunday week containing the run date.
- **Next week:** Next Monday 00:00 through next Sunday 23:59:59.
- **Due this week:** due date within this week, regardless of completion status.
- **Completed this week:** completion/payment/signing/filing date within this week.

If the user requests a different week, honor that window and state it in the heading.

## Google API Command Pattern

All Google calls must use:

```bash
python ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py \
  --account {account} --as {delegate} {service} {command}
```

Calendar windows:

```bash
python ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py \
  --account {account} --as {delegate} calendar list \
  --time-min {week_start_iso} --time-max {week_end_iso}

python ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py \
  --account {account} --as {delegate} calendar list \
  --time-min {next_week_start_iso} --time-max {next_week_end_iso}
```

Optional Drive queries for filing/signed-doc summaries should use `drive` commands through the same wrapper only when Drive root configuration exists. Do not fail the weekly review if Drive summaries are unavailable; state the limitation.

## Aggregation Workflow

1. **Load config.** Read `shared/config/company.yaml`; resolve `paths.project_root`, `paths.wiki_path`, `delivery.weekly_review_time`, `delivery.weekly_review_day`, and `delivery.timezone`. Completion criterion: reporting windows are timezone-explicit.
2. **Load YAML stores.** Read `pipeline.yaml`, `todos.yaml`, `invoices.yaml`, and `expenses.yaml` from `project_root`. Treat missing stores as empty but name the missing files under limitations.
3. **Deadline review.** Use Deadline Tracker rules to identify deadlines due this week, completed if records indicate completion, overdue, and due next week. Completion criterion: overdue and next-week items are not omitted.
4. **Pipeline review.** Detect new leads/deals created this week, deals with stage movement or activity notes this week, closed/won/lost items, and stale active proposals. Completion criterion: each movement includes deal ID/client code, old/new stage if available, and date/source.
5. **Finance review.** From invoices and expenses, compute invoices sent this week, payments received this week, outstanding AR by currency, overdue AR, AP due/paid if present, and a simple P&L snapshot for the week/month-to-date when possible. Completion criterion: no cross-currency summing without grouping.
6. **To-do review.** Compute completed count, opened count, open carry-over count, overdue open count, and completion rate. Completion criterion: carry-over list is sorted by priority/due date.
7. **Calendar review.** Fetch meetings this week and next week. Summarize significant client/prospect meetings and next-week prep needs; omit routine internal holds unless relevant.
8. **Knowledge review.** Inspect `wiki_path` for pages created/modified this week, `index.md` changes, and notable new entity/concept pages. Completion criterion: knowledge-base section includes count and representative topics.
9. **Drive/document/signing review.** Summarize files filed, documents prepared, and self-signed documents this week when source records/logs/filenames are available. Completion criterion: signed/filed items are shown with paths or IDs, not assumed.
10. **Needs attention synthesis.** Combine overdue deadlines, overdue invoices, stale deals, carry-over high-priority to-dos, unsigned/pending docs, and upcoming important meetings. Completion criterion: all red/yellow issues map back to a source.
11. **Render weekly review.** Use the required section order, skipping empty details but keeping top-level section headings when a concise `None found` is useful for the weekly management rhythm.

## Output Format

Use this exact top-level shape:

```text
📊 Weekly Review — Week of {date}

✅ Completed This Week
📈 Pipeline Movement
💰 Finance Summary
📅 Next Week
⚠️ Needs Attention
📝 Knowledge Base
```

Recommended detail format:

```text
📊 Weekly Review — Week of 2026-07-06

✅ Completed This Week
• To-dos: 12 completed / 18 opened (67% completion); 6 carried over
• Deadlines met: Annual Return prep submitted
• Documents: 2 signed, 3 filed to Drive

📈 Pipeline Movement
• New: deal-014 / ACME — Lead — SGD 8,000
• Moved: deal-009 / BETA — Proposal Sent → Contract Sent
• Stale: 2 proposals >14d inactive

💰 Finance Summary
• Invoices sent: SGD 4,500 across 1 invoice
• Payments received: SGD 2,000 across 1 invoice
• Outstanding AR: SGD 12,500 across 3 unpaid invoices; SGD 3,000 overdue
• P&L snapshot: revenue SGD X vs expenses SGD Y (cash basis)

📅 Next Week
• Meetings: 4 client/prospect meetings; 2 need prep briefs
• Deadlines: 1 due within 7 days, 3 due within 30 days
• To-dos due: 5 open items

⚠️ Needs Attention
• {source-linked overdue/stale/high-priority items}

📝 Knowledge Base
• 5 pages created/updated: Acme Corp, Pricing Notes, SG Tax Q3...
```

Formatting rules:

- Protect confidentiality; use client codes if `delivery.use_client_codes: true`.
- Keep financial totals grouped by currency.
- Show counts first, then exceptions/items needing action.
- Include source IDs: deal ID, invoice ID, to-do ID, calendar event title/date, file path.
- Do not claim completion unless a completion/payment/signature/file timestamp or user-confirmed record supports it.

## Source-Specific Logic

### Deadline Tracker

Use the same jurisdiction pack and custom deadline logic as `deadline-tracker`. Weekly Review should answer:

- due this week,
- completed this week when known,
- overdue at review time,
- due next week,
- due within 30 days for awareness.

If completion status is not tracked for a statutory deadline, say `status not recorded` instead of assuming missed/met.

### Pipeline Manager

Look for fields commonly used by the pipeline skill:

- `created` within week → new lead/deal,
- `last_activity` within week → recent activity,
- `notes[].date` within week with movement text,
- `stage_history[]` if present,
- active stage with inactivity > `stale_threshold_days` → stale.

Terminal stages include `Paid`, `Lost`, `Cancelled`, and similar configured final states.

### Bookkeeper

From `invoices.yaml`:

- `direction: sent`, `issue_date` within week → invoices sent,
- `status: paid`, `paid_date` within week → payments received,
- `direction: sent`, not paid/cancelled → outstanding AR,
- unpaid due date before today → overdue AR,
- `direction: received` not paid → AP due/outstanding.

From `expenses.yaml`:

- expenses dated within week/month-to-date by category,
- recurring expenses due next week if configured.

Use cash basis unless `bookkeeper.revenue_recognition` says otherwise.

### To-Do List

From `todos.yaml`:

- `completed` within week or `status: done` with date → completed,
- `created` within week → opened,
- `status: open` → carry-over,
- due date before today and open → overdue.

Completion rate:

```text
completed_this_week / max(completed_this_week + open_created_this_week, 1)
```

State the formula if the result might be misleading due to missing dates.

### Calendar

Summarize:

- number of meetings this week,
- client/prospect meetings,
- significant internal/project meetings,
- upcoming next-week meetings with Meet links when available,
- meetings that generated action items if notes/todos indicate it.

### Note Taker

Inspect `wiki_path` recursively for Markdown files. Count files created/modified this week when filesystem timestamps are available. Prefer page frontmatter dates if present. Include:

- new entity pages,
- new concept pages,
- new raw sources ingested,
- orphan/lint warnings only if a note-taker log exists.

### Drive Filer / Self-Sign / Document Preparer

Use available evidence only:

- documents linked in pipeline records with `added` date this week,
- invoice document paths with issue dates this week,
- signed PDFs named `*_signed.pdf` modified this week,
- Drive listings under configured folders if accessible,
- skill logs if present.

If evidence is not available, put a concise limitation in `Needs Attention` or omit the subsection rather than fabricating counts.

## Cron Setup

Weekly Review is normally installed as a Hermes cron job on `delivery.weekly_review_day` at `delivery.weekly_review_time`.

### Schedule Examples

For Friday 17:00 local time:

```text
0 17 * * 5
```

Map days as standard cron: Sunday `0`, Monday `1`, ..., Friday `5`, Saturday `6`.

### Complete Cron Prompt Template

```bash
cat > /tmp/chief-of-staff-weekly-review.prompt <<'PROMPT'
You are running the scheduled Chief of Staff Weekly Review.

This cron run is self-contained. Do not rely on conversation history.

Plugin root: /root/.hermes/plugins/chief-of-staff
Config file: /root/.hermes/plugins/chief-of-staff/shared/config/company.yaml
Jurisdiction packs: /root/.hermes/plugins/chief-of-staff/shared/config/jurisdictions/
Required skill: chief-of-staff:weekly-review

Task:
1. Load the chief-of-staff weekly-review skill.
2. Read company.yaml for delivery.weekly_review_day, delivery.weekly_review_time, delivery.channel, delivery.timezone, google account/delegate, paths.project_root, and paths.wiki_path.
3. Define this-week and next-week windows in delivery.timezone.
4. Pull deadline signals from company.yaml plus the jurisdiction pack: due this week, overdue, and coming next week.
5. Read pipeline.yaml for new deals, stage movements/activity, closed/lost deals, and stale proposals.
6. Read invoices.yaml and expenses.yaml for invoices sent, payments received, overdue/outstanding AR/AP, and a P&L snapshot.
7. Read todos.yaml for completed/opened/carry-over/overdue tasks and completion rate.
8. Use google_api.py to pull calendar meetings for this week and next week.
9. Inspect paths.wiki_path for new or updated wiki pages this week.
10. Summarize Drive Filer, Document Preparer, Self-Sign, Deep Research, Entity Research, Travel, and Backup activity only where source records/logs/files provide evidence.
11. Produce the Weekly Review in the required format:
   📊 Weekly Review — Week of {date}
   ✅ Completed This Week
   📈 Pipeline Movement
   💰 Finance Summary
   📅 Next Week
   ⚠️ Needs Attention
   📝 Knowledge Base
12. Protect confidentiality; use client codes when delivery.use_client_codes is true.
13. Do not modify email, calendar, Drive, YAML files, documents, cron jobs, or source data.

Deliver the final review to the configured delivery.channel.
PROMPT

hermes cron create "0 17 * * 5" \
  --title "chief-of-staff:weekly-review" \
  --skills "chief-of-staff:weekly-review,google-workspace" \
  --delivery "telegram" \
  --workdir "/root/.hermes/plugins/chief-of-staff" \
  --prompt-file /tmp/chief-of-staff-weekly-review.prompt
```

Replace the schedule and delivery channel with `company.yaml` values. If your Hermes cron CLI has different flags, run `hermes cron create --help` and preserve the same prompt contents.

## Common Pitfalls

1. **Claiming activity without evidence.** Use file timestamps, YAML dates, Google results, or logs. Otherwise say unavailable.
2. **Treating Friday-only as the whole week.** Use the defined week window and state it.
3. **Mixing currencies in finance summaries.** Group totals by currency.
4. **Forgetting carry-over.** Weekly Review should show both wins and unresolved work.
5. **Overloading `Needs Attention`.** Include only overdue, stale, blocked, high-priority, or next-week prep items.
6. **Mutating records.** Reviews are reports; defer writes to source skills with confirmation.
7. **Breaking confidentiality in scheduled delivery.** Client codes and source IDs are safer than full names/details.

## Verification Checklist

- [ ] `company.yaml` loaded and report windows resolved in `delivery.timezone`.
- [ ] All local YAML stores were read or explicitly reported missing.
- [ ] Calendar data was pulled through `google_api.py` when available.
- [ ] Deadlines, pipeline, finance, to-dos, calendar, wiki, filing, and signing sources were considered.
- [ ] Counts and totals are source-grounded.
- [ ] Financial totals are grouped by currency.
- [ ] Needs Attention includes only actionable risk/follow-up items.
- [ ] Cron prompt is self-contained.
- [ ] No source data was modified.
