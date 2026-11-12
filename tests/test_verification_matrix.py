#!/usr/bin/env python3
"""Live-integration verification matrix (P1.1).

Every supported provider action carries one verification level:
``live`` (executed against a real account, with dated evidence), ``catalog``
(the vendor's catalog lists the tool, but it has never been executed),
``mocked`` (unit tests against a mocked transport only) or ``unverified``.
These tests stop a ``live`` label being added without evidence, keep the
matrix in step with the capability table, and pin the evidence-recording
harness and the no-silent-retry contract for writes.
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

import verification_matrix as vm  # noqa: E402
import workspace_capabilities as caps  # noqa: E402


# ── the matrix itself ───────────────────────────────────────────────────────

class TestMatrixIntegrity:
    def test_matrix_validates_clean(self):
        assert vm.validate() == []

    def test_levels_are_the_documented_four(self):
        assert vm.LEVELS == ("live", "catalog", "mocked", "unverified")

    @pytest.mark.parametrize("provider", sorted(vm.EVIDENCE))
    def test_every_entry_names_a_supported_action(self, provider):
        for action in vm.EVIDENCE[provider]:
            assert caps.supports(provider, action), (
                f"{provider}/{action} has evidence but the capability table says unsupported")

    @pytest.mark.parametrize("provider", sorted(vm.EVIDENCE))
    def test_live_entries_carry_a_past_date_and_evidence(self, provider):
        today = datetime.date.today()
        for action, entry in vm.EVIDENCE[provider].items():
            if entry["level"] != "live":
                continue
            date = vm.parse_date(entry["date"])
            assert date is not None, f"{provider}/{action} has no usable date"
            assert date <= today, f"{provider}/{action} dated in the future"
            assert len(entry["evidence"].strip()) >= 20, f"{provider}/{action} evidence too thin"

    def test_catalog_entries_name_their_source(self):
        for provider, entries in vm.EVIDENCE.items():
            for action, entry in entries.items():
                if entry["level"] == "catalog":
                    assert entry.get("source"), f"{provider}/{action} catalog entry has no source"

    def test_every_provider_in_the_capability_table_is_covered(self):
        for provider in caps.CAPABILITIES:
            # Every provider resolves to a summary, even with no evidence at all.
            summary = vm.provider_summary(provider)
            assert summary["provider"] == provider


class TestValidateRefusesBadEvidence:
    """validate() is the gate: these are the mistakes it must catch."""

    def _with(self, monkeypatch, provider, action, entry):
        evidence = {p: dict(e) for p, e in vm.EVIDENCE.items()}
        evidence.setdefault(provider, {})[action] = entry
        monkeypatch.setattr(vm, "EVIDENCE", evidence)

    def test_live_without_date(self, monkeypatch):
        self._with(monkeypatch, "m365", "mail.search", {"level": "live", "evidence": "x" * 40})
        assert any("m365/mail.search" in p and "date" in p for p in vm.validate())

    def test_live_without_evidence(self, monkeypatch):
        self._with(monkeypatch, "m365", "mail.search", {"level": "live", "date": "2026-07-01", "evidence": ""})
        assert any("m365/mail.search" in p and "evidence" in p for p in vm.validate())

    def test_live_with_future_date(self, monkeypatch):
        future = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
        self._with(monkeypatch, "m365", "mail.search",
                   {"level": "live", "date": future, "evidence": "ran against a tenant " * 3})
        assert any("future" in p for p in vm.validate())

    def test_live_with_malformed_date(self, monkeypatch):
        self._with(monkeypatch, "m365", "mail.search",
                   {"level": "live", "date": "July 2026", "evidence": "ran against a tenant " * 3})
        assert any("date" in p for p in vm.validate())

    def test_entry_for_unsupported_action(self, monkeypatch):
        self._with(monkeypatch, "m365", "files.untrash",
                   {"level": "live", "date": "2026-07-01", "evidence": "ran against a tenant " * 3})
        assert any("m365/files.untrash" in p and "unsupported" in p for p in vm.validate())

    def test_unknown_level(self, monkeypatch):
        self._with(monkeypatch, "m365", "mail.search", {"level": "probably", "evidence": "x"})
        assert any("level" in p for p in vm.validate())

    def test_unknown_provider(self, monkeypatch):
        self._with(monkeypatch, "nonesuch", "mail.search",
                   {"level": "live", "date": "2026-07-01", "evidence": "ran against a tenant " * 3})
        assert any("nonesuch" in p for p in vm.validate())

    def test_catalog_without_source(self, monkeypatch):
        self._with(monkeypatch, "m365", "mail.search", {"level": "catalog"})
        assert any("source" in p for p in vm.validate())

    def test_legacy_key_is_refused(self, monkeypatch):
        """Evidence uses neutral keys only, so one action cannot have two entries."""
        self._with(monkeypatch, "google_api", "gmail.send",
                   {"level": "live", "date": "2026-07-01", "evidence": "sent to self " * 3})
        assert any("gmail.send" in p and "neutral" in p for p in vm.validate())


# ── levels and provider summaries ───────────────────────────────────────────

class TestLevels:
    def test_unsupported_action_reports_unsupported(self):
        assert vm.level("m365", "files.untrash") == "unsupported"

    def test_supported_without_evidence_is_mocked(self):
        assert vm.level("m365", "mail.send") == "mocked"

    def test_legacy_key_resolves(self):
        assert vm.level("google_api", "gmail.draft") == vm.level("google_api", "mail.draft") == "live"

    def test_alias_providers_share_evidence(self):
        assert vm.level("composio:mcp", "mail.send") == vm.level("composio", "mail.send") == "live"
        assert vm.level("composio_microsoft:mcp", "mail.send") == "live"

    def test_unknown_provider_is_unsupported_everywhere(self):
        assert vm.level("nonesuch", "mail.send") == "unsupported"


class TestProviderSummary:
    def test_m365_has_no_live_actions_and_is_not_verified(self):
        summary = vm.provider_summary("m365")
        assert summary["provider_verified"] is False
        assert summary["counts"]["live"] == 0
        assert summary["counts"]["mocked"] > 0

    def test_composio_microsoft_is_verified_with_live_writes(self):
        summary = vm.provider_summary("composio_microsoft")
        assert summary["provider_verified"] is True
        assert summary["actions"]["mail.send"]["level"] == "live"
        assert summary["actions"]["mail.send"]["date"]

    def test_counts_cover_every_supported_action(self):
        summary = vm.provider_summary("composio")
        supported = [a for a, ok in caps.CAPABILITIES["composio"].items() if ok]
        assert sum(summary["counts"].values()) == len(supported)
        assert set(summary["actions"]) == set(supported)

    def test_agent_provider_is_delegated_not_live(self):
        """The agent provider does no I/O; the connector does. Say so — no 'live' claim."""
        summary = vm.provider_summary("agent")
        assert summary["provider_verified"] is True
        assert summary["counts"]["live"] == 0
        assert "connector" in summary["note"].lower()

    def test_unverified_provider_note_explains(self):
        assert "never" in vm.provider_summary("m365")["note"].lower()

    def test_summary_is_json_serialisable(self):
        for provider in caps.CAPABILITIES:
            json.dumps(vm.provider_summary(provider))


# ── capabilities report ─────────────────────────────────────────────────────

class TestCapabilityReport:
    def _report(self, provider, **workspace):
        import chief_of_staff

        config = {"integrations": {"workspace": {"provider": provider, **workspace}}}
        return chief_of_staff.build_capability_report(config)

    def test_report_carries_the_matrix(self):
        report = self._report("m365")
        assert report["verification"]["counts"]["live"] == 0
        assert report["verification"]["actions"]["mail.send"]["level"] == "mocked"

    def test_provider_verified_is_derived_from_the_matrix(self):
        assert self._report("m365")["provider_verified"] is False
        assert self._report("google_api")["provider_verified"] is True

    def test_composio_microsoft_family_uses_microsoft_evidence(self):
        report = self._report("composio", mode="mcp", family="microsoft")
        assert report["verification"]["provider"].startswith("composio_microsoft")

    def test_summary_prints_verification_counts(self, capsys, monkeypatch, tmp_path):
        import chief_of_staff

        monkeypatch.setattr(chief_of_staff, "load_config",
                            lambda *_a, **_k: {"integrations": {"workspace": {"provider": "m365"}}})
        args = MagicMock(summary=True, config=None)
        chief_of_staff.cmd_capabilities(args)
        out = capsys.readouterr().out
        assert "Verification" in out
        assert "0 live" in out


# ── evidence recording (the opt-in live harness) ────────────────────────────

def _report(**statuses):
    checks = {name: {"status": "not_tested", "detail": ""} for name in vm.CHECK_ACTIONS}
    for name, status in statuses.items():
        checks[name] = {"status": status, "detail": "ok"}
    return {"provider": "m365", "checks": checks, "read_ready": True, "write_ready": "yes"}


class TestEvidenceRecording:
    def _config(self, tmp_path):
        return {"paths": {"project_root": str(tmp_path)},
                "integrations": {"workspace": {"provider": "m365"}}}

    def test_writes_a_dated_file_under_the_project_root(self, tmp_path):
        path = vm.record_evidence(self._config(tmp_path), _report(mail_read="pass"), version="0.9.0")
        assert path.parent == tmp_path / "verify" / "evidence"
        data = json.loads(path.read_text())
        assert data["provider"] == "m365"
        assert data["plugin_version"] == "0.9.0"
        assert data["date"] == datetime.date.today().isoformat()
        assert data["checks"]["mail_read"]["status"] == "pass"

    def test_never_overwrites_previous_evidence(self, tmp_path):
        cfg = self._config(tmp_path)
        first = vm.record_evidence(cfg, _report(mail_read="pass"), version="0.9.0")
        second = vm.record_evidence(cfg, _report(mail_read="pass"), version="0.9.0")
        assert first != second and first.exists() and second.exists()

    def test_evidence_carries_no_free_text_details(self, tmp_path):
        """Check details can quote provider errors; evidence keeps status only."""
        report = _report(mail_read="pass")
        report["checks"]["mail_read"]["detail"] = "Subject: payroll for alice@example.com"
        path = vm.record_evidence(self._config(tmp_path), report, version="0.9.0")
        assert "alice@example.com" not in path.read_text()

    def test_promotion_candidates_map_passing_checks_to_actions(self, tmp_path):
        path = vm.record_evidence(self._config(tmp_path),
                                  _report(mail_read="pass", files_write="pass", mail_draft="fail"),
                                  version="0.9.0")
        candidates = vm.promotion_candidates(path)
        actions = {c["action"] for c in candidates}
        assert {"mail.search", "files.upload", "files.download", "files.trash"} <= actions
        assert "mail.draft" not in actions  # failed
        assert "mail.send" not in actions   # the harness never sends
        assert all(c["provider"] == "m365" and c["date"] for c in candidates)

    def test_already_live_actions_are_not_candidates(self, tmp_path):
        cfg = {"paths": {"project_root": str(tmp_path)}}
        report = _report(mail_draft="pass")
        report["provider"] = "google_api"
        path = vm.record_evidence(cfg, report, version="0.9.0")
        assert "mail.draft" not in {c["action"] for c in vm.promotion_candidates(path)}

    def test_recording_never_edits_the_matrix(self, tmp_path):
        before = json.dumps(vm.EVIDENCE, sort_keys=True)
        path = vm.record_evidence(self._config(tmp_path), _report(mail_read="pass"), version="0.9.0")
        vm.promotion_candidates(path)
        assert json.dumps(vm.EVIDENCE, sort_keys=True) == before

    def test_check_actions_cover_every_verify_check(self):
        import workspace_verify

        assert set(vm.CHECK_ACTIONS) == set(workspace_verify.CHECK_NAMES)
        assert vm.CHECK_ACTIONS["mail_send"] == ()


class TestVerifyCliRecordsEvidence:
    def test_record_flag_writes_evidence(self, tmp_path, capsys):
        import connect_workspace

        config = {"paths": {"project_root": str(tmp_path)},
                  "integrations": {"workspace": {"provider": "m365"}}}
        with patch("workspace_verify.run_verification", return_value=_report(mail_read="pass")):
            rc = connect_workspace.cmd_verify(config, record_evidence=True)
        assert rc == 0
        files = list((tmp_path / "verify" / "evidence").glob("*.json"))
        assert len(files) == 1
        assert "Evidence recorded" in capsys.readouterr().out

    def test_default_records_nothing(self, tmp_path):
        import connect_workspace

        config = {"paths": {"project_root": str(tmp_path)},
                  "integrations": {"workspace": {"provider": "m365"}}}
        with patch("workspace_verify.run_verification", return_value=_report(mail_read="pass")):
            connect_workspace.cmd_verify(config)
        assert not (tmp_path / "verify" / "evidence").exists()

    def test_parser_exposes_the_flag(self):
        import connect_workspace

        args = connect_workspace.build_parser().parse_args(["--verify", "--record-evidence"])
        assert args.record_evidence is True


# ── no silent retry of a write ──────────────────────────────────────────────

class TestNoSilentRetry:
    """A write whose outcome is unknown must surface, never be re-sent."""

    def _client(self, monkeypatch):
        monkeypatch.setenv("COMPOSIO_MCP_KEY", "test-key")
        from mcp_client import MCPClient

        client = MCPClient("https://connect.composio.dev/mcp")
        client._initialized = True
        client._session_id = "sess"
        return client

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_mcp_tool_call_is_not_retried_on_server_error(self, monkeypatch, status):
        client = self._client(monkeypatch)
        resp = MagicMock(status_code=status, text="boom")
        with patch("mcp_client.requests.post", return_value=resp) as post:
            with pytest.raises(ConnectionError):
                client.call_tool("GMAIL_SEND_EMAIL", {"to": "x"})
        assert post.call_count == 1

    def test_mcp_tool_call_is_not_retried_on_timeout(self, monkeypatch):
        import requests

        client = self._client(monkeypatch)
        with patch("mcp_client.requests.post", side_effect=requests.Timeout("slow")) as post:
            with pytest.raises(requests.Timeout):
                client.call_tool("GMAIL_SEND_EMAIL", {"to": "x"})
        assert post.call_count == 1

    def test_session_rejection_is_retried_once_only(self, monkeypatch):
        """A 404 'unknown session' was rejected before the tool ran: one re-send, no more."""
        client = self._client(monkeypatch)
        rejected = MagicMock(status_code=404, text="session not found")
        calls = []

        def post(url, headers=None, json=None, timeout=None):
            calls.append(json.get("method"))
            return rejected

        monkeypatch.setattr(client, "initialize", lambda: None)
        with patch("mcp_client.requests.post", side_effect=post):
            with pytest.raises(ConnectionError):
                client.call_tool("GMAIL_SEND_EMAIL", {"to": "x"})
        assert calls.count("tools/call") == 2

    @pytest.mark.parametrize("status", [503, 504])
    def test_m365_post_is_not_retried_on_ambiguous_failure(self, status):
        from providers import m365_graph

        assert "POST" not in m365_graph.IDEMPOTENT_METHODS
        assert status in m365_graph.RETRYABLE_STATUS

    def _fail_once(self, tmp_path, action_type, target="x@example.com", payload=None):
        import state_db

        config = {"paths": {"project_root": str(tmp_path)}}
        with state_db._open_db(config) as db:
            action_id = db.create_action(action_type, "google_api", target,
                                         payload or {"to": target})["id"]
        state_db.approve_pending_action(config, action_id, approver="op")
        state_db.mark_executing(config, action_id)
        return state_db.mark_failed(config, action_id, "timeout")

    @pytest.mark.parametrize("action_type", sorted(
        __import__("state_db").NON_IDEMPOTENT_ACTIONS))
    def test_failed_non_idempotent_action_is_final(self, tmp_path, action_type):
        """A send that timed out may have gone out: never hand it back as approved."""
        failed = self._fail_once(tmp_path, action_type)
        assert failed["state"] == "failed"
        assert failed["failed_at"]

    def test_send_types_are_non_idempotent(self):
        import state_db

        for action_type in ("gmail.send", "mail.send", "calendar.create", "files.upload"):
            assert action_type in state_db.NON_IDEMPOTENT_ACTIONS

    def test_idempotent_action_still_returns_for_a_manual_retry(self, tmp_path):
        """Archiving twice is harmless, so the documented manual retry stays."""
        failed = self._fail_once(tmp_path, "gmail.archive", target="msg-1", payload={"message_id": "msg-1"})
        assert failed["state"] == "approved"
        assert failed["retry_count"] == 1
