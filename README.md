# 🧭 Chief of Staff

**A private, approval-gated AI Chief of Staff for founders, operators, and small teams.**

Your inbox, calendar, deadlines, pipeline, invoices, tasks, documents, and notes — pulled into one operating view, every day, so you always know:

> What changed? What needs my attention? What is waiting for my decision? What should I do next?

It watches, prioritises, prepares, and proposes. **You approve. It executes. Everything is audited.**

> **Status:** v0.3.17 internal beta  
> **Runtime:** Python 3.11+ · runs as a [Hermes](docs/SETUP.md) agent plugin  
> **License:** Apache License 2.0

---

## 🤖 Easiest install: ask your agent

Chief of Staff is built to be agent-operated — every setup step is a CLI with machine-readable output, a readiness go/no-go, and self-diagnosis on failure. So the simplest install is to paste this to your agent (Hermes, OpenClaw, Claude Code, …):

> Clone https://github.com/moonlight-lupin/chief-of-staff-agent.git into my plugins directory and install its requirements. Run `chief_of_staff.py demo` and show me the output. Then bootstrap it for **\<company\>**, jurisdiction **\<SG\>**, operator **\<me@company.com\>**, workspace provider **\<m365 | google_api | composio\>**, assistant name **\<Ada\>**. I'll give you the credentials when you ask — put them in `.env`. Verify with `connect_workspace.py --verify`, run `chief_of_staff.py readiness --summary`, and if anything fails, use `chief_of_staff.py logs diagnose` and fix it. Stop before anything that sends email or modifies my workspace.

Your agent handles the clone, config, verification, and troubleshooting; you supply credentials and approvals. Prefer to drive it yourself? Everything below works by hand too.

Never paste API keys or secrets directly into chat logs.

---

## ⚡ See it in 60 seconds

No account, no credentials, no config:

```bash
git clone https://github.com/moonlight-lupin/chief-of-staff-agent.git && cd chief-of-staff-agent
python -m pip install -r requirements.txt
python shared/scripts/chief_of_staff.py demo
```

You'll get a full sample-data morning brief — urgent email, today's meetings with join links, stale deals, overdue invoices, open tasks — the view your mornings start with once it's connected to your own workspace.

The real thing looks like this every day:

```text
Chief-of-Staff Daily
  1. System health          ✅
  2. Briefing               2 urgent · 3 meetings · 1 deadline ≤7d
  3. Needs review           2 actions waiting for your approval
  4. Pipeline / CRM         1 deal inactive for 21 days
  5. Bookkeeper             3 invoice candidates · 1 possible duplicate
  6. Knowledge maintenance  4 wiki pages updated
  7. State safety           no stuck actions, no malformed files
  8. Recommended next commands
```

The daily loop is deliberately **read-only** — it reports and recommends, it never acts on its own.

---

## 🤔 Why this instead of another SaaS dashboard

**1. Nothing happens without you.** Suggestion ≠ approval ≠ execution. Sending email, changing calendar events, filing documents, touching CRM or invoice records — every consequential action sits in a Review Queue showing what will happen, why, the risk, and how to reverse it. You approve and execute as separate steps. Every execution is audited with before/after history.

**2. Your data is yours, in files you can read.** Deals, invoices, expenses, tasks, and your knowledge wiki live as plain YAML and Markdown on your own machine. Inspect them, version them, edit them in any editor, walk away anytime. No hosted database, no vendor lock-in.

**3. Works with the workspace you already have.** Google Workspace (service account or managed auth via Composio) **and Microsoft 365** (Outlook, Calendar, OneDrive — via Microsoft Graph, or via Composio managed OAuth with no Entra admin) are first-class, switchable with one config line. If your AI agent has its own Gmail/M365 connectors, it can fetch data itself and feed the same pipeline — no API client needed.

**4. It tells you when — and why — it isn't working.** A generated readiness report gives a go/no-go verdict per capability (not a vague health check). Every run writes structured, secret-redacted logs, and `logs diagnose` turns failures into plain-English findings with the exact commands to fix them: *"Microsoft Graph rejected your credentials — the client secret may have expired. Run: …"*

**5. You can name it.** `--assistant-name "Ada"` — then "Ask Ada to check my email" routes to *your* Chief of Staff, not a generic handler.

---

## ✨ What it handles

| Area | What it does |
|---|---|
| **Daily operations** | Morning briefings, weekly reviews, tasks, statutory + custom deadlines (SG/HK/US/UK packs) |
| **Email** | Classifies messages, proposes labels and organisation policies, drafts replies for approval |
| **Calendar & meetings** | Reads schedules, surfaces join links, builds pre-meeting briefs from CRM, notes, and open items |
| **CRM** | Lightweight pipeline: deals, stages, contacts, documents, stale-deal detection |
| **Bookkeeping** | Invoice candidates from email, field validation, duplicate detection, AR/AP tracking, P&L snapshots |
| **Documents & signing** | Fills templates, files to Drive/OneDrive, self-signs locally, or sends for e-signature (self-hosted DocuSeal) |
| **Knowledge** | Structured memory + a linked Markdown wiki, with autonomous low-risk curation |
| **Research** | Cited deep research and entity due-diligence dossiers |
| **Reliability** | Readiness verdicts, self-diagnosis, redacted support bundles, backups, audit logs |

Eighteen skills across command centre, planning, meetings and email, documents, commercial operations, knowledge, travel, and resilience — start with five (briefing, review queue, pipeline, bookkeeper, notes) and grow from there.

---

## 🚀 Set it up for real

```bash
# 1. Bootstrap: your company, your provider, your assistant's name
python shared/scripts/bootstrap.py \
  --company "Acme Studio" --jurisdiction SG \
  --operator you@acme.com \
  --workspace-provider m365 \
  --assistant-name "Ada"

# 2. Secrets go in .env (auto-loaded; never in config files)
echo 'M365_CLIENT_SECRET=...' >> .env

# 3. Verify every capability independently, then get the go/no-go
python shared/scripts/connect_workspace.py --verify
python shared/scripts/chief_of_staff.py readiness --summary
```

```text
Chief of Staff Readiness
  Core configuration        PASS
  Workspace authentication  PASS
  Mail read                 PASS
  Calendar read             PASS
  Files read                PASS
  Review queue              PASS
  Daily loop                PASS
  Optional writes           NOT TESTED
  Ready for daily read-only operation: YES
```

Full provider walkthroughs (Google service account, Composio, Microsoft 365, DocuSeal e-sign): [`docs/SETUP.md`](docs/SETUP.md).

---

## 🔒 The safety model

```text
Observe → Understand → Suggest → Approve → Execute → Audit
```

- **The daily loop never mutates anything** — no email sent, no events changed, no records written, no "auto-repair".
- **Destructive actions are double-gated**: sending email requires an explicit environment flag on top of approval.
- **Reversibility is a design rule**: archive/trash/tag operations are chosen for their undo paths; anything without a restore path (like cancelling a Microsoft 365 calendar event) is honestly refused rather than silently risked.
- **Logs are safe to share**: tokens, secrets, message bodies, and document contents never reach the operational logs — at any log level — and support bundles are redacted by construction.

```bash
python shared/scripts/review_queue.py preview --action-id <ID>   # what, why, risk, reversal
python shared/scripts/review_queue.py approve --action-id <ID> --approver "MH" --reason "…"
python shared/scripts/review_queue.py execute --action-id <ID>   # only now does it act
```

---

## 🔌 Workspace providers

One config line selects the backend; every skill works unchanged:

| Provider | Auth | Best for |
|---|---|---|
| `google_api` | Service account, domain-wide delegation | Self-hosted Google Workspace |
| `composio` | Managed OAuth (Composio MCP) | Google **or Microsoft 365** via managed OAuth (Connect Link, no admin) |
| `m365` | Entra ID app (client credentials or device code) | Microsoft 365 / Outlook / OneDrive |
| `agent` | Your AI agent's own connectors | Agent-native: fetch with connector tools, feed via `--input` |

Gmail-style queries are translated automatically for Microsoft Graph — the bundled search templates work on both stacks. Provider failures come back with permission-specific guidance (missing admin consent, unprovisioned OneDrive, expired secrets).

---

## 🩺 When something breaks

Every command runs under a correlation ID with structured JSONL logs. Failures are classified deterministically — throttling, expired credentials, missing admin consent, ambiguous writes, and a dozen more — each with an explanation, safe remediation, and whether auto-retry is safe:

```bash
python shared/scripts/chief_of_staff.py logs diagnose --latest-failed
python shared/scripts/chief_of_staff.py logs bundle --latest-failed   # redacted zip for bug reports
```

Failed readiness rows print the exact diagnose command. No stack-trace archaeology.

---

## ⚙️ Configuration and data

- One config file: `shared/config/company.yaml` (start from the bootstrap output or `company.yaml.example`)
- Secrets: `.env` in the plugin root (auto-loaded, shell env wins) — never in YAML
- Your records, in plain text under `paths.project_root`: `pipeline.yaml` · `invoices.yaml` · `expenses.yaml` · `todos.yaml` · `wiki/`

---

## 📚 Documentation

| Document | Purpose |
|---|---|
| [`docs/SETUP.md`](docs/SETUP.md) | Fastest path + all provider walkthroughs |
| [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md) | Run IDs, logs, diagnosis, support bundles |
| [`docs/REVIEW_QUEUE.md`](docs/REVIEW_QUEUE.md) | Review, approve, execute, audit |
| [`docs/BETA_DAILY_LOOP.md`](docs/BETA_DAILY_LOOP.md) | The daily operating loop |
| [`docs/PIPELINE_MANAGER.md`](docs/PIPELINE_MANAGER.md) | CRM operations |
| [`docs/BOOKKEEPER_INGESTION.md`](docs/BOOKKEEPER_INGESTION.md) | Invoice ingestion |
| [`docs/MEMORY_WIKI_HARDENING.md`](docs/MEMORY_WIKI_HARDENING.md) | Knowledge lint, backup, rollback |

---

## ⚠️ Honest limitations

Internal beta. One company and operator per instance. CLI- and agent-driven (no web UI). A lightweight CRM and operational bookkeeper — not replacements for Salesforce or Xero. Graph webhooks and >4 MB OneDrive uploads are deliberately deferred (polling works fine at this scale). It produces operational assistance and drafts — not legal, tax, accounting, or compliance advice.

---

## 📄 License

[Apache License 2.0](LICENSE) — use it, modify it, fork it, build on it, including commercially.
