#!/usr/bin/env python3
"""Contract tests for Phase 2 Loop 1: tasks 2.1, 2.5, 2.6 + Opus deferred.

2.1: Transactional state_store — load→mutate→save under one lock
2.5: HMAC webhook timestamp — sign timestamp.body, reject skew > 300s
2.6: store_name path-escape guard — reject ../ and absolute paths

Opus deferred fixes:
  O-Major-3: ConcurrencyError retry loop for mark_executed/mark_failed
  O-Major-5: Replay cache processing lease (crashed worker recovery)
  O-Minor-1: mail.list / files.delete neutral/legacy parity gaps
  O-Minor-2: Directory fsync after rename (pending_actions, event_store)
  O-Minor-3: Version can go backwards when expected_version=None
"""

import sys
import os
import json
import time
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


@pytest.fixture
def temp_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = {
        "google": {"delegate_email": "test@test.com", "account_alias": "test"},
        "integrations": {"workspace": {"provider": "google_api"}},
        "paths": {"project_root": str(project)},
    }
    return config, project


# ═══════════════════════════════════════════════════════════════
# 2.1: Transactional state_store
# ═══════════════════════════════════════════════════════════════

class TestTransactionalStateStore:
    """save_store_atomic must hold the lock across load→mutate→save.

    Currently the lock covers only the write, not the load. Two processes
    can both load the same version, both mutate, and one overwrites the
    other. The fix: add a with_store_lock context manager or hold the
    lock across the full transaction.
    """

    def test_concurrent_saves_no_lost_data(self, temp_project):
        """Two concurrent saves to the same store must not lose either write."""
        from state_store import load_store, save_store_atomic
        config, project = temp_project

        # Initialize with empty pipeline
        data1 = load_store("pipeline", config=config)
        data1["deals"].append({"id": "deal-1", "name": "Deal 1"})
        save_store_atomic("pipeline", data1, config=config, _fill_defaults=True)

        # Second load + append (simulating concurrent process that loaded
        # before the first save completed)
        data2 = load_store("pipeline", config=config)
        data2["deals"].append({"id": "deal-2", "name": "Deal 2"})
        save_store_atomic("pipeline", data2, config=config, _fill_defaults=True)

        # Both deals must be present
        loaded = load_store("pipeline", config=config)
        deal_ids = [d["id"] for d in loaded["deals"]]
        assert "deal-1" in deal_ids, "First deal was lost (concurrent write race)"
        assert "deal-2" in deal_ids, "Second deal was lost (concurrent write race"

    def test_with_store_lock_context_manager(self, temp_project):
        """with_store_lock should exist and protect load→mutate→save."""
        from state_store import load_store, save_store_atomic
        config, project = temp_project

        # Initialize
        data = load_store("pipeline", config=config)
        data["deals"].append({"id": "deal-x", "name": "Deal X"})
        save_store_atomic("pipeline", data, config=config, _fill_defaults=True)

        # Try using with_store_lock if it exists
        try:
            from state_store import with_store_lock
        except ImportError:
            pytest.fail("with_store_lock context manager must exist for transactional access")

        # Use it: load→mutate→save inside the lock
        with with_store_lock("pipeline", config=config):
            data = load_store("pipeline", config=config)
            data["deals"].append({"id": "deal-y", "name": "Deal Y"})
            save_store_atomic("pipeline", data, config=config, _fill_defaults=True)

        loaded = load_store("pipeline", config=config)
        deal_ids = [d["id"] for d in loaded["deals"]]
        assert "deal-x" in deal_ids
        assert "deal-y" in deal_ids


# ═══════════════════════════════════════════════════════════════
# 2.5: HMAC webhook timestamp
# ═══════════════════════════════════════════════════════════════

class TestHMACTimestamp:
    """HMAC webhook signatures must include a timestamp to prevent replay.

    Currently sign_payload signs only the body. A captured request can be
    replayed indefinitely after the 24h replay cache expires.

    Fix: sign timestamp.body, require X-Webhook-Timestamp header, reject
    skew > 300s.
    """

    def test_sign_payload_with_timestamp(self):
        """sign_payload should accept a timestamp and include it in the signature."""
        from webhook_security import sign_payload, verify_signature
        secret = "test-secret-key-123456"
        body = b'{"event": "test"}'
        timestamp = str(int(time.time()))

        # New API: sign_payload(body, secret, timestamp=timestamp)
        sig = sign_payload(body, secret, timestamp=timestamp)
        assert sig is not None
        # The signature should be different from body-only signing
        sig_no_ts = sign_payload(body, secret)
        assert sig != sig_no_ts, "Timestamp must be part of the signature"

    def test_verify_signature_with_timestamp(self):
        """verify_signature should accept a timestamp and verify against it."""
        from webhook_security import sign_payload, verify_signature
        secret = "test-secret-key-123456"
        body = b'{"event": "test"}'
        timestamp = str(int(time.time()))

        sig = sign_payload(body, secret, timestamp=timestamp)
        assert verify_signature(body, sig, secret=secret, timestamp=timestamp)

    def test_verify_rejects_old_timestamp(self):
        """verify_signature should reject timestamps older than 300s."""
        from webhook_security import sign_payload, verify_signature
        secret = "test-secret-key-123456"
        body = b'{"event": "test"}'
        old_timestamp = str(int(time.time()) - 600)  # 10 minutes ago

        sig = sign_payload(body, secret, timestamp=old_timestamp)
        # Even with correct signature, old timestamp should be rejected
        result = verify_signature(body, sig, secret=secret, timestamp=old_timestamp)
        assert result is False, "Must reject timestamps older than 300s"

    def test_verify_rejects_missing_timestamp(self):
        """verify_signature should reject when timestamp is required but missing."""
        from webhook_security import verify_signature
        secret = "test-secret-key-123456"
        body = b'{"event": "test"}'

        # Without timestamp, verification should fail (when timestamp is required)
        # We need a way to signal "require timestamp" — either a flag or
        # the function always requires it when a secret is set
        result = verify_signature(body, "fake-sig", secret=secret)
        assert result is False

    def test_verify_accepts_recent_timestamp(self):
        """verify_signature should accept timestamps within 300s skew."""
        from webhook_security import sign_payload, verify_signature
        secret = "test-secret-key-123456"
        body = b'{"event": "test"}'
        timestamp = str(int(time.time()) - 60)  # 1 minute ago

        sig = sign_payload(body, secret, timestamp=timestamp)
        assert verify_signature(body, sig, secret=secret, timestamp=timestamp)


# ═══════════════════════════════════════════════════════════════
# 2.6: store_name path-escape guard
# ═══════════════════════════════════════════════════════════════

class TestStoreNamePathEscape:
    """store_name must not allow path escape via ../ or absolute paths.

    Currently get_store_path does root / f"{store_name}.yaml" with no
    validation. A store_name like "../../etc/passwd" would escape the
    project root.
    """

    def test_rejects_dotdot_path(self, temp_project):
        """store_name with ../ must be rejected."""
        from state_store import get_store_path, StateStoreError
        config, project = temp_project

        with pytest.raises((StateStoreError, ValueError, RuntimeError)):
            get_store_path("../../etc/passwd", config=config)

    def test_rejects_absolute_path(self, temp_project):
        """Absolute path as store_name must be rejected."""
        from state_store import get_store_path, StateStoreError
        config, project = temp_project

        with pytest.raises((StateStoreError, ValueError, RuntimeError)):
            get_store_path("/etc/passwd", config=config)

    def test_rejects_empty_name(self, temp_project):
        """Empty store_name must be rejected."""
        from state_store import get_store_path, StateStoreError
        config, project = temp_project

        with pytest.raises((StateStoreError, ValueError)):
            get_store_path("", config=config)

    def test_rejects_slash_in_name(self, temp_project):
        """store_name with forward slashes must be rejected."""
        from state_store import get_store_path, StateStoreError
        config, project = temp_project

        with pytest.raises((StateStoreError, ValueError, RuntimeError)):
            get_store_path("subdir/store", config=config)

    def test_accepts_valid_name(self, temp_project):
        """Valid store names must work normally."""
        from state_store import get_store_path
        config, project = temp_project

        path = get_store_path("pipeline", config=config)
        assert path.name == "pipeline.yaml"
        assert path.parent == project.resolve() or path.parent == project


# ═══════════════════════════════════════════════════════════════
# O-Major-3: ConcurrencyError retry loop for mark_executed/mark_failed
# ═══════════════════════════════════════════════════════════════

class TestConcurrencyErrorRetry:
    """mark_executed and mark_failed must retry on ConcurrencyError.

    Currently _save raises ConcurrencyError if the version changed since
    load. No caller retries. If the provider side-effect already happened
    (email sent), the action stays in 'executing' state — a sweeper can
    re-send it.

    Fix: wrap load→mutate→save in a bounded retry loop (3 attempts).
    """

    def test_mark_executed_retries_on_conflict(self, temp_project):
        """mark_executed must retry when _save raises ConcurrencyError."""
        from pending_actions import (
            create_pending_action, approve_pending_action,
            mark_executing, mark_executed, get_pending_action,
        )
        config, project = temp_project

        action = create_pending_action(
            config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"}
        )
        approve_pending_action(config, action["id"])
        mark_executing(config, action["id"])

        # Simulate a concurrent write that bumps the version between
        # mark_executed's load and save by patching _save to fail once
        from pending_actions import _save
        original_save = _save
        call_count = {"n": 0}

        def flaky_save(cfg, data, expected_version=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                from pending_actions import ConcurrencyError
                raise ConcurrencyError("Simulated concurrent write")
            return original_save(cfg, data, expected_version=None)

        with patch("pending_actions._save", side_effect=flaky_save):
            result = mark_executed(config, action["id"], {"success": True})

        assert result is not None, "mark_executed must retry and succeed"
        assert result["state"] == "executed"
        assert call_count["n"] >= 2, "Must have retried at least once"

    def test_mark_executed_gives_up_after_max_retries(self, temp_project):
        """mark_executed must stop retrying after 3 attempts and not crash."""
        from pending_actions import (
            create_pending_action, approve_pending_action,
            mark_executing, mark_executed, ConcurrencyError,
        )
        config, project = temp_project

        action = create_pending_action(
            config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"}
        )
        approve_pending_action(config, action["id"])
        mark_executing(config, action["id"])

        # Always raise ConcurrencyError
        with patch("pending_actions._save",
                   side_effect=ConcurrencyError("Always fail")):
            # Must not raise — must return None or handle gracefully
            try:
                result = mark_executed(config, action["id"], {"success": True})
                # If it returns None, the action stays in 'executing' state
                # (acceptable — better than crashing or double-sending)
            except ConcurrencyError:
                pytest.fail("mark_executed must not propagate ConcurrencyError after max retries")


# ═══════════════════════════════════════════════════════════════
# O-Major-5: Replay cache processing lease
# ═══════════════════════════════════════════════════════════════

class TestReplayCacheLease:
    """A crashed worker's 'processing' reservation must expire, not block for 24h.

    Currently reserve_delivery writes state='processing' with no lease.
    If the worker dies, every subsequent redelivery gets rejected until
    the full 24h TTL elapses — the webhook is silently dropped.

    Fix: give 'processing' a short lease (e.g. 5 min). A reservation
    older than the lease can be reclaimed by a new reserve_delivery call.
    """

    def test_stale_processing_reservation_can_be_reclaimed(self, temp_project):
        """A processing reservation older than the lease can be reclaimed."""
        from webhook_security import reserve_delivery, _load_replay_cache
        import time as _time
        config, project = temp_project

        # Reserve a delivery
        ok, _ = reserve_delivery(config, "delivery-stale")
        assert ok

        # Manually age the reservation past the lease (5 min default)
        cache = _load_replay_cache(config)
        cache["entries"]["delivery-stale"]["ts"] = _time.time() - 400  # 6+ min ago
        from webhook_security import _save_replay_cache_unlocked
        _save_replay_cache_unlocked(config, cache)

        # A new reserve_delivery for the same ID should succeed (reclaim)
        ok2, reason = reserve_delivery(config, "delivery-stale")
        assert ok2 is True, f"Stale processing reservation must be reclaimable. Got: {reason}"

    def test_fresh_processing_reservation_blocks(self, temp_project):
        """A fresh processing reservation must still block duplicate delivery."""
        from webhook_security import reserve_delivery
        config, project = temp_project

        ok1, _ = reserve_delivery(config, "delivery-fresh")
        ok2, reason = reserve_delivery(config, "delivery-fresh")
        assert ok1 is True
        assert ok2 is False, "Fresh processing reservation must block duplicate"


# ═══════════════════════════════════════════════════════════════
# O-Minor-1: Neutral/legacy action ID parity gaps
# ═══════════════════════════════════════════════════════════════

class TestActionIDParity:
    """Neutral and legacy action ID spellings must be paired in both sets.

    Opus found: READ_ACTIONS has 'gmail.list' but no 'mail.list'.
    WRITE_ACTIONS has 'drive.delete' but no 'files.delete'.
    If any provider emits the missing spelling, it gets hard-denied.
    """

    def test_mail_list_in_read_actions(self):
        """mail.list must be in READ_ACTIONS (pairing with gmail.list)."""
        from workspace_guardrails import READ_ACTIONS
        assert "mail.list" in READ_ACTIONS, (
            "mail.list missing from READ_ACTIONS — m365 provider will be hard-denied"
        )

    def test_files_delete_in_write_actions(self):
        """files.delete must be in WRITE_ACTIONS (pairing with drive.delete)."""
        from workspace_guardrails import WRITE_ACTIONS
        assert "files.delete" in WRITE_ACTIONS, (
            "files.delete missing from WRITE_ACTIONS — m365 provider will bypass or be denied"
        )


# ═══════════════════════════════════════════════════════════════
# O-Minor-2: Directory fsync after rename
# ═══════════════════════════════════════════════════════════════

class TestDirectoryFsync:
    """_save in pending_actions and event_store must fsync the parent directory.

    Currently only the temp file is fsynced, not the directory after rename.
    A power loss can lose the rename despite fsync on the file.
    """

    def test_pending_actions_save_fsyncs_directory(self, temp_project):
        """pending_actions._save must fsync the parent directory after replace."""
        from pending_actions import create_pending_action
        config, project = temp_project

        fsync_dir_calls = []
        original_fsync = os.fsync
        original_open = os.open

        def spy_open(path, flags, *args, **kwargs):
            fd = original_open(path, flags, *args, **kwargs)
            if flags & os.O_DIRECTORY:
                fsync_dir_calls.append(str(path))
            return fd

        with patch("os.open", side_effect=spy_open):
            create_pending_action(
                config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"}
            )

        assert len(fsync_dir_calls) >= 1, (
            "pending_actions._save must fsync the parent directory after rename"
        )

    def test_event_store_save_fsyncs_directory(self, temp_project):
        """event_store._save must fsync the parent directory after replace."""
        from event_store import ingest_event
        config, project = temp_project

        fsync_dir_calls = []
        original_open = os.open

        def spy_open(path, flags, *args, **kwargs):
            fd = original_open(path, flags, *args, **kwargs)
            if flags & os.O_DIRECTORY:
                fsync_dir_calls.append(str(path))
            return fd

        with patch("os.open", side_effect=spy_open):
            ingest_event(config, "gmail", "msg-001", "email_received", {"from": "a@b.com"})

        assert len(fsync_dir_calls) >= 1, (
            "event_store._save must fsync the parent directory after rename"
        )


# ═══════════════════════════════════════════════════════════════
# O-Minor-3: Version can go backwards when expected_version=None
# ═══════════════════════════════════════════════════════════════

class TestVersionMonotonicity:
    """_save with expected_version=None must not move the version backwards.

    Currently new_version is derived from the caller's possibly-stale
    in-memory data, not from disk. If the on-disk version is higher,
    the save can write a lower version number.
    """

    def test_version_never_goes_backwards(self, temp_project):
        """Version must be monotonically increasing even with stale data."""
        from pending_actions import create_pending_action, _load, _save
        config, project = temp_project

        # Create an action (version goes to 1)
        create_pending_action(
            config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"}
        )

        # Load stale snapshot (version=1)
        stale = _load(config)
        assert stale["_version"] == 1

        # Meanwhile, another write bumps version to 2
        create_pending_action(
            config, "gmail.send", "google_api", "c@d.com", {"to": "c@d.com"}
        )
        fresh = _load(config)
        assert fresh["_version"] == 2

        # Now save the stale snapshot with expected_version=None
        # The version must NOT go backwards to 2 (from stale's 1+1=2)
        # It should be 3 (from disk's 2+1=3)
        stale["actions"]["stale-entry"] = {"id": "stale-entry", "state": "requested"}
        _save(config, stale, expected_version=None)

        loaded = _load(config)
        assert loaded["_version"] >= 3, (
            f"Version went backwards: expected >=3, got {loaded['_version']}. "
            "The version must be derived from disk, not from stale in-memory data."
        )