"""Phase 4b contract tests: prospecting, SEO audit and competitor watch.

The agent fetches (Monid or its own tools, paid calls through spend
envelopes); these modules only compute over what it passes in.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import competitor_watch as cw
import prospecting as pr
import seo_audit as seo
import state_db
from test_notion_mirror import DEAL, _config

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SOURCE = {"provider": "monid", "endpoint": "company-search", "run_id": "run-1",
          "fetched_at": "2026-11-12T01:00:00Z"}
ICP = """\
spend:
  currency: USD
  monid:
    monthly_cap: 100
prospecting:
  icp:
    industries: [fintech, payments]
    countries: [SG]
    min_employees: 10
    max_employees: 500
"""


@pytest.fixture
def config(tmp_path):
    cfg = _config(tmp_path, notion=ICP)
    state_db.save_store_atomic("pipeline", {"deals": [dict(DEAL)]}, config=cfg)
    return cfg


def _lead(**kw):
    base = {"company": "PayFast Pte Ltd", "website": "https://www.payfast.example/about",
            "contact_name": "Mei Tan", "contact_role": "CFO", "email": "mei@payfast.example",
            "industry": "Payments", "country": "SG", "employees": 40}
    base.update(kw)
    return base


# ---------------------------------------------------------------- prospecting


def test_ingest_stores_leads_with_provenance(config):
    report = pr.ingest(config, [_lead()], source=SOURCE, purpose="Q4 fintech outreach")
    assert report["added"] == 1
    (p,) = pr.list_prospects(config)
    assert p["domain"] == "payfast.example"
    assert p["source"]["run_id"] == "run-1" and p["purpose"] == "Q4 fintech outreach"
    assert p["status"] == "new" and p["icp_fit"] == 1.0


def test_ingest_requires_full_provenance(config):
    with pytest.raises(pr.ProspectError, match="run_id"):
        pr.ingest(config, [_lead()], source={"provider": "monid", "endpoint": "x"}, purpose="p")


def test_ingest_minimises_personal_data(config):
    lead = _lead(date_of_birth="1990-01-01", home_address="1 Home St", nric="S1234567A",
                 personal_mobile="+65 9000 0000")
    report = pr.ingest(config, [lead, _lead(company="Gmail Co", website="https://gco.example",
                                            email="someone@gmail.com")], source=SOURCE, purpose="p")
    assert set(report["dropped_fields"]) >= {"date_of_birth", "home_address", "nric", "personal_mobile"}
    assert report["personal_emails_dropped"] == 1
    stored = {p["company"]: p for p in pr.list_prospects(config)}
    assert "nric" not in json.dumps(stored)
    assert stored["Gmail Co"].get("email") is None


def test_phone_numbers_are_blocked_until_dnc_checked(config):
    pr.ingest(config, [_lead(phone="+65 6123 4567")], source=SOURCE, purpose="p")
    (p,) = pr.list_prospects(config)
    assert p["dnc"] == {"phone": "unchecked"}
    with pytest.raises(pr.ProspectError, match="DNC"):
        pr.contact_allowed(config, p["id"], channel="phone")
    pr.set_dnc(config, p["id"], "clear")
    assert pr.contact_allowed(config, p["id"], channel="phone") is True


def test_ingest_dedupes_against_pipeline_and_itself(config):
    leads = [
        _lead(),
        _lead(contact_name="Other", email="ops@payfast.example"),           # same domain + company
        _lead(company="Client Co", website="https://client.example",        # already a deal
              email="jo@client.example"),
    ]
    report = pr.ingest(config, leads, source=SOURCE, purpose="p")
    assert report["added"] == 1 and report["duplicates"] == 1 and report["in_pipeline"] == 1
    again = pr.ingest(config, [_lead()], source=SOURCE, purpose="p")
    assert again["added"] == 0 and again["duplicates"] == 1


def test_opt_out_suppresses_forever_even_after_purge(config):
    pr.ingest(config, [_lead()], source=SOURCE, purpose="p")
    pr.opt_out(config, email="MEI@payfast.example")
    (p,) = pr.list_prospects(config)
    assert p["status"] == "do_not_contact"
    with pytest.raises(pr.ProspectError, match="opted out"):
        pr.contact_allowed(config, p["id"], channel="email")
    assert pr.purge(config, older_than_days=0, confirm=True)["removed"] == 1
    assert pr.ingest(config, [_lead()], source=SOURCE, purpose="p")["suppressed"] == 1
    assert "mei@payfast.example" not in json.dumps(state_db.load_store("pipeline", config))


def test_purge_is_a_dry_run_unless_confirmed(config):
    pr.ingest(config, [_lead()], source=SOURCE, purpose="p")
    assert pr.purge(config, older_than_days=0)["would_remove"] == 1
    assert len(pr.list_prospects(config)) == 1


def test_propose_queues_pipeline_adds_for_approval(config):
    pr.ingest(config, [_lead()], source=SOURCE, purpose="p")
    (p,) = pr.list_prospects(config)
    (action,) = pr.propose(config, [p["id"]])
    assert action["type"] == "pipeline.deal.add" and action["state"] == "requested"
    assert action["payload"]["client_name"] == "PayFast Pte Ltd"
    assert action["payload"]["source"].startswith("prospecting:monid/run-1")
    assert pr.list_prospects(config)[0]["status"] == "proposed"
    assert pr.propose(config, [p["id"]]) == []  # not queued twice


def test_icp_fit_scores_partial_matches(config):
    pr.ingest(config, [_lead(company="Big Bank", website="https://bigbank.example", email=None,
                             industry="Banking", employees=20000)], source=SOURCE, purpose="p")
    (p,) = pr.list_prospects(config)
    assert 0 < p["icp_fit"] < 1


# ---------------------------------------------------------------- SEO audit

PAGES = [
    {"url": "https://acme.example/", "status": 200, "title": "Acme — accounting for SMEs in Singapore",
     "meta_description": "Bookkeeping and tax for small businesses.", "h1": ["Accounting for SMEs"],
     "canonical": "https://acme.example/", "word_count": 800, "images_missing_alt": 0},
    {"url": "https://acme.example/pricing", "status": 200, "title": "Pricing", "meta_description": None,
     "h1": [], "canonical": None, "word_count": 120, "images_missing_alt": 3, "load_ms": 4200},
    {"url": "https://acme.example/old", "status": 404, "title": "Pricing"},
    {"url": "https://acme.example/blog", "status": 200, "title": "Blog", "noindex": True,
     "meta_description": "x" * 200, "h1": ["Blog", "More"], "word_count": 400},
]
SERP = [
    {"keyword": "sme accounting singapore", "results": [
        {"position": 1, "url": "https://rival.example/sme"},
        {"position": 4, "url": "https://acme.example/"},
        {"position": 7, "url": "https://other.example/a"}]},
    {"keyword": "xero bookkeeping singapore", "results": [
        {"position": 1, "url": "https://rival.example/xero"},
        {"position": 2, "url": "https://other.example/b"}]},
]


def test_seo_audit_finds_on_page_issues():
    report = seo.analyze(PAGES, SERP, site="acme.example")
    codes = {(i["code"], i["url"]) for i in report["issues"]}
    assert ("http_error", "https://acme.example/old") in codes
    assert ("noindex", "https://acme.example/blog") in codes
    assert ("missing_meta_description", "https://acme.example/pricing") in codes
    assert ("h1_count", "https://acme.example/pricing") in codes
    assert ("thin_content", "https://acme.example/pricing") in codes
    assert ("slow_page", "https://acme.example/pricing") in codes
    assert ("images_missing_alt", "https://acme.example/pricing") in codes
    assert ("meta_description_length", "https://acme.example/blog") in codes
    assert ("duplicate_title", "https://acme.example/pricing") in codes


def test_seo_fixes_are_prioritised_by_severity():
    report = seo.analyze(PAGES, SERP, site="acme.example")
    severities = [f["severity"] for f in report["fixes"]]
    order = {"high": 0, "medium": 1, "low": 2}
    assert severities == sorted(severities, key=order.__getitem__)
    assert report["fixes"][0]["severity"] == "high"


def test_seo_rankings_and_competitors():
    report = seo.analyze(PAGES, SERP, site="acme.example")
    ranks = {r["keyword"]: r["position"] for r in report["rankings"]}
    assert ranks == {"sme accounting singapore": 4, "xero bookkeeping singapore": None}
    assert report["competitors"][0] == {"domain": "rival.example", "keywords": 2, "best_position": 1}
    assert any(o["keyword"] == "xero bookkeeping singapore" for o in report["opportunities"])


def test_seo_history_shows_rank_changes(config):
    seo.record(config, seo.analyze(PAGES, SERP, site="acme.example"))
    better = json.loads(json.dumps(SERP))
    better[0]["results"][1]["position"] = 2
    report = seo.analyze(PAGES, better, site="acme.example")
    delta = seo.compare(config, report)
    assert delta["rank_changes"] == [{"keyword": "sme accounting singapore", "from": 4, "to": 2}]


def test_seo_markdown_report():
    md = seo.render_markdown(seo.analyze(PAGES, SERP, site="acme.example"))
    assert md.startswith("# SEO audit — acme.example")
    assert "## Fix first" in md and "rival.example" in md


# ---------------------------------------------------------------- competitor watch


def _snapshot(price="S$99/month", title="Rival pricing", extra_page=False):
    pages = [{"url": "https://rival.example/pricing", "title": title,
              "text": f"Plans from {price}. Enterprise USD 1,200 per year."}]
    if extra_page:
        pages.append({"url": "https://rival.example/new-product", "title": "New product", "text": "Launch"})
    return {"captured_at": "2026-11-12T00:00:00Z", "competitors": {"rival.example": {"pages": pages}}}


def test_first_snapshot_is_a_baseline(config):
    out = cw.diff(config, _snapshot(), save=True)
    assert out["baseline"] is True and out["changes"] == []


def test_competitor_changes_are_reported_without_storing_page_text(config):
    cw.diff(config, _snapshot(), save=True)
    out = cw.diff(config, _snapshot(price="S$129/month", title="Rival plans", extra_page=True), save=True)
    kinds = {(c["kind"], c["url"]) for c in out["changes"]}
    assert ("new_page", "https://rival.example/new-product") in kinds
    assert ("title_changed", "https://rival.example/pricing") in kinds
    assert ("prices_changed", "https://rival.example/pricing") in kinds
    price = next(c for c in out["changes"] if c["kind"] == "prices_changed")
    assert price["added"] == ["S$129"] and price["removed"] == ["S$99"]
    assert "Plans from" not in json.dumps(cw.load_snapshots(config))


def test_removed_pages_are_reported(config):
    cw.diff(config, _snapshot(extra_page=True), save=True)
    out = cw.diff(config, _snapshot())
    assert ("page_removed", "https://rival.example/new-product") in {(c["kind"], c["url"]) for c in out["changes"]}


def test_diff_without_save_keeps_the_baseline(config):
    cw.diff(config, _snapshot(), save=True)
    cw.diff(config, _snapshot(price="S$1"))
    assert cw.diff(config, _snapshot())["changes"] == []


# ---------------------------------------------------------------- CLIs + registration


def _load(rel, name):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_entrypoints(config, tmp_path, capsys):
    cfg = str(config.source_path)
    leads = tmp_path / "leads.json"
    leads.write_text(json.dumps({"source": SOURCE, "purpose": "p", "leads": [_lead()]}))
    assert _load("skills/prospecting/scripts/prospects.py", "prospects_cli").main(
        ["--config", cfg, "ingest", "--input", str(leads)]) == 0
    assert json.loads(capsys.readouterr().out)["added"] == 1

    crawl = tmp_path / "crawl.json"
    crawl.write_text(json.dumps({"site": "acme.example", "pages": PAGES, "serp": SERP}))
    assert _load("skills/seo-tracker/scripts/seo_tracker.py", "seo_cli").main(
        ["--config", cfg, "analyze", "--input", str(crawl), "--markdown"]) == 0
    assert capsys.readouterr().out.startswith("# SEO audit")

    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps(_snapshot()))
    assert _load("skills/competitor-watch/scripts/competitor_watch_cli.py", "cw_cli").main(
        ["--config", cfg, "diff", "--input", str(snap), "--save"]) == 0
    assert json.loads(capsys.readouterr().out)["baseline"] is True


def test_prospecting_hidden_without_a_spend_provider(tmp_path, monkeypatch):
    import doctor_base

    plain = _config(tmp_path, notion="")
    monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(plain.source_path))
    names = ["seo-tracker", "competitor-watch", "prospecting"]
    assert doctor_base._filter_configured_skills(names) == ["seo-tracker", "competitor-watch"]
    with_spend = _config(tmp_path, notion=ICP)
    monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(with_spend.source_path))
    assert doctor_base._filter_configured_skills(names) == names
