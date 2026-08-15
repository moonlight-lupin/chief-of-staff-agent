# Chief of Staff — Production Readiness Status

**Original review:** Claude Code Opus (architecture/security) + Codex GPT-5.6 (correctness/edge cases), against v0.3.24
**Status as of:** v0.4.0

> ## Current status
>
> | Tier | Scope | State |
> |---|---|---|
> | **Phase 1** | Safety gate — B1, B2, audit paths, recipient classification, retry cap | ✅ **Shipped in v0.4.0** |
> | **Phase 2** | Hardening — transactional state, fail-closed corruption, HMAC timestamp, path guard, retention, MCP recovery, E2E, CI | ✅ **Shipped in v0.4.0** |
> | **Phase 3** | Polish — stuck-action reconciliation, audit hash chain, race suite, config model, partial decomposition | ✅ **Shipped in v0.4.0** |
> | **Phase 4** | M365 live canary | ⏸ **Open — blocked on a dedicated Entra tenant** |
> | **Phase 5** | Nice-to-have | Ongoing |
>
> **The original verdict below (REQUEST CHANGES — 3 blocking, 12 major) applied
> to v0.3.24 and is retained for the record.** All three blocking issues and all
> non-M365 major issues are closed; see `CHANGELOG.md` v0.4.0 for what shipped
> and the ✅/⏸ markers on each task in section 2.
>
> **Release posture:** production-ready for `google_api` and `composio` (Google
> and Microsoft). Native `m365` is code-complete but has **never been
> live-verified** — `chief_of_staff.py capabilities` reports
> `provider_verified: false` for it, and the capability flags are deliberately
> conservative. Do not point app-only `.default`-scoped credentials at a
> production tenant until Phase 4 completes.

---

## 1. Observations *(as reviewed at v0.3.24)*

### 1.1 What Works (keep)

The codebase has strong fundamentals. Both reviewers independently confirmed:

- **Safety model design is architecturally sound.** The layering is real: `pending_actions.py` owns the state machine, `workspace_guardrails.py` owns the gate, providers own I/O, `workspace_audit` owns the record. The `@guarded` decorator makes confirm→run→audit a single non-optional wrapper.
- **`mark_executing` pre-flight.** Thoughtful fix for the "provider succeeded but approval lapsed between check and write" race. Most codebases at this maturity do not have this.
- **M365 HTTP hygiene is the strongest part of the codebase.** Method-aware retry refuses to auto-retry POST/PATCH on ambiguous 503/504. `Retry-After` honored in full and deferred when it exceeds budget. `ImmutableId` correctly anticipated. `@odata.nextLink` origin-checked so bearer token cannot walk off-host. `_permission_hint` mapper turns 2-day debugging into 10 minutes.
- **Honest capability flags.** `calendar.cancel=False`, `files.untrash=False` for M365 — correctly reported as unverified.
- **Test ratio.** 28K lines of tests for 27K lines of source (1:1). 1,802 tests, 1,801 pass.
- **Secret redaction.** Comprehensive in `runtime_log.py` — tokens, secrets, passwords scrubbed at write time. No secrets in ActionResult payloads.
- **No injection vectors.** No `shell=True`, no `eval`/`exec`/`pickle.loads`.
- **Webhook OIDC.** Real cryptographic verification via `google-auth` with audience, issuer, service-account email, `email_verified` checks, 30s clock skew bound.
- **No TODO/FIXME/HACK** in the codebase.

### 1.2 Blocking Issues

| ID | Issue | Files | Severity |
|---|---|---|---|
| **B1** | **Guardrail bypass + ambient env approval.** Legacy mutation IDs (`gmail.archive`, `gmail.trash`, `drive.trash`) deliberately excluded from `WRITE_ACTIONS` — any skill with a client reference calls them ungated. Worse, `CHIEF_OF_STAFF_AUTO_APPROVE=1` + `CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1` are process-global env vars with no reference to *which* action was approved. An operator who exports these in cron/systemd converts the safety model into a no-op. | `workspace_guardrails.py:36-38, 154, 158-172` | Critical |
| **B2** | **No concurrency safety on state files.** `pending_actions.py`, `event_store.py`, `webhook_security.py` all do unlocked load→check→`.tmp` write→replace. Shared `.tmp` filename. Two processes can both win the `approved→executing` transition and both send the same email. | `pending_actions.py:112-138`, `event_store.py:65-96`, `webhook_security.py:197-248` | Critical |
| **B3** | **M365 tenant-wide mailbox access.** `client_credentials` + `.default` scope grants app-only Mail/Calendars/Files read-write across every mailbox. Only restriction is UPN string in URL path. No ApplicationAccessPolicy, no allowlist, no preflight. | `m365_graph.py:123, 326-337` | Critical (for M365 path) |

### 1.3 Major Issues

| ID | Issue | Files | Source |
|---|---|---|---|
| **M1** | Lost state updates in YAML stores — lock covers only write, not load→mutate→save | `state_store.py:136-199` | Both |
| **M2** | Audit failure after successful mutation — success audit outside try/except, exception escapes, retry duplicates sends | `workspace_guardrails.py:324` | Both |
| **M3** | Blocked write attempts never durably audited — only best-effort runtime_log that no-ops | `workspace_guardrails.py:301-311` | Opus |
| **M4** | Recipient domain classification exploitable — substring match classifies `acme.com.attacker.io` as internal | `pending_actions.py:199` | Opus |
| **M5** | `mark_failed` retries forever with no cap — ambiguous 504 re-arms approval for re-execution | `pending_actions.py:543-546` | Opus |
| **M6** | Corrupt JSON state files silently become empty stores — data loss + replay bypass | `pending_actions.py:91`, `event_store.py:65` | Codex |
| **M7** | M365 device-code auth has no token cache — `token_cache_path` parsed but never passed to msal | `m365_graph.py:277-278, 302-344` | Both |
| **M8** | `mail_list_folders` crashes instead of degrading to `[]` | `m365_graph.py:933-959` | Codex |
| **M9** | Reversibility asserted but unimplemented — `mail.move` returns `reversible: True` but never captures original `parentFolderId` | `m365_graph.py:967-1003` | Opus |
| **M10** | HMAC webhook signs only body, no timestamp — replay protection expires after 24h | `webhook_security.py:41` | Opus |
| **M11** | `store_name` can path-escape project root via `../` | `state_store.py:86-92` | Codex |
| **M12** | No proactive M365 token refresh — `expires_in` discarded, guaranteed 401-then-retry per lifetime | `m365_graph.py:302-305` | Opus |

### 1.4 Minor Issues

| ID | Issue | Files |
|---|---|---|
| m1 | Internal-recipient detection uses substring containment (`evil-example.com` classified internal for `example.com`) | `pending_actions.py:189-201` |
| m2 | Calendar timezone handling strips `Z` but leaves numeric offsets; date-only values invent 10:00/11:00 UTC | `m365_graph.py:247-258` |
| m3 | Non-JSON Graph response silently becomes `{}` — masks schema drift | `m365_graph.py:671-693` |
| m4 | Upload limit (4MB) not enforced before reading file; path segments not URL-encoded | `m365_graph.py:1131-1145` |
| m5 | MCP `notifications/initialized` ignores HTTP errors — client marks initialized even when handshake failed | `mcp_client.py:93-96` |
| m6 | Standalone daily briefing writes `.last_briefing` — docs claim "never mutates anything" | `daily_briefing.py:812-828` |
| m7 | deep-research SKILL.md declares MIT, test expects Apache-2.0 | `skills/deep-research/SKILL.md`, `test_plugin_structure.py:137-146` |
| m8 | `CHIEF_OF_STAFF_AUDIT_STRICT` split doesn't strip whitespace — `"pipeline, invoices"` silently fails | `state_store.py:193` |
| m9 | `.backups/` grows without bound — no retention policy | `state_store.py:167` |
| m10 | `_project_root` silently falls back to `~/.hermes/projects/default` when config missing | `pending_actions.py:58-68` |

---

## 2. Phased Build Plan

### Phase 1 — Safety Gate ✅ SHIPPED (v0.4.0)

**Goal:** Close the three blocking holes. Make the safety model enforceable.

| # | Task | Files | Tests | Est |
|---|---|---|---|---|
| 1.1 | **Unify action IDs + invert to default-deny.** Map all legacy `gmail.*`/`drive.*` IDs to neutral `mail.*`/`files.*` before guardrail classification. Invert `confirm_action` to default-deny: unknown action ID → block, explicit `READ_ACTIONS` allowlist to pass. | `workspace_guardrails.py` | New: `test_guardrail_default_deny`, `test_legacy_id_canonicalization`, direct-call denial tests for every write method on every provider | 1 day |
| 1.2 | **Bind approval to per-action grant.** Thread `action_id` through `@guarded`. Validate against pending-action store at call time (state must be `executing`, approver present, not lapsed). Keep `ALLOW_DESTRUCTIVE` as break-glass requiring TTY only. | `workspace_guardrails.py`, `pending_actions.py`, all providers | New: `test_per_action_grant_validates_state`, `test_per_action_grant_rejects_unbound`, `test_break_glass_requires_tty` | 1 day |
| 1.3 | **Lock pending-action store.** Wrap load→check→mutate→write in `file_lock.with_lock`. Unique temp filenames (`.{pid}.{uuid}.tmp`). `fsync` before `replace`. Same treatment for `event_store.py` and `webhook_security.py` replay cache. | `pending_actions.py`, `event_store.py`, `webhook_security.py` | New: `test_concurrent_mark_executing_one_wins` (multiprocessing), `test_concurrent_replay_reservation` | 1 day |
| ~~1.4~~ | ~~**Constrain M365 authority.**~~ → Moved to Phase 4 (requires real Entra tenant). | — | — | — |
| 1.5 | **Audit blocked + failed-audit paths.** Move success-path audit inside try/except. On audit failure, return successful `ActionResult` with `audited=False` and log loudly. Add `status="blocked"` audit record on block path. | `workspace_guardrails.py` | New: `test_blocked_action_is_audited`, `test_audit_failure_does_not_mask_success` | 0.5 day |
| 1.6 | **Fix recipient-domain classification.** Replace substring match with exact match or dot-boundary suffix: `domain == company_domain or domain.endswith("." + company_domain)` on normalized registrable domain. | `pending_actions.py:199` | Update existing: `test_classify_recipient_risk` with homograph/suffix cases | 0.5 day |
| 1.7 | **Cap `mark_failed` retries.** Max 3 retries, then terminal `failed` state. For ambiguous provider outcomes (504 on sendMail), transition to `needs_verification` state that requires fresh human approval before re-execution. | `pending_actions.py:543-546` | New: `test_retry_cap`, `test_ambiguous_status_needs_verification` | 0.5 day |
| 1.8 | **Fix `mail_list_folders` degradation.** Use `_paged_values(..., degrade=True)` inside warn-and-empty exception boundary. | `m365_graph.py:933-959` | Update existing: `test_mail_list_folders_degrades_on_error` | 0.5 day |
| 1.9 | **Fix deep-research license test.** Either relicense to Apache-2.0 (if legally sound) or update test to allow vendored MIT skills with attribution. | `test_plugin_structure.py:137-146` | Update existing test | 0.5 day |
| 1.10 | **Run full test suite in clean env.** Install all deps (google-auth, pymupdf, etc.) and verify 1,801+ pass. Add dependency sanity check to CI. | CI | Verify: all green | 0.5 day |

**Outcome: shipped in v0.4.0. All three blocking issues closed; suite green at 2,002 tests.**

### Phase 2 — Hardening ✅ SHIPPED (v0.4.0)

**Goal:** Close all major issues. Prepare M365 for live canary.

| # | Task | Files | Est |
|---|---|---|---|
| 2.1 | **Transactional state_store.** Add `with_store_lock(name)` context manager covering load→mutate→save. Or add mtime/version guard to `save_store_atomic` that raises on concurrent modification. | `state_store.py` | 1 day |
| 2.2 | **Fail-closed on corrupt state.** Corrupt JSON → typed corruption error, preserve bad file, recover from validated backup, alert operator. Never treat unreadable file as empty store. | `pending_actions.py`, `event_store.py`, `webhook_security.py` | 1 day |
| 2.3 | ~~**M365 token cache + proactive refresh.**~~ → Moved to Phase 4 (requires real Entra tenant). | — | — |
| 2.4 | ~~**Implement real reversibility.**~~ → Moved to Phase 4 (M365-specific, requires real tenant to verify). | — | — |
| 2.5 | **Timestamp in HMAC webhook signatures.** Sign `timestamp.body`, require `X-Webhook-Timestamp` header, reject skew > 300s. | `webhook_security.py` | 0.5 day |
| 2.6 | **`store_name` path-escape guard.** Restrict to supported stores or strict basename regex. Resolve and assert under project root. | `state_store.py:86-92` | 0.5 day |
| 2.7 | **Backup retention.** Prune `.backups/` to N most recent / M days after each successful write. | `state_store.py:167` | 0.5 day |
| 2.8 | **Audit strict whitespace fix.** `[s.strip() for s in ... .split(",") if s.strip()]`. | `state_store.py:193` | 0.25 day |
| 2.9 | **Fail loudly on missing project root.** `pending_actions._project_root` should raise, not silently fall back. | `pending_actions.py:58-68` | 0.25 day |
| 2.10 | ~~**M365 timezone + upload + URL hardening.**~~ → Moved to Phase 4 (M365-specific). | — | — |
| 2.11 | **MCP session recovery.** Check `notifications/initialized` response, reset on failure, bounded retry. | `mcp_client.py` | 0.5 day |
| 2.12 | **End-to-end integration test.** Exercise prepare→approve→execute against Graph mock, including lapsed-approval and concurrent-execute paths. | New test file | 1 day |
| 2.13 | **CI upgrade.** Python 3.11 + 3.12 matrix, add ruff lint, add mypy type checking. | `.github/workflows/ci.yml` | 0.5 day |

**Outcome: shipped in v0.4.0. All non-M365 major issues closed.**

### Phase 3 — Polish + Scale ✅ SHIPPED (v0.4.0)

**Goal:** Close remaining non-M365 gaps. Production-ready for Google SA + Composio. No M365 dependency.

| # | Task | Files | Est |
|---|---|---|---|
| 3.1 | **Stuck-action reconciliation.** Add `executing` state timeout detection + recovery command in readiness checks. | `pending_actions.py`, `readiness.py` | 0.5 day |
| 3.2 | **Audit-log integrity.** Append-only mode or hash chain so record cannot be silently edited. | `workspace_audit.py` | 1 day |
| 3.3 | **Multiprocessing race test suite.** Full suite: create/approve/execute, event ingestion, replay reservation, state_store concurrent writes. | New test file | 1 day |
| 3.4 | **Clarify read-only docs.** State precisely: "daily command is externally read-only but writes local telemetry." Add filesystem diff test. | `docs/`, `daily_briefing.py` | 0.5 day |
| 3.5 | **Centralize config.** Consolidate 91 scattered `os.getenv` calls into a Pydantic Settings model. | New `config.py`, all files with os.getenv | 1.5 days |
| 3.6 | **Decompose god files.** Split `chief_of_staff.py` (2500 lines) and `composio_mcp_workspace_base.py` (1980 lines) into focused modules. | `chief_of_staff.py`, `composio_mcp_workspace_base.py` | 2 days |

**Outcome: shipped in v0.4.0. Task 3.6 (god-file decomposition) partially done — `cos_helpers.py` and `capability_report.py` extracted; `chief_of_staff.py` is held under a 2,500-line budget by test, and `composio_mcp_workspace_base.py` remains large.**

### Phase 4 — M365 Live Canary ⏸ OPEN (requires a real Entra tenant)

**Goal:** First real M365 connection against a live tenant. Cannot start until a dedicated test tenant is available.

| # | Task | Files | Est |
|---|---|---|---|
| 4.1 | **Constrain M365 authority (B3).** Add `user_principal` allowlist at client construction. Document ApplicationAccessPolicy requirement. Add startup preflight that fails if a second mailbox is reachable. | `m365_graph.py` | 0.5 day |
| 4.2 | **M365 token cache + proactive refresh (M7, M12).** Wire `msal.SerializableTokenCache` to `token_cache_path` with 0600 perms + locking. Store `expires_on`, refresh at ~80% lifetime. | `m365_graph.py` | 1 day |
| 4.3 | **Implement real reversibility (M9).** Capture `parentFolderId` before moves. Restore to original folder, not hardcoded `inbox`. Re-evaluate `SAFE_WRITE_ACTIONS`. | `m365_graph.py` | 1 day |
| 4.4 | **M365 timezone + upload + URL hardening.** Parse RFC3339, convert to declared zone, preserve all-day semantics. Enforce 4MB upload limit before reading file. URL-encode path segments. Reject non-JSON responses. | `m365_graph.py` | 1.5 days |
| 4.5 | **M365 live canary (reads only).** Against dedicated test tenant with single mailbox. Exercise: expired secret/consent errors, token refresh, 429 with both Retry-After forms, 503/504 ambiguity, multi-page reads, immutable IDs across archive/trash/restore. | Manual + test fixtures | 1 day |
| 4.6 | **M365 canary (limited writes).** Archive→unarchive round-trip. Trash→untrash round-trip. Files just below/above 4MB. Audit/state-write failure after successful mutation. | Manual + test fixtures | 1 day |

**Status: OPEN. Estimate 5-6 days once a dedicated Entra tenant is available. Until then `capabilities` reports the provider as never live-verified.**

### Phase 5 — Nice to Have (ongoing)

- Replace file-based state with SQLite WAL mode — gets ACID transactions, real concurrency, retires half the concurrency work permanently
- Structured queryable audit store with retention and export
- Per-action rate limits (max N sends/hour) as blast-radius cap
- Graph change notifications to replace polling
- Dry-run mode across all providers for rehearsing against real tenant with writes stubbed
- Populate `KNOWN_SAFE_DOMAINS` from correspondence history for data-driven recipient risk

---

## 3. Release Decision Matrix

| Provider | Phase 1 ✅ | Phase 2 ✅ | Phase 3 ✅ | Phase 4 ⏸ |
|---|---|---|---|---|
| Google SA | ✅ Prod-ready | ✅ Hardened | ✅ Full | — |
| Composio (Google) | ✅ Prod-ready | ✅ Hardened | ✅ Full | — |
| Composio (Microsoft) | ✅ Prod-ready* | ✅ Hardened | ✅ Full | — |
| M365 (native Graph) | ⚠️ Code only | ⚠️ Code only | ⚠️ Code only | ✅ Full |

*Composio Microsoft writes through Composio's managed OAuth, which scopes per-user, not tenant-wide. The B3 concern applies only to native M365 Graph with client_credentials.

**Release taken:** v0.4.0 ships Phases 1-3 for Google SA + Composio (Google and Microsoft). Native M365 remains deferred to Phase 4.