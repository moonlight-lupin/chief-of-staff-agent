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

import json
from datetime import datetime, timezone
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
        lines.append(f"Recent activity (last 24h):")
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


def render(briefing: dict[str, Any], fmt: str = "text") -> str:
    """Render briefing in the specified format."""
    if fmt == "json":
        return render_json(briefing)
    elif fmt == "markdown":
        return render_markdown(briefing)
    else:
        return render_text(briefing)