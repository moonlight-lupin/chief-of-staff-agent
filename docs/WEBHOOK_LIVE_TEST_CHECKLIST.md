# Webhook Receiver Live Test Checklist

## v0.1.29: Webhook receiver and event replay safety

### Prerequisites

- [ ] `CHIEF_OF_STAFF_WEBHOOK_SECRET` set (minimum 16 characters)
- [ ] Google service account configured with delegation
- [ ] `company.yaml` has `google.service_account_path` and `google.delegate_email`
- [ ] Port 8787 available (or use `--port`)

### Section A: Secret Validation

1. **Validate secret**
   ```bash
   python skills/document-preparer/scripts/webhook_events.py validate-secret
   ```
   - [ ] Shows ✅ Webhook secret configured
   - [ ] Shows algorithm and header name

2. **Sign a test payload**
   ```bash
   python skills/document-preparer/scripts/webhook_events.py sign --body '{"test": true}'
   ```
   - [ ] Returns 64-character hex signature

### Section B: Server Start/Stop

3. **Start receiver**
   ```bash
   export CHIEF_OF_STAFF_WEBHOOK_SECRET="your-secret-here"
   python skills/document-preparer/scripts/webhook_events.py serve --host 0.0.0.0 --port 8787
   ```
   - [ ] Shows 🌐 Webhook receiver listening
   - [ ] Shows signature, replay protection, health check info

4. **Health check**
   ```bash
   curl http://localhost:8787/health
   ```
   - [ ] Returns JSON with status: healthy and stats

5. **Send valid webhook**
   ```bash
   BODY='{"emailAddress":"test@x.com","historyId":"12345"}'
   SIG=$(python skills/document-preparer/scripts/webhook_events.py sign --body "$BODY")
   curl -X POST http://localhost:8787 \
     -H "Content-Type: application/json" \
     -H "X-Webhook-Signature: $SIG" \
     -d "$BODY"
   ```
   - [ ] Returns 200 with status: ingested
   - [ ] Shows event_id, source, source_id

6. **Send duplicate webhook (replay)**
   ```bash
   # Same signature again
   curl -X POST http://localhost:8787 \
     -H "Content-Type: application/json" \
     -H "X-Webhook-Signature: $SIG" \
     -d "$BODY"
   ```
   - [ ] Returns 409 with "Replay detected"

7. **Send invalid signature**
   ```bash
   curl -X POST http://localhost:8787 \
     -H "Content-Type: application/json" \
     -H "X-Webhook-Signature: wrong" \
     -d '{"test": true}'
   ```
   - [ ] Returns 401 with "Invalid or missing signature"

### Section C: Event Inspection

8. **Inspect webhook events**
   ```bash
   python skills/document-preparer/scripts/webhook_events.py --summary inspect --limit 20
   ```
   - [ ] Shows webhook-originated events
   - [ ] Shows source, type, state, source_id, summary

### Section D: Replay (Suggestion Regeneration)

9. **Dry-run replay**
   ```bash
   python skills/document-preparer/scripts/webhook_events.py replay --event-id <id> --dry-run
   ```
   - [ ] Shows DRY-RUN
   - [ ] No suggestions generated

10. **Actual replay**
    ```bash
    python skills/document-preparer/scripts/webhook_events.py replay --event-id <id>
    ```
    - [ ] Shows suggestion count
    - [ ] No execution — only suggestions generated

### Section E: Safety Verification

11. **No mutations during webhook ingestion**
    - [ ] Receiver never calls gmail_send, gmail_label, gmail_archive
    - [ ] Receiver never calls pending_actions.create_pending_action
    - [ ] Receiver never calls approve_pending_action or mark_executing
    - [ ] Receiver can only: verify, parse, store, classify, suggest

12. **No auto-execution**
    - [ ] Suggestion generation never executes provider methods
    - [ ] Replay only generates suggestions, never executes
    - [ ] Webhook cannot bypass approval queue