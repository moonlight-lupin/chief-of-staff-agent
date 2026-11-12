#!/usr/bin/env python3
"""Notion mirror: plan and record sync between the plugin store and Notion.

Design: docs/MULTI_COMPANY.md (decision D1). The plugin's store is the source
of truth and Notion is a mirror. This module never talks to Notion: under the
``agent`` provider the agent reads Notion with its own connector, passes the
rows in, and performs approved writes itself (claim → record-execution).

Three-way comparison per record, using the hash stored at the last sync:

* store changed, Notion unchanged   → push   (``notion.db.upsert``, agent path)
* Notion changed, store unchanged   → pull   (``notion.pull``, Python path,
                                              applied only after approval)
* both changed                      → conflict, no action
* store record gone, was mirrored   → archive (``notion.page.archive``)
* Notion row with no CoS ID         → pull create (new task/deal from Notion)

Planning is read-only. ``queue_plan`` only creates pending actions, which
start in ``requested``; nothing reaches Notion or the store without approval.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Mapping

MIRROR_STORE = "notion_mirror"
KEY_PROP = "CoS ID"
OPEN_STATES = frozenset({"requested", "approved", "executing"})

# Notion property name → store field, per domain. Order is the column order.
PROPERTY_MAP: dict[str, list[tuple[str, str]]] = {
    "pipeline": [
        (KEY_PROP, "id"), ("Name", "client_name"), ("Stage", "stage"), ("Status", "status"),
        ("Value", "value"), ("Currency", "currency"), ("Contact", "contact_name"),
        ("Email", "contact_email"), ("Last activity", "last_activity"),
    ],
    "todos": [
        (KEY_PROP, "id"), ("Name", "title"), ("Status", "status"), ("Priority", "priority"),
        ("Due", "due"), ("Tags", "tags"),
    ],
}
RECORDS_KEY = {"pipeline": "deals", "todos": "todos"}

# Fields a Notion edit may change in the store. Everything else is owned by
# the plugin (ids, client names, history) and is reported, not pulled.
PULL_ALLOWLIST: dict[str, dict[str, str]] = {
    "pipeline": {"Stage": "stage", "Value": "value", "Contact": "contact_name", "Email": "contact_email"},
    "todos": {"Name": "title", "Status": "status", "Priority": "priority", "Due": "due"},
}

ACTION_UPSERT = "notion.db.upsert"
ACTION_ARCHIVE = "notion.page.archive"
ACTION_PAGE_CREATE = "notion.page.create"
ACTION_PAGE_UPDATE = "notion.page.update"
ACTION_PULL = "notion.pull"
AGENT_ACTIONS = frozenset({ACTION_UPSERT, ACTION_ARCHIVE, ACTION_PAGE_CREATE, ACTION_PAGE_UPDATE})


class NotionConfigError(ValueError):
    """Notion is not configured for what was asked."""


# ------------------------------------------------------------------ config


def notion_settings(config: Any) -> dict[str, Any]:
    """``{databases: {domain: id}, wiki_parent_page: id|None}`` from config."""
    try:
        raw = config.get("notion") or {}
    except AttributeError:
        raw = {}
    if not hasattr(raw, "get"):
        raw = {}
    databases = raw.get("databases") or {}
    databases = {str(k): str(v) for k, v in databases.items() if v and str(k) in PROPERTY_MAP} \
        if hasattr(databases, "items") else {}
    wiki = raw.get("wiki_parent_page")
    return {"databases": databases, "wiki_parent_page": str(wiki) if wiki else None}


def is_configured(config: Any) -> bool:
    settings = notion_settings(config)
    return bool(settings["databases"] or settings["wiki_parent_page"])


def _database_id(config: Any, domain: str) -> str:
    if domain not in PROPERTY_MAP:
        raise NotionConfigError(f"'{domain}' cannot be synced to Notion (supported: {', '.join(PROPERTY_MAP)})")
    db_id = notion_settings(config)["databases"].get(domain)
    if not db_id:
        raise NotionConfigError(f"no Notion database is mapped for '{domain}': set notion.databases.{domain}")
    return db_id


# ------------------------------------------------------------------ values


def _norm(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (list, tuple)):
        items = [_norm(v) for v in value]
        items = [v for v in items if v is not None]
        return items or None
    if isinstance(value, date):
        return value.isoformat()
    return value


def to_properties(domain: str, record: Mapping[str, Any]) -> dict[str, Any]:
    return {prop: _norm(record.get(field)) for prop, field in PROPERTY_MAP[domain]}


def _row_properties(domain: str, row: Mapping[str, Any]) -> dict[str, Any]:
    props = row.get("properties") or {}
    return {prop: _norm(props.get(prop)) for prop, _ in PROPERTY_MAP[domain]}


def _hash(props: Mapping[str, Any]) -> str:
    material = json.dumps(props, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _validate_changes(config: Any, domain: str, changes: Mapping[str, Any]) -> str | None:
    """Why these Notion values cannot go into the store, or None."""
    from schemas import TODO_PRIORITIES, TODO_STATUSES

    if domain == "pipeline":
        stages = list(config.get("sales_stages") or []) if hasattr(config, "get") else []
        if "stage" in changes and stages and changes["stage"] not in stages:
            return f"stage {changes['stage']!r} is not one of the configured sales_stages"
        if "value" in changes and changes["value"] is not None and not isinstance(changes["value"], (int, float)):
            return f"value {changes['value']!r} is not a number"
        if "client_name" in changes and not changes["client_name"]:
            return "a new deal needs a Name"
    if domain == "todos":
        if "status" in changes and changes["status"] not in TODO_STATUSES:
            return f"status {changes['status']!r} must be one of {sorted(TODO_STATUSES)}"
        if changes.get("priority") is not None and changes["priority"] not in TODO_PRIORITIES:
            return f"priority {changes['priority']!r} must be one of {sorted(TODO_PRIORITIES)}"
        if "title" in changes and not changes["title"]:
            return "a task needs a Name"
    return None


# ------------------------------------------------------------------ mirror


def load_mirror(config: Any) -> dict[str, Any]:
    from state_db import _open_db

    with _open_db(config) as db:
        return db.get_kv(MIRROR_STORE) or {}


def _update_mirror(config: Any, fn) -> None:
    from state_db import _open_db

    with _open_db(config) as db:
        db.mutate_kv(MIRROR_STORE, fn)


def _load_records(config: Any, domain: str) -> list[dict[str, Any]]:
    from state_db import load_store

    return list(load_store(domain, config).get(RECORDS_KEY[domain]) or [])


# ------------------------------------------------------------------ planning


def plan_sync(config: Any, domain: str, rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare the store with Notion rows (read by the agent). Read-only."""

    database_id = _database_id(config, domain)
    records = {str(r["id"]): r for r in _load_records(config, domain) if r.get("id")}
    mirror = load_mirror(config).get(domain, {})
    page_to_key = {m.get("page_id"): key for key, m in mirror.items() if m.get("page_id")}

    rows_by_key: dict[str, Mapping[str, Any]] = {}
    new_rows: list[Mapping[str, Any]] = []
    for row in rows:
        key = _norm((row.get("properties") or {}).get(KEY_PROP)) or page_to_key.get(row.get("page_id"))
        if key:
            rows_by_key[str(key)] = row
        else:
            new_rows.append(row)

    plan: dict[str, Any] = {
        "domain": domain, "database_id": database_id,
        "push": [], "pull": [], "archive": [], "conflicts": [], "ignored": [],
        "rejected": [], "in_sync": [], "missing_in_notion": [], "adopt": [],
    }

    def push(record_id: str, props: dict[str, Any], row: Mapping[str, Any] | None) -> None:
        plan["push"].append({
            "record_id": record_id, "page_id": row.get("page_id") if row else None,
            "properties": props, "props_hash": _hash(props),
            "expected_last_edited": row.get("last_edited_time") if row else None,
        })

    for record_id, record in records.items():
        props = to_properties(domain, record)
        h = _hash(props)
        m = mirror.get(record_id)
        row = rows_by_key.get(record_id)
        if row is None:
            if m is None:
                push(record_id, props, None)
            else:
                # Mirrored before but the page is gone: deleted or archived in
                # Notion. Re-creating it would undo the user's change.
                plan["missing_in_notion"].append(record_id)
            continue

        key_missing = not _norm((row.get("properties") or {}).get(KEY_PROP))
        notion_props = {**_row_properties(domain, row), KEY_PROP: record_id}
        nh = _hash(notion_props)

        if m is None:
            if nh == h:
                plan["adopt"].append({"record_id": record_id, "page_id": row.get("page_id"), "hash": h,
                                      "last_edited_time": row.get("last_edited_time")})
                if key_missing:
                    push(record_id, props, row)
            else:
                plan["conflicts"].append({"record_id": record_id, "page_id": row.get("page_id"),
                                          "reason": "page exists in Notion but was never synced, and differs"})
            continue

        if nh == h:
            if key_missing:
                push(record_id, props, row)
            else:
                plan["in_sync"].append(record_id)
            continue

        notion_changed = nh != m.get("hash")
        store_changed = h != m.get("hash")
        if store_changed and not notion_changed:
            push(record_id, props, row)
        elif notion_changed and not store_changed:
            allow = PULL_ALLOWLIST[domain]
            changes: dict[str, Any] = {}
            ignored: list[str] = []
            for prop, _field in PROPERTY_MAP[domain]:
                if prop == KEY_PROP or notion_props[prop] == props[prop]:
                    continue
                if prop in allow:
                    changes[allow[prop]] = notion_props[prop]
                else:
                    ignored.append(prop)
            if ignored:
                plan["ignored"].append({"record_id": record_id, "fields": ignored})
            if changes:
                problem = _validate_changes(config, domain, changes)
                if problem:
                    plan["rejected"].append({"record_id": record_id, "page_id": row.get("page_id"),
                                             "reason": problem})
                else:
                    plan["pull"].append({
                        "op": "update", "record_id": record_id, "page_id": row.get("page_id"),
                        "changes": changes, "base_hash": h, "notion_properties": notion_props,
                        "notion_last_edited": row.get("last_edited_time"),
                    })
        else:
            plan["conflicts"].append({"record_id": record_id, "page_id": row.get("page_id"),
                                      "reason": "changed in both the store and Notion since the last sync"})

    for key, row in rows_by_key.items():
        if key in records:
            continue
        if key in mirror:
            plan["archive"].append({"record_id": key, "page_id": row.get("page_id")})
        else:
            plan["conflicts"].append({"record_id": key, "page_id": row.get("page_id"),
                                      "reason": "CoS ID is not in the store and was never synced"})

    for row in new_rows:
        notion_props = _row_properties(domain, row)
        fields = {field: notion_props[prop] for prop, field in PULL_ALLOWLIST[domain].items()}
        if domain == "pipeline":
            fields["client_name"] = notion_props.get("Name")
        fields = {k: v for k, v in fields.items() if v is not None or k in {"title", "client_name"}}
        problem = _validate_changes(config, domain, fields)
        if problem:
            plan["rejected"].append({"record_id": None, "page_id": row.get("page_id"), "reason": problem})
            continue
        plan["pull"].append({"op": "create", "record_id": None, "page_id": row.get("page_id"),
                             "changes": fields, "base_hash": None, "notion_properties": notion_props,
                             "notion_last_edited": row.get("last_edited_time")})
    return plan


def _open_action_keys(config: Any) -> set[str]:
    from state_db import list_pending_actions

    return {
        f"{a.get('type')}:{a.get('target')}"
        for a in list_pending_actions(config)
        if a.get("state") in OPEN_STATES
    }


def queue_plan(config: Any, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Create pending actions for a plan. Nothing executes here."""

    from state_db import create_pending_action

    domain = plan["domain"]
    open_keys = _open_action_keys(config)
    created: list[dict[str, Any]] = []
    skipped: list[str] = []

    def queue(action_type: str, provider: str, target: str, payload: dict[str, Any], summary: str) -> None:
        key = f"{action_type}:{target}"
        if key in open_keys:
            skipped.append(key)
            return
        action = create_pending_action(config, action_type, provider, target, payload, summary=summary)
        if action:
            created.append(action)
            open_keys.add(key)

    if plan.get("adopt"):
        def adopt(data: dict[str, Any]) -> None:
            section = data.setdefault(domain, {})
            for entry in plan["adopt"]:
                section[entry["record_id"]] = {"page_id": entry["page_id"], "hash": entry["hash"],
                                               "last_edited_time": entry["last_edited_time"]}
        _update_mirror(config, adopt)

    for item in plan.get("push", []):
        verb = "Update" if item["page_id"] else "Create"
        queue(ACTION_UPSERT, "agent", f"notion:{domain}/{item['record_id']}",
              {"domain": domain, "database_id": plan["database_id"], **item},
              f"{verb} Notion {domain} row for {item['record_id']}")
    for item in plan.get("archive", []):
        queue(ACTION_ARCHIVE, "agent", f"notion:{domain}/{item['record_id']}",
              {"domain": domain, **item},
              f"Archive Notion {domain} page for removed record {item['record_id']}")
    for item in plan.get("pull", []):
        target = f"{domain}/{item['record_id']}" if item["record_id"] else f"{domain}/new:{item['page_id']}"
        fields = ", ".join(f"{k}={v}" for k, v in item["changes"].items())
        what = f"Apply Notion edit to {item['record_id']}" if item["record_id"] else f"Add {domain} record from Notion"
        queue(ACTION_PULL, "local", target, {"domain": domain, **item}, f"{what}: {fields}")
    return {"created": created, "skipped_open": skipped, "adopted": len(plan.get("adopt", []))}


# ------------------------------------------------------------------ execution


def _new_record_id(domain: str, page_id: str, existing: set[str]) -> str:
    base = f"{'deal' if domain == 'pipeline' else 'todo'}-n{''.join(c for c in str(page_id) if c.isalnum())[:8]}"
    candidate, n = base, 2
    while candidate in existing:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def execute_pull(config: Any, action_id: str) -> dict[str, Any]:
    """Apply an approved ``notion.pull`` to the store (Python path)."""

    from state_db import _open_db, get_pending_action, mark_executed, mark_executing, mark_failed

    action = get_pending_action(config, action_id)
    if not action or action.get("type") != ACTION_PULL:
        return {"success": False, "error": f"not a {ACTION_PULL} action: {action_id}"}
    if mark_executing(config, action_id) is None:
        return {"success": False, "error": "could not claim the action (approval lapsed or already claimed)"}

    payload = action.get("payload") or {}
    domain = payload.get("domain")
    changes = dict(payload.get("changes") or {})
    today = date.today().isoformat()
    outcome: dict[str, Any] = {}

    def apply(data: dict[str, Any]) -> None:
        records = data.setdefault(RECORDS_KEY[domain], [])
        if payload.get("op") == "create":
            record_id = _new_record_id(domain, payload.get("page_id") or "", {str(r.get("id")) for r in records})
            if domain == "todos":
                record = {"id": record_id, "title": changes.get("title"), "status": changes.get("status") or "open",
                          "priority": changes.get("priority") or "medium", "due": changes.get("due"),
                          "created": today, "completed": None, "source_skill": "notion-sync",
                          "source_ref": payload.get("page_id")}
            else:
                stages = list(config.get("sales_stages") or ["Lead"])
                stage = changes.get("stage") or stages[0]
                record = {"id": record_id, "client_name": changes.get("client_name"), "stage": stage,
                          "status": "active", "value": changes.get("value"),
                          "currency": config["company"].get("currency"),
                          "contact_name": changes.get("contact_name"), "contact_email": changes.get("contact_email"),
                          "created": today, "last_activity": today,
                          "stage_history": [{"stage": stage, "at": today}], "source": "notion-sync"}
            records.append({k: v for k, v in record.items() if v is not None or k in {"completed"}})
        else:
            record_id = payload.get("record_id")
            record = next((r for r in records if str(r.get("id")) == record_id), None)
            if record is None:
                raise ValueError(f"{record_id} is no longer in the store")
            if _hash(to_properties(domain, record)) != payload.get("base_hash"):
                raise ValueError(f"{record_id} changed in the store after the sync was planned; plan again")
            if domain == "pipeline" and "stage" in changes and changes["stage"] != record.get("stage"):
                record.setdefault("stage_history", []).append({"stage": changes["stage"], "at": today})
                record["last_activity"] = today
            record.update(changes)
        outcome["record_id"] = record_id
        # The mirror records what Notion holds, not the store: applying the
        # pull may also touch plugin-owned fields (e.g. last_activity), and
        # those must go back to Notion as a push on the next sync.
        outcome["hash"] = _hash({**(payload.get("notion_properties") or {}), KEY_PROP: record_id})

    try:
        with _open_db(config) as db:
            db.mutate_kv(domain, apply)
    except Exception as exc:
        mark_failed(config, action_id, str(exc))
        return {"success": False, "error": str(exc), "action_type": ACTION_PULL}

    def remember(data: dict[str, Any]) -> None:
        data.setdefault(domain, {})[outcome["record_id"]] = {
            "page_id": payload.get("page_id"), "hash": outcome["hash"],
            "last_edited_time": payload.get("notion_last_edited"),
        }

    _update_mirror(config, remember)
    result = {"success": True, "action_type": ACTION_PULL, "domain": domain, "record_id": outcome["record_id"]}
    mark_executed(config, action_id, result)
    return result


def on_executed(config: Any, action: Mapping[str, Any], data: Mapping[str, Any]) -> None:
    """Update the mirror after an agent recorded a successful Notion write."""

    action_type = action.get("type")
    payload = action.get("payload") or {}
    page_id = (data or {}).get("page_id") or payload.get("page_id")

    if action_type == ACTION_UPSERT:
        if not page_id:
            print(
                f"WARNING: {action.get('id')} created a Notion page but no page_id was recorded, so it is "
                f"not mirrored and the next sync will propose it again. Pass --result-json "
                f"'{{\"page_id\": \"...\"}}' with record-execution.",
                file=sys.stderr,
            )
            return
        domain, record_id = payload["domain"], payload["record_id"]

        def remember(mirror: dict[str, Any]) -> None:
            mirror.setdefault(domain, {})[record_id] = {
                "page_id": page_id, "hash": payload.get("props_hash"),
                "last_edited_time": (data or {}).get("last_edited_time"),
            }
        _update_mirror(config, remember)
    elif action_type == ACTION_ARCHIVE:
        def forget(mirror: dict[str, Any]) -> None:
            mirror.get(payload.get("domain"), {}).pop(payload.get("record_id"), None)
        _update_mirror(config, forget)
    elif action_type in {ACTION_PAGE_CREATE, ACTION_PAGE_UPDATE}:
        if not page_id:
            print(f"WARNING: {action.get('id')}: no page_id recorded; the wiki page is not mirrored.",
                  file=sys.stderr)
            return

        def remember_page(mirror: dict[str, Any]) -> None:
            mirror.setdefault("wiki", {})[payload["source_path"]] = {
                "page_id": page_id, "hash": payload.get("content_hash"),
            }
        _update_mirror(config, remember_page)


# ------------------------------------------------------------------ wiki


def queue_wiki_page(config: Any, path: str | Path) -> dict[str, Any] | None:
    """Queue publishing one wiki Markdown file as a Notion page.

    Returns the pending action, or None when the published page is current.
    """

    from config_loader import get_project_root
    from state_db import create_pending_action

    parent = notion_settings(config)["wiki_parent_page"]
    if not parent:
        raise NotionConfigError("no Notion wiki parent page: set notion.wiki_parent_page")
    root = get_project_root(config)
    try:
        wiki_root = Path(str(config["paths"].get("wiki_path") or (root / "wiki"))).expanduser().resolve()
    except (KeyError, AttributeError):
        wiki_root = (root / "wiki").resolve()
    source = Path(path).expanduser().resolve()
    if not source.is_file() or not source.is_relative_to(wiki_root):
        raise NotionConfigError(f"{source} is not a file inside the wiki ({wiki_root})")

    content = source.read_text(encoding="utf-8")
    rel = source.relative_to(wiki_root).as_posix()
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    title = next((line.lstrip("#").strip() for line in content.splitlines() if line.startswith("# ")),
                 source.stem.replace("-", " ").title())

    existing = load_mirror(config).get("wiki", {}).get(rel)
    if existing and existing.get("hash") == content_hash:
        return None
    payload = {"source_path": rel, "title": title, "markdown": content, "content_hash": content_hash,
               "parent_page_id": parent}
    if existing:
        payload["page_id"] = existing["page_id"]
        return create_pending_action(config, ACTION_PAGE_UPDATE, "agent", f"notion:wiki/{rel}", payload,
                                     summary=f"Update Notion page '{title}' from wiki/{rel}")
    return create_pending_action(config, ACTION_PAGE_CREATE, "agent", f"notion:wiki/{rel}", payload,
                                 summary=f"Publish wiki/{rel} to Notion as '{title}'")
