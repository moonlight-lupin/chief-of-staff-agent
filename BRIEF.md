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
---

# FIX ROUND — Codex review round 1 (commit 83b73af)

Contract tests updated: `tests/test_composio_field_followup3.py` gained
`TestAccountRoutingFamilyGuard` (4 tests) and
`TestConnectionStatusMalformedEnvelope` (2 tests). 4 are RED right now — they
encode the fixes below. Make them pass. Do NOT modify or delete that test file.

## Fix 1 (MAJOR) — account routing must be family-scoped

`_account_for` currently gathers toolkit candidates across BOTH families and
uses cross-family membership to disambiguate. With `family: google` and
`toolkits: [gmail, outlook]`:
- only an outlook alias configured → GMAIL call gets the outlook account;
- both aliases configured → routing returns None and silently drops the pin.

Fix: select the toolkit from `_OPERATION_PREFIX_TOOLKITS[self.family]` ONLY
(single family-specific mapping per prefix), verify it is in `self.toolkits`,
then return `self._account_aliases.get(toolkit)`. No cross-family fallback, no
multi-candidate guessing branch — delete it. Keep returning None when the
family toolkit is not enabled or has no alias. Also update the stale
docstring of `_execute_composio_tool`: `operation` now drives (a) account
routing and (b) the unknown-tool self-diagnosis message — drop the "ONLY" claim.

## Fix 2 (MINOR) — malformed connection-status envelopes read as unknown

`refresh_connection_statuses()`: when the parsed result has no `results`
mapping or no entry for the queried toolkit, classify as "unknown" (not
"pending"). Only a well-formed entry with an accounts list maps
active→connected / no-active→pending.

## Fix 3 (MINOR) — weekly-review Drive example contradicts the bounds paragraph

`skills/weekly-review/SKILL.md` ~line 69: the example
`.venv/bin/python skills/weekly-review/scripts/workspace_collect.py drive --query ""`
uses an empty query right above the bounded-query paragraph. Replace the
example query with a folder-scoped one, e.g.:
`.venv/bin/python skills/weekly-review/scripts/workspace_collect.py drive --query "00_Inbox modified > 2026-09-04"`
(one realistic bounded example; keep surrounding text coherent).

## Acceptance criteria (fix round)

1. `python3 -m pytest tests/test_composio_field_followup3.py -q` — all 20 pass.
2. `python3 -m pytest tests/ -q` — full suite green.
3. `python3 -m ruff check shared/scripts/providers/composio_mcp_workspace_base.py shared/scripts/connect_workspace.py tests/test_composio_field_followup3.py` — clean.
4. `_account_for` no longer contains the cross-family candidate logic.

---

# FIX ROUND 2 — Codex re-review (after 4614835)

## Fix (MAJOR) — SharePoint recycle operations route to the share_point toolkit

`_OPERATION_PREFIX_TOOLKITS["microsoft"]["files"] = "one_drive"` maps
`files_recycle_list` / `files_recycle_restore` to one_drive, but their slugs
are `SHARE_POINT_LIST_RECYCLE_BIN_ITEMS` / `SHARE_POINT_RESTORE_RECYCLE_BIN_ITEM`.

Fix: in `_account_for`, resolve operation → toolkit with a per-operation
override table checked BEFORE the prefix fallback:
`_OPERATION_TOOLKIT_OVERRIDES = {("microsoft", "files_recycle_list"): "share_point", ("microsoft", "files_recycle_restore"): "share_point"}`
(or derive it: if `_slug_for(operation)` starts with `SHARE_POINT_`, use
`share_point` — pick one mechanism, keep it explicit and simple). Preserve the
enabled-toolkit check and the no-alias → None behavior.

## Acceptance criteria (fix round 2)

1. New tests `test_sharepoint_recycle_ops_route_to_sharepoint_alias` and
   `test_sharepoint_recycle_ops_without_sharepoint_alias_get_no_account` in
   `tests/test_composio_field_followup3.py` pass (22 total in the file).
2. `python3 -m pytest tests/ -q` — full suite green.
3. Ruff clean on touched production files.
