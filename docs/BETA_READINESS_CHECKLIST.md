# Beta Readiness Checklist (v0.3.18)

Use this checklist before going live with the Chief-of-Staff daily operating loop.

## Workspace provider (pick one)

- [ ] **Google SA (`google_api`)** — preferred for first beta day
  - [ ] External `google_api.py` / google-workspace skill installed (or `GOOGLE_WORKSPACE_API`)
  - [ ] `google.service_account_path` exists
  - [ ] `google.account_alias` + `google.delegate_email` set
  - [ ] `queries.yaml` present (project sibling or `shared/config/queries.yaml`)
- [ ] **Composio Google** — MCP key + connected Gmail/Calendar/Drive
- [ ] **Composio Microsoft / native m365** — mail/calendar/files OK;
      OneDrive `files.untrash` wired but capability **False** (not live-verified:
      a 2026-07-17 probe with `share_point` connected still failed — the deleted
      file did not surface in the SharePoint recycle-bin listing); live Outlook
      email-org E2E deferred without Entra

## Pre-flight

- [ ] **1. Configure project root**
  - Ensure `company.yaml` exists at your project root
  - Verify `paths.project_root` points to the correct directory
  - Run: `python3 shared/scripts/chief_of_staff.py doctor --summary`

- [ ] **2. Run doctor**
  - All checks should be ok or warn (no fail)
  - Run: `python3 shared/scripts/chief_of_staff.py doctor --summary`

- [ ] **3. Run readiness**
  - `read_only_ready: YES` (WARN on daily live sources is OK if credentials pending)
  - Run: `python3 shared/scripts/chief_of_staff.py readiness --summary`

- [ ] **4. Run smoke-test**
  - All subsystems should render without crashing
  - `no_writes` must PASS (catches pipeline/invoice/wiki mutations)
  - Result should be PASS
  - Run: `python3 shared/scripts/chief_of_staff.py smoke-test --summary`

- [ ] **4b. Handoff preflight (optional write path)**
  - Run: `python3 skills/document-preparer/scripts/document_actions.py handoff --file <local> --to <you> --subject "beta" --body "ok" --preflight`
  - Expect `capabilities_ok: true` on Google / Composio Google / Composio Microsoft

## Daily operating loop

- [ ] **5. Run daily summary**
  - Review all 8 panels
  - Confirm **live sources** show Gmail/Calendar `ok` (or intentional degraded/failed)
  - Note any urgent items / review-queue work
  - Run: `python3 shared/scripts/chief_of_staff.py daily --summary`

- [ ] **6. Review pending actions**
  - Check requested actions in Review Queue
  - Preview, approve, or dismiss as appropriate
  - Run: `python3 shared/scripts/review_queue.py list --state requested`

- [ ] **7. Review invoice candidates**
  - Check bookkeeper candidates
  - Validate, prepare, or dismiss as appropriate
  - Run: `python3 skills/bookkeeper/scripts/invoice_ingest.py candidates --summary`

- [ ] **8. Review stale deals**
  - Check pipeline for stale opportunities
  - Follow up or move stage as appropriate
  - Run: `python3 skills/pipeline-manager/scripts/pipeline.py stale --summary`

## Knowledge health

- [ ] **9. Run memory/wiki lint**
  - Check for stale, low-confidence, uncited, duplicate records
  - Check for broken wiki links, missing frontmatter, stale pages
  - Run: `python3 shared/scripts/memory.py lint --summary`
  - Run: `python3 skills/note-taker/scripts/wiki_curator.py lint --summary`

- [ ] **10. Backup memory state**
  - Create timestamped backup before any maintenance
  - Run: `python3 shared/scripts/memory.py backup`

## Safety verification

- [ ] **11. Confirm no unexpected provider writes**
  - Check audit log for any unexpected Gmail/Drive/Calendar mutations
  - Verify no actions were auto-approved or auto-executed
  - Run: `python3 shared/scripts/state_tools.py inspect --summary`

## Sign-off

- [ ] All checks pass (or intentional WARNs understood)
- [ ] Live Gmail/Calendar reads confirmed on daily
- [ ] No unexpected mutations detected
- [ ] Daily summary reviewed
- [ ] Ready for beta operation
