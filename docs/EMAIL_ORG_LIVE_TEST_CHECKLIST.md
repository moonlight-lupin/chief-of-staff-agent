# Email Organisation Live Test Checklist

## v0.1.26–v0.1.28: Full classify → suggest → prepare → approve → execute flow

### Prerequisites

- [ ] Google service account configured with delegation
- [ ] `company.yaml` has `google.service_account_path` and `google.delegate_email`
- [ ] `CHIEF_OF_STAFF_AUTO_APPROVE=1` set for safe reads
- [ ] `google_api.py` accessible via `--account` and `--as` flags

### Section A: Onboarding (v0.1.26)

1. **Inspect labels**
   ```bash
   python skills/email-organisation/scripts/email_organisation.py --config company.yaml --summary inspect-labels
   ```
   - [ ] Shows total labels, user labels, system labels
   - [ ] Detected groups for nested labels

2. **Propose policy**
   ```bash
   python skills/email-organisation/scripts/email_organisation.py --config company.yaml --summary propose-policy
   ```
   - [ ] Shows mode: use_existing_first
   - [ ] Shows mapped categories and unmapped labels
   - [ ] Shows "No Gmail changes were made"

3. **Save policy**
   ```bash
   python skills/email-organisation/scripts/email_organisation.py --config company.yaml --summary save-policy --approved-by "MH"
   ```
   - [ ] Shows ✅ Policy approved
   - [ ] Shows approved_by and category count

4. **Validate policy**
   ```bash
   python skills/email-organisation/scripts/email_organisation.py --config company.yaml validate-policy
   ```
   - [ ] Shows ✅ valid

### Section B: Classification (v0.1.27)

5. **Classify inbox**
   ```bash
   python skills/email-organisation/scripts/email_organisation.py --config company.yaml --summary classify-inbox --limit 20
   ```
   - [ ] Shows classified count
   - [ ] Shows with_category and unmapped counts
   - [ ] Individual emails show category and confidence

6. **Re-classify (idempotent)**
   ```bash
   # Run same command again
   python skills/email-organisation/scripts/email_organisation.py --config company.yaml --summary classify-inbox --limit 20
   ```
   - [ ] Shows 0 newly classified (all already classified)

7. **Generate suggestions**
   ```bash
   python skills/email-organisation/scripts/email_organisation.py --config company.yaml --summary suggest --limit 50
   ```
   - [ ] Shows label/archive/create-label suggestion counts
   - [ ] All suggestions have risk icons and confidence
   - [ ] Shows "No Gmail changes were made"

8. **Dry-run suggestions**
   ```bash
   python skills/email-organisation/scripts/email_organisation.py --config company.yaml --summary suggest --dry-run
   ```
   - [ ] Shows dry-run mode
   - [ ] Nothing saved

### Section C: Prepare → Approve → Execute (v0.1.27)

9. **List suggestions**
   ```bash
   python skills/email-organisation/scripts/email_organisation.py --config company.yaml list-suggestions --state suggested --action-type gmail.label
   ```
   - [ ] Returns JSON with suggestion IDs

10. **Prepare pending action**
    ```bash
    python skills/email-organisation/scripts/email_organisation.py --config company.yaml --summary prepare --suggestion-id <id>
    ```
    - [ ] Shows 📋 Pending action created
    - [ ] Shows action_id

11. **List pending**
    ```bash
    python skills/email-organisation/scripts/email_organisation.py --config company.yaml --summary pending
    ```
    - [ ] Shows pending email organisation action

12. **Approve pending action**
    ```bash
    # Use send_email.py or delete_actions.py depending on action type
    python skills/document-preparer/scripts/send_email.py --config company.yaml --summary approve --action-id <id> --approver "MH" --reason "Reviewed"
    ```
    - [ ] Shows ✅ approved

13. **Execute pending action**
    ```bash
    python skills/document-preparer/scripts/send_email.py --config company.yaml --summary execute --action-id <id>
    ```
    - [ ] Provider method called
    - [ ] Audit trail recorded
    - [ ] Marked as executed

### Section D: Digest and Notification (v0.1.28)

14. **Digest**
    ```bash
    python skills/email-organisation/scripts/email_organisation.py --config company.yaml --summary digest
    ```
    - [ ] Shows 📬 Email Organisation Digest
    - [ ] Shows classified, with_category, unmapped
    - [ ] Shows by_category breakdown
    - [ ] Shows suggestion counts
    - [ ] Shows "No Gmail changes were made"

15. **Notify CLI**
    ```bash
    python skills/email-organisation/scripts/email_organisation.py --config company.yaml --summary notify --channel cli
    ```
    - [ ] Prints digest to stdout

16. **Notify email**
    ```bash
    python skills/email-organisation/scripts/email_organisation.py --config company.yaml --summary notify --channel email --to me@example.com
    ```
    - [ ] Shows 📋 Digest email prepared
    - [ ] Shows approve command
    - [ ] Does NOT auto-send

### Section E: Daily Briefing Integration (v0.1.28)

17. **Run daily briefing**
    ```bash
    python skills/daily-briefing/scripts/daily_briefing.py --config company.yaml --render
    ```
    - [ ] Output includes 📬 Email Organisation line
    - [ ] Shows classified count, suggestions, pending

### Section F: Safety Verification

18. **No mutations during classify**
    - [ ] classify-inbox never calls gmail_send, gmail_label, gmail_archive

19. **No mutations during suggest**
    - [ ] suggest never calls provider write methods
    - [ ] suggest never creates pending actions (only prepare does)

20. **No mutations during digest**
    - [ ] digest never calls any provider method

21. **No auto-send during notify email**
    - [ ] notify --channel email creates pending action but never calls gmail_send
    - [ ] notify never calls approve_pending_action or mark_executing