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

```bash
python shared/scripts/chief_of_staff.py capabilities   # what may I do here?
python shared/scripts/chief_of_staff.py doctor --summary
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
| `.env` (plugin root) | Secrets only. Auto-loaded; shell env wins. **Never echo, never commit, never paste into chat.** |
| `paths.project_root` | The user's data: `pipeline.yaml`, `invoices.yaml`, `expenses.yaml`, `todos.yaml`, `wiki/`. Plain YAML and Markdown. |
| `shared/scripts/` | Shared machinery — guardrails, state, providers, audit. |
| `skills/<name>/` | The nineteen skills, each with `SKILL.md` and its own `scripts/`. |

## Commands

Every CLI prints JSON by default and a human table under `--summary`. Parse the
JSON; show the user the summary.

```bash
chief_of_staff.py capabilities     # operating envelope (start here)
chief_of_staff.py daily            # the daily loop — read-only
chief_of_staff.py doctor           # health check
chief_of_staff.py readiness        # go/no-go per capability
chief_of_staff.py review           # review-queue summary
chief_of_staff.py pipeline         # CRM summary
chief_of_staff.py bookkeeper       # invoices, AR/AP
chief_of_staff.py knowledge        # memory + wiki
chief_of_staff.py smoke-test       # subsystem check
chief_of_staff.py demo             # sample data, no credentials
chief_of_staff.py logs diagnose --latest-failed
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
python skills/daily-briefing/scripts/daily_briefing.py --input /path/to/envelope.json
```

The same `--input` path works for weekly-review and meeting-prep. Under the
other providers (`google_api`, `composio`, `m365`) the Python client fetches and
you do not need to supply anything.

## Executing an approved action

Two paths, one state machine. Which one applies depends on who performs the
mutation.

**Python path** — the guarded client does the work:

```bash
review_queue.py approve --action-id <id> --approver "<who>" --reason "<why>"
review_queue.py execute --action-id <id>
```

**Agent path** — you do the work with your own connector tools:

```bash
review_queue.py approve --action-id <id> --approver "<who>" --reason "<why>"
review_queue.py claim   --action-id <id>      # returns what to execute
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

An action left in `executing` is a stuck action; `doctor` reports it. Never
abandon a claim silently.

## Hosted cloud sessions

If `CLAUDE_CODE_REMOTE_SESSION_ID` is set you are on an ephemeral cloud VM.
Two things change, and `capabilities` will tell you both:

- **Credential-holding providers are refused** (`google_api`, `m365`,
  `composio`). Environment variables there are stored in plain text and are
  readable by anyone who uses the environment, and there is no secrets store.
  Use the `agent` provider — it holds no credentials because your connectors do
  the I/O. Do not ask the user to paste a client secret into the environment.
- **State does not survive the session.** Anything written under
  `project_root` is lost at teardown. Say so before the user invests work in
  it, and commit anything worth keeping.

## Talking to the user

- Show the summary, not the JSON.
- When you propose an action, say what it will do, to whom, and how it is
  reversed — `review_queue.py preview --action-id <id>` gives you all three.
- Report refusals plainly, with the reason from `capabilities`. A refused
  capability is a fact about this installation, not a problem to solve.
- Never paste secrets, tokens, or message bodies into the conversation.
