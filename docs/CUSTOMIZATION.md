# Chief of Staff Plugin Customization

This guide covers the common ways to adapt the Chief of Staff plugin to a specific business: sales stages, deadlines, Gmail queries, Drive filing, jurisdiction packs, self-sign aliases, templates, and backup schedules.

Plugin root:

```text
/root/.hermes/plugins/chief-of-staff/
```

Primary files:

```text
shared/config/company.yaml
shared/config/queries.yaml
shared/config/drive-map.yaml
shared/config/template-index.yaml
shared/config/jurisdictions/{jurisdiction}.yaml
```

## Adding or Changing Sales Stages

Sales stages live in `shared/config/company.yaml`:

```yaml
sales_stages:
  - Lead
  - Qualified
  - Discovery Done
  - Proposal Sent
  - Commercial Review
  - Contract Sent
  - Contract Signed
  - Invoiced
  - Paid
  - Lost
stale_threshold_days: 14
```

Rules:

- Keep stage names stable after records exist; deals store the exact stage string.
- Add new stages at the correct process point rather than renaming old ones casually.
- If you must rename a stage, update existing `pipeline.yaml` records intentionally and preserve an audit note.
- Terminal stages should be obvious, e.g. `Paid`, `Lost`, `Cancelled`; stale detection excludes terminal stages.

After changing stages, test:

```text
Load chief-of-staff:pipeline-manager and list active deals by stage. Flag any records using obsolete stages.
```

## Adding Custom Deadlines

Custom business deadlines live in `company.yaml` under `deadlines.custom[]`:

```yaml
deadlines:
  custom:
    - name: "Cyber insurance renewal"
      due: "2026-10-15"
      authority: "Internal"
      notes: "Renew before current policy lapses."
      owner: "Operations"
      tags: [insurance, risk]
```

Rules:

- Use ISO dates (`YYYY-MM-DD`).
- Keep external statutory obligations in jurisdiction packs where possible.
- Use custom deadlines for company-specific dates: renewals, board meetings, grant reports, client renewals, certifications.
- Do not delete old deadline records if they explain history; mark complete/archived if your schema supports it.

Test:

```text
Load chief-of-staff:deadline-tracker and show deadlines due within 30 days from company.yaml.
```

## Adding New Gmail Queries

Gmail templates live in `shared/config/queries.yaml`.

Template shape:

```yaml
queries:
  proposal_followups:
    description: Unread or recent proposal follow-up emails.
    query: 'newer_than:30d ({client_name} OR from:{contact_email} OR to:{contact_email}) (proposal OR quote OR "commercial offer")'
    max_results: 25
```

Supported placeholders include:

- `{client_name}`
- `{contact_email}`
- `{domain}`
- `{invoice_id}`
- `{days}`

Rules:

- Prefer narrow, auditable templates over broad mailbox searches.
- Keep query names descriptive and stable; aggregator skills refer to them by name.
- For scheduled briefings, use templates only; do not rely on ad hoc query wording hidden in prompts.
- Test against a small `max_results` before using in Daily Briefing.

Test command shape:

```bash
python ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py \
  --account default --as founder@example.com gmail search \
  --query 'newer_than:30d (Acme OR from:john@acme.example) (proposal OR quote)' \
  --max-results 10
```

## Customizing Drive Map Rules

Drive filing rules live in `shared/config/drive-map.yaml`.

Example rule:

```yaml
filing_rules:
  - id: client-security-review
    pattern: ["security questionnaire", "vendor assessment", "SOC 2", "ISO 27001"]
    target: "02_Clients/{client}/Security/"
    fallback: "00_Inbox/"
    tags: [client, security]
```

Rules are evaluated top-to-bottom. The first match wins unless the active skill explicitly asks for alternatives.

Useful fields:

- `id`: stable machine-readable rule ID.
- `pattern`: list of case-insensitive keywords/phrases.
- `direction`: optional, e.g. `sent` or `received`.
- `target`: preferred Drive-relative folder.
- `secondary_target`: optional duplicate/cross-file location.
- `fallback`: safe destination if placeholders cannot be resolved.
- `tags`: optional metadata for reports.

Guidance:

- Put specific rules above general invoice/contract rules.
- Use `{client}`, `{vendor}`, and `{trip}` placeholders consistently.
- Keep `default: "00_Inbox/"` as the safe fallback.
- Avoid writing client-specific hardcoded folders into generic rules unless this is a single-client setup.

## Editing Jurisdiction Packs

Jurisdiction packs live in:

```text
shared/config/jurisdictions/sg.yaml
shared/config/jurisdictions/hk.yaml
shared/config/jurisdictions/us.yaml
shared/config/jurisdictions/uk.yaml
```

To add a new jurisdiction, create a lower-case ISO-style file such as:

```text
shared/config/jurisdictions/au.yaml
```

Minimum shape:

```yaml
jurisdiction: AU
statutory:
  - name: "Annual Review"
    frequency: yearly
    trigger: "on ASIC annual review date"
    authority: ASIC
    penalty: "Late review fee may apply"
    notes: "Confirm company-specific review date."
```

Rules:

- Jurisdiction packs are operational reminders, not legal advice.
- Preserve authority, trigger, penalty/impact, conditionals, and notes.
- Mark conditional items clearly, e.g. GST/VAT/payroll filings.
- Event-driven deadlines without event dates should remain event-driven; do not invent dates.
- Add company-specific concrete notices to `deadlines.custom[]`, not the generic pack.

After adding a pack, update `company.yaml`:

```yaml
company:
  jurisdiction: AU
```

Then run Deadline Tracker.

## Self-Sign Party Aliases

Self-Sign alias settings live in `company.yaml`:

```yaml
self_sign:
  party_aliases:
    - "Service Provider"
    - "Consultant"
    - "Contractor"
    - "The Company"
    - "Supplier"
    - "Vendor"
```

Use aliases that commonly identify the operator's company in contracts.

Guidance:

- Add aliases for roles the company actually plays.
- Do not add both sides of a contract, e.g. avoid adding `Client` if you usually sign as `Service Provider`.
- Keep aliases broad enough to detect signing blocks, but require confirmation before actual signing.
- For unusual documents, let Self-Sign present all detected blocks neutrally.

## Backup Schedule and Retention

Backup settings live in `company.yaml`:

```yaml
backup:
  enabled: true
  schedule: "0 3 * * 0"
  retention_weekly: 4
  retention_monthly: 12
  drive_folder: "09_Backups/"
  exclude:
    - ".env"
    - "auth.json"
    - "state.db"
    - "sessions/"
    - "logs/"
```

Examples:

```yaml
# Daily at 02:30
schedule: "30 2 * * *"

# Sundays at 03:00
schedule: "0 3 * * 0"

# First day of the month at 04:00
schedule: "0 4 1 * *"
```

Rules:

- Exclude secrets by default: `.env`, `auth.json`, service account JSON, OAuth tokens.
- Increase retention for regulated industries or client-contractual requirements.
- Test restore periodically, not just backup creation.
- Use a dedicated Drive backup folder with restricted access.

## Template Registry

Document templates are registered in `shared/config/template-index.yaml`:

```yaml
templates:
  - name: "Proposal"
    file: "shared/templates/Proposal_standard.docx"
    tokens: [client_name, scope, amount, currency, date]
    category: engagement
    last_used: null
```

Guidance:

- Keep token names lowercase snake_case.
- Register each template with the tokens Document Preparer must fill.
- Use categories such as `legal`, `engagement`, `finance`, `hr`, `operations`.
- Store templates in `shared/templates/` or a configured Drive `05_Templates/` folder.

## Delivery and Confidentiality

Delivery settings live in `company.yaml`:

```yaml
delivery:
  channel: telegram
  home_chat_id: "123456789"
  briefing_time: "20:00"
  weekly_review_day: friday
  weekly_review_time: "17:00"
  timezone: "Asia/Singapore"
  use_client_codes: true
```

Set `use_client_codes: true` if briefings go to a shared or potentially visible channel. Skills should then prefer deal IDs/client codes over full names.

## Safe Customization Workflow

1. Copy the current config file before editing.
2. Make one category of change at a time.
3. Validate YAML syntax.
4. Run the dedicated skill manually.
5. Run Daily Briefing or Weekly Review to verify aggregators still interpret the change.
6. Only then update cron prompts or schedules if needed.

YAML validation:

```bash
python - <<'PY'
import yaml, pathlib
for p in [
  'shared/config/company.yaml',
  'shared/config/queries.yaml',
  'shared/config/drive-map.yaml',
  'shared/config/template-index.yaml',
]:
    path = pathlib.Path('/root/.hermes/plugins/chief-of-staff') / p
    if path.exists():
        yaml.safe_load(path.read_text())
        print('ok', p)
PY
```

## Troubleshooting Customizations

| Symptom | Likely cause | Fix |
|---|---|---|
| Daily Briefing misses client emails | Query template too narrow or missing placeholder values | Test rendered query manually; verify `pipeline.yaml` contact emails |
| Pipeline reports obsolete stages | Stage names changed after deals existed | Migrate records intentionally and add audit notes |
| Deadline missing | Custom deadline not in ISO date or jurisdiction pack lacks requirement | Fix date format or add pack/custom entry |
| Drive files go to `00_Inbox` | Rule order or placeholder resolution failed | Move specific rule higher; add fallback or client/vendor mapping |
| Self-Sign suggests wrong block | Party aliases too broad | Remove ambiguous aliases; rely on confirmation prompts |
| Weekly Review has no knowledge count | Wiki files lack dates and filesystem timestamps are unavailable | Add page frontmatter dates or note-taker logs |
| Backup includes too much | Exclude list too short | Add large/transient directories and secret files to `backup.exclude` |
