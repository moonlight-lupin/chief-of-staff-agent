#!/usr/bin/env python3
"""Tests for deep-research deterministic validation gates (research_validation.py).

Covers: evidence store (init-run / register-source / add-claim / add-evidence),
claim-support scoring, citation verification, and report structure validation.
"""

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "skills" / "deep-research" / "scripts" / "research_validation.py"
sys.path.insert(0, str(SCRIPT.parent))
import research_validation as rv  # noqa: E402


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "run"
    rc = rv.cmd_init_run(type("A", (), {"dir": str(d), "query": "test query",
                                        "mode": "standard", "provider": "donsetch"})())
    assert rc == 0
    return d


# ---------------------------------------------------------------- evidence store

class TestEvidenceStore:
    def test_init_run_creates_artifacts(self, run_dir):
        assert (run_dir / "run_manifest.json").exists()
        assert (run_dir / "sources.jsonl").exists()
        assert (run_dir / "claims.jsonl").exists()
        manifest = json.loads((run_dir / "run_manifest.json").read_text())
        assert manifest["provider_preference"] == "donsetch"

    def test_reinit_preserves_and_updates(self, run_dir):
        rv.cmd_init_run(type("A", (), {"dir": str(run_dir), "query": "updated",
                                       "mode": None, "provider": None})())
        m = json.loads((run_dir / "run_manifest.json").read_text())
        assert m["query"] == "updated"
        assert m["mode"] == "standard"  # preserved
        assert m["provider_preference"] == "donsetch"  # preserved

    def test_register_source_dedup(self, run_dir):
        args = type("A", (), {"dir": str(run_dir), "json": json.dumps(
            {"url": "https://Example.com/docs/?utm_source=x", "title": "Docs", "quality": "primary"})})()
        rc1 = rv.cmd_register_source(args)
        rc2 = rv.cmd_register_source(args)
        rc3 = rv.cmd_register_source(type("A", (), {"dir": str(run_dir), "json": json.dumps(
            {"url": "https://example.com/docs", "title": "Docs mirror", "quality": "secondary"})})())
        assert rc1 == 0 and rc2 == 0 and rc3 == 0
        sources = rv._read_jsonl(run_dir / "sources.jsonl")
        assert len(sources) == 1  # tracking params + trailing slash + case all canonicalized

    def test_add_claim_and_evidence(self, run_dir):
        rcs = rv.cmd_register_source(type("A", (), {"dir": str(run_dir), "json": json.dumps(
            {"url": "https://a.example/x", "title": "A"})})())
        assert rcs == 0
        sid = rv._read_jsonl(run_dir / "sources.jsonl")[0]["source_id"]
        rc = rv.cmd_add_claim(type("A", (), {"dir": str(run_dir), "json": json.dumps(
            {"claim_id": "c1", "claim": "Adoption grew 40% in 2025", "kind": "factual",
             "snippet": "Adoption grew 40% in 2025 according to the survey", "source_id": sid})})())
        assert rc == 0
        rc = rv.cmd_add_evidence(type("A", (), {"dir": str(run_dir), "json": json.dumps(
            {"claim_id": "c1", "snippet": "a second corroborating snippet", "source_id": sid})})())
        assert rc == 0
        # duplicate claim_id refused
        rc = rv.cmd_add_claim(type("A", (), {"dir": str(run_dir), "json": json.dumps(
            {"claim_id": "c1", "claim": "dup", "kind": "factual"})})())
        assert rc == 2
        # evidence for unknown claim refused
        rc = rv.cmd_add_evidence(type("A", (), {"dir": str(run_dir), "json": json.dumps(
            {"claim_id": "zz", "snippet": "s"})})())
        assert rc == 2

    def test_stable_source_ids_survive_order(self, run_dir):
        for i, url in enumerate(["https://x.example/p", "https://y.example/q"]):
            rv.cmd_register_source(type("A", (), {"dir": str(run_dir), "json": json.dumps(
                {"url": url, "title": f"S{i}"})})())
        ids = [s["source_id"] for s in rv._read_jsonl(run_dir / "sources.jsonl")]
        assert ids == [rv.stable_source_id(u) for u in ["https://x.example/p", "https://y.example/q"]]


# ---------------------------------------------------------------- claim support

class TestClaimSupport:
    def test_supported_claim(self):
        score = rv.support_score(
            "The library reached 1.1k stars and 121 forks",
            ["The project reached 1.1k stars and 121 forks on GitHub"])
        assert score >= rv.SUPPORTED_THRESHOLD

    def test_unsupported_claim(self):
        score = rv.support_score(
            "Revenue tripled to 900 million dollars in Q3",
            ["The library reached 1.1k stars and 121 forks on GitHub"])
        assert score < rv.PARTIAL_THRESHOLD

    def test_no_evidence_is_unsupported(self):
        assert rv.support_score("any claim", []) == 0.0

    def test_verify_claims_strict_fails_unsupported_factual(self, run_dir):
        rv.cmd_register_source(type("A", (), {"dir": str(run_dir), "json": json.dumps(
            {"url": "https://a.example/x", "title": "A"})})())
        sid = rv._read_jsonl(run_dir / "sources.jsonl")[0]["source_id"]
        rv.cmd_add_claim(type("A", (), {"dir": str(run_dir), "json": json.dumps(
            {"claim_id": "c1", "claim": "Revenue tripled to 900 million in 2026", "kind": "factual",
             "snippet": "Stars grew on GitHub", "source_id": sid})})())
        ns = type("A", (), {"dir": str(run_dir), "strict": False})()
        assert rv.cmd_verify_claims(ns) == 0  # non-strict: report only
        ns.strict = True
        assert rv.cmd_verify_claims(ns) == 1  # strict: exit 1

    def test_verify_claims_synthesis_speculation_not_hard_fail(self, run_dir):
        rv.cmd_add_claim(type("A", (), {"dir": str(run_dir), "json": json.dumps(
            {"claim_id": "c9", "claim": "This suggests a potential mechanism", "kind": "synthesis",
             "snippet": "unrelated evidence text"})})())
        # kind synthesis with weak support must not hard-fail even in strict
        # mode (strict=True exercises the intended path — non-strict always
        # returns 0, so that assertion alone is vacuous).
        assert rv.cmd_verify_claims(type("A", (), {"dir": str(run_dir), "strict": True,
                                                   "provider_used": None})()) == 0

    def test_verify_claims_empty_strict_fails(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        (d / "claims.jsonl").touch()
        assert rv.cmd_verify_claims(type("A", (), {"dir": str(d), "strict": True})()) == 1
        assert rv.cmd_verify_claims(type("A", (), {"dir": str(d), "strict": False})()) == 0


# ---------------------------------------------------------------- citation check

GOOD_REPORT = """# Report

## Executive Summary
Adoption grew 40% in 2025 [1], driven by demand [2].

## Analysis
The market reached $2.4 billion [1]. Critics disagree [2].

## Contradictions
Source A reports growth; source B reports decline [1] [2].

## Gaps
No primary pricing sources found.

## Conclusion
Growth is likely but unconfirmed.

## Sources

**Quality distribution:** 1 primary · 1 secondary · 0 tertiary — acceptable

| # | Title | URL | Quality | Accessed |
|---|-------|-----|---------|----------|
| 1 | Adoption Survey 2026 (https://real.example/survey) | https://real.example/survey | primary | 2026-09-24 |
| 2 | Market Analysis Quarterly (https://news.example/market) | https://news.example/market | secondary | 2026-09-24 |

**Evidence key** — `[VERIFIED]` corroborated across ≥2 independent, cited, dated sources · `[SOURCED]` from one named source, not independently corroborated · `[REASONED]` analytical judgement / inference · `[ESTIMATED]` calculation or stated assumption.

---
📊 Research stats: 12 min · 3 rounds · 8 queries · 9 URLs fetched · 2 sources cited
"""


def _write_report(tmp_path, content):
    p = tmp_path / "report.md"
    p.write_text(content, encoding="utf-8")
    return str(p)


class TestCitationCheck:
    def test_good_report_passes(self, tmp_path):
        rc = rv.cmd_verify_citations(type("A", (), {"report": _write_report(tmp_path, GOOD_REPORT)})())
        assert rc == 0

    def test_dangling_inline_citation_fails(self, tmp_path):
        rc = rv.cmd_verify_citations(type("A", (), {"report": _write_report(
            tmp_path, GOOD_REPORT.replace("demand [2].", "demand [2] [5]."))})())
        assert rc == 1

    def test_uncited_source_entry_fails(self, tmp_path):
        rc = rv.cmd_verify_citations(type("A", (), {"report": _write_report(
            tmp_path, GOOD_REPORT.replace("[2].", "[1]."))})())
        assert rc == 1

    def test_citation_range_fails(self, tmp_path):
        rc = rv.cmd_verify_citations(type("A", (), {"report": _write_report(
            tmp_path, GOOD_REPORT + "\n[3-50] Additional citations\n")})())
        assert rc == 1

    def test_suspicious_title_flagged(self, tmp_path):
        bad = GOOD_REPORT.replace("Adoption Survey 2026 (https://real.example/survey)",
                                  '"Analysis of Things" (2026). Journal. https://real.example/survey')
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = rv.cmd_verify_citations(type("A", (), {"report": _write_report(tmp_path, bad)})())
        assert rc == 1
        assert "suspicious" in buf.getvalue()


class TestStructureCheck:
    def test_good_report_passes(self, tmp_path):
        rc = rv.cmd_validate_report(type("A", (), {"report": _write_report(tmp_path, GOOD_REPORT)})())
        assert rc == 0

    def test_missing_contradictions_fails(self, tmp_path):
        rc = rv.cmd_validate_report(type("A", (), {"report": _write_report(
            tmp_path, GOOD_REPORT.replace("## Contradictions", "## Discussion"))})())
        assert rc == 1

    def test_missing_gaps_fails(self, tmp_path):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = rv.cmd_validate_report(type("A", (), {"report": _write_report(
                tmp_path, GOOD_REPORT.replace("## Gaps", "## Open Items"))})())
        assert rc == 1
        assert "missing required section" in buf.getvalue()

    def test_placeholder_fails(self, tmp_path):
        rc = rv.cmd_validate_report(type("A", (), {"report": _write_report(
            tmp_path, GOOD_REPORT.replace("No primary pricing sources found.",
                                          "TBD — more research needed."))})())
        assert rc == 1

    def test_missing_stats_fails(self, tmp_path):
        rc = rv.cmd_validate_report(type("A", (), {"report": _write_report(
            tmp_path, GOOD_REPORT.replace("📊 Research stats:", ""))})())
        assert rc == 1

    def test_empty_contradictions_fails(self, tmp_path):
        rc = rv.cmd_validate_report(type("A", (), {"report": _write_report(
            tmp_path, GOOD_REPORT.replace("Source A reports growth; source B reports decline [1] [2].", ""))})())
        assert rc == 1


class TestCanonicalUrl:
    def test_canonicalization(self):
        assert rv.canonical_url("https://Example.com/Docs/?utm_source=x#frag") == \
            "https://example.com/Docs"
        assert rv.canonical_url("https://example.com") == "https://example.com"
        assert rv.canonical_url("http://example.com/a?fbclid=1&keep=2") == \
            "https://example.com/a?keep=2"


class TestReviewRound4:
    """Regression tests for the Codex review round 4 (v1.7.4)."""

    def test_shared_figure_cannot_mask_contradiction(self):
        # A shared figure (20 stores) must not mask a contradicted one (1.1k vs 1.1m).
        assert rv.support_score("1.1k units across 20 stores in 2025",
                                ["1.1m units across 20 stores in 2025"]) < rv.SUPPORTED_THRESHOLD

    def test_suffixed_yearlike_figures_count(self):
        # '2025k' is a suffixed figure, not a year.
        assert rv._figures("2025k units in 2025") == {"2025k"}
        assert rv.support_score("2025k units in 2025", ["2025m units in 2025"]) < rv.SUPPORTED_THRESHOLD

    def test_empty_evidence_source_id_fails_strict(self, run_dir):
        # Create a claim with a REGISTERED source first, so the empty-source
        # evidence record is the only defect under test (Codex round 5, #2).
        rv.cmd_register_source(type("A", (), {"dir": str(run_dir), "json": json.dumps(
            {"url": "https://reg.example/x", "title": "Reg"})})())
        sid = rv._read_jsonl(run_dir / "sources.jsonl")[0]["source_id"]
        rc = rv.cmd_add_claim(type("A", (), {"dir": str(run_dir), "json": json.dumps(
            {"claim_id": "c1", "claim": "Adoption grew 40% in 2025", "kind": "factual",
             "snippet": "Adoption grew 40% in 2025 per survey", "source_id": sid})})())
        assert rc == 0
        rc = rv.cmd_add_evidence(type("A", (), {"dir": str(run_dir), "json": json.dumps(
            {"claim_id": "c1", "snippet": "corroboration with no provenance", "source_id": ""})})())
        assert rc == 0  # evidence record accepted...
        # ...but the empty provenance fails strict verification.
        assert rv.cmd_verify_claims(type("A", (), {"dir": str(run_dir), "strict": True,
                                                   "provider_used": None})()) == 1

    def test_cap_still_allows_full_match(self):
        assert rv.support_score("revenue reached 2.4 billion",
                                ["revenue reached 2.4 billion"]) >= rv.SUPPORTED_THRESHOLD

    def test_claim_figure_absent_from_snippet_capped(self):
        # Snippet that paraphrases away the claim's key figure cannot corroborate.
        assert rv.support_score("Acme annual revenue grew steadily to 40 million dollars",
                                ["Acme annual revenue grew steadily to million dollars"]) < rv.SUPPORTED_THRESHOLD

    def test_magnitude_words_contradict(self):
        # Spelled-out magnitude words are suffixes: '40 million' ≠ '40 billion'.
        assert rv._figures("Acme revenue reached 40 million dollars in 2025") == {"40m"}
        assert rv.support_score("Acme revenue reached 40 million dollars in 2025",
                                ["Acme revenue reached 40 billion dollars in 2025"]) < rv.SUPPORTED_THRESHOLD
        # Same magnitude word corroborates.
        assert rv.support_score("Acme revenue reached 40 million dollars in 2025",
                                ["Acme revenue reached 40 million dollars in 2025"]) >= rv.SUPPORTED_THRESHOLD

    def test_year_superset_cap(self):
        # Claim's years must ALL be corroborated: {2024,2025} vs {2024,2026} caps.
        assert rv.support_score("Acme revenue grew between 2024 and 2025",
                                ["Acme revenue grew between 2024 and 2026"]) < rv.SUPPORTED_THRESHOLD
        # Full year corroboration still supported.
        assert rv.support_score("Growth hit 40% in 2025",
                                ["Growth hit 40% in 2025 per survey"]) >= rv.SUPPORTED_THRESHOLD


class TestReviewRound3:
    """Regression tests for the Codex review round 3 (v1.7.3)."""

    def test_partial_figure_overlap_capped(self):
        # Shared year must not mask a contradicted figure.
        assert rv.support_score("Acme revenue grew 40 percent in 2025",
                                ["Acme revenue grew 90 percent in 2025"]) < rv.SUPPORTED_THRESHOLD

    def test_suffix_magnitude_distinguishes(self):
        assert rv._figures("Acme sold 1.1k units") == {"1.1k"}
        assert rv.support_score("Acme sold 1.1k units", ["Acme sold 1.1m units"]) < rv.SUPPORTED_THRESHOLD
        assert rv.support_score("Acme sold 1.1k units", ["Acme sold 1.1k units"]) >= rv.SUPPORTED_THRESHOLD

    def test_extra_evidence_source_ids_resolved(self, run_dir):
        # Build a real claim first so the ghost source_id is the only defect
        # (vacuous-test fix from the net-diff review, Codex finding 3).
        rv.cmd_register_source(type("A", (), {"dir": str(run_dir), "json": json.dumps(
            {"url": "https://reg.example/x", "title": "Reg"})})())
        sid = rv._read_jsonl(run_dir / "sources.jsonl")[0]["source_id"]
        assert rv.cmd_add_claim(type("A", (), {"dir": str(run_dir), "json": json.dumps(
            {"claim_id": "c1", "claim": "Adoption grew 40% in 2025", "kind": "factual",
             "snippet": "Adoption grew 40% in 2025 per survey", "source_id": sid})})()) == 0
        rv.cmd_add_evidence(type("A", (), {"dir": str(run_dir), "json": json.dumps(
            {"claim_id": "c1", "snippet": "more context", "source_id": "ghost"})})())
        rc = rv.cmd_verify_claims(type("A", (), {"dir": str(run_dir), "strict": True,
                                                 "provider_used": None})())
        assert rc == 1

    def test_missing_primary_source_id_fails_strict(self, run_dir):
        d = run_dir
        # overwrite c1 with no source_id
        claims = [r for r in rv._read_jsonl(d / "claims.jsonl") if r.get("claim_id") != "c1"]
        claims.append({"claim_id": "c1", "claim": "Adoption grew 40% in 2025", "kind": "factual",
                       "snippet": "Adoption grew 40% in 2025", "source_id": ""})
        (d / "claims.jsonl").write_text("".join(json.dumps(r) + "\n" for r in claims))
        rc = rv.cmd_verify_claims(type("A", (), {"dir": str(d), "strict": True,
                                                 "provider_used": None})())
        assert rc == 1

    def test_non_object_manifest_untouched(self, tmp_path):
        d = tmp_path / "m"
        d.mkdir()
        (d / "run_manifest.json").write_text("[]")
        (d / "claims.jsonl").touch()
        (d / "sources.jsonl").touch()
        ns = type("A", (), {"dir": str(d), "strict": False, "provider_used": "donsetch"})()
        assert rv.cmd_verify_claims(ns) == 0
        assert (d / "run_manifest.json").read_text() == "[]"


class TestNetReviewRound:
    """Regression tests for the net-diff review (Codex + Claude Opus)."""

    def test_magnitude_word_and_suffix_equivalent(self):
        # '40 million' and '40M' are the same figure.
        assert rv.support_score("Revenue hit 40 million", ["Revenue hit 40M"]) >= rv.SUPPORTED_THRESHOLD
        assert rv.support_score("Revenue hit 40 million", ["Revenue hit 50 million"]) < rv.SUPPORTED_THRESHOLD

    def test_comma_grouped_numbers_normalized(self):
        assert rv._numbers("Sales were 1,200 units") == {"1200"}
        assert rv.support_score("Sales were 1,200 units", ["Sales were 1200 units"]) >= rv.SUPPORTED_THRESHOLD

    def test_unit_bound_numbers_contradict(self):
        # Units are part of the figure: 40ms ≠ 90ms, 100MW ≠ 50MW.
        assert rv.support_score("Latency is 40ms", ["Latency is 90ms"]) < rv.SUPPORTED_THRESHOLD
        assert rv.support_score("Latency is 40ms", ["Latency is 40ms"]) >= rv.SUPPORTED_THRESHOLD
        assert rv.support_score("Capacity is 100MW", ["Capacity is 50MW"]) < rv.SUPPORTED_THRESHOLD

    def test_year_superset_cap(self):
        assert rv.support_score("Acme revenue grew between 2024 and 2025",
                                ["Acme revenue grew between 2024 and 2026"]) < rv.SUPPORTED_THRESHOLD
        assert rv.support_score("Growth hit 40% in 2025",
                                ["Growth hit 40% in 2025 per survey"]) >= rv.SUPPORTED_THRESHOLD

    def test_yearlike_count_contradiction_capped(self):
        # Bare 1900-2099 numbers are years: {2000,2024} vs {1950,2024} —
        # the superset cap catches the contradiction (net review, Opus #1).
        assert rv.support_score("Acme opened 2000 stores in 2024",
                                ["Acme opened 1950 stores in 2024"]) < rv.SUPPORTED_THRESHOLD

    def test_malformed_url_clean_error(self, tmp_path):
        d = tmp_path / "mu"
        d.mkdir()
        (d / "sources.jsonl").touch()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = rv.cmd_register_source(type("A", (), {
                "dir": str(d), "json": '{"url": "http://[", "title": "B"}'})())
        assert rc == 2
        assert "malformed url" in buf.getvalue()

    def test_strict_empty_store_reports_ok_false(self, tmp_path):
        d = tmp_path / "se"
        d.mkdir()
        (d / "claims.jsonl").touch()
        (d / "sources.jsonl").touch()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = rv.cmd_verify_claims(type("A", (), {"dir": str(d), "strict": True,
                                                     "provider_used": None})())
        assert rc == 1
        assert json.loads(buf.getvalue())["ok"] is False

    def test_subheading_sources_does_not_split(self, tmp_path):
        # '### Sources of revenue' in the body must not become the Sources section.
        report = GOOD_REPORT.replace(
            "The market reached $2.4 billion [1].",
            "### Sources of revenue\n\nThe market reached $2.4 billion [1].")
        rc = rv.cmd_verify_citations(type("A", (), {"report": _write_report(tmp_path, report)})())
        assert rc == 0

    def test_contains_word_heading_checked_for_empty(self, tmp_path):
        # The emptiness check mirrors the required-section check (CONTAINS):
        # an empty '## Knowledge Gaps' must fail.
        report = GOOD_REPORT.replace(
            "## Gaps\nNo primary pricing sources found.",
            "## Knowledge Gaps")
        rc = rv.cmd_validate_report(type("A", (), {"report": _write_report(tmp_path, report)})())
        assert rc == 1

    def test_comma_grouped_yearlike_contradiction_capped(self):
        # Net review 2, #1: comma-grouped counts are years too.
        assert rv.support_score("Acme opened 2,000 stores in 2024",
                                ["Acme opened 1,950 stores in 2024"]) < rv.SUPPORTED_THRESHOLD

    def test_model_numbers_contradict(self):
        # Net review 2, #2: digits inside product names are figures.
        assert rv.support_score("Apple iPhone15 sales grew steadily worldwide",
                                ["Apple iPhone16 sales grew steadily worldwide"]) < rv.SUPPORTED_THRESHOLD
        assert rv.support_score("Apple iPhone15 sales grew steadily worldwide",
                                ["Apple iPhone15 sales grew steadily worldwide"]) >= rv.SUPPORTED_THRESHOLD

    def test_full_unit_runs_distinguished(self):
        # Net review 2, #3: the full letter run is kept — milligrams ≠ milliliters.
        assert rv.support_score("Dose is 100milligrams", ["Dose is 100milliliters"]) < rv.SUPPORTED_THRESHOLD
        assert rv.support_score("Dose is 100milligrams", ["Dose is 100milligrams"]) >= rv.SUPPORTED_THRESHOLD

    def test_embedded_yearlike_model_numbers_contradict(self):
        # Net review 3: 'v2000' vs 'v2001' — the digit run inside a token is a
        # figure, not a year (boundary-consistent with _years).
        assert rv.support_score("Acme v2000 sales grew steadily worldwide in 2024",
                                ["Acme v2001 sales grew steadily worldwide in 2024"]) < rv.SUPPORTED_THRESHOLD
        assert rv.support_score("Acme v2000 sales grew steadily worldwide in 2024",
                                ["Acme v2000 sales grew steadily worldwide in 2024"]) >= rv.SUPPORTED_THRESHOLD
        # A bare yearlike count stays a year (handled by the year superset cap).
        assert rv.support_score("Acme opened 2000 stores in 2024",
                                ["Acme opened 1950 stores in 2024"]) < rv.SUPPORTED_THRESHOLD
        # Net review 4: the check is occurrence-level — a bare ' in 2000' in the
        # same text must not excuse the embedded '2000' inside 'v2000'.
        assert rv.support_score("Acme v2000 sales grew steadily worldwide in 2000",
                                ["Acme v2001 sales grew steadily worldwide in 2000"]) < rv.SUPPORTED_THRESHOLD

    def test_magnitude_word_prose_guards(self):
        # Net review 2, #4: a yearlike value never takes a magnitude word, and
        # 'plural of' is prose — no invented '2025m'.
        assert rv._figures("In 2025 millions of users adopted the service") == set()
        assert rv.support_score("In 2025 millions of users adopted the service",
                                ["Millions of users adopted the service in 2025"]) >= rv.SUPPORTED_THRESHOLD
        # A genuine count with a magnitude word still works.
        assert rv.support_score("3 million users adopted the service in 2025",
                                ["3 million users adopted the service in 2025"]) >= rv.SUPPORTED_THRESHOLD

    def test_subheading_content_counts_for_empty_check(self, tmp_path):
        # Net review 2, #5: content under a ### subheading inside Gaps is
        # section content — the section must NOT be flagged as empty.
        report = GOOD_REPORT.replace(
            "## Gaps\nNo primary pricing sources found.",
            "## Gaps\n### Pricing detail\nWe lack pricing data for SKUs.")
        rc = rv.cmd_validate_report(type("A", (), {"report": _write_report(tmp_path, report)})())
        assert rc == 0


class TestReviewRound2:
    """Regression tests for the Codex review round 2 (v1.7.2)."""

    def test_conflicting_numbers_capped(self):
        # C1/C2: near-duplicate prose with one different figure must not score supported.
        assert rv.support_score("Acme annual revenue grew steadily to 40 million dollars",
                                ["Acme annual revenue grew steadily to 90 million dollars"]) < rv.SUPPORTED_THRESHOLD
        assert rv.support_score("Acme revenue grew 40 percent in 2025",
                                ["Acme revenue grew 90 percent in 2026"]) < rv.PARTIAL_THRESHOLD

    def test_suffixed_numbers_extracted(self):
        assert rv._numbers("Acme sold 1.1k units") == {"1.1"}
        assert rv.support_score("Acme sold 1.1k units", ["Acme sold 1.9k units"]) < rv.SUPPORTED_THRESHOLD
        assert rv.support_score("Acme sold 1.1k units", ["Acme sold 1.1k units"]) >= rv.SUPPORTED_THRESHOLD

    def test_fenced_sources_heading_does_not_split(self, tmp_path):
        report = GOOD_REPORT.replace(
            "The market reached $2.4 billion [1].",
            "The market reached $2.4 billion [1].\n\n```\n## Sources example\n```\n")
        rc = rv.cmd_verify_citations(type("A", (), {"report": _write_report(tmp_path, report)})())
        assert rc == 0

    def test_table_url_with_dashes_parses(self, tmp_path):
        report = GOOD_REPORT.replace(
            "https://news.example/market", "https://news.example/mar---ket")
        rc = rv.cmd_verify_citations(type("A", (), {"report": _write_report(tmp_path, report)})())
        assert rc == 0

    def test_malformed_manifest_clean_error(self, tmp_path):
        d = tmp_path / "m"
        d.mkdir()
        (d / "run_manifest.json").write_text("{broken")
        (d / "claims.jsonl").touch()
        (d / "sources.jsonl").touch()
        ns = type("A", (), {"dir": str(d), "strict": False, "provider_used": "donsetch"})()
        assert rv.cmd_verify_claims(ns) == 0  # claims gate still runs
        assert (d / "run_manifest.json").read_text() == "{broken"  # untouched

    def test_init_run_malformed_manifest_clean_error(self, tmp_path):
        d = tmp_path / "m2"
        d.mkdir()
        (d / "run_manifest.json").write_text("{broken")
        with pytest.raises(SystemExit) as exc:
            rv.cmd_init_run(type("A", (), {"dir": str(d), "query": "q", "mode": "deep",
                                           "provider": "donsetch"})())
        assert "malformed" in str(exc.value)

    def test_unregistered_source_fails_strict(self, run_dir):
        rv.cmd_add_claim(type("A", (), {"dir": str(run_dir), "json": json.dumps(
            {"claim_id": "cs1", "claim": "X reached 100 users in 2025", "kind": "factual",
             "snippet": "X reached 100 users in 2025", "source_id": "nosuch"})})())
        rc = rv.cmd_verify_claims(type("A", (), {"dir": str(run_dir), "strict": True,
                                                 "provider_used": None})())
        assert rc == 1

    def test_nonstring_kind_refused(self, run_dir):
        rc = rv.cmd_add_claim(type("A", (), {"dir": str(run_dir), "json": '{"claim_id":"k2","claim":"x","kind":[]}'} )())
        assert rc == 2

    def test_tilde_fence_stripped(self, tmp_path):
        report = GOOD_REPORT.replace(
            "Critics disagree [2].",
            "Critics disagree [2].\n\n~~~\nrows[7]\n~~~\n")
        rc = rv.cmd_verify_citations(type("A", (), {"report": _write_report(tmp_path, report)})())
        assert rc == 0

    def test_decorated_empty_sections_fail(self, tmp_path):
        # A decorated heading with NO content under it must still fail the
        # emptiness check (Codex round 2, #13).
        report = GOOD_REPORT.replace(
            "## Contradictions\nSource A reports growth; source B reports decline [1] [2].",
            "## Contradictions (none)")
        rc = rv.cmd_validate_report(type("A", (), {"report": _write_report(tmp_path, report)})())
        assert rc == 1

    def test_duplicate_bib_numbers_fail(self, tmp_path):
        report = GOOD_REPORT + "\n[1] Author B (2025). \"Other\". Venue. https://other.example/x\n"
        rc = rv.cmd_verify_citations(type("A", (), {"report": _write_report(tmp_path, report)})())
        assert rc == 1

    def test_partial_legend_fails(self, tmp_path):
        report = GOOD_REPORT.replace(
            "`[SOURCED]` from one named source, not independently corroborated · `[REASONED]` analytical judgement / inference · `[ESTIMATED]` calculation or stated assumption.", "")
        rc = rv.cmd_validate_report(type("A", (), {"report": _write_report(tmp_path, report)})())
        assert rc == 1


class TestReviewRound1:
    """Regression tests for the Codex+Muse review round (v1.7.1)."""

    # F1/F13 (Codex BLOCKING, Muse MINOR): vacuous-true scoring.
    def test_unrelated_strings_score_low(self):
        assert rv.support_score("cats sleep", ["rocks erode"]) < rv.PARTIAL_THRESHOLD
        assert rv.support_score("营收增长了百分之四十", ["用户数量翻倍"]) < rv.PARTIAL_THRESHOLD

    # F3 (both): year regex returned only the century.
    def test_year_regex_captures_full_year(self):
        assert rv._years("2025 2026") == {"2025", "2026"}
        assert rv._years("in 1999 and 2007") == {"1999", "2007"}

    # F2 (Codex): negation is invisible to the scorer — document the boundary.
    def test_negation_still_scores_high_known_limitation(self):
        # v2 does not parse polarity; this documents the known boundary.
        assert rv.support_score("Acme is profitable", ["Acme is not profitable"]) >= 0.5

    # F3 (Codex 4): conflicting numbers score partial, not supported.
    def test_conflicting_numbers_below_supported(self):
        s = rv.support_score("Acme revenue grew 40 percent in 2025",
                             ["Acme revenue grew 90 percent in 2026"])
        assert s < rv.SUPPORTED_THRESHOLD

    # Muse 1 (BLOCKING) mirror: the vacuous-true branch is gone. A claim with no
    # signal axes now scores on Jaccard alone — sharing one generic token stays
    # thin support, which is the correct verdict for a vague claim.
    def test_signal_free_claim_jaccard_only(self):
        s = rv.support_score("The library is popular",
                             ["The library reached 1.1k stars and 121 forks"])
        assert 0.0 < s < rv.SUPPORTED_THRESHOLD  # thin, not vacuous-true
        assert rv.support_score("cats sleep", ["the library reached 1.1k stars"]) < 0.2

    # Muse 9: suffixed numbers (1.1k) — NUMBER_RE drops them by design; the
    # regression here is that 1.1 still matches the plain number.
    def test_plain_decimal_numbers_match(self):
        assert rv.support_score("revenue reached 2.4 billion",
                                ["revenue reached 2.4 billion"]) >= rv.SUPPORTED_THRESHOLD

    # R3-5 (Codex round 3): explicit values reset, omitted values preserve.
    def test_reinit_cli_defaults_preserve(self, tmp_path):
        d = tmp_path / "m"
        rv.cmd_init_run(type("A", (), {"dir": str(d), "query": "q1", "mode": "deep",
                                       "provider": "donsetch"})())
        # CLI omits both flags (argparse default=None): values preserved.
        rv.cmd_init_run(type("A", (), {"dir": str(d), "query": "", "mode": None,
                                       "provider": None})())
        m = rv._load_manifest(d)
        assert m["mode"] == "deep"
        assert m["provider_preference"] == "donsetch"
        # Explicit values DO overwrite (reset capability).
        rv.cmd_init_run(type("A", (), {"dir": str(d), "query": "", "mode": "quick",
                                       "provider": "auto"})())
        m = rv._load_manifest(d)
        assert m["mode"] == "quick"
        assert m["provider_preference"] == "auto"

    # Muse 6: --provider-used records provider_used in the manifest.
    def test_provider_used_recorded(self, run_dir):
        ns = type("A", (), {"dir": str(run_dir), "strict": False,
                            "provider_used": "donsetch"})()
        rv.cmd_verify_claims(ns)
        m = rv._load_manifest(run_dir)
        assert m["provider_used"] == "donsetch"

    # Codex 9: duplicate bibliography numbers with different content must fail.
    def test_duplicate_bib_numbers_fail(self, tmp_path):
        report = GOOD_REPORT + "\n[1] Author B (2025). \"Other\". Venue. https://other.example/x\n"
        rc = rv.cmd_verify_citations(type("A", (), {"report": _write_report(tmp_path, report)})())
        assert rc == 1

    # Codex 8: numeric brackets in code blocks are not citations.
    def test_code_block_brackets_not_citations(self, tmp_path):
        report = GOOD_REPORT.replace(
            "The market reached $2.4 billion [1]. Critics disagree [2].",
            "The market reached $2.4 billion [1]. Critics disagree [2].\n\nUse `array[1]` and:\n\n```\nx = rows[7]\n```\n")
        rc = rv.cmd_verify_citations(type("A", (), {"report": _write_report(tmp_path, report)})())
        assert rc == 0

    # Codex 11: body citation ranges now rejected too.
    def test_body_citation_range_fails(self, tmp_path):
        report = GOOD_REPORT.replace(
            "Critics disagree [2].", "Critics disagree [1-2].")
        rc = rv.cmd_verify_citations(type("A", (), {"report": _write_report(tmp_path, report)})())
        assert rc == 1

    # Codex 10: Sources entry without a URL must fail.
    def test_source_entry_without_url_fails(self, tmp_path):
        report = GOOD_REPORT.replace(
            "| 2 | Market Analysis Quarterly (https://news.example/market) | https://news.example/market | secondary | 2026-09-24 |",
            "| 2 | Market Analysis Quarterly | secondary | 2026-09-24 |")
        rc = rv.cmd_verify_citations(type("A", (), {"report": _write_report(tmp_path, report)})())
        assert rc == 1

    # Codex 12: empty report must fail, not pass vacuously.
    def test_empty_report_fails(self, tmp_path):
        p = tmp_path / "empty.md"
        p.write_text("# Nothing here\n", encoding="utf-8")
        rc = rv.cmd_verify_citations(type("A", (), {"report": str(p)})())
        assert rc == 1

    # Codex 16: malformed JSON input must produce an error, not a traceback.
    def test_malformed_json_arg_errors_cleanly(self, tmp_path):
        d = tmp_path / "run"
        d.mkdir()
        (d / "claims.jsonl").touch()
        with pytest.raises(SystemExit) as exc:
            rv.cmd_add_claim(type("A", (), {"dir": str(d), "json": "{not json"})())
        assert "malformed" in str(exc.value)

    # Codex 17: branch refs preserved in canonicalization.
    def test_branch_ref_preserved(self):
        assert rv.canonical_url("https://gitlab.com/g/r/-/blob/main/f?ref_type=heads") != \
            rv.canonical_url("https://gitlab.com/g/r/-/blob/dev/f?ref_type=heads")

    # Muse 16/Codex: invalid claim kind refused at the store boundary.
    def test_invalid_kind_refused(self, run_dir):
        rc = rv.cmd_add_claim(type("A", (), {"dir": str(run_dir), "json": json.dumps(
            {"claim_id": "k9", "claim": "x", "kind": "factoial"})})())
        assert rc == 2