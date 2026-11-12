"""Phase 4 contract tests: spend envelopes (docs/MULTI_COMPANY.md, decision D3).

A human approves an envelope (purpose, topic, cap, expiry) once. Paid calls on
that topic within the cap are approved by the envelope. Exceeding the cap, a
different topic, an expired or tampered envelope all come back to the human.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

import config_loader
import spend
import state_db
from test_notion_mirror import _config

SPEND = """\
spend:
  currency: USD
  monid:
    monthly_cap: 100
"""


@pytest.fixture
def config(tmp_path):
    return _config(tmp_path, notion=SPEND)


def _approved(config, cap=50, topic="sg-fintech-leads", days=30, endpoints=None):
    env = spend.propose(config, "monid", "Find SG fintech prospects", topic, cap, "USD",
                        days=days, endpoints=endpoints)
    state_db.approve_pending_action(config, env["id"], approver="Alicia", reason="ok")
    return env["id"]


def _audit(config) -> str:
    return (config_loader.get_project_root(config) / ".audit" / "workspace.log").read_text()


# ---------------------------------------------------------------- propose


def test_propose_queues_a_high_risk_envelope_for_approval(config):
    import action_risk

    env = spend.propose(config, "monid", "Find SG fintech prospects", "sg-fintech-leads", 50, "USD", days=30)
    assert env["type"] == "data.spend_envelope" and env["state"] == "requested"
    assert env["payload"]["cap"] == 50 and env["payload"]["topic"] == "sg-fintech-leads"
    assert env["payload"]["expires_at"] > datetime.now(timezone.utc).isoformat()
    assert action_risk.get_action_risk("data.spend_envelope") == "high"


@pytest.mark.parametrize("kwargs, match", [
    ({"cap": 0}, "positive"),
    ({"cap": 500}, "monthly"),
    ({"currency": "SGD"}, "currency"),
    ({"topic": "Not A Slug!"}, "topic"),
])
def test_propose_refuses_bad_envelopes(config, kwargs, match):
    args = {"provider": "monid", "purpose": "p", "topic": "t", "cap": 10, "currency": "USD"}
    args.update(kwargs)
    with pytest.raises(spend.SpendError, match=match):
        spend.propose(config, **args)


def test_one_open_envelope_per_topic(config):
    _approved(config)
    with pytest.raises(spend.SpendError, match="already"):
        spend.propose(config, "monid", "again", "sg-fintech-leads", 10, "USD")


# ---------------------------------------------------------------- reserve


def test_reservation_needs_an_approved_envelope(config):
    env = spend.propose(config, "monid", "p", "t", 10, "USD")
    with pytest.raises(spend.SpendRefused) as exc:
        spend.reserve(config, env["id"], "t", 1, endpoint="serp")
    assert exc.value.code == "not_approved"


def test_reservation_within_cap_is_approved_by_the_envelope(config):
    env_id = _approved(config)
    res = spend.reserve(config, env_id, "sg-fintech-leads", 20, endpoint="serp", note="page 1")
    assert res["remaining"] == 30
    assert f"envelope:{env_id}" in _audit(config)


@pytest.mark.parametrize("topic, estimate, endpoint, code", [
    ("sg-fintech-leads", 51, "serp", "cap_exceeded"),
    ("other-topic", 1, "serp", "topic_mismatch"),
    ("sg-fintech-leads", 1, "linkedin", "endpoint_not_allowed"),
    ("sg-fintech-leads", -1, "serp", "invalid_amount"),
])
def test_reservation_refusals(config, topic, estimate, endpoint, code):
    env_id = _approved(config, endpoints=["serp"])
    with pytest.raises(spend.SpendRefused) as exc:
        spend.reserve(config, env_id, topic, estimate, endpoint=endpoint)
    assert exc.value.code == code
    assert spend.status(config, env_id)[0]["reserved"] == 0
    assert "refused" in _audit(config)


def test_expired_envelope_refuses(config, monkeypatch):
    env_id = _approved(config, days=1)
    later = datetime.now(timezone.utc) + timedelta(days=2)
    monkeypatch.setattr(spend, "_now", lambda: later)
    with pytest.raises(spend.SpendRefused) as exc:
        spend.reserve(config, env_id, "sg-fintech-leads", 1, endpoint="serp")
    assert exc.value.code == "expired"


def test_envelope_stays_valid_past_the_usual_approval_window(config, monkeypatch):
    env_id = _approved(config, days=30)
    later = datetime.now(timezone.utc) + timedelta(days=3)  # > 24h approval window
    monkeypatch.setattr(spend, "_now", lambda: later)
    assert spend.reserve(config, env_id, "sg-fintech-leads", 1, endpoint="serp")["remaining"] == 49


def test_tampered_envelope_is_refused(config):
    env_id = _approved(config)
    db = config_loader.get_project_root(config) / "state.db"
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT payload FROM pending_actions WHERE id=?", (env_id,)).fetchone()
        payload = json.loads(row[0])
        payload["cap"] = 10_000
        conn.execute("UPDATE pending_actions SET payload=? WHERE id=?", (json.dumps(payload), env_id))
    with pytest.raises(spend.SpendRefused) as exc:
        spend.reserve(config, env_id, "sg-fintech-leads", 60, endpoint="serp")
    assert exc.value.code == "integrity"


def test_envelope_from_another_company_is_refused(config):
    env_id = _approved(config)
    config["company"]["id"] = "other-co"
    with pytest.raises(spend.SpendRefused) as exc:
        spend.reserve(config, env_id, "sg-fintech-leads", 1, endpoint="serp")
    assert exc.value.code in {"company_mismatch", "integrity"}


def test_monthly_cap_spans_envelopes(config):
    a = _approved(config, cap=60, topic="topic-a")
    b = _approved(config, cap=60, topic="topic-b")
    spend.reserve(config, a, "topic-a", 60, endpoint="serp")
    spend.reserve(config, b, "topic-b", 40, endpoint="serp")
    with pytest.raises(spend.SpendRefused) as exc:
        spend.reserve(config, b, "topic-b", 1, endpoint="serp")
    assert exc.value.code == "monthly_cap_exceeded"


def test_concurrent_reservations_never_exceed_the_cap(config):
    env_id = _approved(config, cap=50)
    results: list[str] = []

    def worker():
        try:
            spend.reserve(config, env_id, "sg-fintech-leads", 15, endpoint="serp")
            results.append("ok")
        except spend.SpendRefused as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 3
    assert spend.status(config, env_id)[0]["reserved"] == 45


# ---------------------------------------------------------------- record / release / close


def test_recording_the_actual_cost_frees_the_difference(config):
    env_id = _approved(config)
    res = spend.reserve(config, env_id, "sg-fintech-leads", 20, endpoint="serp")
    spend.record(config, res["reservation_id"], actual=12, run_id="run-1")
    (env,) = spend.status(config, env_id)
    assert env["spent"] == 12 and env["reserved"] == 0 and env["remaining"] == 38


def test_overrun_is_recorded_and_exhausts_the_envelope(config):
    env_id = _approved(config, cap=50)
    res = spend.reserve(config, env_id, "sg-fintech-leads", 45, endpoint="serp")
    out = spend.record(config, res["reservation_id"], actual=55)
    assert out["over_cap"] is True
    assert spend.status(config, env_id)[0]["spent"] == 55
    with pytest.raises(spend.SpendRefused) as exc:
        spend.reserve(config, env_id, "sg-fintech-leads", 0.01, endpoint="serp")
    assert exc.value.code == "cap_exceeded"
    assert "over_cap" in _audit(config)


def test_failed_call_releases_the_reservation(config):
    env_id = _approved(config)
    res = spend.reserve(config, env_id, "sg-fintech-leads", 20, endpoint="serp")
    spend.record(config, res["reservation_id"], actual=0, status="failure")
    assert spend.status(config, env_id)[0]["remaining"] == 50


def test_a_reservation_is_recorded_once(config):
    env_id = _approved(config)
    res = spend.reserve(config, env_id, "sg-fintech-leads", 20, endpoint="serp")
    spend.record(config, res["reservation_id"], actual=5)
    with pytest.raises(spend.SpendError, match="already"):
        spend.record(config, res["reservation_id"], actual=5)


def test_close_ends_the_envelope_with_a_summary(config):
    env_id = _approved(config)
    res = spend.reserve(config, env_id, "sg-fintech-leads", 20, endpoint="serp")
    spend.record(config, res["reservation_id"], actual=7)
    out = spend.close(config, env_id)
    assert out["spent"] == 7
    assert state_db.get_pending_action(config, env_id)["state"] in {"executed", "expired"}
    with pytest.raises(spend.SpendRefused):
        spend.reserve(config, env_id, "sg-fintech-leads", 1, endpoint="serp")


def test_close_refuses_with_open_reservations(config):
    env_id = _approved(config)
    spend.reserve(config, env_id, "sg-fintech-leads", 20, endpoint="serp")
    with pytest.raises(spend.SpendError, match="open"):
        spend.close(config, env_id)


# ---------------------------------------------------------------- surfaces


def test_cli_reserve_refusal_tells_the_agent_to_come_back(config, capsys):
    env_id = _approved(config)
    rc = spend.main(["--config", str(config.source_path), "reserve", "--envelope", env_id,
                     "--topic", "something-else", "--estimate", "1", "--endpoint", "serp"])
    assert rc == 3
    err = capsys.readouterr().err
    assert "topic_mismatch" in err and "propose" in err

    assert spend.main(["--config", str(config.source_path), "reserve", "--envelope", env_id,
                       "--topic", "sg-fintech-leads", "--estimate", "5", "--endpoint", "serp"]) == 0
    assert json.loads(capsys.readouterr().out)["remaining"] == 45


def test_preview_explains_the_envelope(config):
    import review_queue

    env = spend.propose(config, "monid", "Find SG fintech prospects", "sg-fintech-leads", 50, "USD")
    effect = review_queue._expected_effect(env["type"], env["target"], env["payload"])
    assert "50" in effect and "USD" in effect and "sg-fintech-leads" in effect


def test_daily_briefing_shows_spend_and_hides_active_envelopes_from_approvals(config, monkeypatch):
    import importlib.util
    from pathlib import Path

    monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config.source_path))
    env_id = _approved(config)
    res = spend.reserve(config, env_id, "sg-fintech-leads", 20, endpoint="serp")
    spend.record(config, res["reservation_id"], actual=8)

    script = Path(__file__).resolve().parents[1] / "skills" / "daily-briefing" / "scripts" / "daily_briefing.py"
    s = importlib.util.spec_from_file_location("daily_briefing_spend", script)
    daily = importlib.util.module_from_spec(s)
    s.loader.exec_module(daily)
    briefing = daily._build_structured_briefing(str(config.source_path))

    sp = briefing["sections"]["spend"]
    assert sp["month_to_date"]["monid"] == 8
    assert sp["envelopes"][0]["remaining"] == 42
    approvals = [a["action_id"] for group in briefing["sections"]["pending_approvals"].values() for a in group]
    assert env_id not in approvals

    from briefing_renderer import render
    assert "Spend" in render(briefing, "text") and "42" in render(briefing, "text")


def test_doctor_warns_on_stale_reservations(config, monkeypatch):
    import doctor_base

    monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config.source_path))
    env_id = _approved(config)
    spend.reserve(config, env_id, "sg-fintech-leads", 20, endpoint="serp")
    later = datetime.now(timezone.utc) + timedelta(hours=30)
    monkeypatch.setattr(spend, "_now", lambda: later)
    results = {r.name: r for r in doctor_base.run_checks()}
    assert results["spend_reservations"].status == "warn"
