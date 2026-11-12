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

## Phases

| Phase | Scope |
|---|---|
| 1 | Registry, active-company resolution, disjoint-store validation, `company_id` binding on actions, `capabilities` shows the company. |
| 2 | Group store and queue, `share:` read policy, `daily --group` roll-up, expected-account declaration and executing-account audit. |
| 3 | Done. `shared/scripts/notion_mirror.py` with action types `notion.db.upsert` (low), `notion.page.create/update` (medium), `notion.pull` (medium) and `notion.page.archive` (high), plus skills `notion-sync` (pipeline and todos; new Notion tasks come in through it, so no separate capture skill is needed) and `notion-wiki`. |
| 3b | Done. Notion as an input to existing skills: an optional `notion` block in the workspace envelope (`shared/scripts/notion_inputs.py`), used by daily briefing, weekly review and meeting prep. Read-only. |
| 4 | Done. `shared/scripts/spend.py` (spend envelopes), then the skills `prospecting`, `seo-audit` and `competitor-watch`. Covers PDPA data minimisation, a source record for every lead, a Do Not Call block before any phone or SMS contact, and a permanent opt-out suppression list. |

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
