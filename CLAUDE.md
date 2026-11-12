# Chief of Staff — operating contract for agents

You are driving a tool that can read someone's mail and calendar, and — once
approved — send email and modify their workspace. Read this before running
anything.

This file is for agents operating the plugin. If you are here to *develop* it,
see [`CONTRIBUTING.md`](CONTRIBUTING.md).

## The one invariant

```
Observe → Understand → Suggest → Approve → Execute → Audit
```

These steps never collapse. A suggestion is not an approval; an approval is not
an execution. You may propose freely. You may not act on your own proposal.

**Never** set `CHIEF_OF_STAFF_AUTO_APPROVE=1` or
`CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1` to get past a block. They are operator
break-glass switches, not a workaround. If an action is blocked, the correct
response is to explain the block to the user, not to route around it.

**Never** invent an action ID. The guardrail is default-deny: an unknown ID is
blocked, and inventing one to match an allowlist is a bypass attempt.

**Never** call your own connector write tools (Gmail send, Outlook move, Drive
delete) for a Chief-of-Staff action without claiming it first. See "Executing"
below. Reads with your own tools are expected and encouraged; writes are not.

## Start here

Run every plugin command with the plugin venv interpreter (`.venv/bin/python`), not bare `python`.

```bash
.venv/bin/python shared/scripts/chief_of_staff.py capabilities   # what may I do here?
.venv/bin/python shared/scripts/chief_of_staff.py doctor --summary
```

`capabilities` is the one call that tells you your operating envelope: the
active workspace provider, which actions it supports, which it refuses **and
why**, whether that provider has ever been live-verified, where state lives,
and whether that state survives the session. Run it before planning work.
Do not infer any of this from documentation — the report is generated from the
same tables the code enforces.

If `doctor` fails, run `chief_of_staff.py logs diagnose --latest-failed` before
guessing. Failures are classified deterministically and come with the exact
remediation command.

## Layout

| Path | What it is |
|---|---|
| `shared/config/company.yaml` | The single config file. Not secret — safe to read and show. |
| `.env` (plugin root) | Secrets only, standalone or Claude Code. Auto-loaded; shell env wins. **Never read under Hermes**: there, secrets come from the plugin's Hermes settings. **Never echo, never commit, never paste into chat.** |
| `paths.project_root` | The user's data: `pipeline.yaml`, `invoices.yaml`, `expenses.yaml`, `todos.yaml`, `wiki/`. Plain YAML and Markdown. |
| `shared/scripts/` | Shared machinery — guardrails, state, providers, audit. |
| `skills/<name>/` | The skills, each with `SKILL.md` and its own `scripts/`. Notion skills appear only when `notion:` is configured. |

## Scheduled / headless runs

A scheduled run should read `capabilities` output (from its own invocation; cached from the previous run if unchanged is fine to trust) and the relevant `skills/<name>/SKILL.md` only.

Do not re-read doctor output, CHANGELOG.md, or the full CLAUDE.md on every run. `doctor` is for interactive troubleshooting or when a command fails.

If `capabilities` provider/state lines are unchanged from the last recorded run, skip re-orientation and go straight to the scheduled work.

Cron prompts outlive releases. Reference plugin scripts by their current path
(`skills/<name>/scripts/...`) and plugin skills as `chief-of-staff:<name>`
only when `skills/<name>/` exists; your own agent skills take no
`chief-of-staff:` prefix. After an upgrade, `doctor`'s `cron_prompts` check
lists any job whose prompt points at a path or skill that no longer exists.
It never edits a job. Workflows installed with `workflows install` are bound
by the plugin and checked by `cron_skill_files`.

## Commands

Every CLI prints JSON by default and a human table under `--summary`. Parse the
JSON; show the user the summary.

```bash
chief_of_staff.py capabilities     # operating envelope (start here)
chief_of_staff.py --group <g> group-summary   # read-only group roll-up
spend.py status                    # budgets, spend, remaining
chief_of_staff.py daily            # the daily loop — read-only
chief_of_staff.py doctor           # health check
chief_of_staff.py readiness        # go/no-go per capability
chief_of_staff.py review           # review-queue summary
chief_of_staff.py pipeline         # CRM summary
chief_of_staff.py bookkeeper       # invoices, AR/AP
chief_of_staff.py knowledge        # memory + wiki
chief_of_staff.py smoke-test       # subsystem check
chief_of_staff.py demo             # sample data, no credentials
chief_of_staff.py demo --flow      # the whole loop, simulated: propose → signed approve → execute → audit
chief_of_staff.py logs diagnose --latest-failed
chief_of_staff.py sync status      # git-backed project state (cloud sessions)
chief_of_staff.py sync push        # checkpoint state.db, commit, push the data repo
```

`daily` is externally read-only: it reports and recommends, it never acts. It
writes one local `.last_briefing` timestamp to avoid duplicate briefings, and
nothing else. If you observe it mutating a workspace, stop and report it.

## Reading a workspace

Under the `agent` provider there is no Python API client — **you** are the
fetcher. Use your own connector tools (Gmail, Google Calendar, Drive, Outlook,
OneDrive), normalize the records to the shapes in
`shared/scripts/schemas.py`, write a JSON envelope, and pass it in:

```bash
.venv/bin/python skills/daily-briefing/scripts/daily_briefing.py --input /path/to/envelope.json
```

The same `--input` path works for weekly-review and meeting-prep. Under the
other providers (`google_api`, `composio`, `m365`) the Python client fetches and
you do not need to supply anything.

## Executing an approved action

Two paths, one state machine. Which one applies depends on who performs the
mutation.

Approving is the **user's** step, never yours. The commands below are what
the operator runs at their own terminal. Under Hermes, approvals are signed
in the gateway: `review_queue.py approve` is refused for an enrolled store,
and the user approves with `/<assistant name> approve <id>`
([`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md)).

**Python path** — the guarded client does the work:

```bash
review_queue.py approve --action-id <id> --approver "<who>" --reason "<why>"
review_queue.py execute --action-id <id>
```

**Agent path** — you do the work with your own connector tools:

```bash
review_queue.py approve --action-id <id> --approver "<who>" --reason "<why>"
review_queue.py claim   --action-id <id> --executor-account <account>   # returns what to execute
#   ... now perform exactly that action with your own tool ...
review_queue.py record-execution --action-id <id> --status success \
    --result-json '{"message_id": "..."}' --executor "claude"
```

`claim` comes **before** the action, not after. It is where a lapsed approval
is caught — catching that after the mail has already gone out is worthless —
and it makes the claim exclusive so two agents cannot both act. If `claim`
fails, do not perform the action.

If the action fails, close the loop honestly:

```bash
review_queue.py record-execution --action-id <id> --status failure --error "..."
```

A failed send, draft, calendar create, upload or contact create is final: it
may already have happened, so it never returns to `approved`. Check the
provider (e.g. Sent mail), then queue a new action if it really did not run.

An action left in `executing` is a stuck action; `doctor` reports it, and
`doctor --fix` closes it out as `failed` — never back to `approved`, because it
may already have run. Check the provider (e.g. Sent mail) before queueing it
again. Never abandon a claim silently.

An approval covers exactly what was approved. If an action's type, target or
payload changes after approval, `claim` and `execute` refuse it — queue and
approve a new one.

## Multiple companies

If `capabilities` shows a `Company` line, this install serves several
companies ([`docs/MULTI_COMPANY.md`](docs/MULTI_COMPANY.md)). The process acts
for exactly one scope: `--company <slug>` or `--group <slug>`.

- **Never switch scope to get past a refusal.** An action belongs to the
  company or group it was created in. A claim from any other scope is refused
  as `company_mismatch`, and that is correct.
- **Act from the declared account.** Pass the account your connector is
  signed in to on `claim --executor-account <account>`. When the company
  declares one, `claim` requires it and refuses a mismatch before anything
  runs. Pass it again on `record-execution --executor-account`. If your
  connector is signed in to a different account, stop and tell the user.
  Do not perform the action.
- **Sister-company data is read-only**, and only for domains the group
  shares. Never write one company's records into another company's store.
- Only the approvers listed for a group may approve group actions. Never
  put a listed approver's name on an approval they did not give.

## Paid data calls

Paid endpoints (e.g. Monid runs) are spent only against an **approved spend
envelope** ([`docs/MULTI_COMPANY.md`](docs/MULTI_COMPANY.md)). Run
`spend.py reserve` **before** every paid call and `spend.py record` after. If
`reserve` refuses (exit 3), do not make the call: tell the user why, and
propose a new envelope only if they want to continue. Never reuse a topic for
a different purpose to stay inside a budget. Free discovery and price checks
need no envelope.

## Hosted cloud sessions

If `CLAUDE_CODE_REMOTE_SESSION_ID` is set you are on an ephemeral cloud VM.
Two things change, and `capabilities` will tell you both:

- **Credential-holding providers are refused** (`google_api`, `m365`,
  `composio`). Environment variables there are stored in plain text and are
  readable by anyone who uses the environment, and there is no secrets store.
  Use the `agent` provider — it holds no credentials because your connectors do
  the I/O. Do not ask the user to paste a client secret into the environment.
- **State does not survive the session unless it is git-backed.** Anything
  written under `project_root` is lost at teardown. When the SessionStart hook
  has linked a private data repo (`capabilities` → `state_sync.git_backed`),
  run `chief_of_staff.py sync push` after changing state and before you stop.
  The Stop hook will block if you forget. Without a data repo, say so before
  the user invests work, and point them at [`docs/CLAUDE_CODE.md`](docs/CLAUDE_CODE.md).
- **Git storage is the user's choice, not yours.** When onboarding, ask
  whether they want their data in a private git repo (needed to keep it
  across cloud sessions) or as local files. Then pass `--storage git
  --data-repo <owner/name>` or `--storage local` to `bootstrap.py`. Never
  create the data repo or choose its visibility for them.
- **Never commit Chief-of-Staff data into this repository.** It may be public.
  Data goes only to the private data repo, through `sync`, and never with
  `git add` in the plugin checkout.

## Under Hermes: tools and the slash command

Under Hermes you have read-only tools: `cos_capabilities`, `cos_daily`,
`cos_review_queue`, `cos_preview`, `cos_pipeline` and `cos_spend_status`. Prefer
them to running the scripts. None of them can approve anything. Approvals are
signed in the Hermes gateway, so `review_queue.py approve` and the break-glass
env flags do not work here: propose, preview, and ask. When an action
needs a decision, show the preview and tell the user to run
`/<assistant name> approve <id>` (or `/cos approve <id>`). Never type that
command for them, and never run `review_queue.py approve` yourself.

## Talking to the user

- Show the summary, not the JSON.
- When you propose an action, say what it will do, to whom, and how it is
  reversed — `review_queue.py preview --action-id <id>` gives you all three.
- Report refusals plainly, with the reason from `capabilities`. A refused
  capability is a fact about this installation, not a problem to solve.
- Never paste secrets, tokens, or message bodies into the conversation.
