#!/usr/bin/env python3
"""Webhook receiver — HTTP server that ingests events into event_store.

Provider-native support:
- POST /webhooks/gmail     → Gmail (Pub/Sub envelope or direct)
- POST /webhooks/calendar  → Calendar (X-Goog-* headers, empty body)
- POST /webhooks/drive     → Drive (X-Goog-* headers, empty body)
- POST /webhooks/generic   → Generic signed webhooks
- GET  /health             → Health check with stats

Safety flow:
  verify signature → validate channel token (Calendar/Drive)
  → reserve delivery ID → parse/adapt → ingest → complete delivery
  On failure: release delivery (allows retry)

The receiver NEVER executes, approves, or mutates external systems.
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

MAX_BODY_BYTES = 1_048_576  # 1 MB


class WebhookStats:
    """Track receiver statistics in memory."""
    def __init__(self):
        self.received = 0
        self.verified = 0
        self.rejected_signature = 0
        self.rejected_channel_token = 0
        self.rejected_replay = 0
        self.rejected_oversized = 0
        self.rejected_bad_request = 0
        self.ingested = 0
        self.duplicated = 0
        self.errors = 0

    def to_dict(self) -> dict[str, int]:
        return {k: v for k, v in self.__dict__.items()}


def _get_headers_dict(handler) -> dict[str, str]:
    """Extract headers into a plain dict (case-insensitive lookup handled by BaseHTTPRequestHandler)."""
    return {k: v for k, v in handler.headers.items()}


def create_handler(config: Any, stats: WebhookStats, generate_suggestions: bool = False):
    """Create an HTTP request handler class with config and stats bound."""

    class WebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            stats.received += 1
            parsed = urlparse(self.path)
            endpoint = parsed.path

            # Validate endpoint
            valid_endpoints = {"/webhooks/gmail", "/webhooks/calendar",
                               "/webhooks/drive", "/webhooks/generic"}
            if endpoint not in valid_endpoints:
                stats.rejected_bad_request += 1
                self._respond(404, {"error": f"Unknown endpoint: {endpoint}"})
                return

            # Read body with size limit
            content_length_str = self.headers.get("Content-Length", "0")
            try:
                content_length = int(content_length_str)
            except (ValueError, TypeError):
                stats.rejected_bad_request += 1
                self._respond(400, {"error": "Invalid Content-Length"})
                return

            if content_length < 0:
                stats.rejected_bad_request += 1
                self._respond(400, {"error": "Negative Content-Length"})
                return

            if content_length > MAX_BODY_BYTES:
                stats.rejected_oversized += 1
                self._respond(413, {"error": f"Body exceeds {MAX_BODY_BYTES} bytes"})
                return

            body = self.rfile.read(content_length) if content_length > 0 else b""

            # Authentication by endpoint type
            from webhook_validation import (
                verify_signature, verify_channel_token, verify_pubsub_oidc,
            )
            from state_db import reserve_delivery, complete_delivery, release_delivery

            if endpoint == "/webhooks/gmail":
                # Gmail: try OIDC JWT first (native Pub/Sub), fall back to HMAC (dev/proxy)
                auth_header = self.headers.get("Authorization", "")
                if auth_header.startswith("Bearer "):
                    # Native Pub/Sub OIDC
                    ok, reason = verify_pubsub_oidc(auth_header)
                    if not ok:
                        stats.rejected_signature += 1
                        self._respond(401, {"error": f"Pub/Sub OIDC validation failed: {reason}"})
                        return
                else:
                    # Dev/proxy mode with HMAC
                    signature = self.headers.get("X-Webhook-Signature", "")
                    timestamp = self.headers.get("X-Webhook-Timestamp", "")
                    if not verify_signature(body, signature, timestamp=timestamp or None):
                        stats.rejected_signature += 1
                        self._respond(401, {"error": "Invalid or missing signature (no OIDC token or HMAC)"})
                        return

            elif endpoint in ("/webhooks/calendar", "/webhooks/drive"):
                # Calendar/Drive: X-Goog-Channel-Token (fail-closed)
                channel_token = self.headers.get("X-Goog-Channel-Token", "")
                if not verify_channel_token(channel_token):
                    stats.rejected_channel_token += 1
                    self._respond(401, {"error": "Invalid or missing channel token (channel token required, not configured = disabled)"})
                    return

            else:
                # Generic: HMAC signature with timestamp (timestamp required)
                signature = self.headers.get("X-Webhook-Signature", "")
                timestamp = self.headers.get("X-Webhook-Timestamp", "")
                if not verify_signature(body, signature, timestamp=timestamp or None,
                                        require_timestamp=True):
                    stats.rejected_signature += 1
                    self._respond(401, {"error": "Invalid or missing signature"})
                    return

            stats.verified += 1

            # Parse body (may be empty for Calendar/Drive)
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                stats.rejected_bad_request += 1
                self._respond(400, {"error": "Invalid JSON"})
                return

            # Adapt payload via endpoint-specific adapter
            headers_dict = _get_headers_dict(self)
            try:
                from webhook_adapters import adapt_for_endpoint
                event = adapt_for_endpoint(endpoint, payload, headers_dict)
            except ValueError as exc:
                # Validation failure — bad request, not server error
                stats.rejected_bad_request += 1
                self._respond(400, {"error": f"Invalid payload: {exc}"})
                return
            except Exception as exc:
                stats.errors += 1
                self._respond(500, {"error": f"Adapter failure: {exc}"})
                return

            delivery_id = event.get("delivery_id", event.get("source_id", ""))

            # Reserve delivery ID (replay protection)
            reserved = reserve_delivery(config, delivery_id)
            is_valid, replay_reason = reserved
            lease_token = reserved.lease_token
            if not is_valid:
                stats.rejected_replay += 1
                self._respond(409, {"error": replay_reason})
                return

            # Ingest into event_store
            try:
                from state_db import ingest_event
                result = ingest_event(
                    config,
                    source=event["source"],
                    source_id=event["source_id"],
                    event_type=event["event_type"],
                    payload=event["payload"],
                )
            except Exception as exc:
                # Release delivery on failure — allows retry
                release_delivery(config, delivery_id, lease_token=lease_token)
                stats.errors += 1
                self._respond(500, {"error": f"Ingestion failure: {exc}"})
                return

            if result is None:
                # Duplicate in event_store (already ingested before)
                complete_delivery(config, delivery_id, lease_token=lease_token)
                stats.duplicated += 1
                self._respond(200, {"status": "duplicate", "event_id": None})
                return

            stats.ingested += 1

            # Optionally generate suggestions (read-only, no execution)
            suggestions_generated = 0
            if generate_suggestions and result.get("id"):
                try:
                    from suggested_actions import generate_for_events
                    sugs = generate_for_events(config, event_ids=[result["id"]])
                    suggestions_generated = len(sugs) if sugs else 0
                except Exception:
                    pass  # Suggestion generation failure is non-fatal

            # Mark delivery as completed
            complete_delivery(config, delivery_id, lease_token=lease_token)

            self._respond(200, {
                "status": "ingested",
                "event_id": result.get("id"),
                "source": event["source"],
                "source_id": event["source_id"],
                "event_type": event["event_type"],
                "delivery_id": delivery_id,
                "suggestions_generated": suggestions_generated,
            })

        def do_GET(self):
            """Health check endpoint."""
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._respond(200, {
                    "status": "healthy",
                    "stats": stats.to_dict(),
                })
                return
            self._respond(404, {"error": "Not found"})

        def _respond(self, status: int, data: dict[str, Any]):
            body = json.dumps(data, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args):
            pass

    return WebhookHandler


def start_server(
    config: Any,
    host: str = "0.0.0.0",
    port: int = 8787,
    generate_suggestions: bool = False,
) -> None:
    """Start the webhook receiver HTTP server."""
    from webhook_validation import validate_secret_config
    check = validate_secret_config()

    if check["issues"]:
        print("⚠️  Configuration warnings:")
        for issue in check["issues"]:
            print(f"   {issue}")
        print()

    endpoints = check.get("endpoints", {})
    print(f"🌐 Webhook receiver listening on {host}:{port}")
    print("   Endpoints:")
    for ep, status in endpoints.items():
        icon = "✅" if status == "enabled" else "❌"
        print(f"     {icon} POST /webhooks/{ep} — {status}")
    print("     GET  /health — health check")
    print("   Auth: OIDC JWT (gmail), Channel Token (calendar/drive), HMAC (generic)")
    print("   Replay: delivery-ID-based, 24h TTL, atomic writes")
    print(f"   Max body: {MAX_BODY_BYTES:,} bytes")
    print(f"   Suggestions: {'enabled' if generate_suggestions else 'disabled'}")
    print()

    if not any(status == "enabled" for status in endpoints.values()):
        print("❌ No endpoints enabled — configure at least one auth method.", file=sys.stderr)
        raise SystemExit(1)

    stats = WebhookStats()
    handler = create_handler(config, stats, generate_suggestions)
    server = HTTPServer((host, port), handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Shutting down...")
        server.shutdown()
        print(f"   Final stats: {stats.received} received, {stats.ingested} ingested, {stats.duplicated} duplicated")