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
ACTION_LINK = "notion.link"
ACTION_RESOLVE = "notion.resolve"
ACTION_ADD_PROPERTY = "notion.db.add_property"
AGENT_ACTIONS = frozenset({ACTION_UPSERT, ACTION_ARCHIVE, ACTION_PAGE_CREATE, ACTION_PAGE_UPDATE,
                           ACTION_ADD_PROPERTY})
LOCAL_ACTIONS = frozenset({ACTION_PULL, ACTION_LINK, ACTION_RESOLVE})


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


# ------------------------------------------------------------------ columns

# Notion property types each column accepts; the first is used when adding it.
EXPECTED_TYPES: dict[str, tuple[str, ...]] = {
    KEY_PROP: ("rich_text",), "Name": ("title",), "Stage": ("status", "select"),
    "Status": ("status", "select"), "Value": ("number",), "Currency": ("select", "rich_text"),
    "Contact": ("rich_text",), "Email": ("email", "rich_text"), "Last activity": ("date",),
    "Priority": ("select", "status"), "Due": ("date",), "Tags": ("multi_select",),
}
# Common names in existing databases, tried before fuzzy matching.
SYNONYMS: dict[str, tuple[str, ...]] = {
    KEY_PROP: ("cos id", "cos_id", "chief of staff id"),
    "Name": ("name", "title", "company", "client", "task"),
    "Stage": ("stage", "deal stage", "pipeline stage", "sales stage"),
    "Status": ("status", "state"),
    "Value": ("value", "amount", "deal value", "deal size"),
    "Currency": ("currency", "ccy"),
    "Contact": ("contact", "contact name", "contact person", "person"),
    "Email": ("email", "contact email", "e-mail"),
    "Last activity": ("last activity", "last contacted", "last touch"),
    "Priority": ("priority",),
    "Due": ("due", "due date", "deadline"),
    "Tags": ("tags", "labels"),
}


class NotionNotReady(NotionConfigError):
    """Sync cannot run yet: the database must be checked or linked first."""


def column_map(config: Any, domain: str) -> dict[str, str | None]:
    """Plugin column → the property name in this Notion database (None = not present).

    Defaults to the documented names. ``notion.columns.<domain>`` overrides
    them, so an existing database keeps its own column names.
    """
    canon = [prop for prop, _ in PROPERTY_MAP[domain]]
    cmap: dict[str, str | None] = {c: c for c in canon}
    try:
        raw = ((config.get("notion") or {}).get("columns") or {}).get(domain) or {}
    except AttributeError:
        raw = {}
    for key, value in raw.items():
        key = str(key)
        if key not in cmap:
            raise NotionConfigError(
                f"notion.columns.{domain}: unknown column '{key}' (known: {', '.join(canon)})")
        cmap[key] = str(value).strip() if value not in (None, "") else None
    if cmap["Name"] is None:
        raise NotionConfigError(f"notion.columns.{domain}: Name cannot be null; every Notion database has a title")
    return cmap


def _map_hash(cmap: Mapping[str, Any]) -> str:
    return _hash(dict(cmap))


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
    """Every plugin column for a record, under the plugin's column names."""
    return {prop: _norm(record.get(field)) for prop, field in PROPERTY_MAP[domain]}


def _store_props(cmap: Mapping[str, Any], domain: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """The columns this database has, for a store record (plugin names)."""
    return {k: v for k, v in to_properties(domain, record).items() if cmap[k] is not None}


def row_values(config: Any, domain: str, row: Mapping[str, Any],
               cmap: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A Notion row's values under the plugin's column names (mapped columns only)."""
    cmap = cmap or column_map(config, domain)
    props = row.get("properties") or {}
    return {canon: _norm(props.get(actual)) for canon, actual in cmap.items() if actual is not None}


def _keyed(cmap: Mapping[str, Any], props: Mapping[str, Any], record_id: str) -> dict[str, Any]:
    return {**props, KEY_PROP: record_id} if cmap[KEY_PROP] is not None else dict(props)


def _to_notion_names(cmap: Mapping[str, Any], props: Mapping[str, Any]) -> dict[str, Any]:
    return {cmap[k]: v for k, v in props.items() if cmap.get(k)}


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


# ------------------------------------------------------------------ schema check


def check_schema(config: Any, domain: str, properties: Mapping[str, str]) -> dict[str, Any]:
    """Compare a Notion database's properties (name → type) with what sync needs.

    The agent reads the database schema with its Notion connector. The result
    is recorded so ``plan_sync`` can refuse to run against a database that
    was never checked, failed its check, or whose column map changed since.
    Recording the check is local bookkeeping; nothing is written to Notion.
    """
    import difflib

    database_id = _database_id(config, domain)
    cmap = column_map(config, domain)
    present = {str(name): str(kind) for name, kind in (properties or {}).items()}
    columns: list[dict[str, Any]] = []
    used = {actual for actual in cmap.values() if actual and actual in present}

    for canon, _field in PROPERTY_MAP[domain]:
        actual = cmap[canon]
        entry: dict[str, Any] = {"canonical": canon, "mapped_to": actual,
                                 "expected_types": list(EXPECTED_TYPES[canon]), "suggestion": None}
        if actual is None:
            entry["status"] = "excluded"
        elif actual not in present:
            entry["status"] = "missing"
        elif present[actual] not in EXPECTED_TYPES[canon]:
            entry["status"] = "wrong_type"
            entry["found_type"] = present[actual]
        else:
            entry["status"] = "ok"
        columns.append(entry)

    def candidates(canon: str) -> list[str]:
        return [n for n, t in present.items() if n not in used and t in EXPECTED_TYPES[canon]]

    missing = [c for c in columns if c["status"] == "missing"]
    for c in missing:  # pass 1: the title column, exact names and known synonyms
        names = candidates(c["canonical"])
        pick = None
        if c["canonical"] == "Name":
            pick = names[0] if names else None
        else:
            wanted = {c["canonical"].lower(), *SYNONYMS.get(c["canonical"], ())}
            pick = next((n for n in names if n.lower() in wanted), None)
        if pick:
            c["suggestion"] = pick
            used.add(pick)
    for c in missing:  # pass 2: close spellings (never for the key column)
        if c["suggestion"] or c["canonical"] == KEY_PROP:
            continue
        names = candidates(c["canonical"])
        close = difflib.get_close_matches(c["canonical"].lower(), [n.lower() for n in names], n=1, cutoff=0.6)
        if close:
            pick = next(n for n in names if n.lower() == close[0])
            c["suggestion"] = pick
            used.add(pick)

    key = next(c for c in columns if c["canonical"] == KEY_PROP)
    key_column = {"ok": "present", "excluded": "excluded"}.get(key["status"], "missing")
    ready = all(c["status"] in {"ok", "excluded"} for c in columns)
    suggested = {c["canonical"]: c["suggestion"] for c in missing if c["canonical"] != KEY_PROP}
    suggested.update({c["canonical"]: c["mapped_to"] for c in columns
                      if c["status"] == "ok" and c["mapped_to"] != c["canonical"]})
    report = {
        "domain": domain, "database_id": database_id, "ready": ready, "key_column": key_column,
        "columns": columns,
        "suggested_config": {"notion": {"columns": {domain: suggested}}} if suggested else None,
        "can_add": [c["canonical"] for c in missing if c["canonical"] != "Name"],
    }

    def record(mirror: dict[str, Any]) -> None:
        mirror.setdefault("_schema", {})[domain] = {
            "database_id": database_id, "ready": ready, "map_hash": _map_hash(cmap),
            "key_column": key_column,
        }
    _update_mirror(config, record)
    return report


def _require_ready(config: Any, domain: str, database_id: str, cmap: Mapping[str, Any],
                   mirror_all: Mapping[str, Any]) -> None:
    check = (mirror_all.get("_schema") or {}).get(domain)
    hint = (f"run notion_sync.py check for '{domain}' (read the database's properties with your "
            f"Notion connector) and fix what it reports")
    if not check or check.get("database_id") != database_id:
        raise NotionNotReady(f"the '{domain}' Notion database has not been checked: {hint}")
    if check.get("map_hash") != _map_hash(cmap):
        raise NotionNotReady(f"notion.columns.{domain} changed since the last check: {hint}")
    if not check.get("ready"):
        raise NotionNotReady(f"the last check of the '{domain}' database found problems: {hint}")


# ------------------------------------------------------------------ planning


def plan_sync(config: Any, domain: str, rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare the store with Notion rows (read by the agent). Read-only."""

    database_id = _database_id(config, domain)
    cmap = column_map(config, domain)
    mirror_all = load_mirror(config)
    _require_ready(config, domain, database_id, cmap, mirror_all)
    records = {str(r["id"]): r for r in _load_records(config, domain) if r.get("id")}
    mirror = mirror_all.get(domain, {})
    page_to_key = {m.get("page_id"): key for key, m in mirror.items() if m.get("page_id")}
    key_enabled = cmap[KEY_PROP] is not None

    rows_by_key: dict[str, Mapping[str, Any]] = {}
    new_rows: list[Mapping[str, Any]] = []
    values: dict[int, dict[str, Any]] = {}
    for row in rows:
        vals = row_values(config, domain, row, cmap)
        values[id(row)] = vals
        key = (vals.get(KEY_PROP) if key_enabled else None) or page_to_key.get(row.get("page_id"))
        if key:
            rows_by_key[str(key)] = row
        else:
            new_rows.append(row)

    linked = bool(mirror) or bool((mirror_all.get("_linked") or {}).get(domain))
    if new_rows and records and not linked:
        raise NotionNotReady(
            f"the '{domain}' Notion database already has {len(new_rows)} row(s) that are not linked to "
            f"plugin records; syncing now would duplicate them. Run notion_sync.py link first."
        )

    plan: dict[str, Any] = {
        "domain": domain, "database_id": database_id,
        "push": [], "pull": [], "archive": [], "conflicts": [], "ignored": [],
        "rejected": [], "in_sync": [], "missing_in_notion": [], "adopt": [],
    }

    def push(record_id: str, props: dict[str, Any], row: Mapping[str, Any] | None) -> None:
        plan["push"].append({
            "record_id": record_id, "page_id": row.get("page_id") if row else None,
            "properties": _to_notion_names(cmap, props), "props_hash": _hash(props),
            "expected_last_edited": row.get("last_edited_time") if row else None,
        })

    for record_id, record in records.items():
        props = _store_props(cmap, domain, record)
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

        vals = values[id(row)]
        key_missing = key_enabled and not vals.get(KEY_PROP)
        notion_props = _keyed(cmap, vals, record_id)
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

        if m.get("hash") is None:
            plan["conflicts"].append({"record_id": record_id, "page_id": row.get("page_id"),
                                      "reason": "linked, but the store and Notion differ; choose which side to keep"})
            continue
        notion_changed = nh != m.get("hash")
        store_changed = h != m.get("hash")
        if store_changed and not notion_changed:
            push(record_id, props, row)
        elif notion_changed and not store_changed:
            allow = PULL_ALLOWLIST[domain]
            changes: dict[str, Any] = {}
            ignored: list[str] = []
            for prop in props:
                if prop == KEY_PROP or notion_props.get(prop) == props[prop]:
                    continue
                if prop in allow:
                    changes[allow[prop]] = notion_props.get(prop)
                else:
                    ignored.append(cmap[prop] or prop)
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
        vals = values[id(row)]
        fields = {field: vals.get(prop) for prop, field in PULL_ALLOWLIST[domain].items() if cmap[prop]}
        if domain == "pipeline":
            fields["client_name"] = vals.get("Name")
        fields = {k: v for k, v in fields.items() if v is not None or k in {"title", "client_name"}}
        problem = _validate_changes(config, domain, fields)
        if problem:
            plan["rejected"].append({"record_id": None, "page_id": row.get("page_id"), "reason": problem})
            continue
        plan["pull"].append({"op": "create", "record_id": None, "page_id": row.get("page_id"),
                             "changes": fields, "base_hash": None, "notion_properties": vals,
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
            if _hash(_store_props(column_map(config, domain), domain, record)) != payload.get("base_hash"):
                raise ValueError(f"{record_id} changed in the store after the sync was planned; plan again")
            if domain == "pipeline" and "stage" in changes and changes["stage"] != record.get("stage"):
                record.setdefault("stage_history", []).append({"stage": changes["stage"], "at": today})
                record["last_activity"] = today
            record.update(changes)
        outcome["record_id"] = record_id
        # The mirror records what Notion holds, not the store: applying the
        # pull may also touch plugin-owned fields (e.g. last_activity), and
        # those must go back to Notion as a push on the next sync.
        outcome["hash"] = _hash(_keyed(column_map(config, domain), payload.get("notion_properties") or {},
                                       record_id))

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
    elif action_type == ACTION_ADD_PROPERTY:
        # The database's columns changed: the recorded check no longer holds.
        def invalidate(mirror: dict[str, Any]) -> None:
            (mirror.get("_schema") or {}).pop(payload.get("domain"), None)
        _update_mirror(config, invalidate)
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


# ------------------------------------------------------------------ onboarding


def queue_add_columns(config: Any, report: Mapping[str, Any], columns: list[str]) -> list[dict[str, Any]]:
    """Queue adding missing columns to the Notion database (high risk, agent path)."""
    from state_db import create_pending_action

    domain = report["domain"]
    cmap = column_map(config, domain)
    by = {c["canonical"]: c for c in report["columns"]}
    for canon in columns:
        entry = by.get(canon)
        if entry is None or entry["status"] != "missing" or canon == "Name":
            status = entry["status"] if entry else "unknown"
            raise NotionConfigError(f"'{canon}' cannot be added: it is {status}, not a missing column")
    open_keys = _open_action_keys(config)
    created: list[dict[str, Any]] = []
    for canon in columns:
        prop = cmap[canon]
        target = f"notion:{domain}/schema/{prop}"
        if f"{ACTION_ADD_PROPERTY}:{target}" in open_keys:
            continue
        kind = EXPECTED_TYPES[canon][0]
        action = create_pending_action(
            config, ACTION_ADD_PROPERTY, "agent", target,
            {"domain": domain, "database_id": report["database_id"], "property": prop, "type": kind},
            summary=f"Add a '{prop}' ({kind}) column to the Notion {domain} database",
        )
        if action:
            created.append(action)
    return created


def _match_key(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def plan_link(config: Any, domain: str, rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Match existing Notion rows to store records, once. Read-only.

    Matches on the name (case- and space-insensitive); a tie is broken by the
    contact email (deals) or due date (tasks). Anything not clear-cut is
    reported, never guessed.
    """
    database_id = _database_id(config, domain)
    cmap = column_map(config, domain)
    mirror_all = load_mirror(config)
    _require_ready(config, domain, database_id, cmap, mirror_all)
    mirror = mirror_all.get(domain, {})
    mirrored_pages = {m.get("page_id") for m in mirror.values()}
    records = {str(r["id"]): r for r in _load_records(config, domain) if r.get("id")}
    name_field = "client_name" if domain == "pipeline" else "title"
    tie_prop, tie_field = ("Email", "contact_email") if domain == "pipeline" else ("Due", "due")

    matched: list[dict[str, Any]] = []
    free: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    for row in rows:
        if row.get("page_id") in mirrored_pages:
            continue
        vals = row_values(config, domain, row, cmap)
        key = vals.get(KEY_PROP) if cmap[KEY_PROP] else None
        if key and str(key) in records and str(key) not in mirror:
            matched.append({"record_id": str(key), "page_id": row.get("page_id"), "by": "id"})
        else:
            free.append((row, vals))

    taken_records = {m["record_id"] for m in matched}
    claimed: dict[str, str] = {}
    ambiguous: list[dict[str, Any]] = []
    for record_id, record in records.items():
        if record_id in mirror or record_id in taken_records:
            continue
        name = _match_key(record.get(name_field))
        cands = [(row, vals) for row, vals in free if name and _match_key(vals.get("Name")) == name]
        if len(cands) > 1:
            tie = _match_key(record.get(tie_field))
            narrowed = [c for c in cands if tie and _match_key(c[1].get(tie_prop)) == tie]
            cands = narrowed if len(narrowed) == 1 else cands
        if len(cands) == 1 and cands[0][0].get("page_id") not in claimed:
            page = cands[0][0].get("page_id")
            claimed[page] = record_id
            matched.append({"record_id": record_id, "page_id": page, "by": "name"})
        elif cands:
            ambiguous.append({"record_id": record_id, "page_ids": sorted(c[0].get("page_id") for c in cands)})

    in_doubt = {p for a in ambiguous for p in a["page_ids"]}
    matched_pages = {m["page_id"] for m in matched}
    matched_records = {m["record_id"] for m in matched} | {a["record_id"] for a in ambiguous}
    return {
        "domain": domain, "database_id": database_id,
        "matched": matched,
        "ambiguous": ambiguous,
        "notion_only": sorted(r.get("page_id") for r, _ in free
                              if r.get("page_id") not in matched_pages and r.get("page_id") not in in_doubt),
        "store_only": sorted(r for r in records if r not in matched_records and r not in mirror),
    }


def queue_link(config: Any, plan: Mapping[str, Any]) -> dict[str, Any] | None:
    """Queue recording the link (local, applied after approval)."""
    from state_db import create_pending_action

    domain = plan["domain"]
    target = f"notion:{domain}/link"
    if f"{ACTION_LINK}:{target}" in _open_action_keys(config):
        raise NotionConfigError(f"a link for '{domain}' is already waiting for approval")
    matches = [{"record_id": m["record_id"], "page_id": m["page_id"]} for m in plan["matched"]]
    summary = (f"Link {len(matches)} existing Notion {domain} row(s) to plugin records; "
               f"{len(plan['notion_only'])} Notion-only row(s) will be offered as imports, "
               f"{len(plan['store_only'])} plugin record(s) will be offered as new rows, "
               f"{len(plan['ambiguous'])} ambiguous left unlinked")
    return create_pending_action(config, ACTION_LINK, "local", target,
                                 {"domain": domain, "database_id": plan["database_id"], "matches": matches},
                                 summary=summary)


def queue_resolve(config: Any, domain: str, record_id: str, keep: str,
                  rows: list[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Queue resolving one conflict by choosing the side to keep (local, after approval)."""
    from state_db import create_pending_action

    if keep not in {"store", "notion"}:
        raise NotionConfigError("keep must be 'store' or 'notion'")
    plan = plan_sync(config, domain, rows)
    conflict = next((c for c in plan["conflicts"] if c["record_id"] == record_id), None)
    record = next((r for r in _load_records(config, domain) if str(r.get("id")) == record_id), None)
    row = next((r for r in rows if r.get("page_id") == (conflict or {}).get("page_id")), None)
    if conflict is None or record is None or row is None:
        raise NotionConfigError(f"{record_id} is not in conflict in these rows; nothing to resolve")
    cmap = column_map(config, domain)
    h = _hash(_store_props(cmap, domain, record))
    nh = _hash(_keyed(cmap, row_values(config, domain, row, cmap), record_id))
    # The baseline is the side being replaced: keeping the store makes Notion
    # look stale (→ push); keeping Notion makes the store look stale (→ pull).
    payload = {"domain": domain, "record_id": record_id, "keep": keep, "page_id": row.get("page_id"),
               "baseline": nh if keep == "store" else h, "store_hash": h}
    verb = "plugin values to Notion" if keep == "store" else "Notion values into the plugin"
    return create_pending_action(config, ACTION_RESOLVE, "local", f"notion:{domain}/{record_id}/resolve",
                                 payload, summary=f"Resolve {record_id}: copy {verb} on the next sync")


def execute_local(config: Any, action_id: str) -> dict[str, Any]:
    """Run an approved local Notion action: pull, link or resolve (Python path)."""
    from state_db import get_pending_action, mark_executed, mark_executing, mark_failed

    action = get_pending_action(config, action_id)
    action_type = (action or {}).get("type")
    if action_type == ACTION_PULL:
        return execute_pull(config, action_id)
    if action_type not in {ACTION_LINK, ACTION_RESOLVE}:
        return {"success": False, "error": f"not a local Notion action: {action_id}"}
    if mark_executing(config, action_id) is None:
        return {"success": False, "error": "could not claim the action (approval lapsed or already claimed)"}
    payload = action.get("payload") or {}
    domain = payload.get("domain")
    try:
        if action_type == ACTION_LINK:
            records = {str(r.get("id")) for r in _load_records(config, domain)}
            linked: list[str] = []

            def link(mirror: dict[str, Any]) -> None:
                section = mirror.setdefault(domain, {})
                for m in payload.get("matches") or []:
                    if m["record_id"] in records and m["record_id"] not in section:
                        # Unknown baseline: equal rows sync, differing rows become
                        # conflicts for the user to resolve — never a silent overwrite.
                        section[m["record_id"]] = {"page_id": m["page_id"], "hash": None}
                        linked.append(m["record_id"])
                mirror.setdefault("_linked", {})[domain] = True
            _update_mirror(config, link)
            result = {"success": True, "action_type": action_type, "linked": linked}
        else:
            record = next((r for r in _load_records(config, domain)
                           if str(r.get("id")) == payload.get("record_id")), None)
            if record is None:
                raise ValueError(f"{payload.get('record_id')} is no longer in the store")
            if _hash(_store_props(column_map(config, domain), domain, record)) != payload.get("store_hash"):
                raise ValueError(f"{payload.get('record_id')} changed in the store after this was queued; "
                                 f"resolve it again")

            def resolve(mirror: dict[str, Any]) -> None:
                mirror.setdefault(domain, {})[payload["record_id"]] = {
                    "page_id": payload.get("page_id"), "hash": payload.get("baseline"),
                }
            _update_mirror(config, resolve)
            result = {"success": True, "action_type": action_type, "record_id": payload["record_id"],
                      "keep": payload.get("keep")}
    except Exception as exc:
        mark_failed(config, action_id, str(exc))
        return {"success": False, "error": str(exc), "action_type": action_type}
    mark_executed(config, action_id, result)
    return result


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
