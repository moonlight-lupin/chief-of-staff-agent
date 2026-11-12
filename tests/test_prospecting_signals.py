"""Prospecting v2 contract: buying signals, written verdicts, tiers, replies.

Concepts synthesised from three public outbound projects (ideas only, no
code or text reused): signals are stored observations with evidence, date and
confidence; the qualification is a written verdict a human can disagree with;
the tier is computed deterministically from fit and the freshest strong
signal; a reply from anyone at an account stops outreach to that account.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import prospecting as pr
from test_growth_skills import SOURCE, _lead, config  # noqa: F401  (fixture)


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).date().isoformat()


def _signal(**kw):
    base = {"type": "hiring", "summary": "Hiring a Head of Finance Operations (posted on their careers page)",
            "evidence_url": "https://payfast.example/careers/head-finops", "observed_at": _days_ago(5),
            "confidence": 0.9, "attribution": "named"}
    base.update(kw)
    return base


def _one(config):  # noqa: F811
    pr.ingest(config, [_lead()], source=SOURCE, purpose="Q4 fintech outreach")
    (p,) = pr.list_prospects(config)
    return p["id"]


# ------------------------------------------------------------------ signals


def test_observe_stores_signals_with_evidence(config):  # noqa: F811
    pid = _one(config)
    out = pr.observe(config, pid, [_signal()])
    assert out["added"] == 1
    (p,) = pr.list_prospects(config)
    (s,) = p["signals"]
    assert s["type"] == "hiring" and s["evidence_url"].startswith("https://")
    assert s["id"].startswith("sig-")


@pytest.mark.parametrize("missing", ["type", "summary", "evidence_url", "observed_at", "confidence"])
def test_observe_requires_evidence_fields(config, missing):  # noqa: F811
    pid = _one(config)
    bad = _signal()
    bad.pop(missing)
    with pytest.raises(pr.ProspectError, match=missing):
        pr.observe(config, pid, [bad])


def test_observe_rejects_unknown_signal_type(config):  # noqa: F811
    pid = _one(config)
    with pytest.raises(pr.ProspectError, match="signal type"):
        pr.observe(config, pid, [_signal(type="vibes")])


def test_industry_wide_news_is_excluded_not_attributed(config):  # noqa: F811
    pid = _one(config)
    out = pr.observe(config, pid, [_signal(attribution="industry"), _signal(type="funding")])
    assert out["added"] == 1 and out["excluded_industry_wide"] == 1


def test_ingest_accepts_signals_inline(config):  # noqa: F811
    pr.ingest(config, [_lead(signals=[_signal()])], source=SOURCE, purpose="p")
    (p,) = pr.list_prospects(config)
    assert len(p["signals"]) == 1
    assert "signals" not in pr.ingest(config, [], source=SOURCE, purpose="p")["dropped_fields"]


# ----------------------------------------------------------- assess / tiers


def test_assess_needs_a_written_reason(config):  # noqa: F811
    pid = _one(config)
    with pytest.raises(pr.ProspectError, match="reason"):
        pr.assess(config, pid, "fit", "")


def test_fit_with_fresh_strong_signal_is_tier_a_with_why_now(config):  # noqa: F811
    pid = _one(config)
    pr.observe(config, pid, [_signal(), _signal(type="tech_present", summary="Uses Xero",
                                                evidence_url="https://payfast.example/stack")])
    out = pr.assess(config, pid, "fit", "Series A payments firm in SG building a finance team")
    assert out["tier"] == "A" and out["status"] == "qualified"
    assert out["anchor_signal"]["type"] == "hiring"
    assert "Head of Finance Operations" in out["why_now"]


def test_fit_without_signal_is_tier_b(config):  # noqa: F811
    pid = _one(config)
    assert pr.assess(config, pid, "fit", "Right size and sector, no trigger yet")["tier"] == "B"


def test_stale_signals_do_not_count(config):  # noqa: F811
    pid = _one(config)
    pr.observe(config, pid, [_signal(observed_at=_days_ago(120))])
    out = pr.assess(config, pid, "fit", "Good fit")
    assert out["tier"] == "B" and out["anchor_signal"] is None


def test_low_confidence_signals_do_not_count(config):  # noqa: F811
    pid = _one(config)
    pr.observe(config, pid, [_signal(confidence=0.5)])
    assert pr.assess(config, pid, "fit", "Good fit")["anchor_signal"] is None


def test_older_signals_are_weaker(config):  # noqa: F811
    assert pr.signal_strength(_signal(observed_at=_days_ago(3))) > \
        pr.signal_strength(_signal(observed_at=_days_ago(45))) > 0


def test_strong_trigger_with_weak_fit_is_do_not_pursue(config):  # noqa: F811
    pr.ingest(config, [_lead(company="Big Bank", website="https://bigbank.example", email=None,
                             industry="Banking", country="US", employees=20000)],
              source=SOURCE, purpose="p")
    (p,) = pr.list_prospects(config)
    pr.observe(config, p["id"], [_signal(type="funding", summary="Raised $50m",
                                         evidence_url="https://news.example/bigbank")])
    out = pr.assess(config, p["id"], "fit", "Interesting raise")
    assert out["tier"] == "C" and out["do_not_pursue"] is True


def test_no_fit_verdict_drops_the_prospect(config):  # noqa: F811
    pid = _one(config)
    out = pr.assess(config, pid, "no_fit", "They are a competitor")
    assert out["status"] == "dropped" and out["tier"] is None


def test_unknown_fit_cannot_reach_tier_a(config):  # noqa: F811
    pr.ingest(config, [_lead(company="Mystery", website="https://mystery.example", email=None,
                             industry=None, country=None, employees=None)], source=SOURCE, purpose="p")
    (p,) = pr.list_prospects(config)
    pr.observe(config, p["id"], [_signal(evidence_url="https://mystery.example/jobs")])
    out = pr.assess(config, p["id"], "fit", "Looks right from their site")
    assert out["tier"] == "B" and "fit_not_assessed" in out["flags"]


def test_propose_requires_a_fit_verdict(config):  # noqa: F811
    pid = _one(config)
    assert pr.propose(config, [pid]) == []          # unassessed: nothing queued
    pr.assess(config, pid, "fit", "Good fit")
    (action,) = pr.propose(config, [pid])
    assert "Good fit" in action["payload"]["notes"][0]["note"]


# ------------------------------------------------------------------ replies


def test_add_contact_shares_the_account_and_is_minimised(config):  # noqa: F811
    pid = _one(config)
    new = pr.add_contact(config, pid, {"contact_name": "Raj", "contact_role": "COO",
                                       "email": "raj@payfast.example", "nric": "S1"})
    assert new["account_id"] == pr.list_prospects(config)[0]["account_id"]
    assert "nric" not in new and new["status"] == "new"


def test_reply_from_anyone_pauses_the_whole_account(config):  # noqa: F811
    pr.ingest(config, [_lead()], source=SOURCE, purpose="p")
    (p,) = pr.list_prospects(config)
    pr.add_contact(config, p["id"], {"contact_name": "Raj", "contact_role": "COO",
                                     "email": "raj@payfast.example"})
    a, b = (x["id"] for x in pr.list_prospects(config))
    pr.reply(config, a, "interested")
    assert pr.contact_allowed(config, a, "email") is True          # continue the live thread
    with pytest.raises(pr.ProspectError, match="account"):
        pr.contact_allowed(config, b, "email")


def test_out_of_office_does_not_pause_the_account(config):  # noqa: F811
    pid = _one(config)
    out = pr.reply(config, pid, "out_of_office")
    assert out["account_paused"] is False


def test_opt_out_wording_overrides_the_given_kind(config):  # noqa: F811
    pid = _one(config)
    out = pr.reply(config, pid, "question", text="Please remove me from your list.")
    assert out["kind"] == "unsubscribe"
    with pytest.raises(pr.ProspectError, match="opted out"):
        pr.contact_allowed(config, pid, "email")


def test_not_interested_stops_contact(config):  # noqa: F811
    pid = _one(config)
    pr.reply(config, pid, "not_interested")
    with pytest.raises(pr.ProspectError, match="not_interested"):
        pr.contact_allowed(config, pid, "email")


def test_escalate_holds_for_a_human(config):  # noqa: F811
    pid = _one(config)
    pr.reply(config, pid, "escalate")
    with pytest.raises(pr.ProspectError, match="human"):
        pr.contact_allowed(config, pid, "email")


def test_reply_text_is_not_stored(config):  # noqa: F811
    pid = _one(config)
    pr.reply(config, pid, "question", text="What does it cost? My mobile is 9123 4567")
    assert "9123" not in str(pr.list_prospects(config))


def test_unknown_reply_kind_is_rejected(config):  # noqa: F811
    pid = _one(config)
    with pytest.raises(pr.ProspectError, match="reply kind"):
        pr.reply(config, pid, "maybe")


# ------------------------------------------------------------ touches/stats


def test_touch_banks_used_signals_and_stats_report_by_angle(config):  # noqa: F811
    pid = _one(config)
    pr.observe(config, pid, [_signal()])
    sig = pr.list_prospects(config)[0]["signals"][0]["id"]
    pr.touch(config, pid, signal_id=sig, step=1)
    assert pr.list_prospects(config)[0]["touches"][0]["signal_type"] == "hiring"
    pr.reply(config, pid, "interested")
    stats = pr.stats(config)
    assert stats["by_signal"]["hiring"] == {"touched": 1, "replied": 1, "positive": 1}


def test_touch_refuses_a_paused_prospect(config):  # noqa: F811
    pid = _one(config)
    pr.reply(config, pid, "not_interested")
    with pytest.raises(pr.ProspectError):
        pr.touch(config, pid, signal_id=None, step=2)


# --------------------------------------------------------------------- CLI


def test_cli_signal_assess_reply_flow(config, tmp_path, capsys):  # noqa: F811
    import json

    from test_growth_skills import _load

    cli = _load("skills/prospecting/scripts/prospects.py", "prospects_cli_v2").main
    cfg = str(config.source_path)
    pid = _one(config)
    sig = tmp_path / "signals.json"
    sig.write_text(json.dumps([_signal()]))
    assert cli(["--config", cfg, "observe", pid, "--input", str(sig)]) == 0
    assert json.loads(capsys.readouterr().out)["added"] == 1
    assert cli(["--config", cfg, "assess", pid, "--verdict", "fit", "--reason", "Good fit"]) == 0
    assert json.loads(capsys.readouterr().out)["tier"] == "A"
    assert cli(["--config", cfg, "list"]) == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert row["tier"] == "A" and "email" not in row and row["why_now"]
    reply_text = tmp_path / "reply.txt"
    reply_text.write_text("please unsubscribe me")
    assert cli(["--config", cfg, "reply", pid, "--kind", "question", "--text-file", str(reply_text)]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "unsubscribe"
    assert cli(["--config", cfg, "stats"]) == 0
    assert "by_signal" in json.loads(capsys.readouterr().out)
