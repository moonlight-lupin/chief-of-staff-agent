---
name: meeting-prep
description: "Use when preparing a concise pre-meeting intelligence brief from calendar event metadata, recent Gmail threads, wiki notes, pipeline status, invoices, to-dos, and entity research. When the user addresses 'Chief of Staff' (the CoS assistant name) for meeting prep, use the company workspace account configured in company.yaml for your organization, NOT the agent's personal email."
version: 0.1.0
author: "moonlight-lupin"
license: Apache-2.0
metadata:
  hermes:
    tags: [chief-of-staff, meetings, calendar, gmail, pipeline, briefing]
    related_skills: [calendar-manager, google-workspace, note-taker, pipeline-manager, bookkeeper, entity-research, todo-list]
---

# Meeting Prep

## Overview

Meeting Prep produces a pre-meeting intelligence brief: who is joining, why the meeting matters, what's open, what to say, and what action items may carry forward. It is invoked either manually by the user ("Prep me for X") or automatically by Calendar Manager's one-shot cron reminder, usually 15 minutes before a meeting.

The skill is read-first and brief-first. It gathers context, summarizes, and suggests talking points. It does not change calendar events, send emails, update pipeline stages, mark invoices, or write to-dos unless the user separately asks and confirms through the relevant source skill.

## When to Use

Use this skill when:

- The user says "Prep me for X", "Who am I meeting?", "Give me context for this meeting", or "Meeting prep".
- Calendar Manager invokes a self-contained cron prompt with event title, attendees, and Meet link.
- A meeting starts soon and the operator needs a concise context packet.

Also use when the operator addresses their Chief of Staff by its configured name (`assistant.name` in company.yaml), e.g. "Ask <name> to check my email" / "<name>, what's on today?".

Do **not** use this skill for scheduling or modifying meetings; use `calendar-manager`. Do **not** use it for general entity due diligence unless the meeting context specifically requires it; use `entity-research` directly.

## Inputs

| Source | What to pull | How |
|---|---|---|
| Calendar event | Title, start/end, attendees, organizer, Meet link, description, event ID | Input from user or Calendar Manager cron; query via `workspace_actions.py calendar-context` |
| Gmail | Recent threads with attendees from the last 90 days | Use `workspace_actions.py gmail-context` with attendee-specific queries |
| Note Taker wiki | Pages mentioning the contact, company, aliases, or deal | Search `paths.wiki_path` for names/domains |
| Pipeline Manager | Deal/client/prospect status | Read `{project_root}/pipeline.yaml` |
| Bookkeeper | Invoice/payment status for client | Read `{project_root}/invoices.yaml` |
| To-Do List | Action items tagged with contact/company/deal | Read `{project_root}/todos.yaml` |
| Entity Research | Quick entity scan if no prior knowledge exists | Load `entity-research` only when local sources are empty or insufficient |

Configuration comes from `shared/config/company.yaml`; use `paths.project_root`, `paths.wiki_path`, `google.delegate_email`, `google.account` or `google.service_account_path`, and `delivery.timezone`.

## Workspace Access

Meeting Prep is read-only. It needs recent mail with attendees, the calendar event/context, and optional related files. Prefer the `workspace_actions.py` wrapper — it routes through `WorkspaceClient`:

```bash
python skills/meeting-prep/scripts/workspace_actions.py gather --event-id <id> --attendees a@x.com,b@y.com
python skills/meeting-prep/scripts/workspace_actions.py gmail-context --query "from:a@x.com" --max 5
python skills/meeting-prep/scripts/workspace_actions.py calendar-context --start 2026-07-09 --end 2026-07-16
python skills/meeting-prep/scripts/workspace_actions.py drive-context --query "meeting notes" --max 5
```

Normalize records to the canonical `message`, `event`, and `file` shapes in `shared/scripts/schemas.py`. Obtain the data through the first available path in this order:

1. **Agent-side connectors** — native Gmail / Calendar / Drive / Microsoft 365 connectors, **or an already-authed Hermes Composio MCP session** (mail/calendar/files read tools). Use as a **read front-end only**; CoS writes still go through `get_workspace_client`.
2. **The configured workspace provider** via `shared/scripts/workspace_client.py`: `get_workspace_client(config).mail_search(...)`, `.calendar_list(start, end)`, `.files_search(...)`. The provider is chosen by `integrations.workspace.provider` in `company.yaml` (`google_api` | `composio` | `m365` | `agent`).
3. **Pre-fetched data via `--input`** — when the agent has already gathered the context with path 1, pass it to `workspace_actions.py --input <file>` as a `schemas.py` workspace envelope (`{messages: [...], events: [...], files: [...]}`).

For ad hoc attendee searches when there is no pipeline client name, use a narrow attendee email query. The template below is the Gmail search dialect; the `m365` provider translates the same intent to Microsoft Graph, and native connectors accept natural-language equivalents:

```text
newer_than:90d (from:{attendee_email} OR to:{attendee_email})
```

Do not fetch or expose full message bodies unless snippets are insufficient to identify an open item.

## Workflow

1. **Identify the meeting.** Use the cron-provided event metadata or resolve the user's reference against calendar events. Completion criterion: title, start time, attendees, and Meet URL (if any) are known.
2. **Normalize attendees.** Extract names, emails, domains, companies, and likely roles from event metadata, email addresses, pipeline records, and wiki pages. Exclude the configured `user.email` and internal aliases from the primary `Who` line.
3. **Search recent Gmail.** For each external attendee or matched client, search recent 90-day threads. Completion criterion: last contact date/channel and 1-3 relevant threads are identified or explicitly absent.
4. **Search the wiki.** Search `paths.wiki_path` for attendee name, company name, email domain, deal ID, and known aliases. Completion criterion: relevant pages and last-meeting/action references are summarized with file paths.
5. **Match pipeline records.** Read `pipeline.yaml` and match by `client_name`, `contact_name`, `contact_email`, or email domain. Completion criterion: current stage, value/currency, last activity, and stale status are known when a match exists.
6. **Match invoices.** Read `invoices.yaml`; show unpaid/overdue AR for the matched client/deal, and AP only if relevant to the meeting. Completion criterion: invoice IDs, amounts, due dates, and statuses are summarized without exposing payment details.
7. **Match to-dos.** Read `todos.yaml`; extract open to-dos tagged with attendee, company, deal ID, or meeting title. Completion criterion: action items from prior meetings are listed or marked absent.
8. **Fill gaps.** If there is no local wiki, pipeline, invoice, or Gmail context for an external company/contact, run a quick `entity-research` scan limited to identity/background and recent public context. Completion criterion: no more than 2-3 public facts are used to orient the meeting.
9. **Generate talking points.** Suggest 1-3 bullets grounded in the gathered context: follow-up, decision needed, risk, relationship cue, or next step. Completion criterion: every talking point maps to a source signal.
10. **Render concise brief.** Use the required output template and skip sections that would be empty except for required labels.

## Output Format

Use this format for the final answer:

```text
⏰ Meeting in 15 min: {title}

🎥 [Join Google Meet]({url})

📋 Who: {name}, {role} at {company}
Last contact: {N days ago} ({channel})

🔑 Open items:
• {pipeline status if client}
• {outstanding invoices if any}
• {recent email thread summary}

💡 Suggested talking points:
• {1-3 bullets}

📝 Action items from last meeting:
• {to-dos tagged with this contact}
```

Rendering rules:

- If the meeting is not exactly 15 minutes away, adjust the first line: `⏰ Meeting in {N} min: {title}` or `⏰ Meeting prep: {title}`.
- If there is no Meet link, replace the link line with `🎥 No Google Meet link found` and include the physical/other location if present.
- For multiple external attendees, use a compact `Who` block:
  - primary contact first,
  - then `Also attending: ...`,
  - or `External attendees: ...` when roles are unknown.
- If no prior local context exists, state `No prior local context found; using quick public/entity scan where available.`
- Keep brief enough to read in under one minute.

## Matching Rules

### Contacts and Companies

Priority order for matching:

1. Exact attendee email equals `pipeline.contact_email`.
2. Attendee email domain matches a client/prospect domain in pipeline notes/wiki aliases.
3. Exact or fuzzy company name from event title/description matches `pipeline.client_name`.
4. Exact contact name matches `pipeline.contact_name` or wiki entity title.
5. User-provided meeting target overrides fuzzy matches.

If multiple deals match the same contact, show all active deals but keep the main `Open items` section to the most urgent/current one.

### Last Contact

Derive last contact from:

1. Most recent Gmail thread with the attendee.
2. Most recent pipeline note/activity with that contact.
3. Most recent wiki meeting note or action item.
4. Calendar prior event if available.

Show the channel as `email`, `pipeline note`, `wiki note`, `calendar`, or `unknown`.

### Action Items from Last Meeting

Search `todos.yaml` for:

- tags matching contact/company/deal ID,
- source values such as `meeting`, `meeting-prep`, or `note-taker`,
- titles containing the contact/company,
- due/open status.

Prefer open items. If prior completed items matter, summarize under context rather than action items.

## Cron Invocation Contract

Calendar Manager may create a one-shot cron prompt with event metadata. Meeting Prep must treat that prompt as complete context and should not ask follow-up questions unless the event itself is ambiguous.

Minimum cron-provided fields:

```text
Meeting starts in {N} minutes.
Title: {event_title}
Start: {event_start_iso}
End: {event_end_iso}
Timezone: {timezone}
Attendees: {attendee_names_and_emails}
Organizer: {organizer}
Google Meet link: {meet_link}
Calendar event ID: {event_id}
Delivery channel: {delivery_channel}
```

If a required field is missing, proceed with available fields and state the limitation instead of blocking the reminder.

## Confidentiality Rules

- Use short summaries, IDs, and links; do not quote sensitive emails or contract terms verbatim.
- Do not include bank details, private payment instructions, or full invoice contents.
- For scheduled delivery, prefer client codes/deal IDs when configured by Daily Briefing/company settings.
- Public entity scan output must be clearly labeled as public context, not verified internal knowledge.

## Common Pitfalls

1. **Over-prepping.** A 15-minute reminder must be concise; do not produce a research memo.
2. **Treating attendees as clients without evidence.** Match to pipeline or invoices before calling someone a client.
3. **Missing the Meet link.** The join link must be prominent when available.
4. **Inventing roles.** Infer roles only from sources; otherwise say `role unknown`.
5. **Exposing email bodies.** Summarize threads; avoid verbatim private content.
6. **Writing follow-up tasks automatically.** Suggest action items, but writes require a separate confirmed `todo-list` action.
7. **Running entity research unnecessarily.** Use it only when local sources are empty or the user asks for public background.

## Verification Checklist

- [ ] Meeting title, time, attendees, and Meet/location are identified.
- [ ] Mail searches used an approved workspace access path (connector tools, workspace_client, or --input) and covered the last 90 days.
- [ ] Wiki search checked `paths.wiki_path` for contact/company mentions.
- [ ] Pipeline, invoices, and to-dos were read from `paths.project_root`.
- [ ] Last contact is grounded in a source or marked unknown.
- [ ] Suggested talking points are source-grounded and limited to 1-3 bullets.
- [ ] The brief includes the join link prominently when available.
- [ ] No source data was modified.
