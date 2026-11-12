# Security model

What Chief of Staff guarantees, against whom, and where those guarantees end.
The policy for reporting a vulnerability is in [`SECURITY.md`](../SECURITY.md).

## The invariant

```
Observe → Understand → Suggest → Approve → Execute → Audit
```

The agent may observe and propose freely. Approval belongs to a person.
Execution happens only for an approved action, and it is audited.

## Who we defend against

| Actor | In scope? | What protects you |
|---|---|---|
| **Hostile content**: an email, calendar invite, web page, Notion page, prospect record or competitor site that contains instructions | **Yes**: the main threat | Content is data, never authority. An instruction found in content cannot approve anything (below). |
| **An agent that follows such instructions**, running under Hermes with its default approval settings | **Yes** | Signed approvals and execution grants (below). |
| **Someone else in a shared chat** (Telegram, Discord…) | **Yes** | `/cos approve` accepts only your own CLI, TUI or Desktop session, or chat users you list in `approvals.chat_approvers`. Refusals are audited. |
| **A deliberately malicious agent with an unrestricted shell as your OS user**, Hermes approvals off | **No**: a plugin cannot contain this | Run Hermes with an isolated terminal backend (for example Docker) that does not mount your credentials, the plugin's data or Hermes's `.env`. |

## Signed approvals (Hermes)

When the Hermes gateway loads the plugin, it creates a signing key **in its
own memory**. The key is never written to disk or passed to any command. The
gateway then *enrols* every company store it serves. From then on, for that
store:

- **`/cos approve` and `/cos dismiss` sign the decision** in the gateway
  process. The signature covers the action, its approval fingerprint, its
  company, the decision and the approver's name.
- **Unsigned decisions are refused.** That includes `review_queue.py approve`
  and its bulk form, and the other approve commands. Each refusal is audited.
- **Every execution re-verifies the signature.** An approved row edited
  afterwards, or an approval made before enrolment, does not run. Re-approve it
  with `/cos approve`.
- **Approval is only ever a fresh decision on a proposal.** An executing or
  finished action cannot be approved again. A failed action stays failed, and
  a retry is a new proposal and a new approval.
- **Enrolment covers the whole install.** Once one store is enrolled, every
  store on the install is enforced, including a new or copied one. If a store
  can't be read, the check fails closed.
- **Enforcement is sticky and visible.** No plugin API can switch it off.
  Every enrolment is recorded in the audit log, with the process that made
  it, and `doctor` lists the enrolled keys.
- **The break-glass switches stop counting as approval.**
  `CHIEF_OF_STAFF_AUTO_APPROVE` and `CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE`, and a
  "y" typed at a prompt, no longer let a write through. A guarded write runs
  only inside an *execution grant*. The grant is tied to one signed action
  while it is executing, and covers that action's own kind of write to its
  approved target, once.

Stores that are never enrolled, such as a standalone CLI install or Claude
Code, keep the original model: the operator approves at their own terminal.

`doctor` reports the enrolment state (`approval_signing`).

### Belt and braces: Hermes deny rules

Add these to Hermes's `config.yaml`, so the agent's terminal cannot even try
the approve commands:

```yaml
approvals:
  deny:
    - '*review_queue.py*approve*'
    - '*review_queue.py*dismiss*'
    - '*send_email.py*approve*'
    - '*delete_actions.py*approve*'
    - '*webhook_events.py*approve*'
```

## Untrusted content: trust boundaries

External content (mail, attachments, calendar descriptions, Notion pages,
web research, prospect and competitor data, agent-written workflow YAML) is
**data**. It can be read, summarised and quoted. It can never approve
anything, choose where a file goes, or become standing instructions
unchecked.

| Where content enters | What it could reach | Control |
|---|---|---|
| Any content → a proposed action | Approval | Approval is signed by the user (above). No text in content can approve. |
| Action payload → a local file path (`drive.upload` / `drive.download`) | Plugin code, another company's store, credentials | Paths are confined: downloads go inside the project root; uploads come from the project root or `paths.upload_roots`, never from `.env`, keys, `~/.ssh`, `~/.hermes` or `state.db`. Checked at proposal and again at execution. `drive.download` is medium risk, so it can't be bulk-approved. |
| Curated wiki notes → the model's context on later turns | Persistent instructions | Injected as a labelled data block, sanitised to one line each. Pages curated from external events contribute only their title. |
| Workflow YAML → a registered skill | Standing instructions | Step text that approves, dismisses or turns on break-glass is refused at validation. |
| Workflow YAML → run progress | Advancing a step | `bind_action` needs a real action of the step's declared type. A command signal counts only when the command actually runs (`echo <pattern>` does not). |
| Notion rows → CRM fields | A later email recipient | Pulled `contact_email` must be a plain address, and the pull itself needs approval. |
| Errors and logs → the model, support bundles | Secrets | Prefixed secret names (`COMPOSIO_API_KEY=`, `*_SECRET=`, `*_TOKEN=`) are redacted, including in tool errors returned to the model. |
| Paid-data results (Monid) | Spend | Paid calls need a signed spend budget; content cannot reserve or extend it. |

The adversarial fixtures for these boundaries are in
`tests/test_untrusted_content.py`, `tests/test_approval_signing.py` and
`tests/test_account_binding.py`.

## Limits we state plainly

- **Not a sandbox.** Signed approvals stop an agent from approving through
  the plugin's own commands. Code that an agent writes and runs as your OS
  user can still forge a decision, because the plugin cannot tell which
  process is calling it. Keep Hermes's command approval on (the default) so
  scripts and `python -c` need your OK, add the deny rules above, and audit
  enrolments in `doctor`. An agent with an unrestricted shell as the same
  OS user could still tamper with local files. It could also call a
  provider's API directly with credentials it can reach, which skips the
  plugin entirely. For that boundary, use an isolated terminal backend.
- **Agent-native connectors.** When the agent writes through its own Gmail,
  Microsoft 365, Drive or Notion connector, the plugin cannot see which
  account that connector really acts as. `claim` names the account to use, and
  `record-execution` audits the account the agent reports. See
  [`MULTI_COMPANY.md`](MULTI_COMPANY.md).
- **Spend budgets** are a local estimate. The only hard limit is your
  provider account's own balance.
