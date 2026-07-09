---
name: daily-briefing
description: "Use when producing the Chief-of-Staff daily command-center briefing from Gmail, Calendar, deadlines, pipeline, to-dos, and bookkeeping sources."
version: 0.1.0
author: "Phronesis Applied"
license: MIT
metadata:
  hermes:
    tags: [chief-of-staff, briefing, gmail, calendar, deadlines, finance]
    related_skills: [google-workspace, deadline-tracker, pipeline-manager, todo-list, bookkeeper, calendar-manager]
---

# Daily Briefing

## Overview

Daily Briefing is the Chief-of-Staff plugin's daily command center. It consolidates the minimum set of signals the operator needs to act today: urgent email, today's and tomorrow's calendar, statutory/commercial deadlines, stale pipeline, overdue/open to-dos, and accounts receivable/payable risk.

This skill is intentionally **read-only**. It observes and reports. It must never mark email as read, update a calendar event, move a deal, edit a to-do, change invoices, file documents, or create new tasks without a separate explicit user instruction and the relevant source skill loaded.

## When to Use

Use this skill when the user asks:

- "Briefing"
- "What's happening today?"
- "What needs my attention?"
- "Daily briefing"
- "Anything urgent?"

Also use it for the scheduled daily cron job at `delivery.briefing_time` from `company.yaml`.

Do **not** use this skill for deep analysis of one source. If the user asks only for deadlines, pipeline, invoices, calendar, or tasks, load the dedicated source skill instead.

## Required Sources

Daily Briefing pulls from exactly these six source families:

| Source | What to read | How |
|---|---|---|
| Gmail inbox | Unread priority threads, unread client threads, signature requests, invoice notices | Use `google_api.py` with query templates from `shared/config/queries.yaml` |
| Calendar | Today + tomorrow events, especially Google Meet links | Use `google_api.py calendar list` |
| Deadline Tracker | Overdue, due ≤7 days, due ≤30 days | Read `company.yaml` + jurisdiction pack; use `deadline-tracker`/`date_utils.py` logic |
| Pipeline Manager | Stale deals and recent stage/activity movement | Read `{project_root}/pipeline.yaml` |
| To-Do List | Open items, overdue items, high-priority items | Read `{project_root}/todos.yaml` |
| Bookkeeper | Overdue invoices and outstanding AR total | Read `{project_root}/invoices.yaml` |

Configuration comes from `shared/config/company.yaml`:

```yaml
google:
  account: default                 # optional google-workspace account profile
  service_account_path: ~/.hermes/secrets/acme-google-service-account.json
  delegate_email: founder@example.com
paths:
  project_root: ~/.hermes/projects/acme/
delivery:
  briefing_time: "20:00"
  channel: telegram
  timezone: Asia/Singapore
  use_client_codes: false
```

If `shared/config/company.yaml` is missing, stop and tell the user to copy `shared/config/company.yaml.example` to `shared/config/company.yaml` and complete onboarding. Do not guess company data.

## Google API Command Pattern

All Google calls must go through the external `google-workspace` skill script:

```bash
python ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py \
  --account {account} --as {delegate} {service} {command}
```

Resolve `{account}` from `company.yaml` `google.account` when present; otherwise use the account/profile value expected by the installed `google-workspace` skill. Resolve `{delegate}` from `google.delegate_email`.

### Gmail Query Rules

1. Load templates from `shared/config/queries.yaml` first. If absent, fall back to `shared/config/queries.yaml.example` and state that defaults are being used.
2. Use only pre-built templates; do not invent broad mailbox searches for scheduled briefings.
3. Substitute placeholders such as `{client_name}`, `{contact_email}`, `{domain}`, `{invoice_id}`, and `{days}` from pipeline/invoice/company records.
4. For generic unread priority, use `briefing_unread_priority` as-is.
5. For client-specific unread checks, iterate active deals in `pipeline.yaml` and apply `briefing_unread_clients` per deal/contact.
6. For signature and invoice flags, use `documents_for_signature`, `invoices_received`, and `invoices_sent_followup` when present.

Example command shape:

```bash
python ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py \
  --account {account} --as {delegate} gmail search \
  --query '{rendered_query}' --max-results {max_results}
```

Return enough metadata for the briefing: thread/message ID, sender, subject, date, snippet, labels, and whether attachments exist. Do not fetch message bodies unless snippets are insufficient to determine urgency.

### Calendar Query Rules

List events from local midnight today through tomorrow 23:59:59 in `delivery.timezone`:

```bash
python ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py \
  --account {account} --as {delegate} calendar list \
  --time-min {today_start_iso} --time-max {tomorrow_end_iso}
```

Normalize each event into: local start/end, title, attendees, organizer, location, Google Meet/conference link, and event ID. Include Google Meet links prominently. Skip declined events and low-signal all-day holds unless they affect availability.

## Aggregation Workflow

1. **Load central config.** Use `shared/scripts/config_loader.py` if available, otherwise read `shared/config/company.yaml` directly. Completion criterion: `company`, `google`, `paths.project_root`, and `delivery` are known.
2. **Resolve dates.** Get the current date in `delivery.timezone`. Use ISO dates internally and human-readable dates in the heading. Completion criterion: today/tomorrow windows are timezone-explicit.
3. **Load local YAML sources.** Read `{project_root}/pipeline.yaml`, `{project_root}/todos.yaml`, `{project_root}/invoices.yaml`, and optionally `{project_root}/expenses.yaml` if finance context requires it. Missing files count as empty lists after stating the file is not initialized.
4. **Run Gmail templates.** Execute `briefing_unread_priority`, client-specific `briefing_unread_clients`, and relevant document/invoice templates. Completion criterion: unread count and key threads are deduplicated by thread/message ID.
5. **Run Calendar query.** Fetch today + tomorrow events and extract Meet links. Completion criterion: every meeting with a conference link shows a clickable join URL.
6. **Compute deadlines.** Use `deadline-tracker` rules: statutory pack + custom deadlines, categorized as overdue, within 7 days, within 30 days. Completion criterion: no due-within-30 item is omitted.
7. **Detect pipeline attention.** Flag stale non-terminal deals using `stale_threshold_days` and summarize recent movements from `notes`, `stage_history`, `last_activity`, or document timestamps where available. Completion criterion: stale deal count and most important stale items are shown.
8. **Detect finance attention.** In `invoices.yaml`, calculate overdue AR (`direction: sent` not paid and due before today), outstanding AR total (sent/unpaid), and any AP due/overdue if present. Completion criterion: totals preserve currency and invoice IDs.
9. **Detect task attention.** In `todos.yaml`, show overdue, due today, and open high-priority tasks first. Completion criterion: open items are sorted by priority then due date.
10. **Rank urgent items.** Build `Urgent / Action Needed` from same-day response needs, overdue deadlines, overdue invoices/AP, overdue to-dos, meetings needing preparation, signature requests, and stale deals that require follow-up. Completion criterion: every urgent item links back to its source.
11. **Render sections.** Use the required output format and skip empty sections. If nothing is urgent, lead with exactly: `No urgent items.`

## Urgency Classification

An item belongs in `🔴 Urgent / Action Needed` if any of these are true:

- Gmail template marks it as urgent/action required/approval/deadline and it is unread.
- Calendar has a meeting today that lacks obvious prep or has client/pipeline context.
- Deadline is overdue or due within 7 days.
- Invoice is overdue or payment is due today.
- To-do is overdue, due today, or `priority: high` with no later due date.
- Deal is stale and in an active commercial stage such as `Proposal Sent`, `Contract Sent`, `NDA Signed`, or `Contract Signed`.
- Email asks for signature or review of a document.

Do not inflate urgency. If a source has only FYI items, keep them in its section, not in urgent.

## Output Format

Use this exact shape, omitting sections that have no content:

```text
📋 Daily Briefing — {date}

🔴 Urgent / Action Needed
{items requiring response today}

📅 Calendar (Today + Tomorrow)
{events with Google Meet links}

⏰ Deadlines
🔴 Overdue / 🟡 ≤7 days / 🟠 ≤30 days

📊 Pipeline
{stale deals, recent stage movements}

💰 Finance
{overdue invoices, outstanding AR total}

✅ To-Dos
{open items sorted by priority}

📧 Inbox Summary
{unread count, key threads}

🟢 All Clear
{or: note what's handled}
```

Formatting rules:

- If nothing is urgent, place `No urgent items.` immediately after the heading before other sections.
- Skip empty sections completely; do not print headings with `None` or `No items` unless the absence itself is useful.
- Keep each bullet short: action, source, due/date, owner/contact, link/ID if available.
- Use client codes instead of full client names when `delivery.use_client_codes: true` or the delivery channel may be observed.
- Do not expose full email bodies, private invoice details, or sensitive contract terms in notification channels. Include IDs and short summaries.

## Section Guidance

### 🔴 Urgent / Action Needed

Use bullets such as:

```text
• Reply today: {sender/client_code} — {subject} ({thread_id})
• Deadline overdue: {requirement} was due {date} ({authority})
• Invoice overdue: {invoice_id} — {currency} {amount} due {date}
• Meeting prep: {time} {title} — {meet_link}
```

### 📅 Calendar

Group by `Today` and `Tomorrow`. Include Meet links:

```text
Today
• 10:00–10:30 — Acme sync — [Meet]({url}) — attendees: John, Alicia
Tomorrow
• 14:00–15:00 — Proposal review — [Meet]({url})
```

### ⏰ Deadlines

Show categories only when non-empty:

```text
🔴 Overdue
• {date} ({days}d): {name} — {authority}; {next action}
🟡 ≤7 days
• ...
🟠 ≤30 days
• ...
```

### 📊 Pipeline

Prioritize stale active deals and recent stage movement:

```text
• Stale: {deal_id}/{client_code} in {stage} for {days_inactive}d — suggested follow-up: {action}
• Moved: {deal_id}/{client_code} {from_stage} → {to_stage} on {date}
```

### 💰 Finance

Preserve currencies; do not sum across currencies without grouping:

```text
• Overdue AR: {invoice_id} {currency} {amount} from {client_code}, {days_late}d late
• Outstanding AR: SGD 12,500 across 3 unpaid invoices
```

### ✅ To-Dos

Sort by `priority` (`high`, `medium`, `low`) then due date. Include source/tag:

```text
• HIGH due today — {title} ({id}, tags: sales/acme)
```

### 📧 Inbox Summary

Report total unread retrieved and key threads:

```text
Unread priority threads: 5. Client unread threads: 3.
• {sender/client_code}: {subject} — {why it matters}
```

### 🟢 All Clear

Use only when there is no urgent/action-needed content. State what was checked:

```text
🟢 All Clear
Checked Gmail priority queries, calendar, deadlines, pipeline, invoices, and to-dos. No same-day action items found.
```

## Cron Setup

Daily Briefing is normally installed as a Hermes cron job using `delivery.briefing_time`, `delivery.channel`, and `delivery.timezone` from `company.yaml`.

### 1. Read Config

```bash
python - <<'PY'
import yaml, pathlib
cfg = yaml.safe_load(pathlib.Path('/root/.hermes/plugins/chief-of-staff/shared/config/company.yaml').read_text())
print(cfg['delivery']['briefing_time'], cfg['delivery']['timezone'], cfg['delivery']['channel'])
PY
```

### 2. Convert to Cron Schedule

For a daily briefing at `HH:MM`, use:

```text
{minute} {hour} * * *
```

Example for `20:00`:

```text
0 20 * * *
```

### 3. Create the Cron Job

Use the Hermes cron CLI. If your installed CLI flags differ, run `hermes cron create --help` and preserve the schedule, skills, delivery channel, workdir, and prompt file.

```bash
cat > /tmp/chief-of-staff-daily-briefing.prompt <<'PROMPT'
You are running the scheduled Chief of Staff Daily Briefing.

This cron run is self-contained. Do not rely on conversation history.

Plugin root: /root/.hermes/plugins/chief-of-staff
Config file: /root/.hermes/plugins/chief-of-staff/shared/config/company.yaml
Query templates: /root/.hermes/plugins/chief-of-staff/shared/config/queries.yaml
Jurisdiction packs: /root/.hermes/plugins/chief-of-staff/shared/config/jurisdictions/
Required skill: chief-of-staff:daily-briefing

Task:
1. Load the chief-of-staff daily-briefing skill.
2. Read company.yaml for delivery.briefing_time, delivery.channel, delivery.timezone, google account/delegate, and paths.project_root.
3. Pull Gmail unread priority/client/signature/invoice signals using google_api.py and queries.yaml templates only.
4. Pull Google Calendar events for today and tomorrow with Meet links using google_api.py.
5. Read deadlines from company.yaml plus the jurisdiction pack and categorize overdue, ≤7 days, and ≤30 days.
6. Read pipeline.yaml, todos.yaml, and invoices.yaml from paths.project_root.
7. Produce the Daily Briefing in the required format, skipping empty sections.
8. If nothing is urgent, lead with exactly: "No urgent items."
9. Protect confidentiality; use client codes when delivery.use_client_codes is true.
10. Do not modify email, calendar, YAML files, Drive, cron jobs, or any source data.

Deliver the final briefing to the configured delivery.channel.
PROMPT

hermes cron create "0 20 * * *" \
  --title "chief-of-staff:daily-briefing" \
  --skills "chief-of-staff:daily-briefing,google-workspace" \
  --delivery "telegram" \
  --workdir "/root/.hermes/plugins/chief-of-staff" \
  --prompt-file /tmp/chief-of-staff-daily-briefing.prompt
```

Replace `0 20 * * *` and `--delivery telegram` with the values from `company.yaml`.

## Confidentiality Rules

- Use client codes or deal IDs where public delivery channels may be visible.
- Never paste full email bodies, contract clauses, or bank/payment details into scheduled notifications.
- Include enough source identifiers for follow-up: email thread ID, event ID, deal ID, to-do ID, invoice ID.
- Keep scheduled output concise. The briefing is a command center, not a data dump.

## Common Pitfalls

1. **Mutating sources during briefing.** Do not mark emails read, create to-dos, move deals, or update invoices from this skill.
2. **Inventing Gmail queries.** Scheduled runs must use `queries.yaml` templates so behavior is predictable and auditable.
3. **Losing timezone.** Calendar windows and due-date wording must use `delivery.timezone`.
4. **Double-counting email threads.** Deduplicate by thread/message ID across generic and client-specific searches.
5. **Summing mixed currencies.** Group finance totals by currency.
6. **Printing empty headings.** Skip empty sections; only include `All Clear` when genuinely clear.
7. **Overexposing private details.** Notifications should be actionable but not a data leak.

## Verification Checklist

- [ ] `company.yaml` was loaded and `project_root`, `delivery`, and `google` settings resolved.
- [ ] Gmail searches used `google_api.py` and `queries.yaml` templates.
- [ ] Calendar query covered today + tomorrow in the configured timezone and included Meet links.
- [ ] Deadlines were categorized into overdue, ≤7 days, and ≤30 days.
- [ ] `pipeline.yaml`, `todos.yaml`, and `invoices.yaml` were read from `project_root`.
- [ ] Urgent items are source-linked and not exaggerated.
- [ ] Empty sections were skipped.
- [ ] If no urgent items exist, the briefing leads with `No urgent items.`
- [ ] No source data was modified.
