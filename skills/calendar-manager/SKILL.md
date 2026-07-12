---
name: calendar-manager
description: Calendar visibility and safe Google Calendar operations for the Chief of Staff plugin, including proactive pre-meeting prep reminders via one-shot Hermes cron jobs.
version: 0.1.0
author: moonlight-lupin
license: Apache-2.0
metadata:
  hermes:
    tags: [calendar, google-workspace, meetings, cron, chief-of-staff]
    related_skills: [google-workspace, meeting-prep, daily-briefing]
---

# Calendar Manager

## Overview

Calendar Manager is the Chief of Staff plugin's operational layer for Google Calendar. It lists upcoming commitments, creates Google Meet-enabled events, safely modifies or deletes events, and runs the pre-meeting reminder pipeline that invokes `meeting-prep` shortly before meetings.

All Google Calendar access goes through the shared `WorkspaceClient` layer:

```bash
python skills/calendar-manager/scripts/calendar_actions.py scan --today
python skills/calendar-manager/scripts/calendar_actions.py create --title "Team Sync" --start 2026-07-10 --end 2026-07-10
python skills/calendar-manager/scripts/calendar_actions.py update --event-id <id> --title "New Title"
```

`WorkspaceClient` routes to the workspace provider selected by `integrations.workspace.provider` in `company.yaml` (`google_api` | `composio` | `m365`); calendar methods (`calendar_list`, `calendar_create`, `calendar_update`, `calendar_cancel`) are provider-neutral, so the same commands work on Google Calendar or Microsoft 365 (Outlook Calendar). Write actions (create/update) use guardrails (`CHIEF_OF_STAFF_AUTO_APPROVE=1`) and return standardized `ActionResult` objects.

## When to Use

Use this skill when the user asks to:

- See today's calendar, this week's calendar, or events in a specific date range.
- Schedule a meeting, call, appointment, or focus block.
- Add a Google Meet link to a new meeting.
- Move, rename, update attendees for, or cancel an event.
- Enable, audit, or troubleshoot pre-meeting prep reminders.

Also use when the operator addresses their Chief of Staff by its configured name (`assistant.name` in company.yaml), e.g. "Ask <name> to check my email" / "<name>, what's on today?".

Do not use this skill for meeting intelligence itself; Calendar Manager hands event metadata to `meeting-prep`, which produces the brief.

## Configuration

Read configuration from `company.yaml` before acting:

```yaml
google:
  account: default                 # optional; google-workspace account profile
  service_account_path: ~/.hermes/service_account.json
  delegate_email: founder@example.com

delivery:
  channel: telegram
  timezone: Asia/Singapore

calendar:
  reminder_minutes: 15
  auto_prep_brief: true
```

Defaults if keys are missing:

- `calendar.reminder_minutes`: `15`
- `calendar.auto_prep_brief`: `true`
- `delivery.timezone`: system timezone if unavailable; state the assumption.
- `delivery.channel`: current conversation channel if known; otherwise ask before creating cron delivery.

## Safety Policy

Calendar Manager is read-only by default.

| Operation | Confirmation required? | Rule |
|---|---:|---|
| List events | No | Safe read-only query. |
| Create event | Yes | Show title, time, timezone, attendees, Meet setting, reminders, and target calendar before creating. |
| Modify event | Yes | Show before/after diff and event ID before updating. |
| Delete event | Yes | Require explicit confirmation naming the event or event ID. |
| Create one-shot reminder cron jobs | No for the daily automated scan if `auto_prep_brief: true`; yes for manual bulk changes | Cron prompt is read-only and invokes `meeting-prep`; never mutates calendar. |

If an event looks ambiguous, list matching events and ask the user to choose one. Never guess which event to edit or delete.

## Workspace Access

Calendar Manager's intent is: list events in a window, create an event with a join link, update an event, and cancel/delete an event. Prefer the `calendar_actions.py` wrapper (shown in the Overview) — it routes through `WorkspaceClient` and applies guardrails/audit. Normalize every event you read or report to the canonical `event` shape in `shared/scripts/schemas.py` (`{id, title, start, end, attendees?, organizer?, location?, conference_link?, status?, source?}`).

If you access the calendar directly instead of through the wrapper, use the first available path in this order:

1. **Native connector tools** in the agent's environment — the Google Calendar connector, or the Microsoft 365 Outlook Calendar connector.
2. **The configured workspace provider** via `shared/scripts/workspace_client.py`: `get_workspace_client(config).calendar_list(start, end)`, `.calendar_create(title, start, end, attendees=..., description=...)`, `.calendar_update(event_id, **fields)`, `.calendar_cancel(event_id)`. The provider is chosen by `integrations.workspace.provider` in `company.yaml` (`google_api` | `composio` | `m365`).

Request a conference/join link on create (Google Meet on Google, Teams on Microsoft 365). Always capture and report event IDs and join links returned by the provider.

## Operations

### 1. List Events

1. Resolve the date window:
   - `today`: local midnight to local 23:59:59 in `delivery.timezone`.
   - `week`: Monday 00:00 through Sunday 23:59:59 unless the user specifies another week.
   - `range`: parse user-provided dates and include timezone.
2. Call `calendar list` through an approved workspace access path (`calendar_actions.py` / `calendar_list`).
3. Normalize the result into: time, title, attendees, location/join link, calendar, event ID.
4. Present a concise agenda. Include event IDs only when the user may edit/delete next.

Completion criterion: every returned event in the requested range is shown or explicitly grouped as low-priority/declined.

### 2. Create Event with Meet Link

1. Gather title, date/time, duration or end time, timezone, attendees, and description.
2. Default `conference` to Google Meet unless the user asks for another location.
3. Show a confirmation block:

```text
Create calendar event?
Title: {title}
When: {start}–{end} ({timezone})
Attendees: {attendees}
Location: Google Meet link will be generated
Calendar delegate: {delegate}
```

4. Only after confirmation, run `calendar create` with a conference link requested.
5. Report the event ID, calendar link, and join link.

Completion criterion: the provider returns a created event ID and conference link, or the error is surfaced verbatim.

### 3. Modify Event

1. Identify the event by ID or by listing candidate matches.
2. Show the current values and proposed changes.
3. Require confirmation.
4. Run `calendar update` through an approved workspace access path.
5. Report the final event details.

Completion criterion: user sees before/after summary and the provider's update result.

### 4. Delete Event

1. Identify the event by ID or candidate match.
2. Require explicit confirmation: `Delete "{title}" on {date}?`.
3. Run `calendar delete` through an approved workspace access path.
4. Report deletion status.

Completion criterion: deletion is confirmed by the provider or failure reason is shown.

## Killer Feature: Pre-Meeting Cron Reminders

Calendar Manager runs a daily scan, normally at 06:00 local time, for today and tomorrow. For each upcoming event with a conference/join link (Google Meet, Teams, etc.) and at least one attendee, it creates a one-shot Hermes cron job scheduled at:

```text
event.start - calendar.reminder_minutes
```

Default reminder offset is 15 minutes. If the computed fire time is in the past, skip that reminder and log it as skipped.

### Daily Scan Workflow

1. Read `company.yaml` and confirm `calendar.auto_prep_brief` is true.
2. List calendar events from now through tomorrow 23:59 in `delivery.timezone`.
3. Filter out:
   - all-day events,
   - declined events,
   - events without attendees,
   - events without a conference/join link,
   - events whose reminder time is already past,
   - events that already have a matching one-shot cron job.
4. For each remaining event, create one one-shot cron job using Hermes cron.
5. Use a deterministic job title/key such as `calendar-prep:{event_id}:{start_iso}` to avoid duplicates.
6. Report created, skipped, and duplicate counts.

### Cron Prompt Template

The one-shot cron prompt must be self-contained because cron jobs do not inherit conversation history:

```text
You are running a scheduled Chief of Staff pre-meeting reminder.

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

Load the chief-of-staff meeting-prep skill and generate a concise pre-meeting brief for this exact event. Include:
- A prominent Google Meet join link.
- Who is attending and any known companies/roles.
- Recent relevant email or notes context if available.
- Open pipeline, invoice, or to-do items tied to the attendees.
- 1-3 suggested talking points.

Deliver the brief to {delivery_channel}. Do not modify the calendar event.
```

### Cron Creation Command Shape

Use Hermes cron, not a shell sleep or background process. Example command shape:

```bash
hermes cron create "{one_shot_iso}" \
  --title "calendar-prep:{event_id}:{start_iso}" \
  --skills "chief-of-staff:meeting-prep" \
  --delivery "{delivery_channel}" \
  --prompt-file /tmp/calendar-prep-{event_id}.txt
```

If the installed Hermes cron CLI uses a different flag name, run `hermes cron create --help` and adapt while preserving the one-shot schedule and self-contained prompt.

## Integration Points

- **google-workspace:** Required external dependency for Calendar API calls.
- **meeting-prep:** Invoked by one-shot cron reminders with event metadata.
- **daily-briefing:** Pulls today's and tomorrow's events for the command-center briefing.
- **travel-itinerary:** Travel bookings can create or block calendar events.
- **todo-list:** Action items from meetings may become to-dos after `meeting-prep` or meeting notes ingestion.

## Common Pitfalls

1. **Creating events without confirmation.** Read-only is the default. Writes require explicit confirmation.
2. **Missing timezone.** Always include `delivery.timezone` in displayed times and Google API calls.
3. **Duplicate cron jobs.** Use event ID + start time as the dedupe key before creating reminders.
4. **Cron prompt missing event context.** Cron jobs must include title, attendees, Meet link, delivery channel, and event ID.
5. **Editing the wrong event.** When a natural-language query matches multiple events, list candidates and ask.

## Verification Checklist

- [ ] `company.yaml` account/delegate and calendar settings were read.
- [ ] All calendar calls used an approved workspace access path (`calendar_actions.py`, connector tools, or `workspace_client`).
- [ ] Writes were confirmed before create/update/delete.
- [ ] Join links are shown for new or listed meeting events.
- [ ] Reminder cron jobs are one-shot, deduped, and scheduled before event start.
- [ ] Cron prompt is self-contained and includes delivery channel.
