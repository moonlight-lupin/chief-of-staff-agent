def register(ctx):
    """Register all 16 skills + 7 quality hooks."""
    for skill_name in [
        "daily-briefing", "deadline-tracker", "note-taker",
        "todo-list", "calendar-manager", "drive-filer",
        "meeting-prep", "weekly-review", "document-preparer",
        "pipeline-manager", "bookkeeper", "deep-research",
        "entity-research", "travel-itinerary", "backup", "self-sign",
    ]:
        ctx.register_skill(skill_name, f"skills/{skill_name}/SKILL.md")

    # Register all 7 quality hooks
    from . import hooks
    hooks.register_all_hooks(ctx)