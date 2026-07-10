#!/usr/bin/env python3
"""Webhook security — HMAC signature verification and replay protection.

Security model:
- Webhook secret is read from env var CHIEF_OF_STAFF_WEBHOOK_SECRET
- Signatures are HMAC-SHA256 of the raw request body
- Header name: X-Webhook-Signature (hex-encoded)
- Replay protection: track seen signatures in a local file with TTL

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


def sign_payload(body: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature of body with secret. Returns hex string."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, signature: str, secret: str | None = None) -> bool:
    """Verify that the signature matches the body using the secret.

    Uses constant-time comparison to prevent timing attacks.
    Returns False if no secret is available.
    """
    if secret is None:
        secret = get_webhook_secret()
    if not secret:
        return False
    expected = sign_payload(body, secret)
    return hmac.compare_digest(expected, signature)


def _replay_cache_path(config: Any) -> Path:
    from email_label_policy import _project_root  # reuse project root resolver
    root = _project_root(config)
    return root / ".webhook_replay_cache.json"


def _load_replay_cache(config: Any) -> dict[str, float]:
    path = _replay_cache_path(config)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: float(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def _save_replay_cache(config: Any, cache: dict[str, float]) -> None:
    path = _replay_cache_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2), encoding="utf-8")


REPLAY_TTL_SECONDS = 3600 * 24  # 24 hours


def check_replay(
    config: Any,
    signature: str,
    ttl_seconds: int = REPLAY_TTL_SECONDS,
) -> tuple[bool, str]:
    """Check if a signature has been seen before (replay attack).

    Returns (is_valid, reason).
    is_valid=True means this is a NEW request (not a replay).
    is_valid=False means this signature was already seen.
    """
    cache = _load_replay_cache(config)
    now = time.time()

    # Expire old entries
    expired = [k for k, ts in cache.items() if now - ts > ttl_seconds]
    for k in expired:
        del cache[k]

    if signature in cache:
        return False, "Replay detected: signature already seen"

    # Record this signature
    cache[signature] = now
    _save_replay_cache(config, cache)
    return True, "OK"


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
    return {
        "valid": True,
        "length": len(secret),
        "algorithm": "HMAC-SHA256",
        "header": "X-Webhook-Signature",
    }