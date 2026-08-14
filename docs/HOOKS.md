# Chief of Staff Plugin — Hooks

Hooks are Hermes plugin callbacks that fire at specific points in the agent lifecycle. They can improve output quality, enforce conventions, and provide guardrails.

All 9 quality hooks are registered and active in `__init__.py` via `hooks.register_all_hooks(ctx)`.

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

### 2. `pre_tool_call` — Drive Filer Safety (unimplemented stub)

**Purpose:** Before any Drive upload or file deletion, verify the target folder matches the drive-map rules. Prevents filing sensitive documents to wrong folders.

**Status: unimplemented.** This stub is documentation-only. It is not registered in `__init__.py` and always returns `None`.

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

Live registration is in `__init__.py`:

```python
def register(ctx):
    """Register all 18 skills + 8 quality hooks."""
    skills = _get_registered_skills()
    for skill_name in skills:
        ctx.register_skill(skill_name, skill_path)

    from . import hooks
    hooks.register_all_hooks(ctx)
```

The 8 registered hooks in `hooks.py → ALL_HOOKS`:

| Event | Hook | Purpose |
|---|---|---|
| `pre_llm_call` | `company_context_primer` | Inject company context strip |
| `pre_llm_call` | `deadline_urgency_injection` | Inject overdue deadlines |
| `pre_tool_call` | `pipeline_stage_validator` | Warn on invalid pipeline stages |
| `post_tool_call` | `yaml_integrity_checker` | Verify YAML files after writes |
| `post_tool_call` | `self_sign_guard` | Ensure all signature blocks presented |
| `post_llm_call` | `format_enforcer` | Check briefing/review section markers |
| `post_llm_call` | `note_capture_reminder` | Detect note-worthy output, remind ingestion |
| `pre_llm_call` | `wiki_context_injection` | Search wiki and inject relevant context on question-like messages |
| `on_session_start` | `stale_briefing_detector` | Warn if last briefing was > 26h ago |

The `drive_filer_safety` hook described below is an unimplemented stub — not registered.

## Hook Design Principles

1. **Soft warnings, not hard blocks** — hooks return advisory messages, not exceptions. The agent and user can proceed after seeing the warning.
2. **Skill-aware** — hooks check which skills are loaded before firing. No point checking briefing format if briefing skill isn't active.
3. **Registered by default** — `__init__.py` calls `hooks.register_all_hooks(ctx)` so the live hooks run in every session. The `drive_filer_safety` stub is not in that set.
4. **Composable** — each hook is independent. Enable any subset without conflicts.
5. **Auditable** — hook messages are prefixed with emoji (⚠️, ℹ️, 📅) for easy identification in logs.