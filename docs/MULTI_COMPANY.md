# Multi-company, Notion and Monid: design decisions

Status: accepted (2026-11-12). Phases 1 to 4 are implemented.

## Decisions

| # | Decision | Consequence |
|---|---|---|
| D1 | **The plugin's own store is the source of truth; Notion is a mirror.** | Live records sit in each scope's `state.db` (YAML files are imported once and exported for backup). Sync runs from the store to Notion. Edits made in Notion come back as *suggestions* in the review queue and are never written into the store silently. |
| D2 | **Group-level writes are allowed.** | A group owns its own store and approval queue. A company never writes directly into a sister company's store. |
| D3 | **Paid data calls run against approved budgets.** | A human approves a *spend envelope* once. Paid calls made for the same purpose within the envelope's cap are approved by the envelope. Exceeding the cap or starting a new topic means a new envelope, which goes back to the human. |

## Isolation model

```
registry (companies.yaml)
├── group: lupin ── group store (group.yaml, own state.db + audit + data repo)
│   ├── company: acme   (company.yaml, own project_root/state.db/audit/data repo)
│   └── company: beta   (company.yaml, own ...)
└── company: solo       (standalone, no group)
```

- **One company per process.** You choose it with `--company <slug>` or
  `CHIEF_OF_STAFF_COMPANY`. If more than one company is registered and none is
  chosen, the process refuses to run rather than picking one.
- **Stores are disjoint.** The registry refuses two companies, or a company and
  a group, whose `project_root` is the same path or nested inside the other.
- **Actions are bound to one company.** Every pending action records the
  `company_id` it was created under, and that ID is part of the approval
  fingerprint. A claim from any other company is refused and audited as
  `company_mismatch`.
- **Reads across companies** need the reader to be a sister company or the
  group scope, *and* a `share:` entry for that data domain
  (`pipeline`, `todos`, `invoices`, `expenses`). `company_registry.read_shared`
  opens the owner's `state.db` read-only, creates nothing in it, and audits
  every attempt (`shared.read`, success or denied) in the reader's log.
- **Group writes** happen in group scope (`--group <slug>`), which has its own
  config, `state.db`, audit log and queue. Its actions are bound to
  `group:<slug>`, so no member company can claim them. A group config must
  list `approvals.approvers`; approval by anyone else is refused and audited
  as `approver_not_authorised`. A company config may list approvers too.
- **Group roll-up:** `chief_of_staff.py --group <slug> group-summary` is a
  read-only summary of each member's shared domains. Domains that are not
  shared show as `not shared`.
- **Connector identity is detected, not prevented.** Under the `agent`
  provider the plugin cannot stop a connector acting from the wrong mailbox.
  Each company declares `accounts:` (mail, calendar, drive, notion; calendar
  and drive fall back to mail). `claim` returns `expected_account`, and
  `record-execution --executor-account` is checked against it. A mismatch is
  still recorded as executed, because it has already happened, but it is
  audited as `account_mismatch` and `doctor` warns about it.

## Account binding: what is enforced before the write

Every workspace action (mail, calendar, files, contacts, Notion) is bound,
when it is created, to two things:

- the account its company declares for that family (`accounts:` in
  company.yaml; calendar and drive fall back to `mail`);
- the configured workspace provider;
- the identity the configured client acts as: the Google `delegate_email`,
  the Microsoft 365 client-credentials `user_principal`, or a digest of the
  Composio routing (`user_id`, `account_aliases`). This holds even with no
  `accounts:` block, which is the default.

The binding is part of the approval fingerprint.

| Check | When | Result |
|---|---|---|
| The account or provider changed in company.yaml after approval | Before execution, on every path | Refused as `binding_changed`; nothing runs |
| Python path: the client knowably acts as another account | Before the provider call | Refused as `account_mismatch`; the action is marked failed |
| Agent path: the agent states an account at `claim` | Before the action moves to `executing` | Missing (when one is declared) or mismatched: refused |
| Agent path: the account reported at `record-execution` | After the side effect (it already happened) | Recorded; a mismatch or missing account is audited and `doctor` warns |
| Python path: the client cannot know who it is (M365 device code, Composio) | At execution | Runs, recorded as `unverified`; `doctor` warns |
| Two registered scopes (companies or groups) send as the same account (`accounts.mail`, the Google delegate, the M365 mailbox) | When the registry loads | Refused. A shared calendar or Notion workspace is allowed |

**What the client can know.** Google `google_api` acts as `delegate_email`.
Microsoft 365 with client credentials acts as `user_principal`. Microsoft 365
device code, Composio and agent-native connectors act as whoever signed in,
which the plugin cannot see. For those, the check is the stated account
(agent path) and the audit. The plugin never presents that as proof.

**Secrets are install-wide.** Under Hermes, a provider's key (for example
`GOOGLE_SERVICE_ACCOUNT_JSON`, `M365_CLIENT_SECRET`, `COMPOSIO_MCP_KEY`) is
one per install. Companies that need different Google service accounts or
Composio users need separate installs or Hermes profiles until per-company
secret names land.

## Spend envelopes (phase 4)

An envelope is a pending action of type `data.spend_envelope`. Its payload is
`{provider, purpose, topic_key, cap, currency, expires_at}`. A human approves
it through the normal queue. After that:

1. Before a paid call, the agent runs
   `spend.py reserve --envelope <id> --topic <key> --estimate <amount> --endpoint <name>`.
   The cost is reserved atomically.
2. If the topic matches and `spent + reserved + estimate <= cap`, the call is
   approved, with the approver recorded as `envelope:<id>`. The agent runs it
   and records the actual cost.
3. Otherwise the call is refused, and the agent proposes a new envelope.

This keeps the core rule: no proposal is approved by the agent that made it.
The human approved the envelope, so the plugin only carries that approval out.
Every call is audited along with its cost.

**Limits:**

- **The ledger is a local estimate, not the provider's invoice.** It totals the
  costs the agent recorded. The provider does not enforce it, and a call made
  outside `spend.py` never reaches it. Reconcile against the provider's
  billing.
- **One currency per provider.** Set `spend.currency`. Even without it, a
  provider's envelopes cannot mix currencies. `reserve --currency` refuses an
  estimate in another currency.
- **Overruns are recorded, never hidden.**
  - An actual cost above the envelope's cap exhausts that envelope.
  - A month-to-date total above `spend.<provider>.monthly_cap` is reported by
    `record` (`over_monthly_cap`), `status` and the daily briefing.
  - Further reservations are refused until the next month.
- **Stale reservations.** A reservation still open after a day appears in
  `spend.py stale` and in `doctor`. Check the provider's run history, then
  either `record` the actual cost or `release` it if the call never ran. It is
  never closed automatically.
- **Paid data needs provenance.** Data from a provider with a `spend:` section
  is ingested only when its `run_id` was recorded against an approved envelope
  of the same company. A claimed `envelope_id` must be that envelope.

## Phases

| Phase | Scope |
|---|---|
| 1 | Registry, active-company resolution, disjoint-store validation, `company_id` binding on actions, `capabilities` shows the company. |
| 2 | Group store and queue, `share:` read policy, `daily --group` roll-up, expected-account declaration and executing-account audit. |
| 3 | Done. `shared/scripts/notion_mirror.py` with action types `notion.db.upsert` (low), `notion.page.create/update` (medium), `notion.pull` (medium) and `notion.page.archive` (high), plus skills `notion-sync` (pipeline and todos; new Notion tasks come in through it, so no separate capture skill is needed) and `notion-wiki`. |
| 3b | Done. Notion as an input to existing skills: an optional `notion` block in the workspace envelope (`shared/scripts/notion_inputs.py`), used by daily briefing, weekly review and meeting prep. Read-only. |
| 4 | Done. `shared/scripts/spend.py` (spend envelopes), then the skills `prospecting`, `seo-tracker` and `competitor-watch`. Covers PDPA data minimisation, a source record for every lead, a Do Not Call block before any phone or SMS contact, and a permanent opt-out suppression list. |

## Registry format

```yaml
# shared/config/companies.yaml (or CHIEF_OF_STAFF_REGISTRY)
groups:
  lupin:
    name: "Lupin Holdings"
    config: groups/lupin/group.yaml       # optional; needed for group writes
    share:                                # read-only, per data domain
      pipeline: read
      todos: read
companies:
  acme:
    config: companies/acme/company.yaml   # relative to this file
    group: lupin
  solo:
    config: companies/solo/company.yaml
```

A group config is an ordinary company.yaml for the holding entity, plus:

```yaml
approvals:
  approvers: ["Alicia Tan"]
```

A company config may declare the accounts its connectors should act from:

```yaml
accounts:
  mail: ops@acme.example
  notion: acme-workspace
```

`--group` and `--company` cannot be combined. The order of precedence is: an explicit `--config` path, then
`CHIEF_OF_STAFF_COMPANY` (looked up in the registry), then
`CHIEF_OF_STAFF_CONFIG`, then the registry's only company, then
`shared/config/company.yaml`. Without a registry, behaviour is unchanged.
