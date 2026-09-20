---
name: workflow-architect
description: "Use when capturing a repeating business process as a declarative workflow YAML draft via a structured interview, then writing it to the project after operator confirmation."
version: 0.1.0
author: moonlight-lupin
license: Apache-2.0
metadata:
  hermes:
    tags: [chief-of-staff, workflow, architect, interview, yaml]
    related_skills: [daily-briefing, weekly-review, todo-list]
---

# Workflow Architect

## Overview

Workflow Architect captures a repeating business process in conversation and proposes a complete workflow YAML draft. After the operator confirms the proposal, it writes that draft to `<project_root>/workflows/<name>.yaml`.

This skill does **not** install the generated overlay skill, does **not** register cron, and does **not** propose or execute connector actions. Those happen later, through the existing `workflows install` path and the review queue.

The YAML write is a **project-local artifact write**, the same class of edit as `pipeline.yaml` or `todos.yaml`. It is **out of scope** of action-specific review-queue approval. The review queue governs connector mutations (email, Drive, calendar, workspace). Capturing a workflow never mutates a workspace.

## When to Use

Use this skill when the operator asks to:

- "Capture this process as a workflow"
- "Turn this recurring process into YAML"
- "Design a workflow for the weekly close / status nudge / …"
- Walk a structured interview that produces `workflows/<name>.yaml`

Do **not** use this skill to start, advance, or abort a run. Do not use it to send mail, move files, or create calendar events. Do not call `workflows install` until the operator has confirmed the YAML and asked for install.

## Structured interview

Conduct the interview in this order. Do not invent a step, trigger, or delivery target the operator did not supply.

1. **Name.** Lowercase kebab-case, at most 32 characters, filesystem-safe (`[a-z0-9]` with optional interior hyphens). Refuse `../`, absolute paths, underscores, spaces, and uppercase.
2. **Trigger.** Required for capture even though the schema treats `triggers` as optional. Collect at least one of:
   - `message`: explicit invocation phrases such as `run weekly-close`
   - `schedule`: cron expression plus timezone
3. **Steps.** A non-empty linear list. Each step needs `id`, `name`, and `description`. Completion signals:
   - `command` — `{pattern: "…"}` matches a successful terminal call
   - `file` — `{path: "relative/path.md"}` inside the project
   - `review_queue` — `{action_type: "gmail.send"}` (approval-gated; the agent proposes later)
   - `manual` — operator/agent advances with `workflows advance`
   A step with no command/file/review_queue signature **defaults to manual**.
4. **Inputs.** What the run needs (folded into the workflow `description`; not a YAML key).
5. **Outputs.** What the run produces (also folded into `description`; not a YAML key).
6. **Delivery.** Channel plus target (interview field `delivery_target` → YAML `delivery`). Both must be non-empty strings, e.g. `{channel: briefing, target: operator}`.
7. **Failure policy.** `on_failure` (typically `halt` or `skip`). If a `step` is named, it must be one of the interview step ids.

Interview fields `trigger`, `delivery_target`, `inputs`, and `outputs` are **not** top-level YAML keys. Map them:

| Interview | YAML |
|---|---|
| `name` | `name` |
| `trigger` | `triggers` |
| `steps` | `steps` (unsigned → `manual: true`) |
| `inputs` / `outputs` | absorbed into `description` |
| `delivery_target` | `delivery` |
| `failure_policy` | `failure_policy` |

## Propose, then confirm, then write

Call the skill script with the plugin venv interpreter (`.venv/bin/python`), never bare `python`.

```bash
.venv/bin/python skills/workflow-architect/scripts/workflow_architect.py propose \
  --answers-json /path/to/answers.json \
  --config /path/to/company.yaml
```

`propose_workflow(answers, config, now=None)` returns a dict in the `workflows.py` loader shape. It does not write YAML. Invalid answers raise `WorkflowValidationError`.

Show the operator the draft: name, trigger, steps, delivery, failure policy. Say that confirming writes `<project_root>/workflows/<name>.yaml` and that this is visibility approval of an artifact write, **not review-queue approval** of any workspace action.

Only after the operator confirms:

```bash
.venv/bin/python skills/workflow-architect/scripts/workflow_architect.py propose \
  --answers-json /path/to/answers.json \
  --config /path/to/company.yaml \
  --write --confirm
```

`write_workflow(draft, config, confirm=False)` is a dry-run (returns the proposal, writes nothing). `confirm=True` persists the file. Invalid drafts are never written. The same answers with a frozen `now=` rewrite a byte-identical file (no timestamps in the body).

Do **not** invent an action id. Do **not** call `review_queue.py`. Do **not** send mail, touch Drive, or register cron from this skill.

## After confirmation

Install and listing are separate operator commands, not part of capture:

```bash
.venv/bin/python shared/scripts/chief_of_staff.py workflows install <name>
.venv/bin/python shared/scripts/chief_of_staff.py workflows list
```

`workflows install <name>` validates the YAML, writes `skills.local/<name>/SKILL.md`, and — if the workflow declares a schedule — proposes a `cron.create` review-queue action. That install step is out of this skill's scripts.

## Rules

- Observe → Understand → Suggest → Approve → Execute → Audit never collapses: a proposal is not a write.
- Confirm-before-write is this skill's own confirmation of a project-local YAML artifact, not a review-queue connector mutation.
- Name length ≤ 32; step id/name bounds follow `workflows.py`.
- Relative file paths only; refuse path escapes.
- Unsigned steps default to `manual`.
- `review_queue` steps cannot opt out of approval; the agent proposes the bound action later, at run time.
- No PII, secrets, tokens, or message bodies in the YAML or in chat.

## Verification checklist

- [ ] Interview covered trigger, steps, inputs, outputs, delivery, and failure policy.
- [ ] Draft validates (`validate_workflow`) before any write.
- [ ] Operator confirmed the proposal before `write_workflow(..., confirm=True)`.
- [ ] File landed at `<project_root>/workflows/<name>.yaml` only.
- [ ] No overlay skill, cron job, or pending review-queue action was created by this skill.
- [ ] Operator was told the next commands: `workflows install <name>` and `chief_of_staff.py workflows list`.
