# Fix Brief: v0.5.0 Beta Review Fixes

Fix all 12 findings from the Codex + Opus cross-model review.

## BLOCKING (4)

### B1: --html and --output flags do nothing
File: `skills/daily-briefing/scripts/daily_briefing.py:1035-1056`
Problem: `cmd_run()` only branches on `args.json` and `args.markdown`. Never reads `args.html` or `args.output_path`.
Fix:
- Add `elif args.html: rendered = render(briefing, "html")` branch
- After rendering, check `args.output_path`: if set, write to file (create parent dirs); else print to stdout
- Make format flags mutually exclusive (argparse `add_mutually_exclusive_group`)
- Read `delivery.default_format` from company.yaml as fallback when no explicit format flag is set

### B2: URL scheme allowlist missing in _link()
File: `shared/scripts/briefing_renderer.py:430-434`
Problem: `_link()` HTML-escapes href but doesn't validate scheme. `javascript:` URIs pass through.
Fix: In `_link()`, reject hrefs that don't start with `http://` or `https://` (case-insensitive). Return empty string for invalid schemes. Use `re.match(r'^https?://', href, re.I)`.

### B3: _risk_badge() doesn't escape risk label — XSS
File: `shared/scripts/briefing_renderer.py:419-422`
Problem: `label = risk.title()` inserted into HTML without escaping. A crafted risk string like `<img src=x onerror=alert(1)>` produces executable markup.
Fix: Escape the label with `_esc()`. Better: map known risks to fixed labels, escape unknown ones. `_risk_badge` should return `f'<span class="badge {cls}">{_esc(label)}</span>'`.

### B4: Calendar data-shape mismatch — dead links in production
File: `shared/scripts/briefing_sources.py` (_calendar_summary) vs `briefing_renderer.py:464-486` (_html_calendar)
Problem: `_calendar_summary()` emits `{when, summary, event_id, location}` but `_html_calendar()` reads `start`, `end`, `event_link`, `conference_link`. In production, calendar events show blank time ranges and no links.
Fix: Update `_calendar_summary()` to also include `start`, `end`, `event_link`, `conference_link` from the event record. Add an integration test that runs an envelope through `_build_structured_briefing` and asserts HTML output contains the event link.

## MAJOR (4)

### M1: delivery.default_format is dead config
File: `skills/daily-briefing/scripts/daily_briefing.py`, `shared/config/company.yaml.example`
Problem: `default_format` is in company.yaml.example but never read by any code.
Fix: In `cmd_run()`, read `delivery.default_format` from config as the fallback format when no explicit CLI flag is set. Implement the "html sends as file attachment" behavior.

### M2: Legacy YAML remains active second source of truth
File: `shared/scripts/state_db.py:1399-1621`
Problem: Migration records YAML sources but leaves files in place. `load_pipeline_store(strict=False)` reads stale YAML. SQLite and YAML can diverge.
Fix: After successful migration, rename legacy YAML files to `.migrated` (the contract). Make all read paths use SQLite. If human-readable exports are needed, generate them as explicit snapshots, never read as authoritative state.

### M3: pipeline_actions/bookkeeper_actions bypass CAS
File: `shared/scripts/pipeline_actions.py:183-209`, `shared/scripts/bookkeeper_actions.py:487-535`
Problem: These paths call `load_store()`, modify the whole document, then `save_store_atomic()` (full-table replace). This bypasses `mutate_kv()` CAS. Concurrent updates can be lost.
Fix: Move the complete lookup/validation/mutation into a `mutate_kv()` callback for every write path. Do not retain no-op `with_store_lock()`.

### M4: _html_pending_approvals mislabels by risk level
File: `shared/scripts/briefing_renderer.py:453-461`
Problem: `_html_pending_approvals` iterates `pa.items()` assuming keys are action types. But the real producer (`_build_structured_briefing` lines 964-973) keys by risk level (`high`/`medium`/`low`). In production, renders `<strong>high</strong>: ...` instead of `<strong>mail.send</strong>: ...`.
Fix: Rewrite `_html_pending_approvals` to iterate risk levels like `render_text`/`render_markdown` do. Use `a.get("type")` for the label and `_risk_badge(risk_level)` for the badge.

## MINOR (2)

### m1: Lease renewal accepts None token for tokenized leases
File: `shared/scripts/state_db.py:1216-1231`
Problem: `renew_delivery()` rejects token mismatch only when caller supplies a truthy token. Passing `None` renews a row even when it has a stored lease token.
Fix: When a stored token exists, require a non-null exact match. If tokenless legacy rows must be supported, limit tokenless renewal to rows whose stored token is also null.

### m2: _esc() collapses falsy-but-valid values to empty
File: `shared/scripts/briefing_renderer.py:425-427`
Problem: `if text` treats `0`, `0.0`, `False` as absent. `_html_table` renders `amount: 0` as blank cell.
Fix: `return _html.escape(str(text)) if text not in (None, "") else ""`

## NIT (2)

### n1: Stale docstrings reference state_store/pipeline.yaml
File: `shared/scripts/pipeline_actions.py:11,183-200` and other migrated callers
Fix: Update docstrings to reference state_db / SQLite.

### n2: Redundant import re inside attachment_drive_suggestion
File: `hooks.py:696`
Fix: Remove the local `import re` — `re` is already imported at module scope (line 12).

## Constraints
- Do NOT modify tests/test_state_db.py, tests/test_html_briefing.py, tests/test_attachment_hook.py
- Add new tests for the fixes (XSS tests, CLI --html test, integration test for calendar links, pending approvals shape test)
- Run: `python -m pytest -q` (all must pass)
- Run: `ruff check shared/ skills/ hooks.py __init__.py` (must be clean)