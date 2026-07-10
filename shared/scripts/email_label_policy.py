#!/usr/bin/env python3
"""Email label policy — label parsing, category inference, policy generation.

Read-only: discovers existing Gmail labels, classifies them, infers
organisation categories, and generates a proposed label policy.

Never creates, applies, archives, trashes, or sends anything.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))


# ─── System Labels ────────────────────────────────────────────

SYSTEM_LABELS = frozenset({
    "INBOX", "SENT", "TRASH", "SPAM", "DRAFT", "IMPORTANT",
    "STARRED", "UNREAD", "CHAT", "CATEGORY_PERSONAL",
    "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES",
    "CATEGORY_FORUMS", "CATEGORY_NEWSLETTERS",
})

# Labels that should be ignored for policy purposes.
IGNORED_LABEL_NAMES = frozenset({"UNREAD", "CHAT"})


# ─── Category Taxonomy ────────────────────────────────────────

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "finance_invoice": ["invoice", "invoices", "bill", "bills", "ap", "payment"],
    "finance_receipt": ["receipt", "receipts", "expense", "reimburse"],
    "finance_bank": ["bank", "banking", "transfer", "wire", "statement"],
    "legal_contract": ["legal", "contract", "contracts", "nda", "agreement", "agreements"],
    "kyc_compliance": ["kyc", "aml", "compliance", "onboarding", "due diligence"],
    "tax_audit": ["tax", "audit", "irs", "accounting", "gst"],
    "investor_lp": ["investor", "lp", "fund", "capital", "portfolio"],
    "board_approval": ["board", "resolution", "director", "approval"],
    "vendor_supplier": ["vendor", "supplier", "procurement", "purchase order"],
    "travel": ["travel", "flight", "hotel", "trip", "itinerary"],
    "calendar_meeting": ["calendar", "meeting", "schedule", "agenda"],
    "newsletter_marketing": ["newsletter", "marketing", "promo", "campaign", "subscription"],
    "system_notification": ["notification", "alert", "automated", "no-reply", "noreply"],
    "reference": ["reference", "info", "archive", "keep", "reference"],
    "action_required": ["action", "todo", "follow up", "followup", "pending", "urgent"],
    "archive_candidate": ["old", "archive", "done", "completed", "resolved"],
}


def _normalize(text: str) -> str:
    return text.lower().strip()


def infer_category(label_name: str, path: list[str] | None = None) -> tuple[str | None, float]:
    """Infer a policy category from a label name and optional path.

    Returns (category, confidence) or (None, 0.0) if no match.
    Checks more specific categories first (kyc before legal, etc.).
    """
    full_text = _normalize(label_name)
    if path:
        full_text = _normalize(" ".join(path))

    # Order categories by specificity — check more specific first
    # so "Legal/KYC" matches kyc_compliance before legal_contract
    priority_order = [
        "kyc_compliance",  # before legal_contract
        "finance_invoice",  # before finance_bank
        "finance_receipt",
        "finance_bank",
        "tax_audit",
        "legal_contract",
        "investor_lp",
        "board_approval",
        "vendor_supplier",
        "travel",
        "calendar_meeting",
        "newsletter_marketing",
        "system_notification",
        "action_required",
        "archive_candidate",
        "reference",
    ]

    best_cat = None
    best_score = 0.0

    for cat in priority_order:
        keywords = CATEGORY_KEYWORDS.get(cat, [])
        for kw in keywords:
            if kw in full_text:
                score = min(0.95, 0.60 + len(kw) * 0.03)
                # First match in priority order wins (more specific categories
                # are checked first, so kyc_compliance beats legal_contract)
                return cat, score

    return best_cat, best_score


# ─── Provider-aware tag resolution ────────────────────────────
#
# Policy categories carry a human-readable label NAME. Different providers key
# their tags differently:
#   * Gmail   — the tag id is an opaque label id (e.g. "Label_12"); the name is
#     a separate display string. mail_tag() takes the label id.
#   * Microsoft 365 (Outlook categories) — the tag id IS the category
#     displayName; there is no separate opaque id. mail_tag() takes the name.
#
# resolve_tag_id() hides this difference: given a client and a desired label
# NAME it returns the id to pass to client.mail_tag(), matching case-insensitively
# against the provider's existing tags. This works for any WorkspaceClient via
# the neutral mail_list_tags()/mail_create_tag() surface — no provider
# conditionals — because each provider already reports {"id", "name"} entries
# with the right id semantics for that provider.


def resolve_tag_id(client: Any, label_name: str,
                   create_if_missing: bool = False) -> str | None:
    """Resolve a policy label NAME to the tag id expected by client.mail_tag().

    Returns the provider-appropriate id (Gmail label id, or the Outlook category
    displayName for m365), or None if the tag does not exist and
    ``create_if_missing`` is False. Read-only unless ``create_if_missing`` is
    True (in which case creation still flows through the guarded
    mail_create_tag()).
    """
    target = _normalize(label_name)
    try:
        tags = client.mail_list_tags() or []
    except Exception:
        tags = []
    for tag in tags:
        if not isinstance(tag, Mapping):
            continue
        name = tag.get("name") or tag.get("displayName") or ""
        if _normalize(str(name)) == target:
            # Prefer the explicit id; fall back to the name (m365 id == name).
            return str(tag.get("id") or name)
    if not create_if_missing:
        return None
    result = client.mail_create_tag(label_name)
    if isinstance(result, Mapping):
        data = result.get("data") if isinstance(result.get("data"), Mapping) else result
        return str(data.get("id") or data.get("name") or label_name)
    return label_name


# ─── Label Parsing ────────────────────────────────────────────

def parse_labels(raw_labels: list[dict[str, Any]]) -> dict[str, Any]:
    """Parse raw Gmail labels into structured form.

    Separates system labels from user labels, detects hierarchy,
    and infers categories.

    Returns:
    {
        "system_labels": [...],
        "user_labels": [...],
        "nested_user_labels": [...],
        "ignored_labels": [...],
        "groups": {parent: [label_names]},
        "total": int,
    }
    """
    system = []
    user = []
    nested = []
    ignored = []
    groups: dict[str, list[str]] = {}

    for label in raw_labels:
        name = label.get("name", "")
        label_type = label.get("type", "user")
        label_id = label.get("id", "")

        parsed = {
            "id": label_id,
            "name": name,
            "type": label_type,
            "path": name.split("/") if "/" in name else [name],
            "depth": name.count("/") + 1 if name else 1,
            "parent": name.split("/")[0] if "/" in name else None,
            "leaf": name.split("/")[-1] if "/" in name else name,
            "message_count": label.get("messageCount") or label.get("messagesTotal") or 0,
            "unread_count": label.get("messagesUnread") or 0,
        }

        # Infer category
        cat, conf = infer_category(name, parsed["path"])
        parsed["inferred_category"] = cat
        parsed["inferred_confidence"] = round(conf, 2)

        if label_type == "system" or name.upper() in SYSTEM_LABELS:
            parsed["type"] = "system"
            system.append(parsed)
            if name in IGNORED_LABEL_NAMES:
                ignored.append(parsed)
        else:
            user.append(parsed)
            if "/" in name:
                nested.append(parsed)
                parent = name.split("/")[0]
                groups.setdefault(parent, []).append(name)

    return {
        "system_labels": system,
        "user_labels": user,
        "nested_user_labels": nested,
        "ignored_labels": ignored,
        "groups": groups,
        "total": len(raw_labels),
    }


# ─── Policy Generation ───────────────────────────────────────

def generate_policy(
    parsed: dict[str, Any],
    provider: str = "unknown",
) -> dict[str, Any]:
    """Generate a proposed email organisation policy from parsed labels.

    Uses existing labels first. No new labels proposed by default.
    """
    categories: dict[str, Any] = {}
    unmapped: list[dict[str, str]] = []

    for label in parsed["user_labels"]:
        cat = label.get("inferred_category")
        conf = label.get("inferred_confidence", 0)

        if cat and conf >= 0.60:
            if cat not in categories:
                categories[cat] = {
                    "preferred_label": label["name"],
                    "label_id": label["id"],
                    "confidence": conf,
                    "source": "existing_label",
                    "aliases": [label["leaf"]],
                }
            else:
                # Multiple labels match same category — keep highest confidence
                existing = categories[cat]
                if conf > existing.get("confidence", 0):
                    existing["preferred_label"] = label["name"]
                    existing["label_id"] = label["id"]
                    existing["confidence"] = conf
                if label["leaf"] not in existing["aliases"]:
                    existing["aliases"].append(label["leaf"])
        else:
            unmapped.append({
                "name": label["name"],
                "reason": "No confident category match" if not cat else f"Low confidence ({conf:.0%})",
            })

    return {
        "version": 1,
        "mode": "use_existing_first",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "status": "proposed",
        "source": {
            "provider": provider,
            "label_count": parsed["total"],
            "user_label_count": len(parsed["user_labels"]),
            "system_label_count": len(parsed["system_labels"]),
        },
        "categories": categories,
        "unmapped_labels": unmapped,
        "new_label_policy": {
            "default": "approval_required",
            "create_only_if": {
                "min_matching_emails": 5,
                "min_days_observed": 14,
                "no_existing_label_fits": True,
            },
        },
        "safety": {
            "allow_auto_create_labels": False,
            "allow_auto_apply_labels": False,
            "allow_auto_archive": False,
            "allow_auto_trash": False,
        },
    }


# ─── Policy Storage ──────────────────────────────────────────


def _get_default_project_root_fallback() -> Path:
    """Default project root for fallback paths (env-configurable).

    Returns <hermes_home>/projects/default, NOT <hermes_home> itself,
    so state files like .events.json go under projects/default/ not
    polluting the Hermes home root.
    """
    env = os.getenv("CHIEF_OF_STAFF_HERMES_HOME") or os.getenv("HERMES_HOME")
    home = Path(env).expanduser() if env else Path.home() / ".hermes"
    return home / "projects" / "default"

def _project_root(config: Any) -> Path:
    root = None
    if isinstance(config, Mapping):
        paths = config.get("paths", {})
        if isinstance(paths, Mapping):
            root = paths.get("project_root")
    if not root:
        root = os.getenv("CHIEF_OF_STAFF_PROJECT_ROOT",
                         str(_get_default_project_root_fallback()))
    return Path(str(root)).expanduser()


def proposal_path(config: Any) -> Path:
    return _project_root(config) / ".email_organisation_policy.proposal.json"


def policy_path(config: Any) -> Path:
    return _project_root(config) / ".email_organisation_policy.json"


def save_proposal(config: Any, policy: dict[str, Any]) -> Path:
    """Save a proposed policy (not approved)."""
    path = proposal_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def save_approved_policy(config: Any, policy: dict[str, Any], approved_by: str) -> Path:
    """Save an approved policy from a proposal."""
    path = policy_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    policy["status"] = "approved"
    policy["approved_by"] = approved_by
    policy["approved_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(policy, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def load_proposal(config: Any) -> dict[str, Any] | None:
    path = proposal_path(config)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_policy(config: Any) -> dict[str, Any] | None:
    path = policy_path(config)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def validate_policy(policy: dict[str, Any]) -> list[str]:
    """Validate a policy dict. Returns list of errors (empty = valid)."""
    errors = []
    required_top = {"version", "mode", "status", "categories", "safety"}
    for field in required_top:
        if field not in policy:
            errors.append(f"Missing required field: {field}")

    safety = policy.get("safety", {})
    for flag in ("allow_auto_create_labels", "allow_auto_apply_labels",
                 "allow_auto_archive", "allow_auto_trash"):
        if flag in safety and safety[flag] is not False:
            errors.append(f"safety.{flag} must be False")

    if policy.get("mode") and policy["mode"] != "use_existing_first":
        errors.append(f"mode must be 'use_existing_first', got '{policy['mode']}'")

    return errors