# Release Readiness Assessment — v0.3.24 (+36 unreleased commits)

**Assessed:** 2026-08-14 · **Branch:** `claude/production-release-readiness-miecj7` · **Scope:** everything except the native M365 Graph path (no Entra tenant available to verify).

---

## Part A — Release maturity

### Verdict

**The code is ready. The release is not.**

Every blocking and major engineering issue from `docs/PRODUCTION_ROADMAP.md` Phases 1–3 is closed and verified in the tree. What stands between this repo and a public v1.0 is release *hygiene*: the changelog stopped 36 commits ago, the roadmap still headlines "REQUEST CHANGES", there is no tag or release to install, and the flagship 60-second demo prints dates from five weeks ago.

Those are 1–2 days of work, not engineering. But shipped as-is, a first-time visitor reads the two most prominent documents in the repo and concludes the project is mid-remediation and unshippable.

| Dimension | Grade | Note |
|---|---|---|
| Correctness / test coverage | **A** | 1,950 tests, all pass on a clean interpreter |
| Safety model implementation | **A** | Default-deny guardrail, per-action grants, locked state, hash-chained audit |
| Concurrency + state integrity | **A−** | File locks on all four stores; fail-closed on corruption; retention capped |
| Observability / diagnosability | **A** | Correlation IDs, redacted JSONL, `logs diagnose`, support bundles |
| Install / first-run robustness | **C** | No dependency preflight; the most likely install failure produces a Rust panic traceback |
| Release artifacts + versioning | **D** | 0 tags, 0 GitHub releases, changelog 36 commits stale |
| Doc accuracy | **C** | Roadmap verdict stale, skill count wrong, demo data expired |
| Repo governance | **C** | No SECURITY.md, CONTRIBUTING.md, NOTICE, or issue templates |

### Evidence

**Test suite.** `python -m pytest` → **1,930 passed, 20 failed**. All 20 failures are environmental, not defects: this container ships a Debian `cryptography` 41.0.7 whose `_cffi_backend` is missing, so `google.auth.crypt` raises `pyo3_runtime.PanicException` on import. Re-run with a working `cryptography` on `PYTHONPATH`:

```
tests/test_jwt_crypto_v0202.py tests/test_webhook_hotfix_v0201.py tests/test_e2e_v022.py
→ 77 passed
```

**Effective result: 1,950/1,950 green.** CI (3.11 + 3.12 matrix) does not hit this because `actions/setup-python` provides a clean interpreter.

**Phase 1–3 remediation is real, not claimed.** Spot-verified against the source:

- **B1** — `workspace_guardrails.py` is default-deny (`READ_ACTIONS` allowlist, unknown IDs blocked, legacy `gmail.*`/`drive.*` canonicalized before classification, `"unknown"` deliberately excluded from reads).
- **B2** — `file_lock.with_lock` wraps load→check→write in `pending_actions.py`, `event_store.py`, `state_store.py`, and `webhook_security.py` (4 call sites).
- **Phase 3** — audit hash chain, `config.Settings` model, `cos_helpers` extraction, backup retention, stuck-action reconciliation all present.

**Lint.** `ruff check shared/ skills/ hooks.py` with error-class rules (`E9,F63,F7,F82`) → clean. Full default ruleset → **113 findings, 67 auto-fixable**, overwhelmingly unused imports (`F401`). CI runs ruff over `shared/scripts/` only, with four rules, under `continue-on-error: true` — so lint is currently decorative.

**Demo.** `chief_of_staff.py demo` runs clean with no config, exit 0. **Doctor** correctly fails-loud with no config and gives actionable per-check messages.

### Release blockers

**R1 — The CHANGELOG stops 36 commits before HEAD.** The newest entry is `v0.3.24 — Vendor deep-research v1.4.0`. Everything since — Phase 1 guardrail default-deny, Phase 2 transactional state, Phase 3 audit hash chain + config model, Phase 4A–4C knowledge/wiki work, deep-research v1.5.0 and v1.6.0, the `news-monitoring` skill — is undocumented. The entire safety-hardening programme, which is the project's strongest selling point, is invisible to anyone reading the changelog. Cut a `v0.4.0` entry covering it.

**R2 — `docs/PRODUCTION_ROADMAP.md` still says "REQUEST CHANGES — 3 blocking issues, 12 major issues."** Dated today, contradicted by the code. It should be restructured as a status document: Phases 1–3 → shipped, Phase 4 (M365) → the only open tier, blocked on tenant access. Right now it reads as a live indictment of the release you're about to make.

**R3 — No tags, no releases.** 24 versions in the changelog, `git tag` returns nothing, GitHub releases API returns `[]`. Users following the README's "clone the repo" instruction get whatever `main` happens to be. Tag `v0.4.0` and publish a release with the Part A summary as notes.

**R4 — No dependency preflight.** The failure I hit in this container is exactly what a user hits on a machine with a distro-managed `cryptography`, and it surfaces as `pyo3_runtime.PanicException: Python API call failed` with a Rust backtrace. `doctor` checks first-party imports (`briefing_sources`, `action_risk`, `pending_actions`, `memory`) but never `cryptography`, `msal`, `fitz`/pymupdf, `google.auth`, or `docx`. For a plugin whose headline install path is "paste this to your agent," the #1 install failure must produce a sentence, not a backtrace. Add a `doctor:dependencies` check with version floors, and a dependency-sanity step in CI (roadmap task 1.10, never done).

### Should fix before announcing

**S1 — Demo data has expired.** `examples/sample-workspace.json` hard-codes `2026-07-12`. The demo prints those events under the heading "Calendar / deadlines (next 48h)" — today is 2026-08-14. The 60-second first impression is visibly wrong and degrades every day. Generate the sample envelope relative to `today()`.

**S2 — Skill count is wrong in two places.** README says "Eighteen skills"; `plugin.yaml` registers 19 (`news-monitoring` landed in the last two commits). `doctor` reports "all 18 effective skills present" because `esign-connector` is filtered out when DocuSeal is unconfigured — so 18 is right for a default install and 19 is right for the manifest, and neither document says which it means.

**S3 — License hygiene for vendored work.** `skills/deep-research/SKILL.md` declares `license: MIT` inside an Apache-2.0 repo. The test was relaxed to allow it (Phase 1.9), which is the correct call, but there is no `NOTICE` file recording the upstream attribution.

**S4 — Governance files absent.** No `SECURITY.md` (this project handles mail, calendars, and credentials — it needs a disclosure address), no `CONTRIBUTING.md`, no issue or PR templates.

**S5 — Make lint mean something.** Fix the 67 auto-fixable findings, then drop `continue-on-error` and widen coverage to `skills/` and `hooks.py`. Roadmap task 2.13 also called for mypy; not done.

### Accepted for now

- **God files.** `chief_of_staff.py` (2,423 lines) and `composio_mcp_workspace_base.py` (1,980) remain large; roadmap 3.6 was partially done via the `cos_helpers` extraction. Not a release blocker.
- **M365 native path.** Phase 4 items — B3 tenant-wide authority, token cache, real reversibility, timezone/upload hardening — are open by design. Capability flags are honestly `False` and `workspace_capabilities.py` carries per-capability reasons, which is the right posture. **Recommendation:** have `readiness` print an explicit "native m365 is code-complete but never live-verified" banner so nobody points app-only `.default`-scoped credentials at a production tenant on the strength of the README table.
- **Webhooks.** Polling is the supported path; the Pub/Sub receiver is fine as an optional extra.

### Recommended release shape

Ship **v0.4.0** for `google_api` + `composio` (both Google and Microsoft), with native `m365` labelled *code-complete, unverified*. That matches the roadmap's own decision matrix and is defensible today.

Sequence: R1 → R2 → R4 → S1 → S2 → tag → R3. Roughly two days.

---

## Part B — Running this as a Claude Desktop / Cowork plugin

### The core mismatch

This is a **Hermes** plugin: `plugin.yaml`, `__init__.py:register(ctx)`, `ctx.register_skill()`, `ctx.register_hook()`, nine Python callbacks on `pre_llm_call` / `post_llm_call` / `pre_tool_call` / `post_tool_call` / `on_session_start`, state under `~/.hermes/projects/<slug>`, and cron installed into the system crontab.

Claude Code plugins — which is what Claude Desktop and Cowork consume — use `.claude-plugin/plugin.json`, auto-scanned `skills/<name>/SKILL.md`, and `hooks/hooks.json` declaring **subprocess** hooks on a different event vocabulary.

The good news: **the skills layer is already compatible.** `skills/<name>/SKILL.md` with `name` + `description` frontmatter, `scripts/`, `references/`, `templates/` is exactly the expected shape. Nineteen skills port with near-zero content change. The work is in the plugin shell, the hooks, and — for remote — in state and secrets.

### Gap register

Severity: **B** = blocks install/operation · **F** = functionality lost · **P** = polish.

| # | Gap | Today | Required | Sev | Est |
|---|---|---|---|---|---|
| P1 | Manifest | `plugin.yaml` | `.claude-plugin/plugin.json` (`name` required; `version`, `description`, `author{}`, `license`, `keywords`, `homepage`, `repository`) | B | 2h |
| P2 | Marketplace | none | `.claude-plugin/marketplace.json` at repo root → `/plugin marketplace add moonlight-lupin/chief-of-staff-agent` | B | 1h |
| P3 | Skill frontmatter | `version:`, `author:` at top level | Not recognized fields — move under `metadata:`. Keep `name`, `description`, `license` | P | 1h |
| P4 | Skill profiles | `__init__.py` filters `esign-connector` when DocuSeal is unconfigured | No equivalent — every `skills/*/SKILL.md` loads. Guard inside the SKILL body, or `disable-model-invocation: true` | F | 3h |
| P5 | Hooks | 9 Python callbacks, in-process | `hooks/hooks.json`; each hook becomes a CLI reading hook JSON on stdin. Event map below | F | 2d |
| P6 | Python deps | "run `pip install -r requirements.txt`" | Cloud VM has none. `SessionStart` hook running an idempotent installer into `${CLAUDE_PLUGIN_DATA}`, plus a documented setup script | B | 1d |
| P7 | **State persistence** | YAML/Markdown under `paths.project_root` | Cloud sessions are a **fresh VM with a fresh clone**; nothing else carries over. Every deal, invoice, todo, and wiki page vanishes at session end | **B** | 1–2w |
| P8 | **Secrets** | `.env` in plugin root | Cloud environment variables are **plaintext and readable by anyone using the environment**; the docs explicitly say not to put credentials there. Desktop plugins get `userConfig` with `sensitive: true` → OS keychain → `CLAUDE_PLUGIN_OPTION_<KEY>` | **B** | 2d |
| P9 | Agent-executed writes | `agent` provider raises `NotImplementedError` on every write | Cowork's own Gmail/Outlook/Drive connectors are the natural write path, but there's no way to record their outcome in the audit chain — `mark_executed()` exists in Python and is not exposed as a CLI subcommand | **B** | 3d |
| P10 | Scheduling | `install_cron.py` → system crontab, `hermes` binary | Routines / scheduled tasks. `doctor` already warns `cannot inspect cron jobs: No such file or directory: 'hermes'` | F | 1d |
| P11 | Network egress | assumes open internet | Cloud default is a **Trusted allowlist**; `graph.microsoft.com`, `login.microsoftonline.com`, Composio, Google APIs, and DocuSeal are **not** on it. Needs a documented Custom domain list | B | 2h |
| P12 | Inbound webhooks | `webhook_receiver.py` HTTP server | No inbound ports in cloud sessions — polling only, remote | P | doc |
| P13 | Path assumptions | `~/.hermes`, `HERMES_HOME`, `hermes` CLI | Use `${CLAUDE_PLUGIN_ROOT}` / `${CLAUDE_PLUGIN_DATA}`. The existing `CHIEF_OF_STAFF_HERMES_HOME` override is the right seam — 12 call sites already route through it | P | 4h |
| P14 | Skill surface | 19 always-listed skills | Every description competes in one picker. Audit trigger quality; `user-invocable: false` for background-knowledge skills; consider splitting core vs. research into two plugins | P | 1d |

### Hook event mapping (P5)

| Hermes event | Hook | Claude Code event | Notes |
|---|---|---|---|
| `pre_llm_call` | `company_context_primer` | `UserPromptSubmit` | Return context via `additionalContext` |
| `pre_llm_call` | `deadline_urgency_injection` | `UserPromptSubmit` | Same |
| `pre_llm_call` | `wiki_context_injection` | `UserPromptSubmit` | Same; keeps its question-intent gating |
| `pre_tool_call` | `pipeline_stage_validator` | `PreToolUse` | `matcher: "Write\|Edit"` |
| `post_tool_call` | `yaml_integrity_checker` | `PostToolUse` | `matcher: "Write\|Edit"` |
| `post_tool_call` | `self_sign_guard` | `PostToolUse` | `matcher: "Bash"` |
| `post_llm_call` | `format_enforcer` | `Stop` | **Semantics change**: fires once at turn end, not per LLM call |
| `post_llm_call` | `note_capture_reminder` | `Stop` | Same caveat |
| `on_session_start` | `stale_briefing_detector` | `SessionStart` | `matcher: "startup\|resume"` |

Each becomes a small executable reading the hook payload as JSON on stdin and emitting a JSON decision. The existing callbacks are pure functions over a `context` dict, so the porting cost is a thin adapter per hook plus payload-shape changes — the logic itself carries over.

`_cos_skills_loaded()` deserves a rethink: it exists because the Hermes runtime never passes `loaded_skills`, so it defaults to `False` and the persona only appears when a CoS skill is confirmed loaded. Under Claude Code the hook payload is different, and the gating needs to be rebuilt against what's actually available.

### The two hard problems

Everything above except **P7** and **P8** is mechanical. These two decide whether Cowork *remote* is viable at all.

**P7 — where does the state live?**

The project's second-biggest selling point is "your data is yours, in plain files on your own machine." A cloud session is a fresh VM with a fresh clone of a repo and nothing else. `${CLAUDE_PLUGIN_DATA}` survives plugin *updates*, not session teardown.

Three options:

- **(a) Git-backed state.** A private state repo attached as a session source; mutations commit and push. Preserves the plain-files philosophy exactly, gives free versioning and audit-adjacent history, and reuses the existing atomic-write + locking discipline. Needs conflict handling for concurrent sessions and a `cos sync` command. **Recommended.**
- **(b) SQLite + object store.** More robust concurrently, but abandons the "readable files you can walk away from" promise and duplicates the YAML-to-SQLite migration already sketched in `shared/docs/`.
- **(c) Desktop-local only.** Full read/write on the desktop app where the filesystem persists; remote sessions get a read-only briefing view. Cheapest, and honest — but it means "Cowork remote" never really operates the assistant.

**P8 — where do the credentials live?**

Cloud environment variables are explicitly documented as unsuitable for credentials: plaintext, visible to anyone using the environment, no secrets store. That rules out `M365_CLIENT_SECRET`, Google service-account JSON, `COMPOSIO_MCP_KEY`, and `DOCUSEAL_API_KEY` in any shared remote environment.

The split that works:

- **Desktop (local sessions):** declare each secret in `plugin.json` `userConfig` with `sensitive: true`. Values go to the OS keychain and arrive as `CLAUDE_PLUGIN_OPTION_<KEY>`. Teach `config_loader` to read those alongside `.env` — a small, well-isolated change.
- **Cowork remote:** **refuse the credential-holding providers entirely.** Only the `agent` provider is safe there, because it holds no credentials — Claude's own connectors do the I/O. `readiness` should hard-fail `google_api` / `m365` / `composio` when it detects a cloud session (`CLAUDE_CODE_REMOTE_SESSION_ID` is set), rather than letting someone paste a tenant secret into a shared environment.

That makes **P9 the linchpin of the whole remote story.** The `agent` provider is read-only by construction — every write raises `NotImplementedError` pointing back at `get_workspace_client()`. So on Cowork remote today: briefings work, review-queue *preparation* works, and nothing can ever execute. Closing it needs a documented three-step seam:

1. `review_queue.py approve` → action reaches `executing` (the existing `mark_executing` pre-flight already handles the approval-lapse race)
2. Claude executes the mutation with its own connector tool
3. `review_queue.py record-execution --action-id … --result-json …` → wraps `mark_executed()`, writes the workspace-audit record, enforces that the action was genuinely in `executing`

Step 3 does not exist as a CLI. It is roughly a day of work and it is what converts remote Cowork from a read-only viewer into an actual chief of staff — while keeping the approve→execute→audit invariant intact.

### Suggested phasing

| Phase | Contents | Outcome | Est |
|---|---|---|---|
| **A — Installable** | P1, P2, P3, P13, P11 (docs) | `/plugin install` works; 19 skills load on desktop; scripts run against a local project root | 2–3d |
| **B — Full desktop parity** | P5 hooks, P4 profiles, P6 deps, P8 desktop half | Feature-equivalent to the Hermes plugin on Claude Desktop | 4–5d |
| **C — Cowork remote read-only** | P6 remote, P8 refusal logic, P9 step 1, P10 routines, P12 docs | Briefings, deadlines, pipeline, research run remotely; writes prepare but do not execute | 3–4d |
| **D — Cowork remote operating** | P7 git-backed state, P9 record-execution | State survives sessions; approved actions execute through connectors and land in the audit chain | 1–2w |

Phases A and B are worth doing regardless — they widen distribution with no architectural commitment. **Phase D is where the real decision is**, and it is a state-architecture decision (P7) more than a plugin-format one.

A reasonable sequencing: finish the Part A release hygiene and tag v0.4.0 first, then Phase A+B as v0.5.0 ("runs on Claude Desktop"), and treat C+D as its own milestone once the state question is settled.

---

## Appendix — how to reproduce

```bash
# Full suite (needs a working `cryptography`; a distro-managed one may panic)
python -m pytest -q

# Confirm the 20 crypto failures are environmental
pip install --ignore-installed --target=/tmp/pylibs cffi cryptography
PYTHONPATH=/tmp/pylibs python -m pytest tests/test_jwt_crypto_v0202.py \
  tests/test_webhook_hotfix_v0201.py tests/test_e2e_v022.py -q   # → 77 passed

ruff check shared/ skills/ hooks.py --select=E9,F63,F7,F82   # clean
ruff check shared/ skills/ hooks.py                          # 113 findings

python shared/scripts/chief_of_staff.py demo
python shared/scripts/chief_of_staff.py doctor --summary
git log --oneline f3e0b41..HEAD | wc -l                      # 36 unreleased commits
```
