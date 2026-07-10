# Chief of Staff Plugin — Hooks

Hooks are Hermes plugin callbacks that fire at specific points in the agent lifecycle. They can improve output quality, enforce conventions, and provide guardrails.

## Available Hooks

Hermes plugins can register hooks via `__init__.py` using `ctx.register_hook(event, callback)`. The chief-of-staff plugin uses the following hooks:

### 1. `post_llm_call` — Briefing Quality Check

**Purpose:** After the agent generates a Daily Briefing or Weekly Review, verify the output follows the required format and doesn't miss critical sections.

```python
# hooks.py
def briefing_quality_check(response: str, context: dict) -> str | None:
    """Check briefing output for required sections. Returns warning or None."""
    if "daily-briefing" not in context.get("loaded_skills", []):
        return None

    required_markers = ["📋", "📅", "⏰", "📧"]
    missing = [m for m in required_markers if m not in response]

    if missing:
        return f"⚠️ Briefing may be missing sections: {', '.join(missing)}. " \
               f"Check if corresponding data sources returned empty (skip empty sections, " \
               f"but verify the agent actually queried them)."
    return None
```

### 2. `pre_tool_call` — Drive Filer Safety

**Purpose:** Before any Drive upload or file deletion, verify the target folder matches the drive-map rules. Prevents filing sensitive documents to wrong folders.

```python
def drive_filer_safety(tool_name: str, args: dict, context: dict) -> str | None:
    """Block Drive uploads to non-mapped folders."""
    if tool_name != "terminal":
        return None

    cmd = args.get("command", "")
    if "drive upload" not in cmd:
        return None

    # Check that the parent folder ID is in the known drive-map
    # If not, warn the agent to verify the filing target
    if "--parent" in cmd:
        # Extract parent ID and check against drive-map.yaml
        # This is a soft check — warn but don't block
        return None  # Would return warning string if folder not in map
    return None
```

### 3. `post_tool_call` — Self-Sign Confirmation Audit

**Purpose:** After sign_detector.py runs, ensure the agent presented ALL detected locations to the user (not just the first one) and got explicit confirmation for each.

```python
def self_sign_audit(tool_name: str, args: dict, result: str, context: dict) -> str | None:
    """Ensure agent presents all signature locations, not just the first."""
    if "sign_detector" not in str(args.get("command", "")):
        return None

    import json
    try:
        locations = json.loads(result)
        if isinstance(locations, list) and len(locations) > 1:
            return f"⚠️ {len(locations)} signature locations detected. " \
                   f"Present ALL {len(locations)} to the user with party context " \
                   f"and get confirmation for each before signing."
    except (json.JSONDecodeError, TypeError):
        pass
    return None
```

### 4. `on_session_start` — Config Validation

**Purpose:** When a new session starts and the chief-of-staff plugin is active, verify company.yaml exists and is valid. Warn if not configured.

```python
def config_validation(context: dict) -> str | None:
    """Check that company.yaml exists and is parseable."""
    import os
    from pathlib import Path

    config_path = os.getenv("CHIEF_OF_STAFF_CONFIG")
    if not config_path:
        plugin_root = Path(__file__).resolve().parent
        config_path = plugin_root / "shared" / "config" / "company.yaml"

    if not Path(config_path).exists():
        return "ℹ️ Chief of Staff plugin is active but company.yaml is not configured. " \
               "Run `python3 shared/scripts/onboard.py` to set up."
    return None
```

### 5. `pre_approval_request` — Calendar Modification Guard

**Purpose:** Before calendar create/modify/delete commands are approved, add an extra warning to the approval prompt reminding the user what event is being changed.

```python
def calendar_modification_guard(tool_name: str, args: dict, context: dict) -> str | None:
    """Add context to calendar modification approvals."""
    cmd = args.get("command", "")
    if "calendar" not in cmd:
        return None

    if any(word in cmd for word in ["create", "update", "modify", "delete", "remove"]):
        return "📅 This command modifies your Google Calendar. " \
               "Verify the event details are correct before approving."
    return None
```

## Registering Hooks in `__init__.py`

To enable these hooks, update `__init__.py`:

```python
def register(ctx):
    """Register all 17 skills + quality hooks."""
    for skill_name in [
        "daily-briefing", "deadline-tracker", "note-taker",
        "todo-list", "calendar-manager", "drive-filer",
        "meeting-prep", "weekly-review", "document-preparer",
        "pipeline-manager", "bookkeeper", "deep-research",
        "entity-research", "travel-itinerary", "backup", "self-sign",
    ]:
        ctx.register_skill(skill_name, f"skills/{skill_name}/SKILL.md")

    # Register quality hooks (optional — uncomment to enable)
    # from .hooks import (
    #     briefing_quality_check,
    #     drive_filer_safety,
    #     self_sign_audit,
    #     config_validation,
    #     calendar_modification_guard,
    # )
    # ctx.register_hook("post_llm_call", briefing_quality_check)
    # ctx.register_hook("pre_tool_call", drive_filer_safety)
    # ctx.register_hook("post_tool_call", self_sign_audit)
    # ctx.register_hook("on_session_start", config_validation)
    # ctx.register_hook("pre_approval_request", calendar_modification_guard)
```

Hooks are **commented out by default** — enable them per deployment as needed. This keeps the plugin lightweight for users who don't want hooks, while providing quality guardrails for managed service customers.

## Hook Design Principles

1. **Soft warnings, not hard blocks** — hooks return advisory messages, not exceptions. The agent and user can proceed after seeing the warning.
2. **Skill-aware** — hooks check which skills are loaded before firing. No point checking briefing format if briefing skill isn't active.
3. **Zero overhead when disabled** — commented out in `__init__.py` means zero import cost.
4. **Composable** — each hook is independent. Enable any subset without conflicts.
5. **Auditable** — hook messages are prefixed with emoji (⚠️, ℹ️, 📅) for easy identification in logs.