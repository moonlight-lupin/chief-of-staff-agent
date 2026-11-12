#!/usr/bin/env python3
"""Notion as a read-only input to briefings, reviews and meeting prep.

The agent reads Notion with its own connector and adds an optional ``notion``
block to the workspace envelope (shape: ``schemas.validate_notion_block``).
This module turns that block into briefing material:

* recently edited pages and comments/pages that mention the operator;
* Notion-only tasks (not yet in the plugin store) that are due or overdue;
* sync status per mirrored database: Notion edits waiting for review,
  conflicts, changes not yet queued (via ``notion_mirror.plan_sync``,
  which is read-only).

Nothing here writes to the store, the queue or Notion. Comment and page text
is used only for matching and is never returned.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

DONE_STATUSES = frozenset({"done", "cancelled", "canceled", "complete", "completed", "archived"})
GENERIC_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
    "icloud.com", "me.com", "proton.me", "protonmail.com",
})
MAX_PAGES = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_day(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None



def _recent_pages(pages: Iterable[Mapping[str, Any]], since: datetime) -> list[dict[str, Any]]:
    recent = [p for p in pages if (_parse(p.get("last_edited_time")) or since - timedelta(1)) >= since]
    recent.sort(key=lambda p: _parse(p.get("last_edited_time")), reverse=True)
    return [
        {k: p.get(k) for k in ("id", "title", "last_edited_time", "last_edited_by", "url") if p.get(k)}
        for p in recent[:MAX_PAGES]
    ]


def _mentions(block: Mapping[str, Any], since: datetime) -> list[dict[str, Any]]:
    titles = {p.get("id"): p.get("title") for p in block.get("pages") or []}
    out: list[dict[str, Any]] = []
    for c in block.get("comments") or []:
        when = _parse(c.get("created_time"))
        if c.get("mentions_operator") and when and when >= since:
            out.append({"page_id": c.get("page_id"),
                        "page_title": c.get("page_title") or titles.get(c.get("page_id")) or c.get("page_id"),
                        "author": c.get("author"), "at": c.get("created_time"), "kind": "comment"})
    for p in block.get("pages") or []:
        when = _parse(p.get("last_edited_time"))
        if p.get("mentions_operator") and when and when >= since:
            out.append({"page_id": p.get("id"), "page_title": p.get("title"),
                        "author": p.get("last_edited_by") or "someone", "at": p.get("last_edited_time"),
                        "kind": "page"})
    out.sort(key=lambda m: _parse(m["at"]), reverse=True)
    return out


def _values(config: Any, domain: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """Row values under the plugin's column names, honouring notion.columns."""
    from notion_mirror import PROPERTY_MAP, NotionConfigError, row_values

    if domain not in PROPERTY_MAP:
        return dict(row.get("properties") or {})
    try:
        return row_values(config, domain, row)
    except NotionConfigError:
        return dict(row.get("properties") or {})


def _notion_only_tasks(config: Any, rows: list[Mapping[str, Any]], today: date) -> list[dict[str, Any]]:
    from notion_mirror import KEY_PROP, load_mirror

    mirrored_pages = {m.get("page_id") for m in load_mirror(config).get("todos", {}).values()}
    soon = today + timedelta(days=1)
    tasks: list[dict[str, Any]] = []
    for row in rows:
        vals = _values(config, "todos", row)
        if vals.get(KEY_PROP) or row.get("page_id") in mirrored_pages:
            continue  # already in the store; the store's own todos cover it
        if str(vals.get("Status") or "open").lower() in DONE_STATUSES:
            continue
        due = _parse_day(vals.get("Due"))
        if due is None or due > soon:
            continue
        tasks.append({"title": vals.get("Name") or "(untitled)", "due": due.isoformat(),
                      "overdue": due < today, "page_id": row.get("page_id")})
    tasks.sort(key=lambda t: t["due"])
    return tasks


def _sync_status(config: Any, databases: Mapping[str, list], notes: list[str]) -> dict[str, Any]:
    from notion_mirror import (
        ACTION_ARCHIVE, ACTION_PULL, ACTION_UPSERT, NotionConfigError, _open_action_keys, notion_settings,
        plan_sync,
    )

    mapped = notion_settings(config)["databases"]
    open_keys = _open_action_keys(config)
    status: dict[str, Any] = {}
    for domain, rows in databases.items():
        if domain not in mapped:
            notes.append(f"Notion rows for '{domain}' were supplied but no database is mapped for it; not compared.")
            continue
        try:
            plan = plan_sync(config, domain, rows)
        except NotionConfigError as exc:
            notes.append(str(exc))
            continue
        keys = [f"{ACTION_UPSERT}:notion:{domain}/{p['record_id']}" for p in plan["push"]]
        keys += [f"{ACTION_ARCHIVE}:notion:{domain}/{a['record_id']}" for a in plan["archive"]]
        keys += [
            f"{ACTION_PULL}:{domain}/{p['record_id']}" if p["record_id"] else f"{ACTION_PULL}:{domain}/new:{p['page_id']}"
            for p in plan["pull"]
        ]
        queued = sum(1 for k in keys if k in open_keys)
        status[domain] = {
            "to_push": len(plan["push"]),
            "notion_edits_to_review": len(plan["pull"]),
            "to_archive": len(plan["archive"]),
            "conflicts": plan["conflicts"],
            "rejected": len(plan["rejected"]),
            "in_sync": len(plan["in_sync"]),
            "already_queued": queued,
            "not_yet_queued": len(keys) - queued,
        }
    return status


def summarise(config: Any, block: Mapping[str, Any] | None, since_hours: float = 24,
              now: datetime | None = None, since: datetime | None = None) -> dict[str, Any]:
    """Read-only briefing material from an envelope's ``notion`` block."""

    block = block or {}
    now = now or _now()
    since = since or now - timedelta(hours=since_hours)
    notes: list[str] = []

    recent_pages = _recent_pages(block.get("pages") or [], since)
    mentions = _mentions(block, since)
    databases = block.get("databases") or {}
    tasks = _notion_only_tasks(config, databases.get("todos") or [], now.date())
    sync = _sync_status(config, databases, notes)

    needs_attention: list[dict[str, Any]] = []
    for m in mentions:
        needs_attention.append({
            "title": f"Notion: {m['author']} mentioned you on '{m['page_title']}'",
            "risk": "medium",
            "why": f"Notion {m['kind']} at {m['at']}",
        })
    for t in tasks:
        state = "overdue" if t["overdue"] else "due"
        needs_attention.append({
            "title": f"Notion task {state}: {t['title']} ({t['due']})",
            "risk": "high" if t["overdue"] else "medium",
            "why": "In Notion only; run notion-sync to bring it into the task list",
        })
    next_actions: list[dict[str, Any]] = []
    for domain, s in sync.items():
        for conflict in s["conflicts"]:
            needs_attention.append({
                "title": f"Notion sync conflict: {domain} {conflict['record_id']}",
                "risk": "medium",
                "why": conflict["reason"],
            })
        if s["not_yet_queued"]:
            next_actions.append({
                "title": f"Run notion-sync for {domain}: {s['not_yet_queued']} change(s) to queue for review",
                "risk": "low",
                "why": (f"{s['notion_edits_to_review']} Notion edit(s) to review, {s['to_push']} store "
                        f"change(s) to push, {s['to_archive']} to archive"),
            })

    return {
        "window_start": since.isoformat(),
        "recent_pages": recent_pages,
        "mentions": mentions,
        "notion_only_tasks": tasks,
        "sync": sync,
        "needs_attention": needs_attention,
        "next_actions": next_actions,
        "notes": notes,
        "count": len(needs_attention) + len(next_actions),
    }


# ------------------------------------------------------------------ meeting prep


def _operator_domain(config: Any) -> str | None:
    try:
        email = str((config.get("user") or {}).get("email") or "")
    except AttributeError:
        return None
    return email.rsplit("@", 1)[1].lower() if "@" in email else None


def meeting_context(config: Any, block: Mapping[str, Any] | None, attendees: Iterable[str]) -> list[dict[str, Any]]:
    """Notion pages and mirrored rows that relate to the meeting's attendees.

    Matches on attendee emails, their company domains (generic mail providers
    and the operator's own domain excluded), and the client names of pipeline
    deals whose contact is an attendee. Read-only; page text is used for
    matching only and is not returned.
    """

    from state_db import load_store

    block = block or {}
    emails = {a.strip().lower() for a in attendees if a and "@" in a}
    own = _operator_domain(config)
    domains = {e.rsplit("@", 1)[1] for e in emails} - GENERIC_MAIL_DOMAINS - ({own} if own else set())
    external = {e for e in emails if e.rsplit("@", 1)[1] in domains}

    clients: set[str] = set()
    for deal in load_store("pipeline", config).get("deals") or []:
        contact = str(deal.get("contact_email") or "").lower()
        if contact and (contact in external or contact.rsplit("@", 1)[-1] in domains) and deal.get("client_name"):
            clients.add(str(deal["client_name"]))

    def reasons(*texts: Any) -> list[str]:
        hay = " ".join(str(t) for t in texts if t).lower()
        found = [f"client {c}" for c in sorted(clients) if c.lower() in hay]
        found += [f"attendee {e}" for e in sorted(external) if e in hay]
        found += [f"domain {d}" for d in sorted(domains) if d in hay]
        return found

    out: list[dict[str, Any]] = []
    for page in block.get("pages") or []:
        matched = reasons(page.get("title"), page.get("text"))
        if matched:
            out.append({"kind": "page", "title": page.get("title"), "url": page.get("url"),
                        "last_edited_time": page.get("last_edited_time"), "matched": matched})
    for domain, rows in (block.get("databases") or {}).items():
        for row in rows:
            vals = _values(config, domain, row)
            name = vals.get("Name")
            matched = reasons(name, vals.get("Contact"), vals.get("Email"))
            if matched:
                out.append({"kind": f"{domain} row", "title": name or row.get("page_id"),
                            "page_id": row.get("page_id"), "last_edited_time": row.get("last_edited_time"),
                            "matched": matched})
    out.sort(key=lambda c: str(c.get("last_edited_time") or ""), reverse=True)
    return out
