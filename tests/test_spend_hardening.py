"""Spend-envelope hardening (P1.2).

Builds on test_spend_envelopes.py. The gaps closed here:

* a recorded overrun of the provider's *monthly* cap is surfaced, not silent;
* one provider's budgets never mix currencies, configured or not;
* a stale reservation has a recovery path that names the two honest outcomes;
* paid prospect data is ingested only with provenance that points at a real,
  approved envelope and a recorded call on it;
* budgets are labelled as a local estimate, not the provider's bill.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import prospecting as pr
import spend
from test_notion_mirror import _config
from test_spend_envelopes import SPEND, _approved, _audit, config  # noqa: F401  (fixture)


# ---------------------------------------------------------------- monthly cap


def test_recorded_monthly_overrun_is_surfaced(config):  # noqa: F811
    a = _approved(config, cap=60, topic="topic-a")
    b = _approved(config, cap=60, topic="topic-b")
    spend.record(config, spend.reserve(config, a, "topic-a", 50, endpoint="serp")["reservation_id"], actual=50)
    res = spend.reserve(config, b, "topic-b", 45, endpoint="serp")
    out = spend.record(config, res["reservation_id"], actual=58)   # 108 > monthly 100
    assert out["over_monthly_cap"] is True
    assert out["over_cap"] is False                                # within its own envelope
    assert "over_monthly_cap" in _audit(config)


def test_monthly_overrun_shows_in_status_and_briefing(config):  # noqa: F811
    a = _approved(config, cap=100, topic="topic-a")
    spend.record(config, spend.reserve(config, a, "topic-a", 95, endpoint="serp")["reservation_id"], actual=120)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    expected = {"monid": {"month": month, "spent": 120, "cap": 100}}
    assert spend.month_over_caps(config) == expected
    assert spend.briefing_summary(config)["over_monthly_caps"] == expected


def test_within_monthly_cap_is_not_flagged(config):  # noqa: F811
    a = _approved(config, cap=50)
    out = spend.record(config, spend.reserve(config, a, "sg-fintech-leads", 10, endpoint="serp")["reservation_id"],
                       actual=9)
    assert out["over_monthly_cap"] is False
    assert spend.month_over_caps(config) == {}


# ---------------------------------------------------------------- currency


NO_CURRENCY = """\
spend:
  monid:
    monthly_cap: 100
"""


@pytest.fixture
def loose(tmp_path):
    return _config(tmp_path, notion=NO_CURRENCY)


def test_unconfigured_currency_still_cannot_mix_for_one_provider(loose):
    spend.propose(loose, "monid", "p", "topic-a", 10, "USD")
    with pytest.raises(spend.SpendError, match="currency"):
        spend.propose(loose, "monid", "p", "topic-b", 10, "SGD")


def test_reserve_refuses_an_estimate_in_another_currency(config):  # noqa: F811
    env_id = _approved(config)
    with pytest.raises(spend.SpendRefused) as exc:
        spend.reserve(config, env_id, "sg-fintech-leads", 5, endpoint="serp", currency="EUR")
    assert exc.value.code == "currency_mismatch"


def test_reserve_accepts_matching_currency_any_case(config):  # noqa: F811
    env_id = _approved(config)
    assert spend.reserve(config, env_id, "sg-fintech-leads", 5, endpoint="serp", currency="usd")["currency"] == "USD"


def test_cli_reserve_takes_a_currency(config, capsys):  # noqa: F811
    env_id = _approved(config)
    rc = spend.main(["--config", str(config.source_path), "reserve", "--envelope", env_id,
                     "--topic", "sg-fintech-leads", "--estimate", "1", "--endpoint", "serp",
                     "--currency", "GBP"])
    assert rc == 3
    assert "currency_mismatch" in capsys.readouterr().err


# ---------------------------------------------------------------- stale reservations


def _age(monkeypatch, hours):
    later = datetime.now(timezone.utc) + timedelta(hours=hours)
    monkeypatch.setattr(spend, "_now", lambda: later)


def test_stale_lists_details_and_both_recovery_commands(config, monkeypatch):  # noqa: F811
    env_id = _approved(config)
    res = spend.reserve(config, env_id, "sg-fintech-leads", 20, endpoint="serp")
    _age(monkeypatch, 30)
    (item,) = spend.stale_details(config)
    assert item["reservation_id"] == res["reservation_id"]
    assert item["envelope_id"] == env_id and item["estimate"] == 20 and item["age_hours"] >= 29
    assert "record" in item["if_the_call_ran"] and "release" in item["if_it_did_not"]


def test_cli_stale_command(config, monkeypatch, capsys):  # noqa: F811
    env_id = _approved(config)
    spend.reserve(config, env_id, "sg-fintech-leads", 20, endpoint="serp")
    _age(monkeypatch, 30)
    assert spend.main(["--config", str(config.source_path), "stale"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["stale"]) == 1 and "provider" in out["note"].lower()


def test_close_with_open_reservations_points_at_recovery(config):  # noqa: F811
    env_id = _approved(config)
    spend.reserve(config, env_id, "sg-fintech-leads", 20, endpoint="serp")
    with pytest.raises(spend.SpendError, match="spend.py stale"):
        spend.close(config, env_id)


# ---------------------------------------------------------------- local estimate


def test_status_says_the_ledger_is_a_local_estimate(config, capsys):  # noqa: F811
    _approved(config)
    assert spend.main(["--config", str(config.source_path), "status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "estimate" in out["note"].lower() and "invoice" in out["note"].lower()


def test_capabilities_spend_rule_says_local_estimate(config):  # noqa: F811
    import capability_report

    section = capability_report._spend_section(config)
    assert "local estimate" in section["rule"].lower()


# ---------------------------------------------------------------- prospecting provenance


SOURCE = {"provider": "monid", "endpoint": "company-search", "run_id": "run-77",
          "fetched_at": "2026-11-12T01:00:00Z"}


def _lead():
    return {"company": "PayFast Pte Ltd", "website": "https://www.payfast.example",
            "industry": "Payments", "country": "SG", "employees": 40}


def _paid_run(config, run_id="run-77", topic="sg-fintech-leads"):  # noqa: F811
    env_id = _approved(config, topic=topic)
    res = spend.reserve(config, env_id, topic, 5, endpoint="company-search")
    spend.record(config, res["reservation_id"], actual=4, run_id=run_id)
    return env_id


def test_paid_source_without_a_recorded_run_is_refused(config):  # noqa: F811
    with pytest.raises(pr.ProspectError, match="spend"):
        pr.ingest(config, [_lead()], source=SOURCE, purpose="p")


def test_paid_source_with_a_recorded_run_is_accepted_and_linked(config):  # noqa: F811
    env_id = _paid_run(config)
    assert pr.ingest(config, [_lead()], source=SOURCE, purpose="p")["added"] == 1
    (p,) = pr.list_prospects(config)
    assert p["source"]["envelope_id"] == env_id


def test_invented_envelope_id_is_refused(config):  # noqa: F811
    _paid_run(config)
    with pytest.raises(pr.ProspectError, match="envelope"):
        pr.ingest(config, [_lead()], source={**SOURCE, "envelope_id": "made-up-123"}, purpose="p")


def test_envelope_id_from_another_run_is_refused(config):  # noqa: F811
    _paid_run(config)
    other = _paid_run(config, run_id="run-99", topic="other-topic")
    with pytest.raises(pr.ProspectError, match="envelope"):
        pr.ingest(config, [_lead()], source={**SOURCE, "envelope_id": other}, purpose="p")


def test_unrecorded_reservation_does_not_count(config):  # noqa: F811
    env_id = _approved(config)
    spend.reserve(config, env_id, "sg-fintech-leads", 5, endpoint="company-search")  # never recorded
    with pytest.raises(pr.ProspectError, match="spend"):
        pr.ingest(config, [_lead()], source=SOURCE, purpose="p")


def test_free_source_needs_no_envelope(config):  # noqa: F811
    free = {**SOURCE, "provider": "manual"}
    assert pr.ingest(config, [_lead()], source=free, purpose="p")["added"] == 1


def test_provider_name_case_and_spacing_resolve_to_the_paid_provider(config):  # noqa: F811
    with pytest.raises(pr.ProspectError, match="spend"):
        pr.ingest(config, [_lead()], source={**SOURCE, "provider": "Monid"}, purpose="p")
    env_id = _paid_run(config)
    assert pr.ingest(config, [_lead()], source={**SOURCE, "provider": " Monid "}, purpose="p")["added"] == 1
    assert pr.list_prospects(config)[0]["source"]["envelope_id"] == env_id


def test_ledger_lookup_is_scoped_to_this_company(config):  # noqa: F811
    """A run recorded on an envelope of another company is not this company's provenance."""
    from state_db import _open_db

    env_id = _paid_run(config)
    with _open_db(config) as db:
        db.conn.execute("UPDATE pending_actions SET company_id='other-co' WHERE id=?", (env_id,))
        db.conn.commit()
    with pytest.raises(pr.ProspectError, match="company"):
        pr.ingest(config, [_lead()], source=SOURCE, purpose="p")


# ---------------------------------------------------------------- review regressions


def _render(summary):
    import briefing_renderer

    return "\n".join(briefing_renderer._spend_lines(summary))


def test_briefing_renders_monthly_overrun_and_local_estimate(config):  # noqa: F811
    a = _approved(config, cap=100, topic="topic-a")
    spend.record(config, spend.reserve(config, a, "topic-a", 95, endpoint="serp")["reservation_id"], actual=120)
    text = _render(spend.briefing_summary(config))
    assert "OVER MONTHLY CAP" in text and "120" in text
    assert "local estimate" in text


def test_failed_zero_cost_call_is_not_provenance(config):  # noqa: F811
    env_id = _approved(config)
    res = spend.reserve(config, env_id, "sg-fintech-leads", 5, endpoint="company-search")
    spend.record(config, res["reservation_id"], actual=0, status="failure", run_id="run-77")
    with pytest.raises(pr.ProspectError, match="spend"):
        pr.ingest(config, [_lead()], source=SOURCE, purpose="p")


def test_run_on_another_endpoint_is_not_provenance(config):  # noqa: F811
    env_id = _approved(config)
    res = spend.reserve(config, env_id, "sg-fintech-leads", 5, endpoint="serp")
    spend.record(config, res["reservation_id"], actual=4, run_id="run-77")
    with pytest.raises(pr.ProspectError, match="spend"):
        pr.ingest(config, [_lead()], source=SOURCE, purpose="p")


@pytest.mark.parametrize("alias", ["monid-dataforseo", "monid:serp", "MONID_people"])
def test_provider_aliases_are_still_paid(config, alias):  # noqa: F811
    with pytest.raises(pr.ProspectError, match="spend"):
        pr.ingest(config, [_lead()], source={**SOURCE, "provider": alias}, purpose="p")


def test_unrelated_provider_name_is_free(config):  # noqa: F811
    assert pr.ingest(config, [_lead()], source={**SOURCE, "provider": "monidex"}, purpose="p")["added"] == 1


def test_closed_or_expired_envelope_still_proves_a_past_paid_run(config):  # noqa: F811
    env_id = _paid_run(config)
    spend.close(config, env_id)
    assert pr.ingest(config, [_lead()], source=SOURCE, purpose="p")["added"] == 1


def test_currency_check_covers_cancelled_envelopes(loose):
    import state_db

    env = spend.propose(loose, "monid", "p", "topic-a", 10, "USD")
    state_db.approve_pending_action(loose, env["id"], approver="op", reason="ok")
    spend.record(loose, spend.reserve(loose, env["id"], "topic-a", 5, endpoint="serp")["reservation_id"], actual=5)
    state_db.cancel_pending_action(loose, env["id"], reason="done")
    with pytest.raises(spend.SpendError, match="currency"):
        spend.propose(loose, "monid", "p", "topic-b", 10, "SGD")


def test_dismissed_envelope_does_not_fix_the_currency(loose):
    import state_db

    env = spend.propose(loose, "monid", "p", "topic-a", 10, "USD")
    state_db.dismiss_pending_action(loose, env["id"], reason="no")
    assert spend.propose(loose, "monid", "p", "topic-b", 10, "SGD")["state"] == "requested"


def test_overrun_booked_to_last_month_stays_visible(config, monkeypatch):  # noqa: F811
    env_id = _approved(config, cap=100)
    res = spend.reserve(config, env_id, "sg-fintech-leads", 95, endpoint="serp")
    first_of_next = (datetime.now(timezone.utc).replace(day=28) + timedelta(days=5)).replace(day=1, hour=1)
    monkeypatch.setattr(spend, "_now", lambda: first_of_next)
    assert spend.record(config, res["reservation_id"], actual=150)["over_monthly_cap"] is True
    over = spend.month_over_caps(config)
    assert over["monid"]["spent"] == 150
    assert over["monid"]["month"] == datetime.now(timezone.utc).strftime("%Y-%m")


def test_both_overruns_are_audited(config):  # noqa: F811
    env_id = _approved(config, cap=100)
    res = spend.reserve(config, env_id, "sg-fintech-leads", 95, endpoint="serp")
    spend.record(config, res["reservation_id"], actual=150)
    line = [ln for ln in _audit(config).splitlines() if "spend.record" in ln][-1]
    assert "over_cap" in line and "over_monthly_cap" in line


@pytest.mark.parametrize("created_at", ["2026-01-01T00:00:00", "not-a-date", ""])
def test_stale_survives_bad_timestamps(config, created_at):  # noqa: F811
    from state_db import _open_db

    env_id = _approved(config)
    res = spend.reserve(config, env_id, "sg-fintech-leads", 5, endpoint="serp")

    def corrupt(ledger):
        ledger["reservations"][res["reservation_id"]]["created_at"] = created_at
    with _open_db(config) as db:
        db.mutate_kv(spend.LEDGER, corrupt)
    assert spend.stale_reservations(config) == [res["reservation_id"]]
    assert spend.stale_details(config)[0]["reservation_id"] == res["reservation_id"]
