#!/usr/bin/env python3
"""Webhook receiver — HTTP server that ingests events into event_store.

The receiver:
- Listens on a configurable host/port
- Verifies HMAC-SHA256 signatures
- Protects against replay attacks
- Converts payloads via adapters
- Ingests into event_store (idempotent by source+source_id)
- Optionally generates suggestions after ingestion
- Never executes, approves, or mutates external systems

Safety: the receiver is read-only with respect to external systems.
It can only: verify, parse, store, classify, and suggest.
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))


class WebhookStats:
    """Track receiver statistics in memory."""
    def __init__(self):
        self.received = 0
        self.verified = 0
        self.rejected_signature = 0
        self.rejected_replay = 0
        self.ingested = 0
        self.duplicated = 0
        self.errors = 0


def create_handler(config: Any, stats: WebhookStats, generate_suggestions: bool = False):
    """Create an HTTP request handler class with config and stats bound."""

    class WebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            stats.received += 1

            # Read body
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b""

            # Verify signature
            from webhook_security import verify_signature, check_replay
            signature = self.headers.get("X-Webhook-Signature", "")

            if not verify_signature(body, signature):
                stats.rejected_signature += 1
                self._respond(401, {"error": "Invalid or missing signature"})
                return

            stats.verified += 1

            # Check replay
            is_valid, replay_reason = check_replay(config, signature)
            if not is_valid:
                stats.rejected_replay += 1
                self._respond(409, {"error": replay_reason})
                return

            # Parse payload
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                stats.errors += 1
                self._respond(400, {"error": "Invalid JSON"})
                return

            # Detect provider and adapt
            from webhook_adapters import detect_provider, adapt_payload
            provider = self.headers.get("X-Webhook-Provider", "") or detect_provider(payload)
            event = adapt_payload(provider, payload)

            # Ingest into event_store
            from event_store import ingest_event
            result = ingest_event(
                config,
                source=event["source"],
                source_id=event["source_id"],
                event_type=event["event_type"],
                payload=event["payload"],
            )

            if result is None:
                stats.duplicated += 1
                self._respond(200, {"status": "duplicate"})
                return

            if result.get("status") == "duplicate":
                stats.duplicated += 1
                self._respond(200, {"status": "duplicate", "event_id": result.get("id")})
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

            self._respond(200, {
                "status": "ingested",
                "event_id": result.get("id"),
                "source": event["source"],
                "source_id": event["source_id"],
                "event_type": event["event_type"],
                "suggestions_generated": suggestions_generated,
            })

        def do_GET(self):
            """Health check endpoint."""
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._respond(200, {
                    "status": "healthy",
                    "stats": {
                        "received": stats.received,
                        "verified": stats.verified,
                        "ingested": stats.ingested,
                        "duplicated": stats.duplicated,
                        "rejected_signature": stats.rejected_signature,
                        "rejected_replay": stats.rejected_replay,
                        "errors": stats.errors,
                    },
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
            # Suppress default logging — caller can add their own
            pass

    return WebhookHandler


def start_server(
    config: Any,
    host: str = "0.0.0.0",
    port: int = 8787,
    generate_suggestions: bool = False,
) -> None:
    """Start the webhook receiver HTTP server."""
    from webhook_security import validate_secret_config
    secret_check = validate_secret_config()
    if not secret_check["valid"]:
        print(f"❌ {secret_check['error']}", file=sys.stderr)
        if "hint" in secret_check:
            print(f"   {secret_check['hint']}", file=sys.stderr)
        raise SystemExit(1)

    stats = WebhookStats()
    handler = create_handler(config, stats, generate_suggestions)
    server = HTTPServer((host, port), handler)

    print(f"🌐 Webhook receiver listening on {host}:{port}")
    print(f"   Signature: HMAC-SHA256 via X-Webhook-Signature header")
    print(f"   Replay protection: enabled (24h TTL)")
    print(f"   Suggestion generation: {'enabled' if generate_suggestions else 'disabled'}")
    print(f"   Health check: GET /health")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Shutting down...")
        server.shutdown()
        print(f"   Final stats: {stats.received} received, {stats.ingested} ingested, {stats.duplicated} duplicated")