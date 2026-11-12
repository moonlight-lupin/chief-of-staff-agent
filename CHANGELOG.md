# Changelog

## Unreleased

### Untrusted content is data, not authority

- **New `shared/scripts/payload_policy.py`.** `drive.download` writes only
  inside the project root. `drive.upload` reads only from the project root or
  `paths.upload_roots`, and never from `.env`, keys, `~/.ssh`, `~/.hermes` or
  `state.db`. Symlinks are resolved. Paths are checked when the action is
  proposed and again at execution.
- **`drive.download` is now medium risk** and can no longer be bulk-approved.
- **Wiki context injected into the model** is a labelled data block,
  sanitised to one line per note. Pages curated from external events give
  only their title.
- **Workflows.**
  - Step text that approves, dismisses or turns on break-glass is refused.
  - `bind_action` requires an existing action of the step's declared type.
  - A command signal counts only when the command actually runs, not when it
    is echoed or grepped.
- **Redaction** covers prefixed secret names (`COMPOSIO_API_KEY=`,
  `M365_CLIENT_SECRET=`, `*_TOKEN=`, `GOOGLE_SERVICE_ACCOUNT_JSON=`). Tool
  errors returned to the model are redacted.
- **Notion pulls** refuse a `contact_email` that is not a plain address.
- **`docs/SECURITY_MODEL.md`** gains the trust-boundary table. The adversarial
  fixtures are in `tests/test_untrusted_content.py`.


### Account binding: refuse the wrong account before the write

- **Workspace actions are bound when they are created.** This covers mail,
  calendar, files, contacts and Notion.
  - The `binding` records the account the company declares, the workspace
    provider, and the identity the configured client acts as (the Google
    delegate, the M365 mailbox, or a digest of the Composio routing).
    Identity is bound even when no `accounts:` block exists.
  - The binding is part of the approval fingerprint, for new actions only,
    so existing approvals keep theirs.
  - If either changes in company.yaml after approval, execution is refused as
    `binding_changed` before anything runs.
- **Python path.** Clients report the identity they act as when they can know
  it without a network call: Google `delegate_email`, Microsoft 365
  client-credentials `user_principal`. `send_email`, `delete_actions` and
  `webhook_events` refuse a mismatch before the provider call.
- **Agent path.** `claim --executor-account` is required when an account is
  declared, and a mismatch is refused (and audited) before the action moves
  to `executing`. `record-execution` compares against the approved binding,
  not today's config.
- **`doctor`** now warns when executions were recorded without a verified
  account. Previously that passed.
- **The registry** refuses two scopes (companies or groups) that send as the same account. A shared calendar or Notion workspace is allowed.
- **Python-path executions** record `account_check`. Unverifiable ones show as `unverified` in `doctor`.
- **Google.** The guessed `account_alias` heuristic (from the key file's name)
  is removed. The alias comes only from config.
- **Breaking for agents:** `claim` needs `--executor-account` wherever
  `accounts:` is declared. CLAUDE.md, SETUP and the Notion skills are updated.
- **Docs:** `docs/MULTI_COMPANY.md` gains "Account binding: what is enforced
  before the write", including what cannot be verified.


### Signed approvals: only the user approves (Hermes)

- **New `shared/scripts/approval_signing.py`.**
  - When the Hermes gateway loads the plugin, it creates an Ed25519 key in
    its own memory, never on disk or in any environment, and enrols every
    company store it serves.
  - For an enrolled store, `requested → approved/dismissed` needs a signature
    from that key over the action, its approval fingerprint, its company, the
    decision and the approver. `approved → executing` re-verifies it.
  - Refusals are audited (`approval_unsigned`, `approval_signature_invalid`).
  - Every approve command is covered, because all of them go through
    `state_db.transition_action`: `review_queue.py approve` (single and bulk),
    `send_email.py`, `delete_actions.py` and `webhook_events.py`.
  - Approvals made before enrolment must be re-approved.
- **`/cos approve` and `/cos dismiss`** now sign in-process instead of
  running a child process. A copy of the module imported anywhere else has no
  key and cannot decide anything. Refused slash attempts are audited
  (`slash_approval_refused`).
- **Break-glass.** For an enrolled store, `CHIEF_OF_STAFF_AUTO_APPROVE`,
  `CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE` and a typed "y" no longer approve a write.
  The new `workspace_guardrails.execution_grant` allows only the guarded
  writes of one signed, executing action, and only of its own type. The
  executors use it.
- **Hardening from an adversarial review**, each fix with a regression test:
  - An executing or finished action can no longer be re-approved. On a
    signed store, a failure is final; a retry needs a new proposal.
  - Enrolment is recorded for the whole install
    (`<Hermes home>/chief-of-staff/approval-signing.json`), so a new or
    copied store is enforced too. A store that can't be read fails closed.
  - Execution grants are bound to the approved target, used once, and end
    when the action leaves `executing`.
  - Spend envelopes need a signed approval.
  - No store API can switch enforcement off. Every enrolment is audited
    with its process.
  - An executor whose grant is refused marks the action failed instead of
    leaving it stuck in `executing`.
- **Standalone and Claude Code installs** never enrol and are unchanged.
- **New `doctor` check `approval_signing`.** It reports the enrolment state
  and the recommended Hermes `approvals.deny` rules.
- **Docs.** New `docs/SECURITY_MODEL.md` (who we defend against, guarantees,
  limits). CLAUDE.md, the workflow pointer and the README now say approval is
  the user's step.

## v0.9.0 (beta) — Hermes tools and your own slash command

The agent gets tools to look; you get a slash command to decide. The split
makes the plugin's invariant structural: no tool can approve.

- **Six read-only tools** (`register_tool`, toolset `chief-of-staff`):
  `cos_capabilities`, `cos_daily`, `cos_review_queue`, `cos_preview`,
  `cos_pipeline` and `cos_spend_status`. Each runs the plugin's existing
  script as a child process, with secrets read through Hermes, and returns
  JSON. Unlike plugin skills, which load only when named, tools are always
  visible to the agent. They are declared in `plugin.yaml` →
  `provides_tools`.
- **A slash command named after your assistant**: `/<assistant.name>`
  (e.g. `/ada`), plus `/cos`, which always works. Subcommands: `briefing`
  (the default), `review`, `preview <id>`, `approve <id> [reason]`,
  `dismiss <id> <reason>` and `help`.
  - Names are lower case, spaces and symbols become `_`, and they are at most
    32 characters. A default or unusable name registers `/cos` only.
  - Hermes drops a name that clashes with a built-in command; `/cos` still
    works then.
- **The name is restricted, because it is also the command.** The new
  `config_loader.validate_assistant_name` rule: start with a letter; then
  letters, digits, spaces, `-` or `_`; at most 32 characters.
  `bootstrap --assistant-name` and skill-name rendering refuse anything else
  (this replaces the looser routing-name rule for `assistant.name`; `company.name`
  is unchanged). An existing invalid name only makes `doctor` warn, and the
  command falls back to `/cos`.
- **Renaming.** Change `assistant.name` (e.g. `bootstrap --assistant-name Bea`)
  and restart Hermes or reload the plugin: `/bea` is registered. Hermes cannot
  un-register a command, so the old `/ada` stays wired until restart. It reads
  the current name on every call and only replies that it is retired, never
  acting.
- **Who may approve.** `approve` and `dismiss` run only from the owner's own
  surfaces (`cli`, `tui`, `desktop`, `local`), recorded under `user.name`, or
  for chat users listed in `approvals.chat_approvers`
  (`"telegram:<user id>": "<name>"`). Everyone else is refused: an unlisted
  user, a listed user on another platform, a chat command without a source,
  or an unknown platform. Action ids that look like flags are rejected.
- Execution is unchanged: approved actions still go through claim →
  record-execution, or `execute`.
- New `shared/scripts/hermes_surface.py` and `tests/test_hermes_surface.py`.
  Handlers never raise: tools return JSON, the slash command returns text.
- CLAUDE.md tells agents to prefer the tools, and to ask the user to approve
  with the slash command rather than ever approving themselves.

## v0.8.1 (beta) — Hermes plugin-catalog readiness

Makes the plugin admissible to the Hermes plugin catalog. Every rule that can
be checked in code is covered by `tests/test_hermes_catalog.py`. See
[`docs/HERMES_CATALOG.md`](docs/HERMES_CATALOG.md) for the rule-by-rule
mapping, the draft entry and the submission steps.

### Credentials through Hermes (catalog rule 11)

- New `shared/scripts/plugin_secrets.py`, the one place secrets are read.
  Inside Hermes it uses `agent.secret_scope.get_secret`; outside, the command
  environment.
- Every secret is declared in `plugin.yaml` → `config_schema` as
  `type: secret`, so Hermes stores it and the Desktop shows a masked field.
  All of them are optional, so none is listed in `requires_env`, which would
  disable the plugin when unset.
- `register()` hands the secrets to the plugin's own commands per session
  through `ctx.register_session_env`. Each value is read with `get_secret`,
  and older Hermes releases skip this.
- Under Hermes the plugin never reads a `.env` file, its own included.
  Standalone and Claude Code installs still load the plugin-root `.env`.
- Google service account: the new `GOOGLE_SERVICE_ACCOUNT_JSON` secret holds
  the key's contents and is used with `from_service_account_info`. Under
  Hermes a key file (`google.service_account_path`) is refused with a pointer
  to the secret.
- Every direct environment read of a secret now goes through
  `plugin_secrets.get`. A test fails if one is added back.

### Unattended runs (rule 12)

These now refuse cleanly when there is no terminal, instead of waiting:
- interactive onboarding (use `--non-interactive --config`);
- self-sign confirmation (pass `--locations`);
- Microsoft 365 device-code sign-in (scheduled and gateway runs need
  `client_credentials`).

### Manifest (rules 6, 10, 14)

- `plugin.yaml` declares `requires_hermes: ">=0.23.0"`, `provides_tools: []`,
  the five registered hooks, `requires_env: []`, `homepage`, `tags` and
  `python_dependencies`.
- Every dependency now has an upper bound, in both `pyproject.toml` and
  `plugin.yaml`.

### Disclosure (rule 13)

- README gains "What it touches": network hosts, shell commands, data
  locations, the optional webhook listener, credentials, approvals, and no
  telemetry.

### Catalog readiness

- `task-brief` is now registered in the default and enterprise profiles.
  Since v0.7.12 it shipped but never loaded. Its SKILL.md is replaced with the
  clean 1.3.0 from moonlight-lupin/agent-skills (the plugin copy had a second
  frontmatter block spliced into its body). Only the frontmatter was adapted
  to the plugin's top-level `version` convention.
- The `plugin.yaml`, `plugin.json` and `marketplace.json` descriptions now
  list prospecting, SEO and AI-search visibility, competitor watch, spend
  budgets, multiple companies and Notion sync.
- New `tests/test_skill_registration.py`:
  - every skill directory is registered and every registered skill exists;
  - each SKILL.md has a single frontmatter block;
  - the manifest descriptions mention the current features.

## v0.8.0 (beta) — Multi-company phases 1 and 2, Notion mirror, prospecting v2

### Prospecting v2: signals, written verdicts, replies

- `prospecting` skill 0.2.0 reworks the workflow into an 11-step funnel where
  spending rises with confidence. It runs brief, size and sample, budget,
  accounts first, signals, written verdict, contacts for tier A and B only,
  propose, one-signal drafts, replies, then learn. New references:
  `references/signals.md` (vocabulary, weights, recency, hook rules) and
  `references/messaging.md` (first-email anatomy, follow-ups, reply playbook).
- `shared/scripts/prospecting.py` and `prospects.py` gain these commands:
  - `observe` stores buying signals with evidence URL, event date, confidence
    and attribution. Industry-wide news is refused as a company signal.
  - `assess` takes a written fit verdict and derives the tier (A/B/C), the
    anchor signal, a why-now line and a `do_not_pursue` flag (strong trigger,
    weak fit). Strength is type weight × recency decay. Signals under 0.7
    confidence or over 90 days old are unusable.
  - `add-contact` adds a second contact at the same account.
  - `reply` records the reply category. Reply text is never stored, and
    opt-out wording overrides the category given. Any real reply pauses the
    whole account.
  - `touch` and `stats` give reply rates by opener signal and by tier.
- Behaviour changes:
  - `propose` now requires a `fit` verdict.
  - A missing ICP field now counts as "not assessed" rather than a failed
    check.
  - `list` ranks by tier and shows the why-now line.
- The concepts draw on three public outbound projects (explorium-ai/gtm-skills,
  ethanplusai/sellbird, eracle/OpenOutreach). They are credited in NOTICE.
  Ideas only: no code or text was copied.

### Prospecting, SEO audit, competitor watch (phase 4b)

- `prospecting` skill and `shared/scripts/prospecting.py`. Ingests leads the
  agent fetched, each with full provenance (provider, endpoint, run,
  fetched-at, purpose).
  - PDPA minimisation: business contact fields only; others are dropped and
    reported by name; personal mailbox addresses are dropped.
  - Leads already in the pipeline, already listed, or opted out are skipped.
  - Opt-outs are kept as hashes in a suppression list that outlives purges.
  - Phone and SMS contact are blocked until the number is cleared against the
    DNC registry.
  - Chosen prospects become queued `pipeline.deal.add` actions.
  - Retention purge is a dry run unless confirmed.
  - The skill is registered only when `spend:` names a provider.
- `seo-tracker` skill and `shared/scripts/seo_audit.py`. The workflows are
  adapted from OpenSEO's skills (every-app/open-seo, MIT, credited in
  NOTICE): choosing a workflow, project setup, site audit, keyword research,
  competitive landscape, local SEO, backlinks, AI visibility, and the report
  structure. The OpenSEO app and MCP server are not used. SEO data comes from
  DataForSEO through Monid: endpoints are found with `monid_discover`,
  priced with `monid_inspect`, and every paid call goes through a spend
  envelope. The default location is the company's own market. Project
  context and the research log live in the wiki. `seo_tracker.py` records
  rankings and issue counts after each run to show what moved, and its
  built-in on-page checks give a quick health check.
  Endpoint table re-checked against Monid (2026-11-12): DataForSEO has no
  site-wide crawl. Site audits now crawl with Firecrawl `/crawl` (0.001/page,
  limit sized into the envelope) and use `/onpage/instant-pages` only for
  per-URL deep checks. The AI visibility section now says when to use
  `/ai/mentions-*` (brand mentions, 0.10 flat), `/ai/*-response` (single
  prompts) and `/ai/keyword-search-volume` (topic demand only).
- `competitor-watch` skill and `shared/scripts/competitor_watch.py`: snapshot
  diffs showing new and removed pages, title, content and price changes.
  Only titles, hashes and the prices found are stored.
- Paid fetching for all three goes through spend envelopes. Their
  instructions name Monid's discover, inspect, run and dispute flow.

### Spend envelopes (phase 4a)

**Adds `data.spend_envelope` (high risk) and a spend ledger.**

- New `shared/scripts/spend.py`. A human approves an envelope once:
  provider, purpose, topic, cap, currency, expiry, and optionally which
  endpoints it allows. Before each paid call the agent reserves the estimated
  cost (`spend.py reserve`). A call on the same topic within the cap is
  approved by the envelope and audited with `envelope:<id>` as the approver.
  The call is refused with a reason code (and exit code 3) for a different
  topic, an endpoint not on the list, going over the cap or the company
  monthly cap (`spend.<provider>.monthly_cap`), an envelope that has expired
  or changed after approval, or an envelope from another company.
- Reservations are atomic, so concurrent calls cannot overshoot the cap. The
  actual cost is recorded after the call. An overrun is recorded as it
  happened and exhausts the envelope. Unrecorded reservations are flagged by
  `doctor` (`spend_reservations`).
- The daily briefing gains a **Spend** section (month to date against the
  cap, remaining budget per envelope). Approved envelopes no longer appear as
  pending approvals. `capabilities` reports spend, and `preview` explains an
  envelope before it is approved.

### Onboarding an existing Notion database

**Adds `notion.db.add_property` (high risk, agent path) and `notion.link` /
`notion.resolve` (medium risk, local).**

- `notion.columns.<domain>` maps plugin columns to an existing database's own
  names. `null` marks a column the database doesn't have.
- `notion_sync.py check`: compares the database's properties with what sync
  needs and suggests a column mapping. `plan_sync` now refuses to run until
  the latest check passes, and again after the mapping changes.
- `check --add <column>` queues adding a missing column, the `CoS ID` column
  included. It runs only after approval, and the check is cleared afterwards.
- Sync refuses an existing database whose rows are not linked yet, instead
  of duplicating them. `notion_sync.py link` matches rows by name, with
  contact email or due date as tie-breakers. Ambiguous matches are reported,
  not guessed. Links are applied after approval.
- `notion_sync.py resolve --keep store|notion` settles a conflict. The
  copying itself is still a separate approved sync action.
- The briefing and meeting-prep Notion inputs honour the column map.

### Notion as an input to existing skills

- The workspace envelope (`--input`) accepts an optional `notion` block: pages,
  comments, and rows of the mapped databases. It is validated in `schemas.py`
  and kept only when present, so envelopes without Notion normalise exactly
  as before.
- New `shared/scripts/notion_inputs.py` (read-only). It produces recently
  edited pages, mentions of the operator, Notion-only tasks due or overdue,
  and sync status per mirrored database (edits waiting for review, conflicts,
  changes not yet queued). Page and comment text is used only for matching
  and is never returned.
- Daily briefing: a new **Notion** section in text, Markdown and HTML.
  Mentions, Notion-only tasks and sync conflicts appear under Needs
  Attention, and pending sync work appears as a suggested `notion-sync` run.
  The summary gains `notion_items`.
- Weekly review: `workspace_collect.py all --input` adds a `notion` key for
  the week.
- Meeting prep: `gather --input` adds `notion_context`: pages and rows that
  match attendees by email, by company domain, or by the client name of their
  pipeline deal.

### Notion mirror (phase 3)

**Adds agent-path action types and a new local action, `notion.pull`.**

- New `shared/scripts/notion_mirror.py`. The plugin store stays the source of
  truth and Notion is a mirror. Each record is compared three ways against
  the hash stored at the last sync. A store change is queued as
  `notion.db.upsert`. A Notion edit is queued as `notion.pull` and applied
  only after approval. An edit on both sides is a conflict and produces no
  action. A record removed from the store is queued as `notion.page.archive`.
  A new Notion row is queued as a `notion.pull` create. Planning is
  read-only, and queueing never duplicates an open action.
- Notion edits can change only allowlisted fields (pipeline: stage, value,
  contact; todos: title, status, priority, due). Other edits are reported as
  ignored. Values the store would reject are refused at planning. A pull is
  refused if the store record changed after planning.
- New skills `notion-sync` and `notion-wiki` (wiki Markdown → Notion pages,
  republished as an update only when the file changed). Both are registered
  only when `notion:` is configured.
- Risk levels: `notion.db.upsert` low, `notion.page.create`/`update` and
  `notion.pull` medium, `notion.page.archive` high. `preview` explains the
  effect and how to reverse each one.
- `notion.*` writes are refused on the Python `execute` path; they go through
  claim → record-execution. `record-execution` updates the mirror and warns
  when `page_id` is missing. After approval, agent-path actions now point to
  `claim` instead of `execute`.
- `capabilities` reports a `notion` section. `claim` returns the declared
  Notion account as `expected_account`.


### Phase 2 — group scope, sharing, roll-up, connector accounts

**Touches the approval transition and record-execution.**

- Group scope: `--group <slug>` / `CHIEF_OF_STAFF_GROUP` selects the group's
  own config and store. Its actions are bound to `group:<slug>`. Selecting a
  company and a group at the same time is refused.
- `approvals.approvers`: when a config lists approvers, approval by anyone
  else is refused and audited as `approver_not_authorised`. Group configs
  must list approvers. Configs without a list are unchanged.
- Registry `share:` gives per-domain, read-only sharing within a group.
  `company_registry.read_shared` opens the owner's store read-only and audits
  every attempt. Writable or unknown share entries are rejected, and group
  stores must not overlap company stores.
- New `chief_of_staff.py --group <slug> group-summary`: a read-only roll-up of
  pipeline, todos, invoices and expenses for domains the group shares.
- `accounts:` in company.yaml. `claim` returns `expected_account`, and
  `record-execution --executor-account` is checked against it (`match`,
  `mismatch`, `unverified`, or `not_declared` in the result). A mismatch is
  audited, and `doctor` gains the `executor_accounts` and `company_registry`
  checks. `doctor` now follows the registry's company selection.
- CLAUDE.md gains a "Multiple companies" section for agents.

### Phase 1

Lets one install serve several companies, each with its own isolated store.
Design and decisions: [`docs/MULTI_COMPANY.md`](docs/MULTI_COMPANY.md).
**This change touches the pending-action state machine and the approval
fingerprint.**

### Changes

- New `shared/scripts/company_registry.py`. The optional registry
  (`shared/config/companies.yaml` or `CHIEF_OF_STAFF_REGISTRY`) lists
  companies and groups. Loading it refuses a company whose `project_root` is
  the same as, or nested inside, another company's, and refuses a declared
  `company.id` that does not match the registry slug.
- You select the active company with `--company <slug>` on
  `chief_of_staff.py` / `review_queue.py`, or with `CHIEF_OF_STAFF_COMPANY`.
  If several companies are registered and none is selected, `load_config`
  refuses to run instead of guessing.
- Pending actions now record `company_id` (a new column, migrated in place).
  The ID is part of the approval fingerprint when set, so single-company
  fingerprints are unchanged. A claim made from a different company is blocked
  and audited as `company_mismatch`.
- `capabilities` reports the active company and group, and states that the
  agent provider cannot stop a connector acting from another company's
  account.
- Without a registry, behaviour is unchanged.

## v0.7.12 — Research skills 1.13.0 sync + task-brief skill

deep-research 1.12.0 → **1.13.0** and entity-research 1.1.0 → **1.2.0**
(pulled from the morning `agent-skills` PRs, three-way-merged so the
plugin-owned contracts survive), plus a new **task-brief 1.3.0**
skill (Done-when tests + reusable-prompt mode), which deep-research now
takes as plan input when confirmed.

### Changes

- `deep-research` 1.13.0: new `references/evidence-hygiene.md` (source
  families — count one story once, `[VERIFIED]` needs two source *families*;
  shelf life for time-sensitive facts; money-figure bases; user-supplied
  reports) and `references/comparison-mode-and-recipes.md` (items × fields
  grid with a user-approved outline); `step3-iterative-loop.md` gains the
  near-duplicate-query skip and typed standing instructions
  (`focus` / `exclude` / `prioritise` / `stop`) that persist across rounds;
  Done-when tests + load-bearing-claims planning in Step 1; Step 0 carries a
  confirmed task brief straight into the plan.
- `entity-research` 1.2.0: gap-analysis pass wording and Weigh-step detail.
- `task-brief` 1.3.0: new skill — goal with 2–5 checkable "done when" tests,
  constraints injected not asked, tooling routing, reusable-prompt mode.
- Plugin contracts unchanged: research run directories still live under
  `<project_root>/research/`, stats still report `provider: [mcp/built-in]`,
  Apache-2.0 (entity) / MIT (deep-research, task-brief) licence split intact,
  search/fetch worded for the MCP tier.

## v0.7.11 — Research skills knowledge port (deep-research 1.12.0, entity-research 1.1.0)

deep-research 1.8.1 → **1.12.0**, entity-research 0.1.0 → **1.1.0**: ported the
current deep-research engine (iterative loop moved to `references/step3-iterative-loop.md`,
quote anchoring + `check-quote`, search `estimate-budget`, report revision via
`map-citations`, search/fetch fallback page, firmware-status recipe, referent
disambiguation checks, provider benchmark page) and the entity-research Exa
organisation-evidence option (`references/organisation-evidence-exa.md`).
Provider references generified (no operator-specific names). The plugin-owned
contracts are unchanged: research run directories stay under
`<project_root>/research/`, the Apache-2.0/MIT licence split and the
`mm`-not-million scoring doc fix survive, and all gate-contract tests pass (121).


## v0.7.10 — Composio Google mail.list_folders + contacts refusal copy

Wire Composio Google-family **`mail.list_folders`** as an alias over
`GMAIL_LIST_LABELS` (same label `{id, name, type}` records as `mail.list_tags`;
destination for `mail.move` remains a label id). Operator-facing **contacts**
refusal text for Composio now explains Gmail vs googlecontacts toolkit setup.

### Changes

- `FAMILY_SLUGS["google"]["mail_list_folders"]` → `GMAIL_LIST_LABELS`.
- `ComposioMCPWorkspaceClient.mail_list_folders`: Google reuses the
  `mail_list_tags` normalization path; Microsoft unchanged.
- Capabilities: `mail.list_folders` True for `composio` and `composio:mcp`;
  stays False on `google_api`.
- `_CONTACTS_REASONS["composio"]`: self-serve fix (connect **googlecontacts**
  in Composio dashboard; `google_api` alternate). No contacts CRUD wiring.


## v0.7.9 — Composio Google calendar.cancel + mail.move

Wire Composio Google-family **soft** `calendar.cancel` (via
`GOOGLECALENDAR_UPDATE_EVENT` with `status=cancelled`, matching `google_api`)
and **label-id** `mail.move` (`GMAIL_BATCH_MODIFY_MESSAGES`). Hard delete is
separate: `calendar.delete` → `GOOGLECALENDAR_DELETE_EVENT` (approval-gated).
`mail.list_folders` stays False on Google.

### Changes

- `FAMILY_SLUGS["google"]`: `mail_move`, `mail_modify_thread_labels`,
  `calendar_delete`, `calendar_batch`.
- `ComposioMCPWorkspaceClient`: `calendar_cancel` / `calendar_uncancel`,
  Google `mail_move_to_folder`, `calendar_delete` for Google (not MS-only).
- Capabilities: `calendar.cancel`, `calendar.uncancel`, and `mail.move` True for
  `composio` and `composio:mcp`; removed `UNSUPPORTED_REASONS` for Google
  `calendar.cancel`.
- Gmail `mail.move` removes `INBOX` when filing to a user label and returns
  `undo_add_label_ids` / `undo_remove_label_ids` for a symmetric batch-modify undo.


## v0.7.8 — Field follow-up (cron bindings, backup CLI, test isolation)

Production follow-up after v0.7.5’s `cron_prompts` doctor and suite sandboxing
(Hermes Agent v0.21.5, headless daily crons, plugin v0.5.7 → v0.7.7).

### Changes

- **Upgrade: bind scheduled work through workflows, not freeform cron paths.**
  Cron jobs keep their own prompt text; hardcoding plugin script paths or
  `chief-of-staff:…` skill refs breaks across upgrades when files move or the
  skill is agent-scope. The supported path is `chief_of_staff.py workflows
  install` (writes `skills.local/` + optional cron binding) and the v0.7.5
  `cron_prompts` doctor check. Re-run `doctor --summary` after upgrading and
  fix any flagged jobs; doctor does not edit cron jobs. See `docs/SETUP.md`
  (Upgrading).
- **`cron_prompts` agent-scope hint.** Unknown `chief-of-staff:<name>` skills
  now say explicitly that **agent-scope skills take no plugin prefix** (reference
  them without `chief-of-staff:`).
- **Backup skill matches the google-workspace `google_api.py` CLI.** Hermes
  v0.21.x drops `--account` / `--as`; Drive upload is positional file +
  `--parent`, folder listing uses `drive search … --raw-query`, and delete passes
  the file id positionally. The skill prefers the google-workspace skill venv
  Python when present. When the connector surface changes, plugin skill updates
  ship in the same release. Contract tests in
  `tests/test_backup_google_api_v078.py` stub the CLI via
  `tests/fixtures/google_api/backup_drive.py`.
- **Hermes home isolation in tests.** `doctor`’s google-workspace skill check,
  `google_client`, and briefing/calendar `google_api_script()` helpers now
  resolve through `get_hermes_home()` / `HERMES_HOME` instead of
  `Path.home() / ".hermes"`. Regression tests prove an install under the real
  home is not visible when the suite sandbox env is set.

## v0.7.7 — Contacts writes that match the install

Field report on v0.7.6 (provider `google_api`, service account with
domain-wide delegation). v0.7.1's contacts writes shelled out to
`google_api.py contacts create|update|delete`, but the shipped
google-workspace skill's script offers only `contacts list`. `doctor` was
green and the approval chain ran, then the action failed at the last step
with `invalid choice: 'create' (choose from 'list')`. The v0.7.1 and v0.7.3
tests mocked the subprocess, so none of them ever checked the real CLI
surface.

> Touches the google_api provider's contacts writes, `require_capability`,
> `review_queue approve`, the capability report and the doctor.

### Changes

- **Contacts writes go through the People API.** When
  `google.service_account_path` and `google.delegate_email` are set, create,
  update and delete call the People API directly (scope
  `https://www.googleapis.com/auth/contacts`). This is the same service
  account path that drafts and calendar events already use, so the external
  skill is no longer needed for writes. Update is merge-safe: only the
  supplied field types change. `email`, `phone` and `organization` replace
  the first entry of their type and keep the rest (and the entry's type or
  title). The new `emails` field replaces the whole address list.
- **The installed script's surface is probed.** `google_api.py contacts
  --help` runs with no credentials, and the result is cached per script
  version. Without a service account, a write runs through the CLI only if
  the script has that subcommand. A `--help` the probe cannot read is not
  treated as proof of absence, so the CLI is still tried.
- **An install that cannot do the write refuses it up front**, with the
  reason (which subcommand is missing, what the script offers, and how to
  enable the People API path):
  - `capabilities` lists the write as unsupported and reports
    `contacts_write_backend` (`rest`, `cli` or none) per action.
  - `doctor` has a new `google_contacts` check. It warns when contacts
    writes are impossible and otherwise says which path performs them.
  - `review_queue.py approve` refuses to approve an action this install
    positively cannot execute. This applies only to install-dependent gaps;
    anything uncertain approves as before.
  - `execute` refuses before any subprocess runs, and records the reason as
    `last_error`, not a generic "not supported".
- **Several email addresses per contact.** `contacts.create` and
  `contacts.update` payloads take `emails: [...]`, alongside `email`. The
  CLI path takes one `--email`, so several addresses need the People API
  path and are refused otherwise.
- **Contract tests against a real subprocess.**
  `tests/fixtures/google_api/` holds two argparse stand-ins: one for the
  shipped skill (`contacts list` only) and one with the full set. The end
  to end tests run them for real. When the google-workspace skill is
  installed, a contract test also runs the operator's real `google_api.py
  contacts --help`. It is read-only and needs no credentials.
- **Test isolation covers Google credentials.** The autouse fixture from
  v0.7.5 now also unsets `GOOGLE_SERVICE_ACCOUNT_PATH` and
  `GOOGLE_WORKSPACE_API`. Otherwise the People API path's env fallback could
  let a contacts test reach the operator's live account.
- `plugin.yaml` documents the google-workspace dependency surface next to
  `optional_skills`. The runtime probe enforces it, because a structured
  manifest there would need Hermes' plugin loader to understand it.
- `docs/SETUP.md` explains the contacts scope and the two write paths.

## v0.7.6 — Generated-section attribution

The briefing archive carried generated content that was rewritten on every
run: operator notes wedged between generated sections disappeared, and two
producers (the agent skill path and the Python CLI) could clobber each other.
This release makes generated sections first-class: each is fenced with
`<!-- cos:generated <id> start|end -->` markers and a per-span content hash,
merged instead of overwritten, and attributed in the audit log.

> Touches the briefing archive writer (`briefing_attribution.py`, new), the
> daily-briefing skill text, the doctor, the markdown renderer, and
> `cmd_run`. Adds 104 contract tests.

- Archive model: `briefing.md` becomes a mixed document. Operator text is
  never inside a generated span. Generated spans carry a 12-hex hash of the
  body, updated through a managed path on merge; a span whose hash does not
  match its content is refused, not overwritten.
- Locking and atomicity: every merge takes an advisory lock
  (`.cos-briefing.lock`) and writes via `os.link`. Failed merges keep the
  archive untouched; the pre-merge bytes are backed up to `.cos-backups/`
  on structural failure.
- Two producer paths, one helper: the agent path composes an envelope JSON,
  writes it under `.cos-tmp/`, and calls the helper CLI; the Python CLI
  (`cmd_run --markdown`) calls `merge(envelope=...)` in memory and stages
  nothing. Both go through the same validation, same lock, same audit log.
- Injection hardening: single-line fields pass `sanitize_scalar` on the
  archive path, so a forged marker inside a calendar title (CR/LF/U+2028/
  U+2029 collapsed) cannot freeze the archive. Marker lines are matched
  whole-line; partial matches refuse.
- Doctor: `briefing_archive` check reports staleness from SUCCESSFUL merges
  only (36 h threshold); failed runs surface as a warning, never reset the
  timer. Malformed audit lines are skipped and counted.
- Renderer: `render_markdown` is now a join over `render_markdown_sections`;
  delivery output is byte-identical to the pre-refactor renderer (pinned by
  captured goldens). Weekly briefings do not archive.
- Skill adoption: `daily-briefing/SKILL.md` documents the envelope flow with
  a heredoc snippet; git-backed installs ignore `.cos-backups/`, `.cos-tmp/`
  and `.cos-briefing.lock` (including the `state_sync` backfill).
- Spec adjudications recorded in
  `.hermes/specs/2026-09-26_generated-section-attribution.md`: batch-2
  containment/fence-indent/C-2 window/audit-`reason`/undeclared-id notes and
  slice-2 notes (in-memory envelope, registry span order, C-3 known-but-empty
  lifecycle, unmapped-block attribution cases).

- Version 0.7.4 → 0.7.6 in all six locations. v0.7.5 shipped separately
  (Claude Code session, test isolation + cron_prompts doctor check).

## v0.7.5 — Test isolation and stale cron prompts

Two findings from field use of v0.5.7 through v0.7.4 on a production-like
install running headless crons.

### Changes

- **The test suite can no longer see the operator's state.** Tests that
  called state code without a config (`StateDB(None)`) fell back to
  `CHIEF_OF_STAFF_PROJECT_ROOT` or `load_config()`, and on a real install that
  is the operator's live `state.db`. `test_build_trends_section_never_raises`
  failed when that database held trend snapshots. On an empty root it passed
  but created a `state.db` there. A new autouse fixture in `tests/conftest.py`
  unsets `CHIEF_OF_STAFF_PROJECT_ROOT`, points `CHIEF_OF_STAFF_CONFIG` at a
  missing file and gives every test an empty Hermes home. Every test now sees
  what a clean CI checkout sees. The full suite leaves a seeded operator root,
  its Hermes cron store and a real `shared/config/company.yaml` byte-for-byte
  unchanged.
- **New doctor check: `cron_prompts`.** Cron jobs carry their own prompt
  text, so after an upgrade moves a script or a prompt names a skill the
  plugin never shipped, the scheduled run breaks silently. `cron_jobs` and
  `cron_skill_files` stayed green because neither reads the prompts of the
  user's own jobs. `cron_prompts` reads `$HERMES_HOME/cron/jobs.json` and
  warns about:
  - plugin script paths that no longer exist. It names where the script
    lives now.
  - `chief-of-staff:<name>` skills that are not plugin skills. It suggests
    the closest real skill, or dropping the prefix for an agent-scope skill.

  The check ignores the user's own scripts, bare script names and paths
  relative to a skill the job loads. It is read-only, `--fix` included, and
  it reports only the offending reference, never a prompt body.
- `CLAUDE.md` documents how cron prompts should reference plugin paths and
  skills. `CONTRIBUTING.md` documents the isolation fixture.

## v0.7.4 — Approval integrity

An independent assessment of v0.7.3 found that the safety core held, but the
edges around approval did not. Approvals were not tied to what was approved,
one executor sent email for any approved action, a repair step could send an
email twice, and the cloud-session refusal existed only in a report. This
release closes those gaps. It also includes the follow-up to PR #27.

> Touches the pending-action state machine, the guardrail, every executor
> and the workspace client factory.

### Changes

- **Approval is bound to what was approved.** Approving now stores a SHA-256
  of the action's type, target and payload, in a new `approval_hash` column
  that is added automatically. Before execution, `claim`/`execute` refuse,
  and audit as `blocked`, any action whose content changed after approval.
  They also refuse any action that was never approved through the queue (for
  example a row inserted into `state.db` as `approved`). A retry after a
  provider failure keeps the original approval. Actions approved before
  v0.7.4 have no hash: cancel them and queue them again (approvals lapse
  after 24h in any case).
- **`send_email.py execute` only sends email.** It checks for `gmail.send` /
  `mail.send` before claiming, so an action approved as, say, a label can no
  longer become a send. A failed send is recorded as a failure, not as
  executed.
- **A stale claim is never re-armed.** `doctor --fix` and `state_tools`
  repair now close an action stuck in `executing` as `failed`, with a
  "reconcile manually" note. They used to reset it to `approved`, which could
  send an email a second time if the first send had happened without being
  recorded. The two tests that pinned the old behaviour are updated to match.
- **Executors only run a claim they won.** The pipeline and `cron.create`
  executors used to proceed on an action another process had already claimed.
- **The break-glass switches are operator-only and visible.**
  - `.env` can no longer set `CHIEF_OF_STAFF_AUTO_APPROVE` or
    `CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE`.
  - `capabilities` reports their live values (`break_glass`, and "Approval
    gates … BYPASSED by …" in `--summary`).
  - `doctor` warns while either one is on.
- **Cloud sessions really refuse credential providers.** `get_workspace_client`
  raises `HostedSessionRefusal` for `google_api`, `m365` and `composio` when
  `CLAUDE_CODE_REMOTE_SESSION_ID` is set. Before, only `capabilities`
  reported the refusal. `daily`, `readiness` and `doctor` show the reason
  instead of crashing.
- **The audit tamper test can fail.** It swallowed its own `AssertionError`,
  so a verifier that ignored hash mismatches still passed. That mutation now
  fails it.
- **Schema race fixed:** two processes opening a fresh `state.db` no longer
  race to add a column.
- **PR #27 follow-up** (deep-research 1.8.0 → 1.8.1):
  - Tests now pin the three PR #27 gate fixes that shipped untested: `mm` is
    not "million", a blank `--refute-none` is ignored, and legacy quality
    values count as `secondary`.
  - `references/validation-gates.md` no longer says `mm` = `m`, as the v0.7.2
    entry below does. It also documents that each `[VERIFIED]` host needs its
    own supporting snippet, so paraphrased corroboration scores low, and that
    counter-evidence claims must be evidence-backed.
- Version 0.7.3 → 0.7.4 in all six locations.

## v0.7.3 — Contacts follow-ups

A review of v0.7.1 found that approved contacts actions could not execute,
that one documented safety gate did not exist, and that the tests would not
notice if the guardrail classification changed.

> Touches the guardrail: `contacts.update` moves into `DESTRUCTIVE_ACTIONS`.

### Changes

- **Approved contacts actions now execute.** The review-queue execute router
  (`webhook_events.py execute`) gains `contacts.create`, `contacts.update`
  and `contacts.delete` branches. Before, an approved contacts action was
  marked failed with "Unknown action type" and the provider was never called.
  `person_id` falls back to the action's target.
- **`contacts.update` really is gated under auto-approve.** v0.7.1 said it
  was, but a non-interactive run with `CHIEF_OF_STAFF_AUTO_APPROVE=1`
  overwrote the contact. It now needs the destructive dual gate
  (`AUTO_APPROVE` + `ALLOW_DESTRUCTIVE`), like `contacts.delete`. The
  approved-execute path sets both, so queued updates still run.
- **The guardrail classification is pinned by tests.** Set membership is
  asserted, and the real `confirm_action` runs across every flag combination
  in a non-interactive session. A blocked attempt is shown to be audited as
  `blocked`. The v0.7.1 tests replaced `confirm_action` with a stub, so
  reclassifying `contacts.delete` as a read passed the whole suite; six
  tests now fail on that change.
- **Refusals explain themselves.** composio, composio:mcp,
  composio_microsoft(:mcp), m365 and agent get a specific reason for every
  `contacts.*` action, and the recommendation is `google_api`. It used to
  point at `composio`, which also refuses.
- **The preview states reversibility.** `review_queue.py preview` says a
  contact delete is permanent with no undo, and that an update keeps no
  prior values. An update's preview names the fields, not the new values.
  Risk explanations now cover `contacts.*`.
- **Test fixtures use placeholder values** instead of a real work address
  and domain.
- Version 0.7.2 → 0.7.3 in all six locations.

Known, not changed here: `capabilities` reports google_api contacts as
supported whether or not the installed `google_api.py` has the `contacts`
subcommands. A write against an older script fails closed.

## v0.7.2 — Deep-research gate hardening

A review of deep-research after v0.7.0 found gates that could be fooled,
rules the skill states but never checked, and routing to skills this plugin
does not ship. deep-research 1.7.9 → 1.8.0.

### Changes

- **`validate-report` can no longer be fooled by look-alike headings.** A
  required section is now a `##` heading that *starts with* its name.
  Before, `### Sources of revenue` counted as the Sources table and
  `## Mind the Gaps` as the Gaps section. The evidence-key check used to be
  skipped when there was no real `## Sources`; a missing Sources section now
  also fails that check.
- **`verify-citations`** no longer treats a bracketed year (`fiscal [2024]`)
  as a citation, unless the Sources list really has an entry with that
  number.
- **The evidence store validates its enumerations.** `quality`
  (primary / secondary / tertiary), `polarity` (support / refute / neutral)
  and the new optional `basis` (verified / sourced / reasoned / estimated)
  are case-normalised. Typos are rejected instead of silently dropping out
  of the counts.
- **Figure matching** normalises magnitude abbreviations: `2.4bn` =
  `2.4b` = `2.4 billion`, `mn`/`mln`/`mm` = `m`, `tn`/`trn` = `t`. A
  correctly sourced "$2.4 billion" no longer fails against "US$2.4bn".
- **`verify-claims` checks three rules the skill only stated.** Each is a
  warning by default and a failure under `--strict`:
  - A `basis: verified` claim needs sources on ≥2 different hosts.
  - A store with 5+ sources needs at least one `polarity: refute` claim, or
    `--refute-none "<what was searched>"`, which is recorded in the manifest.
  - The source-quality mix is reported as `source_quality` with a
    healthy / acceptable / weak rating. `weak` is a warning only, never a
    failure.
- **Routing matches this plugin.**
  - `related_skills` and "When NOT to use" no longer point at
    `notebooklm-mode`, `fact-checker`, `source-tracker` or
    `youtube-topic-research`, which are not shipped here.
  - A single claim to fact-check stays in deep-research's fact-check
    category. Q&A over user-supplied documents needs no skill.
  - New `tests/test_routing_fixtures.py` keeps every
    `skills/*/evals/routing-fixtures.json` pointed at shipped skills. The
    skill already cited this test, but it did not exist.
- **Research is saved with the user's data.** The run folder is now
  `<project_root>/research/<YYYY-MM-DD>-<topic-slug>/`, holding `report.md`
  and the evidence store. Under git storage, `sync push` carries it across
  cloud sessions. This replaces the `notebooklm-mode` vault section.
- **Consistency fixes.**
  - `init-run --mode` uses the skill's simple / moderate / complex
    vocabulary; the default is now `moderate`.
  - The gates reference no longer calls the plugin MIT-licensed. The skill
    is MIT per NOTICE; the plugin is Apache-2.0.
- Version 0.7.1 → 0.7.2 in all six locations.

## v0.7.1 — Contacts on the workspace client

Adds Google Contacts (People API) to the provider-neutral workspace layer,
following the same architecture as mail/calendar/files: common interface,
per-provider connector, capability matrix, guardrail registration.

### Changes

- **`WorkspaceClient`** gains four neutral methods: `contacts_list` (read),
  `contacts_create`, `contacts_update` (merge-safe), `contacts_delete`
  (permanent). Unimplemented providers raise `NotImplementedError`.
- **Google connector** (`GoogleWorkspaceClient`) implements all four via the
  `google_api.py` contacts subcommands. Writes are `@guarded` — approval gate,
  audit, `ActionResult`.
- **Guardrails**: `contacts.list` → READ_ACTIONS; `contacts.create/update/delete`
  → WRITE_ACTIONS; `contacts.create` → SAFE_WRITE_ACTIONS (creates, destroys
  nothing); `contacts.update` stays approval-queued (NOT SAFE_WRITE — a
  supplied field replaces the existing value with no prior-value record to
  reverse from); `contacts.delete` → DESTRUCTIVE_ACTIONS (permanent, no trash).
- **Risk model**: `contacts.delete` high; create/update medium; list low.
- **Capability matrix**: `google_api` all four True (live-verified 2026-09-25
  against the Phronesis Workspace account). `composio`/`composio:mcp` explicit
  False with a not-wired reason — the GOOGLECONTACTS toolkit needs a BYO OAuth
  client and flips True only after a live run (tripwire convention).
- **Docs**: `SETUP.md` gains the Google Contacts Composio setup (BYO OAuth
  client, redirect URI, consent screen, scope) and the DWD direct link for the
  google_api path.


## v0.7.0 — Claude Code on the web

Make the plugin usable from an ephemeral Claude Code cloud session, where the
repo is cloned fresh and everything under `project_root` is lost at teardown.
See [`docs/CLAUDE_CODE.md`](docs/CLAUDE_CODE.md).

### Changes

- **`chief_of_staff.py sync status|pull|push`** (`state_sync.py`). Makes
  `project_root` a clone of a *private* data repo and commits and pushes it on
  request. It refuses a root inside the plugin checkout or a remote equal to
  the plugin's own, since the plugin repo may be public. It never commits
  `.env`, SQLite sidecars or `.runs/`, and refuses a repo that already tracks
  `.env`. It WAL-checkpoints `state.db` before commit. Pull is fast-forward
  only, and a dirty or diverged tree is refused rather than merged. `push`
  warns about actions still `executing`. Remote URLs are credential-redacted.
- **`.claude/settings.json` hooks.** `session-start.sh` builds `.venv`, clones
  or fast-forwards the data repo (`CHIEF_OF_STAFF_DATA_REPO`), links
  `shared/config/*.yaml` into `<data>/config/`, and exports
  `CHIEF_OF_STAFF_PROJECT_ROOT`. `stop-sync-check.sh` blocks the session from
  ending with unsynced state. Both hooks are no-ops outside
  `CLAUDE_CODE_REMOTE=true`.
- **`capabilities`** reports `state_sync`. A hosted session whose
  `project_root` is a syncable git clone is now `state_persistent: true`, with
  a note saying it survives only once pushed.
- **Test suite runs green inside a cloud session.** An autouse fixture clears
  `CLAUDE_CODE_REMOTE_SESSION_ID`. Before this, 114 tests failed when pytest
  inherited it from the session.
- **Storage is chosen at onboarding, and git is opt-in.**
  `bootstrap.py --storage local|git [--data-repo owner/name]` and a new
  `onboard.py` "Data storage" step record `storage.mode`. Choosing `git`
  clones the data repo into `project_root`, or initialises one with no
  remote, before any store is seeded. It refuses a root inside the plugin
  checkout and never clones over existing files. `local` is honoured
  afterwards: `sync pull`/`push` refuse, and `capabilities` never reports it
  as git-durable. Bootstrap without `--storage` is unchanged.
- `.venv/` is gitignored.

## v0.6.1 — Deep-research validation gates

The deep-research skill could deliver reports whose factual claims were not
backed by the evidence gathered during the run: citations could point at rows
that said nothing relevant, structure drift slipped past prompt discipline,
and everything lived in context — lost on compaction. v0.6.1 adds a
deterministic, stdlib-only validation layer and an on-disk evidence store.

### Changes

- **`research_validation.py` (new, `skills/deep-research/scripts/`).** Four
  subcommands: an evidence store (`init-run` / `register-source` /
  `add-claim` / `add-evidence` — append-only `claims.jsonl` + run manifest,
  sha256 source IDs, URL-canonicalized dedup), `verify-claims` (deterministic
  token/number/year/entity overlap scoring with figure and year contradiction
  caps, occurrence-level year boundaries, `--strict` exit 1 on unsupported
  factual claims), `verify-citations` (inline `[N]` ↔ Sources-row
  reconciliation, URL well-formedness, generic-title and citation-range
  flags), and `validate-report` (required sections incl. Contradictions and
  Gaps, placeholder/truncation detection, stats block, evidence-key legend).
- **SKILL.md 1.6.0 → 1.7.9.** Step 5.5 validation gates (mandatory for 5+
  source reports, 3-cycle fix loop), evidence persistence §3b.1, provider
  preference block, provider in the stats line. Full gates contract moved to
  `references/validation-gates.md`; SKILL.md stays under the 450-line
  contract (448).
- **Tests: `tests/test_deep_research_gates.py`, 78 tests** — store CRUD,
  dedup, support scoring (incl. the occurrence-level year/figure boundary
  cases from review rounds 3-5), citation and structure gates.

Review: Codex confirm loop 5 rounds (findings 3→5→1→1→0, SHIP) + Claude Opus
net review (SHIP). Suite: 2577 passed. Ruff clean.

## v0.6.0 — Workflow Orchestrator

Capture a repeating business process as declarative YAML and run it under the
same approval gates as every other capability. This is the feature release for
the Workflow Orchestrator spec (2026-09-20): six build batches, a live smoke
run, and a three-round review chain (r1 4 MAJOR + 1 minor → r2 cross-thread
race + fractional exit codes → r3 SHIP).

### Changes

- **`workflow-architect` skill (US-1).** A structured interview captures a
  repeating business process as `workflows/<name>.yaml`. Nothing is written
  until the operator confirms the draft; invalid drafts are never written;
  regeneration is deterministic (identical YAML → byte-identical file).
- **`workflows` CLI (US-2/US-3/US-6/US-11).** `list`, `runs`, `start`,
  `advance`, `sync`, `resume`, `abort`, `install`, `uninstall`,
  `bind-action`, `refresh-facts`, `generate-skill`. Install validates the
  YAML, writes a `skills.local/<name>/SKILL.md` overlay (never touching the
  bundled `skills/` tree), refuses to shadow a bundled skill or an
  operator-edited overlay, and — for scheduled workflows — proposes a
  `cron.create` review-queue action.
- **Runtime (US-2).** Cron-fired runs advance via two nonblocking hooks
  (post-tool-call completion matching with strict success evidence: exact
  integer exit code 0 or boolean `success: True`; pointer strips in the
  daily briefing surface active runs). Hook database access uses a scoped
  connection factory with a short busy timeout — no process-global patching,
  no cross-thread state leak, no blocking on a locked database.
- **Approval-gated steps.** A `review_queue` step proposes an action and the
  run pauses until the operator approves and executes in the review queue;
  unapproved cron registration is refused.
- **Optional steps degrade, never strand.** A skipped optional step is
  recorded and the run continues; the pointer strip reports DEGRADED, and
  every advancement path (hook, manual advance, review-queue observation)
  drives one shared helper.
- **Registration completeness (post-review fix).** The plugin entry point's
  skill registration now includes the `skills.local/` overlay discovery pass
  (matching doctor), so installed workflow skills are invocable via normal
  skill loading, and `workflow-architect` joins both plugin.yaml profiles.
- **New modules.** `workflows.py` (pure validator/generator),
  `workflow_runs.py`, `workflow_hooks.py`, `workflow_install.py`,
  `workflows_cli.py` (parser attach), `readiness_render.py` (extracted
  render layer); `chief_of_staff.py` stays under the 2500-line contract.

### Evidence

- 2499 tests passed (suite grew by 3: overlay-discovery contract tests),
  ruff clean, CI green on Python 3.11 + 3.12.
- Live smoke run on a real install: invoice-nudge workflow driven end-to-end,
  stopping at the real review-queue approval gate.

## v0.5.7 — daily + weekly briefing trends, weekly HTML report

Adds operational trend visualization to the daily HTML briefing (pure-CSS bars,
no JavaScript, self-contained) and an HTML output for the weekly review.
Trend snapshots accumulate in the existing `kv_stores` table (no schema
migration), pruned to 90 days, daily+weekly kinds kept separate.

### Changes

- **Settled currency series reach 0.** When a later daily snapshot omits a
  previously seen `bookkeeper.outstanding_ar::CCY` / `outstanding_ap::CCY`
  child, trend history appends a synthetic `{value: 0, carried: true}` point
  at that snapshot's timestamp. Settlement requires the latest snapshot to
  contain at least one `bookkeeper.*` counter (a degraded run with only
  summary scalars is "no bookkeeper data", not "collected as zero") and
  skips any metric listed in that snapshot's `dropped` list (ambiguous
  flatten ≠ settled). A missing later snapshot is "no data yet" (series
  stops). Scalar metrics such as `overdue_count` are not carried forward.
  No snapshot schema migration: the extra point is computed at series time.
  A non-numeric `::CCY` value is treated as missing and would therefore
  settle; current collectors only emit numeric flatten values, so this is
  unreachable without a hand-edited store.
- **Dropped-key disclosure.** Ambiguous flatten destinations still drop (and
  still `note_exception`); the dest list is now stored as `snapshot["dropped"]`
  when non-empty. `build_trends_section` surfaces `dropped_count` as the
  number of distinct dest keys on in-window **daily** snapshots (same kind
  filter as the series). `render_trends_html` emits that count as a small
  chip when present.
- **Daily AR/AP uses `_load_records`.** `collect_bookkeeper_stats` no longer
  `yaml.safe_load`s `invoices.yaml` itself. Malformed YAML falls through to
  the store with a `sources` disclosure; the bare except notes via
  `note_exception`. The already-resolved project root (env fallback included)
  is passed into `_load_records` so daily and weekly no longer disagree on
  where `invoices.yaml` lives. Daily disclosure stays on
  `sections.bookkeeper.sources` (not hoisted to top-level `briefing.sources`,
  which weekly uses and which the other daily path uses for wrap_source
  hashes); daily HTML/text/markdown read that key. Weekly is unchanged:
  top-level `briefing.sources`.
- **Bucket formatting:** `_amt(-0.001)` renders `0`, not `-0`. Empty-bucket
  `"0"` vs `pl_report`'s `"0.00"` is unchanged.
- **`_DONE_STATUSES`** is the hard set `{done}` (aliases still map
  `completed` → `done`).

### Documented, not changed

- **Daily vs weekly Outstanding AR.** Daily / `chief_of_staff.py bookkeeper`
  (and `pl_report`) count commitments including drafts. Weekly Outstanding AR
  is issued-only (drafts skipped, attested by the r2 weekly fixture). Same
  `invoices.yaml` can print SGD 1208 daily and SGD 1200 weekly; the split is
  intentional. See `skills/weekly-review/SKILL.md`.
- **`overdue_count` on the daily bookkeeper panel** sums past-due sent and
  received (and counts drafts). `pl_report` keeps `overdue_ar` / `overdue_ap`
  separate. Pre-existing; the panel label is just "Overdue count".
- **`outstanding_unknown`** (direction-less invoices in the weekly JSON) is
  not rendered in weekly HTML/text. It remains on the envelope.
- **String amounts.** Weekly `_amount` returns `None` for `amount: "1200"`;
  `pl_report.money()` parses via Decimal. Pre-existing, inherited by daily
  through the shared helper.
- **Weekly `render_markdown`** is `render_weekly_text`. Two-space-indented
  lines can reflow in clients that treat the body as markdown; content is
  correct.
- **Mixed-shape trend history.** Pre-R4 snapshots may still chart the scalar
  `bookkeeper.outstanding_ar` next to the new `::CCY` rows. Currency children
  now settle to 0 (above); scalar series that simply stop are unchanged.

## v0.5.6 — field follow-up #3: unified calendar reads, multi-account routing, status fixes

Field follow-up #3 from the live deployment (Battery Road Collective,
composio:mcp provider, Raspberry Pi 5, Asia/Singapore). Three provider fixes
and one doc hardening, ported from the operator's tested local batch.

### Changes

- **calendar_list: unified endpoint + normalizer + tz-correct windows**
  (composio:mcp provider). `FAMILY_SLUGS["google"]["calendar_list"]` moved from
  the legacy `GOOGLECALENDAR_FIND_EVENT` (intermittent nested
  `event_data.event_data` payloads in scheduled runs) to
  `GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS`. New normalizer branch unwraps
  `[{event, source_calendar_id, source_calendar_summary}]` and surfaces
  per-item `source_calendar_*` so multi-calendar reads stay attributable.
  Timezone fix: windows are now built with an explicit offset from
  `delivery.timezone` (`+08:00` for an SG workspace) instead of hardcoded
  `T00:00:00Z`/`T23:59:59Z`, which Google interprets as UTC — an all-day SGT
  window previously read events ending 07:59 SGT the next day. Stale contract
  tests updated; debug `tool_map` in `connect_workspace.py` follows the new
  slug.
- **Per-tool account routing.** Workspaces with more than one connection per
  toolkit can pin the connection: `integrations.workspace.account_aliases`
  (already documented in `company.yaml.example`) is now read and passed as
  `tool["account"]` on `COMPOSIO_MULTI_EXECUTE_TOOL` entries. Routing is
  family-scoped, resolves operation → toolkit via a prefix map with a
  per-operation override table (`files_recycle_*` → `share_point`), and never
  attaches a foreign-family alias. Single-account installs are unchanged
  (no alias configured → no `account` key).
- **Connection status: action `list` + casing normalization.**
  `refresh_connection_statuses()` calls `COMPOSIO_MANAGE_CONNECTIONS` with
  `action: "list"` (`status` is not a supported action). Account status
  comparison is now case/whitespace-insensitive via the new
  `_status_is_active` helper, shared with `connect_workspace.py`'s
  existing-accounts icon. Malformed or missing response envelopes now read as
  `unknown` instead of silently `pending`.
- **weekly-review: bounded Drive queries.** `skills/weekly-review/SKILL.md`
  documents metadata-only, folder- and time-scoped Drive queries (page size
  50–100, `trashed=false`, `createdTime`/`modifiedTime` windows, exact
  pagination, union-dedupe by file ID) to avoid the Composio inline-payload
  offload failure mode for inventory reads outside the provider's pinned
  `files_search`.
- Mixed Google+Microsoft toolkit lists without an explicit `family` now
  resolve to `google` (a stray outlook entry can no longer flip the family).
- Tests: 20 contract tests in `tests/test_composio_field_followup3.py`
  (slug/windows/normalizer, account routing incl. family-guard and
  SharePoint override, status action + casing + malformed envelopes); stale
  calendar contract tests updated. Suite: 2138 passed.

### Upstream shape notes (from the field)

- Keep a per-family calendar_list slug table; derive the window offset from
  the configured workspace timezone instead of hardcoding `Z` or `+08:00`
  (implemented: `delivery.timezone`).
- Promote `account_aliases` to a documented config key — it is the only
  deterministic way to pin multi-connection workspaces (already present in
  `company.yaml.example`; now honored by the provider).
- Use `list` everywhere for connection discovery and normalize status casing
  at the adapter boundary (one helper: `_status_is_active`).

## v0.5.5 — note-taker retrieval activation guidance

Doc-only follow-up from wiki-routing research on the live deployment. A wiki
query only happens if the agent first knows to reach for the wiki — and the
wiki is pull-only: nothing injects it into context (Hermes has no
CLAUDE.md/AGENTS.md always-on router for `$HERMES_HOME`).

### Changes

- **note-taker 0.2.1 — new "Retrieval Activation" section** in Operation 2
  (Query). Explains the activation problem, gives a persistent-memory pointer
  template with explicit trigger conditions (comparisons, decisions, lessons,
  client/vendor facts, domain research — a bare path reference gets
  skipped), and a recommendation ladder:
  1. memory pointer by default (~30 tokens, no machinery);
  2. plugin hook escalation only when the agent is observed re-deriving
     wiki-held knowledge twice or more in a month (bounded system-prompt
     section before per-turn `pre_llm_call` injection);
  3. never route from identity files (`SOUL.md` holds identity, not
     infrastructure routing that changes with the tool landscape).
  Adds a scheduled/headless caveat: cron runs often skip persistent memory,
  so the job prompt must name the wiki query step explicitly.
- No code changes; suite unchanged.

## v0.5.4 — Wiki bootstrap/lint contract + headless-scheduler hardening

Field follow-up #2 from the live deployment (Battery Road Collective,
composio:mcp provider, Raspberry Pi 5). Two operator-reported items, one bug
and one doc gap, plus a scheduled-run orientation note. Suite at 2118 passing.

### Changes

- **Wiki seeds now pass wiki_curator lint out of the box** — a fresh
  `initialize_wiki()` seeded `purpose.md` and `SCHEMA.md` with no YAML
  frontmatter and no `index.md`, while `wiki_curator.py lint` treats all three
  as ERRORs. Result: every fresh install started at 3 lint ERRORs and daily
  briefings showed `wiki: error=3` until the operator hand-fixed the seeds.
  Both templates now emit frontmatter (`title`, `type: reference`, `updated`)
  and `initialize_wiki()` seeds an `index.md` (`type: index`) listing both
  seed pages, in the same shape `wiki_curator refresh` writes. Titles are
  serialized with `json.dumps(ensure_ascii=False)` — a JSON string is valid
  YAML, so company names containing `:`, `#`, quotes, or unicode safely
  survive the round-trip (regression-tested with 5 adversarial names).
  `force=False` semantics unchanged: operator-edited seeds are kept, and the
  existing `Kept existing wiki file` output now also covers `index.md`.
- **`run.sh` stable entrypoint** — new executable POSIX-sh wrapper at the
  plugin root: `./run.sh <args>` execs `.venv/bin/python <args>` with `exec`
  (correct exit-code propagation, args verbatim). If `.venv/bin/python` is
  absent it prints one remediation line to stderr and exits 127 — it
  deliberately does NOT fall back to system Python, which is the failure mode
  that produced false doctor findings.
- **Docs: venv interpreter everywhere agents run commands** — `CLAUDE.md`
  states the rule (run every plugin command with `.venv/bin/python`, not bare
  `python`) and its "Start here" + envelope-path examples use it; 12
  `skills/*/SKILL.md` files converted from bare `python` to
  `.venv/bin/python`. Headless runners (cron, CI) that don't activate the
  venv no longer half-run under system Python.
- **"Scheduled / headless runs" section in `CLAUDE.md`** — a scheduled run
  reads `capabilities` output plus the relevant `skills/<name>/SKILL.md`
  only, and skips doctor/CHANGELOG/full-CLAUDE.md re-reads when
  `capabilities` provider/state lines are unchanged from the last run.
  Addresses ~45s of over-orientation per 5-minute briefing cycle measured on
  the live install.

### Review

Two-lane review (Codex GPT-5.6 Sol + Cursor Grok 4.6-medium; Opus lane
unavailable — CLI OAuth expired). Key findings fixed: unquoted company names
in f-string YAML frontmatter (BLOCKING — reproduced with a colon-bearing
name, fix = JSON-quoted titles + adversarial-name regression tests);
force=False overwrite-risk tested explicitly (operator edits preserved);
`safe_dump` line-wrap regression caught by test before release (`...`
document marker breaking frontmatter) — replaced with `json.dumps`.
Accepted non-goals: pre-0.5.4 wikis seeded without frontmatter still lint
dirty until the operator re-runs onboarding with `--force` or hand-adds
frontmatter (auto-overwriting operator content was judged worse than a
documented one-time fix); remaining bare-`python` mentions in README/
CONTRIBUTING/docs/ are developer-facing, not agent-runtime-facing.

## v0.5.3 — Composio hardening from the field (mail_search + test hermeticity)

Upstream fixes found in a live deployment (composio:mcp provider, Google
family, Raspberry Pi 5 hardware, suite at 2104 passing). With Composio's
default `include_payload=true/verbose=true`, larger `GMAIL_FETCH_EMAILS`
result sets get their body offloaded to `data_preview` — the strict-response
adapter then correctly but opaquely refuses the shape as malformed. Six of
eight `queries.yaml` templates failed live before the fix. Short queries
succeeded, which made it look query-dependent; it was result-size dependent.

### Changes

- **Lean mail_search args (Google family)** — `mail_search` pins
  `include_payload=False, verbose=False` so briefing/triage reads stay in the
  metadata+snippets shape and never trip Composio's offload. Microsoft branch
  unchanged (it has its own `_ms_mail_search_args` contract).
- **Named offload error** — `_execute_composio_tool` now inspects the full
  successful response envelope: when `data` is null but `data_preview` is
  non-empty, it raises a distinct error naming the offload with refetch
  guidance, instead of the generic malformed-response message. Defense-in-depth
  check also lives in `_validate_read_payload`. Empty previews keep the
  generic path.
- **Access ladder reorder (`shared/docs/workspace-access.md`)** — agent-side
  Composio reads now go direct to `COMPOSIO_MULTI_EXECUTE_TOOL` with the
  slugs pinned in `tools_allowlist` (batched); `COMPOSIO_SEARCH_TOOLS`
  discovery is fallback only for unknown/renamed slugs. Discovery costs ~54 KB
  of schemas per call and adds a session-ID contract with no benefit when
  slugs are already known. Source-trust note added: a direct Composio read
  that succeeds while `chief_of_staff.py daily` marks the same source
  `unavailable` should trust the records and disclose the adapter gap.
- **Hermetic no-key tests** — the two `connect_workspace.py` "missing-key"
  subprocess tests no longer inherit a production `COMPOSIO_MCP_KEY` from the
  plugin-root `.env` (auto-loaded at startup). They run with a scrubbed
  subprocess environment and a config copy whose `key_env` points at a
  guaranteed-unset variable, with strict assertions on the named variable.

### Review

Red contract tests first (orchestrator), Cursor (Grok 4.6 High) build, then
Codex (GPT-5.6 Sol) review. R1 found 2 MAJOR (offload error unreachable in
production because `data_preview` was discarded before validation; original
no-key tests still non-hermetic) — both fixed and re-reviewed: RESOLVED, zero
new findings. 2107 tests passing, CI green on Python 3.11 + 3.12.

## v0.5.2 — Meet-link creation + follow-up invite email on calendar.create

`calendar.create` under the `google_api` provider now runs entirely through
the service-account REST API: the event is inserted with
`conferenceDataVersion=1` so Google generates a Meet link, and a follow-up
invite email is sent to every attendee with the event details and Meet URL.
Service-account-created calendar events do not reliably email invitations,
so the explicit follow-up is the dependable path — the email lands in the
organizer's Gmail Sent folder and is verified there.

### Changes

- **REST-based insert** — `calendar_create` replaces the `google_api.py` CLI
  subprocess with a direct Calendar API `events.insert` (SA + domain-wide
  delegation), requesting `conferenceDataVersion=1` and reading back
  `hangoutLink`.
- **Follow-up invite email** — after a confirmed insert, a MIME invite
  (event title, time range, Meet link) is sent from the delegate address to
  each attendee via `users.messages.send`. Partial failure (some attendee
  emails delivered, some not) returns a success result carrying the error,
  never raises past the event insert.
- **Fail-fast validation** — attendee and delegate addresses are validated
  with `email.utils.parseaddr` and titles/delegates/attendees are rejected
  when they contain CR/LF (header-injection guard) BEFORE the calendar
  insert happens; naive datetime strings (a `T` with no `Z`/offset) are
  rejected with a clear message; date-only strings keep the existing
  10:00 UTC default.
- **`CalendarInsertError`** — a typed `RuntimeError` subclass raised for all
  insert-path failures (HTTP ≥ 400, credential, network) so callers can
  catch precisely. `calendar_actions.py` catches it and prints a structured
  failure JSON with exit 1 instead of a traceback.
- **Pending-conference guard** — if the insert response reports
  `statusCode: pending` for the Meet-link creation request and no
  `hangoutLink` is present, the invite email is skipped and the result
  records the reason (partial success).
- **Exception propagation without shared state** — the guarded contract
  wrapper re-raises from the result's error prefix, with an
  `action_blocked` passthrough for guardrail refusals; the earlier
  instance-attribute stash design (from mid-development) was removed after
  re-review found it race-prone (two overlapping calls could consume each
  other's stored exception, and a stale error could poison the next call).
- **Tests** — 5 contract tests (Meet link generated, invite sent, partial
  failure, no-attendee skip, 403 propagation) written red-first and kept
  green across every subsequent refactor; 2 stale mocks updated; regression
  test pinning the blocked-then-succeed sequence (no stale error leakage);
  full suite 2104 passing.

### Review

Three Codex (GPT-5.6 Sol) review-fix rounds before ship:

| Round | Findings | Outcome |
|---|---|---|
| R1 | 1 BLOCKING + 7 MAJOR + 3 MINOR | all fixed |
| R2 | 1 MAJOR (shared-mutable exception stash) | removed, regression test added |
| R3 | — | **APPROVE** |

CI green on Python 3.11 + 3.12; installed plugin synced and doctor-clean.

## v0.5.1 — Workspace provider routing in context primer

The CoS `company_context_primer` hook now injects a credential routing line
into every LLM call, scoped to the CoS assistant name and company. This tells
the agent which workspace provider and credential set to use for CoS work,
preventing it from defaulting to a personal OAuth token when the service
account or another provider is the correct one.

### Changes

- **New `_workspace_routing()` function** in `hooks.py` — reads `company.yaml`
  and builds a provider-specific routing hint:
  - `google_api`: prefers explicit `google.account_alias`, falls back to SA
    filename derivation using suffix removal (`removesuffix`-style)
  - `composio`: uses canonical `_resolve_composio_family()` from
    `composio_family.py` (handles `one_drive`, `share_point`, `sharepoint`)
  - `m365`: emits `user_principal` from config
- **Fixed `_cos_skills_loaded()` guard** — when the runtime does not provide
  `loaded_skills` (common case), falls back to `_cos_configured()` which checks
  `company.yaml` for `delegate_email` or `integrations.workspace`. Previously
  returned `False`, suppressing the primer entirely.
- **Routing line scoped** to assistant name + company to prevent bleeding into
  personal-account work. No instance-specific data in code or tests.
- **16 new tests** — explicit alias, filename derivation, hyphenated suffix,
  composio underscore toolkits, `company: null` edge case, delegate-only,
  primer integration, isolated `_cos_configured`, integrations fallback.

### Review

Reviewed by Codex (GPT-5.6 Sol) and Claude Code (Opus) in parallel. All
BLOCKING and MAJOR findings fixed before merge:

- `account_alias` config field now takes precedence over filename derivation
- Composio family inference uses canonical resolver (not duplicated logic)
- `test_cos_configured_true` isolated from gitignored `company.yaml`
- `company: null` handled with Mapping validation
- Filename suffix removal uses `endswith()` + slice, not `.replace()`
- `Mapping` import moved to module level

**Tests:** 58 hook tests pass, 2096 total plugin tests pass.
**CI:** green on push.

## v0.5.0 Beta — SQLite WAL state store, HTML briefing, attachment-to-Drive hook

The first release with transactional state persistence and rich-format
briefings. SQLite WAL replaces four file-based state stores with a single
ACID database. The daily briefing gains an HTML render mode with inline CSS,
risk badges, and calendar event links. A new post-LLM hook suggests filing
email attachments into Google Drive — always with user confirmation, never
auto-uploading.

**Release posture:** beta. The SQLite migration is fully wired and tested
(2084 tests). HTML briefing and the attachment hook are new surfaces with
contract tests but limited production exposure. M365 native remains
code-complete but not live-verified (unchanged from v0.4.0).

### SQLite WAL state store (Phase 5)

- **Full replacement, not a shim.** Four file-based stores
  (`state_store`, `pending_actions`, `event_store`, `webhook_security`)
  replaced by a single SQLite WAL database (`state_db.py`, ~2000 lines).
  All callers updated to the SQLite-native API.
- **Transactional safety.** `mutate_kv()` wraps read-check-write in
  `BEGIN IMMEDIATE` with `SQLITE_BUSY` retry. `transition_action()`
  uses compare-and-swap on state + version columns.
- **Auto-migration.** Legacy JSON/YAML files ingested into SQLite on
  first open, then renamed to `.migrated`. SQLite is the sole source
  of truth; YAML is never read as authoritative state.
- **Strict corruption detection.** Corrupt JSON raises
  `StateCorruptionError` instead of silently becoming an empty store.
- **Lease token replay protection.** `renew_delivery()` requires exact
  token match when a stored token exists.
- **CAS column allowlist.** `_ALLOWED_CAS_COLUMNS` frozenset validates
  column names before SQL interpolation — no injection path.
- **Migration-safe table rebuilds.** `@pragma foreign_keys=off` runner
  directive for safe table rebuilds inside transactions.

### HTML briefing

- **`render_html()`** — self-contained HTML with inline CSS, collapsible
  `<details>` sections, risk badges, dark-mode support. No JavaScript.
- **Calendar event links.** `event_link` field added to event schema
  (Google Calendar `htmlLink` / M365 Graph `webLink`). Separate from
  conference link. `_calendar_summary()` emits `start`/`end`/
  `event_link`/`conference_link` for the renderer.
- **XSS protection.** Risk labels escaped. URL scheme allowlist in
  `_link()` rejects `javascript:` and other dangerous schemes.
- **CLI flags.** `--html` renders HTML, `--output PATH` writes to file.
  `delivery.default_format` in `company.yaml` sets the default format.
  Format flags are mutually exclusive.

### Attachment-to-Drive hook

- **`attachment_drive_suggestion()`** — 10th post-LLM hook. Detects
  attachments in email messages, classifies by extension, suggests
  filing into the Drive structure. Never auto-uploads. Always requires
  explicit user confirmation.
- **Config.** `hooks.attachment_suggestions: true/false` in
  `company.yaml`.

### Other

- Pipeline and bookkeeper write paths moved from load→modify→save
  (full-table replace) to `mutate_kv()` CAS callbacks. Concurrent
  updates can no longer be silently lost.
- `_esc()` preserves `0`/`0.0`/`False` instead of collapsing them
  to empty strings in HTML tables.
- Docstrings updated to reference `state_db` / SQLite throughout.

### Review

Cross-model review by Codex (GPT-5.6 Sol) and Claude Code (Opus 5).
12 findings (4 BLOCKING, 4 MAJOR, 2 MINOR, 2 NIT), all fixed.
14 new regression tests in `test_v050_review_fixes.py`.

## v0.4.0 — Safety hardening complete, agent execution seam, release readiness

The largest release so far, and the first where the safety model is enforceable
rather than merely designed. It closes every blocking and major issue from the
v0.3.24 production review except the Microsoft 365 tier, which is deferred
pending a real Entra tenant.

**Release posture:** production-ready for `google_api` and `composio` (Google
and Microsoft). Native `m365` is code-complete but has never been live-verified;
`capabilities` and `readiness` now say so out loud.

### Safety model — enforceable, not just documented

- **Guardrail is default-deny (B1).** `confirm_action` previously let unknown
  action IDs through, and legacy `gmail.*` / `drive.*` mutation IDs were
  excluded from `WRITE_ACTIONS` entirely — any skill holding a client reference
  could call them ungated. Legacy IDs are now canonicalized to neutral `mail.*`
  / `files.*` before classification, and anything in neither `READ_ACTIONS` nor
  `WRITE_ACTIONS` is blocked. `requires_confirmation` fails closed for unknowns.
- **Per-action approval grants.** `CHIEF_OF_STAFF_AUTO_APPROVE` and
  `CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE` no longer act as ambient process-wide
  approval. Execution validates against the pending-action store at call time.
- **Concurrency safety on all state (B2).** `pending_actions`, `event_store`,
  `state_store`, and `webhook_security` wrap load→check→mutate→write in an
  exclusive file lock with unique temp filenames and `fsync` before `replace`.
  Two processes can no longer both win the `approved→executing` transition and
  both send the same email.
- **Transactional `state_store`** with `with_store_lock`, version monotonicity,
  and `ConcurrencyError` retry.
- **Fail-closed on corrupt state.** A corrupt JSON store raises
  `StateCorruptionError` and preserves the bad file instead of silently
  becoming an empty store — which was both data loss and a replay bypass.
- **Audit completeness.** Blocked attempts are now durably audited; the
  success-path audit moved inside the try/except so an audit failure returns a
  successful result with `audited=False` rather than escaping as an exception
  and inviting a duplicate send.
- **Audit hash chain** with restart markers and corrupt-tail detection, so the
  record cannot be silently edited.
- **Recipient domain classification fixed.** Substring matching classified
  `acme.com.attacker.io` as internal; now exact match or dot-boundary suffix.
- **Retry cap.** `mark_failed` capped at 3 retries then terminal `failed`,
  instead of re-arming approval forever on an ambiguous 504.
- **Stuck-action reconciliation** — `executing` timeout detection and recovery,
  surfaced in doctor and readiness.
- **HMAC webhook signatures bind a timestamp**, with a 300s skew bound and a
  replay lease with fencing.
- **`store_name` path-escape guard**, backup retention, and fail-loud on a
  missing project root.

### Agent execution seam (new)

Under the `agent` provider the AI agent performs mutations with its own
connector tools, so there is no `@guarded` call site to run the state machine.
Every agent-driven write previously dead-ended: the provider raises
`NotImplementedError`, and `mark_executed()` was unreachable from the CLI.

- **`review_queue.py claim`** transitions approved→executing and returns the
  action envelope. It must run *before* the side effect — that is where a
  lapsed approval is caught — and it makes the claim exclusive.
- **`review_queue.py record-execution`** transitions executing→executed/failed.
  A recording verb only: it cannot approve, cannot skip the claim, and cannot
  revive a terminal action. Results carry `executed_externally` so the audit
  never implies more provenance than it has.

### Operability

- **`chief_of_staff.py capabilities`** — one machine-readable call for the
  active provider, supported and refused actions *with reasons*, whether the
  provider was ever live-verified, project root, and state durability.
- **Real dependency preflight.** `doctor` imported 3 of 7 declared packages via
  `find_spec`, which reports an installed-but-broken package as present. It now
  imports all 7, catches `BaseException` (a pyo3 panic from a broken
  `cryptography` is not an `Exception`), suppresses import-time stdout chatter
  that was corrupting `doctor --json`, and names a remedy. CI gained a matching
  dependency-sanity step.
- **Hosted cloud session guardrails.** When `CLAUDE_CODE_REMOTE_SESSION_ID` is
  set, the credential-holding providers are refused: cloud environment
  variables are plain text and readable by anyone using the environment. The
  `agent` provider stays available, and the capability report warns that state
  there is ephemeral.
- **MCP session recovery** — 202/204 accepted, 404 recovery, bounded retry.

### Knowledge

- **OKF 0.2** — sequence numbers, aliases, relations, numeric confidence.
- **Wiki search** subcommand with keyword, alias and tag retrieval, stop-word
  filtering and token-boundary matching.
- **Two new hooks** — `note_capture_reminder` (post_llm_call) and
  `wiki_context_injection` (pre_llm_call), bringing the total to nine.

### Skills

- **`news-monitoring` vendored in** — nineteen skills, eighteen registered on a
  default install (`esign-connector` appears once DocuSeal is configured).
- **`deep-research` v1.6.0** — adaptive depth, clarification phase, token
  budget, numbered citations.
- Writing-for-agents pass across five skills: workspace-access extracted to
  `shared/docs/`, routing tails removed, descriptions rewritten, completion
  criteria added.

### Claude Desktop / Cowork compatibility (additive)

Hermes remains the runtime of record. These files are additive and are not read
by it:

- **`CLAUDE.md`** — the operating contract an agent reads on arrival: the
  invariant, the command index, the fetch/compute split, both execution paths,
  and the prohibitions.
- **`.claude-plugin/plugin.json` + `marketplace.json`** — makes
  `/plugin marketplace add moonlight-lupin/chief-of-staff-agent` work so the
  skills load natively in Claude Desktop and Cowork without conversion.
- **`docs/SETUP.md`** gained a hosted-cloud-session section covering the
  missing secrets store, the Trusted domain allowlist (which excludes every
  workspace endpoint), polling instead of webhooks, and ephemeral state.

### Release hygiene

- Version pins unified at 0.4.0 across `plugin.yaml`, `pyproject.toml`, the
  entrypoint, and the README, with a test asserting they agree. `--help` had
  been advertising v0.3.7 while the plugin shipped 0.3.24.
- **Demo data re-anchored on today.** `examples/sample-workspace.json` is
  pinned to a fixed day, so `demo` printed month-old mail and past meetings
  under "next 48h". Timestamps now shift by whole days at load.
- **`NOTICE`** recording the vendored MIT `deep-research` skill inside this
  Apache-2.0 work.
- **`SECURITY.md`** with a private disclosure route and an explicit in-scope
  list, and **`CONTRIBUTING.md`** covering the invariants a change may not break.
- **Lint enforced.** 113 ruff findings → 0, and CI runs ruff over `shared/`,
  `skills/`, `hooks.py` and `__init__.py` without `continue-on-error`.
- CI matrix on Python 3.11 and 3.12.

### Tests

1,802 → **2,002 passing**. New coverage for guardrail default-deny, legacy ID
canonicalization, multiprocessing races on every state store, blocked-path
auditing, retry caps, the claim/record-execution refusal paths, dependency
preflight, capability reporting, and hosted-session refusal.

### Deferred

Native Microsoft 365 Graph (tenant-wide authority constraint, token cache and
proactive refresh, real `mail.move` reversibility, timezone and upload
hardening) remains open pending a dedicated Entra tenant. See the M365 section
of `docs/PRODUCTION_ROADMAP.md`.

## v0.3.24 — Vendor deep-research v1.4.0 (structured evidence + evidence-basis discipline)

### Skills

- **`deep-research` vendored to v1.4.0** — brings the full v1.3.0 structured-evidence feature set
  into the CoS plugin alongside the v0.3.23 evidence-basis discipline. The vendored copy now ships:

  - **Structured `evidence.json` intermediate** (§3e) — separates evidence gathering from report
    writing; makes fabrication detectable. Schema + worked example in
    `references/structured-evidence-format.md`.
  - **Source quality tiers** (primary/secondary/tertiary) with 3×/2×/1× weighting, a
    healthy/acceptable/weak distribution check, and conflict resolution by quality (primary >
    secondary > tertiary).
  - **Overview-first report structure** — executive summary + at-a-glance comparison table before
    detailed analysis.
  - **Refute polarity requirement** — round 2+ must include ≥1 counter-evidence query.
  - **Language anchoring** — detect and normalize output to a BCP 47 tag.
  - **Four-label evidence-basis discipline** ([VERIFIED]/[SOURCED]/[REASONED]/[ESTIMATED]) from
    v0.3.23, now documented as **orthogonal** to source-quality tiers: tiers classify the
    *source*, labels classify the *fact*. The skill uses both.
  - **3 reference files** — `structured-evidence-format.md`, `real-estate-investment-analysis.md`,
    `skills-monetization.md`.

### Version pins

Bumped plugin/pyproject/entrypoint/README to v0.3.24 with CHANGELOG entry and the two
hardcoded version-pin tests.

## v0.3.23 — Evidence-basis discipline in deep-research

### Skills

- **`deep-research` now tags every material fact with its evidence basis**, adapting
  pere-toolkit's canonical four-label discipline (`evidence.LABELS`) from financial
  *figures* to research *facts*:

  | Label | A fact is `[LABEL]` when it is… |
  |---|---|
  | `[VERIFIED]` | corroborated across ≥2 independent, cited, dated sources |
  | `[SOURCED]` | stated by one named / cited source, not independently corroborated |
  | `[REASONED]` | analytical judgement / inference — not stated by any source |
  | `[ESTIMATED]` | a calculation or stated assumption |

  The labels thread through the loop: graded at extraction (§3c), carried in the
  synthesis state and promoted `[SOURCED]`→`[VERIFIED]` on corroboration (§3d), and
  rendered inline in the final report with a pasted **Evidence key** legend (§4).
  Reports lead on `[VERIFIED]`/`[SOURCED]` and mark `[REASONED]`/`[ESTIMATED]` as
  indicative. New pitfalls cover improvised labels, overclaiming basis, and restating
  precision the source didn't give. Skill frontmatter → v1.1.0; routing fixture
  updated to require the tags + key.

  Note: unlike pere-toolkit (which enforces this with a `memo_qa` Stop hook), this is a
  prompt-level discipline in CoS — faithful to the spirit, not machine-gated.

## v0.3.22 — Reply-awareness follow-ups + vendor updated deep-research skill

### Fixes

- **Operator match is exact, not a substring.** `daily_briefing._is_from_operator`
  now parses the bare address out of `"Name <addr>"` / `"addr"` senders and
  compares it exactly, instead of a loose `operator in sender` test that could
  misfire (e.g. `"me@example.com"` is a substring of `"jme@example.com"`, or the
  address appearing inside an unrelated display name).
- **Reply-scan window is configurable.** The sent-mail lookback (default 14 days)
  and message cap (default 50) can be widened so older replies still suppress,
  via an optional `briefing` config section:

  ```yaml
  briefing:
    reply_lookback_days: 30
    reply_sent_max: 200
  ```

  Invalid or non-positive values fall back to the defaults. Behaviour is
  unchanged when the section is absent.

### Tests

- Full `collect_gmail` unread→reply suppression now exercised end-to-end with the
  real Composio field shape (`messageTimestamp` + `"Name <email>"` sender +
  `threadId`), closing the gap the live 0→5 index check left open. Added
  coverage for the exact-match fix and the configurable scan window.

### Skills

- **Vendored the updated `deep-research` skill from the `agent_skills` repo**
  (v1.0.0): adds `fact-checker` / `source-tracker` cross-references and a
  "When NOT to use" note routing single-claim verification to `fact-checker`,
  and adds the `evals/routing-fixtures.json` routing spec the SKILL.md
  references. Removed a stray research-output file (`skills-monetization.md`)
  that had landed in the skill's `references/` dir. License stays Apache-2.0 to
  match the plugin.

## v0.3.21 — Daily briefing reply awareness (suppress already-answered threads)

### Fixes

- **Briefing no longer lists inbound mail the operator already replied to.**
  `daily_briefing.collect_gmail` fetches recent `in:sent` mail (14-day lookback),
  indexes by `thread_id` / `conversationId`, and drops inbox/unread/engagement
  hits when the operator (`google.delegate_email` or `user.email`) has a later
  message in the same thread. Sent-side queries (`sent_followup`) are left alone.
  Agent-provided `--input` envelopes get the same filter when both sides of the
  thread are present. Render notes how many messages were suppressed.
- Messages without a thread id cannot be matched and remain listed (fail-open).
- **Reply index parses Composio's `messageTimestamp` field** (ISO-8601), not only
  `date` / `internalDate` — without this the index was empty against the live
  Composio Google provider and nothing was ever suppressed. All parsed datetimes
  are normalised to timezone-aware UTC so an aware/naive mix can never raise
  `TypeError` during the in-thread comparison. Live-verified 2026-07-17: the
  reply index went from 0 → 5 entries on the real mailbox once `messageTimestamp`
  was recognised.

## v0.3.20 — Improve OneDrive Business files.untrash wiring (capability stays False)

Improves the OneDrive-for-Business restore wiring from PR #17/#19, but the
`files.untrash` capability **stays False** for all Microsoft providers — a live
probe showed the path still does not work end-to-end.

### Fixes / wiring

- **`composio_microsoft` `files.untrash` now has a real Business fallback.**
  PR #17 left the SharePoint path half-wired: after Personal Graph
  (`ONE_DRIVE_RESTORE_DRIVE_ITEM`) returned "Operation not supported", the
  code raised instead of listing/restoring the SharePoint recycle bin.
  `_ms_files_untrash` now mirrors the m365 flow — GUID → SharePoint restore;
  else Personal Graph; on Personal-only failure → recycle-bin lookup by the
  leaf name from the same-session `files_trash` (or pass `restore_target`).
- **Personal-site `site_name` scoping.** SharePoint recycle tools defaulted to
  the tenant root site. Trash/untrash now derive `/personal/{user}` from the
  item `webUrl` (or `ONE_DRIVE_GET_ROOT`) and pass it as `site_name`. Override
  with `integrations.workspace.sharepoint_site_name` (e.g.
  `/personal/user_contoso_com`).
- **SharePoint toolkit in microsoft bootstrap.** Default toolkits are now
  `[outlook, one_drive, share_point]`; connect/verify docs and next-steps
  include `--connect share_point`.

### Capability stays False — not live-verified

- **`files.untrash` remains `False`** for `composio_microsoft`,
  `composio_microsoft:mcp`, and `m365`. A 2026-07-17 live probe on a real
  OneDrive-for-Business account — **with the SharePoint toolkit connected and a
  correctly derived `/personal/…` `site_name`** — still failed end-to-end: the
  `ONE_DRIVE_DELETE_ITEM`'d file never surfaced in
  `SHARE_POINT_LIST_RECYCLE_BIN_ITEMS` (0 items after a 45s wait), so
  `files_trash` captured no `restore_target` and `files_untrash` returned
  `success=False`. The OneDrive recycle bin and the queried SharePoint recycle
  bin do not line up. The wiring ships so the path can be debugged live; the
  capability flips True only after a live run actually restores a file.

## v0.3.19 — OneDrive Business files.untrash wiring (SharePoint recycle bin)

### Features

- **OneDrive `files.untrash` restore path wired** (Personal Graph + Business
  SharePoint recycle bin). **Capability stays `False`** for `composio_microsoft`,
  `composio_microsoft:mcp`, and `m365` until a live run restores a file — the
  discipline is "True only when execution-verified." A 2026-07-17 live probe on a
  real OneDrive-for-Business account confirmed the flip would be premature:
  Personal Graph restore (`ONE_DRIVE_RESTORE_DRIVE_ITEM`) returns "Operation not
  supported" for work accounts, and the Business SharePoint fallback needs a
  connected SharePoint toolkit / `Sites.ReadWrite.All` that was not present.
  - **`m365`**: `files_trash` captures name + SharePoint recycle-bin GUID as
    `restore_target`; `files_untrash` tries Graph Personal restore then falls
    back to SharePoint REST `RecycleBin/RestoreByIds` (host-scoped SPO token;
    requires SharePoint app permission `Sites.ReadWrite.All`). Not live-verified.
  - **`composio_microsoft`**: Personal via `ONE_DRIVE_RESTORE_DRIVE_ITEM`;
    Business via `SHARE_POINT_LIST_RECYCLE_BIN_ITEMS` /
    `SHARE_POINT_RESTORE_RECYCLE_BIN_ITEM` (SharePoint toolkit must be
    connected so trash can persist `restore_target`). Not live-verified.
- **`delete_actions.py restore`** already prefers `restore_target`, so once the
  Business path is live-verified and enabled, executed trash actions restore by
  recycle-bin GUID automatically.

## v0.3.18 — Ship the Google-first beta daily loop

### Features

- **`chief_of_staff.py daily` collects live briefing sources.** The briefing
  panel now runs `daily_briefing.collect()` in read-only mode (Gmail + Calendar
  reads plus local deadlines/pipeline/todos/invoices/email_org) without recording
  delivery or writing `.last_briefing`. Summary/JSON show per-source status,
  counts, and urgent items. Provider writes remain forbidden.
- **Smoke-test write detection fixed.** `no_writes` now compares every
  snapshotted path (business YAML + wiki + state), not only `.*` files — so
  mutations to `pipeline.yaml` / `invoices.yaml` / wiki pages fail the smoke test.
- **Readiness daily-loop row reports live source health.** Missing credentials
  or failed Gmail/Calendar reads surface as WARN with detail, while local panels
  still render.
- **Beta docs / examples refreshed.** `BETA_DAILY_LOOP.md` and
  `BETA_READINESS_CHECKLIST.md` document the Google-first operator path
  (`python3`, readiness, live sources). Composio Google Drive allowlists in
  `company.yaml` examples include create-from-text / trash / untrash.

## v0.3.17 — document.handoff polish + Drive files.untrash

### Features

- **`document.handoff` readiness.** Preflight/dry-run/error copy no longer claims
  only Composio can draft; `google_api` (SA REST draft) and Composio Microsoft are
  first-class. Upload gaps fail closed before side effects; `--allow-partial`
  covers draft-only gaps. Google Composio uploads normalize `id` + `webViewLink` /
  `link` for draft body linking. Docs (`LIVE_TEST_CHECKLIST`, `APPROVAL_RUNBOOK`)
  and `PROVIDER_RECOMMENDATIONS` updated.
- **`files.untrash` soft-delete restore symmetry (Google).** New capability
  `files.untrash` (legacy `drive.untrash`) with ABC + `drive_untrash` alias.
  - `google_api`: Drive REST `files.update` `trashed=False` (SA + delegate)
  - Composio Google: `GOOGLEDRIVE_UNTRASH_FILE`
  - `delete_actions.py restore` for executed `drive.trash` → `drive_untrash`
  - Guardrails: `files.untrash` in `WRITE_ACTIONS` / `SAFE_WRITE_ACTIONS`
- **OneDrive untrash wired but capability False.** Methods call
  `ONE_DRIVE_RESTORE_DRIVE_ITEM` (Composio MS) / Graph `POST …/restore` (`m365`);
  kept False with Personal-only reason until Business/SharePoint is verified.
- **Beta daily-loop notes.** `BETA_DAILY_LOOP.md` / `BETA_READINESS_CHECKLIST.md`
  refreshed for Google-first beta; Outlook email-org E2E deferred without Entra.

## v0.3.16 — Keyless binary file upload via MCP sandbox staging

### Features

- **Composio binary file upload without `COMPOSIO_API_KEY`.** Google Drive and
  OneDrive binary uploads (`.pdf`/`.docx`) previously required a project
  `COMPOSIO_API_KEY` to stage a `FileUploadable` through the Files REST API (the
  MCP key 401s). New `composio_files.stage_file_uploadable_via_sandbox()` stages
  the local file into Composio's object store over the **MCP meta-tools**
  (`COMPOSIO_REMOTE_BASH_TOOL` base64-pipes the bytes into the remote sandbox with
  an md5 integrity check; `COMPOSIO_REMOTE_WORKBENCH.upload_local_file()` returns
  the `s3key`), needing **only `COMPOSIO_MCP_KEY`**. The provider's `files_upload`
  (both families) routes binary through this path; text is unchanged
  (`GOOGLEDRIVE_CREATE_FILE_FROM_TEXT` / `ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE`).
  The REST stager stays available for keyed setups but is no longer the default.
- **`files.upload` → True** for `composio`, `composio:mcp`, `composio_microsoft`,
  `composio_microsoft:mcp`; the `COMPOSIO_API_KEY` `UNSUPPORTED_REASONS` entries
  are removed. `--verify-writes` now exercises `files_write` instead of skipping
  it. **Execution-verified 2026-07-17** (only `COMPOSIO_MCP_KEY`): a throwaway
  `.pdf` uploaded to Google Drive and OneDrive and was trashed (Drive confirmed in
  Trash).

### Cleanup semantics

- Local temp file (caller-unlinked) and the sandbox working copy (removed in the
  stager's `finally`; sandbox is session-TTL reclaimed) are cleaned up. The
  intermediate Composio **S3 staged object** is **access-revoked** (its presigned
  URL 403s immediately) and **reclaimed on the tool-router session TTL** — there
  is no MCP delete for it, so it is not purged on demand (an explicit delete would
  need the Files REST API). Documented in the `files_upload` CLEANUP note.

### Constraints

- Composio upload tools cap `FileUploadable` at 5 MB (the stager rejects larger);
  base64 inflates the transfer ~1.33× (chunked ~700 KB/round-trip via heredoc,
  bypassing `MAX_ARG_STRLEN`).

## v0.3.15 — Google Drive trash, google_api drafts, Outlook email-org

### Features

- **Google Composio Drive files**: text uploads now use
  `GOOGLEDRIVE_CREATE_FILE_FROM_TEXT` (`file_name`+`text_content`, MCP-native — no
  Files-API staging, no `COMPOSIO_API_KEY`); binary uploads use
  `GOOGLEDRIVE_UPLOAD_FILE` with a staged `file_to_upload` (the raw `file_path`
  was silently ignored — fixed). **`files.trash` → True (execution-verified
  2026-07-16):** a text file created via `CREATE_FILE_FROM_TEXT` was trashed via
  `GOOGLEDRIVE_TRASH_FILE` and confirmed in Drive Trash. **`files.upload` stays
  False** (mirrors OneDrive): text works over MCP, but binary document filing
  needs `COMPOSIO_API_KEY` — or use the `google_api` service-account provider,
  which uploads binary to Drive directly with no Composio key.
- **`google_api` `mail.draft`**: create drafts via Gmail REST
  (`users.drafts.create`) with service-account domain-wide delegation when
  `google_api.py` has no draft CLI. Uses the `gmail.modify` scope (already in
  the provider's standard SCOPES, so no new admin delegation) and surfaces the
  message id as `id` (keeps `draft_id`). **Execution-verified 2026-07-16** — the
  draft landed in the delegate's Drafts folder. Unlocks `document.handoff` on
  google_api.
- **email-organisation Composio Microsoft**: classify → suggest → prepare path
  hardened for Outlook categories (displayName as tag id, Outlook message
  shape, category-aware suggestion copy). Live checklist covers prepare →
  review_queue execute.

## v0.3.14 — Google cleanup hardening + Outlook inspect-labels

### Fixes / hardening

- **Google Composio mail.tag / archive / unarchive / trash / untrash**:
  - Reject Gmail draft ids (`r-…`) on label/trash paths (tools need hex message ids).
  - Resolve label display names → `Label_…` ids before `GMAIL_ADD_LABEL_TO_EMAIL`.
  - `mail_create_tag` reuses an existing label id on 409/already-exists (verify
    path no longer falls back to the bare display name).
  - `workspace_verify` looks up tag ids via `mail_list_tags` on reuse.
  - **Execution-verified 2026-07-16 (live Gmail):** with the hardened path,
    `--verify-writes` on `family: google` ran green (`write_ready: yes`) — a full
    archive→unarchive→trash→untrash cycle plus tag apply on real hex message ids,
    no id-shape errors. These five capabilities are now **True** for
    `composio` / `composio:mcp`.

### Features

- **email-organisation `inspect-labels` (Composio Microsoft)**: Outlook-aware
  summary (`Outlook Category Inspection`, `tag_surface: outlook_categories`);
  `parse_labels` accepts `displayName` / missing `type` for master categories.

## v0.3.13 — Hermes Composio reads + Google Composio cleanup/tags/send

### Features

- **Hermes Composio MCP as read front-end**: document the fetch/compute split when
  Hermes already has Composio MCP connected — agent fetches reads →
  `schemas.py` envelope → `--input`; writes stay on `get_workspace_client`
  (`@guarded` + audit). Updated `agent` provider guidance, daily-briefing /
  weekly-review / meeting-prep Workspace Access, and `docs/SETUP.md`.
- **Google Composio parity** (catalog-wired from docs.composio.dev/toolkits/gmail):
  - `mail_list_tags` → `GMAIL_LIST_LABELS`
  - `mail_create_tag` → `GMAIL_CREATE_LABEL`
  - `mail_tag` / archive / unarchive → `GMAIL_ADD_LABEL_TO_EMAIL`
  - `mail_trash` / `mail_untrash` → `GMAIL_MOVE_TO_TRASH` / `GMAIL_UNTRASH_MESSAGE`
  - `mail_send` → `GMAIL_SEND_EMAIL` (approval-gated, same model as MS)
  - **Execution-verified 2026-07-16 (live Gmail):** `mail.list_tags`
    (`GMAIL_LIST_LABELS`), `mail.create_tag` (`GMAIL_CREATE_LABEL`), `mail.send`
    (`GMAIL_SEND_EMAIL`, sent + received) → capabilities True.
  - **Wired but NOT yet verified → False:** `mail.tag` / `mail.archive` /
    `mail.unarchive` / `mail.trash` / `mail.untrash`. The live probe rejected a
    Gmail draft id where a hex message id is required; `mail_create_draft` now
    surfaces the underlying `message.id` (the fix), and these flip True once
    `--verify-writes` re-runs green on `family: google`.
  - Still False: `mail.list_folders` / `mail.move`, `calendar.cancel`, `files.trash`
- **email-organisation**: skill + live checklist cover Composio Microsoft
  Outlook categories (Phase 4) and the CoS-only write path.

### Unchanged on purpose

- `calendar.cancel` remains False (no restore-path parity).
- Composio Microsoft `files.upload` remains False until `COMPOSIO_API_KEY`
  enables binary filing (text `CREATE_TEXT_FILE` still works when called).

## v0.3.12 — Composio Microsoft Phase 4 categories + MCP-native OneDrive text upload

### Features

- **Outlook categories (Phase 4)**:
  - `mail_list_tags` → `OUTLOOK_GET_MASTER_CATEGORIES`
  - `mail_create_tag` → `OUTLOOK_CREATE_USER_MASTER_CATEGORY`
  - `mail_tag` → `OUTLOOK_GET_MESSAGE` (current categories) +
    `OUTLOOK_UPDATE_EMAIL` (append category displayName)
  - Tag id IS the category `displayName` (same contract as native m365)
  - Capabilities: `mail.list_tags` / `mail.tag` / `mail.create_tag` True for
    `composio_microsoft` / `:mcp`
- **OneDrive text uploads without Files API**:
  - Plain-text files use `ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE` (`name` +
    `content` + optional `folder`) over Connect MCP — no
    `COMPOSIO_API_KEY` / FileUploadable staging
  - Binary files still use `ONE_DRIVE_ONEDRIVE_UPLOAD_FILE` with staged
    `{name,mimetype,s3key}` (project `x-api-key`) or a public `source_url`
  - Files staging retries `x-consumer-api-key` when `x-api-key` returns 401/403
  - Capabilities: `files.download` / `files.trash` True (execution-verified
    2026-07-16). `files.upload` stays **False**: the text path works over MCP,
    but binary document filing (`.pdf`/`.docx`) needs `COMPOSIO_API_KEY`, and a
    coarse boolean must not over-promise it (set the key to enable)

### Docs

- `docs/SETUP.md` clarifies text vs binary OneDrive paths and Phase 4 tags
  (see also https://composio.dev/toolkits/one_drive).

## v0.3.11 — Composio Microsoft Phase 3 folders + approval-gated mail.send

### Features

- **Folder-first Outlook organise (Phase 3)**:
  - `mail_list_folders` → `OUTLOOK_LIST_MAIL_FOLDERS` (also native m365
    `GET /mailFolders`)
  - `mail_move_to_folder` → `OUTLOOK_MOVE_MESSAGE` / Graph move with any folder
    id or well-known name
  - `mail_resolve_folder` helper (well-known names + display-name lookup)
  - Capabilities: `mail.list_folders` / `mail.move` True for
    `composio_microsoft` / `:mcp` and `m365`
- **Approval-gated `mail.send`** for Composio Microsoft via `OUTLOOK_SEND_EMAIL`:
  - Capability True (still destructive)
  - Preferred path: `send_email.py prepare → approve → execute` (works for any
    provider that supports `mail.send`, including m365 / google_api /
    composio_microsoft)
  - Direct calls require `CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1` (plus
    `CHIEF_OF_STAFF_AUTO_APPROVE=1` when approval already happened via the queue)
  - Guardrail messaging names the approve→execute path
- Google Composio `mail.send` remains intentionally disabled.

### Docs

- `docs/SETUP.md` updated for Phase 3 folders and approved send.

## v0.3.10 — Composio OneDrive FileUploadable staging + mail-move verify

### Features

- **Composio Files API staging** (`shared/scripts/composio_files.py`): local files
  are staged via `POST /api/v3.1/files/upload/request` (v3 fallback) + presigned
  PUT, then passed to `ONE_DRIVE_ONEDRIVE_UPLOAD_FILE` as
  `{name, mimetype, s3key}`. Azure Blob PUTs set `x-ms-blob-type: BlockBlob`.
- **OneDrive download persist** prefers Composio `s3url` fetch before inline/
  base64 fallbacks.
- **Capability matrix** for `composio_microsoft` / `:mcp`: `files.upload` /
  `files.download` / `files.trash` and `mail.archive` / `mail.unarchive` /
  `mail.untrash` are True (share execution-verified `OUTLOOK_MOVE_MESSAGE`).
- **Verify harness** adds `mail_move_write` (draft → archive → inbox → trash →
  inbox → trash) and runs `files_write` (upload → optional download → trash)
  when those capabilities are present.

### Docs

- `docs/SETUP.md` updated for FileUploadable staging and the mail-move probe.

## v0.3.9 — Composio Microsoft cleanup + content writes (Phase 1+2)

### Features

- **Composio Microsoft mail cleanup** via catalog slug `OUTLOOK_MOVE_MESSAGE`:
  `mail_archive` → `archive`, `mail_trash` → `deleteditems` (soft-delete),
  `mail_unarchive` / `mail_untrash` → `inbox`. Returns `restore_target` like
  native m365. Does **not** use permanent `OUTLOOK_DELETE_MESSAGE`.
- **Composio Microsoft OneDrive trash** via `ONE_DRIVE_DELETE_ITEM` (recycle bin,
  not `ONE_DRIVE_DELETE_ITEM_PERMANENTLY`).
- **Content writes (Phase 2)** capability-True with **Composio catalog arg shapes**:
  `mail.draft` (`OUTLOOK_CREATE_DRAFT`), `calendar.create` /
  `calendar.update`, `files.upload` / `files.download`. Args no longer send raw
  Graph JSON where the catalog expects Composio fields (`to_recipients`,
  `start_datetime`+`time_zone`, `folder`, `file_name`, …). Write payloads normalize
  a top-level `id` for `workspace_verify`.
- **`calendar_delete`** (`OUTLOOK_DELETE_CALENDAR_EVENT`) for verify cleanup;
  opt-in CLI `--verify-calendar-writes` (create→update→delete of a marked
  `[CoS verify]` event). Default `--verify-writes` still never creates events.
- **Verify draft cleanup without tags**: when `mail.tag` is unsupported, a
  successful draft probe still trashes the artefact (needed for Composio MS).
- **Capability matrix** for `composio_microsoft` / `:mcp`: cleanup + content
  writes True; `mail.send` / `calendar.cancel` / `mail.tag*` still False.
- Slugs overridable via `tool_slugs` (`mail_move`, `files_trash`,
  `calendar_delete`, …). Google Composio family still refuses MS-only cleanup
  methods.

### Docs

- `docs/SETUP.md` Composio Microsoft verification note updated for Phase 1+2.

## v0.3.8 — Code review fixes (v0.3.5→v0.3.7 review findings)

### Breaking changes

- **Composio Microsoft write capabilities** (`mail.draft`, `calendar.create/update`, `files.upload/download`) now report `False` (unsupported) until execution-verified. Write implementations and Graph-correct arg shapes remain in code. Use Google Composio or native M365 Graph for writes.
- **`esign-connector` skill** is no longer registered unless `esign.url` is configured in `company.yaml`. `self-sign` remains always registered.
- **`GmailClient.get_attachment`** restored to pre-v0.3.6 inline `gmail attachment` CLI verb. `download_attachment` is now a separate method using `tempfile.mkdtemp()` (0o700) instead of world-readable `/tmp`.
- **`connect_workspace`** config discovery logging is quiet by default; use `--verbose` or `CHIEF_OF_STAFF_DEBUG=1` for the resolved-path log.

### Security fixes

- **Doctor DocuSeal probe** refuses non-HTTPS URLs, metadata/link-local/loopback/private IPs, and host mismatch with `esign.domain` before attaching API key.
- **Runtime log scrubbing** extended: MSAL/Google token shapes (`ya29.*`, UUIDs, 48+ char tokens), JSON key-value secrets, URL-embedded tokens.
- **`sanitize_provider_error_detail()`** classifies/truncates auth failure blobs before logging to `events.jsonl`.
- **Assistant/company name validation** rejects newlines, double quotes, and >64 char names before YAML interpolation.
- **Dotenv parser** rejects suspicious process-control keys (`PATH`, `LD_PRELOAD`, `PYTHONPATH`, etc.) and strips inline comments.

### Correctness fixes

- **Soft Composio errors** (rate limits, auth failures) now raise `RuntimeError` instead of returning `{successful: False}` → no false `read_ready: true`.
- **Error classifier reordered**: connection errors checked before unknown-tool; bare `"not found"` removed from needles.
- **Identity scrub** completed: `google.domain`, `account_alias`, SA path, Drive root, company legal IDs, phones, `home_chat_id` all scrubbed on first install. Re-bootstrap preserves operator-edited values.
- **Shipped SKILL.md** descriptions now contain rendered defaults (`Chief of Staff` / `your organization`) — no more literal `{assistant_name}` placeholders.
- **Bootstrap overlay**: custom assistant names render to `skills.local/` (gitignored) instead of mutating tracked `skills/*/SKILL.md`.
- **`skills.local/` overlay** is now loaded by `register()` — named routing works end-to-end.
- **Microsoft calendar** date-only end times use `T23:59:59Z` (not zero-duration `T00:00:00Z`).
- **`@guarded` audit slugs** resolve dynamically from family — Microsoft audit records use `OUTLOOK_*` / `ONE_DRIVE_*` slugs.
- **`esign.admin_email`** legacy fallback: both `provider_email` (preferred) and `admin_email` (legacy) accepted.
- **Name-injection skip logic** unified via shared `is_default_assistant_name()` across bootstrap/doctor/hooks.
- **Family resolution** deduplicated into `composio_family.py`, shared by client and `connect_workspace`.
- **Query compile failure** now emits `warnings.warn()` before falling back to `{top: N}`.
- **Family/toolkit mismatch** warning on client init.
- **`list_attachments`** warns on unexpected shapes instead of silent `[]`.
- **`build_briefing()`** public API replaces `cmd_demo`'s private `_build_structured_briefing` call.

### Other

- MCP `clientInfo.version` updated to match plugin version.
- Freemail domain list expanded (`googlemail.com`, `live.com`, `icloud.com`, `proton.me`, etc.).
- README note: "Never paste API keys or secrets directly into chat logs."
- `.gitignore` entry for `skills.local/` overlay.