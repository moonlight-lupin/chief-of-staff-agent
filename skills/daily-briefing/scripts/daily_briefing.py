#!/usr/bin/env python3
"""Daily briefing collector/renderer for the Chief-of-Staff plugin."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

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
    import os as _os
    _script_dir = PLUGIN_ROOT / "shared" / "scripts"
    if str(_script_dir) not in sys.path:
        sys.path.insert(0, str(_script_dir))
    from workspace_client import get_workspace_client
    return get_workspace_client(config)


def collect_gmail(config: Any, project_root: Path) -> list[dict[str, Any]]:
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
    import re as _re
    client = _get_workspace_client(config)
    items: list[dict[str, Any]] = []
    for query in queries:
        if not isinstance(query, dict):
            continue
        q = str(query.get("query", ""))
        # Replace known template variables
        q = q.replace("{delegate_email}", delegate)
        # Skip if any unresolved template variables remain
        if _re.search(r'\{[a-z_]+\}', q):
            continue
        result = client.gmail_search(q, max_results=int(query.get("max", 10)))
        items.append({"name": query.get("name"), "query": q, "result": result})
    return items


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
    data = load_yaml(project_root / "pipeline.yaml")
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
    data = load_yaml(project_root / "todos.yaml")
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
    data = load_yaml(project_root / "invoices.yaml")
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
        items = collector(config, project_root)
        return {"status": "ok", "hash": stable_hash(items), "items": items}
    except Exception as exc:
        err = concise_error(exc)
        return {"status": "failed", "hash": stable_hash({"error": err}), "items": [], "error": err}


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


def collect(config_path: str | None) -> dict[str, Any]:
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


def render_source(title: str, icon: str, source: dict[str, Any], item_formatter: Callable[[dict[str, Any]], str], limit: int = 8) -> list[str]:
    if source.get("status") != "ok":
        return [f"{icon} {title}: failed (error: {source.get('error')})"]
    items = source.get("items", [])
    suffix = " — no material change" if source.get("no_material_change") else ""
    lines = [f"{icon} {title}: {len(items)} item(s){suffix}"]
    for item in items[:limit]:
        if isinstance(item, dict):
            lines.append(f"  - {item_formatter(item)}")
        else:
            lines.append(f"  - {item}")
    if len(items) > limit:
        lines.append(f"  - … {len(items) - limit} more")
    return lines


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
    lines.extend(render_source("Gmail", "📧", sources["gmail"], lambda i: f"{i.get('name')}: {len(i.get('result', [])) if isinstance(i.get('result'), list) else 'result'}"))
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
    parser.add_argument("--dry-run", action="store_true", help="Collect but do not record delivery or update .last_briefing")
    parser.add_argument("--json", action="store_true", help="Output normalized JSON")
    parser.add_argument("--render", action="store_true", help="Render structured briefing text")

    sub = parser.add_subparsers(dest="command")

    # run subcommand
    run_p = sub.add_parser("run", help="Generate structured daily briefing")
    run_p.add_argument("--config", help="Path to company.yaml")
    run_p.add_argument("--summary", action="store_true", help="Text summary output")
    run_p.add_argument("--json", action="store_true", help="JSON output")
    run_p.add_argument("--markdown", action="store_true", help="Markdown output")
    run_p.add_argument("--since", type=int, default=24, help="Hours to look back for events (default: 24)")
    run_p.add_argument("--limit", type=int, default=50, help="Max events to include (default: 50)")
    run_p.add_argument("--dry-run", action="store_true", help="Do not record delivery")

    # notify subcommand
    notify_p = sub.add_parser("notify", help="Send briefing notification")
    notify_p.add_argument("--config", help="Path to company.yaml")
    notify_p.add_argument("--channel", choices=["cli", "email"], default="cli", help="Notification channel")
    notify_p.add_argument("--to", help="Email recipient (for --channel email)")
    notify_p.add_argument("--since", type=int, default=24, help="Hours to look back")
    notify_p.add_argument("--limit", type=int, default=50, help="Max events")
    notify_p.add_argument("--dry-run", action="store_true", help="Do not record or create pending action")

    return parser


def _build_structured_briefing(config_path: str | None, since_hours: int = 24, limit: int = 50) -> dict[str, Any]:
    """Build the structured briefing data shape for v0.2.3."""
    from datetime import datetime, timezone, timedelta

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
            collect_knowledge_stats, collect_bookkeeper_stats,
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
            "calendar_deadlines": [],
            "recent_events": recent_events,
            "suggested_next_actions": next_actions[:15],
            "system_health": sys_health,
            "knowledge_maintenance": knowledge,
            "bookkeeper": bookkeeper,
        },
        "safety": {
            "external_mutations_performed": False,
            "approvals_performed": False,
            "executions_performed": False,
        },
    }


def cmd_run(args: argparse.Namespace) -> int:
    """Generate structured briefing and output in requested format."""
    briefing = _build_structured_briefing(args.config, since_hours=args.since, limit=args.limit)
    from briefing_renderer import render
    if args.json:
        print(render(briefing, "json"))
    elif args.markdown:
        print(render(briefing, "markdown"))
    else:
        # --summary or default → text
        print(render(briefing, "text"))
    if not args.dry_run:
        record_success(args.config, briefing, render(briefing, "text"))
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    """Send briefing notification via CLI or email (pending action only)."""
    briefing = _build_structured_briefing(args.config, since_hours=args.since, limit=args.limit)
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
            print(render(briefing, "markdown"))
            return 0
        # Create a PENDING ACTION only — do NOT auto-send
        try:
            from pending_actions import create_pending_action
            config = load_config(args.config)
            if not config:
                print("Error: cannot load config", file=sys.stderr)
                return 1
            rendered = render(briefing, "markdown")
            action = create_pending_action(
                config=config,
                action_type="gmail.send",
                provider="google_api",
                target=args.to,
                payload={
                    "to": args.to,
                    "subject": f"Daily Briefing — {briefing.get('generated_at', '')[:10]}",
                    "body": rendered,
                },
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Subcommand dispatch
    if args.command == "run":
        return cmd_run(args)
    elif args.command == "notify":
        return cmd_notify(args)

    # Legacy mode (no subcommand)
    try:
        briefing = collect(args.config)
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
