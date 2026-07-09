# Chief of Staff Plugin Setup

This onboarding guide configures the Chief of Staff plugin for one company/operator. The goal is an end-to-end working setup: Google access, project YAML stores, Drive filing, wiki, signature assets, cron jobs, and a test briefing.

Plugin root:

```text
/root/.hermes/plugins/chief-of-staff/
```

## Step 1 — Check Hermes and Required Skills

Confirm Hermes is installed:

```bash
hermes --version
hermes doctor
```

Confirm the external Google Workspace skill is installed:

```bash
hermes skills list | grep -i google-workspace
```

If missing, install/configure it using the standard Hermes skill workflow:

```bash
hermes skills install google-workspace
```

Output: Hermes works and the `google-workspace` script exists at:

```text
~/.hermes/skills/productivity/google-workspace/scripts/google_api.py
```

## Step 2 — Enter Company Details

Copy the example config:

```bash
cd /root/.hermes/plugins/chief-of-staff
cp shared/config/company.yaml.example shared/config/company.yaml
```

Edit `shared/config/company.yaml` and complete:

```yaml
company:
  name: "Acme Advisory Pte Ltd"
  jurisdiction: SG
  incorporation_date: "2024-01-15"
  financial_year_end: "31 Dec"
  currency: SGD
  business_type: professional_services
  registration_number: "..."
  tax_registration_number: "..."
  address: "..."
user:
  name: "..."
  role: "..."
  email: "..."
```

Output: `company.yaml` has the company and operator identity used by all skills.

## Step 3 — Configure Google Workspace Auth (**High Friction**)

This is one of the hardest setup steps. Expect admin-console work and permission propagation delays.

You need a Google Workspace service account or account profile supported by `google-workspace` with access to:

- Gmail read/search for the delegated user,
- Calendar read/write for the delegated user,
- Drive read/write for the root filing folder.

Fill the Google section:

```yaml
google:
  account: default                    # if your google-workspace setup uses account profiles
  service_account_path: "~/.hermes/secrets/acme-google-service-account.json"
  domain: "acme.example"
  delegate_email: "founder@acme.example"
  drive_root_folder_id: "..."
```

Friction callout:

- Service account JSON must be present on the Hermes VM.
- Domain-wide delegation must be enabled by a Workspace admin.
- Scopes must match Gmail, Calendar, and Drive actions.
- The delegated user must have access to the calendar and Drive folder.
- Keep the JSON outside the plugin repo and never commit it.

Output: Google settings are recorded in `company.yaml`.

## Step 4 — Test Google Auth

Run three narrow tests through the required `google_api.py` wrapper:

```bash
python ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py \
  --account default --as founder@acme.example calendar list

python ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py \
  --account default --as founder@acme.example gmail search \
  --query 'in:inbox newer_than:1d' --max-results 5

python ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py \
  --account default --as founder@acme.example drive search \
  --query "name contains 'Chief'" --max-results 5
```

Output: Calendar, Gmail, and Drive calls return successfully. If one fails, fix auth before continuing.

## Step 5 — Set Up Google Drive Folder Structure (**High Friction**)

This step can be medium-to-high friction because Drive folder IDs, permissions, shared drives, and ownership are easy to misconfigure.

Choose one approach:

### Option A — Use an Existing Root Folder

Create a root folder manually in Drive and put its ID in `company.yaml`:

```yaml
google:
  drive_root_folder_id: "1A2B3C4D..."
```

### Option B — Create the Numbered Structure

Use Drive Filer's root structure from `shared/config/drive-map.yaml.example`:

```bash
cp shared/config/drive-map.yaml.example shared/config/drive-map.yaml
```

Create or verify folders such as:

```text
00_Inbox
01_Secretarial
02_Clients
03_Vendors
04_Finance
05_Templates
06_HR
07_Research
08_Travel
09_Backups
10_Knowledge_Base
```

Friction callout:

- Shared Drive permissions may prevent folder creation.
- The delegated user must have writer access.
- Folder IDs differ from visible folder names; store the root ID, not the URL.
- Do not proceed until a Drive list/search/upload test works.

Output: Drive root and default folder map are ready.

## Step 6 — Confirm Drive Filing Rules

Edit `shared/config/drive-map.yaml`.

Review:

- client NDA/proposal/contract targets,
- invoice sent/received rules,
- statutory/tax document rules,
- research and travel filing,
- `default: "00_Inbox/"`.

Output: Drive Filer knows where to suggest or place files.

## Step 7 — Configure Sales Stages

In `company.yaml`, confirm or edit:

```yaml
sales_stages:
  - Lead
  - Qualified
  - Proposal Sent
  - NDA Signed
  - Contract Sent
  - Contract Signed
  - Invoiced
  - Paid
  - Lost
stale_threshold_days: 14
```

Output: Pipeline Manager has allowed stage names and stale-deal threshold.

## Step 8 — Add Custom Deadlines

Jurisdiction packs cover common statutory deadlines. Add business-specific deadlines under:

```yaml
deadlines:
  custom:
    - name: "Professional indemnity insurance renewal"
      due: "2026-09-30"
      authority: "Internal"
      notes: "Renew before the existing policy lapses."
      owner: "Founder"
```

Output: Deadline Tracker can merge statutory and custom deadlines.

## Step 9 — Configure Delivery Channel, Time, and Timezone

In `company.yaml`:

```yaml
delivery:
  channel: telegram
  home_chat_id: "123456789"
  briefing_time: "20:00"
  weekly_review_day: friday
  weekly_review_time: "17:00"
  timezone: "Asia/Singapore"
  use_client_codes: false
```

Output: Scheduled briefings know when and where to deliver.

## Step 10 — Configure Calendar Reminder Preferences

In `company.yaml`:

```yaml
calendar:
  reminder_minutes: 15
  auto_prep_brief: true
```

Output: Calendar Manager can create one-shot Meeting Prep reminders before meetings.

## Step 11 — Set Up Self-Sign Assets (**High Friction**)

This step is high friction because signature images need to be clean, legally appropriate, and correctly matched to the user's signing block.

Prepare assets:

- transparent PNG signature,
- optional initials PNG,
- optional company stamp/chop PNG.

Place them at the configured paths, usually:

```text
/root/.hermes/plugins/chief-of-staff/shared/assets/signature.png
/root/.hermes/plugins/chief-of-staff/shared/assets/initials.png
/root/.hermes/plugins/chief-of-staff/shared/assets/stamp.png
```

Then update `company.yaml`:

```yaml
self_sign:
  signature_image: "shared/assets/signature.png"
  initials_image: "shared/assets/initials.png"
  company_stamp: "shared/assets/stamp.png"
  auto_date: true
  output_format: pdf
  party_aliases:
    - "Service Provider"
    - "Consultant"
    - "Contractor"
    - "The Company"
```

Friction callout:

- Poor scans look unprofessional; use a clean transparent PNG.
- Multi-party contracts need alias rules so the operator signs only their own block.
- Never auto-sign without presenting detected signature locations and receiving confirmation.

Output: Self-Sign can scan and prepare documents for confirmed signing.

## Step 12 — Initialize the Wiki

Create the wiki directory from `paths.wiki_path`:

```bash
mkdir -p ~/.hermes/projects/acme-advisory/wiki/{raw/articles,raw/papers,raw/transcripts,raw/assets,entities,concepts,comparisons,queries}
```

Create starter files:

```text
SCHEMA.md
purpose.md
index.md
overview.md
log.md
```

Seed `purpose.md` and `SCHEMA.md` from company business type, goals, and taxonomy. At minimum, include expected page frontmatter, tags, and link conventions.

Output: Note Taker has a valid knowledge base to search and grow.

## Step 13 — Configure Backups

In `company.yaml`:

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

Output: Backup knows what to package and where to upload.

## Step 14 — Create Cron Jobs

Create scheduled jobs only after the manual tests pass.

Recommended jobs:

| Job | Schedule | Skill |
|---|---|---|
| Daily briefing | `delivery.briefing_time`, daily | `chief-of-staff:daily-briefing` |
| Weekly review | Friday at `delivery.weekly_review_time` | `chief-of-staff:weekly-review` |
| Calendar scan | Daily around 06:00 | `chief-of-staff:calendar-manager` |
| Backup | `backup.schedule` | `chief-of-staff:backup` |

Use the full self-contained prompt templates in the relevant skills:

- `skills/daily-briefing/SKILL.md`
- `skills/weekly-review/SKILL.md`
- `skills/calendar-manager/SKILL.md`
- `skills/backup/SKILL.md`

Output: Hermes cron contains enabled scheduled jobs with self-contained prompts.

## Step 15 — Send a Test Briefing

Run a manual end-to-end briefing:

```text
Load chief-of-staff:daily-briefing. Use /root/.hermes/plugins/chief-of-staff/shared/config/company.yaml. Produce a read-only test briefing now and state any missing setup items.
```

Verify:

- Gmail query works,
- Calendar today/tomorrow works,
- deadlines compute,
- pipeline/todos/invoices read from project root,
- delivery formatting is acceptable,
- no source data was modified.

Output: One successful test briefing delivered or printed, with any remaining blockers clearly listed.

## Final Validation Checklist

- [ ] `google-workspace` skill installed and `google_api.py` works.
- [ ] `company.yaml`, `queries.yaml`, `drive-map.yaml`, and `template-index.yaml` exist.
- [ ] `paths.project_root` exists and contains `pipeline.yaml`, `todos.yaml`, `invoices.yaml`, `expenses.yaml`.
- [ ] Jurisdiction pack exists for `company.jurisdiction`.
- [ ] Google Calendar, Gmail, and Drive calls succeed for the delegate.
- [ ] Drive root folder is accessible.
- [ ] Wiki contains starter files.
- [ ] Signature image exists if Self-Sign is enabled.
- [ ] Cron jobs have self-contained prompts.
- [ ] Test briefing succeeds without modifying source data.
