#!/usr/bin/env python3
"""Structured memory maintenance for the Chief-of-Staff plugin.

This module is READ + INTERNAL-WRITE only. It reads local event/state stores,
extracts low-confidence structured memory, writes only under
``project_root/.knowledge/``, and records every internal memory mutation in an
append-only change log.

It must never call workspace providers, approve/execute pending actions, send
email, or mutate Gmail/Calendar/Drive.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure shared/scripts is importable when run as a standalone script.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

try:
    import config_loader
except Exception:  # pragma: no cover - graceful standalone fallback
    config_loader = None  # type: ignore[assignment]

try:
    import event_store
except Exception:  # pragma: no cover
    event_store = None  # type: ignore[assignment]

try:
    import pending_actions
except Exception:  # pragma: no cover
    pending_actions = None  # type: ignore[assignment]

try:
    import suggested_actions
except Exception:  # pragma: no cover
    suggested_actions = None  # type: ignore[assignment]


MEMORY_VERSION = 0
CHANGES_VERSION = 0
MAX_NEW_CONFIDENCE = 0.70
FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "yahoo.com", "icloud.com", "me.com", "aol.com", "proton.me",
    "protonmail.com", "hey.com", "fastmail.com",
}
GENERIC_DOMAIN_LABELS = {"mail", "email", "smtp", "imap", "calendar", "drive"}
DECISION_WORDS = {"decision", "decided", "approved", "rejected", "agreed", "confirmed"}
QUESTION_WORDS = {"open question", "question", "unclear", "tbd", "to be confirmed", "unknown"}


class ConcurrencyError(Exception):
    """Raised when optimistic version checks fail."""


def _get_default_project_root_fallback() -> Path:
    """Default project root for fallback paths (env-configurable)."""
    if config_loader is not None:
        try:
            return config_loader.get_default_project_root("default")  # type: ignore[attr-defined]
        except Exception:
            pass
    env = os.getenv("CHIEF_OF_STAFF_HERMES_HOME") or os.getenv("HERMES_HOME")
    home = Path(env).expanduser() if env else Path.home() / ".hermes"
    return home / "projects" / "default"


def _project_root(config: object) -> Path:
    """Get project root from config, env, or default fallback.

    Keep this pattern aligned with the other shared scripts:
    config["paths"]["project_root"] → CHIEF_OF_STAFF_PROJECT_ROOT → fallback.
    """
    root: object | None = None
    if isinstance(config, dict):
        paths = config.get("paths", {})
        if isinstance(paths, dict):
            root = paths.get("project_root")
    if not root:
        root = os.getenv("CHIEF_OF_STAFF_PROJECT_ROOT", str(_get_default_project_root_fallback()))
    return Path(str(root)).expanduser()


def _knowledge_dir(config: object) -> Path:
    return _project_root(config) / ".knowledge"


def _memory_path(config: object) -> Path:
    return _knowledge_dir(config) / "memory.json"


def _changes_path(config: object) -> Path:
    return _knowledge_dir(config) / "memory_changes.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _to_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _list_values(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_since(value: str | None) -> datetime:
    """Parse a relative since value such as 24h, 7d, or 30m."""
    text = (value or "24h").strip().lower()
    match = re.fullmatch(r"(\d+)([mhd])", text)
    if not match:
        return datetime.now(timezone.utc) - timedelta(hours=24)
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "m":
        delta = timedelta(minutes=amount)
    elif unit == "d":
        delta = timedelta(days=amount)
    else:
        delta = timedelta(hours=amount)
    return datetime.now(timezone.utc) - delta


def _event_time(item: dict[str, object]) -> datetime | None:
    for key in ("created_at", "received_at", "classified_at", "updated_at", "timestamp"):
        dt = _parse_datetime(item.get(key))
        if dt is not None:
            return dt
    return None


def _is_since(item: dict[str, object], cutoff: datetime) -> bool:
    dt = _event_time(item)
    return dt is None or dt >= cutoff


def _load_json(path: Path, default: dict[str, object]) -> dict[str, object]:
    try:
        if not path.exists():
            return dict(default)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return dict(default)
        return data
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return dict(default)


def _load_memory(config: object) -> dict[str, object]:
    data = _load_json(_memory_path(config), {"records": {}, "_version": MEMORY_VERSION})
    records = data.get("records")
    if not isinstance(records, dict):
        data["records"] = {}
    if not isinstance(data.get("_version"), int):
        data["_version"] = MEMORY_VERSION
    return data


def _load_changes(config: object) -> dict[str, object]:
    data = _load_json(_changes_path(config), {"changes": [], "_version": CHANGES_VERSION})
    changes = data.get("changes")
    if not isinstance(changes, list):
        data["changes"] = []
    if not isinstance(data.get("_version"), int):
        data["_version"] = CHANGES_VERSION
    return data


def _atomic_save(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def _save_memory(config: object, data: dict[str, object], expected_version: int | None = None) -> int:
    if expected_version is not None:
        current = _load_memory(config)
        found = _to_int(current.get("_version", 0))
        if found != expected_version:
            raise ConcurrencyError(
                f"Memory store changed since load (expected v{expected_version}, found v{found})."
            )
    new_version = _to_int(data.get("_version", 0)) + 1
    data["_version"] = new_version
    _atomic_save(_memory_path(config), data)
    return new_version


def _save_changes(config: object, data: dict[str, object], expected_version: int | None = None) -> int:
    if expected_version is not None:
        current = _load_changes(config)
        found = _to_int(current.get("_version", 0))
        if found != expected_version:
            raise ConcurrencyError(
                f"Memory change log changed since load (expected v{expected_version}, found v{found})."
            )
    new_version = _to_int(data.get("_version", 0)) + 1
    data["_version"] = new_version
    _atomic_save(_changes_path(config), data)
    return new_version


def _items_from_store(data: object, key: str = "items") -> list[dict[str, object]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    container = data.get(key)
    if isinstance(container, dict):
        return [item for item in container.values() if isinstance(item, dict)]
    if isinstance(container, list):
        return [item for item in container if isinstance(item, dict)]
    if key not in data:
        return [item for item in data.values() if isinstance(item, dict)]
    return []


def _stringify(value: object, limit: int = 6000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text[:limit]


def _preferred_text(value: object, limit: int = 4000) -> str:
    """Extract human-facing text fields before falling back to raw JSON."""
    wanted = {"summary", "subject", "title", "reason", "snippet", "body", "text", "notes", "description"}
    parts: list[str] = []

    def walk(obj: object) -> None:
        if len(" ".join(parts)) >= limit:
            return
        if isinstance(obj, dict):
            for key, item in obj.items():
                if str(key).lower() in wanted and isinstance(item, (str, int, float)):
                    parts.append(str(item))
                elif isinstance(item, (dict, list)):
                    walk(item)
        elif isinstance(obj, list):
            for item in obj[:20]:
                walk(item)

    walk(value)
    text = re.sub(r"\s+", " ", " ".join(parts)).strip()
    return text[:limit]


def _clean_name(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n<>.,;:()[]{}'\"")
    return value[:120]


def _normalize(value: str) -> str:
    text = value.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _title_from_email(address: str) -> str:
    local = address.split("@", 1)[0]
    local = re.sub(r"[._+\-]+", " ", local)
    local = re.sub(r"\d+", "", local).strip()
    if not local:
        return address.lower()
    return " ".join(part.capitalize() for part in local.split())


def _domain_from_email(address: str) -> str:
    if "@" not in address:
        return ""
    domain = address.rsplit("@", 1)[1].lower().strip(" .>")
    return domain


def _org_name_from_domain(domain: str) -> str:
    domain = domain.lower().strip(" .")
    parts = [p for p in domain.split(".") if p]
    if len(parts) < 2:
        return ""
    label = parts[-2]
    if label in GENERIC_DOMAIN_LABELS and len(parts) >= 3:
        label = parts[-3]
    label = re.sub(r"[-_]+", " ", label)
    label = re.sub(r"\d+", "", label).strip()
    if len(label) < 2:
        return ""
    return " ".join(part.upper() if len(part) <= 3 else part.capitalize() for part in label.split())


def _source_id(prefix: str, item: dict[str, object]) -> str:
    for key in ("id", "event_id", "message_id", "source_id", "thread_id", "action_id"):
        value = item.get(key)
        if value:
            return str(value)
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _record_key(record_type: str, name: str) -> str:
    return f"{record_type}:{_normalize(name)}"


def _next_memory_id(records: dict[str, object]) -> str:
    max_num = 0
    for rid in records:
        match = re.fullmatch(r"mem_(\d+)", str(rid))
        if match:
            max_num = max(max_num, int(match.group(1)))
    return f"mem_{max_num + 1:03d}"


def _change_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"memchg_{stamp}_{uuid.uuid4().hex[:6]}"


def _make_change(
    change_type: str,
    target: str,
    summary: str,
    source_ids: list[str] | None = None,
    risk: str = "low",
    reversible: bool = True,
    before: object | None = None,
    after: object | None = None,
) -> dict[str, object]:
    change: dict[str, object] = {
        "id": _change_id(),
        "timestamp": _now(),
        "mode": "autonomous",
        "change_type": change_type,
        "target": target,
        "summary": summary,
        "source_ids": source_ids or [],
        "risk": risk,
        "reversible": reversible,
    }
    # Extra rollback metadata. The public schema remains present; these fields
    # make the required rollback operation possible without external state.
    if before is not None:
        change["before"] = before
    if after is not None:
        change["after"] = after
    return change


def _append_changes(log_data: dict[str, object], entries: list[dict[str, object]]) -> None:
    changes = log_data.get("changes")
    if not isinstance(changes, list):
        changes = []
        log_data["changes"] = changes
    changes.extend(entries)


def _record_summary(record_type: str, name: str, source_label: str, text: str) -> str:
    clean_text = re.sub(r"\s+", " ", text).strip()
    if len(clean_text) > 180:
        clean_text = f"{clean_text[:177]}..."
    if clean_text:
        return f"{record_type.replace('_', ' ').title()} observed from {source_label}: {clean_text}"
    return f"{record_type.replace('_', ' ').title()} observed from {source_label}: {name}"


def _candidate(
    record_type: str,
    name: str,
    summary: str,
    source_ids: list[str],
    confidence: float,
    status: str = "observed",
    aliases: list[str] | None = None,
) -> dict[str, object] | None:
    clean = _clean_name(name)
    if len(clean) < 2:
        return None
    safe_status = status if status in {"draft", "observed"} else "draft"
    return {
        "type": record_type,
        "name": clean,
        "aliases": aliases or [],
        "summary": summary,
        "status": safe_status,
        "confidence": min(MAX_NEW_CONFIDENCE, max(0.0, _to_float(confidence))),
        "source_ids": [sid for sid in source_ids if sid],
    }


def _extract_email_candidates(item: dict[str, object], source_label: str) -> list[dict[str, object]]:
    text = _stringify(item)
    summary_text = _preferred_text(item) or text
    sid = _source_id(source_label, item)
    candidates: list[dict[str, object]] = []
    seen_emails: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text):
        email = raw.lower().strip(" .,;:<>[]()\"'")
        if email in seen_emails:
            continue
        seen_emails.add(email)
        display_name = _title_from_email(email)
        sender_match = re.search(r"([A-Z][A-Za-z .'-]{2,80})\s*<\s*" + re.escape(email) + r"\s*>", text)
        aliases = [email]
        if sender_match:
            display_name = _clean_name(sender_match.group(1))
            display_name = _clean_name(re.sub(r"(?i)^.*\b(?:from|to|cc|by)\s+", "", display_name))
            aliases.append(_title_from_email(email))
        candidates.append(_candidate(
            "person",
            display_name,
            _record_summary("person", display_name, source_label, summary_text),
            [sid, email],
            0.65,
            aliases=aliases,
        ) or {})
        domain = _domain_from_email(email)
        if domain and domain not in FREE_EMAIL_DOMAINS:
            org_name = _org_name_from_domain(domain)
            if org_name:
                candidates.append(_candidate(
                    "organisation",
                    org_name,
                    f"Organisation inferred from email domain {domain}",
                    [sid, domain],
                    0.55,
                    aliases=[domain],
                ) or {})
    return [c for c in candidates if c]


def _extract_project_candidates(item: dict[str, object], source_label: str) -> list[dict[str, object]]:
    text = _preferred_text(item) or _stringify(item)
    sid = _source_id(source_label, item)
    candidates: list[dict[str, object]] = []
    patterns = [
        r"\b([A-Z][A-Za-z0-9& /'\-]{2,80}\s+(?:Project|Engagement|Deal|Mandate|Proposal|SOW|NDA|Contract))\b",
        r"\b((?:Project|Engagement|Deal|Mandate|Proposal|SOW|NDA|Contract)[:\- ]+[A-Z][A-Za-z0-9& /'\-]{2,80})",
        r"\b([A-Z]{2,}\s+[A-Z]{2,}(?:\s+[A-Z][A-Za-z0-9&'\-]+){0,4})\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            name = _clean_name(match.group(1))
            if len(name) < 4 or "@" in name:
                continue
            if _normalize(name) in {"chief of staff", "gmail send", "gmail draft"}:
                continue
            candidates.append(_candidate(
                "project",
                name,
                _record_summary("project", name, source_label, text),
                [sid],
                0.50,
                status="draft",
            ) or {})
    return [c for c in candidates if c]


def _extract_decision_question_candidates(item: dict[str, object], source_label: str) -> list[dict[str, object]]:
    text = _preferred_text(item) or _stringify(item, limit=3000)
    sid = _source_id(source_label, item)
    candidates: list[dict[str, object]] = []
    sentences = re.split(r"(?<=[.!?])\s+|\\n+", text)
    for sentence in sentences[:40]:
        clean = _clean_name(sentence)
        if len(clean) < 12:
            continue
        lower = clean.lower()
        if any(word in lower for word in DECISION_WORDS):
            name = clean[:90]
            candidates.append(_candidate(
                "decision",
                name,
                f"Decision-like statement observed from {source_label}: {clean[:220]}",
                [sid],
                0.45,
                status="draft",
            ) or {})
        elif "?" in clean or any(word in lower for word in QUESTION_WORDS):
            name = clean[:90]
            candidates.append(_candidate(
                "open_question",
                name,
                f"Open-question-like statement observed from {source_label}: {clean[:220]}",
                [sid],
                0.45,
                status="draft",
            ) or {})
    return [c for c in candidates if c]


def _extract_candidates_from_item(item: dict[str, object], source_label: str) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    candidates.extend(_extract_email_candidates(item, source_label))
    candidates.extend(_extract_project_candidates(item, source_label))
    candidates.extend(_extract_decision_question_candidates(item, source_label))
    return candidates


def _collect_events(config: object, cutoff: datetime, limit: int = 200) -> list[dict[str, object]]:
    if event_store is None:
        return []
    try:
        events = event_store.list_events(config, limit=limit)  # type: ignore[attr-defined]
    except Exception:
        return []
    return [dict(event) for event in events if isinstance(event, dict) and _is_since(event, cutoff)]


def _collect_email_classifications(config: object, cutoff: datetime) -> list[dict[str, object]]:
    root = _project_root(config)
    paths = [
        root / ".email_organisation_classifications.json",
        root / ".email_classifications.json",
    ]
    items: list[dict[str, object]] = []
    for path in paths:
        data = _load_json(path, {"items": {}, "_version": 0})
        items.extend(_items_from_store(data, key="items"))
    return [item for item in items if _is_since(item, cutoff)]


def _collect_suggestions(config: object, cutoff: datetime, limit: int = 200) -> list[dict[str, object]]:
    if suggested_actions is None:
        return []
    try:
        suggestions = suggested_actions.list_suggestions(config, limit=limit)  # type: ignore[attr-defined]
    except Exception:
        return []
    return [dict(item) for item in suggestions if isinstance(item, dict) and _is_since(item, cutoff)]


def _collect_pending_actions(config: object, cutoff: datetime) -> list[dict[str, object]]:
    if pending_actions is None:
        return []
    try:
        actions = pending_actions.list_pending_actions(config, state=None)  # type: ignore[attr-defined]
    except TypeError:
        try:
            actions = pending_actions.list_pending_actions(config)  # type: ignore[attr-defined]
        except Exception:
            return []
    except Exception:
        return []
    return [dict(item) for item in actions if isinstance(item, dict) and _is_since(item, cutoff)]


def collect_candidates(config: object, since: str | None = None) -> dict[str, object]:
    """Collect extraction candidates from local READ-ONLY sources."""
    cutoff = _parse_since(since)
    sources = {
        "event": _collect_events(config, cutoff),
        "email_classification": _collect_email_classifications(config, cutoff),
        "suggestion": _collect_suggestions(config, cutoff),
        "pending_action": _collect_pending_actions(config, cutoff),
    }
    candidates: list[dict[str, object]] = []
    for label, items in sources.items():
        for item in items:
            candidates.extend(_extract_candidates_from_item(item, label))
    # Deduplicate candidates by type+name+source IDs in memory before applying.
    deduped: dict[str, dict[str, object]] = {}
    for cand in candidates:
        key = _record_key(str(cand.get("type", "")), str(cand.get("name", "")))
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = cand
            continue
        existing_sources = _list_values(existing.get("source_ids"))
        cand_sources = _list_values(cand.get("source_ids"))
        merged = list(dict.fromkeys([str(x) for x in existing_sources + cand_sources if x]))
        existing["source_ids"] = merged
        if len(str(cand.get("summary", ""))) > len(str(existing.get("summary", ""))):
            existing["summary"] = cand.get("summary", "")
        try:
            existing["confidence"] = max(_to_float(existing.get("confidence", 0.0)), _to_float(cand.get("confidence", 0.0)))
        except (TypeError, ValueError):
            existing["confidence"] = existing.get("confidence", 0.0)
    return {"cutoff": cutoff.isoformat(), "sources": sources, "candidates": list(deduped.values())}


def _find_existing_record(records: dict[str, object], candidate: dict[str, object]) -> dict[str, object] | None:
    ctype = str(candidate.get("type", ""))
    cname = str(candidate.get("name", ""))
    ckey = _record_key(ctype, cname)
    for value in records.values():
        if not isinstance(value, dict):
            continue
        rtype = str(value.get("type", ""))
        if rtype != ctype:
            continue
        names = [str(value.get("name", ""))]
        aliases = value.get("aliases")
        if isinstance(aliases, list):
            names.extend(str(alias) for alias in aliases)
        for name in names:
            if _record_key(rtype, name) == ckey:
                return value
    return None


def _merge_record(existing: dict[str, object], candidate: dict[str, object]) -> dict[str, object]:
    merged = dict(existing)
    now = _now()
    before_sources = _list_values(existing.get("source_ids"))
    new_sources = _list_values(candidate.get("source_ids"))
    merged["source_ids"] = list(dict.fromkeys([str(x) for x in before_sources + new_sources if x]))

    before_aliases = _list_values(existing.get("aliases"))
    new_aliases = _list_values(candidate.get("aliases"))
    merged["aliases"] = list(dict.fromkeys([str(x) for x in before_aliases + new_aliases if x]))

    old_summary = str(existing.get("summary", ""))
    new_summary = str(candidate.get("summary", ""))
    if new_summary and new_summary not in old_summary:
        merged["summary"] = new_summary if not old_summary else f"{old_summary}\n{new_summary}"

    try:
        merged["confidence"] = min(0.85, max(_to_float(existing.get("confidence", 0.0)), _to_float(candidate.get("confidence", 0.0))))
    except (TypeError, ValueError):
        pass

    if not existing.get("operator_confirmed") and existing.get("status") not in {
        "observed", "draft", "inferred", "conflicting", "stale",
    }:
        merged["status"] = "observed"
    merged["updated_at"] = now
    merged["last_seen_at"] = now
    return merged


def apply_extraction(config: object, since: str | None = None, dry_run: bool = False) -> dict[str, object]:
    """Extract and apply structured memory from local state."""
    collected = collect_candidates(config, since)
    candidates = collected.get("candidates") if isinstance(collected.get("candidates"), list) else []
    memory = _load_memory(config)
    changes_log = _load_changes(config)
    memory_version = _to_int(memory.get("_version", 0))
    changes_version = _to_int(changes_log.get("_version", 0))
    records = memory.get("records")
    if not isinstance(records, dict):
        records = {}
        memory["records"] = records

    proposed_changes: list[dict[str, object]] = []
    created = 0
    updated = 0
    seen_candidate_keys: set[str] = set()

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        ctype = str(candidate.get("type", ""))
        cname = str(candidate.get("name", ""))
        if not ctype or not cname:
            continue
        key = _record_key(ctype, cname)
        if key in seen_candidate_keys:
            continue
        seen_candidate_keys.add(key)
        existing = _find_existing_record(records, candidate)
        if existing is None:
            rid = _next_memory_id(records)
            now = _now()
            record = {
                "id": rid,
                "type": ctype,
                "name": cname,
                "aliases": candidate.get("aliases") if isinstance(candidate.get("aliases"), list) else [],
                "summary": str(candidate.get("summary", "")),
                "status": str(candidate.get("status", "observed")) if str(candidate.get("status", "observed")) in {"draft", "observed"} else "draft",
                "confidence": min(MAX_NEW_CONFIDENCE, max(0.0, _to_float(candidate.get("confidence", 0.0)))),
                "source_ids": candidate.get("source_ids") if isinstance(candidate.get("source_ids"), list) else [],
                "created_at": now,
                "updated_at": now,
                "last_seen_at": now,
                "operator_confirmed": False,
            }
            change = _make_change(
                "memory_create",
                rid,
                f"Created {ctype} memory: {cname}",
                [str(x) for x in record.get("source_ids", []) if x],
                risk="low",
                reversible=True,
                before=None,
                after=record,
            )
            proposed_changes.append(change)
            created += 1
            if not dry_run:
                records[rid] = record
        else:
            before = dict(existing)
            merged = _merge_record(existing, candidate)
            if merged != before:
                rid = str(existing.get("id", ""))
                change = _make_change(
                    "memory_update",
                    rid,
                    f"Updated {ctype} memory: {existing.get('name', cname)}",
                    [str(x) for x in candidate.get("source_ids", []) if x],
                    risk="low",
                    reversible=True,
                    before=before,
                    after=merged,
                )
                proposed_changes.append(change)
                updated += 1
                if not dry_run:
                    records[rid] = merged

    if proposed_changes and not dry_run:
        _save_memory(config, memory, expected_version=memory_version)
        _append_changes(changes_log, proposed_changes)
        _save_changes(config, changes_log, expected_version=changes_version)

    return {
        "created": created,
        "updated": updated,
        "candidate_count": len(candidates),
        "change_count": len(proposed_changes),
        "changes": proposed_changes,
        "dry_run": dry_run,
    }


def _similar_names(left: str, right: str) -> bool:
    a = _normalize(left)
    b = _normalize(right)
    if not a or not b or a == b:
        return False
    if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
        return True
    aset = set(a.split())
    bset = set(b.split())
    if not aset or not bset:
        return False
    overlap = len(aset & bset) / max(1, min(len(aset), len(bset)))
    return overlap >= 0.75


def _existing_notice_targets(changes_log: dict[str, object], change_type: str) -> set[str]:
    changes = changes_log.get("changes")
    if not isinstance(changes, list):
        return set()
    return {
        str(change.get("target", ""))
        for change in changes
        if isinstance(change, dict) and change.get("change_type") == change_type
    }


def detect_duplicates(memory: dict[str, object], changes_log: dict[str, object]) -> list[dict[str, object]]:
    records_obj = memory.get("records")
    if not isinstance(records_obj, dict):
        return []
    records = [record for record in records_obj.values() if isinstance(record, dict)]
    existing_targets = _existing_notice_targets(changes_log, "duplicate_detected")
    notices: list[dict[str, object]] = []
    for i, left in enumerate(records):
        for right in records[i + 1:]:
            if left.get("type") != right.get("type"):
                continue
            lname = str(left.get("name", ""))
            rname = str(right.get("name", ""))
            if not _similar_names(lname, rname):
                continue
            target = ",".join(sorted([str(left.get("id", "")), str(right.get("id", ""))]))
            if target in existing_targets:
                continue
            notices.append(_make_change(
                "duplicate_detected",
                target,
                f"Possible duplicate {left.get('type')} records: {lname} / {rname}",
                [],
                risk="medium",
                reversible=False,
            ))
    return notices


def detect_conflicts(memory: dict[str, object], changes_log: dict[str, object]) -> list[dict[str, object]]:
    records_obj = memory.get("records")
    if not isinstance(records_obj, dict):
        return []
    groups: dict[str, list[dict[str, object]]] = {}
    for record in records_obj.values():
        if not isinstance(record, dict):
            continue
        key = _record_key(str(record.get("type", "")), str(record.get("name", "")))
        groups.setdefault(key, []).append(record)
        aliases = record.get("aliases")
        if isinstance(aliases, list):
            for alias in aliases:
                groups.setdefault(_record_key(str(record.get("type", "")), str(alias)), []).append(record)

    existing_targets = _existing_notice_targets(changes_log, "conflict_detected")
    notices: list[dict[str, object]] = []
    for _key, group in groups.items():
        unique: dict[str, dict[str, object]] = {str(item.get("id", "")): item for item in group if item.get("id")}
        if len(unique) < 2:
            continue
        summaries = {_normalize(str(item.get("summary", ""))) for item in unique.values() if item.get("summary")}
        statuses = {str(item.get("status", "")) for item in unique.values() if item.get("status")}
        if len(summaries) < 2 and len(statuses) < 2:
            continue
        target = ",".join(sorted(unique.keys()))
        if target in existing_targets:
            continue
        notices.append(_make_change(
            "conflict_detected",
            target,
            "Potential conflicting memory facts for same entity/alias",
            [],
            risk="high",
            reversible=False,
        ))
    return notices


def curate_memory(config: object, since: str | None = None, dry_run: bool = False) -> dict[str, object]:
    extract_result = apply_extraction(config, since=since, dry_run=dry_run)
    memory = _load_memory(config) if not dry_run else _memory_with_dry_run_changes(config, extract_result)
    changes_log = _load_changes(config)
    changes_version = _to_int(changes_log.get("_version", 0))

    notices = detect_duplicates(memory, changes_log) + detect_conflicts(memory, changes_log)
    if notices and not dry_run:
        _append_changes(changes_log, notices)
        _save_changes(config, changes_log, expected_version=changes_version)

    return {
        "extract": extract_result,
        "duplicates": [n for n in notices if n.get("change_type") == "duplicate_detected"],
        "conflicts": [n for n in notices if n.get("change_type") == "conflict_detected"],
        "change_count": _to_int(extract_result.get("change_count", 0)) + len(notices),
        "dry_run": dry_run,
    }


def _memory_with_dry_run_changes(config: object, extract_result: dict[str, object]) -> dict[str, object]:
    memory = _load_memory(config)
    records = memory.get("records")
    if not isinstance(records, dict):
        records = {}
        memory["records"] = records
    changes = extract_result.get("changes")
    if not isinstance(changes, list):
        return memory
    for change in changes:
        if not isinstance(change, dict):
            continue
        after = change.get("after")
        target = str(change.get("target", ""))
        if isinstance(after, dict) and target:
            records[target] = after
    return memory


def recent_changes(config: object, limit: int = 20, since: str | None = None) -> list[dict[str, object]]:
    data = _load_changes(config)
    changes_obj = data.get("changes")
    if not isinstance(changes_obj, list):
        return []
    changes = [change for change in changes_obj if isinstance(change, dict)]
    if since:
        cutoff = _parse_since(since)
        changes = [change for change in changes if _parse_datetime(change.get("timestamp")) is None or _parse_datetime(change.get("timestamp")) >= cutoff]
    changes = sorted(changes, key=lambda c: str(c.get("timestamp", "")), reverse=True)
    return changes[:max(0, _to_int(limit))]


def rollback_change(config: object, change_id: str, dry_run: bool = False) -> dict[str, object]:
    memory = _load_memory(config)
    changes_log = _load_changes(config)
    memory_version = _to_int(memory.get("_version", 0))
    changes_version = _to_int(changes_log.get("_version", 0))
    changes = changes_log.get("changes")
    if not isinstance(changes, list):
        changes = []
    change = None
    for item in changes:
        if isinstance(item, dict) and item.get("id") == change_id:
            change = item
            break
    if change is None:
        return {"success": False, "error": f"Change not found: {change_id}", "dry_run": dry_run}
    if not change.get("reversible"):
        return {"success": False, "error": f"Change is not reversible: {change_id}", "change": change, "dry_run": dry_run}

    target = str(change.get("target", ""))
    before = change.get("before") if "before" in change else None
    after = change.get("after") if "after" in change else None
    records = memory.get("records")
    if not isinstance(records, dict):
        records = {}
        memory["records"] = records

    plan = "no-op"
    if isinstance(before, dict):
        plan = f"restore previous record values for {target}"
    elif before is None and isinstance(after, dict):
        plan = f"delete created record {target}"
    else:
        return {"success": False, "error": "No rollback metadata on change", "change": change, "dry_run": dry_run}

    if dry_run:
        return {"success": True, "dry_run": True, "change": change, "plan": plan}

    current = records.get(target)
    if isinstance(before, dict):
        records[target] = before
        rollback_after: object = before
    else:
        records.pop(target, None)
        rollback_after = None

    rollback_entry = _make_change(
        "memory_update",
        target,
        f"Rollback of {change_id}: {plan}",
        [str(x) for x in change.get("source_ids", [])] if isinstance(change.get("source_ids"), list) else [],
        risk="medium",
        reversible=True,
        before=current,
        after=rollback_after,
    )
    _save_memory(config, memory, expected_version=memory_version)
    _append_changes(changes_log, [rollback_entry])
    _save_changes(config, changes_log, expected_version=changes_version)
    return {"success": True, "dry_run": False, "rolled_back": change_id, "rollback_change": rollback_entry, "plan": plan}


def _load_runtime_config(config_path: str | None) -> object:
    if config_loader is None:
        return {}
    # Avoid noisy missing-config errors for memory maintenance commands. The
    # project-root fallback is valid and intentionally supported.
    if not config_path and not os.getenv("CHIEF_OF_STAFF_CONFIG"):
        default_config = _SCRIPT_DIR.parent / "config" / "company.yaml"
        if not default_config.exists():
            return {}
    try:
        loaded = config_loader.load_config(config_path)  # type: ignore[attr-defined]
        return loaded if loaded is not None else {}
    except Exception:
        return {}


def _print_extract_summary(result: dict[str, object]) -> None:
    prefix = "DRY RUN: " if result.get("dry_run") else ""
    print(
        f"{prefix}memory extract: {result.get('created', 0)} created, "
        f"{result.get('updated', 0)} updated, {result.get('candidate_count', 0)} candidates"
    )


def _print_curate_summary(result: dict[str, object]) -> None:
    extract = result.get("extract") if isinstance(result.get("extract"), dict) else {}
    prefix = "DRY RUN: " if result.get("dry_run") else ""
    print(
        f"{prefix}memory curate: {extract.get('created', 0)} created, "
        f"{extract.get('updated', 0)} updated, "
        f"{len(result.get('duplicates', [])) if isinstance(result.get('duplicates'), list) else 0} duplicates, "
        f"{len(result.get('conflicts', [])) if isinstance(result.get('conflicts'), list) else 0} conflicts"
    )


def _print_changes(changes: list[dict[str, object]]) -> None:
    if not changes:
        print("No memory changes found.")
        return
    for change in changes:
        print(f"{change.get('id')} | {change.get('timestamp')} | {change.get('change_type')} | {change.get('target')}")
        print(f"  {change.get('summary', '')}")
        sources = change.get("source_ids")
        if isinstance(sources, list) and sources:
            print(f"  sources: {', '.join(str(s) for s in sources[:8])}")


def _print_report(config: object, since: str | None) -> None:
    changes = recent_changes(config, limit=200, since=since)
    created = [c for c in changes if c.get("change_type") == "memory_create"]
    updated = [c for c in changes if c.get("change_type") == "memory_update"]
    duplicates = [c for c in changes if c.get("change_type") == "duplicate_detected"]
    conflicts = [c for c in changes if c.get("change_type") == "conflict_detected"]
    print(f"Memory report since {since or '24h'}")
    print(f"Changes: {len(changes)} total | {len(created)} new | {len(updated)} updated | {len(duplicates)} duplicates | {len(conflicts)} conflicts")
    if created:
        print("\nNew entities:")
        for change in created[:20]:
            print(f"- {change.get('target')}: {change.get('summary')}")
    if duplicates:
        print("\nPossible duplicates:")
        for change in duplicates[:20]:
            print(f"- {change.get('target')}: {change.get('summary')}")
    if conflicts:
        print("\nPotential conflicts:")
        for change in conflicts[:20]:
            print(f"- {change.get('target')}: {change.get('summary')}")
    if not changes:
        print("No recent memory changes recorded.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chief-of-Staff structured memory maintenance")
    parser.add_argument("--config", help="Path to company.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract", help="Extract structured memory from local state")
    extract.add_argument("--since", default="24h", help="Relative window (e.g. 24h, 7d, 30m)")
    extract.add_argument("--summary", action="store_true", help="Print compact count summary")
    extract.add_argument("--dry-run", action="store_true", help="Report proposed changes without writing")

    curate = sub.add_parser("curate", help="Extract, deduplicate, and detect conflicts")
    curate.add_argument("--since", default="24h", help="Relative window (e.g. 24h, 7d, 30m)")
    curate.add_argument("--summary", action="store_true", help="Print compact count summary")
    curate.add_argument("--dry-run", action="store_true", help="Report proposed changes without writing")

    report = sub.add_parser("report", help="Print human-readable memory change report")
    report.add_argument("--since", default="24h", help="Relative window (e.g. 24h, 7d, 30m)")

    changes = sub.add_parser("changes", help="Print recent change log entries")
    changes.add_argument("--limit", type=int, default=20, help="Number of changes to print")

    rollback = sub.add_parser("rollback", help="Rollback a reversible memory change")
    rollback.add_argument("--change-id", required=True, help="Change ID to rollback")
    rollback.add_argument("--dry-run", action="store_true", help="Report rollback plan without writing")
    return parser


def _main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    config = _load_runtime_config(args.config)

    try:
        if args.command == "extract":
            result = apply_extraction(config, since=args.since, dry_run=args.dry_run)
            if args.summary:
                _print_extract_summary(result)
            else:
                _print_extract_summary(result)
                _print_changes(result.get("changes", []) if isinstance(result.get("changes"), list) else [])
            return 0
        if args.command == "curate":
            result = curate_memory(config, since=args.since, dry_run=args.dry_run)
            if args.summary:
                _print_curate_summary(result)
            else:
                _print_curate_summary(result)
                extract = result.get("extract") if isinstance(result.get("extract"), dict) else {}
                _print_changes(extract.get("changes", []) if isinstance(extract.get("changes"), list) else [])
                notices: list[dict[str, object]] = []
                if isinstance(result.get("duplicates"), list):
                    notices.extend(result.get("duplicates", []))  # type: ignore[arg-type]
                if isinstance(result.get("conflicts"), list):
                    notices.extend(result.get("conflicts", []))  # type: ignore[arg-type]
                _print_changes(notices)
            return 0
        if args.command == "report":
            _print_report(config, args.since)
            return 0
        if args.command == "changes":
            _print_changes(recent_changes(config, limit=args.limit))
            return 0
        if args.command == "rollback":
            result = rollback_change(config, args.change_id, dry_run=args.dry_run)
            if result.get("success"):
                mode = "DRY RUN" if result.get("dry_run") else "ROLLED BACK"
                print(f"{mode}: {result.get('plan')}")
                if result.get("dry_run"):
                    change = result.get("change")
                    if isinstance(change, dict):
                        _print_changes([change])
                return 0
            print(f"Rollback failed: {result.get('error')}", file=sys.stderr)
            return 1
    except ConcurrencyError as exc:
        print(f"Concurrency error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"memory.py error: {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
