# Release Readiness Assessment — v0.3.24 (+36 unreleased commits)

**Assessed:** 2026-08-14 · **Branch:** `claude/production-release-readiness-miecj7` · **Scope:** everything except the native M365 Graph path (no Entra tenant available to verify).

> ## Remediation status
>
> Everything in Part A and Tiers 0–1 of Part B has been implemented on this
> branch and ships as **v0.4.0**. The findings below are kept as written so the
> reasoning survives; this box records what happened to each.
>
> | ID | Finding | Status |
> |---|---|---|
> | R1 | CHANGELOG 36 commits stale | ✅ v0.4.0 entry added |
> | R2 | Roadmap headlines "REQUEST CHANGES" | ✅ restructured as a status document |
> | R3 | No tags, no releases | ⏸ **the one open item** — tag after merge, see below |
> | R4 | No dependency preflight | ✅ `doctor` imports all 7 declared packages; CI step added |
> | S1 | Demo data expired | ✅ envelope re-anchored on today |
> | S2 | `--help` reports v0.3.7 | ✅ derives from `VERSION`, with a test |
> | S3 | Skill count wrong | ✅ README corrected (19 shipped / 18 default) |
> | S4 | No NOTICE for vendored MIT skill | ✅ `NOTICE` added |
> | S5 | No governance files | ✅ `SECURITY.md`, `CONTRIBUTING.md` added |
> | S6 | Lint decorative | ✅ 113 findings → 0; CI enforces without `continue-on-error` |
> | T0.1 | No agent orientation file | ✅ `CLAUDE.md` |
> | T0.2 | No Claude plugin manifest | ✅ `.claude-plugin/plugin.json` + `marketplace.json` |
> | T1.1 | Agent writes dead-end | ✅ `review_queue.py claim` + `record-execution` |
> | T1.2 | Dependency preflight | ✅ (same as R4) |
> | T1.3 | No machine-readable capabilities | ✅ `chief_of_staff.py capabilities` |
> | T1.4 | No hosted-session guardrails | ✅ credential providers refused in cloud sessions |
> | T1.5 | Cloud environment undocumented | ✅ `docs/SETUP.md` hosted-session section |
> | Tier 2 | Git-backed state for remote | ⏸ deferred by design — see Part B |
>
> **Suite: 1,951 → 2,002 passing. ruff: 113 findings → 0.**
>
> **R3 is deliberately not done on this branch.** A tag should point at a commit
> on the default branch, not at a feature branch. After merge:
> `git tag -a v0.4.0 -m "..." && git push origin v0.4.0`, then publish a GitHub
> release using the v0.4.0 CHANGELOG entry as the notes.

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
| Doc accuracy | **C** | Roadmap verdict stale, skill count wrong, demo data expired, CLI reports v0.3.7 |
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

**S2 — The CLI reports the wrong version.** `chief_of_staff.py:2215` hardcodes `"Chief-of-Staff v0.3.7 — read-only daily operating loop…"` in the argparse description, while `VERSION = "0.3.24"` sits at line 40. `--help` is the first thing a human or an agent sees, and it names a version 17 releases old. Two more `v0.3.7` strings survive in section comments (lines 1264, 1797).

**S3 — Skill count is wrong in two places.** README says "Eighteen skills"; `plugin.yaml` registers 19 (`news-monitoring` landed in the last two commits). `doctor` reports "all 18 effective skills present" because `esign-connector` is filtered out when DocuSeal is unconfigured — so 18 is right for a default install and 19 is right for the manifest, and neither document says which it means.

**S4 — License hygiene for vendored work.** `skills/deep-research/SKILL.md` declares `license: MIT` inside an Apache-2.0 repo. The test was relaxed to allow it (Phase 1.9), which is the correct call, but there is no `NOTICE` file recording the upstream attribution.

**S5 — Governance files absent.** No `SECURITY.md` (this project handles mail, calendars, and credentials — it needs a disclosure address), no `CONTRIBUTING.md`, no issue or PR templates.

**S6 — Make lint mean something.** Fix the 67 auto-fixable findings, then drop `continue-on-error` and widen coverage to `skills/` and `hooks.py`. Roadmap task 2.13 also called for mypy; not done.

### Accepted for now

- **God files.** `chief_of_staff.py` (2,423 lines) and `composio_mcp_workspace_base.py` (1,980) remain large; roadmap 3.6 was partially done via the `cos_helpers` extraction. Not a release blocker.
- **M365 native path.** Phase 4 items — B3 tenant-wide authority, token cache, real reversibility, timezone/upload hardening — are open by design. Capability flags are honestly `False` and `workspace_capabilities.py` carries per-capability reasons, which is the right posture. **Recommendation:** have `readiness` print an explicit "native m365 is code-complete but never live-verified" banner so nobody points app-only `.default`-scoped credentials at a production tenant on the strength of the README table.
- **Webhooks.** Polling is the supported path; the Pub/Sub receiver is fine as an optional extra.

### Recommended release shape

Ship **v0.4.0** for `google_api` + `composio` (both Google and Microsoft), with native `m365` labelled *code-complete, unverified*. That matches the roadmap's own decision matrix and is defensible today.

Sequence: R1 → R2 → R4 → S1 → S2 → tag → R3. Roughly two days.

---

## Part B — Closing the gap for Claude agents (Hermes stays the runtime of record)

### Framing

**This is not a port.** Hermes remains the plugin runtime. The goal is narrower and much cheaper: a Claude agent — in Claude Desktop, or a Cowork remote session — should be able to clone this repo and *drive it competently* without anyone rewriting the plugin shell.

That is close to what the project already promises. The README's headline install is "paste this to your agent," every CLI emits JSON by default, and the `agent` workspace provider already models the exact split Cowork needs: Claude fetches with its own connectors, Chief of Staff computes and guards.

So the work divides cleanly into three tiers, and **only the middle one really matters**:

- **Tier 0** — additive files Hermes never reads. Costs nothing, risks nothing.
- **Tier 1** — the genuine functional gap, and it is *not* a format problem. An agent can install and read everything today, then hits a wall the moment it tries to execute an approved action.
- **Tier 2** — remote state durability. A real architectural decision; defer until you want it.

Everything from the previous draft that was pure format conversion — porting nine hooks to `hooks.json`, rebuilding skill-profile filtering, restructuring frontmatter — is **dropped**. It buys an agent nothing.

### Tier 0 — additive compatibility (½ day, zero Hermes risk)

Three files Hermes ignores entirely, because it only reads `plugin.yaml` and `__init__.py`.

**T0.1 — `CLAUDE.md` at the repo root. The single highest-leverage artifact here.**

Claude Code and Cowork load it automatically at session start; Hermes never looks at it. Today an agent cloning this repo gets no orientation at all and has to reverse-engineer the operating contract from 20 docs. `CLAUDE.md` should state, tersely:

- What this is, and the one invariant: *suggest → approve → execute → audit*, never collapsed
- Where config and state live (`company.yaml`, `paths.project_root`), and that secrets go in `.env` only
- The command index — the ten `chief_of_staff.py` subcommands and the review-queue verbs — with the note that JSON is the default output and `--summary` is the human form
- The fetch/compute split: fetch reads with your own connector tools, normalize to `shared/scripts/schemas.py`, pass via `--input`
- The hard prohibitions: **never** call connector *write* tools directly for CoS workflows; **never** set `CHIEF_OF_STAFF_AUTO_APPROVE` or `CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE` to work around a block; **never** invent action IDs
- What to do on failure: `logs diagnose --latest-failed` before guessing

This is the difference between an agent that operates the system correctly and one that improvises around it. It also pays off in Hermes indirectly, since the same text is what you would paste into any other host.

**T0.2 — `.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json`.**

Two small JSON files in a directory Hermes doesn't read. They make `/plugin marketplace add moonlight-lupin/chief-of-staff-agent` work, so the 19 skills load natively in Desktop and Cowork with no conversion — the `skills/<name>/SKILL.md` layout is already exactly the expected shape. `plugin.json` needs only `name`; fill in `version`, `description`, `author`, `license`, `repository` for the install UI.

Worth one sanity check on first load: the skills carry `version:` and `author:` at the frontmatter top level, which aren't in Claude Code's field list. If it warns, moving them under `metadata:` is a ten-minute change that Hermes tolerates too. If it doesn't warn, leave them.

**T0.3 — fix the `v0.3.7` help string** (S2 above). An agent reads `--help` first.

### Tier 1 — the actual gap (2–3 days)

None of this is Claude-specific. It is what any agent needs to operate the system, and it improves the Hermes experience identically.

**T1.1 — `review_queue.py record-execution` (~1 day). The linchpin.**

Today an agent using its own connectors can prepare an action and approve it, and then **every write dead-ends**: the `agent` provider raises `NotImplementedError` by construction, and `mark_executed()` exists in `pending_actions.py` but is not exposed as a CLI subcommand. The queue has `list / preview / approve / dismiss / execute / summary / audit` — there is no verb for "I executed this outside the Python client, here is what happened."

The seam:

1. `review_queue.py approve --action-id …` → the existing `mark_executing` pre-flight handles the approval-lapse race
2. Claude performs the mutation with its connector tool
3. `review_queue.py record-execution --action-id … --result-json …` → wraps `mark_executed()`, writes the workspace-audit record, and **enforces that the action was genuinely in `executing`** so this cannot become a bypass

That last clause is the whole design: it must be a narrow recording verb, not a second execution path. Done right it preserves the approve→execute→audit invariant while letting the agent be the effector. Done loosely it is a hole straight through the safety model, so it deserves the same adversarial tests as the guardrail work.

This one change is what converts an agent from "can read your briefing" to "can actually run your day."

**T1.2 — `doctor:dependencies` (~½ day).** Same item as R4, but it matters twice as much here: an agent that hits a Rust panic on import has nothing actionable to reason about, whereas a JSON finding naming the package and the fix is exactly what `logs diagnose` was built to produce. Check `cryptography`, `msal`, `google.auth`, `fitz`/pymupdf, `docx` with version floors.

**T1.3 — a machine-readable capability call (~½ day).** An agent currently infers what it may do from prose across `README.md`, `SETUP.md`, and the capability table. `workspace_capabilities.py` already holds the truth, including the honest per-capability `False` reasons for M365. Surface it as one call — `chief_of_staff.py capabilities --json`, or a section in `readiness` — returning: active provider, permitted operations, unverified operations *with reasons*, project root, and configured/missing state. One call, and the agent knows its own envelope instead of guessing.

**T1.4 — remote-session guardrails (~½ day).** In a cloud session, `CLAUDE_CODE_REMOTE_SESSION_ID` is set. Use it:

- **Refuse the credential-holding providers.** Cloud environment variables are documented as plaintext and readable by anyone using the environment, with no secrets store. `google_api`, `m365`, and `composio` should hard-fail there with a pointer to the `agent` provider, which holds no credentials because Claude's connectors do the I/O. This is cheap and it prevents someone pasting a tenant secret into a shared environment.
- **Warn that state is ephemeral** until Tier 2 lands, so nothing is silently lost at session teardown.

**T1.5 — document the two environment facts (~2h, docs only).** Cloud sessions default to a *Trusted* domain allowlist that does **not** include `graph.microsoft.com`, `login.microsoftonline.com`, Composio, Google APIs, or a self-hosted DocuSeal — so a Custom allowed-domain list is required for anything but the `agent` provider. And there are no inbound ports, so remote is polling-only; the Pub/Sub receiver is desktop/server territory. Both belong in `SETUP.md` next to the provider walkthroughs.

### Tier 2 — remote state durability (defer)

The one genuine architectural question, and **only** for Cowork remote. On Claude Desktop the filesystem persists and this is a non-issue; the plugin works there today the way it works under Hermes.

A cloud session is a fresh VM with a fresh clone and nothing else carried over, so every deal, invoice, todo, and wiki page written during the session vanishes at teardown. The option that fits this project is **git-backed state**: a private state repo attached as a session source, with mutations committed and pushed. It keeps the plain-files promise exactly, adds free history, and reuses the atomic-write and locking discipline already built. It needs conflict handling for concurrent sessions and a `cos sync` verb.

Don't build it until you want stateful remote operation. Until then, Tier 1's ephemerality warning is the honest answer, and read-only remote briefings are genuinely useful on their own.

### What this comes to

| Tier | Contents | Outcome | Est |
|---|---|---|---|
| **0** | `CLAUDE.md`, `.claude-plugin/*`, version-string fix | Skills load natively in Desktop/Cowork; any agent gets a correct operating contract on arrival | ½ d |
| **1** | `record-execution`, dependency preflight, capabilities call, remote guardrails, env docs | An agent can install, verify, brief, prepare, approve, execute via its own connectors, and audit — end to end | 2–3 d |
| **2** | Git-backed state | Cowork remote becomes stateful | 1–2 w, defer |

**Tiers 0 + 1 are about 3½ days** and leave Hermes untouched — no manifest migration, no hook port, no skill rewrites. Compare with roughly two weeks for the full conversion in the previous draft.

Worth noting that T1.1 through T1.4 are not concessions to Claude. `record-execution` closes a real hole in the agent provider, the dependency check fixes the top install failure, the capabilities call removes prose-inference from every host, and the remote guardrails stop a credential mistake. They belong in the Hermes product regardless — which is the argument for doing them right after the v0.4.0 release rather than treating them as a side quest.

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
python shared/scripts/chief_of_staff.py --help | head -6      # reports v0.3.7
git log --oneline f3e0b41..HEAD | wc -l                       # 36 unreleased commits
```
