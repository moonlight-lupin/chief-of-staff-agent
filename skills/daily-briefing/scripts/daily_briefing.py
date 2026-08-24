#!/usr/bin/env python3
"""Daily briefing collector/renderer for the Chief-of-Staff plugin."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable, Mapping

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    print(f"PyYAML is required for daily_briefing.py: {exc}", file=sys.stderr)
    raise SystemExit(2)

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

try:
    import runtime_log  # type: ignore
except Exception:  # pragma: no cover - operational logging is optional
    runtime_log = None  # type: ignore

try:
    from config_loader import get_project_root, load_config  # type: ignore
    from run_log import last_run, record_run  # type: ignore
except Exception as exc:  # pragma: no cover
    print(
        f"Chief-of-Staff bootstrap incomplete: cannot import shared scripts from {SHARED_SCRIPTS}: {exc}. "
        "Run the plugin bootstrap/foundation setup first.",
        file=sys.stderr,
    )
    raise SystemExit(2)

SOURCE_NAMES = ["gmail", "calendar", "deadlines", "pipeline", "todos", "invoices", "email_org"]


def today() -> str:
    return date.today().isoformat()


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing source file: {path}")
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a top-level mapping")
    return loaded


def sibling_or_shared(config: Any, filename: str) -> Path:
    candidates = []
    source = getattr(config, "source_path", None)
    if source:
        candidates.append(Path(source).parent / filename)
    candidates.append(PLUGIN_ROOT / "shared" / "config" / filename)
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(f"{filename} not found beside company.yaml or shared/config/")


def google_api_script() -> Path:
    candidates = [
        PLUGIN_ROOT / "shared" / "scripts" / "google_api.py",
        Path.home() / ".hermes" / "skills" / "productivity" / "google-workspace" / "scripts" / "google_api.py",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError("google_api.py not found; install/configure google-workspace skill before Gmail/Calendar briefing collection")


def ensure_google_config(config: Any) -> None:
    google = config.get("google", {}) if isinstance(config, dict) else {}
    service_account = str(google.get("service_account_path", "") or "")
    if service_account:
        path = Path(service_account).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Google credentials not configured: {path}")


def ensure_workspace_config(config: Any) -> None:
    """Provider-aware config check — only validates Google config when provider is google_api."""
    integrations = config.get("integrations", {}) if isinstance(config, dict) else {}
    workspace = integrations.get("workspace", {}) if isinstance(integrations, dict) else {}
    provider = workspace.get("provider", "google_api")
    if provider != "google_api":
        return
    ensure_google_config(config)


def _get_workspace_client(config: Any):
    """Get a WorkspaceClient from config. Falls back to google_api if no integrations section."""
    _script_dir = PLUGIN_ROOT / "shared" / "scripts"
    if str(_script_dir) not in sys.path:
        sys.path.insert(0, str(_script_dir))
    from workspace_client import get_workspace_client
    return get_workspace_client(config)


# Lookback for "has the operator already replied in this thread?"
_REPLY_LOOKBACK_DAYS = 14
_REPLY_SENT_MAX = 50


def _operator_email(config: Any) -> str:
    """Resolve the operator mailbox used for reply-awareness checks."""
    if not isinstance(config, dict):
        return ""
    google_cfg = config.get("google", {}) if isinstance(config.get("google"), dict) else {}
    delegate = str(google_cfg.get("delegate_email") or "").strip()
    if delegate:
        return delegate.lower()
    user_cfg = config.get("user", {}) if isinstance(config.get("user"), dict) else {}
    email = str(user_cfg.get("email") or "").strip()
    return email.lower()


def _positive_int(value: Any, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _reply_scan_limits(config: Any) -> tuple[int, int]:
    """Resolve the sent-mail scan window used for reply awareness.

    Defaults to the module constants (14 days / 50 messages). An optional
    ``briefing`` config section widens the window so older replies still
    suppress::

        briefing:
          reply_lookback_days: 30
          reply_sent_max: 200
    """
    lookback = _REPLY_LOOKBACK_DAYS
    sent_max = _REPLY_SENT_MAX
    if isinstance(config, dict):
        briefing = config.get("briefing")
        if isinstance(briefing, dict):
            lookback = _positive_int(briefing.get("reply_lookback_days"), lookback)
            sent_max = _positive_int(briefing.get("reply_sent_max"), sent_max)
    return lookback, sent_max


def _message_field(msg: Mapping[str, Any] | Any, *keys: str) -> Any:
    if not isinstance(msg, Mapping):
        return None
    for key in keys:
        val = msg.get(key)
        if val is not None and val != "":
            return val
    return None


def _message_thread_id(msg: Any) -> str:
    val = _message_field(msg, "thread_id", "threadId", "conversationId", "conversation_id")
    return str(val).strip() if val is not None else ""


def _message_sender(msg: Any) -> str:
    val = _message_field(msg, "sender", "from", "from_email", "fromEmail")
    if isinstance(val, Mapping):
        addr = val.get("email") or val.get("address") or val.get("emailAddress")
        if isinstance(addr, Mapping):
            addr = addr.get("address") or addr.get("email")
        val = addr
    return str(val or "").strip().lower()


def _parse_message_date(msg: Any) -> datetime | None:
    # ``messageTimestamp`` is what Composio Gmail actually returns (ISO 8601 with
    # a trailing Z); the Graph/REST spellings are kept for the other providers.
    # A ms-epoch string/number is also accepted. Every returned datetime is made
    # timezone-AWARE (naive → UTC) so aware/naive values never get compared and
    # raise ``TypeError`` in _message_already_replied.
    raw = _message_field(
        msg,
        "messageTimestamp", "date", "receivedDateTime", "sentDateTime",
        "internalDate", "timestamp",
    )
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        # Gmail sometimes returns ms epoch
        try:
            ts = float(raw)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    # Pure numeric string → epoch (seconds or ms).
    if text.isdigit():
        try:
            ts = float(text)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_from_operator(msg: Any, operator: str) -> bool:
    if not operator:
        return False
    sender = _message_sender(msg)
    if not sender:
        return False
    if sender == operator:
        return True
    # Compare the bare address parsed out of "Name <addr>" / "addr" forms.
    # A loose ``operator in sender`` substring test misfires — e.g. operator
    # "jo@x.com" is a substring of "jojo@x.com", and the address can also show
    # up inside an unrelated display name.
    _, addr = parseaddr(sender)
    return bool(addr) and addr == operator


def _is_sent_side_query(name: str, query: str, operator: str) -> bool:
    """True for queries that list the operator's own sent mail (not inbound outstanding)."""
    n = (name or "").strip().lower()
    q = (query or "").strip().lower()
    if n in {"sent_followup", "invoices_sent_followup"}:
        return True
    if "in:sent" in q or "label:sent" in q:
        return True
    if operator and f"from:{operator}" in q:
        return True
    return False


def _reply_index_from_messages(
    messages: list[Any],
    operator: str,
) -> dict[str, datetime]:
    """Map thread_id -> latest operator-authored message datetime."""
    index: dict[str, datetime] = {}
    if not operator:
        return index
    for msg in messages:
        if not _is_from_operator(msg, operator):
            continue
        thread_id = _message_thread_id(msg)
        if not thread_id:
            continue
        when = _parse_message_date(msg)
        if when is None:
            continue
        prev = index.get(thread_id)
        if prev is None or when > prev:
            index[thread_id] = when
    return index


def _build_operator_reply_index(
    client: Any,
    operator: str,
    lookback_days: int = _REPLY_LOOKBACK_DAYS,
    sent_max: int = _REPLY_SENT_MAX,
) -> dict[str, datetime]:
    """Fetch recent sent mail and index by thread for reply detection."""
    if not operator or client is None:
        return {}
    query = f"in:sent newer_than:{lookback_days}d"
    try:
        sent = client.mail_search(query, max_results=sent_max)
    except Exception:
        # Fail open — briefing still renders; we just can't suppress.
        return {}
    if not isinstance(sent, list):
        return {}
    return _reply_index_from_messages(sent, operator)


def _message_already_replied(
    msg: Any,
    reply_index: dict[str, datetime],
    operator: str,
) -> bool:
    """True when the operator sent a later message in the same thread."""
    if not reply_index or not operator:
        return False
    # Operator-authored mail is not an inbound outstanding item.
    if _is_from_operator(msg, operator):
        return True
    thread_id = _message_thread_id(msg)
    if not thread_id:
        return False
    replied_at = reply_index.get(thread_id)
    if replied_at is None:
        return False
    inbound_at = _parse_message_date(msg)
    if inbound_at is None:
        # Thread match without a parseable date — treat as replied (user
        # already acted in-thread; better than listing a closed loop).
        return True
    return replied_at > inbound_at


def _filter_replied_messages(
    messages: list[Any],
    reply_index: dict[str, datetime],
    operator: str,
) -> tuple[list[Any], int]:
    """Drop inbound messages the operator has already replied to in-thread."""
    if not isinstance(messages, list) or not messages:
        return [], 0
    if not reply_index and not operator:
        return list(messages), 0
    kept: list[Any] = []
    suppressed = 0
    for msg in messages:
        if _message_already_replied(msg, reply_index, operator):
            suppressed += 1
            continue
        kept.append(msg)
    return kept, suppressed


def collect_gmail(config: Any, project_root: Path) -> list[dict[str, Any]] | dict[str, Any]:
    ensure_workspace_config(config)
    queries_file = sibling_or_shared(config, "queries.yaml")
    queries_data = load_yaml(queries_file)
    queries = queries_data.get("queries", []) or []
    # Accept both list format and mapping format for backward compatibility
    if isinstance(queries, dict):
        # Mapping format: {name: {query: ..., max_results: ..., ...}}
        queries = [
            {"name": k, "query": v.get("query", ""), "max": v.get("max_results", v.get("max", 10)),
             "description": v.get("description", ""), "template": v.get("template", False)}
            for k, v in queries.items()
        ]
    if not isinstance(queries, list):
        raise ValueError("queries.yaml 'queries' must be a list or mapping")
    google_cfg = config.get("google", {}) if isinstance(config.get("google"), dict) else {}
    delegate = str(google_cfg.get("delegate_email", ""))
    operator = _operator_email(config) or delegate.strip().lower()
    import re as _re
    client = _get_workspace_client(config)
    items: list[dict[str, Any]] = []
    dropped_constraints: list[str] = []
    raw_results: list[tuple[dict[str, Any], str, list[Any]]] = []
    for query in queries:
        if not isinstance(query, dict):
            continue
        q = str(query.get("query", ""))
        # Replace known template variables
        q = q.replace("{delegate_email}", delegate)
        # Skip if any unresolved template variables remain
        if _re.search(r'\{[a-z_]+\}', q):
            continue
        result = client.mail_search(q, max_results=int(query.get("max", 10)))
        # Microsoft Composio Outlook may drop text-search terms; surface that
        # honesty so the Gmail section is not reported as a clean success.
        meta = getattr(client, "last_mail_search_meta", None) or {}
        if isinstance(meta, dict) and meta.get("degraded"):
            for constraint in meta.get("dropped_constraints") or []:
                if constraint not in dropped_constraints:
                    dropped_constraints.append(str(constraint))
        if not isinstance(result, list):
            result = []
        raw_results.append((query, q, result))

    # One sent-mail fetch → suppress inbound hits the operator already answered.
    lookback_days, sent_max = _reply_scan_limits(config)
    reply_index = _build_operator_reply_index(client, operator, lookback_days, sent_max)
    # Also learn from sent-side query results already in hand (no extra round-trip).
    for query, q, result in raw_results:
        name = str(query.get("name") or "")
        if _is_sent_side_query(name, q, operator):
            for thread_id, when in _reply_index_from_messages(result, operator).items():
                prev = reply_index.get(thread_id)
                if prev is None or when > prev:
                    reply_index[thread_id] = when

    total_suppressed = 0
    for query, q, result in raw_results:
        name = str(query.get("name") or "")
        entry: dict[str, Any] = {"name": name, "query": q, "result": result}
        if not _is_sent_side_query(name, q, operator):
            kept, suppressed = _filter_replied_messages(result, reply_index, operator)
            entry["result"] = kept
            if suppressed:
                entry["suppressed_replied"] = suppressed
                total_suppressed += suppressed
        items.append(entry)

    notes: list[str] = []
    if dropped_constraints:
        notes.append(
            "query degraded; dropped constraints: " + ", ".join(dropped_constraints)
        )
    if total_suppressed:
        notes.append(
            f"suppressed {total_suppressed} message(s) already replied in-thread"
        )
    if notes:
        status = "degraded" if dropped_constraints else "ok"
        out: dict[str, Any] = {
            "status": status,
            "items": items,
            "error": "; ".join(notes) if dropped_constraints else None,
            "suppressed_replied": total_suppressed,
            "note": "; ".join(notes),
        }
        if out["error"] is None:
            del out["error"]
        return out
    return items


def load_workspace_input(path: str) -> dict[str, Any]:
    """Load and validate an agent-fetched workspace envelope from PATH or stdin.

    ``path`` of ``"-"`` reads the envelope from stdin. Returns a normalized
    payload (defaults filled). Raises FileNotFoundError / ValueError / SchemaError
    on a missing file, malformed JSON, or a schema violation.
    """
    from schemas import normalize_workspace_payload  # SchemaError is a ValueError

    if path == "-":
        raw = sys.stdin.read()
    else:
        file_path = Path(path).expanduser()
        if not file_path.exists():
            raise FileNotFoundError(f"--input file not found: {file_path}")
        raw = file_path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--input is not valid JSON: {exc}")
    return normalize_workspace_payload(payload)


def _messages_to_gmail_items(
    messages: list[dict[str, Any]],
    *,
    operator: str = "",
) -> list[dict[str, Any]] | dict[str, Any]:
    """Wrap agent-provided messages into the gmail source item shape.

    When ``operator`` is set, inbound messages whose thread already contains a
    later message from the operator are suppressed (same rule as live collect).
    """
    msgs = list(messages) if isinstance(messages, list) else []
    reply_index = _reply_index_from_messages(msgs, operator)
    kept, suppressed = _filter_replied_messages(msgs, reply_index, operator)
    entry: dict[str, Any] = {
        "name": "agent-input",
        "query": "(agent-provided)",
        "result": kept,
    }
    if suppressed:
        entry["suppressed_replied"] = suppressed
        return {
            "status": "ok",
            "items": [entry],
            "suppressed_replied": suppressed,
            "note": f"suppressed {suppressed} message(s) already replied in-thread",
        }
    return [entry]


def collect_calendar(config: Any, project_root: Path) -> list[dict[str, Any]]:
    ensure_workspace_config(config)
    client = _get_workspace_client(config)
    start = date.today().isoformat()
    end = (date.today() + timedelta(days=2)).isoformat()
    loaded = client.calendar_list(start, end)
    return loaded if isinstance(loaded, list) else [loaded]


def collect_deadlines(config: Any, project_root: Path) -> list[dict[str, Any]]:
    candidates = [
        PLUGIN_ROOT / "skills" / "deadline-tracker" / "scripts" / "deadlines.py",
        PLUGIN_ROOT / "shared" / "scripts" / "deadlines.py",
    ]
    script = next((path for path in candidates if path.exists()), None)
    if script is None:
        raise FileNotFoundError("deadlines.py not found in deadline-tracker/scripts or shared/scripts")
    cfg_path = getattr(config, "source_path", None)
    cmd = [sys.executable, str(script), "--within", "30", "--json"]
    if cfg_path:
        cmd.extend(["--config", str(cfg_path)])
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=45)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"deadlines.py exited {proc.returncode}")
    loaded = json.loads(proc.stdout or "[]")
    return loaded if isinstance(loaded, list) else [loaded]


def parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def collect_pipeline(config: Any, project_root: Path) -> list[dict[str, Any]]:
    try:
        from state_db import load_store
        data = load_store("pipeline", config, validate=False)
    except Exception:
        data = {"deals": []}
    if not isinstance(data, dict):
        data = {"deals": []}
    threshold = int(config.get("stale_threshold_days") or 14)
    now = date.today()
    stale: list[dict[str, Any]] = []
    for deal in data.get("deals", []) or []:
        if not isinstance(deal, dict):
            continue
        last = parse_date(deal.get("last_activity"))
        if last is None:
            age = threshold + 1
        else:
            age = (now - last).days
        if age > threshold and str(deal.get("status", "active")) == "active":
            item = dict(deal)
            item["stale_days"] = age
            stale.append(item)
    return sorted(stale, key=lambda d: (-int(d.get("stale_days", 0)), str(d.get("client_name", ""))))


def collect_todos(config: Any, project_root: Path) -> list[dict[str, Any]]:
    try:
        from state_db import load_store
        data = load_store("todos", config, validate=False)
    except Exception:
        data = {"todos": []}
    if not isinstance(data, dict):
        data = {"todos": []}
    now = date.today()
    items: list[dict[str, Any]] = []
    for todo in data.get("todos", []) or []:
        if not isinstance(todo, dict) or todo.get("status") != "open":
            continue
        item = dict(todo)
        due = parse_date(item.get("due"))
        item["overdue"] = bool(due and due < now)
        item["days_until_due"] = (due - now).days if due else None
        items.append(item)
    priority = {"high": 0, "medium": 1, "low": 2}
    return sorted(items, key=lambda t: (priority.get(str(t.get("priority")), 99), t.get("due") or "9999-12-31"))


def dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def collect_invoices(config: Any, project_root: Path) -> list[dict[str, Any]]:
    try:
        from state_db import load_store
        data = load_store("invoices", config, validate=False)
    except Exception:
        data = {"invoices": []}
    if not isinstance(data, dict):
        data = {"invoices": []}
    now = date.today()
    overdue: list[dict[str, Any]] = []
    ar_totals: dict[str, Decimal] = {}
    for inv in data.get("invoices", []) or []:
        if not isinstance(inv, dict):
            continue
        status = str(inv.get("status"))
        if status in {"paid", "cancelled"}:
            continue
        currency = str(inv.get("currency") or "UNSPECIFIED").upper()
        if inv.get("direction") == "sent":
            ar_totals[currency] = ar_totals.get(currency, Decimal("0")) + dec(inv.get("amount"))
        due = parse_date(inv.get("due_date"))
        if due and due < now:
            overdue.append(dict(inv))
    return [
        {"kind": "ar_total", "currency": cur, "amount": str(amount.quantize(Decimal('0.01')))}
        for cur, amount in sorted(ar_totals.items())
    ] + [{"kind": "overdue", **item} for item in sorted(overdue, key=lambda i: (str(i.get("due_date")), str(i.get("id"))))]


def concise_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        # Tracebacks from subprocess stderr are noisy and environment-specific;
        # keep the actionable final line for stable run logs and fixtures.
        text = lines[-1]
    if len(text) > 500:
        text = text[:497].rstrip() + "..."
    return text


def wrap_source(name: str, collector: Callable[[Any, Path], list[dict[str, Any]]], config: Any, project_root: Path) -> dict[str, Any]:
    try:
        result = collector(config, project_root)
        # Collectors may return a rich envelope when a source is partially
        # honest but degraded (e.g. Outlook dropped text-search constraints).
        if isinstance(result, dict) and "items" in result and "status" in result:
            items = result.get("items") or []
            out: dict[str, Any] = {
                "status": result["status"],
                "hash": stable_hash(items),
                "items": items,
            }
            if result.get("error"):
                out["error"] = result["error"]
            if result.get("note"):
                out["note"] = result["note"]
            if result.get("suppressed_replied"):
                out["suppressed_replied"] = result["suppressed_replied"]
            return out
        items = result if isinstance(result, list) else [result]
        return {"status": "ok", "hash": stable_hash(items), "items": items}
    except Exception as exc:
        err = concise_error(exc)
        # Hard Composio failures (disconnected toolkit, unknown slug, rate
        # limits, auth errors, malformed responses) must not look like an
        # empty successful read — mark the section unavailable.
        status = "failed"
        try:
            from providers.composio_mcp_workspace import ComposioReadError  # type: ignore
            if isinstance(exc, ComposioReadError):
                status = "unavailable"
        except Exception:
            pass
        return {"status": status, "hash": stable_hash({"error": err}), "items": [], "error": err}


def render_source(title: str, icon: str, source: dict[str, Any], item_formatter: Callable[[dict[str, Any]], str], limit: int = 8) -> list[str]:
    status = source.get("status")
    if status == "unavailable":
        return [f"{icon} {title}: unavailable (error: {source.get('error')})"]
    if status == "degraded":
        return [f"{icon} {title}: degraded (error: {source.get('error')})"]
    if status != "ok":
        return [f"{icon} {title}: failed (error: {source.get('error')})"]
    items = source.get("items", [])
    bits: list[str] = []
    if source.get("no_material_change"):
        bits.append("no material change")
    suppressed = int(source.get("suppressed_replied") or 0)
    if suppressed:
        bits.append(f"{suppressed} already-replied suppressed")
    suffix = f" — {'; '.join(bits)}" if bits else ""
    lines = [f"{icon} {title}: {len(items)} item(s){suffix}"]
    for item in items[:limit]:
        if isinstance(item, dict):
            lines.append(f"  - {item_formatter(item)}")
        else:
            lines.append(f"  - {item}")
    if len(items) > limit:
        lines.append(f"  - … {len(items) - limit} more")
    return lines


def build_urgent(sources: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    urgent: list[dict[str, Any]] = []
    for deal in sources.get("pipeline", {}).get("items", []):
        urgent.append({"source": "pipeline", "severity": "medium", "message": f"Stale deal: {deal.get('client_name')} ({deal.get('stale_days')} days idle)", "ref": deal.get("id")})
    for todo in sources.get("todos", {}).get("items", []):
        if todo.get("overdue") or todo.get("priority") == "high":
            severity = "high" if todo.get("overdue") else "medium"
            urgent.append({"source": "todos", "severity": severity, "message": f"Todo: {todo.get('title')}", "ref": todo.get("id")})
    for inv in sources.get("invoices", {}).get("items", []):
        if inv.get("kind") == "overdue":
            urgent.append({"source": "invoices", "severity": "high", "message": f"Overdue invoice: {inv.get('id')} {inv.get('counterparty')}", "ref": inv.get("id")})
    for dl in sources.get("deadlines", {}).get("items", []):
        if isinstance(dl, dict):
            urgent.append({"source": "deadlines", "severity": "high", "message": str(dl.get("name") or dl.get("title") or dl), "ref": dl.get("id")})
    return urgent


def collect_email_org(config: Any, project_root: Path) -> list[dict[str, Any]]:
    """Collect email organisation status — read-only, never mutates Gmail."""
    try:
        from email_classifier import email_org_status_for_briefing
    except ImportError:
        return []
    status = email_org_status_for_briefing(config)
    # Return as a list with a single summary item
    return [{
        "classified": status.get("classified", 0),
        "with_category": status.get("with_category", 0),
        "unmapped": status.get("unmapped", 0),
        "suggestions": status.get("suggestions", 0),
        "label_suggestions": status.get("label_suggestions", 0),
        "archive_suggestions": status.get("archive_suggestions", 0),
        "create_label_suggestions": status.get("create_label_suggestions", 0),
        "pending_actions": status.get("pending_actions", 0),
    }]


def collect(config_path: str | None, workspace_input: dict[str, Any] | None = None) -> dict[str, Any]:
    if config_path:
        os.environ["CHIEF_OF_STAFF_CONFIG"] = config_path
    config = load_config(config_path)
    if config is None:
        raise RuntimeError("Could not load company.yaml; pass --config or set CHIEF_OF_STAFF_CONFIG")
    root = get_project_root(config)
    if root is None:
        raise RuntimeError("Could not resolve paths.project_root from company.yaml")
    collectors: dict[str, Callable[[Any, Path], list[dict[str, Any]]]] = {
        "gmail": collect_gmail,
        "calendar": collect_calendar,
        "deadlines": collect_deadlines,
        "pipeline": collect_pipeline,
        "todos": collect_todos,
        "invoices": collect_invoices,
        "email_org": collect_email_org,
    }
    if workspace_input is not None:
        # Fetch/compute split: the agent already fetched mail + calendar via its
        # native connector. Feed those records into the SAME downstream gmail /
        # calendar sources instead of constructing a workspace client. Local YAML
        # stores (deadlines/pipeline/todos/invoices/email_org) are unaffected.
        messages = workspace_input.get("messages", []) or []
        events = workspace_input.get("events", []) or []
        operator = _operator_email(config)
        collectors["gmail"] = (
            lambda _config, _root, _msgs=messages, _op=operator: _messages_to_gmail_items(
                _msgs, operator=_op,
            )
        )
        collectors["calendar"] = lambda _config, _root: list(events)
    sources = {name: wrap_source(name, collectors[name], config, root) for name in SOURCE_NAMES}
    last = last_run("daily-briefing")
    previous_hashes = {}
    if isinstance(last, dict):
        previous_hashes = (last.get("metadata") or {}).get("source_hashes", {}) or {}
        if not previous_hashes and isinstance(last.get("input_sources"), dict):
            candidate = last.get("input_sources") or {}
            if all(name in candidate for name in SOURCE_NAMES):
                previous_hashes = candidate
            elif isinstance(candidate.get("source_hashes"), dict):
                previous_hashes = candidate["source_hashes"]
    current_hashes = {name: src.get("hash") for name, src in sources.items()}
    for name, src in sources.items():
        src["no_material_change"] = bool(previous_hashes.get(name) == src.get("hash"))
    return {
        "date": today(),
        "sources": sources,
        "urgent": build_urgent(sources),
        "no_change": bool(previous_hashes) and all(previous_hashes.get(n) == current_hashes.get(n) for n in SOURCE_NAMES),
    }


def render(briefing: dict[str, Any]) -> str:
    sources = briefing["sources"]
    lines: list[str] = [f"📋 Daily Briefing — {briefing['date']}", ""]
    if briefing.get("no_change"):
        lines.append("No material change since the last run.")
        lines.append("")
    lines.append("🚨 Urgent")
    if briefing.get("urgent"):
        for item in briefing["urgent"][:12]:
            lines.append(f"  - [{item.get('severity')}] {item.get('message')}")
    else:
        lines.append("  - None")
    lines.append("")
    lines.extend(render_source("Calendar", "📅", sources["calendar"], lambda i: f"{i.get('title') or i.get('summary') or i.get('id')} {i.get('start', '')}"))
    lines.append("")
    lines.extend(render_source("Deadlines", "⏰", sources["deadlines"], lambda i: f"{i.get('name') or i.get('title')} due {i.get('due') or i.get('date', '')}"))
    lines.append("")
    def _gmail_line(i: dict[str, Any]) -> str:
        n = len(i.get("result", [])) if isinstance(i.get("result"), list) else "result"
        extra = i.get("suppressed_replied")
        if extra:
            return f"{i.get('name')}: {n} (suppressed {extra} already-replied)"
        return f"{i.get('name')}: {n}"

    lines.extend(render_source("Gmail", "📧", sources["gmail"], _gmail_line))
    lines.append("")
    lines.extend(render_source("Pipeline", "📊", sources["pipeline"], lambda i: f"{i.get('client_name')} — {i.get('stage')} ({i.get('stale_days')} days idle)"))
    lines.append("")
    lines.extend(render_source("Todos", "✅", sources["todos"], lambda i: f"{i.get('title')} [{i.get('priority')}] due {i.get('due') or 'none'}"))
    lines.append("")
    lines.extend(render_source("Invoices", "💰", sources["invoices"], lambda i: f"{i.get('kind')} {i.get('id', '')} {i.get('currency', '')} {i.get('amount', '')}"))
    lines.append("")
    # Email organisation — read-only summary
    email_org = sources.get("email_org", {})
    if email_org.get("status") == "ok":
        org_items = email_org.get("items", [])
        if org_items:
            org = org_items[0]
            lines.append(f"📬 Email Organisation: {org.get('classified', 0)} classified, {org.get('with_category', 0)} with category, {org.get('suggestions', 0)} suggestions, {org.get('pending_actions', 0)} pending")
        else:
            lines.append("📬 Email Organisation: no data")
    else:
        lines.append(f"📬 Email Organisation: {email_org.get('status', 'unknown')}")
    return "\n".join(lines)


def record_success(config_path: str | None, briefing: dict[str, Any], rendered: str) -> None:
    config = load_config(config_path)
    if config is None:
        return
    root = get_project_root(config)
    if root:
        (root / ".last_briefing").write_text(today() + "\n", encoding="utf-8")
    source_hashes = {name: source.get("hash") for name, source in briefing.get("sources", {}).items()}
    source_statuses = {name: source.get("status") for name, source in briefing.get("sources", {}).items()}
    errors = [
        {"source": name, "error": source.get("error")}
        for name, source in briefing.get("sources", {}).items()
        if source.get("status") != "ok"
    ]
    # The foundation run_log stores caller-provided sources as input_sources;
    # use source hashes there so the next run can detect material changes.
    record_run("daily-briefing", status="delivered", sources=source_hashes, errors=errors, actions=[{"delivery": "rendered", "statuses": source_statuses}])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and render Chief-of-Staff daily briefing")
    parser.add_argument("--config", help="Path to company.yaml (or CHIEF_OF_STAFF_CONFIG)")
    if runtime_log is not None:
        try:
            runtime_log.add_cli_args(parser)
        except Exception:
            pass
    parser.add_argument("--dry-run", action="store_true", help="Collect but do not record delivery or update .last_briefing")
    parser.add_argument("--json", action="store_true", help="Output normalized JSON")
    parser.add_argument("--render", action="store_true", help="Render structured briefing text")
    parser.add_argument("--input", dest="input", help=(
        "Path to an agent-fetched workspace JSON envelope (or '-' for stdin) "
        "conforming to shared/scripts/schemas.py workspace payload schema. "
        "When set, mail + calendar are read from this file instead of a workspace client."))

    sub = parser.add_subparsers(dest="command")

    # run subcommand
    run_p = sub.add_parser("run", help="Generate structured daily briefing")
    run_p.add_argument("--config", help="Path to company.yaml")
    fmt_g = run_p.add_mutually_exclusive_group()
    fmt_g.add_argument("--summary", action="store_true", help="Text summary output")
    fmt_g.add_argument("--json", action="store_true", help="JSON output")
    fmt_g.add_argument("--markdown", action="store_true", help="Markdown output")
    fmt_g.add_argument("--html", action="store_true", help="HTML output (self-contained, inline CSS)")
    run_p.add_argument("--output", dest="output_path", help="Write output to file instead of stdout")
    run_p.add_argument("--since", type=int, default=24, help="Hours to look back for events (default: 24)")
    run_p.add_argument("--limit", type=int, default=50, help="Max events to include (default: 50)")
    run_p.add_argument("--dry-run", action="store_true", help="Do not record delivery")
    run_p.add_argument("--input", dest="input", help=(
        "Path to an agent-fetched workspace JSON envelope (or '-' for stdin). "
        "When set, calendar events are read from this file instead of a workspace client."))

    # notify subcommand
    notify_p = sub.add_parser("notify", help="Send briefing notification")
    notify_p.add_argument("--config", help="Path to company.yaml")
    notify_p.add_argument("--channel", choices=["cli", "email"], default="cli", help="Notification channel")
    notify_p.add_argument("--to", help="Email recipient (for --channel email)")
    notify_p.add_argument("--since", type=int, default=24, help="Hours to look back")
    notify_p.add_argument("--limit", type=int, default=50, help="Max events")
    notify_p.add_argument("--dry-run", action="store_true", help="Do not record or create pending action")
    notify_p.add_argument("--input", dest="input", help=(
        "Path to an agent-fetched workspace JSON envelope (or '-' for stdin). "
        "When set, calendar events are read from this file instead of a workspace client."))

    return parser


def _build_structured_briefing(config_path: str | None, since_hours: int = 24, limit: int = 50,
                               workspace_input: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the structured briefing data shape for v0.2.3."""
    from datetime import datetime, timezone

    config = load_config(config_path)
    operator = "Operator"
    if config:
        company = config.get("company", {})
        operator = company.get("name", config.get("operator", "Operator"))

    # Collect from briefing_sources (READ-ONLY)
    try:
        from briefing_sources import (
            collect_pending_actions, collect_suggestions,
            collect_recent_events, collect_email_org_stats, collect_system_health,
            collect_knowledge_stats, collect_bookkeeper_stats, collect_pipeline_stats,
        )
    except Exception:
        # Fallback if briefing_sources not available
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "window": f"{since_hours}h",
            "summary": {"needs_attention": 0, "pending_approvals": 0, "suggestions": 0,
                        "classified_emails": 0, "system_warnings": 1},
            "sections": {"needs_attention": [], "pending_approvals": {},
                         "email_organisation": {}, "calendar_deadlines": [],
                         "recent_events": [], "suggested_next_actions": [],
                         "system_health": {"state_files": "missing"}},
            "safety": {"external_mutations_performed": False,
                       "approvals_performed": False, "executions_performed": False},
        }

    pending = collect_pending_actions(config) if config else []
    suggestions = collect_suggestions(config) if config else []
    recent_events = collect_recent_events(config, since_hours=since_hours, limit=limit) if config else []
    email_org = collect_email_org_stats(config) if config else {}
    sys_health = collect_system_health(config) if config else {}
    knowledge = collect_knowledge_stats(config) if config else {}
    bookkeeper = collect_bookkeeper_stats(config) if config else {}
    pipeline = collect_pipeline_stats(config) if config else {}

    # Group pending by risk
    try:
        from action_risk import group_actions_by_risk, get_action_risk, get_risk_explanation
        # action_risk expects "action_type" key; our pending actions use "type"
        risk_groups = group_actions_by_risk(
            [{**a, "action_type": a.get("type", "")} for a in pending]
        )
    except Exception:
        risk_groups = {"high": [], "medium": [], "low": []}

    # Build needs attention
    needs_attention: list[dict[str, Any]] = []
    for a in pending:
        if a.get("state") == "requested" or a.get("state") == "pending":
            risk = get_action_risk(a.get("type", "")) if "get_action_risk" in dir() else "low"
            needs_attention.append({
                "title": f"{a.get('type', '?')} — {a.get('summary', '')}",
                "risk": risk,
                "why": f"Pending approval: {a.get('type', '?')} action created {a.get('created_at', '?')}",
            })
    for s in suggestions:
        needs_attention.append({
            "title": f"Suggestion: {s.get('title', s.get('summary', ''))}",
            "risk": s.get("execution_risk", s.get("risk", "low")),
            "why": f"Suggested action in state '{s.get('state', '?')}'",
        })

    # Build suggested next actions
    next_actions: list[dict[str, Any]] = []
    for a in pending:
        if a.get("state") in ("requested", "pending", "approved"):
            risk = get_action_risk(a.get("type", "")) if "get_action_risk" in dir() else "low"
            next_actions.append({
                "title": f"Review: {a.get('type', '?')} — {a.get('summary', '')}",
                "risk": risk,
                "why": get_risk_explanation(a.get("type", ""), risk) if "get_risk_explanation" in dir() else "",
            })
    for s in suggestions[:5]:
        next_actions.append({
            "title": f"Suggestion: {s.get('title', s.get('summary', ''))}",
            "risk": s.get("execution_risk", s.get("risk", "low")),
            "why": f"Auto-generated from event {s.get('event_id', '?')}",
        })

    # Pending approvals grouped
    pa_grouped: dict[str, list[dict[str, Any]]] = {}
    for risk_level in ("high", "medium", "low"):
        pa_grouped[risk_level] = [
            {"action_id": a.get("id", "?"), "type": a.get("type", "?"),
             "summary": a.get("summary", ""), "state": a.get("state", "?"),
             "risk": risk_level, "created_at": a.get("created_at", "")}
            for a in risk_groups.get(risk_level, [])
            if a.get("state") in ("requested", "pending", "approved")
        ]

    # Calendar deadlines — from agent-provided --input envelope when present.
    calendar_deadlines: list[dict[str, Any]] = []
    if workspace_input is not None:
        try:
            from briefing_sources import collect_calendar_summary_from_records
            calendar_deadlines = collect_calendar_summary_from_records(
                workspace_input.get("events", []) or []
            )
        except Exception:
            calendar_deadlines = []

    # System warnings count
    sys_warnings = 0
    if sys_health.get("state_files") == "missing":
        sys_warnings += 1
    if not sys_health.get("audit_dir", True):
        sys_warnings += 1
    if not sys_health.get("runs_dir", True):
        sys_warnings += 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": f"{since_hours}h",
        "operator": operator,
        "summary": {
            "needs_attention": len(needs_attention),
            "pending_approvals": sum(len(v) for v in pa_grouped.values()),
            "suggestions": len(suggestions),
            "classified_emails": email_org.get("classified", 0),
            "system_warnings": sys_warnings,
        },
        "sections": {
            "needs_attention": needs_attention[:20],
            "pending_approvals": pa_grouped,
            "email_organisation": email_org,
            "calendar_deadlines": calendar_deadlines,
            "recent_events": recent_events,
            "suggested_next_actions": next_actions[:15],
            "system_health": sys_health,
            "knowledge_maintenance": knowledge,
            "bookkeeper": bookkeeper,
            "pipeline": pipeline,
        },
        "safety": {
            "external_mutations_performed": False,
            "approvals_performed": False,
            "executions_performed": False,
        },
    }


def build_briefing(config_path: str | None, since_hours: int = 24, limit: int = 50,
                   workspace_input: dict[str, Any] | None = None) -> dict[str, Any]:
    """Public entrypoint for structured briefing construction (used by ``cmd_demo``)."""
    return _build_structured_briefing(
        config_path, since_hours=since_hours, limit=limit,
        workspace_input=workspace_input,
    )


def _resolve_briefing_format(args: argparse.Namespace, config: Mapping[str, Any] | None) -> str:
    """CLI format flags win; otherwise use delivery.default_format from config."""
    if getattr(args, "json", False):
        return "json"
    if getattr(args, "markdown", False):
        return "markdown"
    if getattr(args, "html", False):
        return "html"
    if getattr(args, "summary", False):
        return "text"
    delivery = (config or {}).get("delivery") if isinstance(config, Mapping) else None
    raw = ""
    if isinstance(delivery, Mapping):
        raw = str(delivery.get("default_format") or "").strip().lower()
    if raw in {"html", "json", "markdown", "text"}:
        return raw
    return "text"


def _html_attachment_path(config: Mapping[str, Any] | None, briefing: dict[str, Any]) -> Path:
    root = get_project_root(config) if config else None
    base = Path(root) if root else Path.cwd()
    stamp = str(briefing.get("generated_at") or "")[:10] or today()
    return base / f"daily-briefing-{stamp}.html"


def _emit_rendered(
    rendered: str,
    *,
    fmt: str,
    output_path: str | None,
    explicit_flag: bool,
    config: Mapping[str, Any] | None,
    briefing: dict[str, Any],
) -> str | None:
    """Write rendered briefing to --output, stdout, or an HTML attachment.

    Returns the attachment path when HTML was delivered as a file (MEDIA:),
    otherwise None.
    """
    if output_path:
        dest = Path(output_path).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rendered, encoding="utf-8")
        return str(dest)
    if fmt == "html" and not explicit_flag:
        dest = _html_attachment_path(config, briefing)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rendered, encoding="utf-8")
        print(f"MEDIA:{dest}")
        return str(dest)
    print(rendered)
    return None


def cmd_run(args: argparse.Namespace) -> int:
    """Generate structured briefing and output in requested format."""
    workspace_input = None
    if getattr(args, "input", None):
        try:
            workspace_input = load_workspace_input(args.input)
        except Exception as exc:
            print(f"daily_briefing.py --input error: {concise_error(exc)}", file=sys.stderr)
            return 1
    briefing = _build_structured_briefing(args.config, since_hours=args.since, limit=args.limit,
                                          workspace_input=workspace_input)
    from briefing_renderer import render
    config = load_config(args.config)
    fmt = _resolve_briefing_format(args, config)
    rendered = render(briefing, fmt)
    _emit_rendered(
        rendered,
        fmt=fmt,
        output_path=getattr(args, "output_path", None),
        explicit_flag=bool(getattr(args, "html", False) or getattr(args, "json", False)
                           or getattr(args, "markdown", False) or getattr(args, "summary", False)),
        config=config,
        briefing=briefing,
    )
    if not args.dry_run:
        record_success(args.config, briefing, render(briefing, "text"))
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    """Send briefing notification via CLI or email (pending action only)."""
    workspace_input = None
    if getattr(args, "input", None):
        try:
            workspace_input = load_workspace_input(args.input)
        except Exception as exc:
            print(f"daily_briefing.py --input error: {concise_error(exc)}", file=sys.stderr)
            return 1
    briefing = _build_structured_briefing(args.config, since_hours=args.since, limit=args.limit,
                                          workspace_input=workspace_input)
    from briefing_renderer import render

    if args.channel == "cli":
        print(render(briefing, "text"))
        return 0
    elif args.channel == "email":
        if not args.to:
            print("Error: --to required for email channel", file=sys.stderr)
            return 1
        if args.dry_run:
            print(f"[DRY-RUN] Would create pending gmail.send to {args.to}")
            fmt = _resolve_briefing_format(args, load_config(args.config))
            if fmt == "html":
                print(render(briefing, "html"))
            else:
                print(render(briefing, "markdown"))
            return 0
        # Create a PENDING ACTION only — do NOT auto-send
        try:
            from state_db import create_pending_action
            config = load_config(args.config)
            if not config:
                print("Error: cannot load config", file=sys.stderr)
                return 1
            fmt = _resolve_briefing_format(args, config)
            payload: dict[str, Any] = {
                "to": args.to,
                "subject": f"Daily Briefing — {briefing.get('generated_at', '')[:10]}",
            }
            if fmt == "html":
                dest = _html_attachment_path(config, briefing)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(render(briefing, "html"), encoding="utf-8")
                payload["body"] = f"Daily briefing attached.\nMEDIA:{dest}"
                payload["attachments"] = [{
                    "path": str(dest),
                    "filename": dest.name,
                    "mime_type": "text/html",
                }]
            else:
                payload["body"] = render(briefing, "markdown")
            action = create_pending_action(
                config=config,
                action_type="gmail.send",
                provider="google_api",
                target=args.to,
                payload=payload,
                summary=f"Email daily briefing to {args.to}",
            )
            print(f"✅ Pending action created: {action['id']} (gmail.send to {args.to})")
            print("   This will NOT auto-send. Approve it to send:")
            print(f"   python skills/document-preparer/scripts/webhook_events.py approve --action-id {action['id']}")
            return 0
        except Exception as exc:
            print(f"Error creating pending action: {exc}", file=sys.stderr)
            return 1
    return 1


def _dispatch(args: argparse.Namespace) -> int:
    # Subcommand dispatch
    if args.command == "run":
        return cmd_run(args)
    elif args.command == "notify":
        return cmd_notify(args)

    # Legacy mode (no subcommand)
    try:
        workspace_input = None
        if getattr(args, "input", None):
            workspace_input = load_workspace_input(args.input)
        briefing = collect(args.config, workspace_input=workspace_input)
        if args.json or args.dry_run and not args.render:
            print(json.dumps(briefing, indent=2, ensure_ascii=False, default=str))
            return 0
        rendered = render(briefing)
        print(rendered)
        if not args.dry_run:
            record_success(args.config, briefing, rendered)
        return 0
    except Exception as exc:
        print(f"daily_briefing.py error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Runtime-log lifecycle. When invoked under chief_of_staff the run id is
    # inherited via CHIEF_OF_STAFF_RUN_ID, so events append to the parent run.
    started = False
    if runtime_log is not None:
        try:
            runtime_log.init_run(
                f"daily_briefing {args.command or 'legacy'}",
                None,
                level=getattr(args, "log_level", None),
                quiet=bool(getattr(args, "quiet", False)),
            )
            started = True
        except Exception:
            started = False

    outcome = "success"
    try:
        rc = _dispatch(args)
        if rc != 0:
            outcome = "failed"
        return rc
    except Exception:
        outcome = "failed"
        raise
    finally:
        if started and runtime_log is not None:
            try:
                runtime_log.finish_run(outcome)
            except Exception:
                pass


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
