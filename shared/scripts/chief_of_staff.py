#!/usr/bin/env python3
"""Chief-of-Staff top-level entrypoint (v0.3.0).

READ-ONLY orchestration layer for the daily operating loop and subsystem
summaries. This module must NEVER approve, execute, send, write, or mutate
any state. When a subsystem is missing or misconfigured it degrades with
warnings instead of writing fixes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"

if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

for skill_dir in (
    "daily-briefing",
    "note-taker",
    "pipeline-manager",
    "bookkeeper",
    "document-preparer",
):
    d = PLUGIN_ROOT / "skills" / skill_dir / "scripts"
    if d.exists() and str(d) not in sys.path:
        sys.path.insert(0, str(d))

VERSION = "0.3.0"

# ---------------------------------------------------------------------------
# Optional imports (graceful degradation)
# ---------------------------------------------------------------------------

_IMPORT_ERRORS: dict[str, str] = {}


def _try_import(name: str, import_fn):
    try:
        return import_fn()
    except Exception as exc:  # pragma: no cover - exercised when deps absent
        _IMPORT_ERRORS[name] = str(exc)
        return None


load_config = None
get_project_root = None
get_hermes_home = None
_config_loader = _try_import(
    "config_loader",
    lambda: __import__("config_loader"),
)
if _config_loader is not None:
    load_config = getattr(_config_loader, "load_config", None)
    get_project_root = getattr(_config_loader, "get_project_root", None)
    get_hermes_home = getattr(_config_loader, "get_hermes_home", None)

briefing_sources = _try_import("briefing_sources", lambda: __import__("briefing_sources"))
briefing_renderer = _try_import("briefing_renderer", lambda: __import__("briefing_renderer"))
action_risk = _try_import("action_risk", lambda: __import__("action_risk"))
state_tools = _try_import("state_tools", lambda: __import__("state_tools"))
state_store = _try_import("state_store", lambda: __import__("state_store"))
doctor_mod = _try_import("doctor", lambda: __import__("doctor"))
memory_mod = _try_import("memory", lambda: __import__("memory"))
pending_actions_mod = _try_import("pending_actions", lambda: __import__("pending_actions"))
review_queue_mod = _try_import("review_queue", lambda: __import__("review_queue"))
wiki_curator_mod = _try_import("wiki_curator", lambda: __import__("wiki_curator"))
daily_briefing_mod = _try_import("daily_briefing", lambda: __import__("daily_briefing"))
pipeline_mod = _try_import("pipeline", lambda: __import__("pipeline"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safety_block() -> dict[str, Any]:
    return {
        "read_only": True,
        "approved_actions_executed": 0,
        "provider_writes": 0,
        "pipeline_mutations": 0,
        "invoice_writes": 0,
    }


def _safe_load_config(config_path: str | None) -> Any:
    """Load config via config_loader; never raise."""
    if load_config is None:
        return None
    try:
        return load_config(config_path)
    except Exception:
        return None


def _resolve_project_root(config: Any) -> Path | None:
    if config is None:
        return None
    if get_project_root is not None:
        try:
            root = get_project_root(config)
            if root is not None:
                return Path(root)
        except Exception:
            pass
    try:
        if isinstance(config, Mapping):
            paths = config.get("paths", {})
            if isinstance(paths, Mapping) and paths.get("project_root"):
                return Path(str(paths["project_root"])).expanduser()
    except Exception:
        return None
    return None


def _resolve_hermes_home() -> Path | None:
    if get_hermes_home is not None:
        try:
            return Path(get_hermes_home())
        except Exception:
            pass
    env = os.getenv("CHIEF_OF_STAFF_HERMES_HOME") or os.getenv("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def _get_action_risk(action_type: str) -> str:
    if action_risk is not None and hasattr(action_risk, "get_action_risk"):
        try:
            return str(action_risk.get_action_risk(action_type or ""))
        except Exception:
            return "unknown"
    return "unknown"


def _call_collector(fn_name: str, config: Any, *args: Any, default: Any = None) -> Any:
    """Call a briefing_sources collector, returning default on any failure."""
    if default is None and fn_name.endswith("stats"):
        default = {}
    if default is None:
        default = [] if "actions" in fn_name or "events" in fn_name else {}
    if briefing_sources is None:
        return default
    fn = getattr(briefing_sources, fn_name, None)
    if fn is None:
        return default
    try:
        return fn(config, *args)
    except Exception:
        return default


def _indent_lines(text: str, prefix: str = "  ") -> str:
    return "\n".join(f"{prefix}{line}" if line else prefix.rstrip() for line in text.splitlines())


def _json_dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Panel collectors (all read-only)
# ---------------------------------------------------------------------------

def collect_system_health_panel(config: Any, config_path: str | None) -> dict[str, Any]:
    """System health: config, roots, provider, state file readability."""
    panel: dict[str, Any] = {
        "config_loaded": config is not None,
        "config_path": config_path or os.getenv("CHIEF_OF_STAFF_CONFIG") or str(
            PLUGIN_ROOT / "shared" / "config" / "company.yaml"
        ),
        "project_root": None,
        "project_root_exists": False,
        "hermes_home": None,
        "hermes_home_exists": False,
        "workspace_provider": "unknown",
        "state_files": {},
        "status": "fail",
        "warnings": [],
        "errors": [],
    }

    if config is None:
        panel["errors"].append("config not loaded")
    else:
        root = _resolve_project_root(config)
        if root is not None:
            panel["project_root"] = str(root)
            panel["project_root_exists"] = root.exists()
            if not root.exists():
                panel["warnings"].append(f"project root missing: {root}")
        else:
            panel["errors"].append("project root not resolvable")

        try:
            integrations = config.get("integrations", {}) if isinstance(config, Mapping) else {}
            workspace = integrations.get("workspace", {}) if isinstance(integrations, Mapping) else {}
            provider = (
                workspace.get("provider")
                if isinstance(workspace, Mapping)
                else None
            )
            if not provider and isinstance(config, Mapping):
                google = config.get("google", {})
                if isinstance(google, Mapping) and google:
                    provider = "google_api"
            panel["workspace_provider"] = str(provider or "google_api")
        except Exception:
            panel["workspace_provider"] = "unknown"
            panel["warnings"].append("workspace provider unresolved")

    hermes = _resolve_hermes_home()
    if hermes is not None:
        panel["hermes_home"] = str(hermes)
        panel["hermes_home_exists"] = hermes.exists()
        if not hermes.exists():
            panel["warnings"].append(f"hermes home missing: {hermes}")

    # State file readability (no mutations)
    root_path = Path(panel["project_root"]) if panel["project_root"] else None
    state_names = [
        ".pending_actions.json",
        ".events.json",
        ".email_organisation_classifications.json",
        ".email_organisation_suggestions.json",
        ".webhook_replay_cache.json",
    ]
    if root_path is not None and root_path.exists():
        for name in state_names:
            path = root_path / name
            entry: dict[str, Any] = {"path": str(path), "exists": path.exists(), "readable": False, "error": None}
            if path.exists():
                try:
                    path.read_text(encoding="utf-8")
                    entry["readable"] = True
                except Exception as exc:
                    entry["error"] = str(exc)
                    panel["warnings"].append(f"unreadable state file: {name}")
            else:
                entry["status"] = "missing_optional"
            panel["state_files"][name] = entry
        for dirname in (".audit", ".runs", ".knowledge"):
            dpath = root_path / dirname
            panel["state_files"][dirname] = {
                "path": str(dpath),
                "exists": dpath.is_dir(),
                "readable": dpath.is_dir(),
            }
    else:
        panel["warnings"].append("state files not checked (no project root)")

    # Optionally enrich with briefing_sources system health
    local_health = _call_collector("collect_system_health", config, default={})
    if isinstance(local_health, dict):
        panel["pending_summary"] = local_health.get("pending_summary", {})
        panel["local_state_files"] = local_health.get("state_files", "unknown")

    if panel["errors"]:
        panel["status"] = "fail"
    elif panel["warnings"]:
        panel["status"] = "warn"
    elif panel["config_loaded"] and panel["project_root_exists"]:
        panel["status"] = "ok"
    else:
        panel["status"] = "warn"
    return panel


def collect_briefing_panel(config: Any) -> dict[str, Any]:
    """Briefing sources: recent events, email org, system health (read-only)."""
    events = _call_collector("collect_recent_events", config, 24, 50, default=[])
    email_org = _call_collector("collect_email_org_stats", config, default={})
    health = _call_collector("collect_system_health", config, default={})
    suggestions = _call_collector("collect_suggestions", config, default=[])
    return {
        "recent_events_count": len(events) if isinstance(events, list) else 0,
        "recent_events": events[:10] if isinstance(events, list) else [],
        "email_organisation": email_org if isinstance(email_org, dict) else {},
        "system_health": health if isinstance(health, dict) else {},
        "suggestions_count": len(suggestions) if isinstance(suggestions, list) else 0,
    }


def collect_review_queue_panel(config: Any) -> dict[str, Any]:
    """Pending actions grouped by state and risk."""
    states = ("requested", "approved", "failed", "executing", "dismissed", "executed")
    by_state: dict[str, int] = {s: 0 for s in states}
    by_risk: dict[str, int] = {"high": 0, "medium": 0, "low": 0, "unknown": 0}
    by_state_risk: dict[str, dict[str, int]] = {
        s: {"high": 0, "medium": 0, "low": 0, "unknown": 0} for s in states
    }
    total = 0
    actions = _call_collector("collect_pending_actions", config, default=[])
    if not actions and pending_actions_mod is not None:
        try:
            actions = pending_actions_mod.list_pending_actions(config or {})
        except Exception:
            actions = []

    sample: list[dict[str, Any]] = []
    for action in actions or []:
        if not isinstance(action, Mapping):
            continue
        total += 1
        state = str(action.get("state") or "unknown")
        action_type = str(action.get("type") or action.get("action_type") or "")
        risk = _get_action_risk(action_type)
        if state in by_state:
            by_state[state] += 1
        else:
            by_state[state] = by_state.get(state, 0) + 1
        by_risk[risk] = by_risk.get(risk, 0) + 1
        if state in by_state_risk:
            by_state_risk[state][risk] = by_state_risk[state].get(risk, 0) + 1
        if len(sample) < 8:
            sample.append(
                {
                    "action_id": action.get("action_id") or action.get("id") or "",
                    "type": action_type,
                    "state": state,
                    "risk": risk,
                    "summary": action.get("summary") or action.get("title") or "",
                }
            )

    return {
        "total": total,
        "by_state": by_state,
        "by_risk": by_risk,
        "by_state_risk": by_state_risk,
        "sample": sample,
    }


def collect_pipeline_panel(config: Any) -> dict[str, Any]:
    stats = _call_collector("collect_pipeline_stats", config, default={})
    if not isinstance(stats, dict):
        stats = {}
    return {
        "active_deals": stats.get("active_deals", 0),
        "stale_deals": stats.get("stale_deals", 0),
        "oldest_stale_id": stats.get("oldest_stale_id", ""),
        "oldest_stale_days": stats.get("oldest_stale_days", 0),
        "oldest_stale_stage": stats.get("oldest_stale_stage", ""),
        "recently_moved": stats.get("recently_moved", 0),
        "pending_crm_actions": stats.get("pending_crm_actions", 0),
        "contract_signed_no_invoice": stats.get("contract_signed_no_invoice", 0),
        "invoiced_not_paid": stats.get("invoiced_not_paid", 0),
    }


def collect_bookkeeper_panel(config: Any) -> dict[str, Any]:
    stats = _call_collector("collect_bookkeeper_stats", config, default={})
    if not isinstance(stats, dict):
        stats = {}
    return {
        "candidates_found": stats.get("candidates_found", 0),
        "candidates_needs_review": stats.get("candidates_needs_review", 0),
        "duplicate_warnings": stats.get("duplicate_warnings", 0),
        "pending_record_actions": stats.get("pending_record_actions", 0),
        "outstanding_ap": stats.get("outstanding_ap", "0"),
        "outstanding_ar": stats.get("outstanding_ar", "0"),
        "overdue_count": stats.get("overdue_count", 0),
    }


def collect_knowledge_panel(config: Any) -> dict[str, Any]:
    stats = _call_collector("collect_knowledge_stats", config, default={})
    if not isinstance(stats, dict):
        stats = {}

    # Optional deeper lint (read-only)
    memory_lint: dict[str, Any] = {}
    if memory_mod is not None and hasattr(memory_mod, "lint_memory"):
        try:
            memory_lint = dict(memory_mod.lint_memory(config) or {})
        except Exception as exc:
            memory_lint = {"error": str(exc)}

    wiki_findings: list[dict[str, Any]] = []
    wiki_counts = {"error": 0, "warn": 0, "info": 0}
    if wiki_curator_mod is not None and hasattr(wiki_curator_mod, "validate_wiki"):
        try:
            findings = wiki_curator_mod.validate_wiki(config or {})
            for f in findings or []:
                level = str(getattr(f, "level", None) or getattr(f, "severity", None) or "info").lower()
                if level not in wiki_counts:
                    level = "info"
                wiki_counts[level] = wiki_counts.get(level, 0) + 1
                if len(wiki_findings) < 20:
                    wiki_findings.append(
                        {
                            "level": level,
                            "path": str(getattr(f, "path", "") or getattr(f, "page", "")),
                            "message": str(getattr(f, "message", "") or getattr(f, "detail", "")),
                        }
                    )
        except Exception as exc:
            wiki_findings.append({"level": "error", "path": "", "message": str(exc)})
            wiki_counts["error"] += 1

    lint_warning_count = sum(
        int(stats.get(k, 0) or 0)
        for k in (
            "stale_records",
            "low_confidence_records",
            "contested_records",
            "uncited_records",
            "duplicate_records",
            "wiki_broken_links",
            "wiki_missing_frontmatter",
            "wiki_duplicate_pages",
            "wiki_stale_pages",
        )
    )
    if isinstance(memory_lint, dict):
        for key in ("warnings", "stale_records", "low_confidence", "contested", "uncited", "duplicates"):
            val = memory_lint.get(key)
            if isinstance(val, list):
                lint_warning_count = max(lint_warning_count, len(val)) if key == "warnings" else lint_warning_count
            elif isinstance(val, int) and key != "warnings":
                pass  # already covered via stats; keep totals from memory_lint too
        # Prefer explicit memory lint counts if present
        for key, alias in (
            ("stale_records", "stale_records"),
            ("low_confidence", "low_confidence_records"),
            ("contested", "contested_records"),
            ("uncited", "uncited_records"),
            ("duplicates", "duplicate_records"),
        ):
            if isinstance(memory_lint.get(key), int) and not stats.get(alias):
                stats[alias] = memory_lint[key]

    return {
        "total_records": stats.get("total_records", 0),
        "memory_records_created": stats.get("memory_records_created", 0),
        "memory_records_updated": stats.get("memory_records_updated", 0),
        "wiki_pages_created": stats.get("wiki_pages_created", 0),
        "wiki_pages_updated": stats.get("wiki_pages_updated", 0),
        "stale_records": stats.get("stale_records", 0),
        "low_confidence_records": stats.get("low_confidence_records", 0),
        "contested_records": stats.get("contested_records", 0),
        "uncited_records": stats.get("uncited_records", 0),
        "duplicate_records": stats.get("duplicate_records", 0),
        "wiki_broken_links": stats.get("wiki_broken_links", 0),
        "wiki_missing_frontmatter": stats.get("wiki_missing_frontmatter", 0),
        "wiki_duplicate_pages": stats.get("wiki_duplicate_pages", 0),
        "wiki_stale_pages": stats.get("wiki_stale_pages", 0),
        "lint_warning_count": lint_warning_count,
        "memory_lint": {
            k: memory_lint.get(k)
            for k in (
                "warnings_count",
                "error_count",
                "stale_records",
                "low_confidence",
                "contested",
                "uncited",
                "duplicates",
                "error",
            )
            if k in memory_lint or memory_lint.get(k) is not None
        }
        if memory_lint
        else {},
        "wiki_lint_counts": wiki_counts,
        "wiki_findings_sample": wiki_findings,
    }


def collect_state_safety_panel(config: Any) -> dict[str, Any]:
    """Check stuck executing actions, malformed files, missing optional files."""
    panel: dict[str, Any] = {
        "stuck_executing": [],
        "stuck_executing_count": 0,
        "malformed_files": [],
        "malformed_count": 0,
        "missing_optional": [],
        "missing_optional_count": 0,
        "status": "ok",
        "warnings": [],
    }
    root = _resolve_project_root(config)
    if root is None or not root.exists():
        panel["status"] = "warn"
        panel["warnings"].append("project root unavailable for state safety checks")
        return panel

    # Malformed + missing via state_tools when available
    if state_tools is not None:
        try:
            if hasattr(state_tools, "_find_malformed_json"):
                malformed = state_tools._find_malformed_json(root)  # noqa: SLF001 — read-only
                panel["malformed_files"] = malformed or []
                panel["malformed_count"] = len(panel["malformed_files"])
            if hasattr(state_tools, "_pending_actions_data") and hasattr(
                state_tools, "_find_executing_actions"
            ):
                data, _path = state_tools._pending_actions_data(  # noqa: SLF001
                    root, panel["malformed_files"]
                )
                executing = state_tools._find_executing_actions(  # noqa: SLF001
                    data, min_age_minutes=15
                )
                # Flag stale and no_ts as stuck
                stuck = [
                    a
                    for a in (executing or [])
                    if str(a.get("age_status")) in {"stale", "no_ts"}
                ]
                # Also include any executing (even fresh) for visibility
                if not stuck:
                    stuck = list(executing or [])
                panel["stuck_executing"] = stuck
                panel["stuck_executing_count"] = len(stuck)
            if hasattr(state_tools, "STATE_ITEMS"):
                for item in state_tools.STATE_ITEMS:
                    name = str(item.get("name", ""))
                    path = root / name
                    if not path.exists():
                        panel["missing_optional"].append(
                            {
                                "name": name,
                                "path": str(path),
                                "description": item.get("description", ""),
                            }
                        )
        except Exception as exc:
            panel["warnings"].append(f"state_tools inspection failed: {exc}")
    else:
        # Minimal fallback without state_tools
        pending_path = root / ".pending_actions.json"
        if pending_path.exists():
            try:
                data = json.loads(pending_path.read_text(encoding="utf-8"))
                actions = data.get("actions", {}) if isinstance(data, dict) else {}
                if isinstance(actions, dict):
                    for aid, action in actions.items():
                        if isinstance(action, dict) and action.get("state") == "executing":
                            panel["stuck_executing"].append(
                                {
                                    "id": str(action.get("id") or aid),
                                    "type": str(action.get("type", "")),
                                    "summary": str(action.get("summary", "")),
                                    "age_status": "unknown",
                                }
                            )
                panel["stuck_executing_count"] = len(panel["stuck_executing"])
            except Exception as exc:
                panel["malformed_files"].append(
                    {"name": ".pending_actions.json", "path": str(pending_path), "error": str(exc)}
                )
                panel["malformed_count"] = 1
        for name in (".events.json", ".audit", ".runs"):
            if not (root / name).exists():
                panel["missing_optional"].append(
                    {"name": name, "path": str(root / name), "description": "optional state"}
                )

    panel["missing_optional_count"] = len(panel["missing_optional"])
    if panel["malformed_count"] or panel["stuck_executing_count"]:
        panel["status"] = "warn"
    return panel


def build_recommended_commands(
    system_health: Mapping[str, Any],
    review_queue: Mapping[str, Any],
    pipeline: Mapping[str, Any],
    bookkeeper: Mapping[str, Any],
    knowledge: Mapping[str, Any],
    state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """3–7 prioritized operator commands based on current attention items."""
    cmds: list[dict[str, Any]] = []

    def add(priority: int, reason: str, command: str) -> None:
        cmds.append({"priority": priority, "reason": reason, "command": command})

    if not system_health.get("config_loaded"):
        add(1, "Config failed to load — fix company.yaml first", "python shared/scripts/doctor.py --summary")
    elif system_health.get("status") in {"fail", "warn"}:
        add(2, "System health needs attention", "python shared/scripts/chief_of_staff.py doctor --summary")

    by_state = review_queue.get("by_state") if isinstance(review_queue.get("by_state"), dict) else {}
    by_risk = review_queue.get("by_risk") if isinstance(review_queue.get("by_risk"), dict) else {}
    requested = int(by_state.get("requested", 0) or 0)
    approved = int(by_state.get("approved", 0) or 0)
    failed = int(by_state.get("failed", 0) or 0)
    high = int(by_risk.get("high", 0) or 0)

    if requested:
        reason = f"{requested} requested action(s)"
        if high:
            reason += f" ({high} high risk)"
        add(1 if high else 3, reason, "python shared/scripts/review_queue.py list --state requested")
    if approved:
        add(2, f"{approved} approved action(s) waiting for execution", "python shared/scripts/review_queue.py list --state approved")
    if failed:
        add(2, f"{failed} failed action(s) need investigation", "python shared/scripts/review_queue.py list --state failed")

    stale = int(pipeline.get("stale_deals", 0) or 0)
    cs_no_inv = int(pipeline.get("contract_signed_no_invoice", 0) or 0)
    if stale:
        add(4, f"{stale} stale deal(s) in pipeline", "python skills/pipeline-manager/scripts/pipeline.py stale --summary")
    if cs_no_inv:
        add(3, f"{cs_no_inv} Contract Signed deal(s) without invoice", "python skills/pipeline-manager/scripts/pipeline.py list --summary")

    needs_review = int(bookkeeper.get("candidates_needs_review", 0) or 0)
    dupes = int(bookkeeper.get("duplicate_warnings", 0) or 0)
    candidates = int(bookkeeper.get("candidates_found", 0) or 0)
    if needs_review or dupes:
        add(
            3,
            f"Bookkeeper: {needs_review or candidates} candidate(s) needing review, {dupes} duplicate warning(s)",
            "python skills/bookkeeper/scripts/invoice_ingest.py candidates --summary",
        )
    elif candidates:
        add(5, f"{candidates} invoice candidate(s)", "python skills/bookkeeper/scripts/invoice_ingest.py candidates --summary")

    lint_n = int(knowledge.get("lint_warning_count", 0) or 0)
    stale_rec = int(knowledge.get("stale_records", 0) or 0)
    if lint_n or stale_rec:
        add(
            5,
            f"Knowledge: {lint_n} lint warning(s), {stale_rec} stale record(s)",
            "python shared/scripts/memory.py lint --summary",
        )
    wiki_counts = knowledge.get("wiki_lint_counts") if isinstance(knowledge.get("wiki_lint_counts"), dict) else {}
    if int(wiki_counts.get("error", 0) or 0) or int(wiki_counts.get("warn", 0) or 0):
        add(5, "Wiki lint findings present", "python skills/note-taker/scripts/wiki_curator.py lint --summary")

    stuck = int(state.get("stuck_executing_count", 0) or 0)
    malformed = int(state.get("malformed_count", 0) or 0)
    if stuck:
        add(
            1,
            f"{stuck} stuck executing action(s) — inspect before reset",
            "python shared/scripts/state_tools.py inspect",
        )
    if malformed:
        add(
            2,
            f"{malformed} malformed state file(s)",
            "python shared/scripts/state_tools.py inspect",
        )

    # Always leave the operator with a next daily step if list is empty/short
    if not any(c["command"].endswith("daily --summary") for c in cmds):
        add(9, "Re-run the daily operating loop", "python shared/scripts/chief_of_staff.py daily --summary")

    cmds.sort(key=lambda c: (int(c["priority"]), c["command"]))
    # Deduplicate by command, keep highest priority (lowest number already sorted)
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for c in cmds:
        if c["command"] in seen:
            continue
        seen.add(c["command"])
        unique.append(c)
    return unique[:7]


def build_daily_payload(config: Any, config_path: str | None) -> dict[str, Any]:
    system_health = collect_system_health_panel(config, config_path)
    briefing = collect_briefing_panel(config)
    review_queue = collect_review_queue_panel(config)
    pipeline = collect_pipeline_panel(config)
    bookkeeper = collect_bookkeeper_panel(config)
    knowledge = collect_knowledge_panel(config)
    state = collect_state_safety_panel(config)
    recommended = build_recommended_commands(
        system_health, review_queue, pipeline, bookkeeper, knowledge, state
    )
    if _IMPORT_ERRORS:
        system_health.setdefault("import_errors", dict(_IMPORT_ERRORS))
    return {
        "version": VERSION,
        "generated_at": _now_iso(),
        "mode": "daily",
        "safety": _safety_block(),
        "sections": {
            "system_health": system_health,
            "briefing": briefing,
            "review_queue": review_queue,
            "pipeline": pipeline,
            "bookkeeper": bookkeeper,
            "knowledge": knowledge,
            "state": state,
            "recommended_commands": recommended,
        },
    }


# ---------------------------------------------------------------------------
# Text / markdown rendering
# ---------------------------------------------------------------------------

def render_daily_summary(payload: Mapping[str, Any]) -> str:
    s = payload.get("sections") if isinstance(payload.get("sections"), Mapping) else {}
    lines: list[str] = ["Chief-of-Staff Daily", ""]

    # 1. System health
    sh = s.get("system_health") if isinstance(s.get("system_health"), Mapping) else {}
    lines.append("1. System health")
    lines.append(f"  config: {'ok' if sh.get('config_loaded') else 'fail'}")
    if sh.get("project_root"):
        lines.append(
            f"  project root: {'ok' if sh.get('project_root_exists') else 'missing'} ({sh.get('project_root')})"
        )
    else:
        lines.append("  project root: fail")
    if sh.get("hermes_home"):
        lines.append(
            f"  hermes home: {'ok' if sh.get('hermes_home_exists') else 'missing'} ({sh.get('hermes_home')})"
        )
    lines.append(f"  workspace provider: {sh.get('workspace_provider', 'unknown')}")
    lines.append(f"  state files: {sh.get('local_state_files', 'unknown')}")
    if sh.get("warnings"):
        for w in sh["warnings"][:5]:
            lines.append(f"  warn: {w}")
    if sh.get("errors"):
        for e in sh["errors"][:5]:
            lines.append(f"  fail: {e}")
    lines.append("")

    # 2. Briefing
    br = s.get("briefing") if isinstance(s.get("briefing"), Mapping) else {}
    email = br.get("email_organisation") if isinstance(br.get("email_organisation"), Mapping) else {}
    lines.append("2. Briefing")
    lines.append(f"  recent events (24h): {br.get('recent_events_count', 0)}")
    lines.append(f"  email classified: {email.get('classified', 0)}")
    lines.append(f"  unmapped: {email.get('unmapped', 0)}")
    lines.append(f"  archive candidates: {email.get('archive_candidates', 0)}")
    lines.append(f"  label suggestions: {email.get('label_suggestions', 0)}")
    lines.append(f"  active suggestions: {br.get('suggestions_count', 0)}")
    lines.append("")

    # 3. Needs review
    rq = s.get("review_queue") if isinstance(s.get("review_queue"), Mapping) else {}
    by_state = rq.get("by_state") if isinstance(rq.get("by_state"), Mapping) else {}
    by_state_risk = rq.get("by_state_risk") if isinstance(rq.get("by_state_risk"), Mapping) else {}
    by_risk = rq.get("by_risk") if isinstance(rq.get("by_risk"), Mapping) else {}

    def _risk_fragment(state: str) -> str:
        risks = by_state_risk.get(state) if isinstance(by_state_risk.get(state), Mapping) else {}
        parts = []
        for level in ("high", "medium", "low"):
            n = int(risks.get(level, 0) or 0)
            if n:
                parts.append(f"{n} {level}")
        return f" ({', '.join(parts)})" if parts else ""

    lines.append("3. Needs review")
    lines.append(f"  Total: {rq.get('total', 0)}")
    lines.append(f"  Requested: {by_state.get('requested', 0)}{_risk_fragment('requested')}")
    lines.append(f"  Approved: {by_state.get('approved', 0)}{_risk_fragment('approved')}")
    lines.append(f"  Executing: {by_state.get('executing', 0)}")
    lines.append(f"  Failed: {by_state.get('failed', 0)}")
    lines.append(f"  Dismissed: {by_state.get('dismissed', 0)}")
    lines.append(
        f"  Risk totals: high={by_risk.get('high', 0)} medium={by_risk.get('medium', 0)} low={by_risk.get('low', 0)}"
    )
    if by_state.get("requested"):
        lines.append("  → python shared/scripts/review_queue.py list --state requested")
    lines.append("")

    # 4. Pipeline
    pl = s.get("pipeline") if isinstance(s.get("pipeline"), Mapping) else {}
    lines.append("4. Pipeline / CRM")
    lines.append(f"  Active deals: {pl.get('active_deals', 0)}")
    lines.append(f"  Stale deals: {pl.get('stale_deals', 0)}")
    if pl.get("oldest_stale_id"):
        lines.append(
            f"  Oldest stale: {pl.get('oldest_stale_id')} — "
            f"{pl.get('oldest_stale_stage', '?')}, {pl.get('oldest_stale_days', 0)} days inactive"
        )
    lines.append(f"  Recently moved (7d): {pl.get('recently_moved', 0)}")
    lines.append(f"  Pending CRM actions: {pl.get('pending_crm_actions', 0)}")
    lines.append(f"  Contract Signed without invoice: {pl.get('contract_signed_no_invoice', 0)}")
    lines.append(f"  Invoiced not paid: {pl.get('invoiced_not_paid', 0)}")
    lines.append("")

    # 5. Bookkeeper
    bk = s.get("bookkeeper") if isinstance(s.get("bookkeeper"), Mapping) else {}
    lines.append("5. Bookkeeper")
    lines.append(f"  Invoice candidates: {bk.get('candidates_found', 0)}")
    lines.append(f"  Candidates needing review: {bk.get('candidates_needs_review', 0)}")
    lines.append(f"  Duplicate warnings: {bk.get('duplicate_warnings', 0)}")
    lines.append(f"  Pending record actions: {bk.get('pending_record_actions', 0)}")
    lines.append("")

    # 6. Knowledge
    kn = s.get("knowledge") if isinstance(s.get("knowledge"), Mapping) else {}
    lines.append("6. Knowledge maintenance")
    lines.append(f"  Memory records: {kn.get('total_records', 0)} total")
    lines.append(f"  Stale records: {kn.get('stale_records', 0)}")
    lines.append(f"  Low-confidence: {kn.get('low_confidence_records', 0)}")
    lines.append(f"  Contested: {kn.get('contested_records', 0)}")
    lines.append(f"  Uncited: {kn.get('uncited_records', 0)}")
    lines.append(f"  Duplicate records: {kn.get('duplicate_records', 0)}")
    lines.append(f"  Broken wiki links: {kn.get('wiki_broken_links', 0)}")
    lines.append(f"  Lint warnings (total): {kn.get('lint_warning_count', 0)}")
    wiki_counts = kn.get("wiki_lint_counts") if isinstance(kn.get("wiki_lint_counts"), Mapping) else {}
    if wiki_counts:
        lines.append(
            f"  Wiki lint: error={wiki_counts.get('error', 0)} "
            f"warn={wiki_counts.get('warn', 0)} info={wiki_counts.get('info', 0)}"
        )
    lines.append("")

    # 7. State safety
    st = s.get("state") if isinstance(s.get("state"), Mapping) else {}
    lines.append("7. State safety")
    lines.append(f"  Stuck executing: {st.get('stuck_executing_count', 0)}")
    lines.append(f"  Malformed files: {st.get('malformed_count', 0)}")
    lines.append(f"  Missing optional: {st.get('missing_optional_count', 0)}")
    for item in (st.get("stuck_executing") or [])[:5]:
        if isinstance(item, Mapping):
            lines.append(
                f"  - executing {item.get('id', '?')} ({item.get('type', '?')}) age={item.get('age_status', '?')}"
            )
    for item in (st.get("malformed_files") or [])[:5]:
        if isinstance(item, Mapping):
            lines.append(f"  - malformed {item.get('name', '?')}: {item.get('error', '')}")
    lines.append("")

    # 8. Recommended commands
    rec = s.get("recommended_commands") if isinstance(s.get("recommended_commands"), list) else []
    lines.append("8. Recommended next commands")
    if not rec:
        lines.append("  (none — all clear)")
    for i, cmd in enumerate(rec, 1):
        if not isinstance(cmd, Mapping):
            continue
        lines.append(f"  {i}. {cmd.get('reason', '')}")
        lines.append(f"     {cmd.get('command', '')}")
    lines.append("")

    safety = payload.get("safety") if isinstance(payload.get("safety"), Mapping) else {}
    lines.append(
        f"Safety: read_only={safety.get('read_only', True)} "
        f"approved_executed={safety.get('approved_actions_executed', 0)} "
        f"provider_writes={safety.get('provider_writes', 0)}"
    )
    return "\n".join(lines)


def render_daily_markdown(payload: Mapping[str, Any]) -> str:
    text = render_daily_summary(payload)
    # Light markdown polish while keeping the same numbered structure
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("Chief-of-Staff Daily"):
            out.append("# Chief-of-Staff Daily")
        elif line and line[0].isdigit() and ". " in line[:4]:
            out.append(f"## {line}")
        else:
            out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_daily(args: argparse.Namespace) -> int:
    config = _safe_load_config(getattr(args, "config", None))
    payload = build_daily_payload(config, getattr(args, "config", None))
    if getattr(args, "json", False):
        print(_json_dump(payload))
    elif getattr(args, "markdown", False):
        print(render_daily_markdown(payload))
    else:
        print(render_daily_summary(payload))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Lightweight + optional full doctor health checks (read-only)."""
    checks: list[dict[str, str]] = []
    config_path = getattr(args, "config", None)
    config = _safe_load_config(config_path)

    def record(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    # config loads
    if config is not None:
        record("config", "ok", f"loaded from {config_path or 'default'}")
    else:
        record("config", "fail", "company.yaml missing or invalid")

    # project root
    root = _resolve_project_root(config)
    if root is None:
        record("project_root", "fail", "not resolvable")
    elif root.exists():
        record("project_root", "ok", str(root))
    else:
        record("project_root", "fail", f"missing: {root}")

    # state dirs
    if root is not None and root.exists():
        for dirname in (".audit", ".runs"):
            d = root / dirname
            if d.is_dir():
                record(f"dir:{dirname}", "ok", str(d))
            else:
                record(f"dir:{dirname}", "warn", f"missing optional: {d}")
        for fname in (".pending_actions.json", ".events.json"):
            f = root / fname
            if f.exists():
                try:
                    f.read_text(encoding="utf-8")
                    record(f"file:{fname}", "ok", "readable")
                except Exception as exc:
                    record(f"file:{fname}", "fail", f"unreadable: {exc}")
            else:
                record(f"file:{fname}", "warn", "missing (optional until first use)")
    else:
        record("state_dirs", "fail", "skipped — no project root")

    # Hermes home
    hermes = _resolve_hermes_home()
    if hermes and hermes.exists():
        record("hermes_home", "ok", str(hermes))
    else:
        record("hermes_home", "warn", str(hermes) if hermes else "unresolved")

    # Import readiness
    for mod_name in (
        "briefing_sources",
        "action_risk",
        "pending_actions",
        "memory",
    ):
        if mod_name in _IMPORT_ERRORS:
            record(f"import:{mod_name}", "warn", _IMPORT_ERRORS[mod_name])
        else:
            record(f"import:{mod_name}", "ok", "available")

    # Optionally fold doctor.run_checks (read-only: fix=False)
    if doctor_mod is not None and hasattr(doctor_mod, "run_checks"):
        try:
            report = doctor_mod.run_checks(fix=False, config=config_path)
            for r in report:
                status = {"pass": "ok", "warn": "warn", "fail": "fail"}.get(
                    getattr(r, "status", "warn"), "warn"
                )
                record(f"doctor:{getattr(r, 'name', '?')}", status, str(getattr(r, "detail", "")))
        except Exception as exc:
            record("doctor:run_checks", "warn", str(exc))

    # Print
    status_label = {"ok": "ok", "warn": "warn", "fail": "fail"}
    if getattr(args, "summary", True):
        print("Chief-of-Staff Doctor")
        for c in checks:
            st = status_label.get(c["status"], c["status"])
            print(f"  [{st}] {c['name']}: {c['detail']}")
        failed = sum(1 for c in checks if c["status"] == "fail")
        warned = sum(1 for c in checks if c["status"] == "warn")
        print(f"\nSummary: {len(checks) - failed - warned} ok, {warned} warn, {failed} fail")
    else:
        print(_json_dump({"mode": "doctor", "checks": checks, "safety": _safety_block()}))
    return 1 if any(c["status"] == "fail" for c in checks) else 0


def cmd_review(args: argparse.Namespace) -> int:
    config = _safe_load_config(getattr(args, "config", None))
    panel = collect_review_queue_panel(config)
    if getattr(args, "json", False):
        print(_json_dump({"mode": "review", "safety": _safety_block(), "review_queue": panel}))
        return 0
    print("Review queue summary")
    print(f"Total items: {panel.get('total', 0)}")
    print("By state:")
    by_state = panel.get("by_state") if isinstance(panel.get("by_state"), Mapping) else {}
    for state, count in sorted(by_state.items(), key=lambda kv: (-int(kv[1] or 0), kv[0])):
        print(f"  {state}: {count}")
    print("By risk:")
    by_risk = panel.get("by_risk") if isinstance(panel.get("by_risk"), Mapping) else {}
    for risk in ("high", "medium", "low", "unknown"):
        print(f"  {risk}: {by_risk.get(risk, 0)}")
    if panel.get("sample"):
        print("Sample:")
        for item in panel["sample"][:5]:
            print(
                f"  [{item.get('action_id')}] {item.get('state')} "
                f"{item.get('risk')} {item.get('type')} — {item.get('summary')}"
            )
    return 0


def cmd_pipeline(args: argparse.Namespace) -> int:
    config = _safe_load_config(getattr(args, "config", None))
    panel = collect_pipeline_panel(config)
    if getattr(args, "json", False):
        print(_json_dump({"mode": "pipeline", "safety": _safety_block(), "pipeline": panel}))
        return 0
    print("Pipeline / CRM summary")
    print(f"  Active deals: {panel.get('active_deals', 0)}")
    print(f"  Stale deals: {panel.get('stale_deals', 0)}")
    if panel.get("oldest_stale_id"):
        print(
            f"  Oldest stale: {panel.get('oldest_stale_id')} — "
            f"{panel.get('oldest_stale_stage', '?')}, {panel.get('oldest_stale_days', 0)} days inactive"
        )
    print(f"  Recently moved (7d): {panel.get('recently_moved', 0)}")
    print(f"  Pending CRM actions: {panel.get('pending_crm_actions', 0)}")
    print(f"  Contract Signed without invoice: {panel.get('contract_signed_no_invoice', 0)}")
    print(f"  Invoiced not paid: {panel.get('invoiced_not_paid', 0)}")
    return 0


def cmd_bookkeeper(args: argparse.Namespace) -> int:
    config = _safe_load_config(getattr(args, "config", None))
    panel = collect_bookkeeper_panel(config)
    if getattr(args, "json", False):
        print(_json_dump({"mode": "bookkeeper", "safety": _safety_block(), "bookkeeper": panel}))
        return 0
    print("Bookkeeper summary")
    print(f"  Invoice candidates: {panel.get('candidates_found', 0)}")
    print(f"  Candidates needing review: {panel.get('candidates_needs_review', 0)}")
    print(f"  Duplicate warnings: {panel.get('duplicate_warnings', 0)}")
    print(f"  Pending record actions: {panel.get('pending_record_actions', 0)}")
    print(f"  Outstanding AP: {panel.get('outstanding_ap', '0')}")
    print(f"  Outstanding AR: {panel.get('outstanding_ar', '0')}")
    print(f"  Overdue count: {panel.get('overdue_count', 0)}")
    return 0


def cmd_knowledge(args: argparse.Namespace) -> int:
    config = _safe_load_config(getattr(args, "config", None))
    panel = collect_knowledge_panel(config)
    if getattr(args, "json", False):
        print(_json_dump({"mode": "knowledge", "safety": _safety_block(), "knowledge": panel}))
        return 0
    print("Knowledge / memory / wiki summary")
    print(f"  Memory records: {panel.get('total_records', 0)}")
    print(f"  Stale records: {panel.get('stale_records', 0)}")
    print(f"  Low-confidence: {panel.get('low_confidence_records', 0)}")
    print(f"  Contested: {panel.get('contested_records', 0)}")
    print(f"  Uncited: {panel.get('uncited_records', 0)}")
    print(f"  Duplicates: {panel.get('duplicate_records', 0)}")
    print(f"  Lint warnings: {panel.get('lint_warning_count', 0)}")
    wiki = panel.get("wiki_lint_counts") if isinstance(panel.get("wiki_lint_counts"), Mapping) else {}
    print(
        f"  Wiki lint findings: error={wiki.get('error', 0)} "
        f"warn={wiki.get('warn', 0)} info={wiki.get('info', 0)}"
    )
    for finding in (panel.get("wiki_findings_sample") or [])[:5]:
        if isinstance(finding, Mapping):
            print(f"    [{finding.get('level')}] {finding.get('path')}: {finding.get('message')}")
    return 0


def cmd_smoke_test(args: argparse.Namespace) -> int:
    """Beta readiness: verify read-only render paths and that no writes occur."""
    results: list[dict[str, Any]] = []

    def check(name: str, fn) -> None:
        try:
            ok, detail = fn()
            results.append({"name": name, "pass": bool(ok), "detail": detail})
        except Exception as exc:
            results.append({"name": name, "pass": False, "detail": str(exc)})

    config_path = getattr(args, "config", None)

    # Track project root for write detection (mtime snapshot of known state files)
    config = _safe_load_config(config_path)
    root = _resolve_project_root(config)
    mtimes_before: dict[str, float] = {}
    # Watched non-hidden business files
    _WATCHED_BUSINESS_FILES = {"pipeline.yaml", "invoices.yaml", "expenses.yaml"}
    if root is not None and root.exists():
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            # Watch hidden dotfiles (state files)
            if path.name.startswith("."):
                try:
                    mtimes_before[str(path)] = path.stat().st_mtime
                except OSError:
                    continue
            # Watch known business files at project root
            if path.name in _WATCHED_BUSINESS_FILES and path.parent == root:
                try:
                    mtimes_before[str(path)] = path.stat().st_mtime
                except OSError:
                    continue
        # Watch wiki markdown files
        wiki_path = root / "wiki"
        if wiki_path.exists():
            for path in wiki_path.rglob("*.md"):
                try:
                    mtimes_before[str(path)] = path.stat().st_mtime
                except OSError:
                    continue

    def _config_loads() -> tuple[bool, str]:
        cfg = _safe_load_config(config_path)
        return (cfg is not None, "config loaded" if cfg is not None else "config load failed")

    def _project_root_ok() -> tuple[bool, str]:
        cfg = _safe_load_config(config_path)
        r = _resolve_project_root(cfg)
        if r is None:
            return False, "project root not resolvable"
        return True, str(r)

    def _daily_renders() -> tuple[bool, str]:
        cfg = _safe_load_config(config_path)
        payload = build_daily_payload(cfg, config_path)
        text = render_daily_summary(payload)
        ok = "Chief-of-Staff Daily" in text and "sections" in payload
        return ok, f"daily rendered ({len(text)} chars)"

    def _review_renders() -> tuple[bool, str]:
        cfg = _safe_load_config(config_path)
        panel = collect_review_queue_panel(cfg)
        return True, f"review total={panel.get('total', 0)}"

    def _pipeline_renders() -> tuple[bool, str]:
        cfg = _safe_load_config(config_path)
        panel = collect_pipeline_panel(cfg)
        return True, f"active_deals={panel.get('active_deals', 0)}"

    def _bookkeeper_renders() -> tuple[bool, str]:
        cfg = _safe_load_config(config_path)
        panel = collect_bookkeeper_panel(cfg)
        return True, f"candidates={panel.get('candidates_found', 0)}"

    def _memory_lint_runs() -> tuple[bool, str]:
        if memory_mod is None or not hasattr(memory_mod, "lint_memory"):
            return True, "memory module unavailable (degraded OK)"
        cfg = _safe_load_config(config_path)
        result = memory_mod.lint_memory(cfg or {})
        return isinstance(result, dict), f"keys={list(result.keys())[:6] if isinstance(result, dict) else type(result)}"

    def _wiki_lint_runs() -> tuple[bool, str]:
        if wiki_curator_mod is None or not hasattr(wiki_curator_mod, "validate_wiki"):
            return True, "wiki_curator unavailable (degraded OK)"
        cfg = _safe_load_config(config_path)
        findings = wiki_curator_mod.validate_wiki(cfg or {})
        return isinstance(findings, list), f"findings={len(findings) if isinstance(findings, list) else '?'}"

    def _no_writes() -> tuple[bool, str]:
        if root is None or not root.exists():
            return True, "no project root to compare"
        changed: list[str] = []
        for path in root.rglob("*"):
            if not path.is_file() or not path.name.startswith("."):
                continue
            key = str(path)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            before = mtimes_before.get(key)
            if before is None:
                changed.append(f"new:{path.name}")
            elif mtime > before + 0.001:
                changed.append(f"mtime:{path.name}")
        if changed:
            return False, f"writes detected: {', '.join(changed[:8])}"
        return True, "no state file writes observed"

    check("config_loads", _config_loads)
    check("project_root_resolves", _project_root_ok)
    check("daily_briefing_renders", _daily_renders)
    check("review_queue_renders", _review_renders)
    check("pipeline_renders", _pipeline_renders)
    check("bookkeeper_renders", _bookkeeper_renders)
    check("memory_lint_runs", _memory_lint_runs)
    check("wiki_lint_runs", _wiki_lint_runs)
    check("no_writes", _no_writes)

    all_pass = all(r["pass"] for r in results)
    if getattr(args, "json", False):
        print(
            _json_dump(
                {
                    "mode": "smoke-test",
                    "version": VERSION,
                    "generated_at": _now_iso(),
                    "safety": _safety_block(),
                    "result": "PASS" if all_pass else "FAIL",
                    "checks": results,
                }
            )
        )
    else:
        print("Chief-of-Staff smoke-test")
        for r in results:
            mark = "PASS" if r["pass"] else "FAIL"
            print(f"  [{mark}] {r['name']}: {r['detail']}")
        print("")
        print(f"RESULT: {'PASS' if all_pass else 'FAIL'}")
    return 0 if all_pass else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chief_of_staff.py",
        description=(
            "Chief-of-Staff v0.3.0 — read-only daily operating loop and subsystem summaries. "
            "Never approves, executes, or mutates state."
        ),
    )
    parser.add_argument(
        "--config",
        help="Path to company.yaml (default: CHIEF_OF_STAFF_CONFIG or shared/config/company.yaml)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    daily = sub.add_parser("daily", help="Main daily operating loop (read-only)")
    out = daily.add_mutually_exclusive_group()
    out.add_argument("--summary", action="store_true", default=True, help="Human-readable text (default)")
    out.add_argument("--json", action="store_true", help="Stable JSON schema")
    out.add_argument("--markdown", action="store_true", help="Markdown formatted")
    daily.set_defaults(func=cmd_daily)

    doctor = sub.add_parser("doctor", help="System health check (read-only)")
    doctor.add_argument("--summary", action="store_true", default=True)
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    review = sub.add_parser("review", help="Review queue summary (read-only)")
    review.add_argument("--summary", action="store_true", default=True)
    review.add_argument("--json", action="store_true")
    review.set_defaults(func=cmd_review)

    pipeline = sub.add_parser("pipeline", help="Pipeline/CRM summary (read-only)")
    pipeline.add_argument("--summary", action="store_true", default=True)
    pipeline.add_argument("--json", action="store_true")
    pipeline.set_defaults(func=cmd_pipeline)

    bookkeeper = sub.add_parser("bookkeeper", help="Bookkeeper summary (read-only)")
    bookkeeper.add_argument("--summary", action="store_true", default=True)
    bookkeeper.add_argument("--json", action="store_true")
    bookkeeper.set_defaults(func=cmd_bookkeeper)

    knowledge = sub.add_parser("knowledge", help="Memory/wiki summary (read-only)")
    knowledge.add_argument("--summary", action="store_true", default=True)
    knowledge.add_argument("--json", action="store_true")
    knowledge.set_defaults(func=cmd_knowledge)

    smoke = sub.add_parser("smoke-test", help="Beta readiness check (read-only)")
    smoke.add_argument("--summary", action="store_true", default=True)
    smoke.add_argument("--json", action="store_true")
    smoke.set_defaults(func=cmd_smoke_test)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    # Propagate top-level --config already on args.
    try:
        return int(args.func(args))
    except BrokenPipeError:  # pragma: no cover
        return 0
    except Exception as exc:
        print(f"chief_of_staff.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
