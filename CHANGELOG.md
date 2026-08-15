# Changelog

All notable changes to Chief of Staff, newest first.

> **On this file's provenance.** Entries from **v0.3.8 onward were written at
> release time**. Entries for **v0.1.0–v0.3.7 were reconstructed on 2026-08-15**
> from the full git history (160 commits, `0f96090..cc78d24`) — `CHANGELOG.md`
> was not created until v0.3.8, so those releases had never been documented
> anywhere. Reconstructed entries are marked by the divider below and are
> necessarily terser: they record what the commits changed, and anything that
> only ever lived in a reviewer's head is not recoverable.

## v0.4.0 — Safety hardening complete, agent execution seam, release readiness

The largest release so far, and the first where the safety model is enforceable
rather than merely designed. It closes every blocking and major issue from the
v0.3.24 production review except the Microsoft 365 tier, which is deferred
pending a real Entra tenant.

**Release posture:** production-ready for `google_api` and `composio` (Google
and Microsoft). Native `m365` is code-complete but has never been live-verified;
`capabilities` and `readiness` now say so out loud.

### Safety model — enforceable, not just documented

- **Guardrail is default-deny (B1).** `confirm_action` previously let unknown
  action IDs through, and legacy `gmail.*` / `drive.*` mutation IDs were
  excluded from `WRITE_ACTIONS` entirely — any skill holding a client reference
  could call them ungated. Legacy IDs are now canonicalized to neutral `mail.*`
  / `files.*` before classification, and anything in neither `READ_ACTIONS` nor
  `WRITE_ACTIONS` is blocked. `requires_confirmation` fails closed for unknowns.
- **Per-action approval grants.** `CHIEF_OF_STAFF_AUTO_APPROVE` and
  `CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE` no longer act as ambient process-wide
  approval. Execution validates against the pending-action store at call time.
- **Concurrency safety on all state (B2).** `pending_actions`, `event_store`,
  `state_store`, and `webhook_security` wrap load→check→mutate→write in an
  exclusive file lock with unique temp filenames and `fsync` before `replace`.
  Two processes can no longer both win the `approved→executing` transition and
  both send the same email.
- **Transactional `state_store`** with `with_store_lock`, version monotonicity,
  and `ConcurrencyError` retry.
- **Fail-closed on corrupt state.** A corrupt JSON store raises
  `StateCorruptionError` and preserves the bad file instead of silently
  becoming an empty store — which was both data loss and a replay bypass.
- **Audit completeness.** Blocked attempts are now durably audited; the
  success-path audit moved inside the try/except so an audit failure returns a
  successful result with `audited=False` rather than escaping as an exception
  and inviting a duplicate send.
- **Audit hash chain** with restart markers and corrupt-tail detection, so the
  record cannot be silently edited.
- **Recipient domain classification fixed.** Substring matching classified
  `acme.com.attacker.io` as internal; now exact match or dot-boundary suffix.
- **Retry cap.** `mark_failed` capped at 3 retries then terminal `failed`,
  instead of re-arming approval forever on an ambiguous 504.
- **Stuck-action reconciliation** — `executing` timeout detection and recovery,
  surfaced in doctor and readiness.
- **HMAC webhook signatures bind a timestamp**, with a 300s skew bound and a
  replay lease with fencing.
- **`store_name` path-escape guard**, backup retention, and fail-loud on a
  missing project root.

### Agent execution seam (new)

Under the `agent` provider the AI agent performs mutations with its own
connector tools, so there is no `@guarded` call site to run the state machine.
Every agent-driven write previously dead-ended: the provider raises
`NotImplementedError`, and `mark_executed()` was unreachable from the CLI.

- **`review_queue.py claim`** transitions approved→executing and returns the
  action envelope. It must run *before* the side effect — that is where a
  lapsed approval is caught — and it makes the claim exclusive.
- **`review_queue.py record-execution`** transitions executing→executed/failed.
  A recording verb only: it cannot approve, cannot skip the claim, and cannot
  revive a terminal action. Results carry `executed_externally` so the audit
  never implies more provenance than it has.

### Operability

- **`chief_of_staff.py capabilities`** — one machine-readable call for the
  active provider, supported and refused actions *with reasons*, whether the
  provider was ever live-verified, project root, and state durability.
- **Real dependency preflight.** `doctor` imported 3 of 7 declared packages via
  `find_spec`, which reports an installed-but-broken package as present. It now
  imports all 7, catches `BaseException` (a pyo3 panic from a broken
  `cryptography` is not an `Exception`), suppresses import-time stdout chatter
  that was corrupting `doctor --json`, and names a remedy. CI gained a matching
  dependency-sanity step.
- **Hosted cloud session guardrails.** When `CLAUDE_CODE_REMOTE_SESSION_ID` is
  set, the credential-holding providers are refused: cloud environment
  variables are plain text and readable by anyone using the environment. The
  `agent` provider stays available, and the capability report warns that state
  there is ephemeral.
- **MCP session recovery** — 202/204 accepted, 404 recovery, bounded retry.

### Knowledge

- **OKF 0.2** — sequence numbers, aliases, relations, numeric confidence.
- **Wiki search** subcommand with keyword, alias and tag retrieval, stop-word
  filtering and token-boundary matching.
- **Two new hooks** — `note_capture_reminder` (post_llm_call) and
  `wiki_context_injection` (pre_llm_call), bringing the total to nine.

### Skills

- **`news-monitoring` vendored in** — nineteen skills, eighteen registered on a
  default install (`esign-connector` appears once DocuSeal is configured).
- **`deep-research` v1.6.0** — adaptive depth, clarification phase, token
  budget, numbered citations.
- Writing-for-agents pass across five skills: workspace-access extracted to
  `shared/docs/`, routing tails removed, descriptions rewritten, completion
  criteria added.

### Claude Desktop / Cowork compatibility (additive)

Hermes remains the runtime of record. These files are additive and are not read
by it:

- **`CLAUDE.md`** — the operating contract an agent reads on arrival: the
  invariant, the command index, the fetch/compute split, both execution paths,
  and the prohibitions.
- **`.claude-plugin/plugin.json` + `marketplace.json`** — makes
  `/plugin marketplace add moonlight-lupin/chief-of-staff-agent` work so the
  skills load natively in Claude Desktop and Cowork without conversion.
- **`docs/SETUP.md`** gained a hosted-cloud-session section covering the
  missing secrets store, the Trusted domain allowlist (which excludes every
  workspace endpoint), polling instead of webhooks, and ephemeral state.

### Release hygiene

- Version pins unified at 0.4.0 across `plugin.yaml`, `pyproject.toml`, the
  entrypoint, and the README, with a test asserting they agree. `--help` had
  been advertising v0.3.7 while the plugin shipped 0.3.24.
- **Demo data re-anchored on today.** `examples/sample-workspace.json` is
  pinned to a fixed day, so `demo` printed month-old mail and past meetings
  under "next 48h". Timestamps now shift by whole days at load.
- **`NOTICE`** recording the vendored MIT `deep-research` skill inside this
  Apache-2.0 work.
- **`SECURITY.md`** with a private disclosure route and an explicit in-scope
  list, and **`CONTRIBUTING.md`** covering the invariants a change may not break.
- **Lint enforced.** 113 ruff findings → 0, and CI runs ruff over `shared/`,
  `skills/`, `hooks.py` and `__init__.py` without `continue-on-error`.
- CI matrix on Python 3.11 and 3.12.

### Tests

1,802 → **2,002 passing**. New coverage for guardrail default-deny, legacy ID
canonicalization, multiprocessing races on every state store, blocked-path
auditing, retry caps, the claim/record-execution refusal paths, dependency
preflight, capability reporting, and hosted-session refusal.

### Deferred

Native Microsoft 365 Graph (tenant-wide authority constraint, token cache and
proactive refresh, real `mail.move` reversibility, timezone and upload
hardening) remains open pending a dedicated Entra tenant. See the M365 section
of `docs/PRODUCTION_ROADMAP.md`.

## v0.3.24 — Vendor deep-research v1.4.0 (structured evidence + evidence-basis discipline)

### Skills

- **`deep-research` vendored to v1.4.0** — brings the full v1.3.0 structured-evidence feature set
  into the CoS plugin alongside the v0.3.23 evidence-basis discipline. The vendored copy now ships:

  - **Structured `evidence.json` intermediate** (§3e) — separates evidence gathering from report
    writing; makes fabrication detectable. Schema + worked example in
    `references/structured-evidence-format.md`.
  - **Source quality tiers** (primary/secondary/tertiary) with 3×/2×/1× weighting, a
    healthy/acceptable/weak distribution check, and conflict resolution by quality (primary >
    secondary > tertiary).
  - **Overview-first report structure** — executive summary + at-a-glance comparison table before
    detailed analysis.
  - **Refute polarity requirement** — round 2+ must include ≥1 counter-evidence query.
  - **Language anchoring** — detect and normalize output to a BCP 47 tag.
  - **Four-label evidence-basis discipline** ([VERIFIED]/[SOURCED]/[REASONED]/[ESTIMATED]) from
    v0.3.23, now documented as **orthogonal** to source-quality tiers: tiers classify the
    *source*, labels classify the *fact*. The skill uses both.
  - **3 reference files** — `structured-evidence-format.md`, `real-estate-investment-analysis.md`,
    `skills-monetization.md`.

### Version pins

Bumped plugin/pyproject/entrypoint/README to v0.3.24 with CHANGELOG entry and the two
hardcoded version-pin tests.

## v0.3.23 — Evidence-basis discipline in deep-research

### Skills

- **`deep-research` now tags every material fact with its evidence basis**, adapting
  pere-toolkit's canonical four-label discipline (`evidence.LABELS`) from financial
  *figures* to research *facts*:

  | Label | A fact is `[LABEL]` when it is… |
  |---|---|
  | `[VERIFIED]` | corroborated across ≥2 independent, cited, dated sources |
  | `[SOURCED]` | stated by one named / cited source, not independently corroborated |
  | `[REASONED]` | analytical judgement / inference — not stated by any source |
  | `[ESTIMATED]` | a calculation or stated assumption |

  The labels thread through the loop: graded at extraction (§3c), carried in the
  synthesis state and promoted `[SOURCED]`→`[VERIFIED]` on corroboration (§3d), and
  rendered inline in the final report with a pasted **Evidence key** legend (§4).
  Reports lead on `[VERIFIED]`/`[SOURCED]` and mark `[REASONED]`/`[ESTIMATED]` as
  indicative. New pitfalls cover improvised labels, overclaiming basis, and restating
  precision the source didn't give. Skill frontmatter → v1.1.0; routing fixture
  updated to require the tags + key.

  Note: unlike pere-toolkit (which enforces this with a `memo_qa` Stop hook), this is a
  prompt-level discipline in CoS — faithful to the spirit, not machine-gated.

## v0.3.22 — Reply-awareness follow-ups + vendor updated deep-research skill

### Fixes

- **Operator match is exact, not a substring.** `daily_briefing._is_from_operator`
  now parses the bare address out of `"Name <addr>"` / `"addr"` senders and
  compares it exactly, instead of a loose `operator in sender` test that could
  misfire (e.g. `"me@example.com"` is a substring of `"jme@example.com"`, or the
  address appearing inside an unrelated display name).
- **Reply-scan window is configurable.** The sent-mail lookback (default 14 days)
  and message cap (default 50) can be widened so older replies still suppress,
  via an optional `briefing` config section:

  ```yaml
  briefing:
    reply_lookback_days: 30
    reply_sent_max: 200
  ```

  Invalid or non-positive values fall back to the defaults. Behaviour is
  unchanged when the section is absent.

### Tests

- Full `collect_gmail` unread→reply suppression now exercised end-to-end with the
  real Composio field shape (`messageTimestamp` + `"Name <email>"` sender +
  `threadId`), closing the gap the live 0→5 index check left open. Added
  coverage for the exact-match fix and the configurable scan window.

### Skills

- **Vendored the updated `deep-research` skill from the `agent_skills` repo**
  (v1.0.0): adds `fact-checker` / `source-tracker` cross-references and a
  "When NOT to use" note routing single-claim verification to `fact-checker`,
  and adds the `evals/routing-fixtures.json` routing spec the SKILL.md
  references. Removed a stray research-output file (`skills-monetization.md`)
  that had landed in the skill's `references/` dir. License stays Apache-2.0 to
  match the plugin.

## v0.3.21 — Daily briefing reply awareness (suppress already-answered threads)

### Fixes

- **Briefing no longer lists inbound mail the operator already replied to.**
  `daily_briefing.collect_gmail` fetches recent `in:sent` mail (14-day lookback),
  indexes by `thread_id` / `conversationId`, and drops inbox/unread/engagement
  hits when the operator (`google.delegate_email` or `user.email`) has a later
  message in the same thread. Sent-side queries (`sent_followup`) are left alone.
  Agent-provided `--input` envelopes get the same filter when both sides of the
  thread are present. Render notes how many messages were suppressed.
- Messages without a thread id cannot be matched and remain listed (fail-open).
- **Reply index parses Composio's `messageTimestamp` field** (ISO-8601), not only
  `date` / `internalDate` — without this the index was empty against the live
  Composio Google provider and nothing was ever suppressed. All parsed datetimes
  are normalised to timezone-aware UTC so an aware/naive mix can never raise
  `TypeError` during the in-thread comparison. Live-verified 2026-07-17: the
  reply index went from 0 → 5 entries on the real mailbox once `messageTimestamp`
  was recognised.

## v0.3.20 — Improve OneDrive Business files.untrash wiring (capability stays False)

Improves the OneDrive-for-Business restore wiring from PR #17/#19, but the
`files.untrash` capability **stays False** for all Microsoft providers — a live
probe showed the path still does not work end-to-end.

### Fixes / wiring

- **`composio_microsoft` `files.untrash` now has a real Business fallback.**
  PR #17 left the SharePoint path half-wired: after Personal Graph
  (`ONE_DRIVE_RESTORE_DRIVE_ITEM`) returned "Operation not supported", the
  code raised instead of listing/restoring the SharePoint recycle bin.
  `_ms_files_untrash` now mirrors the m365 flow — GUID → SharePoint restore;
  else Personal Graph; on Personal-only failure → recycle-bin lookup by the
  leaf name from the same-session `files_trash` (or pass `restore_target`).
- **Personal-site `site_name` scoping.** SharePoint recycle tools defaulted to
  the tenant root site. Trash/untrash now derive `/personal/{user}` from the
  item `webUrl` (or `ONE_DRIVE_GET_ROOT`) and pass it as `site_name`. Override
  with `integrations.workspace.sharepoint_site_name` (e.g.
  `/personal/user_contoso_com`).
- **SharePoint toolkit in microsoft bootstrap.** Default toolkits are now
  `[outlook, one_drive, share_point]`; connect/verify docs and next-steps
  include `--connect share_point`.

### Capability stays False — not live-verified

- **`files.untrash` remains `False`** for `composio_microsoft`,
  `composio_microsoft:mcp`, and `m365`. A 2026-07-17 live probe on a real
  OneDrive-for-Business account — **with the SharePoint toolkit connected and a
  correctly derived `/personal/…` `site_name`** — still failed end-to-end: the
  `ONE_DRIVE_DELETE_ITEM`'d file never surfaced in
  `SHARE_POINT_LIST_RECYCLE_BIN_ITEMS` (0 items after a 45s wait), so
  `files_trash` captured no `restore_target` and `files_untrash` returned
  `success=False`. The OneDrive recycle bin and the queried SharePoint recycle
  bin do not line up. The wiring ships so the path can be debugged live; the
  capability flips True only after a live run actually restores a file.

## v0.3.19 — OneDrive Business files.untrash wiring (SharePoint recycle bin)

### Features

- **OneDrive `files.untrash` restore path wired** (Personal Graph + Business
  SharePoint recycle bin). **Capability stays `False`** for `composio_microsoft`,
  `composio_microsoft:mcp`, and `m365` until a live run restores a file — the
  discipline is "True only when execution-verified." A 2026-07-17 live probe on a
  real OneDrive-for-Business account confirmed the flip would be premature:
  Personal Graph restore (`ONE_DRIVE_RESTORE_DRIVE_ITEM`) returns "Operation not
  supported" for work accounts, and the Business SharePoint fallback needs a
  connected SharePoint toolkit / `Sites.ReadWrite.All` that was not present.
  - **`m365`**: `files_trash` captures name + SharePoint recycle-bin GUID as
    `restore_target`; `files_untrash` tries Graph Personal restore then falls
    back to SharePoint REST `RecycleBin/RestoreByIds` (host-scoped SPO token;
    requires SharePoint app permission `Sites.ReadWrite.All`). Not live-verified.
  - **`composio_microsoft`**: Personal via `ONE_DRIVE_RESTORE_DRIVE_ITEM`;
    Business via `SHARE_POINT_LIST_RECYCLE_BIN_ITEMS` /
    `SHARE_POINT_RESTORE_RECYCLE_BIN_ITEM` (SharePoint toolkit must be
    connected so trash can persist `restore_target`). Not live-verified.
- **`delete_actions.py restore`** already prefers `restore_target`, so once the
  Business path is live-verified and enabled, executed trash actions restore by
  recycle-bin GUID automatically.

## v0.3.18 — Ship the Google-first beta daily loop

### Features

- **`chief_of_staff.py daily` collects live briefing sources.** The briefing
  panel now runs `daily_briefing.collect()` in read-only mode (Gmail + Calendar
  reads plus local deadlines/pipeline/todos/invoices/email_org) without recording
  delivery or writing `.last_briefing`. Summary/JSON show per-source status,
  counts, and urgent items. Provider writes remain forbidden.
- **Smoke-test write detection fixed.** `no_writes` now compares every
  snapshotted path (business YAML + wiki + state), not only `.*` files — so
  mutations to `pipeline.yaml` / `invoices.yaml` / wiki pages fail the smoke test.
- **Readiness daily-loop row reports live source health.** Missing credentials
  or failed Gmail/Calendar reads surface as WARN with detail, while local panels
  still render.
- **Beta docs / examples refreshed.** `BETA_DAILY_LOOP.md` and
  `BETA_READINESS_CHECKLIST.md` document the Google-first operator path
  (`python3`, readiness, live sources). Composio Google Drive allowlists in
  `company.yaml` examples include create-from-text / trash / untrash.

## v0.3.17 — document.handoff polish + Drive files.untrash

### Features

- **`document.handoff` readiness.** Preflight/dry-run/error copy no longer claims
  only Composio can draft; `google_api` (SA REST draft) and Composio Microsoft are
  first-class. Upload gaps fail closed before side effects; `--allow-partial`
  covers draft-only gaps. Google Composio uploads normalize `id` + `webViewLink` /
  `link` for draft body linking. Docs (`LIVE_TEST_CHECKLIST`, `APPROVAL_RUNBOOK`)
  and `PROVIDER_RECOMMENDATIONS` updated.
- **`files.untrash` soft-delete restore symmetry (Google).** New capability
  `files.untrash` (legacy `drive.untrash`) with ABC + `drive_untrash` alias.
  - `google_api`: Drive REST `files.update` `trashed=False` (SA + delegate)
  - Composio Google: `GOOGLEDRIVE_UNTRASH_FILE`
  - `delete_actions.py restore` for executed `drive.trash` → `drive_untrash`
  - Guardrails: `files.untrash` in `WRITE_ACTIONS` / `SAFE_WRITE_ACTIONS`
- **OneDrive untrash wired but capability False.** Methods call
  `ONE_DRIVE_RESTORE_DRIVE_ITEM` (Composio MS) / Graph `POST …/restore` (`m365`);
  kept False with Personal-only reason until Business/SharePoint is verified.
- **Beta daily-loop notes.** `BETA_DAILY_LOOP.md` / `BETA_READINESS_CHECKLIST.md`
  refreshed for Google-first beta; Outlook email-org E2E deferred without Entra.

## v0.3.16 — Keyless binary file upload via MCP sandbox staging

### Features

- **Composio binary file upload without `COMPOSIO_API_KEY`.** Google Drive and
  OneDrive binary uploads (`.pdf`/`.docx`) previously required a project
  `COMPOSIO_API_KEY` to stage a `FileUploadable` through the Files REST API (the
  MCP key 401s). New `composio_files.stage_file_uploadable_via_sandbox()` stages
  the local file into Composio's object store over the **MCP meta-tools**
  (`COMPOSIO_REMOTE_BASH_TOOL` base64-pipes the bytes into the remote sandbox with
  an md5 integrity check; `COMPOSIO_REMOTE_WORKBENCH.upload_local_file()` returns
  the `s3key`), needing **only `COMPOSIO_MCP_KEY`**. The provider's `files_upload`
  (both families) routes binary through this path; text is unchanged
  (`GOOGLEDRIVE_CREATE_FILE_FROM_TEXT` / `ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE`).
  The REST stager stays available for keyed setups but is no longer the default.
- **`files.upload` → True** for `composio`, `composio:mcp`, `composio_microsoft`,
  `composio_microsoft:mcp`; the `COMPOSIO_API_KEY` `UNSUPPORTED_REASONS` entries
  are removed. `--verify-writes` now exercises `files_write` instead of skipping
  it. **Execution-verified 2026-07-17** (only `COMPOSIO_MCP_KEY`): a throwaway
  `.pdf` uploaded to Google Drive and OneDrive and was trashed (Drive confirmed in
  Trash).

### Cleanup semantics

- Local temp file (caller-unlinked) and the sandbox working copy (removed in the
  stager's `finally`; sandbox is session-TTL reclaimed) are cleaned up. The
  intermediate Composio **S3 staged object** is **access-revoked** (its presigned
  URL 403s immediately) and **reclaimed on the tool-router session TTL** — there
  is no MCP delete for it, so it is not purged on demand (an explicit delete would
  need the Files REST API). Documented in the `files_upload` CLEANUP note.

### Constraints

- Composio upload tools cap `FileUploadable` at 5 MB (the stager rejects larger);
  base64 inflates the transfer ~1.33× (chunked ~700 KB/round-trip via heredoc,
  bypassing `MAX_ARG_STRLEN`).

## v0.3.15 — Google Drive trash, google_api drafts, Outlook email-org

### Features

- **Google Composio Drive files**: text uploads now use
  `GOOGLEDRIVE_CREATE_FILE_FROM_TEXT` (`file_name`+`text_content`, MCP-native — no
  Files-API staging, no `COMPOSIO_API_KEY`); binary uploads use
  `GOOGLEDRIVE_UPLOAD_FILE` with a staged `file_to_upload` (the raw `file_path`
  was silently ignored — fixed). **`files.trash` → True (execution-verified
  2026-07-16):** a text file created via `CREATE_FILE_FROM_TEXT` was trashed via
  `GOOGLEDRIVE_TRASH_FILE` and confirmed in Drive Trash. **`files.upload` stays
  False** (mirrors OneDrive): text works over MCP, but binary document filing
  needs `COMPOSIO_API_KEY` — or use the `google_api` service-account provider,
  which uploads binary to Drive directly with no Composio key.
- **`google_api` `mail.draft`**: create drafts via Gmail REST
  (`users.drafts.create`) with service-account domain-wide delegation when
  `google_api.py` has no draft CLI. Uses the `gmail.modify` scope (already in
  the provider's standard SCOPES, so no new admin delegation) and surfaces the
  message id as `id` (keeps `draft_id`). **Execution-verified 2026-07-16** — the
  draft landed in the delegate's Drafts folder. Unlocks `document.handoff` on
  google_api.
- **email-organisation Composio Microsoft**: classify → suggest → prepare path
  hardened for Outlook categories (displayName as tag id, Outlook message
  shape, category-aware suggestion copy). Live checklist covers prepare →
  review_queue execute.

## v0.3.14 — Google cleanup hardening + Outlook inspect-labels

### Fixes / hardening

- **Google Composio mail.tag / archive / unarchive / trash / untrash**:
  - Reject Gmail draft ids (`r-…`) on label/trash paths (tools need hex message ids).
  - Resolve label display names → `Label_…` ids before `GMAIL_ADD_LABEL_TO_EMAIL`.
  - `mail_create_tag` reuses an existing label id on 409/already-exists (verify
    path no longer falls back to the bare display name).
  - `workspace_verify` looks up tag ids via `mail_list_tags` on reuse.
  - **Execution-verified 2026-07-16 (live Gmail):** with the hardened path,
    `--verify-writes` on `family: google` ran green (`write_ready: yes`) — a full
    archive→unarchive→trash→untrash cycle plus tag apply on real hex message ids,
    no id-shape errors. These five capabilities are now **True** for
    `composio` / `composio:mcp`.

### Features

- **email-organisation `inspect-labels` (Composio Microsoft)**: Outlook-aware
  summary (`Outlook Category Inspection`, `tag_surface: outlook_categories`);
  `parse_labels` accepts `displayName` / missing `type` for master categories.

## v0.3.13 — Hermes Composio reads + Google Composio cleanup/tags/send

### Features

- **Hermes Composio MCP as read front-end**: document the fetch/compute split when
  Hermes already has Composio MCP connected — agent fetches reads →
  `schemas.py` envelope → `--input`; writes stay on `get_workspace_client`
  (`@guarded` + audit). Updated `agent` provider guidance, daily-briefing /
  weekly-review / meeting-prep Workspace Access, and `docs/SETUP.md`.
- **Google Composio parity** (catalog-wired from docs.composio.dev/toolkits/gmail):
  - `mail_list_tags` → `GMAIL_LIST_LABELS`
  - `mail_create_tag` → `GMAIL_CREATE_LABEL`
  - `mail_tag` / archive / unarchive → `GMAIL_ADD_LABEL_TO_EMAIL`
  - `mail_trash` / `mail_untrash` → `GMAIL_MOVE_TO_TRASH` / `GMAIL_UNTRASH_MESSAGE`
  - `mail_send` → `GMAIL_SEND_EMAIL` (approval-gated, same model as MS)
  - **Execution-verified 2026-07-16 (live Gmail):** `mail.list_tags`
    (`GMAIL_LIST_LABELS`), `mail.create_tag` (`GMAIL_CREATE_LABEL`), `mail.send`
    (`GMAIL_SEND_EMAIL`, sent + received) → capabilities True.
  - **Wired but NOT yet verified → False:** `mail.tag` / `mail.archive` /
    `mail.unarchive` / `mail.trash` / `mail.untrash`. The live probe rejected a
    Gmail draft id where a hex message id is required; `mail_create_draft` now
    surfaces the underlying `message.id` (the fix), and these flip True once
    `--verify-writes` re-runs green on `family: google`.
  - Still False: `mail.list_folders` / `mail.move`, `calendar.cancel`, `files.trash`
- **email-organisation**: skill + live checklist cover Composio Microsoft
  Outlook categories (Phase 4) and the CoS-only write path.

### Unchanged on purpose

- `calendar.cancel` remains False (no restore-path parity).
- Composio Microsoft `files.upload` remains False until `COMPOSIO_API_KEY`
  enables binary filing (text `CREATE_TEXT_FILE` still works when called).

## v0.3.12 — Composio Microsoft Phase 4 categories + MCP-native OneDrive text upload

### Features

- **Outlook categories (Phase 4)**:
  - `mail_list_tags` → `OUTLOOK_GET_MASTER_CATEGORIES`
  - `mail_create_tag` → `OUTLOOK_CREATE_USER_MASTER_CATEGORY`
  - `mail_tag` → `OUTLOOK_GET_MESSAGE` (current categories) +
    `OUTLOOK_UPDATE_EMAIL` (append category displayName)
  - Tag id IS the category `displayName` (same contract as native m365)
  - Capabilities: `mail.list_tags` / `mail.tag` / `mail.create_tag` True for
    `composio_microsoft` / `:mcp`
- **OneDrive text uploads without Files API**:
  - Plain-text files use `ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE` (`name` +
    `content` + optional `folder`) over Connect MCP — no
    `COMPOSIO_API_KEY` / FileUploadable staging
  - Binary files still use `ONE_DRIVE_ONEDRIVE_UPLOAD_FILE` with staged
    `{name,mimetype,s3key}` (project `x-api-key`) or a public `source_url`
  - Files staging retries `x-consumer-api-key` when `x-api-key` returns 401/403
  - Capabilities: `files.download` / `files.trash` True (execution-verified
    2026-07-16). `files.upload` stays **False**: the text path works over MCP,
    but binary document filing (`.pdf`/`.docx`) needs `COMPOSIO_API_KEY`, and a
    coarse boolean must not over-promise it (set the key to enable)

### Docs

- `docs/SETUP.md` clarifies text vs binary OneDrive paths and Phase 4 tags
  (see also https://composio.dev/toolkits/one_drive).

## v0.3.11 — Composio Microsoft Phase 3 folders + approval-gated mail.send

### Features

- **Folder-first Outlook organise (Phase 3)**:
  - `mail_list_folders` → `OUTLOOK_LIST_MAIL_FOLDERS` (also native m365
    `GET /mailFolders`)
  - `mail_move_to_folder` → `OUTLOOK_MOVE_MESSAGE` / Graph move with any folder
    id or well-known name
  - `mail_resolve_folder` helper (well-known names + display-name lookup)
  - Capabilities: `mail.list_folders` / `mail.move` True for
    `composio_microsoft` / `:mcp` and `m365`
- **Approval-gated `mail.send`** for Composio Microsoft via `OUTLOOK_SEND_EMAIL`:
  - Capability True (still destructive)
  - Preferred path: `send_email.py prepare → approve → execute` (works for any
    provider that supports `mail.send`, including m365 / google_api /
    composio_microsoft)
  - Direct calls require `CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1` (plus
    `CHIEF_OF_STAFF_AUTO_APPROVE=1` when approval already happened via the queue)
  - Guardrail messaging names the approve→execute path
- Google Composio `mail.send` remains intentionally disabled.

### Docs

- `docs/SETUP.md` updated for Phase 3 folders and approved send.

## v0.3.10 — Composio OneDrive FileUploadable staging + mail-move verify

### Features

- **Composio Files API staging** (`shared/scripts/composio_files.py`): local files
  are staged via `POST /api/v3.1/files/upload/request` (v3 fallback) + presigned
  PUT, then passed to `ONE_DRIVE_ONEDRIVE_UPLOAD_FILE` as
  `{name, mimetype, s3key}`. Azure Blob PUTs set `x-ms-blob-type: BlockBlob`.
- **OneDrive download persist** prefers Composio `s3url` fetch before inline/
  base64 fallbacks.
- **Capability matrix** for `composio_microsoft` / `:mcp`: `files.upload` /
  `files.download` / `files.trash` and `mail.archive` / `mail.unarchive` /
  `mail.untrash` are True (share execution-verified `OUTLOOK_MOVE_MESSAGE`).
- **Verify harness** adds `mail_move_write` (draft → archive → inbox → trash →
  inbox → trash) and runs `files_write` (upload → optional download → trash)
  when those capabilities are present.

### Docs

- `docs/SETUP.md` updated for FileUploadable staging and the mail-move probe.

## v0.3.9 — Composio Microsoft cleanup + content writes (Phase 1+2)

### Features

- **Composio Microsoft mail cleanup** via catalog slug `OUTLOOK_MOVE_MESSAGE`:
  `mail_archive` → `archive`, `mail_trash` → `deleteditems` (soft-delete),
  `mail_unarchive` / `mail_untrash` → `inbox`. Returns `restore_target` like
  native m365. Does **not** use permanent `OUTLOOK_DELETE_MESSAGE`.
- **Composio Microsoft OneDrive trash** via `ONE_DRIVE_DELETE_ITEM` (recycle bin,
  not `ONE_DRIVE_DELETE_ITEM_PERMANENTLY`).
- **Content writes (Phase 2)** capability-True with **Composio catalog arg shapes**:
  `mail.draft` (`OUTLOOK_CREATE_DRAFT`), `calendar.create` /
  `calendar.update`, `files.upload` / `files.download`. Args no longer send raw
  Graph JSON where the catalog expects Composio fields (`to_recipients`,
  `start_datetime`+`time_zone`, `folder`, `file_name`, …). Write payloads normalize
  a top-level `id` for `workspace_verify`.
- **`calendar_delete`** (`OUTLOOK_DELETE_CALENDAR_EVENT`) for verify cleanup;
  opt-in CLI `--verify-calendar-writes` (create→update→delete of a marked
  `[CoS verify]` event). Default `--verify-writes` still never creates events.
- **Verify draft cleanup without tags**: when `mail.tag` is unsupported, a
  successful draft probe still trashes the artefact (needed for Composio MS).
- **Capability matrix** for `composio_microsoft` / `:mcp`: cleanup + content
  writes True; `mail.send` / `calendar.cancel` / `mail.tag*` still False.
- Slugs overridable via `tool_slugs` (`mail_move`, `files_trash`,
  `calendar_delete`, …). Google Composio family still refuses MS-only cleanup
  methods.

### Docs

- `docs/SETUP.md` Composio Microsoft verification note updated for Phase 1+2.

## v0.3.8 — Code review fixes (v0.3.5→v0.3.7 review findings)

### Breaking changes

- **Composio Microsoft write capabilities** (`mail.draft`, `calendar.create/update`, `files.upload/download`) now report `False` (unsupported) until execution-verified. Write implementations and Graph-correct arg shapes remain in code. Use Google Composio or native M365 Graph for writes.
- **`esign-connector` skill** is no longer registered unless `esign.url` is configured in `company.yaml`. `self-sign` remains always registered.
- **`GmailClient.get_attachment`** restored to pre-v0.3.6 inline `gmail attachment` CLI verb. `download_attachment` is now a separate method using `tempfile.mkdtemp()` (0o700) instead of world-readable `/tmp`.
- **`connect_workspace`** config discovery logging is quiet by default; use `--verbose` or `CHIEF_OF_STAFF_DEBUG=1` for the resolved-path log.

### Security fixes

- **Doctor DocuSeal probe** refuses non-HTTPS URLs, metadata/link-local/loopback/private IPs, and host mismatch with `esign.domain` before attaching API key.
- **Runtime log scrubbing** extended: MSAL/Google token shapes (`ya29.*`, UUIDs, 48+ char tokens), JSON key-value secrets, URL-embedded tokens.
- **`sanitize_provider_error_detail()`** classifies/truncates auth failure blobs before logging to `events.jsonl`.
- **Assistant/company name validation** rejects newlines, double quotes, and >64 char names before YAML interpolation.
- **Dotenv parser** rejects suspicious process-control keys (`PATH`, `LD_PRELOAD`, `PYTHONPATH`, etc.) and strips inline comments.

### Correctness fixes

- **Soft Composio errors** (rate limits, auth failures) now raise `RuntimeError` instead of returning `{successful: False}` → no false `read_ready: true`.
- **Error classifier reordered**: connection errors checked before unknown-tool; bare `"not found"` removed from needles.
- **Identity scrub** completed: `google.domain`, `account_alias`, SA path, Drive root, company legal IDs, phones, `home_chat_id` all scrubbed on first install. Re-bootstrap preserves operator-edited values.
- **Shipped SKILL.md** descriptions now contain rendered defaults (`Chief of Staff` / `your organization`) — no more literal `{assistant_name}` placeholders.
- **Bootstrap overlay**: custom assistant names render to `skills.local/` (gitignored) instead of mutating tracked `skills/*/SKILL.md`.
- **`skills.local/` overlay** is now loaded by `register()` — named routing works end-to-end.
- **Microsoft calendar** date-only end times use `T23:59:59Z` (not zero-duration `T00:00:00Z`).
- **`@guarded` audit slugs** resolve dynamically from family — Microsoft audit records use `OUTLOOK_*` / `ONE_DRIVE_*` slugs.
- **`esign.admin_email`** legacy fallback: both `provider_email` (preferred) and `admin_email` (legacy) accepted.
- **Name-injection skip logic** unified via shared `is_default_assistant_name()` across bootstrap/doctor/hooks.
- **Family resolution** deduplicated into `composio_family.py`, shared by client and `connect_workspace`.
- **Query compile failure** now emits `warnings.warn()` before falling back to `{top: N}`.
- **Family/toolkit mismatch** warning on client init.
- **`list_attachments`** warns on unexpected shapes instead of silent `[]`.
- **`build_briefing()`** public API replaces `cmd_demo`'s private `_build_structured_briefing` call.

### Other

- MCP `clientInfo.version` updated to match plugin version.
- Freemail domain list expanded (`googlemail.com`, `live.com`, `icloud.com`, `proton.me`, etc.).
- README note: "Never paste API keys or secrets directly into chat logs."
- `.gitignore` entry for `skills.local/` overlay.
---

# Reconstructed history (v0.3.7 and earlier)

Everything below was reconstructed from git history on 2026-08-15. `CHANGELOG.md`
did not exist until v0.3.8, so these releases were never written up at the time.
Each entry summarises what its commits actually changed; they are terser than the
contemporaneous entries above, and detail that never reached a commit message is
not recoverable.

---

## v0.3.7 — Microsoft 365 via Composio (toolkit families, config-driven slugs)

Composio's managed OAuth — Connect Link, no Entra app, no admin consent — now
drives Microsoft 365: Outlook mail and calendar plus OneDrive.

- **Toolkit families.** `integrations.workspace.family: google | microsoft`.
  Google stays the default and its existing behaviour is treated as a
  byte-for-byte compatibility contract; the provider name `composio:mcp` is
  unchanged. Microsoft reports as `composio_microsoft:mcp`.
- **Config-driven tool slugs** rather than hardcoded names, so a slug rename
  upstream is a config edit.
- Live Composio M365 verification over two runs corrected the Outlook and
  OneDrive slugs, argument shapes, and envelope handling; OneDrive connected and
  `files_read` passed. Documentation was corrected to the live-verified names.
- Removed `LIVE_VERIFY_RESULTS.md`, a point-in-time test log that did not belong
  in the repository.

## v0.3.6 — Onboarding friction fixes and assistant naming

Every fix here traces to an observed stumble in a fresh-operator friction audit
(17 stumbles, 2 blockers) rather than to hypothesis.

- **Config auto-discovery.** `connect_workspace` resolves `company.yaml` through
  the same chain as `chief_of_staff` (`--config` > `CHIEF_OF_STAFF_CONFIG` >
  plugin default), and announces which one it used.
- **Assistant naming.** `bootstrap.py` injects `{assistant_name}` and
  `{company_name}` into the routing descriptions of five skills after
  `company.yaml` is written, so "Ask Arthur to check my email" routes to the
  operator's own Chief of Staff. Injection is idempotent, sandboxed in tests, and
  scrubs aliases. `doctor` warns when `assistant.name` is unset.
- **Gmail attachments.** `GmailClient` gains `list_attachments()` and
  `download_attachment()`, with a backward-compatible `get_attachment()` wrapper.
- Demo hygiene, the skill count corrected to 18, and `.env` exported properly.
- README rewritten for prospective adopters, then reordered to lead with the
  agent-driven install path.

## v0.3.5 — esign-connector v0.2.0 + council code review fixes

- **esign-connector v0.2.0**: dual-token auth, no browser login required.
- E-sign onboarding folded into `bootstrap` and the setup guide.
- Addressed a three-reviewer code review: 4 blocking, 8 major, 6 minor findings.
- 19 new tests for e-sign onboarding and the doctor DocuSeal check; a
  `User-Agent` header added to the DocuSeal health check.
- Version bumped across all three sources of truth (`plugin.yaml`,
  `pyproject.toml`, the entrypoint) plus the README status line, which was then
  guarded by the version test.

## v0.3.4 — Observability and self-diagnosis

Operational logging kept distinct from — and linked to, never merged with — the
audit log.

- **Structured runtime logger** (`runtime_log.py`): per-run JSONL events and a
  summary under `project_root/.runs/<run_id>/`, a stable schema, contextvars run
  context, cross-process propagation via `CHIEF_OF_STAFF_RUN_ID`, and
  conventional levels with `--log-level` / `--quiet`.
- **Deterministic diagnosis** and redacted support bundles, so a failure becomes
  a finding with a remediation command rather than a stack trace.
- Follow-up fixes for run-context integrity, transport failure events, and
  honest counters.

## v0.3.3 — Workspace readiness and onboarding

- **Per-capability verification** (`connect_workspace --verify`):
  `workspace_verify.py` probes auth, mail read, folder-scoped search, categories,
  calendar, and files independently, because a single `health_check()` cannot
  detect partially-granted Entra permissions. Read failures are detected via
  warning capture.
- **Honest readiness semantics.** `write_ready` reports `yes` only when writes
  were requested, at least one representative write actually ran, every test
  passed, and all cleanup succeeded. `partial` covers not-requested, unsupported,
  or not-tested; `no` on any tested-write or cleanup failure. No false positives.

## v0.3.2 — Microsoft 365 Tier-1 operational hardening

Graph API resilience behind a new `_send` HTTP seam, leaving the existing
`_request` / `requests.request` test seams untouched.

- **Throttle retry** on 429/503/504: up to 3 retries honouring `Retry-After`,
  falling back to 1s/2s/4s exponential backoff, capped at 30s per wait, with an
  injectable sleep. Exhausted retries fail exactly as a non-2xx does today.
- **Method-aware retry policy.** 503/504 no longer auto-retry non-idempotent
  methods. A gateway timeout is ambiguous — `sendMail`, draft, event, move, and
  category creation may have completed upstream — so these fail once with *"The
  request may have completed, but confirmation was not received. Do not retry
  automatically; verify the external system first."*
- **`@odata.nextLink` pagination** across mail search, calendar list, and file
  search.
- Token refresh.

## v0.3.1 — Provider-neutral workspace seam, agent fetch/compute split, M365 provider

The release that made the workspace layer provider-agnostic.

- **Neutral method names.** `WorkspaceClient` methods renamed to `mail_*` /
  `files_*` / mail tags; legacy `gmail_*` / `drive_*` names kept as deprecated
  base-class aliases.
- **`guarded()` decorator** factors confirm → run → audit boilerplate out of the
  providers into one non-optional wrapper. Legacy action-id strings preserved for
  already-stored queues.
- **Registry-based provider factory**: `google_api | composio | m365 | agent`.
- **The `agent` provider**, introducing the fetch/compute split — the AI agent
  fetches with its own connectors and feeds compute scripts via `--input`.
- **Microsoft 365 provider** via Graph.
- Review follow-ups closed six blockers: every mutating neutral action id is now
  classified (`mail.archive/unarchive/trash/untrash/tag/create_tag`,
  `calendar.cancel`, `files.trash` as SAFE_WRITE; `mail.send` stays DESTRUCTIVE);
  inbox and system-folder translation maps Gmail scopes to Graph well-known
  folders (`INBOX→inbox`, `SENT→sentitems`, `DRAFT→drafts`, `TRASH→deleteditems`,
  `SPAM→junkemail`), with `label:UNREAD` setting the unread flag and only
  non-system labels becoming Outlook categories; plus delete-action state
  integrity and CI.

## v0.3.0 — Daily operating loop beta

One-command front door to every Chief-of-Staff subsystem. Read-only
orchestration: observe, summarise, prioritise, recommend next commands. No
approvals, no executions, no provider writes, no mutations.

```bash
python shared/scripts/chief_of_staff.py daily --summary
python shared/scripts/chief_of_staff.py daily --json
```

- Smoke-test write detection expanded to watch mtime on `pipeline.yaml`,
  `invoices.yaml`, `expenses.yaml`, and `wiki/*.md` in addition to hidden
  dotfiles — so a claimed read-only run is actually verified as one.
- **Relicensed from MIT to Apache 2.0**, including all 17 `SKILL.md` frontmatters
  and the structure test, which the initial relicense commit had missed.
- User-focused project README added.
- Tests: 1,071 passing.

## v0.2.8 — CRM / Pipeline Manager hardening

A lightweight CRM layer: deals tracked in `pipeline.yaml`, stale opportunities
visible, notes, documents, and invoices linking back to deals, CRM updates
auditable, and autonomous flows able to suggest but never mutate.

- New `pipeline_actions.py` execution module: load, save, find by id or name,
  validate.
- Invoice linkage made canonical via `invoices.yaml` `deal_id` rather than a
  document-type heuristic — "Contract Signed without invoice" now checks whether
  the deal id appears in `invoices.yaml`, and "invoiced but not paid" checks the
  linked invoice's status, so deals with paid invoices stop being flagged.

## v0.2.7 — Note-taking and memory hardening, rollback, lint

Reliability hardening for autonomous memory and wiki maintenance: the agent can
maintain notes automatically, but every change is linted, logged, reversible, and
visible in the daily briefing.

- `wiki_curator.py lint` and `memory.py lint`, both with `--summary`.
- Rollback for knowledge changes.

## v0.2.6 — Bookkeeper invoice ingestion hardening

- Detect invoice-like material from local events, extract structured candidates,
  validate, duplicate-check, route through the review queue, and write to
  `invoices.yaml` only after explicit approval.
- `review_queue.py` switched to the canonical `action_risk.get_action_risk()`
  instead of a local duplicate.
- Fixed approved `bookkeeper.invoice.record` actions being rejected:
  `require_capability()` only knows Gmail/Calendar/Drive actions, and the
  bookkeeper branch sat inside the workspace try/except after the capability
  check, so it was never reached for real executions.

## v0.2.5 — Review queue / operator UX

A unified operator review queue: one CLI for listing, previewing, approving,
dismissing, executing, and auditing pending actions and suggestions, replacing a
scatter of separate scripts.

## v0.2.4 — Autonomous memory maintenance and wiki curation

Chief of Staff can keep the private second brain tidy, current, linked, and
source-backed; report meaningful knowledge changes; and reserve approval for
destructive, external, or high-impact knowledge decisions.

- New `memory.py` structured memory system.
- `memory.py` gained `_preferred_text()` to extract human-facing fields
  (summary, subject, title, body) before falling back to raw JSON, improving
  extraction quality.
- Knowledge reporting corrected: the briefing had been conflating memory records
  with wiki pages under `pages_created` / `pages_updated`; wiki and memory are
  now counted separately, with `wiki_curator.py` logging its own changes.
- Company-specific names, projects, and addresses scrubbed from test data.
- Tests: 947 passing.

## v0.2.3 — Daily workflow polish

Turned the daily briefing into the operator's primary surface, with structured
sections, priority buckets, risk grouping, and a one-command run.

- `action_risk.py`: deterministic high/medium/low classification per action type,
  shared by the briefing and the review queue.
- `briefing_sources.py`: read-only data collectors.

## v0.2.2 — Live beta hardening

- `doctor --summary` one-line readiness report (READY / WARNINGS / NOT READY).
- New checks: webhook config (OIDC/HMAC/channel token), state-file JSON
  integrity, and orphaned `executing` actions.
- **Age-gated the orphan reset.** Both `doctor --fix` and `state_tools repair`
  had been resetting *all* executing actions unconditionally, risking duplicate
  sends, labels, uploads, and calendar changes if an action was genuinely
  executing when repair ran. Now a 15-minute minimum age applies.
- End-to-end tests and a smoke test.

## v0.2.1 — De-hardcode paths, register email-organisation

- `config_loader.get_hermes_home()` resolves `CHIEF_OF_STAFF_HERMES_HOME` →
  `HERMES_HOME` → `~/.hermes`, with `get_default_project_root(slug)` for path
  construction. Every `~/.hermes` literal in `bootstrap.py` and `onboard.py`
  replaced — the change that made adoption outside Hermes possible.
- Fixed the fallback project root: `email_label_policy`, `event_store`,
  `pending_actions`, and `suggested_actions` were returning the Hermes home
  directory instead of `projects/default`, so state files would have been written
  loose under `~/.hermes/`.
- `email-organisation` registered; Composio backends collapsed; dependencies fixed.

## v0.2.0.2 — Cryptographic JWT verification

**Critical.** Pub/Sub JWTs are now cryptographically verified.
`_decode_jwt_unverified()` was replaced with the `google-auth` library —
`id_token.verify_oauth2_token(token, Request(), audience=aud,
clock_skew_in_seconds=30)`. The dead unverified helper was removed, `google-auth`
was added to `requirements.txt` (it had only been in `pyproject.toml`), and Gmail
endpoint status now reports `native (OIDC)` when a Pub/Sub audience and service
account are configured versus `HMAC (dev/proxy)` when only an HMAC secret is set.

## v0.2.0.1 — Internal beta execution and authentication hotfix

Pub/Sub OIDC JWT validation: `verify_pubsub_oidc()` checks the issuer
(`accounts.google.com`), `email_verified`, the audience, and the service-account
email, failing closed unless `CHIEF_OF_STAFF_PUBSUB_AUDIENCE` is configured.

## v0.2.0 — Internal beta: provider-native webhooks

- **Gmail Pub/Sub envelope decoding**: `adapt_gmail_pubsub()` base64url-decodes
  `message.data` and extracts `emailAddress` and `historyId` from inside the
  envelope, using the Pub/Sub `messageId` as the dedup delivery id, and handling
  malformed data gracefully.
- Calendar and Drive `X-Goog-*` header parsing.
- Operational hardening throughout.

## v0.1.29 — Webhook receiver and event replay safety

- `webhook_security.py`: HMAC-SHA256 signature verification via
  `X-Webhook-Signature`, constant-time comparison (`hmac.compare_digest`) against
  timing attacks, and replay protection tracking seen signatures with a 24h TTL.
- `validate_secret_config()` and `sign_payload()` for configuration checks and
  testing.
- `webhook_adapters.py` for provider-specific payload shapes.

## v0.1.28 — Email organisation digest and briefing integration

`render_email_org_digest()` produces a structured digest — totals, category
breakdown, label/archive/create-label suggestion counts, pending actions — and
`email_org_status_for_briefing()` returns a compact counts-only summary for the
daily briefing.

## v0.1.27 — Email classification and approval-gated organisation

- New capabilities `gmail.label` (apply an existing label) and
  `gmail.create_label`, both `True` for `google_api` and `False` for Composio.
- Classification with label suggestions, all routed through approval.

## v0.1.26 — Email organisation onboarding and label policy

New `email-organisation` sub-skill, `gmail_list_labels()` read-only listing on
`WorkspaceClient`, and `email_label_policy.py` with a `SYSTEM_LABELS` frozenset.

## v0.1.25 — Act-on-suggestion bridge

Split suggestion handling by consequence: `SAFE_READ_ACTIONS`
(`calendar.list`, `drive.search`, `gmail.search`) execute directly with no
mutation and no approval; `WRITE_ACTIONS` (`gmail.send`, `gmail.draft`,
`gmail.archive`, `gmail.trash`, …) require the approval path.

## v0.1.24 — Operator notification and suggestion digest

Introduced the distinction between **suggestion risk** (how risky it is to ignore
a suggestion) and **execution risk** (how risky the underlying action is), keeping
`risk` as a backward-compatible alias for the former.

## v0.1.23 — Suggested action generation

`suggested_actions.py`: suggestions carrying id, event id and key, action type,
title, reason, and risk. `poll_events.py` gained Drive polling, and its `--max`
flag was renamed `--limit` now that it covers both Gmail and Drive.

## v0.1.22 — Event state fix and polling connectors

`ingest_event()` now creates events in `classified` state rather than `received`,
since classification happens during ingest. `mark_surfaced()` works immediately
after ingest, which had been broken. Four new tests prove the full
classified → surfaced → processed lifecycle.

## v0.1.21 — Event ingestion foundation and idempotency

`event_store.py`: an event carries id, key, source, source id, type, payload,
summary, state, classification, and timestamps. The `source` + `source_id` pair
uniquely identifies an event, which is the idempotency key.

## v0.1.20 — Soft-delete live tests, restore completeness, operator docs

`docs/APPROVAL_RUNBOOK.md` — state machine diagram, timeout table, every command
for both `send_email` and `delete_actions`, the restore capability table,
failed-action retry instructions, risk classification, audit trail reference, and
the provider capability matrix — plus `docs/SOFT_DELETE_TEST_CHECKLIST.md`.

## v0.1.19 — Execution-state hardening and restore workflows

**The critical race fix.** `mark_executing()` transitions approved → executing
*before* any provider call and checks whether approval has lapsed first. If it
has, the action is marked expired and the provider is never called.
`mark_executed()` now requires `executing` state rather than `approved`. The
state machine becomes approved → executing → executed | failed.

## v0.1.18 — Soft-delete and archive architecture

Approved actions expire too: `APPROVED_EXPIRY_HOURS = 24`, with
`_is_approval_lapsed()` checking whether an approved action has gone stale. CC
wiring fixed end to end — `gmail_send()` accepts and forwards `cc`.

## v0.1.17 — Approval delivery, concurrency safety, pending-action UX

- **Optimistic versioning** on `.pending_actions.json`: `_save()` takes an
  `expected_version`, raising `ConcurrencyError` if the on-disk version changed,
  preventing lost updates from concurrent channels.
- **Approval expiry**: requested actions older than 72 hours are stale.

## v0.1.16 — Approval queue and gated Gmail send

The release that established the core rule: **no user-facing workflow calls
`gmail_send()` directly.** Everything goes through
prepare → preview → pending → approve → execute.

`pending_actions.py` stores actions as JSON under
`project_root/.pending_actions.json` with atomic tmp-and-replace writes and a
requested → approved → executed | cancelled state machine.

## v0.1.15 — Workflow preflight checks and safer execution plans

`--dry-run` on calendar create/update and Drive upload/download shows the plan
without calling the provider. Partial completion is now detected by inspecting
step results rather than by matching error text.

## v0.1.14 — Provider-aware CLI UX and workflow summaries

`action_result_cli.py` centralises result printing — JSON or human-readable,
single actions or multi-step workflows — extracting the reason from errors and
surfacing the provider recommendation.

## v0.1.13 — Provider capability truthfulness

Corrected the capability matrix: `google_api` `gmail.draft` was advertised as
`True` but `google_api.py` has no draft subcommand, so it became `False`. Added
`require_capability()` and `recommend_provider_for()` so an unsupported action
returns a clear error naming the provider that can do it.

## v0.1.12 — Google service-account hardening

`doctor` became provider-aware: the Google auth check skips with a clear message
when `provider=composio`, and under `google_api` checks the script, the service
account file, `account_alias`, `delegate_email`, and runs a delegated health
check.

## v0.1.11 — Workspace action adoption across core skills

Five skills moved off direct `google_api.py` calls onto `WorkspaceClient`:
calendar-manager, drive-filer, and three others, each returning `ActionResult`
under guardrails. Live testing also revealed that `COMPOSIO_MULTI_EXECUTE_TOOL`
expects tool input under `arguments`, not `input` — Gmail tools accepted both,
but Calendar Create rejected it.

## v0.1.10 — MCP-only polish, docs cleanup, safe action guardrails

## v0.1.9 — Remove the legacy Composio SDK backend

## v0.1.8 — MCP hardening and live execution polish

## v0.1.7 — Composio MCP execution adapter

## v0.1.6 — Composio workspace actions: Drive, drafts, calendar writes

## v0.1.5 — Composio workspace provider

`ComposioWorkspaceClient` implementing `gmail_search` (`GMAIL_FETCH_EMAILS`) and
`calendar_list` (`GOOGLECALENDAR_FIND_EVENT`), with `health_check` via
`session.toolkits(is_connected=True)`. Sessions are created from the configured
toolkits and allowlist, read-only by default, with metadata saved under
`project_root/.integrations/composio/session.json`. The factory now routes
`composio` here instead of raising. v0.1.5.1 added provider-gated config checks
and connection-status refresh.

## v0.1.4 — Workspace provider abstraction

The abstraction that everything since rests on.

- `workspace_client.py`: the `WorkspaceClient` ABC and `get_workspace_client()`
  factory, reading `integrations.workspace.provider` from config.
- `providers/google_workspace.py` wrapping `google_api.py` behind the neutral
  interface.
- Daily Briefing wired to `WorkspaceClient` for both Gmail and Calendar, so it
  would auto-switch to Composio the moment that provider landed.
- `doctor` uses `WorkspaceClient.health_check()` rather than a raw subprocess.
- Repository genericised: company-specific names removed from `plugin.yaml`, 14
  `SKILL.md` files, and `pyproject.toml`; the `phronesis` profile renamed
  `enterprise`; hardcoded account aliases replaced by config.
- Workspace tests monkeypatched so no real `google_api.py` is needed, making them
  CI-safe.
- Tests: 288 passing.

## v0.1.3 — Compatibility fixes

Skill profiles (`default` + `enterprise`) driven from `plugin.yaml` rather than
hardcoded in `__init__.py`; `queries.yaml` accepted in both list and mapping
form; `install_cron.py` accepting either delivery config shape; and
`CHIEF_OF_STAFF_AUDIT_STRICT` to make audit failures fatal per store, best-effort
by default.

## v0.1.2 — Hardening: atomic state, script-backed mutations, doctor, CI

The release that made mutations safe.

- `state_store.py` (atomic YAML writes, file locking, backup-before-write),
  `file_lock.py` (cross-process fcntl advisory locking), `audit_log.py`
  (append-only JSONL), `run_log.py` (idempotency records), `schemas.py`
  (validation for deals, invoices, expenses, todos).
- `deadlines.py` unifying statutory and custom deadlines, replacing duplicated
  logic.
- `doctor.py` with an 18-check health report and `--fix`; `bootstrap.py` for
  deterministic clone-to-setup; `install_cron.py` with `--dry-run`.
- Skill mutations became **script-backed rather than instruction-backed**:
  `pipeline.py`, `todo.py`, `invoices.py`, `sign_pdf.py`, `drive_map.py`,
  `daily_briefing.py`, `calendar_scan.py`.
- Packaging: `requirements.txt`, `pyproject.toml`, CI on push and PR, and
  `examples/` fixtures.
- Tests: 265 passing.

## v0.1.1 — Seven quality hooks

All opt-in, wired in `__init__.py`: company context primer, YAML integrity
checker, stale briefing detector, pipeline stage validator, format enforcer,
self-sign multi-block guard, and deadline urgency injection. 43 hook tests; 224
passing overall.

## v0.1.0 — Initial release

Sixteen skills: daily-briefing, deadline-tracker, note-taker, todo-list,
calendar-manager, drive-filer, meeting-prep, weekly-review, document-preparer,
pipeline-manager, bookkeeper, deep-research, entity-research, travel-itinerary,
backup, self-sign.

Infrastructure: `config_loader`, `date_utils`, the onboarding wizard, four
jurisdiction packs (SG/HK/US/UK), drive-map, Gmail query templates, a
YAML→SQLite migration guide, and five quality hooks (documented, opt-in).

Tests: 179 passing, 2 skipped. Licensed MIT — relicensed to Apache 2.0 at v0.3.0.
