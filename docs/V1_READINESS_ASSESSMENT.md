# v1.0 readiness: go/no-go

Assessed 2026-11-12, after the eight readiness PRs (#40–#45, #49, and the demo/docs PR).
The version stays at 0.9.0 until the owner decides.

## Verdict

**Not yet: conditional GO.** No known code blocker remains. Four steps belong to the
owner and can't be done from a pull request: run Hermes's validator, refresh the live
evidence, turn on repository protections, and publish the advisory. Tag 1.0 once
those four are done.

## What changed since 0.9.0

| Area | Before | Now | PR |
|---|---|---|---|
| Approvals | An agent could approve through plugin commands or environment flags | Decisions are signed in the Hermes gateway; each execution needs a single-use grant bound to its target | #40 |
| Accounts | An action could run from whichever account the client used | The action is bound to its account and identity, and drift is refused before the write | #41 |
| Untrusted content | Payload paths, wiki text and workflow strings were trusted | Paths are confined, re-injected text is fenced, workflows can't self-approve, and secrets are redacted | #42 |
| Live verification | "Execution-verified" lived in code comments | An evidence matrix; a `live` label needs a dated record; failed sends are never retried silently | #43 |
| Spend | Overruns were silent; currencies could mix; provenance was unchecked | Overruns are surfaced, one currency per provider, stale reservations are recoverable, paid data needs a recorded run | #44 |
| Supply chain | Unpinned actions; unbounded `requirements.txt`; vulnerable floors | SHA pins, read-only tokens, pip-audit on the newest and the minimum versions, Dependabot, CODEOWNERS | #45 |
| Maintainability | Three copies of the execution path | One `run_approved`; pre-call refusals are never final | #49 |
| Demo and docs | A briefing-only demo; a README example that failed | The seven-step `demo --flow`; onboarding tested; catalog rules 1–19 checked | PR 8 |

Tests: 3760 passed, 1 skipped. Every PR had an independent adversarial review, and
each finding was fixed with a regression test or written down as an accepted risk
below.

## Blockers: owner actions before tagging 1.0

1. **Run Hermes's validator at the release commit:**
   `hermes plugins validate <checkout> --install-deps`. Paste the report into the
   catalog PR. It was not run in this assessment: installing and running Hermes's code was
   outside the permissions here. Expect one `caution`: about 410 tracked files against a
   400-file soft limit. Accept it or consolidate the test files.
2. **Refresh the live evidence** for every provider you advertise:
   `connect_workspace.py --verify-writes --record-evidence`, then promote the candidates
   in a reviewed change. The recorded runs are from July 2026. `google_api` has only
   drafts and contacts as `live`, and native `m365` has none.
3. **Turn on repository protections** in GitHub settings: "Require review from Code Owners",
   the required checks `test (3.11)`, `test (3.12)` and `security`, and secret scanning
   with push protection.
4. **Publish the security advisory** for the approval, account and untrusted-content fixes
   (#40–#42), as agreed ("fix in public PRs, advisory later"), before announcing 1.0.

## Accepted risks (documented, not fixed)

- **Same-user shell.** An agent that can write and run code as the same OS user can still
  forge a decision or call a provider directly. Mitigations: Hermes command approval, the
  deny rules, the enrolment audit and an isolated terminal backend. See `SECURITY_MODEL.md`.
- **Agent-native connectors.** The plugin can't see which account an agent's own
  connector acts as. `claim` names the account, and `record-execution` audits what the
  agent reports.
- **Native Microsoft Graph** is code-complete but has never run against a real tenant.
  `capabilities` reports 0 live actions for it.
- **Spend.** The ledger is a local estimate, not the invoice. Two concurrent proposals in
  different currencies are not serialised. One recorded run can back several paged ingests.
- **The demo** plays the gateway's part itself. It says so on screen.
- **`--verify-writes`** sets the break-glass flags in-process for its own create-and-clean-up
  probes. It is operator opt-in and is ignored on enrolled stores.

## Next (after 1.0)

- Keep the gateway signing key in the Hermes secret store, so a restart doesn't
  need re-enrolment.
- Per-company secret names for multi-company installs under Hermes.
- Retire or redesign roadmap item 4.2 (token cache): it conflicts with catalog rule 11.
- `bootstrap.py --workspace-provider agent` for hosted sessions. Drop the Google
  placeholder on m365 installs, and stop `readiness` repeating the hosted-session
  refusal five times.
- Get under the scanner's 400-file soft limit if the catalog reviewer asks.
