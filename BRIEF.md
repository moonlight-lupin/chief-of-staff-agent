# BRIEF — CoS field follow-up #3 (2026-09-11)

## Context

Repo: chief-of-staff (Composio MCP provider). Current HEAD = v0.5.5 (267a3d5). A
field note from the live Battery Road Collective deployment (single operator,
Asia/Singapore) reported three provider bugs and one doc gap, fixed locally
there and described precisely. This brief ports those fixes into this repo.
The orchestrator has already written contract tests at
`tests/test_composio_field_followup3.py` (committed red) — they encode the
expected behavior. Your implementation must make them pass. **Do NOT modify or
delete `tests/test_composio_field_followup3.py`.**

## Tasks

### Task 1 — calendar_list: new slug + normalizer + tz-correct windows

`shared/scripts/providers/composio_mcp_workspace_base.py`:

1. `FAMILY_SLUGS["google"]["calendar_list"]`: `GOOGLECALENDAR_FIND_EVENT` →
   `GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS`.
2. Add a normalizer branch in `_normalize_tool_result` for
   `GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS`:
   - Input: a list of `{event, source_calendar_id, source_calendar_summary}`
     dicts (also tolerate a bare list of raw event dicts, and a dict containing
     an `items` list).
   - Output: flat list of event dicts. Unwrap `item["event"]` and merge in
     `source_calendar_id` + `source_calendar_summary` (both optional, pass
     through when present) so multi-calendar reads stay attributable.
   - Non-list / empty input → `[]`.
3. `calendar_list()` google-family args (line ~1417):
   - `time_min`/`time_max`: when the incoming `start`/`end` are date-only, build
     tz-**localized** ISO windows instead of the current hardcoded
     `T00:00:00Z`/`T23:59:59Z`:
     - tz source: `delivery.timezone` from config (fall back to `"UTC"` when
       absent) via `zoneinfo.ZoneInfo`.
     - start → that date at 00:00:00 in the configured tz; end → that date at
       23:59:59 in the configured tz; format as
       `YYYY-MM-DDTHH:MM:SS±HH:MM` (ISO 8601 with explicit offset).
   - When `start`/`end` already contain a `T` (full ISO datetime), pass
     through unchanged.
   - Plus `max_results_per_calendar: 50`, `response_detail: "full"`,
     `single_events: True`.
   - The microsoft branch of `calendar_list` stays as-is.
4. Update stale contract tests that still assert the old contract:
   - `tests/test_composio_workspace.py` (TestComposioMCPCalendar +
     TestNormalizeToolResult::test_normalize_calendar) and
   - `tests/test_composio_mcp_workspace.py` (TestComposioMCPCalendar).
   Update them to assert the new contract: slug `GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS`, the events
   normalizer (including `source_calendar_id` passthrough), and `+08:00`
   windows (mock a `delivery.timezone: "Asia/Singapore"` config — see
   `tests/test_composio_field_followup3.py::_make_config` for the fixture
   pattern; the two existing test files have their own fixtures, extend them
   minimally by adding `delivery.timezone` to the existing fixture dicts,
   don't restructure).
   Also update `shared/scripts/connect_workspace.py` `cmd_composio_debug_tool`
   `tool_map` for `googlecalendar`/`calendar` entries: slug
   `GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS` with args
   `{"max_results_per_calendar": 2, "response_detail": "full", "single_events": True}`
   (drop the time_min/time_max debug args — the unified endpoint scopes by
   calendar, not by window).

### Task 2 — per-tool account routing

`shared/scripts/providers/composio_mcp_workspace_base.py`:

1. In `__init__` (near the `tool_slugs` handling, ~line 551): read
   `integrations.workspace.account_aliases` (the config example already
   documents this key) into `self._account_aliases: dict[str, str]`.
   Normalize: only keep keys present in the current `self.toolkits` list; strip
   values; drop empty values; keys are toolkit names (lowercase, as listed in
   `toolkits`).
2. Add a helper `_account_for(self, operation) -> str | None`: map operation →
   toolkit (`mail_*` → gmail; `calendar_*` → googlecalendar for google family /
   outlook for microsoft; `files_*` → googledrive for google family /
   one_drive for microsoft; unknown → None). Use the SAME mapping for both
   families where slugs are family-specific: derive the toolkit from the
   operation prefix family-agnostically (mail/calendar/files), then verify the
   resulting toolkit is in `self.toolkits`; return None when absent or not
   enabled. Config lookup is the fallback when the prefix mapping is
   ambiguous — do not guess.
3. In `_execute_composio_tool(...)`: when `_account_for(operation)` returns an
   alias, add `"account": str(alias)` to the per-tool payload dict (sibling of
   `tool_slug` and `arguments`). Absent alias → no `account` key (single-
   account installs unchanged).
4. The microsoft `OUTLOOK_*` and `ONE_DRIVE_*` operations flow through the
   same `_execute_composio tool` path, so they inherit routing automatically.

### Task 3 — connection status: action "list" + case normalization

`shared/scripts/providers/composio_mcp_workspace_base.py`:

1. `refresh_connection_statuses()`: call `self._manage_connections("list", toolkit)`
   instead of `"status"`. Keep the per-toolkit try/except → "unknown" fallback.
2. Replace `a.get("status") == "active"` with the shared helper
   `_status_is_active(...)` (see below).
3. Add module-level helper:
   ```python
   def _status_is_active(status: Any) -> bool:
       return str(status or "").strip().lower() == "active"
   ```
4. `shared/scripts/connect_workspace.py` line ~354 (`cmd_composio_connect`):
   `status == "active"` → `str(status).strip().lower() == "active"` (or import
   the helper — prefer importing the helper for one canonical comparison;
   import path: the script already imports from the providers package).
   Keep the ⏳/✅ icons as-is.

### Task 4 — weekly-review Drive doc paragraph

`skills/weekly-review/SKILL.md`: add one paragraph to the Drive section (the
section describing `workspace_collect.py drive --query`):

> Drive queries in scheduled runs are bounded and metadata-only: never run an
> unbounded empty-query Drive search or request full `allDrives` scope. Scope
> each query to the configured root/operational folder, filter
> `trashed=false` plus a `createdTime`/`modifiedTime` window, request only
> metadata fields, page size 50–100, handle pagination tokens exactly
> (no silent retries that skip tokens), split large date slices into windows,
> and union-dedupe by file ID. This avoids the Composio inline-payload offload
> failure mode (data_preview offload, same family as the 2026-08-29
> data_preview issue) for agents doing inventory reads outside the provider's
> pinned `files_search`.

Match the file's existing heading level and tone.

## Acceptance criteria

1. `python3 -m pytest tests/test_composio_field_followup3.py -q` — all 12 tests pass, test file unmodified (`git diff` on the file empty).
2. `python3 -m pytest tests/test_composio_workspace.py tests/test_composio_mcp_workspace.py -q` — updated stale tests pass.
3. `python3 -m pytest tests/ -q` — full suite green.
4. `python3 -m ruff check shared/scripts/providers/composio_mcp_workspace_base.py shared/scripts/connect_workspace.py tests/` — clean (config in repo).
5. No changes outside: the provider base file, `connect_workspace.py`, the 3 test files (2 stale + 1 new), `skills/weekly-review/SKILL.md`, and — only if needed for docs accuracy — `shared/config/company.yaml.example` (add a comment for account_aliases if the example lacks one; it already has the key at lines 200-203, so likely nothing needed).

## Constraints

- Do NOT modify `tests/test_composio_field_followup3.py`.
- Microsoft-family calendar args and normalizers unchanged.
- `GOOGLECALENDAR_FIND_EVENT` only leaves the **google** family table; do not touch microsoft-family slugs or args.
- No new dependencies (zoneinfo is stdlib).
- Keep the module docstring/FAMILY_SLUGS comment block accurate (it claims FAMILY_SLUGS is the single source of truth).
- Audit messages embed `self._slug_for(...)` slugs (e.g. audit_tool lambdas) — they follow the new slug automatically; don't hand-edit audit strings beyond what the slug change forces.

## Integration boundaries changed

| Boundary | Old shape | New shape | Test | Mock updated? |
|---|---|---|---|---|
| calendar_list google args | time_min/time_max with T00:00:00Z | +08:00-localized ISO | test_composio_field_followup3.py | N/A |
| calendar_list slug | GOOGLECALENDAR_FIND_EVENT | GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS | same file | N/A |
| _normalize_tool_result(calendar_list) | nested event_data.event_data | flat unified events list w/ source_calendar_* | same file | N/A |
| COMPOSIO_MULTI_EXECUTE_TOOL payload | {tool_slug, arguments} | + optional {account} | same file | N/A |
| _manage_connections action | "status" | "list" | same file | N/A |
| account_aliases | unused | read → per-tool account field | same file | N/A |