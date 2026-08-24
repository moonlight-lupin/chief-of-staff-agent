# BRIEF: v0.5.0 Beta — HTML Briefing + Attachment-to-Drive Hook

## Feature 1: HTML Briefing Format

### Goal

Add `render_html()` to `shared/scripts/briefing_renderer.py` as a fourth output format alongside text, markdown, and JSON.

### Requirements

1. **New function:** `render_html(briefing: dict[str, Any]) -> str` in `briefing_renderer.py`
   - Takes the same `briefing` dict as `render_text` / `render_markdown` / `render_json`
   - Returns a self-contained HTML string (inline CSS, no external dependencies)
   - Must be purely functional — no I/O, no mutations

2. **HTML structure:**
   - Header with operator name, date, and executive summary counts
   - Collapsible sections (`<details>`/`<summary>`) for each source family:
     - Needs attention (risk-colored badges: red/yellow/green)
     - Pending approvals
     - Email organisation
     - Calendar / deadlines (next 48h)
     - Recent events
     - Suggested next actions
     - System health
     - Knowledge maintenance
     - Bookkeeper (overdue invoices, AR/AP)
     - Pipeline
   - Footer with generation timestamp

3. **Links (critical):**
   - **Email items:** If `item.get("link")` is present, render as `<a href="...">Open in Gmail</a>`
   - **Calendar events:** Render TWO links per event:
     - Event link: `item.get("event_link")` → `<a href="...">View event</a>` (opens in Google Calendar)
     - Join button: `item.get("conference_link")` → `<a href="...">Join meeting</a>` (if present)
   - **Files:** If `item.get("link")` is present, render as `<a href="...">Open in Drive</a>`

4. **Event link field:** Add `event_link` to the event schema in `schemas.py`:
   - Optional field on `event` shape: `event_link (str)`
   - In `google_workspace.py` `calendar_list()`: extract `htmlLink` from the Google Calendar API response and set it as `event_link`
   - In `m365_graph.py`: extract `webLink` from the Graph API response

5. **CLI integration:** Add `--html` flag to the `run` subcommand in `daily_briefing.py`:
   - `daily_briefing.py run --html` → outputs HTML to stdout
   - `daily_briefing.py run --html --output /path/to/file.html` → writes to file

6. **Default delivery = send as attachment:**
   - Add `delivery.default_format` to `company.yaml` (default: `text`)
   - When the briefing cron runs and `default_format` is `html`, render as HTML, save to a temp file, and deliver as a file attachment (not inline text)
   - The Hermes cron delivery mechanism handles file attachments via `MEDIA:` prefix

7. **Styling:**
   - Clean, professional, responsive
   - Dark-mode compatible (prefers-color-scheme)
   - Risk badges: red (#dc2626), yellow (#f59e0b), green (#16a34a)
   - Monospace font for timestamps/IDs
   - Max-width 800px, centered
   - Tables for invoices and pipeline
   - No JavaScript (pure HTML+CSS, works in Telegram's in-app browser)

### Files to modify
- `shared/scripts/briefing_renderer.py` — add `render_html()`
- `shared/scripts/schemas.py` — add `event_link` to event shape
- `shared/scripts/providers/google_workspace.py` — extract `htmlLink` in `calendar_list()`
- `shared/scripts/providers/m365_graph.py` — extract `webLink` in calendar method
- `skills/daily-briefing/scripts/daily_briefing.py` — add `--html` flag, `--output` flag
- `shared/config/company.yaml.example` — add `delivery.default_format: text`

---

## Feature 2: Attachment-to-Drive Hook

### Goal

Add a new hook in `hooks.py` that detects when the user sends or shares a file attachment in the conversation, classifies it, checks if it should be filed to Drive, and suggests filing via the drive-filer skill.

### Requirements

1. **New hook:** `attachment_drive_suggestion` registered as `post_llm_call`
   - Fires after each LLM response
   - Scans the current conversation context for file attachments (files the user sent)
   - Does NOT fire on every turn — only when attachments are detected

2. **Detection:**
   - Check `context.get("attachments")` or `context.get("files")` for file metadata
   - Also check `context.get("message")` for file references (MEDIA: paths)
   - If no attachments found, return None (silent)

3. **Classification:**
   - Classify by file extension and name pattern:
     - `.pdf` → document (invoice, contract, receipt, report)
     - `.docx`/`.doc` → document
     - `.xlsx`/`.xls` → spreadsheet (invoice, financial)
     - `.png`/`.jpg`/`.jpeg`/`.webp` → image (receipt, screenshot)
     - `.eml` → email export
   - Match against `drive-map.yaml` patterns for filing target

4. **Drive existence check:**
   - Search Drive by filename to check if the file already exists
   - Use `WorkspaceClient.files_search()` (read-only, safe)
   - If already exists, do not suggest filing

5. **Suggestion (not automatic):**
   - Return a suggestion string: "I found an attachment: {filename}. This looks like a {category}. Would you like me to file it to Drive in the {folder} folder?"
   - The user must confirm before any upload happens
   - Never auto-upload — always ask first

6. **Safety:**
   - Read-only until user confirms
   - No mutations in the hook itself
   - Filing only happens when the user responds with confirmation and the drive-filer skill is invoked
   - Log the suggestion in the runtime log

7. **Configuration:**
   - Add `hooks.attachment_suggestions: true` to `company.yaml` (default: true)
   - Can be disabled by setting to false

### Files to modify
- `hooks.py` — add `attachment_drive_suggestion()` function and register it
- `shared/config/company.yaml.example` — add `hooks.attachment_suggestions: true`

### Files NOT to modify
- `skills/drive-filer/scripts/drive_file.py` — the hook suggests, the skill acts
- `skills/drive-filer/scripts/drive_map.py` — read-only import for classification

---

## Constraints

- Stdlib only (no new dependencies)
- All existing tests must pass
- Ruff must be clean
- Do NOT modify `tests/test_state_db.py`
- Add new tests in `tests/test_html_briefing.py` and `tests/test_attachment_hook.py`
- Run: `python -m pytest -q` (all must pass)
- Run: `ruff check shared/ skills/ hooks.py __init__.py` (must be clean)