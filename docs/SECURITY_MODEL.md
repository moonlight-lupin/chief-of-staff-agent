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
- **The break-glass switches stop counting as approval.**
  `CHIEF_OF_STAFF_AUTO_APPROVE` and `CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE`, and a
  "y" typed at a prompt, no longer let a write through. A guarded write runs
  only inside an *execution grant*, which is tied to one signed action that is
  executing, and covers only that action's own kind of write.

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

## Limits we state plainly

- **Not a sandbox.** Signed approvals stop an agent from approving through
  the plugin's own commands. An agent with an unrestricted shell as the same
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
