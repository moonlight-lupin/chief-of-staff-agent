#!/usr/bin/env python3
"""Tests for v0.3.4 — structured operational (runtime) logging.

Covers: run-id format; init/emit/finish round-trip; join-existing-run via env;
child-process env propagation; level filtering (file + console); quiet;
console-only fallback when project_root is missing; log_event never raises;
credential/secret leak prevention at every level; prune by retention_days and
max_runs (non-run entries untouched); contextvars isolation across runs.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

import runtime_log as rl  # noqa: E402


RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_context():
    """Ensure no run context or env var bleeds between tests."""
    rl._CURRENT.set(None)
    saved = {k: os.environ.get(k) for k in (rl.RUN_ID_ENV, rl.LOG_LEVEL_ENV, rl.PROJECT_ROOT_ENV)}
    for k in saved:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        rl._CURRENT.set(None)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / ".runs").mkdir(parents=True)
    return root


def _config(root: Path, **logging_block):
    cfg = {"paths": {"project_root": str(root)}}
    if logging_block:
        cfg["logging"] = logging_block
    return cfg


def _read_events(run_dir: Path) -> list[dict]:
    path = run_dir / "events.jsonl"
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


# ─── run-id format ───────────────────────────────────────────────────────────


def test_run_id_format(project):
    run_id = rl.init_run("daily-briefing", _config(project))
    assert RUN_ID_RE.match(run_id), run_id
    # Timestamp prefix parses as UTC.
    datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ")
    rl.finish_run("success")


# ─── init / emit / finish round-trip ─────────────────────────────────────────


def test_round_trip_schema(project):
    run_id = rl.init_run("daily-briefing", _config(project))
    rl.log_event(
        "request",
        level="info",
        component="m365_graph",
        operation="messages.list",
        status_code=200,
        duration_ms=42,
        result_count=7,
        message="ok",
    )
    rl.log_event("throttled", level="warning", component="m365_graph", attempt=2, status_code=429)
    rl.finish_run("success", pages=3)

    run_dir = project / ".runs" / run_id
    events = _read_events(run_dir)
    # run_started + 2 caller events + run_completed
    kinds = [e["event"] for e in events]
    assert kinds[0] == "run_started"
    assert "request" in kinds and "throttled" in kinds
    assert kinds[-1] == "run_completed"

    for e in events:
        for key in ("timestamp", "level", "run_id", "command", "event"):
            assert key in e
        assert e["run_id"] == run_id
        assert e["command"] == "daily-briefing"
        # timestamp is ISO8601 with tz
        datetime.fromisoformat(e["timestamp"])

    req = next(e for e in events if e["event"] == "request")
    assert req["component"] == "m365_graph"
    assert req["operation"] == "messages.list"
    assert req["result_count"] == 7

    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["run_id"] == run_id
    assert summary["command"] == "daily-briefing"
    assert summary["outcome"] == "success"
    assert summary["pages"] == 3
    assert set(summary["counts"]) == {"error", "warning", "info", "debug"}
    assert summary["counts"]["warning"] == 1
    assert summary["first_error"] is None
    assert summary["warnings"] == ["throttled"]
    for key in ("started_at", "finished_at"):
        datetime.fromisoformat(summary[key])


def test_failed_outcome_emits_run_failed(project):
    run_id = rl.init_run("cmd", _config(project))
    rl.log_event("boom", level="error", component="x", message="kaboom")
    rl.finish_run("failed")
    run_dir = project / ".runs" / run_id
    events = _read_events(run_dir)
    assert events[-1]["event"] == "run_failed"
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["outcome"] == "failed"
    assert summary["counts"]["error"] == 1
    assert summary["first_error"] == "kaboom"


# ─── join existing run via env ───────────────────────────────────────────────


def test_join_existing_run_appends_no_new_dir(project):
    run_id = rl.init_run("parent", _config(project))
    rl.log_event("parent_event", level="info", component="p")
    # Simulate a child process/context joining via env, without finishing parent.
    assert os.environ[rl.RUN_ID_ENV] == run_id

    before = {p.name for p in (project / ".runs").iterdir()}
    joined = rl.init_run("child", _config(project))
    assert joined == run_id
    after = {p.name for p in (project / ".runs").iterdir()}
    assert before == after  # no new dir created on join

    rl.log_event("child_event", level="info", component="c")
    rl.finish_run("success")  # child finish

    events = _read_events(project / ".runs" / run_id)
    kinds = [e["event"] for e in events]
    assert "parent_event" in kinds
    assert "child_event" in kinds
    # child event recorded under the joined run id with the child's command
    child = next(e for e in events if e["event"] == "child_event")
    assert child["run_id"] == run_id
    assert child["command"] == "child"


def test_join_does_not_unset_env_it_did_not_set(project):
    rl.init_run("parent", _config(project))
    run_id = os.environ[rl.RUN_ID_ENV]
    # A joining context finishes; env must remain because it did not set it.
    rl.init_run("child", _config(project))
    rl.finish_run("success")
    assert os.environ.get(rl.RUN_ID_ENV) == run_id


# ─── child-process propagation ───────────────────────────────────────────────


def test_env_propagates_to_children_and_clears_on_finish(project):
    assert rl.RUN_ID_ENV not in os.environ
    run_id = rl.init_run("cmd", _config(project))
    # A subprocess would inherit os.environ, carrying the run id.
    assert os.environ.get(rl.RUN_ID_ENV) == run_id
    rl.finish_run("success")
    # Owner clears it so the next unrelated process starts fresh.
    assert rl.RUN_ID_ENV not in os.environ


# ─── level filtering (file + console) ────────────────────────────────────────


def test_level_filtering_file_and_console(project):
    buf = io.StringIO()
    with redirect_stderr(buf):
        run_id = rl.init_run("cmd", _config(project), level="warning")
        rl.log_event("dbg", level="debug", component="x")
        rl.log_event("inf", level="info", component="x")
        rl.log_event("warn", level="warning", component="x")
        rl.log_event("err", level="error", component="x")
        rl.finish_run("success")

    events = _read_events(project / ".runs" / run_id)
    kinds = {e["event"] for e in events}
    assert "dbg" not in kinds and "inf" not in kinds
    assert "warn" in kinds and "err" in kinds
    # run_started is info -> filtered out at warning level.
    assert "run_started" not in kinds

    console = buf.getvalue()
    assert "warn" in console and "err" in console
    assert "dbg" not in console


def test_console_format_one_liner(project):
    buf = io.StringIO()
    with redirect_stderr(buf):
        rl.init_run("cmd", _config(project))
        rl.log_event("request_throttled", level="warning", component="m365_graph", attempt=2)
        rl.finish_run("success")
    console = buf.getvalue()
    assert "warning m365_graph request_throttled" in console
    assert "attempt=2" in console


# ─── quiet ───────────────────────────────────────────────────────────────────


def test_quiet_silences_console_not_file(project):
    buf = io.StringIO()
    with redirect_stderr(buf):
        run_id = rl.init_run("cmd", _config(project), quiet=True)
        rl.log_event("something", level="warning", component="x")
        rl.finish_run("success")
    assert buf.getvalue().strip() == ""
    events = _read_events(project / ".runs" / run_id)
    assert any(e["event"] == "something" for e in events)


# ─── console-only fallback when project_root missing ─────────────────────────


def test_console_only_fallback_no_exception():
    buf = io.StringIO()
    with redirect_stderr(buf):
        run_id = rl.init_run("cmd", config=None)  # no root resolvable
        assert RUN_ID_RE.match(run_id)
        assert rl.current_run_id() == run_id
        rl.log_event("hello", level="info", component="x")
        rl.finish_run("success")
    # Console still received output; nothing raised.
    assert "hello" in buf.getvalue()


def test_console_only_with_unresolvable_config():
    # config present but project_root cannot resolve -> still no raise, no dir.
    bad_cfg = {"paths": {}}
    buf = io.StringIO()
    with redirect_stderr(buf):
        run_id = rl.init_run("cmd", bad_cfg)
        rl.log_event("x", level="error", component="c", message="m")
        rl.finish_run("failed")
    assert RUN_ID_RE.match(run_id)


# ─── log_event never raises ──────────────────────────────────────────────────


def test_log_event_never_raises_on_unwritable_dir(project, monkeypatch):
    run_id = rl.init_run("cmd", _config(project))
    run_dir = project / ".runs" / run_id

    def _boom(*a, **k):
        raise OSError("disk full")

    # Break file writes entirely; log_event must swallow it.
    monkeypatch.setattr(Path, "open", _boom)
    rl.log_event("still_ok", level="error", component="x", message="m")  # must not raise
    monkeypatch.undo()
    # finish_run also must not raise even though summary write may fail
    rl.finish_run("failed")


def test_log_event_no_active_run_no_raise():
    # No init_run; log_event should quietly print to console and not raise.
    buf = io.StringIO()
    with redirect_stderr(buf):
        rl.log_event("orphan", level="warning", component="x")
    assert "orphan" in buf.getvalue()
    assert rl.current_run_id() is None


# ─── secret-leak prevention ──────────────────────────────────────────────────

_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"


@pytest.mark.parametrize("level", ["debug", "info", "warning", "error"])
def test_no_secret_leak_at_every_level(project, level):
    run_id = rl.init_run("cmd", _config(project), level="debug")
    rl.log_event(
        "http_request",
        level=level,
        component="m365_graph",
        Authorization=f"Bearer {_JWT}",
        authorization_header=f"Bearer {_JWT}",
        access_token="abc123secretvalue",
        api_key="sk-supersecret",
        client_secret="csx-supersecret",
        cookie="session=deadbeef",
        set_cookie="a=b",
        password="hunter2",
        message=f"calling with Bearer {_JWT} and client_secret=csx-shh and password=hunter2",
        headers={"Authorization": f"Bearer {_JWT}", "X-Trace": "ok"},
        body="the full email body with secrets",
        payload={"api_key": "leak", "note": "keep"},
        content="raw content payload",
        snippet="a snippet of the message",
        email_body="Dear founder, ...",
        nested=[{"token": "leak"}, "Bearer " + _JWT, "clean string"],
    )
    rl.finish_run("success")

    raw = (project / ".runs" / run_id / "events.jsonl").read_text()

    # Absolute leak checks: none of these secret values appear anywhere.
    for needle in [
        _JWT,
        "abc123secretvalue",
        "sk-supersecret",
        "csx-supersecret",
        "csx-shh",
        "hunter2",
        "session=deadbeef",
        "the full email body",
        "raw content payload",
        "a snippet of the message",
        "Dear founder",
        '"note": "keep"',  # dropped because payload key is dropped
    ]:
        assert needle not in raw, f"leaked: {needle!r}"

    events = _read_events(project / ".runs" / run_id)
    rec = next(e for e in events if e["event"] == "http_request")
    # Sensitive keys replaced.
    assert rec["Authorization"] == rl.REDACTED
    assert rec["access_token"] == rl.REDACTED
    assert rec["api_key"] == rl.REDACTED
    assert rec["client_secret"] == rl.REDACTED
    assert rec["password"] == rl.REDACTED
    assert rec["cookie"] == rl.REDACTED
    # Dropped keys absent entirely.
    for dropped in ("body", "payload", "content", "snippet", "email_body"):
        assert dropped not in rec
    # Nested dict: sensitive key redacted, benign key kept.
    assert rec["headers"]["Authorization"] == rl.REDACTED
    assert rec["headers"]["X-Trace"] == "ok"
    # Message string scrubbed but partially readable.
    assert "Bearer [redacted]" in rec["message"]
    assert "client_secret=[redacted]" in rec["message"]
    assert "password=[redacted]" in rec["message"]
    # Nested list scrubbed.
    flat = json.dumps(rec["nested"])
    assert _JWT not in flat
    assert "clean string" in flat


def test_reserved_keys_not_overridable(project):
    run_id = rl.init_run("real-command", _config(project))
    rl.log_event("evt", level="info", component="c", run_id="FAKE", command="FAKE", timestamp="FAKE")
    rl.finish_run("success")
    rec = next(e for e in _read_events(project / ".runs" / run_id) if e["event"] == "evt")
    assert rec["run_id"] == run_id
    assert rec["command"] == "real-command"
    assert rec["timestamp"] != "FAKE"


# ─── prune ───────────────────────────────────────────────────────────────────


def _make_run_dir(runs: Path, age_days: int) -> str:
    ts = datetime.now(timezone.utc) - timedelta(days=age_days)
    run_id = f"{ts.strftime('%Y%m%dT%H%M%SZ')}-{age_days:06x}"[:23]
    run_id = f"{ts.strftime('%Y%m%dT%H%M%SZ')}-{age_days % 0xffffff:06x}"
    d = runs / run_id
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text("{}\n")
    return run_id


def test_prune_by_retention_days(project):
    runs = project / ".runs"
    old = _make_run_dir(runs, age_days=40)
    older = _make_run_dir(runs, age_days=100)
    fresh = _make_run_dir(runs, age_days=1)

    result = rl.prune_runs(_config(project, retention_days=30, max_runs=1000))
    assert set(result["removed"]) == {old, older}
    assert (runs / fresh).exists()
    assert not (runs / old).exists()
    assert not (runs / older).exists()


def test_prune_by_max_runs(project):
    runs = project / ".runs"
    ids = [_make_run_dir(runs, age_days=i) for i in range(1, 6)]  # 5 recent dirs
    result = rl.prune_runs(_config(project, retention_days=3650, max_runs=2))
    # Keep 2 newest (smallest age), remove 3 oldest.
    assert result["kept"] == 2
    assert len(result["removed"]) == 3
    remaining = {p.name for p in runs.iterdir() if RUN_ID_RE.match(p.name)}
    assert len(remaining) == 2
    # The two youngest (age 1 and 2) survive.
    assert ids[0] in remaining and ids[1] in remaining


def test_prune_ignores_non_run_entries(project):
    runs = project / ".runs"
    # run_log.py-style skill directory and a stray file must be untouched.
    skill_dir = runs / "daily-briefing"
    skill_dir.mkdir()
    (skill_dir / "20260101T000000.000000Z.json").write_text("{}")
    stray = runs / "notes.txt"
    stray.write_text("keep me")
    old = _make_run_dir(runs, age_days=90)

    result = rl.prune_runs(_config(project, retention_days=30, max_runs=1000))
    assert old in result["removed"]
    assert skill_dir.exists()
    assert (skill_dir / "20260101T000000.000000Z.json").exists()
    assert stray.exists()


def test_prune_defaults_when_no_logging_block(project):
    runs = project / ".runs"
    _make_run_dir(runs, age_days=5)
    result = rl.prune_runs(_config(project))  # defaults: 30 days / 200 runs
    assert result["removed"] == []
    assert result["kept"] == 1


def test_prune_console_only_no_root():
    result = rl.prune_runs({"paths": {}})
    assert result == {"removed": [], "kept": 0}


# ─── contextvars isolation ───────────────────────────────────────────────────


def test_sequential_runs_do_not_bleed(project):
    id1 = rl.init_run("first", _config(project))
    rl.log_event("a", level="info", component="x")
    rl.finish_run("success")
    assert rl.current_run_id() is None

    id2 = rl.init_run("second", _config(project))
    rl.log_event("b", level="info", component="x")
    rl.finish_run("success")

    assert id1 != id2
    e1 = _read_events(project / ".runs" / id1)
    e2 = _read_events(project / ".runs" / id2)
    assert all(e["run_id"] == id1 for e in e1)
    assert all(e["run_id"] == id2 for e in e2)
    assert {e["event"] for e in e1} == {"run_started", "a", "run_completed"}
    assert {e["event"] for e in e2} == {"run_started", "b", "run_completed"}


def test_add_cli_args():
    import argparse

    parser = argparse.ArgumentParser()
    rl.add_cli_args(parser)
    args = parser.parse_args(["--log-level", "debug", "--quiet"])
    assert args.log_level == "debug"
    assert args.quiet is True
    args2 = parser.parse_args([])
    assert args2.log_level is None
    assert args2.quiet is False


def test_level_resolution_precedence(project, monkeypatch):
    # explicit arg wins over env and config
    monkeypatch.setenv(rl.LOG_LEVEL_ENV, "error")
    run_id = rl.init_run("cmd", _config(project, level="debug"), level="warning")
    ctx = rl._CURRENT.get()
    assert ctx.level == "warning"
    rl.finish_run("success")

    # env wins over config when no explicit arg
    run_id2 = rl.init_run("cmd", _config(project, level="debug"))
    ctx2 = rl._CURRENT.get()
    assert ctx2.level == "error"
    rl.finish_run("success")

    monkeypatch.delenv(rl.LOG_LEVEL_ENV, raising=False)
    # config wins when no arg/env
    rl.init_run("cmd", _config(project, level="warning"))
    assert rl._CURRENT.get().level == "warning"
    rl.finish_run("success")

    # default INFO when nothing set
    rl.init_run("cmd", _config(project))
    assert rl._CURRENT.get().level == "info"
    rl.finish_run("success")
