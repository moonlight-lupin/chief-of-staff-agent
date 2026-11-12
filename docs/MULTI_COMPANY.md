# Multi-company, Notion and Monid: design decisions

Status: accepted (2026-11-12). Phase 1 is implemented; phases 2 to 4 are planned.

## Decisions

| # | Decision | Consequence |
|---|---|---|
| D1 | **YAML is the source of truth.** Notion is a mirror and capture surface. | Sync runs from YAML to Notion. Edits made in Notion come back as *suggestions* in the review queue and are never written into YAML silently. |
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
- **Reads across companies (phase 2)** need *both* companies in the same group
  *and* a `share:` entry for that data domain. They are read-only.
- **Group writes (phase 2)** go into the group store through the group's own
  queue. The group config lists its approvers.
- **Connector identity is detected, not prevented.** Under the `agent`
  provider the plugin cannot stop a connector acting from the wrong mailbox.
  `capabilities` states this limitation now. In phase 2, each company declares
  the accounts it expects and `record-execution` records the account that
  actually acted, so a mismatch shows up in the audit log and in `doctor`.

## Spend envelopes (phase 4)

An envelope is a pending action of type `data.spend_envelope`. Its payload is
`{provider, purpose, topic_key, cap, currency, expires_at}`. A human approves
it through the normal queue. After that:

1. Before a paid call, the agent runs
   `review_queue.py spend --envelope <id> --topic <key> --estimate <amount>`.
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
| 3 | Notion action types (`notion.page.create/update`, `notion.db.upsert`, `notion.page.archive`) and skills `notion-sync`, `notion-wiki`, `notion-capture`. |
| 4 | Spend envelopes, then Monid skills `prospecting`, `seo-audit`, `competitor-watch`. Covers PDPA data minimisation, a source record for every lead, and a Do Not Call flag before any contact. |

## Registry format

```yaml
# shared/config/companies.yaml (or CHIEF_OF_STAFF_REGISTRY)
groups:
  lupin:
    name: "Lupin Holdings"
companies:
  acme:
    config: companies/acme/company.yaml   # relative to this file
    group: lupin
  solo:
    config: companies/solo/company.yaml
```

The order of precedence is: an explicit `--config` path, then
`CHIEF_OF_STAFF_COMPANY` (looked up in the registry), then
`CHIEF_OF_STAFF_CONFIG`, then the registry's only company, then
`shared/config/company.yaml`. Without a registry, behaviour is unchanged.
