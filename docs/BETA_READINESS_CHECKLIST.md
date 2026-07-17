# Beta Readiness Checklist (v0.3.17)

Use this checklist before going live with the Chief-of-Staff daily operating loop.

## Workspace provider (pick one)

- [ ] **Google SA (`google_api`)** — `document.handoff`, Drive trash/restore, daily loop
- [ ] **Composio Google** — same write surface via MCP (text + binary upload)
- [ ] **Composio Microsoft / native m365** — mail/calendar/files reads+writes OK;
      OneDrive `files.untrash` and live Outlook email-org E2E deferred without Entra

## Pre-flight

- [ ] **1. Configure project root**
  - Ensure `company.yaml` exists at your project root
  - Verify `paths.project_root` points to the correct directory
  - Run: `python shared/scripts/chief_of_staff.py doctor --summary`

- [ ] **2. Run doctor**
  - All checks should be ok or warn (no fail)
  - Run: `python shared/scripts/chief_of_staff.py doctor --summary`

- [ ] **3. Run smoke-test**
  - All subsystems should render without crashing
  - Result should be PASS
  - Run: `python shared/scripts/chief_of_staff.py smoke-test --summary`

- [ ] **3b. Handoff preflight (optional write path)**
  - Run: `python skills/document-preparer/scripts/document_actions.py handoff --file <local> --to <you> --subject "beta" --body "ok" --preflight`
  - Expect `capabilities_ok: true` on Google / Composio Google / Composio Microsoft

## Daily operating loop

- [ ] **4. Run daily summary**
  - Review all 8 panels
  - Note any warnings or items needing attention
  - Run: `python shared/scripts/chief_of_staff.py daily --summary`

- [ ] **5. Review pending actions**
  - Check requested actions in Review Queue
  - Preview, approve, or dismiss as appropriate
  - Run: `python shared/scripts/review_queue.py list --state requested`

- [ ] **6. Review invoice candidates**
  - Check bookkeeper candidates
  - Validate, prepare, or dismiss as appropriate
  - Run: `python skills/bookkeeper/scripts/invoice_ingest.py candidates --summary`

- [ ] **7. Review stale deals**
  - Check pipeline for stale opportunities
  - Follow up or move stage as appropriate
  - Run: `python skills/pipeline-manager/scripts/pipeline.py stale --summary`

## Knowledge health

- [ ] **8. Run memory/wiki lint**
  - Check for stale, low-confidence, uncited, duplicate records
  - Check for broken wiki links, missing frontmatter, stale pages
  - Run: `python shared/scripts/memory.py lint --summary`
  - Run: `python skills/note-taker/scripts/wiki_curator.py lint --summary`

- [ ] **9. Backup memory state**
  - Create timestamped backup before any maintenance
  - Run: `python shared/scripts/memory.py backup`

## Safety verification

- [ ] **10. Confirm no unexpected provider writes**
  - Check audit log for any unexpected Gmail/Drive/Calendar mutations
  - Verify no actions were auto-approved or auto-executed
  - Run: `python shared/scripts/state_tools.py inspect --summary`

## Sign-off

- [ ] All checks pass
- [ ] No unexpected mutations detected
- [ ] Daily summary reviewed
- [ ] Ready for beta operation