#!/usr/bin/env python3
"""Webhook security — HMAC signature verification and delivery-ID-based replay protection.

Security model:
- Webhook secret via CHIEF_OF_STAFF_WEBHOOK_SECRET (HMAC-SHA256)
- Header: X-Webhook-Signature (hex-encoded HMAC of raw body)
- Calendar/Drive: X-Goog-Channel-Token validated against configured token
- Replay protection: delivery-ID-based, not body-signature-based
- Atomic cache writes (temp-file + rename)
- Reservation-before-ingest: reserve ID → process → mark done (or release on failure)

The webhook receiver NEVER executes, approves, or mutates anything.
Security only verifies and deduplicates.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any


def get_webhook_secret() -> str | None:
    """Get the webhook secret from environment."""
    return os.getenv("CHIEF_OF_STAFF_WEBHOOK_SECRET")


def get_channel_token() -> str | None:
    """Get the X-Goog-Channel-Token for Calendar/Drive validation."""
    return os.getenv("CHIEF_OF_STAFF_WEBHOOK_CHANNEL_TOKEN")


def sign_payload(body: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature of body with secret. Returns hex string."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, signature: str, secret: str | None = None) -> bool:
    """Verify HMAC-SHA256 signature using constant-time comparison."""
    if secret is None:
        secret = get_webhook_secret()
    if not secret:
        return False
    expected = sign_payload(body, secret)
    return hmac.compare_digest(expected, signature)


def verify_channel_token(token: str | None) -> bool:
    """Verify Google Calendar/Drive channel token.

    If no token is configured, accepts any token (disabled mode).
    If configured, requires exact match.
    """
    configured = get_channel_token()
    if not configured:
        return True  # Token validation disabled
    if not token:
        return False
    return hmac.compare_digest(token, configured)


# ─── Delivery-ID-based Replay Protection ───────────────────────

REPLAY_TTL_SECONDS = 3600 * 24  # 24 hours


def _replay_cache_path(config: Any) -> Path:
    from email_label_policy import _project_root
    root = _project_root(config)
    return root / ".webhook_replay_cache.json"


def _load_replay_cache(config: Any) -> dict[str, Any]:
    path = _replay_cache_path(config)
    if not path.exists():
        return {"entries": {}, "_version": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "entries" not in data:
            return {"entries": {}, "_version": 0}
        return data
    except (json.JSONDecodeError, OSError):
        return {"entries": {}, "_version": 0}


def _save_replay_cache(config: Any, data: dict[str, Any]) -> None:
    """Atomic write: write to temp file, then rename."""
    path = _replay_cache_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_version = (data.get("_version", 0) or 0) + 1
    data["_version"] = new_version
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def reserve_delivery(
    config: Any,
    delivery_id: str,
    ttl_seconds: int = REPLAY_TTL_SECONDS,
) -> tuple[bool, str]:
    """Reserve a delivery ID for processing.

    Returns (is_valid, reason).
    is_valid=True means this is a NEW delivery (not seen before).
    is_valid=False means this delivery was already seen.

    States: "processing" (reserved but not done) → "done" (completed)
    If an old "processing" entry exists past TTL, it's expired (retryable).
    """
    cache = _load_replay_cache(config)
    now = time.time()
    entries = cache.get("entries", {})

    # Expire old entries
    expired = [k for k, v in entries.items()
               if now - v.get("ts", 0) > ttl_seconds]
    for k in expired:
        del entries[k]

    entry = entries.get(delivery_id)
    if entry:
        if entry.get("state") == "done":
            return False, "Replay detected: delivery already completed"
        if entry.get("state") == "processing":
            # Still processing — reject to prevent concurrent duplicate
            return False, "Replay detected: delivery already processing"
    else:
        # New delivery — reserve it
        entries[delivery_id] = {"state": "processing", "ts": now}
        cache["entries"] = entries
        _save_replay_cache(config, cache)
        return True, "OK"

    return False, "Replay detected"


def complete_delivery(config: Any, delivery_id: str) -> None:
    """Mark a delivery as completed."""
    cache = _load_replay_cache(config)
    entries = cache.get("entries", {})
    if delivery_id in entries:
        entries[delivery_id]["state"] = "done"
        entries[delivery_id]["ts"] = time.time()
        cache["entries"] = entries
        _save_replay_cache(config, cache)


def release_delivery(config: Any, delivery_id: str) -> None:
    """Release a delivery reservation on failure (allows retry)."""
    cache = _load_replay_cache(config)
    entries = cache.get("entries", {})
    if delivery_id in entries:
        del entries[delivery_id]
        cache["entries"] = entries
        _save_replay_cache(config, cache)


def validate_secret_config() -> dict[str, Any]:
    """Validate that webhook secret is configured. Returns status dict."""
    secret = get_webhook_secret()
    if not secret:
        return {
            "valid": False,
            "error": "CHIEF_OF_STAFF_WEBHOOK_SECRET not set",
            "hint": "Export CHIEF_OF_STAFF_WEBHOOK_SECRET=<your-secret> before starting the receiver",
        }
    if len(secret) < 16:
        return {
            "valid": False,
            "error": "Secret too short (minimum 16 characters recommended)",
            "length": len(secret),
        }
    channel_token = get_channel_token()
    return {
        "valid": True,
        "length": len(secret),
        "algorithm": "HMAC-SHA256",
        "header": "X-Webhook-Signature",
        "channel_token": "configured" if channel_token else "disabled",
    }