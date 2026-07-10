# 🧭 Chief of Staff

**A private, approval-gated AI Chief of Staff for founders, operators, and small teams.**

Chief of Staff brings together your inbox, calendar, deadlines, CRM pipeline, invoices, tasks, documents, and internal knowledge so you can answer:

> What changed? What needs my attention? What is waiting for my decision? What should I do next?

It can organise information, detect stale deals, extract invoice candidates, prepare documents, maintain a private second brain, and propose actions — while keeping consequential changes behind explicit review and approval.

### Observe → Understand → Suggest → Approve → Execute → Audit

> **Status:** v0.3.0 internal beta  
> **Runtime:** Python 3.11+  
> **License:** Apache License 2.0

---

## 👀 See the daily loop

Run one command:

```bash
python shared/scripts/chief_of_staff.py daily --summary
```

Chief of Staff gives you one operating view:

```text
Chief-of-Staff Daily

1. System health
2. Briefing
3. Needs review
4. Pipeline / CRM
5. Bookkeeper
6. Knowledge maintenance
7. State safety
8. Recommended next commands
```

A typical brief may tell you:

```text
- 2 actions are waiting for review
- 1 deal has been inactive for 21 days
- 3 invoice candidates were detected
- 1 invoice may be a duplicate
- 4 knowledge pages were updated
- no stuck actions or malformed state files were found
```

The daily loop is deliberately **read-only**. It reports, prioritises, and recommends — it does not approve or execute actions.

---

## 🚀 Install

Chief of Staff currently runs as a Hermes plugin.

```bash
cd ~/.hermes/plugins

git clone \
  https://github.com/moonlight-lupin/chief-of-staff-agent.git \
  chief-of-staff

cd chief-of-staff
python -m pip install -r requirements.txt
```

Bootstrap your workspace:

```bash
python shared/scripts/bootstrap.py \
  --company "Acme Studio" \
  --jurisdiction SG \
  --operator you@example.com
```

For Google Workspace and Composio setup, see [`docs/SETUP.md`](docs/SETUP.md).

---

## ⏱️ Your first 15 minutes

### 1. Check your setup

```bash
python shared/scripts/chief_of_staff.py doctor --summary
```

### 2. Run the beta smoke test

```bash
python shared/scripts/chief_of_staff.py smoke-test --summary
```

The smoke test verifies that the main subsystems can run without modifying state, business records, or wiki pages.

### 3. Run your first daily brief

```bash
python shared/scripts/chief_of_staff.py daily --summary
```

Other output formats:

```bash
python shared/scripts/chief_of_staff.py daily --json
python shared/scripts/chief_of_staff.py daily --markdown
```

### 4. Ask naturally in Hermes

Try:

> Give me today’s operating brief and explain what needs my attention.

> Show me the pending actions that need my review.

> Which deals have gone stale?

> Check whether any invoice candidates need review.

---

## 🎯 The core operating pathway

You do not need to learn every skill at once.

Start with five surfaces:

1. **Daily Briefing** — your operating command centre.
2. **Review Queue** — the control surface for proposed actions.
3. **Pipeline Manager** — lightweight CRM and stale-deal detection.
4. **Bookkeeper** — invoice candidates, AR/AP, expenses, and duplicate checks.
5. **Note Taker** — structured memory and a linked Markdown wiki.

Together they form the operating loop:

```text
Events and workspace activity
          ↓
Daily Briefing
          ↓
Suggestions and prepared actions
          ↓
Review Queue
          ↓
Operator approval
          ↓
Controlled execution
          ↓
Audit history and knowledge updates
```

---

## ✨ What it handles

| Area | What Chief of Staff does |
|---|---|
| **Daily operations** | Briefings, weekly reviews, tasks, deadlines, and next-step recommendations |
| **Email** | Classifies messages and suggests labels, archive actions, and organisation policies |
| **Calendar** | Reads schedules, detects upcoming meetings, and prepares context |
| **Meetings** | Builds briefs from calendar, CRM, notes, documents, and open items |
| **CRM** | Tracks deals, stages, contacts, values, notes, documents, and stale opportunities |
| **Bookkeeping** | Extracts invoice candidates, validates fields, detects duplicates, and tracks AR/AP |
| **Documents** | Prepares documents from templates and helps build reusable templates |
| **Knowledge** | Maintains structured memory and a linked Markdown wiki |
| **Research** | Produces cited general and entity-specific research |
| **Files** | Organises Drive content and links operational records to source documents |
| **Travel** | Builds structured itineraries from travel information |
| **Signing** | Supports local self-signing or an enterprise e-signing profile |
| **Reliability** | Diagnostics, smoke tests, backups, state inspection, and audit logs |

---

## 🧰 The 17 skills

| Operating area | Skills |
|---|---|
| **Command centre** | `daily-briefing` · `weekly-review` |
| **Planning** | `todo-list` · `deadline-tracker` · `calendar-manager` |
| **Meetings and email** | `meeting-prep` · `email-organisation` |
| **Documents and files** | `document-preparer` · `drive-filer` · `self-sign` |
| **Commercial operations** | `pipeline-manager` · `bookkeeper` |
| **Knowledge and research** | `note-taker` · `deep-research` · `entity-research` |
| **Travel and resilience** | `travel-itinerary` · `backup` |

The enterprise profile replaces `self-sign` with `esign-connector`.

---

## 🔒 Why trust it

### You remain the decision-maker

Chief of Staff separates preparation from execution:

```text
suggestion ≠ approval
approval ≠ execution
```

Emails, calendar changes, Drive actions, invoice records, and CRM changes are not executed merely because the agent suggested them.

### The daily loop is read-only

It does not:

```text
approve or execute pending actions
send or draft email
modify Gmail, Calendar, or Drive
write pipeline.yaml or invoices.yaml
delete or merge wiki pages
confirm inferred facts
repair state automatically
```

### Data stays inspectable

Core operating records remain human-readable:

```text
pipeline.yaml
invoices.yaml
expenses.yaml
todos.yaml
wiki/
```

You can inspect, version, back up, or edit them without a proprietary hosted database.

### Internal knowledge is traceable

The knowledge layer can autonomously perform low-risk maintenance such as adding source-backed observations, links, daily logs, and open questions.

Destructive or high-impact changes remain approval-gated.

### Actions are reviewed and audited

The Review Queue shows:

```text
what will happen
why it matters
the risk level
the target and payload
the expected effect
how to correct or reverse it
```

Review an action:

```bash
python shared/scripts/review_queue.py preview --action-id <ID>
```

Approve and execute separately:

```bash
python shared/scripts/review_queue.py approve \
  --action-id <ID> \
  --approver "MH" \
  --reason "Reviewed and confirmed"

python shared/scripts/review_queue.py execute --action-id <ID>
```

---

## 🤝 CRM and Bookkeeper work together

Pipeline Manager tracks the commercial relationship:

```text
Who is the opportunity?
What stage is it in?
When was the last activity?
What documents belong to it?
```

Bookkeeper tracks the financial record:

```text
What was invoiced?
How much?
When is it due?
Has it been paid?
```

They link through `deal_id`, allowing Chief of Staff to surface issues such as:

```text
Contract Signed but no invoice exists
Invoiced but not paid
Invoice candidate may belong to an active deal
Deal has gone stale after proposal
```

These are lightweight operating tools — not replacements for a full CRM or accounting platform.

---

## 🧠 Memory and the second brain

Chief of Staff maintains:

- **Structured memory** for people, organisations, projects, decisions, preferences, and open questions.
- **A Markdown wiki** for durable, human-readable context.

Check knowledge quality:

```bash
python shared/scripts/memory.py lint --summary
python skills/note-taker/scripts/wiki_curator.py lint --summary
```

Create a backup:

```bash
python shared/scripts/memory.py backup
```

---

## 🔌 Workspace connections

Two Google Workspace connection modes are supported.

### Google service account

For self-hosted environments with domain-wide delegation:

```yaml
integrations:
  workspace:
    provider: google_api
    mode: direct
```

### Composio MCP

For managed authentication:

```yaml
integrations:
  workspace:
    provider: composio
    mode: mcp
```

See [`docs/SETUP.md`](docs/SETUP.md) for the full setup process.

---

## ⚙️ Configuration and data

Main configuration:

```text
shared/config/company.yaml
```

Start from:

```text
shared/config/company.yaml.example
```

Project data is stored under your configured `paths.project_root`:

```text
pipeline.yaml
invoices.yaml
expenses.yaml
todos.yaml
wiki/
```

Do not store passwords, API keys, private keys, bank details, or other secrets in project YAML.

---

## 🩺 Useful commands

```bash
# Daily operating loop
python shared/scripts/chief_of_staff.py daily --summary

# Diagnostics
python shared/scripts/chief_of_staff.py doctor --summary
python shared/scripts/chief_of_staff.py smoke-test --summary

# Review Queue
python shared/scripts/chief_of_staff.py review --summary
python shared/scripts/review_queue.py list --state requested

# CRM
python shared/scripts/chief_of_staff.py pipeline --summary
python skills/pipeline-manager/scripts/pipeline.py stale --summary

# Bookkeeper
python shared/scripts/chief_of_staff.py bookkeeper --summary
python skills/bookkeeper/scripts/invoice_ingest.py candidates --summary

# Knowledge
python shared/scripts/chief_of_staff.py knowledge --summary
python shared/scripts/memory.py lint --summary
python skills/note-taker/scripts/wiki_curator.py lint --summary
```

---

## 📚 Documentation

| Document | Purpose |
|---|---|
| [`docs/SETUP.md`](docs/SETUP.md) | Installation and Workspace providers |
| [`docs/BETA_DAILY_LOOP.md`](docs/BETA_DAILY_LOOP.md) | Daily operating loop |
| [`docs/BETA_READINESS_CHECKLIST.md`](docs/BETA_READINESS_CHECKLIST.md) | Beta readiness checks |
| [`docs/REVIEW_QUEUE.md`](docs/REVIEW_QUEUE.md) | Review, approve, dismiss, execute, and audit |
| [`docs/PIPELINE_MANAGER.md`](docs/PIPELINE_MANAGER.md) | CRM operations |
| [`docs/BOOKKEEPER_INGESTION.md`](docs/BOOKKEEPER_INGESTION.md) | Invoice ingestion |
| [`docs/MEMORY_WIKI_HARDENING.md`](docs/MEMORY_WIKI_HARDENING.md) | Knowledge lint, backup, and rollback |

---

## ⚠️ Current limitations

Chief of Staff is an internal beta.

It is currently:

```text
designed primarily for one company and operator per instance
Google Workspace-first
CLI- and agent-driven rather than a web application
a lightweight CRM, not a full CRM platform
an operational bookkeeper, not accounting software
```

It provides operational assistance and drafts, not legal, tax, accounting, investment, or compliance advice.

---

## 📄 License

Licensed under the [Apache License 2.0](LICENSE).

Use it, modify it, fork it, and build on it — including commercially — subject to the terms of the license.
