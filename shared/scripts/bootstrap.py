#!/usr/bin/env python3
"""Compatibility facade for bootstrap hardening.

The established implementation lives in ``bootstrap_base``. This facade keeps
all existing imports and CLI behaviour while adding upgrade-safe path
preservation and migration of pre-manifest routing overlays.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

import bootstrap_base as _impl


_original_identity_overlay = _impl._identity_overlay
_original_cleanup_routing_overlays = _impl._cleanup_routing_overlays


def _identity_overlay(
    args: Any,
    current_config: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Preserve real custom wiki/staging paths on an ordinary re-bootstrap."""
    overlay, notices = _original_identity_overlay(args, current_config)
    current = current_config or {}
    current_paths = current.get("paths", {}) if isinstance(current.get("paths"), Mapping) else {}

    # An explicit --project-root intentionally rebases its dependent paths.
    if not getattr(args, "project_root", None):
        paths = overlay.setdefault("paths", {})
        for key in ("wiki_path", "staging"):
            value = current_paths.get(key)
            if value is not None and not _impl._is_identity_sample(value):
                paths[key] = str(value)
    return overlay, notices


def _frontmatter_and_body(text: str) -> tuple[Mapping[str, Any], str] | None:
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        frontmatter = _impl.yaml.safe_load(parts[1]) or {}
    except Exception:
        return None
    if not isinstance(frontmatter, Mapping):
        return None
    return frontmatter, parts[2].strip()


def _matches_generated_description(skill_slug: str, description: Any) -> bool:
    if not isinstance(description, str):
        return False
    template = _impl.ROUTING_DESCRIPTION_TEMPLATES.get(skill_slug)
    if not template:
        return False
    pattern = re.escape(template)
    pattern = pattern.replace(re.escape("{assistant_name}"), r".+?")
    pattern = pattern.replace(re.escape("{company_name}"), r".+?")
    return re.fullmatch(pattern, description, flags=re.DOTALL) is not None


def _is_legacy_generated_overlay(overlay: Path, skill_slug: str) -> bool:
    """Conservatively identify v0.3.6-v0.3.8 generated overlays.

    Legacy generated copies changed only the routing description and otherwise
    copied the shipped skill body. Manual overlays are preserved.
    """
    overlay_md = overlay / skill_slug / "SKILL.md"
    shipped_md = _impl.SKILLS_DIR / skill_slug / "SKILL.md"
    if not overlay_md.exists() or not shipped_md.exists():
        return False
    try:
        overlay_parts = _frontmatter_and_body(overlay_md.read_text(encoding="utf-8"))
        shipped_parts = _frontmatter_and_body(shipped_md.read_text(encoding="utf-8"))
    except OSError:
        return False
    if overlay_parts is None or shipped_parts is None:
        return False
    overlay_fm, overlay_body = overlay_parts
    shipped_fm, shipped_body = shipped_parts
    return (
        str(overlay_fm.get("name") or "") == skill_slug
        and str(shipped_fm.get("name") or "") == skill_slug
        and overlay_body == shipped_body
        and _matches_generated_description(skill_slug, overlay_fm.get("description"))
    )


def _cleanup_routing_overlays(overlay: Path) -> None:
    """Remove generated overlays, including legacy pre-manifest copies."""
    manifest = _impl._routing_overlay_manifest_path(overlay)
    generated = _impl._load_routing_overlay_manifest(overlay)
    if generated:
        _original_cleanup_routing_overlays(overlay)
        return

    # Upgrade path for overlays generated before .routing-overlays.json existed.
    removed = False
    for skill_slug in _impl.ROUTING_SKILLS:
        if _is_legacy_generated_overlay(overlay, skill_slug):
            shutil.rmtree(overlay / skill_slug, ignore_errors=True)
            removed = True

    # A corrupt/empty manifest should not linger after a successful legacy
    # migration, but it must not authorise deleting unrelated manual content.
    if removed and manifest.exists():
        manifest.unlink()
    if removed and overlay.exists():
        try:
            next(overlay.iterdir())
        except StopIteration:
            overlay.rmdir()


_impl._identity_overlay = _identity_overlay
_impl._cleanup_routing_overlays = _cleanup_routing_overlays
_impl._frontmatter_and_body = _frontmatter_and_body
_impl._matches_generated_description = _matches_generated_description
_impl._is_legacy_generated_overlay = _is_legacy_generated_overlay

if __name__ == "__main__":
    # Keep imports performed by the implementation pointed at the patched module.
    sys.modules.setdefault("bootstrap", _impl)
    raise SystemExit(_impl._main())

# Imported callers receive the original module object, preserving monkeypatching
# and all existing public/private attributes while applying the fixes above.
sys.modules[__name__] = _impl
