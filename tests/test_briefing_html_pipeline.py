"""HTML briefing: the pipeline section carries counts, not lists.

collect_pipeline_stats reports ``stale_deals`` as an int. The HTML renderer
iterated it as a list, so the HTML briefing crashed as soon as any deal went
stale (found when the sample deal aged past the threshold).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "shared" / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "shared" / "scripts"))

import briefing_renderer  # noqa: E402


def _briefing(pipeline):
    return {"kind": "daily", "company": "Sample Co", "date": "2026-11-27", "summary": {},
            "sections": {"pipeline": pipeline}}


def test_stale_count_renders_with_the_oldest_deal():
    html = briefing_renderer.render(_briefing({
        "active_deals": 3, "stale_deals": 2, "oldest_stale_id": "deal-001",
        "oldest_stale_stage": "Proposal Sent", "oldest_stale_days": 40}), "html")
    assert "2 stale" in html and "deal-001" in html and "40" in html


def test_no_stale_deals_says_so():
    html = briefing_renderer.render(_briefing({"active_deals": 3, "stale_deals": 0}), "html")
    assert "No stale deals." in html


def test_a_list_of_stale_deals_still_renders_as_a_table():
    html = briefing_renderer.render(_briefing({"stale_deals": [
        {"client_name": "Acme Corp", "stage": "Lead", "stale_days": 20}]}), "html")
    assert "<table>" in html and "Acme Corp" in html
