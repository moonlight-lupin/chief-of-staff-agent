#!/usr/bin/env python3
"""
Chief-of-Staff plugin hooks.

Each hook is an independent callback registered via __init__.py.
All hooks are soft warnings (advisory, not blocking) unless stated otherwise.
"""

from __future__ import annotations

import os
import re
import sys
import json
import subprocess
import yaml
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Any, Optional

# ── helpers ──────────────────────────────────────────────────────────────────

_PLUGIN_ROOT = Path(__file__).resolve().parent
_SHARED_SCRIPTS = _PLUGIN_ROOT / "shared" / "scripts"

if str(_SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SHARED_SCRIPTS))

from config_loader import is_default_assistant_name  # noqa: E402


def _load_company_yaml() -> Optional[dict]:
    """Load company.yaml from default or env-var path."""
    config_path = os.getenv("CHIEF_OF_STAFF_CONFIG")
    if not config_path:
        config_path = str(_PLUGIN_ROOT / "shared" / "config" / "company.yaml")
    p = Path(config_path).expanduser()
    if not p.exists():
        return None
    try:
        with open(p) as f:
            return yaml.safe_load(f)
    except Exception:
        return None


def _project_root(config: dict) -> Optional[Path]:
    raw = config.get("paths", {}).get("project_root")
    if not raw:
        return None
    return Path(os.path.expanduser(str(raw)))


def _load_yaml_file(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except Exception:
        return None


def _cos_skills_loaded(context: dict) -> bool:
    """Check if any chief-of-staff skills are loaded in this session.

    The Hermes runtime does not currently pass ``loaded_skills`` to plugin
    hooks, so this is best-effort: when we genuinely cannot tell (no context,
    or no ``loaded_skills`` key), default to ``False`` so the CoS persona and
    context banner are only injected when a CoS skill is confirmed loaded —
    not on every casual conversation.
    """
    if not context:
        return False
    loaded = context.get("loaded_skills", [])
    if not loaded:
        return False
    cos_skills = {
        "daily-briefing", "deadline-tracker", "note-taker", "todo-list",
        "calendar-manager", "drive-filer", "meeting-prep", "weekly-review",
        "document-preparer", "pipeline-manager", "bookkeeper", "deep-research",
        "entity-research", "travel-itinerary", "backup", "self-sign",
        "chief-of-staff:daily-briefing", "chief-of-staff:deadline-tracker",
        "chief-of-staff:pipeline-manager", "chief-of-staff:bookkeeper",
        "chief-of-staff:todo-list", "chief-of-staff:calendar-manager",
    }
    return any(s in cos_skills for s in loaded)


_CONTEXT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._&'()+,/-]{0,63}$")


def _safe_context_name(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text or len(text) > 64:
        return None
    if "\n" in text or "\r" in text or '"' in text:
        return None
    if not _CONTEXT_NAME_RE.fullmatch(text):
        return None
    return text


# ── 1. Company Context Primer (pre_llm_call) ────────────────────────────────

def company_context_primer(context: dict = None, **kwargs) -> Optional[str]:
    """Inject a 1-line company context strip before every LLM call.

    Keeps the agent grounded in company state without loading full config.
    """
    if not _cos_skills_loaded(context):
        return None

    config = _load_company_yaml()
    if not config:
        return None

    parts = []

    company = config.get("company", {})
    name = company.get("name", "")
    juris = company.get("jurisdiction", "")
    if name:
        parts.append(f"Company: {name} ({juris})")

    root = _project_root(config)
    if root and root.exists():
        # Quick deadline check
        try:
            from date_utils import categorize_deadline
            custom = config.get("deadlines", {}).get("custom", [])
            overdue = []
            for d in custom:
                if str(d.get("status", "")).strip().lower() == "done":
                    continue
                due = d.get("due")
                if due:
                    cat = categorize_deadline(due)
                    if cat == "overdue":
                        overdue.append(d.get("name", "Unknown"))
            if overdue:
                parts.append(f"OVERDUE: {', '.join(overdue[:3])}")
        except Exception:
            pass

        # Pipeline summary
        pipeline = _load_yaml_file(root / "pipeline.yaml")
        if pipeline and "deals" in pipeline:
            deals = pipeline["deals"]
            active = [d for d in deals if d.get("stage") not in ("Paid",)]
            stale_threshold = config.get("stale_threshold_days", 14)
            today = date.today()
            stale = 0
            for d in active:
                la = d.get("last_activity")
                if la:
                    try:
                        la_date = datetime.strptime(str(la), "%Y-%m-%d").date()
                        if (today - la_date).days > stale_threshold:
                            stale += 1
                    except Exception:
                        pass
            parts.append(f"Pipeline: {len(active)} active, {stale} stale")

        # AR outstanding
        invoices = _load_yaml_file(root / "invoices.yaml")
        if invoices and "invoices" in invoices:
            sent_unpaid = [
                i for i in invoices["invoices"]
                if i.get("direction") == "sent"
                and i.get("status") not in ("paid", "cancelled")
            ]
            if sent_unpaid:
                total = sum(float(i.get("amount", 0)) for i in sent_unpaid)
                ccy = config.get("company", {}).get("currency", "SGD")
                parts.append(f"AR outstanding: {ccy} {total:,.0f}")

    if parts:
        strip = f"[CoS Context] {' | '.join(parts)}"
        # If the operator has configured a distinctive assistant name, lead with
        # an identity line so the agent answers as the named Chief of Staff.
        assistant = config.get("assistant", {})
        aname = assistant.get("name") if isinstance(assistant, dict) else None
        aname = _safe_context_name(aname)
        if aname and not is_default_assistant_name(aname):
            strip = f"You are {aname}, the operator's Chief of Staff. {strip}"
        return strip
    return None


# ── 2. YAML Integrity Checker (post_tool_call) ───────────────────────────────

_KNOWN_YAML_FILES = {
    "pipeline.yaml", "invoices.yaml", "expenses.yaml", "todos.yaml",
    "company.yaml", "drive-map.yaml", "queries.yaml",
}

_KNOWN_YAML_EXAMPLES = {
    "company.yaml.example", "drive-map.yaml.example",
    "queries.yaml.example", "template-index.yaml.example",
}


def yaml_integrity_checker(tool_name: str = "", args: dict = None, result: str = "", context: dict = None, **kwargs) -> Optional[str]:
    """After any tool that writes files, verify known YAML files still parse."""
    if not _cos_skills_loaded(context):
        return None

    # Check if the tool command touched a known YAML file
    cmd = ""
    if not args:
        return None
    if tool_name == "terminal":
        cmd = args.get("command", "")
    elif tool_name in ("write_file", "patch"):
        cmd = args.get("path", "")

    if not cmd:
        return None

    # Find which known YAML files were potentially touched
    touched = []
    for fname in _KNOWN_YAML_FILES:
        if fname in str(cmd):
            touched.append(fname)

    if not touched:
        return None

    # Re-parse each touched file
    config = _load_company_yaml()
    root = _project_root(config) if config else None

    errors = []
    for fname in touched:
        # Determine file path
        if fname == "company.yaml":
            fpath = Path(os.getenv("CHIEF_OF_STAFF_CONFIG", str(_PLUGIN_ROOT / "shared" / "config" / "company.yaml")))
        elif root and (root / fname).exists():
            fpath = root / fname
        elif (_PLUGIN_ROOT / "shared" / "config" / fname).exists():
            fpath = _PLUGIN_ROOT / "shared" / "config" / fname
        else:
            continue

        try:
            with open(fpath) as f:
                yaml.safe_load(f)
        except yaml.YAMLError as e:
            errors.append(f"{fname}: {str(e)[:100]}")
        except FileNotFoundError:
            pass  # File might have been moved/deleted, not our concern

    if errors:
        return "⚠️ YAML integrity check failed after write:\n" + "\n".join(f"  • {e}" for e in errors)
    return None


# ── 3. Stale Briefing Detector (on_session_start) ────────────────────────────

def stale_briefing_detector(context: dict = None, **kwargs) -> Optional[str]:
    """Warn if the last briefing was > 26 hours ago."""
    if not _cos_skills_loaded(context):
        return None

    config = _load_company_yaml()
    if not config:
        return None

    root = _project_root(config)
    if not root:
        return None

    marker = root / ".last_briefing"
    if not marker.exists():
        # Check if this is a fresh install (no briefing ever sent)
        # Only warn if pipeline/data exists but no briefing marker
        if (root / "pipeline.yaml").exists():
            return "ℹ️ No briefing has been delivered yet. Say 'briefing' to get your first daily briefing."
        return None

    try:
        last_str = marker.read_text().strip()
        last_dt = datetime.fromisoformat(last_str)
        elapsed = datetime.now() - last_dt
        if elapsed > timedelta(hours=26):
            days = elapsed.days
            return f"ℹ️ Last briefing was {days} day(s) ago. Say 'briefing' to get caught up."
    except Exception:
        return None

    return None


# ── 4. Pipeline Stage Validator (pre_tool_call) ──────────────────────────────

def pipeline_stage_validator(tool_name: str = "", args: dict = None, context: dict = None, **kwargs) -> Optional[str]:
    """Warn if a command is about to set an invalid pipeline stage."""
    if not _cos_skills_loaded(context):
        return None

    if tool_name not in ("terminal", "write_file", "patch"):
        return None

    if not args:
        return None
    cmd = args.get("command", "") or args.get("path", "") or ""
    if "pipeline.yaml" not in str(cmd):
        return None

    config = _load_company_yaml()
    if not config:
        return None

    valid_stages = config.get("sales_stages", [])
    if not valid_stages:
        return None  # Can't validate without configured stages

    # Look for stage-related keywords in the command
    # Match: --stage "X", --stage=X, stage: "X", stage="X", stage: X
    stage_keywords = re.findall(r'(?:--stage[=\s]+|stage[:"\']\s*)["\']?([^"\'\n]+)', str(cmd), re.IGNORECASE)
    if not stage_keywords:
        # Also check for "move.*to.*<stage>" patterns
        move_match = re.search(r'move.*?\bto\b\s+["\']?([^"\'\n]+?)(?:["\']?\s|$)', str(cmd), re.IGNORECASE)
        if move_match:
            stage_keywords = [move_match.group(1)]

    warnings = []
    for kw in stage_keywords:
        kw_clean = kw.strip().strip(",").strip("'").strip('"')
        if kw_clean and kw_clean not in valid_stages:
            # Fuzzy check — case insensitive
            if not any(kw_clean.lower() == s.lower() for s in valid_stages):
                warnings.append(f"'{kw_clean}' is not a configured sales stage. Valid stages: {', '.join(valid_stages)}")

    if warnings:
        return "⚠️ Pipeline stage validation:\n" + "\n".join(f"  • {w}" for w in warnings)
    return None


# ── 5. Briefing/Review Format Enforcer (post_llm_call) ───────────────────────

_BRIEFING_MARKERS = {
    "daily-briefing": ["📋", "📅", "⏰", "📧"],
    "weekly-review": ["📊", "✅", "📅", "⚠️"],
}


def format_enforcer(response: str = "", context: dict = None, **kwargs) -> Optional[str]:
    """Check that briefing/review output contains required section markers."""
    if not context:
        return None
    loaded = context.get("loaded_skills", [])

    skill_to_check = None
    for skill in loaded:
        for key in _BRIEFING_MARKERS:
            if key in skill:
                skill_to_check = key
                break
        if skill_to_check:
            break

    if not skill_to_check:
        return None

    required = _BRIEFING_MARKERS[skill_to_check]
    missing = [m for m in required if m not in response]

    if missing:
        skill_label = "Briefing" if skill_to_check == "daily-briefing" else "Weekly Review"
        return (
            f"⚠️ {skill_label} may be missing sections ({', '.join(missing)}). "
            f"Skip empty sections, but verify the agent actually queried those data sources."
        )
    return None


# ── 6. Self-Sign Multi-Block Guard (post_tool_call) ──────────────────────────

def self_sign_guard(tool_name: str = "", args: dict = None, result: str = "", context: dict = None, **kwargs) -> Optional[str]:
    """After sign_detector runs, ensure agent presents ALL signature locations."""
    if not _cos_skills_loaded(context):
        return None

    if not args:
        return None
    cmd = args.get("command", "") or ""
    if "sign_detector" not in cmd:
        return None

    # Try to parse the result as JSON
    try:
        # Result might be wrapped in tool output format
        result_text = str(result)
        # Find JSON array in the output
        json_match = re.search(r'\[.*\]', result_text, re.DOTALL)
        if not json_match:
            return None

        locations = json.loads(json_match.group())
        if isinstance(locations, list) and len(locations) > 1:
            return (
                f"⚠️ {len(locations)} signature blocks detected. "
                f"Present ALL {len(locations)} to the user with party context. "
                f"Do NOT sign the first one without checking the others — "
                f"multi-party contracts require confirming which block belongs to the user."
            )
    except (json.JSONDecodeError, TypeError):
        pass

    return None


# ── 7. Deadline Urgency Injection (pre_llm_call) ─────────────────────────────

def deadline_urgency_injection(context: dict = None, **kwargs) -> Optional[str]:
    """If there's an overdue deadline, inject it prominently into context."""
    if not _cos_skills_loaded(context):
        return None

    config = _load_company_yaml()
    if not config:
        return None

    root = _project_root(config)
    if not root:
        return None

    try:
        from date_utils import days_until, categorize_deadline
    except ImportError:
        return None

    # Check custom deadlines
    custom = config.get("deadlines", {}).get("custom", [])
    overdue_items = []
    for d in custom:
        if str(d.get("status", "")).strip().lower() == "done":
            continue
        due = d.get("due")
        if due:
            try:
                cat = categorize_deadline(due)
                if cat == "overdue":
                    days_late = abs(days_until(due))
                    overdue_items.append(f"{d.get('name', 'Unknown')} ({days_late}d overdue)")
            except Exception:
                pass

    # Also check jurisdiction pack deadlines
    juris = config.get("company", {}).get("jurisdiction", "")
    if juris:
        juris_path = _PLUGIN_ROOT / "shared" / "config" / "jurisdictions" / f"{juris.lower()}.yaml"
        juris_data = _load_yaml_file(juris_path)
        if juris_data:
            for req in juris_data.get("statutory", []):
                # Jurisdiction pack deadlines are computed dynamically
                # We can't easily compute them here without full date_utils logic
                # Skip for now — custom deadlines cover the immediate need
                pass

    if overdue_items:
        return f"🚨 OVERDUE DEADLINES: {' | '.join(overdue_items[:3])}. These should be prioritized."
    return None


# ── 8. Wiki Context Injection (pre_llm_call) ─────────────────────────────────

_SIMPLE_COMMAND_VERBS = {
    "run", "create", "delete", "update", "send", "file", "lint", "validate",
    "deploy", "build", "push", "commit", "install", "test", "start", "stop",
    "restart", "backup", "restore", "execute", "generate", "process", "check",
}

_QUESTION_HINTS = {"?", "what", "who", "whom", "whose", "where", "when", "why", "how",
                   "which", "tell me about", "remind me", "do we have", "have we",
                   "did we", "what about", "status of", "summary of"}


def _is_simple_command(message: str) -> bool:
    """True when the message looks like an imperative command, not a question."""
    text = message.strip().lower()
    words = text.split()
    if len(words) < 2:
        return False
    # Strip polite prefixes
    while words and words[0] in ("please", "can", "could", "would", "just", "now"):
        words = words[1:]
    if not words:
        return False
    if words[0] in _SIMPLE_COMMAND_VERBS:
        return True
    return False


def _has_question_intent(message: str) -> bool:
    """True when the message looks like a question or context lookup."""
    text = message.strip().lower()
    if "?" in text:
        return True
    if any(hint in text for hint in _QUESTION_HINTS):
        return True
    return False


def _resolve_wiki_path(config: dict) -> Optional[Path]:
    paths = config.get("paths") if isinstance(config, dict) else None
    if not isinstance(paths, dict):
        paths = {}
    raw = paths.get("wiki_path")
    root = _project_root(config)
    if raw:
        path = Path(os.path.expanduser(str(raw)))
        if not path.is_absolute() and root:
            path = root / path
        return path
    if root:
        return root / "wiki"
    return None


def wiki_context_injection(context: dict = None, message: str = "", **kwargs) -> Optional[str]:
    """Before LLM calls, inject relevant wiki context if the message is a question."""
    try:
        text = str(message or "").strip()
        if not text:
            return None
        if len(text.split()) < 5:
            return None
        if _is_simple_command(text):
            return None
        # Only fire on question-like intent to avoid noisy injections
        if not _has_question_intent(text):
            return None

        config = _load_company_yaml()
        if not config:
            return None

        wiki_path = _resolve_wiki_path(config)
        if wiki_path is None:
            return None

        curator = _PLUGIN_ROOT / "skills" / "note-taker" / "scripts" / "wiki_curator.py"
        if not curator.exists():
            return None

        result = subprocess.run(
            [
                sys.executable,
                str(curator),
                "search",
                "--",
                text,
                "--format", "json",
                "--limit", "5",
                "--wiki", str(wiki_path),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        if not isinstance(data, list) or not data:
            return None

        lines = ["📚 Wiki context:"]
        for item in data:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip() or Path(str(item.get("path") or "")).stem
            page_type = str(item.get("type") or "").strip() or "page"
            snippet = str(item.get("snippet") or "").strip()
            try:
                score_s = f"{float(item.get('score', 0)):.1f}"
            except (TypeError, ValueError):
                score_s = str(item.get("score", "0"))
            lines.append(f"- [[{title}]] ({page_type}, {score_s}): {snippet}")
        if len(lines) == 1:
            return None
        return "\n".join(lines)
    except Exception:
        return None


# ── 9. Note Capture Reminder (post_llm_call) ─────────────────────────────────

# Patterns that indicate the LLM output contains note-worthy content.
# Require stronger evidence than generic headings to avoid false positives:
# meeting notes need a decision/action item companion, research needs
# findings + citation markers, etc.
_NOTE_PATTERNS = [
    r"##\s+meeting\s+notes?\b.*\n.*(?:decision|action\s+item|attendee)",
    r"##\s+decisions?\b.*\n.*(?:action\s+item|follow.?up|owner|responsible)",
    r"##\s+(action\s+items?|follow.?up)\b.*\n.*(?:owner|due|responsible|assign)",
    r"##\s+research\s+(summary|findings?)\b.*\n.*(?:source|citation|\[\d+\]|reference)",
    r"##\s+(key\s+)?(takeaways?|insights?)\b.*\n.*(?:lesson|implication|recommend)",
    r"##\s+(trip\s+)?(learnings?|reflections?)\b.*\n.*(?:lesson|insight|recommend)",
]
_NOTE_RE = re.compile("|".join(_NOTE_PATTERNS), re.IGNORECASE | re.DOTALL)


def note_capture_reminder(response: str = "", context: dict = None, **kwargs) -> Optional[str]:
    """After LLM output, detect note-worthy content and remind ingestion.

    Fires when the response contains meeting notes with decisions,
    research findings with citations, or action items with owners.

    The hook checks ``loaded_skills`` when Hermes provides it. When the
    context is absent (the documented Hermes runtime does not pass
    ``loaded_skills`` to plugin hooks), the hook fires on content alone
    — the post_llm_call event only fires during active sessions, so the
    note-taker skill is likely available.
    """
    # Check loaded_skills if available, but don't block when absent
    if context:
        loaded = context.get("loaded_skills", [])
        if loaded and not any("note-taker" in s for s in loaded):
            return None  # note-taker not loaded, skip

    if not response or not isinstance(response, str):
        return None

    # Check if the response contains note-worthy patterns
    if not _NOTE_RE.search(response):
        return None

    # Don't fire if the response shows evidence of completed capture:
    # explicit wiki path writes or curator runs (not just vocabulary)
    lower = response.lower()
    if "wiki_curator" in lower or "raw/transcripts" in lower:
        return None
    # Check for actual wiki file creation/update, not just [[wikilinks]]
    if re.search(r"(created|updated|wrote|saved).*\.md", lower):
        return None

    return (
        "📝 This output contains note-worthy content (meeting notes, decisions, "
        "or research findings). Consider ingesting it into the wiki via the "
        "note-taker skill: capture the source into raw/, then create or update "
        "entity/concept pages with [[wikilinks]]. Run "
        "`python skills/note-taker/scripts/wiki_curator.py lint` after."
    )


# ── 10. Attachment Drive Suggestion (post_llm_call) ─────────────────────────

_ATTACHMENT_CATEGORIES = {
    ".pdf": "document",
    ".doc": "document", ".docx": "document",
    ".xls": "spreadsheet", ".xlsx": "spreadsheet",
    ".ppt": "presentation", ".pptx": "presentation",
    ".png": "image", ".jpg": "image", ".jpeg": "image",
    ".webp": "image", ".gif": "image",
    ".eml": "email export",
    ".zip": "archive", ".tar": "archive", ".gz": "archive",
    ".csv": "data",
    ".txt": "text", ".md": "text",
}


def _classify_attachment(filename: str) -> str:
    """Classify a file by its extension into a broad category."""
    ext = Path(filename).suffix.lower() if filename else ""
    return _ATTACHMENT_CATEGORIES.get(ext, "file")


def attachment_drive_suggestion(response: str = "", context: dict = None, **kwargs) -> Optional[str]:
    """Detect attachments in the conversation and suggest filing to Drive.

    Read-only: never uploads. Returns a suggestion string asking the user
    for confirmation. Returns None when no attachments are found or when
    the feature is disabled in company.yaml.
    """
    if not context:
        return None

    # Check if attachment suggestions are enabled
    config = _load_company_yaml()
    if config:
        hooks_cfg = config.get("hooks", {})
        if hooks_cfg.get("attachment_suggestions") is False:
            return None

    # Detect attachments from context
    attachments = context.get("attachments") or context.get("files") or []
    if not attachments:
        # Also check for MEDIA: references in the message
        message = context.get("message", "") or ""
        if "MEDIA:" not in message:
            return None
        # Extract MEDIA: paths
        media_paths = re.findall(r"MEDIA:([^\s]+)", message)
        if not media_paths:
            return None
        attachments = [{"name": Path(p).name, "path": p} for p in media_paths]

    if not attachments:
        return None

    # Build suggestion for each attachment
    suggestions = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        name = att.get("name") or att.get("filename") or ""
        if not name:
            continue
        category = _classify_attachment(name)
        suggestions.append(f"  • {name} ({category})")

    if not suggestions:
        return None

    lines = [
        "I found the following attachment(s):",
        *suggestions,
        "",
        "Would you like me to file any of these to Google Drive? "
        "I can classify them and suggest the right folder. "
        "Just confirm and I'll handle the upload.",
    ]
    return "\n".join(lines)


# ── Registration helper ──────────────────────────────────────────────────────

ALL_HOOKS = {
    "pre_llm_call": [
        ("company_context_primer", company_context_primer),
        ("deadline_urgency_injection", deadline_urgency_injection),
        ("wiki_context_injection", wiki_context_injection),
    ],
    "post_tool_call": [
        ("yaml_integrity_checker", yaml_integrity_checker),
        ("self_sign_guard", self_sign_guard),
    ],
    "on_session_start": [
        ("stale_briefing_detector", stale_briefing_detector),
    ],
    "pre_tool_call": [
        ("pipeline_stage_validator", pipeline_stage_validator),
    ],
    "post_llm_call": [
        ("format_enforcer", format_enforcer),
        ("note_capture_reminder", note_capture_reminder),
        ("attachment_drive_suggestion", attachment_drive_suggestion),
    ],
}


def register_all_hooks(ctx):
    """Register all 10 hooks. Called from __init__.py."""
    for event, hooks in ALL_HOOKS.items():
        for name, callback in hooks:
            try:
                ctx.register_hook(event, callback)
            except Exception as e:
                # Log but don't fail — hooks are best-effort
                print(f"[CoS] Warning: failed to register hook '{name}' for '{event}': {e}", file=sys.stderr)
