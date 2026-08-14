#!/usr/bin/env python3
"""Webhook security — authentication, replay protection, payload validation.

Security model:
- Gmail Pub/Sub: OIDC JWT validation via Authorization: Bearer <jwt>
  Config: CHIEF_OF_STAFF_PUBSUB_AUDIENCE, CHIEF_OF_STAFF_PUBSUB_SERVICE_ACCOUNT
- Calendar/Drive: X-Goog-Channel-Token (fail-closed if not configured)
- Generic: HMAC-SHA256 via X-Webhook-Signature (CHIEF_OF_STAFF_WEBHOOK_SECRET)
- Replay: delivery-ID-based, atomic cache, reserve-before-ingest

The webhook receiver NEVER executes, approves, or mutates anything.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import file_lock


# ─── HMAC (generic endpoint) ──────────────────────────────────

def get_webhook_secret() -> str | None:
    return os.getenv("CHIEF_OF_STAFF_WEBHOOK_SECRET")


def sign_payload(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, signature: str, secret: str | None = None) -> bool:
    if secret is None:
        secret = get_webhook_secret()
    if not secret:
        return False
    expected = sign_payload(body, secret)
    return hmac.compare_digest(expected, signature)


# ─── Pub/Sub OIDC JWT (Gmail endpoint) ────────────────────────

def get_pubsub_audience() -> str | None:
    return os.getenv("CHIEF_OF_STAFF_PUBSUB_AUDIENCE")


def get_pubsub_service_account() -> str | None:
    return os.getenv("CHIEF_OF_STAFF_PUBSUB_SERVICE_ACCOUNT")


def verify_pubsub_oidc(authorization_header: str | None) -> tuple[bool, str]:
    """Verify a Gmail Pub/Sub push notification OIDC JWT.

    Google Pub/Sub sends an OIDC token in the Authorization header:
      Authorization: Bearer <jwt>

    Uses google-auth library to cryptographically verify the JWT signature
    against Google's public keys, plus validates audience, issuer,
    service account email, and email_verified claims.

    Returns (is_valid, reason).
    """
    if not authorization_header:
        return False, "Missing Authorization header"

    if not authorization_header.startswith("Bearer "):
        return False, "Authorization header must be 'Bearer <jwt>'"
    token = authorization_header.removeprefix("Bearer ").strip()

    audience = get_pubsub_audience()
    expected_email = get_pubsub_service_account()

    if not audience or not expected_email:
        return False, "Pub/Sub OIDC configuration incomplete (set CHIEF_OF_STAFF_PUBSUB_AUDIENCE and CHIEF_OF_STAFF_PUBSUB_SERVICE_ACCOUNT)"

    try:
        from google.oauth2 import id_token
        from google.auth.transport.requests import Request
    except ImportError:
        return False, "google-auth library not installed (pip install google-auth>=2.38)"

    try:
        claims = id_token.verify_oauth2_token(
            token,
            Request(),
            audience=audience,
            clock_skew_in_seconds=30,
        )
    except Exception as exc:
        return False, f"JWT verification failed: {exc}"

    # Verify service account email
    token_email = claims.get("email", "")
    if token_email != expected_email:
        return False, f"Unexpected service account: expected {expected_email}, got {token_email}"

    # Verify email_verified
    if claims.get("email_verified") is not True:
        return False, "Service account email is not verified"

    # Verify issuer
    issuer = claims.get("iss", "")
    if issuer not in ("accounts.google.com", "https://accounts.google.com"):
        return False, f"Unexpected issuer: {issuer}"

    return True, "OK"


# ─── Calendar/Drive Channel Token (fail-closed) ──────────────

def get_channel_token() -> str | None:
    return os.getenv("CHIEF_OF_STAFF_WEBHOOK_CHANNEL_TOKEN")


def verify_channel_token(token: str | None) -> bool:
    """Verify Google Calendar/Drive channel token. FAIL-CLOSED.

    If no token is configured, returns False (endpoint disabled).
    """
    configured = get_channel_token()
    if not configured:
        return False  # Fail-closed — no token configured
    if not token:
        return False
    return hmac.compare_digest(token, configured)


def is_channel_token_configured() -> bool:
    """Check if channel token is configured (for startup warnings)."""
    return bool(get_channel_token())


# ─── Payload Validation ──────────────────────────────────────

def validate_gmail_pubsub_payload(payload: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """Validate Gmail Pub/Sub envelope and decode inner data.

    Returns (is_valid, reason, decoded_data).
    On failure, decoded_data is empty dict.
    """
    msg = payload.get("message")
    if not isinstance(msg, dict):
        return False, "Missing 'message' object in Pub/Sub envelope", {}
    if "data" not in msg:
        return False, "Missing 'message.data' in Pub/Sub envelope", {}
    if "messageId" not in msg:
        return False, "Missing 'message.messageId' in Pub/Sub envelope", {}

    try:
        decoded_bytes = base64.urlsafe_b64decode(msg["data"] + "=" * (-len(msg["data"]) % 4))
        decoded = json.loads(decoded_bytes)
    except Exception as exc:
        return False, f"Failed to decode message.data: {exc}", {}

    if "emailAddress" not in decoded:
        return False, "Decoded payload missing 'emailAddress'", decoded
    if "historyId" not in decoded:
        return False, "Decoded payload missing 'historyId'", decoded

    return True, "OK", decoded


def validate_calendar_headers(headers: dict[str, str]) -> tuple[bool, str]:
    """Validate required Calendar push headers."""
    required = ["X-Goog-Channel-ID", "X-Goog-Message-Number",
                "X-Goog-Resource-ID", "X-Goog-Resource-State"]
    for h in required:
        if not headers.get(h):
            return False, f"Missing required header: {h}"
    return True, "OK"


def validate_drive_headers(headers: dict[str, str]) -> tuple[bool, str]:
    """Validate required Drive push headers."""
    required = ["X-Goog-Channel-ID", "X-Goog-Message-Number",
                "X-Goog-Resource-ID", "X-Goog-Resource-State"]
    for h in required:
        if not headers.get(h):
            return False, f"Missing required header: {h}"
    return True, "OK"


# ─── Delivery-ID-based Replay Protection ─────────────────────

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


def _save_replay_cache_unlocked(config: Any, data: dict[str, Any]) -> None:
    """Write replay cache atomically (caller must already hold the lock)."""
    path = _replay_cache_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_version = (data.get("_version", 0) or 0) + 1
    data["_version"] = new_version
    tmp = path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def reserve_delivery(
    config: Any,
    delivery_id: str,
    ttl_seconds: int = REPLAY_TTL_SECONDS,
) -> tuple[bool, str]:
    """Reserve a delivery ID for processing.

    The entire load-check-mutate-save transaction is under an exclusive
    file lock so two concurrent workers cannot both reserve the same ID.
    """
    path = _replay_cache_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock.with_lock(str(path), timeout=10):
        cache = _load_replay_cache(config)
        now = time.time()
        entries = cache.get("entries", {})

        expired = [k for k, v in entries.items()
                   if now - v.get("ts", 0) > ttl_seconds]
        for k in expired:
            del entries[k]

        entry = entries.get(delivery_id)
        if entry:
            if entry.get("state") == "done":
                return False, "Replay detected: delivery already completed"
            if entry.get("state") == "processing":
                return False, "Replay detected: delivery already processing"
            return False, "Replay detected"
        else:
            entries[delivery_id] = {"state": "processing", "ts": now}
            cache["entries"] = entries
            _save_replay_cache_unlocked(config, cache)
            return True, "OK"


def complete_delivery(config: Any, delivery_id: str) -> None:
    """Mark a delivery as completed."""
    path = _replay_cache_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock.with_lock(str(path), timeout=10):
        cache = _load_replay_cache(config)
        entries = cache.get("entries", {})
        if delivery_id in entries:
            entries[delivery_id]["state"] = "done"
            entries[delivery_id]["ts"] = time.time()
            cache["entries"] = entries
            _save_replay_cache_unlocked(config, cache)


def release_delivery(config: Any, delivery_id: str) -> None:
    """Release a delivery reservation on failure (allows retry)."""
    path = _replay_cache_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock.with_lock(str(path), timeout=10):
        cache = _load_replay_cache(config)
        entries = cache.get("entries", {})
        if delivery_id in entries:
            del entries[delivery_id]
            cache["entries"] = entries
            _save_replay_cache_unlocked(config, cache)


def validate_secret_config() -> dict[str, Any]:
    """Validate webhook security configuration. Returns status dict."""
    issues = []

    # Check HMAC secret (generic endpoint)
    secret = get_webhook_secret()
    if not secret:
        issues.append("CHIEF_OF_STAFF_WEBHOOK_SECRET not set (generic endpoint disabled)")
    elif len(secret) < 16:
        issues.append(f"HMAC secret too short ({len(secret)} chars, min 16)")

    # Check Pub/Sub OIDC (gmail endpoint)
    pubsub_aud = get_pubsub_audience()
    pubsub_sa = get_pubsub_service_account()
    if not pubsub_aud:
        issues.append("CHIEF_OF_STAFF_PUBSUB_AUDIENCE not set (Gmail Pub/Sub endpoint disabled)")
    if not pubsub_sa:
        issues.append("CHIEF_OF_STAFF_PUBSUB_SERVICE_ACCOUNT not set (Gmail Pub/Sub endpoint disabled)")

    # Check channel token (calendar/drive endpoints)
    channel_token = get_channel_token()
    if not channel_token:
        issues.append("CHIEF_OF_STAFF_WEBHOOK_CHANNEL_TOKEN not set (Calendar/Drive endpoints disabled)")

    # Gmail: native Pub/Sub (OIDC) or HMAC fallback (dev/proxy)
    gmail_native = bool(pubsub_aud and pubsub_sa)
    gmail_hmac = bool(secret and len(secret) >= 16)
    if not gmail_native and not gmail_hmac:
        issues.append("Gmail endpoint has no authentication configured (need OIDC or HMAC)")

    return {
        "valid": len(issues) == 0,
        "issues": issues,
        "endpoints": {
            "gmail": "native (OIDC)" if gmail_native else ("HMAC (dev/proxy)" if gmail_hmac else "disabled"),
            "calendar": "enabled" if channel_token else "disabled",
            "drive": "enabled" if channel_token else "disabled",
            "generic": "enabled" if gmail_hmac else "disabled",
        },
        "secret_length": len(secret) if secret else 0,
        "pubsub_audience": "configured" if pubsub_aud else "missing",
        "pubsub_service_account": "configured" if pubsub_sa else "missing",
        "channel_token": "configured" if channel_token else "missing",
    }