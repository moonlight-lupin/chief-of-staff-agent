# Chief of Staff Hermes Plugin

AI Chief of Staff for SMEs: a practical operations layer for email, calendar, deadlines, pipeline, documents, knowledge, research, travel, bookkeeping, self-signing, and backups.

The plugin is designed for a single company/operator per Hermes instance. It uses Google Workspace as the backbone for Gmail, Calendar, and Drive, and keeps operational records in human-readable YAML under the configured project root.

## Features

The plugin registers 19 Hermes skills:

1. **daily-briefing** — daily command-center briefing from inbox, calendar, deadlines, pipeline, to-dos, and finance.
2. **deadline-tracker** — statutory and custom deadline tracking from company config and jurisdiction packs.
3. **note-taker** — second-brain/wiki ingestion, search, linking, and knowledge-base maintenance.
4. **todo-list** — lightweight internal task management in `todos.yaml`.
5. **calendar-manager** — Google Calendar visibility, safe scheduling, and pre-meeting prep reminders.
6. **drive-filer** — Google Drive folder structure, attachment filing, and project-file sync.
7. **meeting-prep** — 15-minute pre-meeting intelligence briefs with Meet links and open items.
8. **weekly-review** — Friday review across all operational systems.
9. **document-preparer** — fill DOCX templates and reverse-engineer documents into reusable templates.
10. **pipeline-manager** — lightweight CRM in `pipeline.yaml` with configurable sales stages.
11. **bookkeeper** — AR/AP, invoice tracking, expenses, and simple P&L reporting.
12. **deep-research** — autonomous cited research reports, filed into knowledge and Drive systems.
13. **entity-research** — background dossiers on companies/people for due diligence and meeting context.
14. **travel-itinerary** — structured business-trip itineraries from confirmations and travel documents.
15. **backup** — scheduled backup of Hermes config, skills, project data, and wiki to Drive.
16. **self-sign** — scan documents for the operator's signature blocks and place signature assets locally.
17. **email-organisation** — inspect mail labels/categories, propose a label policy, and apply approved organisation through the review queue.
18. **esign-connector** — send documents for third-party e-signature via self-hosted DocuSeal.
19. **news-monitoring** — recurring topic/news monitoring with web search, multi-language sources, and digest delivery via cron.

## Prerequisites

- Hermes Agent installed and working.
- A workspace provider configured via `integrations.workspace.provider` in `company.yaml` (`google_api`, `composio`, `m365`, or `agent`). Skills use the provider-neutral `WorkspaceClient`.
- Python packages used by specific skills:
  - `pyyaml`
  - `python-docx`
  - `pymupdf` / `fitz` for PDF signature detection and placement
- A dedicated project directory for company YAML data and wiki files.
- Optional but recommended: a dedicated Google Drive root folder for the numbered filing structure.

## Installation

Place or clone this plugin at:

```text
/root/.hermes/plugins/chief-of-staff/
```

Required top-level files:

```text
plugin.yaml
__init__.py
skills/*/SKILL.md
shared/config/*.example
docs/*.md
```

Then restart Hermes or reload plugins so the plugin skills are discovered.

Check that the plugin manifest is present:

```bash
hermes plugins list
```

Check skill availability in a fresh Hermes session:

```text
/skill chief-of-staff:daily-briefing
/skill chief-of-staff:weekly-review
```

If your Hermes build uses a different plugin namespace display, list installed skills and look for the 18 skill names above.

## Quick Start

1. Copy and customize config examples:

```bash
cd /root/.hermes/plugins/chief-of-staff
cp shared/config/company.yaml.example shared/config/company.yaml
cp shared/config/queries.yaml.example shared/config/queries.yaml
cp shared/config/drive-map.yaml.example shared/config/drive-map.yaml
cp shared/config/template-index.yaml.example shared/config/template-index.yaml
```

2. Edit `shared/config/company.yaml`:

- company legal details,
- Google Workspace account/delegate,
- `paths.project_root`,
- delivery channel/time/timezone,
- sales stages,
- backup and self-sign settings.

3. Create the project data directory and initial YAML stores:

```bash
mkdir -p ~/.hermes/projects/acme-advisory/wiki
cat > ~/.hermes/projects/acme-advisory/pipeline.yaml <<'EOF'
deals: []
EOF
cat > ~/.hermes/projects/acme-advisory/todos.yaml <<'EOF'
todos: []
EOF
cat > ~/.hermes/projects/acme-advisory/invoices.yaml <<'EOF'
invoices: []
EOF
cat > ~/.hermes/projects/acme-advisory/expenses.yaml <<'EOF'
expenses: []
EOF
```

4. Test Google access through the `google-workspace` script named in the skills:

```bash
python ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py \
  --account default --as founder@example.com calendar list
```

5. Run a manual briefing in Hermes:

```text
Load chief-of-staff:daily-briefing and produce today's briefing from /root/.hermes/plugins/chief-of-staff/shared/config/company.yaml. Read-only only.
```

6. Create cron jobs for daily briefing, weekly review, backup, and calendar prep scan after validation.

## Data Model

The plugin keeps company data in `paths.project_root` from `company.yaml`:

```text
pipeline.yaml     # deals / CRM
invoices.yaml     # AR/AP invoices
expenses.yaml     # expenses for P&L snapshots
todos.yaml        # internal tasks
wiki/             # Note Taker knowledge base
```

Shared plugin configuration lives under:

```text
shared/config/company.yaml
shared/config/queries.yaml
shared/config/drive-map.yaml
shared/config/template-index.yaml
shared/config/jurisdictions/{sg,hk,us,uk}.yaml
```

## Documentation

- [SETUP.md](SETUP.md) — complete 15-step onboarding guide.
- [CUSTOMIZATION.md](CUSTOMIZATION.md) — customizing stages, deadlines, Gmail queries, Drive rules, jurisdiction packs, self-sign aliases, and backup schedule.

## Safety and Privacy

- Aggregator skills (`daily-briefing`, `meeting-prep`, `weekly-review`) are read-only by default. The daily command is externally read-only (no workspace mutations) but writes local telemetry (.last_briefing timestamp) to prevent duplicate briefings.
- Mutations such as sending email, changing calendar events, moving deals, marking invoices, signing documents, or filing to Drive require explicit confirmation through the relevant source skill.
- Scheduled prompts are self-contained and should not rely on conversation history.
- Set `delivery.use_client_codes: true` if briefings may be delivered to channels visible to others.
- Do not store secrets in project YAML. Keep service account JSON paths in config and secrets on disk with restricted permissions.
