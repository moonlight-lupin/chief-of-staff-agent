"""Signed approvals: decisions only the human-facing Hermes surface can make.

Under Hermes the agent has a terminal. Without this module it could run
``review_queue.py approve`` and approve its own proposal. So when the Hermes
gateway loads the plugin it creates an Ed25519 key **in memory** (never on
disk, never in any environment) and *enrols* each store it serves: the public
key is recorded in the store, and from then on the store is enforced:

* ``requested → approved`` and ``requested → dismissed`` need a signature
  over (action id, approval fingerprint, company, decision, approver);
* ``approved → executing`` re-verifies it, so an approved row edited
  afterwards, or an approval made before enrolment, never runs;
* break-glass env flags and the TTY prompt stop counting as approval
  (see ``workspace_guardrails.execution_grant``).

Only ``/cos approve`` / ``/<name> approve`` running inside the gateway can
sign. Stores that never enrol (standalone CLI, Claude Code) are unchanged.

Limits, stated plainly (docs/SECURITY_MODEL.md): this stops an agent from
approving through the plugin's own commands. It is not a sandbox: an agent
with an unrestricted shell as the same OS user can still tamper with the
store, or call a provider API directly with credentials it can reach. Use a
Hermes isolated terminal backend for that boundary.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping

STORE = "approval_signing"
DECISIONS = frozenset({"approve", "dismiss"})
MAX_KEYS = 50


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _message(action: Mapping[str, Any], decision: str, approver: str, issued_at: str, key_id: str) -> bytes:
    from state_db import approval_hash

    body = {
        "v": 1, "id": str(action.get("id") or ""), "decision": decision,
        "approval_hash": approval_hash(action), "company_id": action.get("company_id") or None,
        "approver": str(approver or "").strip(), "issued_at": issued_at, "key_id": key_id,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _key_id(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:16]


class GatewaySigner:
    """An in-memory Ed25519 key held by the Hermes gateway process."""

    def __init__(self, private_key: Any) -> None:
        from cryptography.hazmat.primitives import serialization

        self._key = private_key
        self.public_raw = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.key_id = _key_id(self.public_raw)

    @classmethod
    def generate(cls) -> "GatewaySigner":
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        return cls(Ed25519PrivateKey.generate())

    def enrol(self, config: Any) -> None:
        """Record this key's public half in the store; enforcement starts now."""
        from state_db import _open_db

        now = datetime.now(timezone.utc).isoformat()

        def apply(data: dict[str, Any]) -> None:
            keys = data.setdefault("keys", {})
            keys.setdefault(self.key_id, {"public": _b64(self.public_raw), "registered_at": now,
                                          "registered_by": "hermes-gateway"})
            if len(keys) > MAX_KEYS:   # keep the newest keys; older approvals must be re-approved
                for old in sorted(keys, key=lambda k: keys[k]["registered_at"])[:-MAX_KEYS]:
                    keys.pop(old)
            data["enforced"] = True
            data.setdefault("enrolled_at", now)

        with _open_db(config) as db:
            db.mutate_kv(STORE, apply)

    def sign_decision(self, action: Mapping[str, Any], decision: str, approver: str, config: Any = None) -> str:
        if decision not in DECISIONS:
            raise ValueError(f"unknown decision {decision!r}")
        issued_at = datetime.now(timezone.utc).isoformat()
        sig = self._key.sign(_message(action, decision, approver, issued_at, self.key_id))
        return json.dumps({"v": 1, "key_id": self.key_id, "issued_at": issued_at, "decision": decision,
                           "approver": str(approver or "").strip(), "sig": _b64(sig)}, sort_keys=True)

    def private_bytes_for_tests(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return self._key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                       serialization.NoEncryption())


# ---------------------------------------------------------------- verifying


def signing_state(data: Mapping[str, Any] | None) -> tuple[bool, Mapping[str, Any]]:
    data = data or {}
    return bool(data.get("enforced")), data.get("keys") or {}


def _state(config: Any) -> Mapping[str, Any]:
    from state_db import _open_db

    with _open_db(config) as db:
        return db.get_kv(STORE) or {}


def enforced(config: Any) -> bool:
    try:
        return signing_state(_state(config))[0]
    except Exception:
        return False


def verify_with(store_data: Mapping[str, Any] | None, action: Mapping[str, Any], decision: str,
                approver: str, signature: str | None) -> str | None:
    """None when valid; otherwise a short reason. Pure: the caller supplies the store data."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not signature:
        return "approval_unsigned"
    try:
        sig = json.loads(signature)
        key_id, issued_at = str(sig["key_id"]), str(sig["issued_at"])
        raw_sig = base64.b64decode(sig["sig"])
    except (ValueError, KeyError, TypeError):
        return "approval_signature_invalid"
    if sig.get("decision") != decision or str(sig.get("approver") or "") != str(approver or "").strip():
        return "approval_signature_invalid"
    _, keys = signing_state(store_data)
    entry = keys.get(key_id)
    if not entry:
        return "approval_signature_invalid"
    try:
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(entry["public"]))
        public.verify(raw_sig, _message(action, decision, approver, issued_at, key_id))
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return "approval_signature_invalid"
    return None


def verify_decision(config: Any, action: Mapping[str, Any], decision: str, approver: str,
                    signature: str | None) -> str | None:
    return verify_with(_state(config), action, decision, approver, signature)
