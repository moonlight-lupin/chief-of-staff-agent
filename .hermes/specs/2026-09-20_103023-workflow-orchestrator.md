# Workflow Orchestrator Specification (v3.1 — post review round 3 residuals)

**Goal:** Let any CoS user capture a repeating business process as a declarative workflow, get a matching skill generated from it, and have every run oriented deterministically — step pointer and availability verdict — via hooks. Hooks replace LLM round-trips; they do not enforce execution. Enforcement stays with the existing guardrails.

**Revision note:** v2 folded in all round-1 findings (Codex 12 MAJOR/2 MINOR; Claude Code 6 blocking MAJOR). v3 folds in round-2 findings (Muse 6 MAJOR/3 MINOR/1 NIT): approval-step actor named (agent proposes, run-record state + bind-time overwrite refusal enforces single-proposal, `workflows bind-action`); preflight facts via `workflow_facts` kv doc + `workflows refresh-facts` with freshness rule (daily explicitly NOT a fact writer); skip semantics per-step required/optional (no cascade anywhere; terminal `completed (degraded)`); review-queue signals re-observed every pre_llm_call + `workflows sync` (one-shot events no longer depend on repeat tool events; advancement hook's review-queue read named as a local read); headless approval wakeup via next cron occurrence + max-3-no-progress cap; occurrence dedup vs wakeup distinguished by `last_progress_at`; YAML/skill writes justified as project-local artifact writes; dedup check inside mutate_kv callback with named key (idempotency from the in-callback check, not atomicity); schema name-length bounds making the 200-char cap provable; hook failure counters best-effort. Remaining open questions (3) do not block planning per round-2 review.

## Background

Brainstorm outcome (2026-09-20): Option A (workflow-architect skill, YAML as source of truth) + Option C (hook-only orchestration, state in kv_stores). Option B (declarative Python runner) is rejected — a second execution path the agent-composed loop never calls (v0.5.x live-data lesson).

Operator decisions (verbatim intent):
1. Sample reference workflows + their generated skills ship as packaged examples.
2. Plugin-level concept for all CoS users (machinery + examples are plugin-level; workflow data is per-project).
3. Both trigger models at v1: explicit invocation phrase (message) and per-workflow cron schedule.

## Vocabulary

| Term | Meaning |
|---|---|
| **Workflow** | A YAML file declaring a linear sequence of steps, triggers, delivery, and failure policy. Single source of truth. |
| **Run** | One execution instance of a workflow. Identified by `workflow_run_id` (distinct from `runtime_log`'s `run_id`, which this feature must NOT reuse). |
| **Run definition** | The immutable snapshot of execution-relevant fields (steps, signals, guardrails, delivery, failure policy) taken from the YAML at run start. Mid-run YAML edits never affect an active run; doctor flags the drift for operator decision. |
| **Step** | One unit of work in a run. Steps are executed by the agent (LLM) with its normal tools — hooks never execute steps. |
| **Completion signal** | The declared evidence that a step finished successfully. Four kinds (see US-7). |
| **Verdict** | GO / DEGRADED / HALT — the hook's deterministic local assessment injected before the LLM acts. |
| **Preflight** | The verdict computation. A bounded local assessment of provider support, state-file presence, staleness, and approval state. It is NOT a live availability check and does not prove connectivity or authorization. |

## User Stories

### US-1: Capture a business process as a workflow
As a CoS operator, I want to describe a repeating business process in conversation, so that it becomes a reusable declarative workflow without me writing YAML.
**Acceptance criteria:**
- [ ] The workflow-architect skill conducts a structured interview (trigger, steps, inputs, outputs, delivery target, failure policy) and proposes a complete workflow YAML draft
- [ ] The draft is written to `<project_root>/workflows/<name>.yaml` only after the operator confirms the proposal in conversation (the architect skill's own confirmation; this is visibility approval of the artifact, NOT approval of any workspace action — mutations always need their own review-queue approval)
- [ ] The generated YAML validates against the schema; invalid YAML is never written
- [ ] An operator can hand-edit or hand-create the YAML file directly and it works identically (YAML is the single source of truth)
- [ ] Workflow YAML and generated-skill writes are PROJECT-LOCAL artifact writes and are intentionally OUT of scope of action-specific review-queue approval — consistent with existing practice (pipeline.yaml, todos.yaml, company.yaml are edited outside the review queue today, guarded by the yaml_integrity_checker hook). The review queue governs CONNECTOR mutations (email, Drive, calendar, workspace). The architect's conversational confirmation covers the artifact write; no workspace mutation ever results from capturing a workflow itself

### US-2: Generate a skill from a workflow
As a CoS operator, I want a matching skill generated from each workflow, so that normal skill loading surfaces the process.
**Acceptance criteria:**
- [ ] A generator produces a SKILL.md derived from the workflow YAML (steps, guardrails, delivery, degradation notes)
- [ ] Generated skills live in the existing `skills.local/` overlay (`skills.local/<workflow-name>/SKILL.md`) — never inside the git-tracked `skills/` tree (prevents polluting the shipped plugin and overwriting bundled skills)
- [ ] `_get_registered_skills` gains a discovery pass over `skills.local/` so a newly generated workflow skill is registered without hand-editing plugin.yaml (test: generate → register → visible)
- [ ] Generated files carry a header comment with source YAML path + a regeneration command
- [ ] Generation is deterministic: identical YAML input produces byte-identical output (no generation timestamps in the body; provenance lives in the header)
- [ ] Regeneration refuses to overwrite a generated file whose body no longer matches the YAML-derived output (operator hand-edit detected); the operator must pass an explicit acknowledge-overwrite flag — edited markdown never becomes authoritative; YAML stays the single source of truth
- [ ] A workflow name that shadows an existing bundled skill is refused at generation time

### US-3: Sample workflows ship with the plugin
As a new CoS user, I want sample reference workflows + their generated skills in the box, so that I understand the format before capturing my own.
**Acceptance criteria:**
- [ ] Samples live under `examples/workflows/` (NOT the existing `examples/` demo-data tree, which stays untouched) — at least one complete sample YAML demonstrating: multi-step flow, an approval-required step, a delivery step, and a degradation path
- [ ] The sample's generated SKILL.md is NOT committed; the round-trip test renders it to a temp dir and asserts byte-identity against a stored expectation file
- [ ] Tests cover the sample: schema validation + generation round-trip
- [ ] `chief_of_staff.py demo` behavior is unchanged by this feature (samples are inert data, not demo inputs — unless a later revision explicitly adds a demo workflow)

### US-4: Orchestrated execution with step pointer
As a CoS operator running a workflow, I want the agent told deterministically where we are, so that the LLM never has to remember position.
**Acceptance criteria:**
- [ ] One kv store document `workflow_runs` (single JSON doc via the existing `__root__` KV API; all mutations through `mutate_kv`). Idempotency and no-concurrent-run come from the dedup check running INSIDE the mutation callback (check-and-insert is race-free, not because atomicity alone provides it)
- [ ] The document holds runs keyed by `workflow_run_id`; each run record carries: workflow name, immutable run definition, current step index, per-step status (pending/awaiting-approval/skipped/completed/failed), trigger source (message/cron), started-at, `last_progress_at` (updated on every step advance — consumed by US-9's dedup/wakeup distinction), owning session id
- [ ] A single combined `pre_llm_call` injection (pointer + verdict in one strip): workflow, step n/N, last completed step, next expected action with inline gate marker, and the GO/DEGRADED/HALT verdict
- [ ] Injection gate: an ACTIVE run in kv state AND the session that owns it (or explicitly resumed it) — sole trigger. No active run → zero injection. No "workflow skill loaded" clause (the runtime does not pass loaded_skills; `_cos_skills_loaded` is not a discriminating gate — recorded as a constraint)
- [ ] Unrelated sessions never receive another run's strip and never advance another run's pointer (session/run binding is part of the injection and advancement match)
- [ ] The strip carries an inline gate marker for approval-required steps: `next: step 4 [APPROVAL REQUIRED — propose, do not execute]` — an approval-required step is never rendered as a bare imperative (hook test asserts this)

### US-5: Graceful degradation before every workflow step
As a CoS operator, I want availability assessed deterministically before the LLM acts, so that failures are cheap verdicts instead of wasted LLM calls.
**Acceptance criteria:**
- [ ] The verdict is a bounded local assessment derived from the same facts `capabilities` reports (provider support table, state-file presence, staleness timestamps, pending approval state) — GO means "no local reason to expect failure", NOT live connectivity proof
- [ ] Preflight fact source: the facts are read from a `workflow_facts` kv document, refreshed by `workflows refresh-facts` (invoked at run start, at resume, and by install-cron — no new network calls; it caches the capabilities report + state-file mtimes + review-queue counts into kv). Freshness rule: a fact older than the staleness threshold (default 1h for facts, distinct from the 48h run-staleness) makes the verdict DEGRADED (facts stale), never GO. Hook reads: the pointer hook reads ONLY kv state (workflow_runs + workflow_facts); the advancement hook additionally reads the review-queue store when the current step's signal is review_queue (named local reads, both sqlite on the same host — no network in either path). Writers of workflow_facts: `workflows refresh-facts` (facts section) and the hook failure counter (US-11, best-effort, own section of the same document); no other writers. `daily` is NOT a fact writer (its externally-read-only contract is untouched)
- [ ] Verdict rules: required capability missing → HALT (name it); optional capability missing → DEGRADED with skip-list of affected steps; state file absent → DEGRADED; facts stale/unknown → DEGRADED, never GO
- [ ] DEGRADED strips name affected steps so the model skips without guessing; skipped steps are marked `skipped` in the run record (they do not silently await a signal forever)
- [ ] HALT cases instruct the model to stop and surface the blocker to the operator; HALT is resumable (operator fixes cause, run resumes at the same step)
- [ ] Skip semantics: each step in the YAML declares `required: true|false` (default true). A DEGRADED verdict skips OPTIONAL steps whose inputs are affected; REQUIRED steps are never skipped by preflight — their absence blocks with HALT (operator decision), not skip. No blanket forward cascade: only explicitly optional steps may be skipped. An all-optional run whose steps are all skipped terminates `completed (degraded)` (terminal state recorded with the degraded flag); a run with any HALT stays at the blocking step

### US-6: Explicit workflow invocation
As a CoS operator, I want to start a workflow explicitly by name, so that keyword-guessing never starts the wrong process.
**Acceptance criteria:**
- [ ] Message-driven triggering requires an explicit invocation phrase ("run <workflow-name>", "start <workflow-name>") — no fuzzy keyword routing starts a run
- [ ] Starting a run creates the run record with an immutable run-definition snapshot and records trigger source + started-at
- [ ] A run cannot be started twice concurrently: enforced inside the `mutate_kv` callback — the check (active-run lookup by workflow name) and the insert happen inside the SAME mutation function, so the atomic write path makes the check-and-insert race-free. Dedup key: workflow name → active run id. A second start is rejected with a pointer to the active `workflow_run_id` (test: simultaneous starts, one wins)
- [ ] Another session may resume an existing run explicitly (`workflows resume <run-id>`, rebinds the session); implicit cross-session advancement is refused
- [ ] Hosted cloud sessions (`CLAUDE_CODE_REMOTE_SESSION_ID` set): the feature is inert-with-warning — a run may be viewed but not started (state does not survive teardown); the invocation reply states this
- [ ] Cron-driven triggering: per-workflow `schedule` field in YAML (cron expr + timezone, default operator-local); lifecycle in US-9

### US-7: Step advancement from observable completions
As the orchestrator, I want workflow state advanced when a step's successful completion is observable, so that the pointer stays true without an LLM bookkeeping call.
**Acceptance criteria:**
- [ ] Exactly four completion signal types at v1, each requiring successful, fresh (after run start), run-associated evidence:
  1. `command`: the observed `terminal` tool call matches the step's command pattern AND exited successfully
  2. `file`: the declared file exists with mtime after the run's started-at
  3. `review_queue`: the bound review-queue action reached `executed` with status success via `record-execution` (proposed or approved is NOT complete; see US-8)
  4. `manual`: no observable signal — the operator (or the agent at the operator's direction) advances explicitly via `workflows advance` (fallback for connector-executed steps under the agent provider, and for conversational steps)
- [ ] Out-of-order, duplicate, delayed, and failed events never advance the pointer (freshness + pattern + idempotency tests); the same completion event never advances two steps
- [ ] The pointer never advances past a step whose approval gate is unsatisfied (US-8)
- [ ] The hook performs at most one bounded state write per event, with a short busy_timeout; on lock contention it drops the advance (doctor's staleness check catches the gap) rather than blocking the tool call
- [ ] Audit entries for hook-driven writes use `actor: "hook:workflow-orchestrator"` and carry `workflow_run_id` (no field collision with runtime_log run ids)
- [ ] Model-only steps that produce no tool event are declared `manual` at capture time (the architect skill defaults any step without a command/file/review-queue signature to `manual`)

### US-8: Approval-gated steps bind to the review queue
As a CoS operator, I want approval steps to follow the existing action lifecycle exactly, so that workflow approval never bypasses Observe→Approve→Execute.
**Acceptance criteria:**
- [ ] A step with `requires_approval: true` declares an action type. THE AGENT creates the pending action (following the injected strip's instruction at that step) via the standard propose path; "exactly one" is enforced by the run record: the step's entry in the run snapshot carries an `action_id` field that starts empty, and the strip instructs "propose the action and record its ID via `workflows bind-action`". A step that already has a bound action_id must NOT propose again (the strip says "action <id> already bound — await approval"). Duplicate proposals are thus prevented by run-record state, not by convention — NOTE: the refusal is advisory at the strip level; a second propose attempt with a different ID fails validation at `bind-action` only if the operator/agent tries to overwrite an existing bound ID (overwrite refused without an explicit unbind by the operator). Enforcement is bind-time, consistent with the advisory-hooks convention
- [ ] `workflows bind-action <run-id> <step> <action-id>` writes the ID into the run record inside the mutate_kv callback; it validates the action exists and is pending (binding an unknown/executed ID is refused — no invented IDs)
- [ ] Advancement on a review-queue signal requires `executed` + status success for THAT exact bound action ID; unknown/expired/failed-claim IDs never advance (the hook only matches the stored ID — never invents one)
- [ ] Run-level approval, YAML approval, or invocation never authorizes a mutation; every mutation keeps its own action-specific approval + execute/claim/record-execution lifecycle regardless of `approval-required` metadata
- [ ] The injected strip for an approval-wait step names the action ID and the literal approve command
- [ ] Cross-session and one-shot advancement: the advancement hook is EVENT-DRIVEN for command/file signals, but review-queue signals are RE-OBSERVED — every pre_llm_call in the owning session re-checks the bound action's current state in the review queue (a local read), so a `record-execution` that landed in another session (or an earlier-dropped advance) is caught on the next model turn of the owning session without needing a repeat tool event. Additionally `workflows sync <run-id>` (operator/agent command) forces an immediate re-observation pass
- [ ] Python-path execution also satisfies the signal: the hook matches the action's terminal state in the review queue store (`executed` + success outcome), which both the Python path (approve→execute) and the agent path (claim→perform→record-execution) produce — the hook reads the store, not the invocation path
- [ ] A run-level review-queue proposal (visibility only) may be created at run start if the workflow declares `run_log_delivery`; it authorizes nothing

### US-9: Cron triggering lifecycle
As a CoS operator, I want a workflow's cron schedule to be real infrastructure, so that scheduled runs actually happen and stay correct through edits.
**Acceptance criteria:**
- [ ] `workflows install-cron <name>` creates the `hermes cron` job (prompt: load the generated skill + run the workflow); `workflows uninstall-cron <name>` removes it; editing the YAML's schedule requires re-install (the command states this)
- [ ] Cron job creation is itself a review-queue action (visibility + operator approval), not a hook side effect
- [ ] Occurrence dedup: a scheduled occurrence whose run is ACTIVE AND PROGRESSING (advanced since the previous occurrence) is skipped and logged. A scheduled occurrence whose active run is PARKED at `awaiting-approval` follows the wakeup path below. These two cases are distinguished by the run record's `last_progress_at` vs the previous occurrence's timestamp (test: both paths)
- [ ] Timezone comes from the YAML schedule field; doctor warns when a workflow declares a schedule with no matching cron job, and when a cron job references a workflow that no longer exists
- [ ] Headless (cron) runs follow the same execution path: load generated skill, start run, execute steps as agent work. A headless run that parks at `awaiting-approval` REPORTS THE WAIT to the operator's delivery target and EXITS (bounded session lifetime — no live wait); it is never silently held. Wakeup: the next cron occurrence checks first whether the workflow already has an active parked run — if the approval has since landed, the occurrence runs `workflows sync <run-id>` (re-observes, advances past the approved step) and the run continues in that occurrence; if the approval is still missing it re-reports the wait (with occurrence count) and exits again. An operator can always break the loop with `workflows resume`/`abort`. Max 3 consecutive no-progress occurrences before the run is flagged failed-with-blocker by doctor (never silently looping)

### US-10: Run lifecycle: complete, abort, staleness
As a CoS operator, I want runs to have a complete lifecycle, so that nothing stalls silently.
**Acceptance criteria:**
- [ ] States: `running` → (`awaiting-approval` ⇄) → `running` → terminal (`completed` | `aborted` | `failed`); DEGRADED skips mark steps `skipped` and continue
- [ ] Reaching the final step completes the run: the run record moves to `completed`, is archived (retention: last 10 completed runs per workflow), and injection returns to zero tokens
- [ ] `workflows abort <run-id>` (operator command) aborts an active run in any state; the owning session's strip reports the abort once, then silence
- [ ] A run stuck on one step beyond a configurable staleness threshold (default 48h) is flagged by doctor (warn) with the step name and last-event age — never silently stalled
- [ ] Crash recovery: a `running` run whose owning session is gone is resumable via explicit `workflows resume <run-id>` (session rebinds); until then doctor flags it stale
- [ ] `doctor` adds a workflow check: YAML validity per file, orphaned runs (workflow deleted mid-run), stale runs, cron/workflow reconciliation (ties to US-9)

### US-11: Operator visibility
As a CoS operator, I want to see workflow runs, so that I can trust and debug the orchestration.
**Acceptance criteria:**
- [ ] `chief_of_staff.py workflows list` — workflows + validation status; `workflows runs` — active + recent runs with current step, state, staleness, trigger source
- [ ] JSON default output + human table under `--summary` (existing CLI convention)
- [ ] Audit trail: run start/advance/complete/abort/expire events recorded via the existing audit append with the `workflow_run_id` field; audit entries never carry message bodies or secrets (existing redaction convention)
- [ ] Schema bounds: workflow names and step names have a maximum length (workflow ≤32 chars, step ≤24 chars, enforced by the schema validator) so the 200-char strip cap is provable by construction; worst-case-name strip rendering is tested against the cap
- [ ] Hook failure counters are best-effort: failures increment a counter inside the `workflow_facts` document when the kv write succeeds; when the write itself is contended/failed the count is dropped (lost counts are acceptable — doctor's per-hook failure report is advisory, marked as such)

## Implementation Decisions (settled)

- YAML workflow file is the single source of truth; generated SKILL.md is a deterministic view. Generated files live in `skills.local/<workflow-name>/` (existing overlay), discovered by an added pass in `_get_registered_skills`.
- Orchestration is hook-only: `pre_llm_call` (one combined pointer+verdict strip) and `post_tool_call` (advancement). No new execution engine; the agent executes steps. Hooks are advisory — enforcement stays with guardrails; fail-soft (hook returns None on error) is unchanged, and doctor counts hook failures.
- v1 vocabulary is a bounded linear workflow language: steps in sequence, each depending on the previous; four completion signal types (command / file / review_queue / manual); skip semantics per US-5 (per-step required/optional; required steps block with HALT, never auto-skip). No parallel branches, no nesting, no conditionals at v1.
- Run state: single `workflow_runs` kv document, all mutations via `mutate_kv` (BEGIN IMMEDIATE — the check-and-insert inside the mutation function is race-free; idempotency comes from the in-callback dedup check, not from atomicity alone). The run-definition snapshot is immutable for the life of the run.
- Injection: ONE combined strip per LLM call, only when the calling session owns an active run; character cap ≤200 chars (≈50 tokens) with truncation order: drop last-completed timestamp → drop last-completed name → always keep step n/N + verdict + next action with gate marker. Worst-case names tested against the cap.
- Preflight is a bounded local assessment (no network, no live probes); GO is "no local reason to expect failure" — never a connectivity guarantee.
- The advancement hook is the plugin's first state-mutating hook: at most one bounded write per event, short busy_timeout, drop-don't-block under contention, distinct `actor` and `workflow_run_id` fields.
- Samples under `examples/workflows/` (existing `examples/` demo data untouched); generated sample skills are test-rendered, not committed.
- Both trigger models: explicit invocation phrase + cron schedule via `workflows install-cron` (itself a review-queue action).

## Testing Decisions

- Primary seam: schema validator + generator as pure functions; round-trip property `generate(schema_validate(load(x)))` is byte-stable; the sample workflow is tested (schema + round-trip against a stored expectation).
- Hook seam: fake ctx + real StateDB (temp file) — assert injection text, ≤200-char cap under worst-case names, zero-injection-on-miss, session binding (unrelated session gets nothing), gate-marker rendering.
- Advancement seam: synthetic tool events per signal type; freshness (pre-run file/event rejected), duplicate/out-of-order rejection, idempotency, approval-gate blocking, lock-contention drop path (contended-DB test).
- Lifecycle seam: start/complete/abort/resume/staleness transitions; concurrent starts (one wins via mutate_kv); crash-recovery resume.
- Runtime acceptance (beyond synthetic): one manual smoke run per trigger path (message + cron) on the live Phronesis install before release, verifying strip delivery and a manual-advance completion — recorded in the release checklist.
- Prior art: existing hook tests, doctor check tests, kv store tests, demo round-trip pattern.

## Non-Functional Requirements

- **Token economy:** one combined strip ≤200 chars per LLM call, only when the calling session owns an active run. Sessions with zero active runs see zero added tokens from this feature. Claims are measured: the release checklist records (a) strip char counts for the sample workflow, (b) a turns-saved estimate vs tokens-added per turn. Incremental cost is reported separately from the existing primer/wiki/deadline injections (unchanged by this feature).
- **Latency:** hooks add <50ms at p95 (workload: sample workflow, warm kv read; boundary: hook entry to return; the advancement write is bounded as specified and excluded from the pointer-hook path).
- **Security:** workflow YAML approval, invocation, and run-level proposals never authorize mutations; every mutation keeps the action-specific review-queue lifecycle. No new bypass paths; hooks never carry secrets; approval-required steps render with inline gate markers.
- **Reliability:** invalid/corrupt YAML disables that workflow (doctor warns with the parse error; active runs keep executing on their immutable snapshot, flagged "workflow modified/corrupt during run"); hooks fail soft; hosted cloud sessions: inert-with-warning.

## Constraints

- Fits existing architecture: ALL_HOOKS registration, kv_stores via mutate_kv, doctor checks, capabilities facts as preflight inputs, skills.local overlay, review-queue lifecycle, audit append convention.
- Advisory hooks (existing convention); the orchestrator orients, guardrails enforce.
- No Option B runner; no Python execution of steps.
- The runtime does not pass loaded_skills to hooks — gates must be determinable from kv state alone.

## Out of Scope

- Declarative Python step-runner (Option B).
- Parallel branches, conditionals, nested workflows at v1.
- Workflow versioning/migration beyond the immutable run snapshot.
- UI beyond CLI summary (no dashboard integration at v1).
- Cross-project workflow sharing beyond examples.
- Auto-generated cron edits on YAML save (explicit install/uninstall commands only).

## Edge Cases & Error Behavior

- Workflow YAML invalid → not listed/runnable; doctor warns with parse error; other workflows unaffected.
- Active run's workflow edited mid-run → run continues on its immutable snapshot; doctor flags drift; operator decides (keep going vs abort).
- Active run's workflow deleted mid-run → doctor flags orphaned run; operator aborts or completes manually.
- Hook failure → None (fail-soft), LLM proceeds un-oriented (same as today); doctor counts failures.
- Concurrent triggers → second rejected with pointer (mutate_kv).
- Completion signal never fires → staleness warn via doctor; operator advances manually or aborts.
- Lock contention on an advancement write → advance dropped for that event (idempotent retry on the next matching event); doctor staleness covers the gap.
- Empty workflows dir → feature inert, zero tokens, no errors.
- Hosted cloud session → view-only; start refused with explanation; no state written.
- Generated-skill name collides with a bundled skill → generation refused; architect proposes a different name.

## Open Questions

- [NEEDS CLARIFICATION: retention beyond "last 10 completed runs per workflow" — archive indefinitely, or prune audit-linked run records after N days?]
- [NEEDS CLARIFICATION: should `daily` surface an active workflow run as a briefing item, or stay silent about runs?]
- [NEEDS CLARIFICATION: staleness/skip thresholds (48h proposed) — per-workflow override in YAML at v1, or global config only?]