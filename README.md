# 🧭 Chief of Staff

**A private, approval-gated AI Chief of Staff for founders, operators, and small teams.**

Your inbox, calendar, deadlines, pipeline, invoices, tasks, documents, and notes — pulled into one operating view, every day, so you always know:

> What changed? What needs my attention? What is waiting for my decision? What should I do next?

It watches, prioritises, prepares, and proposes. **You approve. It executes. Everything is audited.**

> **Status:** v0.9.0 (beta)
> **Runtime:** Python 3.11+ · runs as a [Hermes](docs/SETUP.md) agent plugin  
> **License:** Apache License 2.0

---

## 🤖 Easiest install: ask your agent

Chief of Staff is built to be agent-operated — every setup step is a CLI with machine-readable output, a readiness go/no-go, and self-diagnosis on failure. So the simplest install is to paste this to your agent (Hermes, OpenClaw, Claude Code, …):

> Clone https://github.com/moonlight-lupin/chief-of-staff-agent.git into my plugins directory and install its requirements. Run `chief_of_staff.py demo` and show me the output. Then bootstrap it for **\<company\>**, jurisdiction **\<SG\>**, operator **\<me@company.com\>**, workspace provider **\<m365 | google_api | composio\>**, assistant name **\<Ada\>**, storage **\<local | git: owner/private-data-repo\>**. I'll give you the credentials when you ask — under Hermes, in the plugin's settings; otherwise in `.env`. Verify with `connect_workspace.py --verify`, run `chief_of_staff.py readiness --summary`, and if anything fails, use `chief_of_staff.py logs diagnose` and fix it. Stop before anything that sends email or modifies my workspace.

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

**See the whole loop** — still no credentials, nothing leaves your machine:

```bash
python shared/scripts/chief_of_staff.py demo --flow
```

Seven steps on the sample data:

1. the morning briefing;
2. the agent proposes a follow-up email;
3. the preview, showing what it will do, to whom, and how it is reversed;
4. **your** signed approval, exactly as `/cos approve` does it;
5. execution through the real guarded path, against a stand-in mailbox;
6. the audit trail;
7. the queue afterwards.

It runs in a throwaway directory, so every run starts fresh. Add `--keep DIR` to inspect the state, and `demo --reset DIR` to remove it.

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

**3. Works with the workspace you already have.** Google Workspace (service account, or managed auth via Composio) and Microsoft 365 (Outlook, Calendar and OneDrive through Composio managed OAuth, with no Entra admin needed) have been run against live accounts. You switch between them with one config line. Native Microsoft Graph is code-complete but has not yet been run against a real tenant, and `capabilities` says so. It reports, for every action, whether it was proven live, only checked against the vendor's catalog, or covered by tests only. If your AI agent has its own Gmail/M365 connectors, it can fetch data itself and feed the same pipeline — no API client needed.

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
| **Workflows** | Capture a repeating business process as declarative YAML (guided interview), then let the orchestrator run it: cron-fired or manual starts, per-step completion signals, approval gates through the review queue, and live run status via `workflows list`/`runs`/`advance` |
| **Research** | Cited deep research and entity due-diligence dossiers |
| **Prospecting** | Finds companies that fit and have a reason to buy now (hiring, funding, leadership change), writes a fit verdict per account, drafts outreach for approval, handles replies |
| **SEO & AI search** | Site audits, keyword research, search competitors, local/Maps, backlinks, and whether AI assistants mention you, with rankings tracked run over run |
| **Market watch** | Competitor site changes (new pages, copy and price changes) and scheduled news monitoring |
| **Spend control** | Pre-approved budgets for paid data calls, reserved before every call, capped per month, shown in the briefing |
| **Several companies** | One install, several companies with isolated data, plus read-only group roll-ups for holding structures |
| **Notion** | Mirrors pipeline and tasks to Notion with approval on every pull-back, and publishes wiki notes as Notion pages |
| **Reliability** | Readiness verdicts, self-diagnosis, redacted support bundles, backups, audit logs |

Twenty-six skills across command centre, planning, meetings and email, documents, commercial operations, growth, knowledge, workflows, travel, and resilience. Twenty-two register on a default install. The others appear once configured: `esign-connector` with DocuSeal, `notion-sync` and `notion-wiki` with a `notion:` section, and `prospecting` with a `spend:` budget. Start with five (briefing, review queue, pipeline, bookkeeper, notes) and grow from there.

---

## 📈 Skills that grow the business

Running the business is half the job; finding the next customer is the other half. These skills apply the same rule as everything else: they research and propose, you approve, nothing is sent or bought behind your back. Paid data (via Monid's endpoints, e.g. DataForSEO, Firecrawl, company and contact search) is priced for free first and drawn only from a budget you approved.

| Skill | Ask it to… | What you get |
|---|---|---|
| **`prospecting`** | *"Find 20 SG fintechs that are hiring in finance and worth a call"* | A ranked list: each account has a tier (A/B/C), a one-line *why now* from a dated, sourced signal, and a written fit verdict you can disagree with. Contact data is bought only for A/B accounts. Outreach drafts carry one true observation and a free offer; each send is approved separately. Opt-outs are permanent, and a reply from anyone at a company stops follow-ups to everyone there. `stats` shows which signals actually get replies. |
| **`seo-tracker`** | *"Audit our site"*, *"What keywords should we target?"*, *"Do ChatGPT and Google's AI mention us?"* | A site crawl with page-level fixes, keyword and competitor gaps, local/Maps checks, backlinks, AI-visibility findings, and what moved since the last run. |
| **`competitor-watch`** | *"Watch these three competitors"* | A diff each run: new or removed pages, changed titles and copy, and price changes on pricing pages. |
| **`news-monitoring`** | *"Send me a weekly digest on payments regulation in SEA"* | A scheduled, sourced headline digest on the topics you care about. |
| **`entity-research`** | *"Background check on Acme before Tuesday's meeting"* | A due-diligence dossier: ownership, leadership, adverse media, sanctions, with every fact sourced. |
| **`deep-research`** | *"Research the market for X in depth"* | A cited report built in rounds, with quotes anchored to their sources. |
| **`pipeline-manager`** + **`meeting-prep`** | *"Prep me for the call with PayFast"* | Approved prospects become pipeline deals; meeting prep pulls the deal, past threads, notes and research into one brief. |

```bash
# Propose a budget once; the agent reserves against it before every paid call
.venv/bin/python shared/scripts/spend.py propose --provider monid \
  --purpose "Find SG fintech CFOs for Q4" --topic sg-fintech-cfos --cap 20 --currency USD
.venv/bin/python skills/prospecting/scripts/prospects.py list   # ranked by tier, no contact details
```

---

## 🚀 Set it up for real

```bash
# 1. Bootstrap: your company, your provider, your assistant's name
python shared/scripts/bootstrap.py \
  --company "Acme Studio" --jurisdiction SG \
  --operator you@acme.com \
  --workspace-provider m365 \
  --tenant-id <directory-tenant-id> --client-id <application-client-id> \
  --user-principal you@acme.com \
  --assistant-name "Ada" \
  --storage local          # or: --storage git --data-repo you/private-cos-data

# 2. Secrets: under Hermes, in Settings → Plugins → Chief of Staff (masked).
#    Standalone: in .env (auto-loaded; never in config files)
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

## 💬 In Hermes: tools and your own slash command

Under Hermes the plugin adds two kinds of commands. The agent gets tools to look; you get a slash command to decide.

| Who | What | Can it change anything? |
|---|---|---|
| **The agent** | Tools: `cos_capabilities`, `cos_daily`, `cos_review_queue`, `cos_preview`, `cos_pipeline`, `cos_spend_status` | No. They are read-only, and none can approve, dismiss or execute. |
| **You** | `/<assistant name>`, e.g. `/ada` if you named it Ada, plus `/cos` (always works) | Yes: `approve` and `dismiss` |

```text
/ada                        today's briefing
/ada review                 what is waiting for your decision
/ada preview <id>           what an action will do, to whom, and how it is reversed
/ada approve <id> [reason]  approve it
/ada dismiss <id> <reason>  dismiss it
```

The command follows `assistant.name`. The name must start with a letter and use only letters, digits, spaces, `-` or `_`, at most 32 characters, so it always makes a valid command. To rename, run `bootstrap.py --assistant-name Bea` and restart Hermes: `/bea` appears, and `/ada` only replies that it has been retired.

Approving works from your own CLI, TUI or Desktop session. From a chat app (Telegram, Discord…), only people listed in `approvals.chat_approvers` can approve, so nobody else in a shared chat can approve anything. Execution stays where it was: an approved action is claimed and run, then audited.

---

## 🔁 Workflow Orchestrator

Capture a repeating business process once as declarative YAML, and the orchestrator runs it end-to-end — with the same approval gates as everything else.

```bash
.venv/bin/python shared/scripts/chief_of_staff.py workflows list          # project workflow YAMLs
.venv/bin/python shared/scripts/chief_of_staff.py workflows install <name>  # validate + register + optional cron
.venv/bin/python shared/scripts/chief_of_staff.py workflows runs --summary  # live run status
.venv/bin/python shared/scripts/chief_of_staff.py workflows advance --run-id <id>  # complete the current step
```

- **The interview, not code:** ask the agent (via the `workflow-architect` skill) to capture a process; it walks a structured interview and writes `workflows/<name>.yaml` after your confirmation.
- **Per-step completion signals:** command pattern match, project file, review-queue approval, or manual advance.
- **Approval-gated steps:** a `review_queue` step proposes an action; the run pauses until you approve and execute in the review queue.
- **Runs are observable:** pointer strips in briefings, structured logs, and `logs diagnose` for failed runs.

---

## 🔒 The safety model

```text
Observe → Understand → Suggest → Approve → Execute → Audit
```

- **The daily command is externally read-only (no workspace mutations) but writes local telemetry (.last_briefing timestamp) to prevent duplicate briefings.** No email is sent, no events are changed, no business records are written, and there is no "auto-repair".
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
- Secrets, never in YAML:
  - **Under Hermes:** set them in the plugin's settings (Desktop: Settings → Plugins → Chief of Staff), where Hermes stores them. The plugin reads them only through Hermes and never opens a `.env` or key file there.
  - **Standalone or Claude Code:** `.env` in the plugin root (auto-loaded, shell env wins).
- Your records, in plain text under `paths.project_root`: `pipeline.yaml` · `invoices.yaml` · `expenses.yaml` · `todos.yaml` · `wiki/`

---

## 🔎 What it touches

Read this before installing. Nothing below happens without your configuration, and every consequential action waits for your approval.

- **Network:** only the services you configure:
  - Google APIs (`googleapis.com`, `gmail.googleapis.com`, `people.googleapis.com`);
  - Microsoft Graph and Entra sign-in (`graph.microsoft.com`, `login.microsoftonline.com`);
  - Composio (`backend.composio.dev`, `connect.composio.dev`);
  - your own DocuSeal server.

  Prospecting, SEO and competitor watch call Monid through the agent's own connector, and only against budgets you approved. Notion is reached through the agent's own connector. Under the `agent` provider the plugin makes no workspace calls at all: the agent's connectors do.
- **Shell commands:**
  - `hermes cron create` / `hermes cron list` (installing and checking the scheduled briefings you approve);
  - `git` (only `sync push` / `sync status` for your private data repo);
  - the optional google-workspace skill's `google_api.py` for Google reads;
  - the plugin's own Python scripts.
- **Your data, outside the plugin folder:**
  - plain YAML and Markdown under `paths.project_root` (pipeline, invoices, expenses, todos, wiki);
  - a local `state.db` (review queue, audit log, spend ledger);
  - run logs, with secrets and message bodies redacted.

  `sync push` commits and pushes that data to the private git repo you named, never to this repository.
- **Background processes:** none by default. `webhook_receiver.py` is an optional HTTP listener you start yourself for push notifications. It only records events and never acts.
- **Credentials:** all optional, one per provider: `COMPOSIO_MCP_KEY`, `M365_CLIENT_SECRET`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `DOCUSEAL_API_KEY`, `DOCUSEAL_MCP_TOKEN` and the two webhook secrets. Under Hermes they are read only through Hermes (`get_secret`, and per session for the plugin's own commands). The plugin stores no tokens of its own.
- **Approvals:** sending, moving, deleting, filing, signing and paid data calls all go through the review queue: propose, approve, execute, audit. Under Hermes, approvals are signed in the gateway, so only you, through `/<name> approve`, can approve. The agent can't, even from its terminal ([security model](docs/SECURITY_MODEL.md)). `CHIEF_OF_STAFF_AUTO_APPROVE` and `CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE` are operator break-glass switches that are off by default. Without a terminal, any confirmation that needs one is refused, never left waiting.
- **Telemetry:** none.

---

## 📚 Documentation

| Document | Purpose |
|---|---|
| [`docs/SETUP.md`](docs/SETUP.md) | Fastest path + all provider walkthroughs |
| [`docs/CLAUDE_CODE.md`](docs/CLAUDE_CODE.md) | Running in Claude Code on the web: session hooks + a private git data repo |
| [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md) | Run IDs, logs, diagnosis, support bundles |
| [`docs/REVIEW_QUEUE.md`](docs/REVIEW_QUEUE.md) | Review, approve, execute, audit |
| [`docs/BETA_DAILY_LOOP.md`](docs/BETA_DAILY_LOOP.md) | The daily operating loop |
| [`docs/PIPELINE_MANAGER.md`](docs/PIPELINE_MANAGER.md) | CRM operations |
| [`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md) | What is guaranteed, against whom, and where it ends |
| [`docs/HERMES_CATALOG.md`](docs/HERMES_CATALOG.md) | Hermes plugin-catalog entry, rule-by-rule compliance, submission steps |
| [`docs/MULTI_COMPANY.md`](docs/MULTI_COMPANY.md) | Several companies on one install: registry, isolation, group roll-up |
| [`docs/BOOKKEEPER_INGESTION.md`](docs/BOOKKEEPER_INGESTION.md) | Invoice ingestion |
| [`docs/MEMORY_WIKI_HARDENING.md`](docs/MEMORY_WIKI_HARDENING.md) | Knowledge lint, backup, rollback |

---

## ⚠️ Honest limitations

Beta. One operator per instance; several companies are supported, each with its own isolated data. CLI- and agent-driven (no web UI). Prospecting and SEO need a paid data provider and an approved budget, and outreach is drafted for approval, never sent in bulk. A lightweight CRM and operational bookkeeper — not replacements for Salesforce or Xero. Graph webhooks and >4 MB OneDrive uploads are deliberately deferred (polling works fine at this scale). It produces operational assistance and drafts — not legal, tax, accounting, or compliance advice.

---

## 📄 License

[Apache License 2.0](LICENSE) — use it, modify it, fork it, build on it, including commercially.
