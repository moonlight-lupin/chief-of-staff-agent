# Daily Workflow Guide

## One-command daily run

```bash
python skills/daily-briefing/scripts/daily_briefing.py run --summary
```

This gives you:
- What needs attention today
- What's waiting for approval (grouped by risk)
- Email organisation status
- Recent events
- Suggested next actions
- System health snapshot

## Output formats

```bash
# CLI text (default)
python skills/daily-briefing/scripts/daily_briefing.py run --summary

# JSON (machine-readable, stable schema)
python skills/daily-briefing/scripts/daily_briefing.py run --json

# Markdown (for email/notification)
python skills/daily-briefing/scripts/daily_briefing.py run --markdown
```

## Event filtering

```bash
# Last 24 hours (default)
python skills/daily-briefing/scripts/daily_briefing.py run --since 24

# Last 48 hours
python skills/daily-briefing/scripts/daily_briefing.py run --since 48

# Limit events to 20
python skills/daily-briefing/scripts/daily_briefing.py run --limit 20
```

## Notification

```bash
# Print to CLI
python skills/daily-briefing/scripts/daily_briefing.py notify --channel cli

# Create pending email action (does NOT auto-send)
python skills/daily-briefing/scripts/daily_briefing.py notify --channel email --to me@example.com
```

Email notification creates a pending `gmail.send` action that must be approved before sending.

## Risk classification

| Risk | Action types |
|------|-------------|
| 🔴 High | gmail.send, gmail.trash, drive.trash, calendar.cancel |
| 🟡 Medium | calendar.create, calendar.update, drive.upload, gmail.archive |
| 🟢 Low | gmail.label, gmail.create_label, drive.search, gmail.search |

## Safety guarantees

The daily briefing:
- ✅ Reads from event store, pending actions, suggestions, email org state
- ✅ Shows system health (reuses doctor checks)
- ❌ Does NOT call any provider (Gmail, Calendar, Drive)
- ❌ Does NOT approve pending actions
- ❌ Does NOT execute pending actions
- ❌ Does NOT send, label, archive, trash, or create anything

The `safety` field in JSON output confirms:
```json
{
  "safety": {
    "external_mutations_performed": false,
    "approvals_performed": false,
    "executions_performed": false
  }
}
```

## Daily workflow

1. **Morning:** Run `daily_briefing.py run --summary`
2. **Review:** Check pending approvals, grouped by risk
3. **Approve:** Use `webhook_events.py approve --action-id <id>` for items you approve
4. **Execute:** Use `webhook_events.py execute --action-id <id>` to run approved actions
5. **Health:** Run `doctor.py --summary` if anything looks wrong
6. **Backup:** Run `state_tools.py backup` before making changes