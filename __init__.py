def register(ctx):
    """Register all skills + 7 quality hooks.

    Skills: 16 total (15 shared + esign-connector replacing self-sign for Phronesis).
    self-sign skill is still in the plugin directory for other users,
    but for Phronesis we register esign-connector instead.
    """
    skills = [
        "daily-briefing", "deadline-tracker", "note-taker",
        "todo-list", "calendar-manager", "drive-filer",
        "meeting-prep", "weekly-review", "document-preparer",
        "pipeline-manager", "bookkeeper", "deep-research",
        "entity-research", "travel-itinerary", "backup",
        "esign-connector",
    ]
    for skill_name in skills:
        ctx.register_skill(skill_name, f"skills/{skill_name}/SKILL.md")

    # Register all 7 quality hooks
    from . import hooks
    hooks.register_all_hooks(ctx)