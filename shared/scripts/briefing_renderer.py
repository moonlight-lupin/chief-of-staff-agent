#!/usr/bin/env python3
"""Briefing renderer — structured output for daily briefing.

Renders briefing data in three formats:
- text (CLI, human-readable)
- markdown (email/notification)
- json (machine-readable)

The renderer is purely functional — it takes a briefing dict and returns
a string. It never mutates, calls providers, or performs I/O.
"""
from __future__ import annotations

import html as _html
import json
import re
from typing import Any


def _risk_icon(risk: str) -> str:
    return {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(risk, "⚪")


def render_text(briefing: dict[str, Any]) -> str:
    """Render briefing as human-readable text for CLI."""
    lines: list[str] = []
    summary = briefing.get("summary", {})
    sections = briefing.get("sections", {})
    safety = briefing.get("safety", {})

    # Header
    operator = briefing.get("operator", "Operator")
    lines.append(f"Good morning, {operator}.\n")

    # Executive summary
    needs = summary.get("needs_attention", 0)
    pending = summary.get("pending_approvals", 0)
    suggestions = summary.get("suggestions", 0)
    classified = summary.get("classified_emails", 0)
    warnings = summary.get("system_warnings", 0)

    lines.append("Today needs attention:")
    if needs:
        lines.append(f"- {needs} item(s) need attention")
    if pending:
        lines.append(f"- {pending} pending approval(s)")
    if suggestions:
        lines.append(f"- {suggestions} suggested action(s)")
    if classified:
        lines.append(f"- {classified} classified email(s)")
    if warnings:
        lines.append(f"- {warnings} system warning(s)")
    if not any([needs, pending, suggestions, classified, warnings]):
        lines.append("- Nothing urgent today. All clear.")
    lines.append("")

    # Needs attention
    na = sections.get("needs_attention", [])
    if na:
        lines.append("Needs attention:")
        for item in na:
            icon = _risk_icon(item.get("risk", "low"))
            why = item.get("why", "")
            lines.append(f"  {icon} {item.get('title', '?')}")
            if why:
                lines.append(f"     Why: {why}")
        lines.append("")

    # Pending approvals grouped by risk
    pa = sections.get("pending_approvals", {})
    if pa and (pa.get("high") or pa.get("medium") or pa.get("low")):
        lines.append("Pending approvals:")
        for risk_level in ("high", "medium", "low"):
            actions = pa.get(risk_level, [])
            if not actions:
                continue
            icon = _risk_icon(risk_level)
            label = {"high": "High risk", "medium": "Medium risk", "low": "Low risk"}[risk_level]
            lines.append(f"  {icon} {label} ({len(actions)}):")
            for a in actions:
                lines.append(f"    [{a.get('action_id', '?')}] {a.get('type', '?')} — {a.get('summary', '')}")
                lines.append(f"      State: {a.get('state', '?')} | Created: {a.get('created_at', '?')}")
                lines.append(f"      Preview: python shared/scripts/review_queue.py preview --action-id {a.get('action_id', '?')}")
                lines.append(f"      Approve: python shared/scripts/review_queue.py approve --action-id {a.get('action_id', '?')} --approver MH --reason \"Reviewed\"")
                lines.append(f"      Execute: python shared/scripts/review_queue.py execute --action-id {a.get('action_id', '?')}")
        lines.append("")

    # Email organisation
    eo = sections.get("email_organisation", {})
    if eo:
        lines.append("Email organisation:")
        lines.append(f"  Classified: {eo.get('classified', 0)}")
        lines.append(f"  Unmapped: {eo.get('unmapped', 0)}")
        lines.append(f"  Archive candidates: {eo.get('archive_candidates', 0)}")
        lines.append(f"  Label suggestions: {eo.get('label_suggestions', 0)}")
        lines.append(f"  Pending org actions: {eo.get('pending_actions', 0)}")
        lines.append("  (No Gmail changes were made by this briefing.)")
        lines.append("")

    # Calendar / deadlines
    cal = sections.get("calendar_deadlines", [])
    if cal:
        lines.append("Calendar / deadlines (next 48h):")
        for item in cal[:8]:
            lines.append(f"  - {item.get('when', '?')}: {item.get('summary', '?')}")
        lines.append("")

    # Recent events
    re = sections.get("recent_events", [])
    if re:
        lines.append("Recent activity (last 24h):")
        type_counts: dict[str, int] = {}
        for e in re:
            et = e.get("event_type", "unknown")
            type_counts[et] = type_counts.get(et, 0) + 1
        for et, count in sorted(type_counts.items()):
            lines.append(f"  - {count} {et}")
        lines.append("")

    # Suggested next actions
    sna = sections.get("suggested_next_actions", [])
    if sna:
        lines.append("Suggested next actions:")
        for item in sna[:10]:
            icon = _risk_icon(item.get("risk", "low"))
            lines.append(f"  {icon} {item.get('title', '?')}")
            why = item.get("why", "")
            if why:
                lines.append(f"     Why: {why}")
        lines.append("")

    # System health
    sh = sections.get("system_health", {})
    if sh:
        lines.append("System health:")
        lines.append(f"  State files: {sh.get('state_files', '?')}")
        ps = sh.get("pending_summary", {})
        if ps:
            parts = [f"{v} {k}" for k, v in ps.items() if v]
            lines.append(f"  Pending: {', '.join(parts) if parts else 'empty'}")
        if sh.get("audit_dir") is not None:
            lines.append(f"  Audit dir: {'OK' if sh.get('audit_dir') else 'missing'}")
        if sh.get("runs_dir") is not None:
            lines.append(f"  Runs dir: {'OK' if sh.get('runs_dir') else 'missing'}")
        lines.append("")

    # Knowledge maintenance
    km = sections.get("knowledge_maintenance", {})
    if km and (km.get("wiki_pages_updated") or km.get("wiki_pages_created")
               or km.get("memory_records_created") or km.get("memory_records_updated")
               or km.get("duplicates_flagged") or km.get("conflicts_flagged")
               or km.get("total_records")):
        lines.append("Knowledge maintenance:")
        if km.get("wiki_pages_updated"):
            lines.append(f"  Updated {km['wiki_pages_updated']} wiki page(s).")
        if km.get("wiki_pages_created"):
            lines.append(f"  Created {km['wiki_pages_created']} new wiki draft page(s).")
        if km.get("memory_records_created"):
            lines.append(f"  Created {km['memory_records_created']} memory record(s).")
        if km.get("memory_records_updated"):
            lines.append(f"  Updated {km['memory_records_updated']} memory record(s).")
        if km.get("observations_added"):
            lines.append(f"  Added {km['observations_added']} source-backed observation(s).")
        if km.get("backlinks_added"):
            lines.append(f"  Added {km['backlinks_added']} backlink(s).")
        if km.get("duplicates_flagged"):
            lines.append(f"  Flagged {km['duplicates_flagged']} possible duplicate(s) for review.")
        if km.get("conflicts_flagged"):
            lines.append(f"  Flagged {km['conflicts_flagged']} conflict(s) for review.")
        if km.get("open_questions_added"):
            lines.append(f"  Added {km['open_questions_added']} open question(s).")
        if km.get("total_records"):
            lines.append(f"  Memory records: {km['total_records']} total.")
        # v0.2.7: lint warnings
        lint_issues = []
        if km.get("stale_records"):
            lint_issues.append(f"{km['stale_records']} stale")
        if km.get("low_confidence_records"):
            lint_issues.append(f"{km['low_confidence_records']} low-confidence")
        if km.get("contested_records"):
            lint_issues.append(f"{km['contested_records']} contested")
        if km.get("uncited_records"):
            lint_issues.append(f"{km['uncited_records']} uncited")
        if km.get("duplicate_records"):
            lint_issues.append(f"{km['duplicate_records']} duplicate")
        if km.get("wiki_broken_links"):
            lint_issues.append(f"{km['wiki_broken_links']} broken wiki links")
        if km.get("wiki_missing_frontmatter"):
            lint_issues.append(f"{km['wiki_missing_frontmatter']} missing frontmatter")
        if km.get("wiki_duplicate_pages"):
            lint_issues.append(f"{km['wiki_duplicate_pages']} duplicate wiki pages")
        if km.get("wiki_stale_pages"):
            lint_issues.append(f"{km['wiki_stale_pages']} stale wiki pages")
        if lint_issues:
            lines.append(f"  Lint warnings: {', '.join(lint_issues)}")
            lines.append("  Run: python shared/scripts/memory.py lint --summary")
            lines.append("  Run: python skills/note-taker/scripts/wiki_curator.py lint --summary")
        lines.append("")

    # Bookkeeper
    bk = sections.get("bookkeeper", {})
    if bk and (bk.get("candidates_found") or bk.get("pending_record_actions")
               or bk.get("duplicate_warnings") or bk.get("candidates_needs_review")):
        lines.append("Bookkeeper:")
        if bk.get("candidates_found"):
            lines.append(f"  {bk['candidates_found']} invoice candidate(s) found.")
        if bk.get("candidates_needs_review"):
            lines.append(f"  {bk['candidates_needs_review']} candidate(s) need review.")
        if bk.get("duplicate_warnings"):
            lines.append(f"  {bk['duplicate_warnings']} possible duplicate(s).")
        if bk.get("pending_record_actions"):
            lines.append(f"  {bk['pending_record_actions']} pending invoice-record action(s).")
        lines.append("  (No invoices written by this briefing.)")
        lines.append("")

    # Pipeline / CRM
    pl = sections.get("pipeline", {})
    if pl and (pl.get("active_deals") or pl.get("stale_deals")
               or pl.get("pending_crm_actions") or pl.get("recently_moved")
               or pl.get("contract_signed_no_invoice") or pl.get("invoiced_not_paid")):
        lines.append("Pipeline:")
        if pl.get("active_deals"):
            lines.append(f"  Active deals: {pl['active_deals']}")
        if pl.get("stale_deals"):
            lines.append(f"  Stale deals: {pl['stale_deals']}")
        if pl.get("oldest_stale_id"):
            lines.append(f"  Oldest stale: {pl['oldest_stale_id']} — {pl.get('oldest_stale_stage', '?')}, {pl.get('oldest_stale_days', 0)} days inactive")
        if pl.get("recently_moved"):
            lines.append(f"  Recently moved: {pl['recently_moved']}")
        if pl.get("pending_crm_actions"):
            lines.append(f"  Pending CRM actions: {pl['pending_crm_actions']}")
        if pl.get("contract_signed_no_invoice"):
            lines.append(f"  Contract Signed without invoice: {pl['contract_signed_no_invoice']}")
        if pl.get("invoiced_not_paid"):
            lines.append(f"  Invoiced but not paid: {pl['invoiced_not_paid']}")
        lines.append("  (No pipeline mutations by this briefing.)")
        lines.append("")

    # Safety footer
    if safety:
        lines.append("---")
        if not safety.get("external_mutations_performed", True):
            lines.append("No external mutations performed.")
        if not safety.get("approvals_performed", True):
            lines.append("No approvals performed.")
        if not safety.get("executions_performed", True):
            lines.append("No executions performed.")

    return "\n".join(lines)


def render_markdown(briefing: dict[str, Any]) -> str:
    """Render briefing as markdown for email/notification."""
    lines: list[str] = []
    summary = briefing.get("summary", {})
    sections = briefing.get("sections", {})
    operator = briefing.get("operator", "Operator")

    lines.append(f"# Daily Briefing — {operator}\n")

    # Executive summary
    lines.append("## Executive Summary\n")
    lines.append(f"- Needs attention: {summary.get('needs_attention', 0)}")
    lines.append(f"- Pending approvals: {summary.get('pending_approvals', 0)}")
    lines.append(f"- Suggestions: {summary.get('suggestions', 0)}")
    lines.append(f"- Classified emails: {summary.get('classified_emails', 0)}")
    lines.append(f"- System warnings: {summary.get('system_warnings', 0)}\n")

    # Needs attention
    na = sections.get("needs_attention", [])
    if na:
        lines.append("## Needs Attention\n")
        for item in na:
            lines.append(f"- **{item.get('title', '?')}** ({item.get('risk', 'low')})")
            if item.get("why"):
                lines.append(f"  - _Why: {item['why']}_")
        lines.append("")

    # Pending approvals
    pa = sections.get("pending_approvals", {})
    for risk_level in ("high", "medium", "low"):
        actions = pa.get(risk_level, [])
        if not actions:
            continue
        label = {"high": "High Risk", "medium": "Medium Risk", "low": "Low Risk"}[risk_level]
        lines.append(f"## Pending Approvals — {label}\n")
        for a in actions:
            lines.append(f"- `{a.get('action_id', '?')}` {a.get('type', '?')} — {a.get('summary', '')}")
            lines.append(f"  - State: {a.get('state', '?')}")
            lines.append(f"  - Preview: `python shared/scripts/review_queue.py preview --action-id {a.get('action_id', '?')}`")
            lines.append(f"  - Approve: `python shared/scripts/review_queue.py approve --action-id {a.get('action_id', '?')} --approver MH --reason \"Reviewed\"`")
            lines.append(f"  - Execute: `python shared/scripts/review_queue.py execute --action-id {a.get('action_id', '?')}`")
        lines.append("")

    # Email organisation
    eo = sections.get("email_organisation", {})
    if eo:
        lines.append("## Email Organisation\n")
        lines.append(f"- Classified: {eo.get('classified', 0)}")
        lines.append(f"- Unmapped: {eo.get('unmapped', 0)}")
        lines.append(f"- Archive candidates: {eo.get('archive_candidates', 0)}")
        lines.append(f"- Label suggestions: {eo.get('label_suggestions', 0)}")
        lines.append(f"- Pending actions: {eo.get('pending_actions', 0)}")
        lines.append("\n_No Gmail changes were made by this briefing._\n")

    # Calendar
    cal = sections.get("calendar_deadlines", [])
    if cal:
        lines.append("## Calendar / Deadlines\n")
        for item in cal[:8]:
            lines.append(f"- {item.get('when', '?')}: {item.get('summary', '?')}")
        lines.append("")

    # Recent events
    re = sections.get("recent_events", [])
    if re:
        lines.append("## Recent Activity\n")
        type_counts: dict[str, int] = {}
        for e in re:
            et = e.get("event_type", "unknown")
            type_counts[et] = type_counts.get(et, 0) + 1
        for et, count in sorted(type_counts.items()):
            lines.append(f"- {count} {et}")
        lines.append("")

    # Suggested actions
    sna = sections.get("suggested_next_actions", [])
    if sna:
        lines.append("## Suggested Next Actions\n")
        for item in sna[:10]:
            lines.append(f"- **{item.get('title', '?')}** ({item.get('risk', 'low')})")
            if item.get("why"):
                lines.append(f"  - _Why: {item['why']}_")
        lines.append("")

    # System health
    sh = sections.get("system_health", {})
    if sh:
        lines.append("## System Health\n")
        lines.append(f"- State files: {sh.get('state_files', '?')}")
        ps = sh.get("pending_summary", {})
        if ps:
            parts = [f"{v} {k}" for k, v in ps.items() if v]
            lines.append(f"- Pending: {', '.join(parts) if parts else 'empty'}")
        lines.append("")

    # Knowledge maintenance
    km = sections.get("knowledge_maintenance", {})
    if km and (km.get("wiki_pages_updated") or km.get("wiki_pages_created")
               or km.get("memory_records_created") or km.get("memory_records_updated")
               or km.get("duplicates_flagged") or km.get("conflicts_flagged")
               or km.get("total_records")):
        lines.append("## Knowledge Maintenance\n")
        if km.get("wiki_pages_updated"):
            lines.append(f"- Updated {km['wiki_pages_updated']} wiki page(s)")
        if km.get("wiki_pages_created"):
            lines.append(f"- Created {km['wiki_pages_created']} new wiki draft page(s)")
        if km.get("memory_records_created"):
            lines.append(f"- Created {km['memory_records_created']} memory record(s)")
        if km.get("memory_records_updated"):
            lines.append(f"- Updated {km['memory_records_updated']} memory record(s)")
        if km.get("observations_added"):
            lines.append(f"- Added {km['observations_added']} source-backed observation(s)")
        if km.get("backlinks_added"):
            lines.append(f"- Added {km['backlinks_added']} backlink(s)")
        if km.get("duplicates_flagged"):
            lines.append(f"- Flagged {km['duplicates_flagged']} possible duplicate(s)")
        if km.get("conflicts_flagged"):
            lines.append(f"- Flagged {km['conflicts_flagged']} conflict(s)")
        if km.get("open_questions_added"):
            lines.append(f"- Added {km['open_questions_added']} open question(s)")
        if km.get("total_records"):
            lines.append(f"- Memory records: {km['total_records']} total")
        lines.append("")

    lines.append("---")
    lines.append("_No external mutations, approvals, or executions performed._")

    return "\n".join(lines)


def render_json(briefing: dict[str, Any]) -> str:
    """Render briefing as JSON string."""
    return json.dumps(briefing, indent=2, ensure_ascii=False, default=str)


# ── HTML renderer ─────────────────────────────────────────────────────────

_HTML_STYLE = """\
:root{--red:#dc2626;--yellow:#f59e0b;--green:#16a34a;--bg:#f8fafc;--card:#fff;--border:#e2e8f0;--text:#1e293b;--muted:#64748b}
@media(prefers-color-scheme:dark){:root{--bg:#0f172a;--card:#1e293b;--border:#334155;--text:#e2e8f0;--muted:#94a3b8}}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);line-height:1.6;padding:16px}
.container{max-width:800px;margin:0 auto}
h1{font-size:1.4rem;margin-bottom:4px}
.meta{color:var(--muted);font-size:.85rem;margin-bottom:16px}
.summary{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.badge{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:20px;font-size:.8rem;font-weight:600}
.badge-high{background:var(--red);color:#fff}
.badge-medium{background:var(--yellow);color:#fff}
.badge-low{background:var(--green);color:#fff}
.badge-neutral{background:var(--border);color:var(--text)}
details{background:var(--card);border:1px solid var(--border);border-radius:8px;margin-bottom:8px;overflow:hidden}
summary{padding:10px 14px;cursor:pointer;font-weight:600;font-size:.95rem;user-select:none}
summary:hover{background:var(--border)}
.body{padding:10px 14px}
.item{padding:6px 0;border-bottom:1px solid var(--border)}
.item:last-child{border-bottom:none}
a{color:#2563eb;text-decoration:none}
a:hover{text-decoration:underline}
.btn-join{display:inline-block;padding:3px 10px;border-radius:6px;background:#2563eb;color:#fff!important;font-size:.8rem;margin-left:6px}
.btn-event{display:inline-block;padding:3px 10px;border-radius:6px;background:var(--border);color:var(--text);font-size:.8rem;margin-left:6px}
table{width:100%;border-collapse:collapse;font-size:.85rem}
th,td{padding:6px 8px;text-align:left;border-bottom:1px solid var(--border)}
th{color:var(--muted);font-weight:600}
.ts{font-family:monospace;font-size:.8rem;color:var(--muted)}
.footer{margin-top:16px;padding-top:12px;border-top:1px solid var(--border);color:var(--muted);font-size:.8rem;text-align:center}
"""


_KNOWN_RISK_LABELS = {"high": "High", "medium": "Medium", "low": "Low"}


def _risk_badge(risk: str) -> str:
    key = str(risk or "").strip().lower()
    cls = {"high": "badge-high", "medium": "badge-medium", "low": "badge-low"}.get(key, "badge-neutral")
    if key in _KNOWN_RISK_LABELS:
        label = _KNOWN_RISK_LABELS[key]
    elif risk:
        label = str(risk)
    else:
        label = "Info"
    return f'<span class="badge {cls}">{_esc(label)}</span>'


def _esc(text: Any) -> str:
    return _html.escape(str(text)) if text not in (None, "") else ""


def _link(href: str, label: str, cls: str = "") -> str:
    if not href:
        return ""
    url = str(href).strip()
    if not re.match(r"^https?://", url, re.I):
        return ""
    cls_attr = f' class="{cls}"' if cls else ""
    return f'<a href="{_esc(url)}"{cls_attr}>{_esc(label)}</a>'


def _html_needs_attention(items: list) -> str:
    if not items:
        return '<p class="muted">Nothing needs attention. All clear.</p>'
    parts = []
    for item in items:
        risk = item.get("risk", "low")
        title = item.get("title", item.get("summary", "Item"))
        detail = item.get("detail", "")
        link = item.get("link")
        badge = _risk_badge(risk)
        link_html = f' {_link(link, "Open", "btn-event")}' if link else ""
        parts.append(f'<div class="item">{badge} <strong>{_esc(title)}</strong>'
                     f'{" — " + _esc(detail) if detail else ""}{link_html}</div>')
    return "".join(parts)


def _html_pending_approvals(pa: dict) -> str:
    if not pa:
        return '<p class="muted">No pending approvals.</p>'
    parts = []
    for risk_level in ("high", "medium", "low"):
        actions = pa.get(risk_level, [])
        if not actions:
            continue
        for a in actions:
            if not isinstance(a, dict):
                continue
            action_type = a.get("type", a.get("action_type", "?"))
            summary = a.get("summary", a.get("id", a.get("action_id", "")))
            parts.append(
                f'<div class="item">{_risk_badge(risk_level)} <strong>{_esc(action_type)}</strong>: '
                f'{_esc(summary)}</div>'
            )
    return "".join(parts) if parts else '<p class="muted">No pending approvals.</p>'


def _html_calendar(events: list) -> str:
    if not events:
        return '<p class="muted">No events in the next 48 hours.</p>'
    parts = []
    for ev in events:
        title = ev.get("title", ev.get("summary", "Event"))
        start = ev.get("start", "")
        end = ev.get("end", "")
        conf = ev.get("conference_link")
        ev_link = ev.get("event_link")
        loc = ev.get("location", "")
        links = ""
        if ev_link:
            links += f' {_link(ev_link, "View event", "btn-event")}'
        if conf:
            links += f' {_link(conf, "Join", "btn-join")}'
        parts.append(
            f'<div class="item"><strong>{_esc(title)}</strong>'
            f'<br><span class="ts">{_esc(start)} — {_esc(end)}</span>'
            f'{f"<br>📍 {_esc(loc)}" if loc else ""}'
            f'{links}</div>'
        )
    return "".join(parts)


def _html_table(items: list, columns: list[tuple[str, str]]) -> str:
    if not items:
        return '<p class="muted">None.</p>'
    header = "".join(f"<th>{_esc(col)}</th>" for _, col in columns)
    rows = []
    for item in items:
        cells = "".join(f"<td>{_esc(item.get(key, ''))}</td>" for key, _ in columns)
        rows.append(f"<tr>{cells}</tr>")
    return f'<table><thead><tr>{header}</tr></thead><tbody>{"".join(rows)}</tbody></table>'


def _html_suggestions(items: list) -> str:
    if not items:
        return '<p class="muted">No suggestions.</p>'
    parts = []
    for item in items:
        parts.append(f'<div class="item">{_esc(item.get("type", ""))}: '
                     f'{_esc(item.get("summary", ""))}</div>')
    return "".join(parts)


def _html_section(title: str, content: str, open_by_default: bool = False) -> str:
    attr = " open" if open_by_default else ""
    return f'<details{attr}><summary>{_esc(title)}</summary><div class="body">{content}</div></details>'


def render_html(briefing: dict[str, Any]) -> str:
    """Render briefing as a self-contained HTML document.

    Inline CSS only — no external stylesheets or JavaScript.
    Works in Telegram's in-app browser and any modern browser.
    """
    summary = briefing.get("summary", {})
    sections = briefing.get("sections", {})
    operator = briefing.get("operator", "Operator")
    generated = briefing.get("generated_at", "")

    # Summary badges
    badges = []
    na_count = summary.get("needs_attention", 0)
    pa_count = summary.get("pending_approvals", 0)
    sg_count = summary.get("suggestions", 0)
    ce_count = summary.get("classified_emails", 0)
    sw_count = summary.get("system_warnings", 0)
    if na_count:
        badges.append(f'<span class="badge badge-high">{na_count} need attention</span>')
    if pa_count:
        badges.append(f'<span class="badge badge-medium">{pa_count} pending approvals</span>')
    if sg_count:
        badges.append(f'<span class="badge badge-neutral">{sg_count} suggestions</span>')
    if ce_count:
        badges.append(f'<span class="badge badge-low">{ce_count} classified emails</span>')
    if sw_count:
        badges.append(f'<span class="badge badge-high">{sw_count} warnings</span>')
    if not badges:
        badges.append('<span class="badge badge-low">All clear</span>')

    # Build sections
    sections_html = []
    na = sections.get("needs_attention", [])
    if na or na_count:
        sections_html.append(_html_section("Needs Attention", _html_needs_attention(na), open_by_default=True))

    pa = sections.get("pending_approvals", {})
    if pa or pa_count:
        sections_html.append(_html_section("Pending Approvals", _html_pending_approvals(pa)))

    cal = sections.get("calendar_deadlines", [])
    if cal:
        sections_html.append(_html_section("Calendar / Deadlines (48h)", _html_calendar(cal), open_by_default=True))

    sna = sections.get("suggested_next_actions", [])
    if sna or sg_count:
        sections_html.append(_html_section("Suggested Next Actions", _html_suggestions(sna)))

    eo = sections.get("email_organisation", {})
    if eo:
        eo_html = (f'<p>Classified: {eo.get("classified", 0)}'
                   f'<br>Label suggestions: {eo.get("label_suggestions", 0)}</p>')
        sections_html.append(_html_section("Email Organisation", eo_html))

    bk = sections.get("bookkeeper", {})
    if bk:
        bk_parts = []
        overdue = bk.get("overdue_ar", [])
        if overdue:
            bk_parts.append(_html_table(overdue, [("id", "Invoice"), ("client", "Client"),
                                                  ("amount", "Amount"), ("currency", "Currency")]))
        totals = bk.get("outstanding_ar_total", {})
        if totals:
            total_str = ", ".join(f"{cur} {amt}" for cur, amt in totals.items())
            bk_parts.append(f'<p><strong>Outstanding AR:</strong> {_esc(total_str)}</p>')
        sections_html.append(_html_section("Bookkeeper", "".join(bk_parts) or '<p class="muted">No overdue invoices.</p>'))

    pl = sections.get("pipeline", {})
    if pl:
        stale = pl.get("stale_deals", [])
        if stale:
            sections_html.append(_html_section("Pipeline", _html_table(
                stale, [("client_name", "Client"), ("stage", "Stage"), ("stale_days", "Days idle")])))
        else:
            sections_html.append(_html_section("Pipeline", '<p class="muted">No stale deals.</p>'))

    sh = sections.get("system_health", {})
    if sh and sh.get("warnings"):
        sections_html.append(_html_section("System Health",
                                           f'<p>{sh["warnings"]} warning(s)</p>'))

    km = sections.get("knowledge_maintenance", {})
    if km and (km.get("wiki_broken_links") or km.get("total_records")):
        km_parts = []
        if km.get("wiki_broken_links"):
            km_parts.append(f'<p>Broken wiki links: {km["wiki_broken_links"]}</p>')
        if km.get("total_records"):
            km_parts.append(f'<p>Memory records: {km["total_records"]} total</p>')
        sections_html.append(_html_section("Knowledge Maintenance", "".join(km_parts)))

    return (
        f'<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        f'<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f'<title>Briefing — {_esc(operator)}</title>\n'
        f'<style>{_HTML_STYLE}</style>\n'
        f'</head>\n<body>\n<div class="container">\n'
        f'<h1>Briefing — {_esc(operator)}</h1>\n'
        f'<p class="meta">{_esc(generated)}</p>\n'
        f'<div class="summary">{"".join(badges)}</div>\n'
        f'{"".join(sections_html)}\n'
        f'<div class="footer">Generated by Chief of Staff · No mutations performed</div>\n'
        f'</div>\n</body>\n</html>'
    )


def render(briefing: dict[str, Any], fmt: str = "text") -> str:
    """Render briefing in the specified format."""
    if fmt == "json":
        return render_json(briefing)
    elif fmt == "markdown":
        return render_markdown(briefing)
    elif fmt == "html":
        return render_html(briefing)
    else:
        return render_text(briefing)