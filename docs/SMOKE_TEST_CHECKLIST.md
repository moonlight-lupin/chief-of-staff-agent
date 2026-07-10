# Chief-of-Staff Smoke Test Checklist

Run this after installation or configuration changes to verify the system is safe and ready.

## 1. Doctor check

```bash
python shared/scripts/doctor.py --summary
```

Expected: `Chief-of-Staff: READY` or `READY WITH WARNINGS`

## 2. State file inspection

```bash
python shared/scripts/state_tools.py inspect
```

Verify: all state files are valid JSON, no malformed entries.

## 3. Config validation

```bash
python shared/scripts/doctor.py --json | python -m json.tool
```

Verify: no `"status": "fail"` entries.

## 4. Webhook security

```bash
python skills/document-preparer/scripts/webhook_events.py validate-secret
```

Verify: at least one endpoint shows as enabled.

## 5. Provider capability report

```bash
python shared/scripts/doctor.py --summary
```

The `capabilities` check shows the configured provider and supported/unsupported actions.

## 6. Email organisation (read-only)

```bash
python skills/email-organisation/scripts/email_organisation.py inspect-labels --config shared/config/company.yaml
```

Verify: lists Gmail labels without error.

## 7. Daily briefing (dry run)

```bash
python skills/daily-briefing/scripts/daily_briefing.py --dry-run --config shared/config/company.yaml
```

Verify: briefing generates without errors.

## 8. Pending actions queue

```bash
python skills/document-preparer/scripts/webhook_events.py pending --config shared/config/company.yaml
```

Verify: shows empty queue or lists pending/approved actions.

## 9. Webhook receiver (local test)

```bash
python skills/document-preparer/scripts/webhook_events.py serve --port 8787 &
curl http://localhost:8787/health
```

Verify: `{"status": "healthy"}` response.

## 10. State backup

```bash
python shared/scripts/state_tools.py backup
```

Verify: backup directory created with state files copied.

## 11. Orphaned action cleanup

```bash
python shared/scripts/doctor.py --fix
```

Verify: any orphaned `executing` actions reset to `approved`.

## Pass criteria

- Doctor: 0 failures
- State files: all valid JSON
- Webhook: at least 1 endpoint enabled
- Briefing: generates without errors
- Backup: completes successfully