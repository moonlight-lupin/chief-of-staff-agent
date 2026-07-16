#!/usr/bin/env python3
"""Email classification against approved organisation policy.

Classifies inbox emails into policy categories, maps them to existing
approved labels/categories, and generates organisation suggestions
(label/tag, archive, create_label). All mailbox mutations go through the
pending-action approval queue — nothing is executed automatically.

Works with Gmail labels and Outlook master categories (tag id = displayName).

Classification is automatic. Mutation is approval-gated.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from email_label_policy import (
    CATEGORY_KEYWORDS, infer_category, _normalize,
    load_policy, policy_path,
)


# ─── Storage ──────────────────────────────────────────────────

def _project_root(config: Any) -> Path:
    root = None
    if isinstance(config, Mapping):
        paths = config.get("paths", {})
        if isinstance(paths, Mapping):
            root = paths.get("project_root")
    if not root:
        root = os.getenv("CHIEF_OF_STAFF_PROJECT_ROOT",
                         str(Path.home() / ".hermes" / "projects" / "default"))
    return Path(str(root)).expanduser()


def _classifications_path(config: Any) -> Path:
    return _project_root(config) / ".email_organisation_classifications.json"


def _org_suggestions_path(config: Any) -> Path:
    return _project_root(config) / ".email_organisation_suggestions.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"items": {}, "_version": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "items" not in data:
            return {"items": {}, "_version": 0}
        return data
    except (json.JSONDecodeError, OSError):
        return {"items": {}, "_version": 0}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_version = (data.get("_version", 0) or 0) + 1
    data["_version"] = new_version
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


# ─── Email Classification ─────────────────────────────────────

def classify_email(
    email: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Classify a single email against the approved policy.

    Returns a classification dict:
    {
        "id": "emc_...",
        "message_id": "...",
        "from": "...",
        "subject": "...",
        "snippet": "...",
        "category": "finance_invoice" | None,
        "confidence": 0.0-1.0,
        "matched_policy_label": "Finance/Invoices" | None,
        "label_id": "Label_123" | None,
        "classification_reason": "...",
        "created_at": "...",
    }
    """
    msg_id = str(
        email.get("id") or email.get("messageId") or email.get("message_id") or ""
    )
    subject = str(email.get("subject") or email.get("snippet") or "")
    raw_from = email.get("from") or email.get("sender") or ""
    if isinstance(raw_from, Mapping):
        addr = raw_from.get("emailAddress") or raw_from
        if isinstance(addr, Mapping):
            sender = str(addr.get("address") or addr.get("name") or "")
        else:
            sender = str(addr or "")
    else:
        sender = str(raw_from)
    snippet = str(email.get("snippet") or email.get("bodyPreview") or "")

    # Build text for classification
    text = _normalize(f"{subject} {snippet} {sender}")

    # Get categories from policy
    categories = policy.get("categories", {})

    # Try to match against policy categories first
    best_cat = None
    best_conf = 0.0
    best_reason = "No policy category match"

    for cat_name, cat_data in categories.items():
        preferred_label = cat_data.get("preferred_label", "")
        aliases = cat_data.get("aliases", [])

        # Check if email matches this category's keywords
        cat_keywords = CATEGORY_KEYWORDS.get(cat_name, [])
        for kw in cat_keywords:
            if kw in text:
                conf = min(0.95, 0.70 + len(kw) * 0.03)
                if conf > best_conf:
                    best_cat = cat_name
                    best_conf = conf
                    best_reason = f"Email content matches {cat_name} (keyword: '{kw}')"
                    break

        # Also check if sender domain matches label name pattern
        label_text = _normalize(preferred_label)
        for alias in aliases:
            if _normalize(alias) in text and _normalize(alias) not in _normalize(subject):
                conf = 0.75
                if conf > best_conf:
                    best_cat = cat_name
                    best_conf = conf
                    best_reason = f"Sender/snippet matches alias '{alias}'"

    # Also try raw category inference (in case policy doesn't cover it)
    if not best_cat or best_conf < 0.60:
        raw_cat, raw_conf = infer_category(subject)
        if raw_cat and raw_conf > best_conf:
            # Check if this category exists in policy
            if raw_cat in categories:
                best_cat = raw_cat
                best_conf = raw_conf
                best_reason = f"Subject inferred as {raw_cat}"
            else:
                # Category exists in taxonomy but not in approved policy
                best_cat = raw_cat
                best_conf = raw_conf * 0.5  # lower confidence — not in approved policy
                best_reason = f"Subject inferred as {raw_cat} (not in approved policy)"

    # Map to policy label
    matched_label = None
    label_id = None
    if best_cat and best_cat in categories:
        cat_data = categories[best_cat]
        matched_label = cat_data.get("preferred_label")
        label_id = cat_data.get("label_id")

    return {
        "id": f"emc_{uuid.uuid4().hex[:10]}",
        "message_id": msg_id,
        "thread_id": str(email.get("threadId") or ""),
        "from": sender,
        "subject": subject,
        "snippet": snippet[:200],
        "category": best_cat,
        "confidence": round(best_conf, 2),
        "matched_policy_label": matched_label,
        "label_id": label_id,
        "classification_reason": best_reason,
        "created_at": _now(),
    }


def classify_inbox(
    config: Any,
    emails: list[dict[str, Any]],
    limit: int = 50,
) -> dict[str, Any]:
    """Classify a batch of inbox emails against the approved policy.

    Returns summary: {classified, with_category, unmapped, no_policy}
    """
    policy = load_policy(config)
    if not policy:
        return {
            "classified": 0,
            "with_category": 0,
            "unmapped": 0,
            "no_policy": True,
            "error": "No approved policy found. Run propose-policy + save-policy first.",
        }

    data = _load_json(_classifications_path(config))
    classifications = []

    for email in emails[:limit]:
        if not isinstance(email, dict):
            continue
        cls = classify_email(email, policy)
        # Skip if already classified (idempotent by message_id)
        existing = any(
            data["items"].get(k, {}).get("message_id") == cls["message_id"]
            for k in data["items"]
        )
        if not existing and cls["message_id"]:
            data["items"][cls["id"]] = cls
            classifications.append(cls)

    if classifications:
        _save_json(_classifications_path(config), data)

    with_cat = [c for c in classifications if c["category"]]
    unmapped = [c for c in classifications if not c["category"]]

    return {
        "classified": len(classifications),
        "with_category": len(with_cat),
        "unmapped": len(unmapped),
        "no_policy": False,
        "details": classifications,
    }


# ─── Organisation Suggestions ──────────────────────────────────

def generate_org_suggestions(
    config: Any,
    limit: int = 50,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Generate email organisation suggestions from classifications.

    Creates suggestions for:
    - gmail.label: apply existing approved label to email
    - gmail.archive: archive newsletter/low-priority emails
    - gmail.create_label: propose new label for unmapped recurring categories

    All suggestions have auto_execute=False.
    Gmail mutations go through pending-action approval queue.

    Returns summary: {generated, label_suggestions, archive_suggestions, create_label_suggestions}
    """
    policy = load_policy(config)
    if not policy:
        return {"generated": 0, "error": "No approved policy found."}

    data = _load_json(_classifications_path(config))
    classifications = list(data["items"].values())
    classifications = sorted(classifications, key=lambda c: c.get("confidence", 0), reverse=True)
    classifications = classifications[:limit]

    sug_data = _load_json(_org_suggestions_path(config))
    existing_msg_ids = {s.get("payload", {}).get("message_id") for s in sug_data["items"].values()}

    suggestions = []
    label_count = 0
    archive_count = 0
    create_label_count = 0

    # Track categories that don't map to any existing label/category
    unmapped_categories: dict[str, int] = {}

    provider = str(policy.get("provider") or "").lower()
    outlook = (
        "microsoft" in provider
        or provider.startswith("m365")
        or "outlook" in provider
    )
    tag_noun = "category" if outlook else "label"

    for cls in classifications:
        if not cls.get("message_id"):
            continue

        msg_id = cls["message_id"]
        category = cls.get("category")
        confidence = cls.get("confidence", 0)

        if confidence < 0.50:
            continue

        # Skip if already has a suggestion
        if msg_id in existing_msg_ids:
            continue

        # 1. Tag suggestion — if we have a matched policy label/category
        if category and cls.get("label_id") and cls.get("matched_policy_label"):
            sug = _make_suggestion(
                action_type="gmail.label",
                title=f"Apply {tag_noun} {cls['matched_policy_label']}",
                reason=(
                    f"Email classified as {category} ({confidence:.0%}) — "
                    f"maps to approved {tag_noun}"
                ),
                confidence=confidence,
                suggestion_risk="low",
                execution_risk="medium",
                requires_approval=True,
                payload={
                    "message_id": msg_id,
                    "label": cls["matched_policy_label"],
                    "label_id": cls["label_id"],
                    "existing_label": True,
                    "classification_id": cls["id"],
                },
                event_summary=cls.get("subject", ""),
            )
            suggestions.append(sug)
            label_count += 1

        # 2. Archive suggestion — for newsletter/marketing/system categories
        elif category in ("newsletter_marketing", "system_notification"):
            sug = _make_suggestion(
                action_type="gmail.archive",
                title=f"Archive {category.replace('_', ' ')} email",
                reason=f"Email appears informational ({category}), no action required",
                confidence=confidence,
                suggestion_risk="low",
                execution_risk="high",
                requires_approval=True,
                payload={
                    "message_id": msg_id,
                    "classification_id": cls["id"],
                },
                event_summary=cls.get("subject", ""),
            )
            suggestions.append(sug)
            archive_count += 1

        # 3. Create tag suggestion — for recurring unmapped categories
        elif category and not cls.get("label_id"):
            unmapped_categories[category] = unmapped_categories.get(category, 0) + 1

    # Generate create_label suggestions for recurring unmapped categories
    new_label_policy = policy.get("new_label_policy", {})
    min_emails = new_label_policy.get("create_only_if", {}).get("min_matching_emails", 5)

    for cat, count in unmapped_categories.items():
        if count >= min_emails:
            # Propose a label/category name based on organisation category
            label_name = cat.replace("_", " ").title()
            sug = _make_suggestion(
                action_type="gmail.create_label",
                title=f"Create new {tag_noun}: {label_name}",
                reason=(
                    f"{count} recent emails classified as {cat} with no "
                    f"existing {tag_noun} fit"
                ),
                confidence=min(0.80, 0.60 + count * 0.02),
                suggestion_risk="medium",
                execution_risk="medium",
                requires_approval=True,
                payload={
                    "label": label_name,
                    "evidence_count": count,
                    "existing_label": False,
                    "category": cat,
                },
                event_summary=f"{count} emails need {label_name} {tag_noun}",
            )
            suggestions.append(sug)
            create_label_count += 1

    # Save suggestions (unless dry-run)
    if not dry_run and suggestions:
        for sug in suggestions:
            sug_data["items"][sug["id"]] = sug
        _save_json(_org_suggestions_path(config), sug_data)

    return {
        "generated": len(suggestions),
        "label_suggestions": label_count,
        "archive_suggestions": archive_count,
        "create_label_suggestions": create_label_count,
        "dry_run": dry_run,
        "details": suggestions,
    }


def _make_suggestion(
    action_type: str,
    title: str,
    reason: str,
    confidence: float,
    suggestion_risk: str,
    execution_risk: str,
    requires_approval: bool,
    payload: dict[str, Any],
    event_summary: str = "",
) -> dict[str, Any]:
    return {
        "id": f"orgs_{uuid.uuid4().hex[:10]}",
        "action_type": action_type,
        "title": title,
        "reason": reason,
        "confidence": round(confidence, 2),
        "suggestion_risk": suggestion_risk,
        "execution_risk": execution_risk,
        "requires_approval": requires_approval,
        "auto_execute": False,  # ALWAYS false
        "state": "suggested",
        "payload": payload,
        "event_summary": event_summary,
        "created_at": _now(),
    }


# ─── Suggestion CRUD ───────────────────────────────────────────

def list_org_suggestions(
    config: Any,
    state: str | None = None,
    action_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    data = _load_json(_org_suggestions_path(config))
    items = list(data["items"].values())
    if state:
        items = [s for s in items if s.get("state") == state]
    if action_type:
        items = [s for s in items if s.get("action_type") == action_type]
    items = sorted(items, key=lambda s: s.get("confidence", 0), reverse=True)
    return items[:limit]


def get_org_suggestion(config: Any, suggestion_id: str) -> dict[str, Any] | None:
    data = _load_json(_org_suggestions_path(config))
    return data["items"].get(suggestion_id)


def dismiss_org_suggestion(config: Any, suggestion_id: str, reason: str | None = None) -> dict[str, Any] | None:
    data = _load_json(_org_suggestions_path(config))
    sug = data["items"].get(suggestion_id)
    if not sug or sug["state"] != "suggested":
        return None
    sug["state"] = "dismissed"
    sug["dismissed_at"] = _now()
    if reason:
        sug["dismiss_reason"] = reason
    _save_json(_org_suggestions_path(config), data)
    return sug


# ─── Pending Action Bridge ────────────────────────────────────

# Maps org action types to the provider method and CLI for approval
ORG_ACTION_ROUTING = {
    "gmail.label": {"method": "gmail_label", "cli": "email_organisation.py"},
    "gmail.archive": {"method": "gmail_archive", "cli": "email_organisation.py"},
    "gmail.create_label": {"method": "gmail_create_label", "cli": "email_organisation.py"},
}


def prepare_pending_from_suggestion(
    config: Any,
    suggestion_id: str,
) -> dict[str, Any]:
    """Prepare a pending action from an email organisation suggestion.

    Creates a pending action in the approval queue but does NOT execute it.
    The operator must approve and execute separately.

    Returns result dict with success, action_id, and next-step message.
    """
    sug = get_org_suggestion(config, suggestion_id)
    if not sug:
        return {"success": False, "error": f"Suggestion not found: {suggestion_id}"}
    if sug["state"] != "suggested":
        return {"success": False, "error": f"Suggestion not in 'suggested' state (state={sug['state']})"}

    action_type = sug["action_type"]
    if action_type not in ORG_ACTION_ROUTING:
        return {"success": False, "error": f"Unknown action type: {action_type}"}

    from pending_actions import create_pending_action
    from workspace_client import get_workspace_client
    from workspace_capabilities import require_capability

    client = get_workspace_client(config)

    payload = sug.get("payload", {})
    target = payload.get("message_id") or payload.get("label", "")

    unsupported = require_capability(client, action_type, target=target)
    if unsupported:
        return {"success": False, "error": f"{action_type} not supported by {client.provider_name}"}

    action = create_pending_action(
        config=config,
        action_type=action_type,
        provider=client.provider_name,
        target=target,
        payload={
            "source": "email_organisation",
            "suggestion_id": suggestion_id,
            **payload,
        },
        summary=f"Email org: {sug['title']}",
    )

    # Mark suggestion as acted_on
    sug["state"] = "acted_on"
    sug["acted_on_at"] = _now()
    sug["pending_action_id"] = action["id"] if action else None
    data = _load_json(_org_suggestions_path(config))
    data["items"][suggestion_id] = sug
    _save_json(_org_suggestions_path(config), data)

    return {
        "success": True,
        "mode": "pending_created",
        "action_type": action_type,
        "action_id": action["id"] if action else None,
        "message": f"Pending action created — approve and execute via email_organisation.py",
    }


def list_pending_org(config: Any) -> list[dict[str, Any]]:
    """List pending actions from email organisation."""
    from pending_actions import list_pending_actions
    actions = list_pending_actions(config)
    return [a for a in actions if a.get("payload", {}).get("source") == "email_organisation"]


# ─── Email Organisation Digest ────────────────────────────────

def render_email_org_digest(config: Any) -> dict[str, Any]:
    """Render a structured email organisation digest.

    Read-only: summarises classifications, suggestions, and pending actions.
    Never mutates Gmail.

    Returns:
    {
        "total_classified": int,
        "with_category": int,
        "unmapped": int,
        "by_category": {category: count},
        "label_suggestions": int,
        "archive_suggestions": int,
        "create_label_suggestions": int,
        "total_suggestions": int,
        "pending_actions": int,
        "text": str  # human-readable
    }
    """
    # Load classifications
    cls_data = _load_json(_classifications_path(config))
    classifications = list(cls_data["items"].values())

    # Load suggestions
    sug_data = _load_json(_org_suggestions_path(config))
    suggestions = list(sug_data["items"].values())

    # Load pending org actions
    pending = list_pending_org(config)

    # Stats
    total_cls = len(classifications)
    with_cat = [c for c in classifications if c.get("category")]
    unmapped = [c for c in classifications if not c.get("category")]

    by_category: dict[str, int] = {}
    for c in with_cat:
        cat = c["category"]
        by_category[cat] = by_category.get(cat, 0) + 1

    suggested = [s for s in suggestions if s.get("state") == "suggested"]
    label_sugs = [s for s in suggested if s.get("action_type") == "gmail.label"]
    archive_sugs = [s for s in suggested if s.get("action_type") == "gmail.archive"]
    create_label_sugs = [s for s in suggested if s.get("action_type") == "gmail.create_label"]

    pending_count = len(pending)

    # Build text
    lines = [
        f"📬 Email Organisation Digest",
        f"",
        f"Classified: {total_cls}",
        f"With category: {len(with_cat)}",
        f"Unmapped: {len(unmapped)}",
    ]

    if by_category:
        lines.append("")
        lines.append("By category:")
        for cat, count in sorted(by_category.items(), key=lambda x: -x[1]):
            lines.append(f"  {cat}: {count}")

    lines.extend([
        f"",
        f"Suggestions:",
        f"  Label: {len(label_sugs)}",
        f"  Archive: {len(archive_sugs)}",
        f"  Create label: {len(create_label_sugs)}",
        f"  Total pending: {pending_count}",
    ])

    if label_sugs:
        lines.append("")
        lines.append("Label suggestions:")
        for s in label_sugs[:5]:
            conf = f"{s['confidence']:.0%}" if s.get("confidence") else "?"
            lines.append(f"  🟢 {s['title']} ({conf})")
        if len(label_sugs) > 5:
            lines.append(f"  … {len(label_sugs) - 5} more")

    if archive_sugs:
        lines.append("")
        lines.append("Archive candidates:")
        for s in archive_sugs[:5]:
            conf = f"{s['confidence']:.0%}" if s.get("confidence") else "?"
            lines.append(f"  🔴 {s['title']} ({conf})")

    if create_label_sugs:
        lines.append("")
        lines.append("New label proposals:")
        for s in create_label_sugs:
            lines.append(f"  🟡 {s['title']}")

    lines.append("")
    lines.append("No Gmail changes were made.")

    return {
        "total_classified": total_cls,
        "with_category": len(with_cat),
        "unmapped": len(unmapped),
        "by_category": by_category,
        "label_suggestions": len(label_sugs),
        "archive_suggestions": len(archive_sugs),
        "create_label_suggestions": len(create_label_sugs),
        "total_suggestions": len(suggested),
        "pending_actions": pending_count,
        "text": "\n".join(lines),
    }


def email_org_status_for_briefing(config: Any) -> dict[str, Any]:
    """Compact email organisation status for daily briefing integration.

    Read-only summary suitable for inclusion in daily briefing.
    Never mutates Gmail.
    """
    digest = render_email_org_digest(config)
    return {
        "classified": digest["total_classified"],
        "with_category": digest["with_category"],
        "unmapped": digest["unmapped"],
        "suggestions": digest["total_suggestions"],
        "label_suggestions": digest["label_suggestions"],
        "archive_suggestions": digest["archive_suggestions"],
        "create_label_suggestions": digest["create_label_suggestions"],
        "pending_actions": digest["pending_actions"],
    }