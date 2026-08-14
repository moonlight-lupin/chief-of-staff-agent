#!/usr/bin/env python3
"""Centralized settings for the Chief of Staff plugin.

Thin layer over the most critical environment variables. Existing
``os.getenv`` call sites continue to work independently.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    """Centralized settings for Chief of Staff plugin."""
    auto_approve: bool = field(default_factory=lambda: os.getenv("CHIEF_OF_STAFF_AUTO_APPROVE", "") == "1")
    allow_destructive: bool = field(default_factory=lambda: os.getenv("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", "") == "1")
    audit_strict: list[str] = field(default_factory=lambda: [
        s.strip() for s in os.getenv("CHIEF_OF_STAFF_AUDIT_STRICT", "").split(",") if s.strip()
    ])
    project_root: str | None = field(default_factory=lambda: os.getenv("CHIEF_OF_STAFF_PROJECT_ROOT"))
    webhook_secret: str | None = field(default_factory=lambda: os.getenv("CHIEF_OF_STAFF_WEBHOOK_SECRET"))
    pubsub_audience: str | None = field(default_factory=lambda: os.getenv("CHIEF_OF_STAFF_PUBSUB_AUDIENCE"))
