#!/usr/bin/env python3
"""Webhook payload/header validation and authentication helpers.

Replay-cache functions live in state_db.py. This module holds HMAC, OIDC,
channel-token, and payload/header validation only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any


# ─── HMAC (generic endpoint) ──────────────────────────────────

def get_webhook_secret() -> str | None:
    return os.getenv("CHIEF_OF_STAFF_WEBHOOK_SECRET")


def sign_payload(body: bytes, secret: str, timestamp: str | None = None) -> str:
    if timestamp is not None:
        message = timestamp.encode("utf-8") + b"." + body
    else:
        message = body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_signature(
    body: bytes,
    signature: str,
    secret: str | None = None,
    timestamp: str | None = None,
    require_timestamp: bool = False,
) -> bool:
    if secret is None:
        secret = get_webhook_secret()
    if not secret:
        return False
    if require_timestamp and (timestamp is None or timestamp == ""):
        return False
    if timestamp is not None:
        try:
            skew = abs(int(time.time()) - float(timestamp))
        except (ValueError, TypeError):
            return False
        if skew > 300:
            return False
    if not signature:
        return False
    expected = sign_payload(body, secret, timestamp=timestamp)
    return hmac.compare_digest(expected, signature)


# ─── Pub/Sub OIDC JWT (Gmail endpoint) ────────────────────────

def get_pubsub_audience() -> str | None:
    return os.getenv("CHIEF_OF_STAFF_PUBSUB_AUDIENCE")


def get_pubsub_service_account() -> str | None:
    return os.getenv("CHIEF_OF_STAFF_PUBSUB_SERVICE_ACCOUNT")


def verify_pubsub_oidc(authorization_header: str | None) -> tuple[bool, str]:
    """Verify a Gmail Pub/Sub push notification OIDC JWT.

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
        return False, (
            "Pub/Sub OIDC configuration incomplete "
            "(set CHIEF_OF_STAFF_PUBSUB_AUDIENCE and CHIEF_OF_STAFF_PUBSUB_SERVICE_ACCOUNT)"
        )

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

    token_email = claims.get("email", "")
    if token_email != expected_email:
        return False, f"Unexpected service account: expected {expected_email}, got {token_email}"

    if claims.get("email_verified") is not True:
        return False, "Service account email is not verified"

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
        return False
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


def validate_secret_config() -> dict[str, Any]:
    """Validate webhook security configuration. Returns status dict."""
    issues = []

    secret = get_webhook_secret()
    if not secret:
        issues.append("CHIEF_OF_STAFF_WEBHOOK_SECRET not set (generic endpoint disabled)")
    elif len(secret) < 16:
        issues.append(f"HMAC secret too short ({len(secret)} chars, min 16)")

    pubsub_aud = get_pubsub_audience()
    pubsub_sa = get_pubsub_service_account()
    if not pubsub_aud:
        issues.append("CHIEF_OF_STAFF_PUBSUB_AUDIENCE not set (Gmail Pub/Sub endpoint disabled)")
    if not pubsub_sa:
        issues.append("CHIEF_OF_STAFF_PUBSUB_SERVICE_ACCOUNT not set (Gmail Pub/Sub endpoint disabled)")

    channel_token = get_channel_token()
    if not channel_token:
        issues.append("CHIEF_OF_STAFF_WEBHOOK_CHANNEL_TOKEN not set (Calendar/Drive endpoints disabled)")

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
