#!/usr/bin/env python3
"""v0.7.4 — pin the PR #27 gate fixes that shipped without tests, and the docs.

PR #27 fixed five false passes in the v0.7.2 gates but added tests for only
two of them. These pin the other three — reverting any of them now fails the
suite — and check the reference docs no longer describe the old behaviour.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = PLUGIN_ROOT / "skills" / "deep-research"
sys.path.insert(0, str(SKILL_DIR / "scripts"))
import research_validation as rv  # noqa: E402


def _ns(**kw):
    base = {"strict": False, "provider_used": None, "refute_none": None}
    base.update(kw)
    return type("A", (), base)()


def _run(fn, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(_ns(**kw))
    return rc, json.loads(buf.getvalue().strip().splitlines()[-1])


def _five_support_only(tmp_path):
    d = tmp_path / "run"
    _run(rv.cmd_init_run, dir=str(d), query="q", mode=None, provider=None)
    for i in range(5):
        sid = _run(rv.cmd_register_source, dir=str(d),
                   json=json.dumps({"url": f"https://s{i}.example/a", "quality": "primary"}))[1]["source_id"]
        _run(rv.cmd_add_claim, dir=str(d), json=json.dumps({
            "claim_id": f"c{i}", "claim": "Adoption grew 40% in 2025", "snippet": "Adoption grew 40% in 2025",
            "source_id": sid, "polarity": "support"}))
    return d


# PR #27 finding 2 — 'mm' is not a magnitude alias
def test_millimetres_are_not_millions():
    assert rv.support_score("The part is 2mm wide.", ["The part is 2m wide."]) < rv.PARTIAL_THRESHOLD
    assert "mm" not in rv._SUFFIX_ALIASES


# PR #27 finding 4 — a blank waiver is not a waiver
def test_blank_refute_waiver_is_ignored(tmp_path):
    d = _five_support_only(tmp_path)
    rc, out = _run(rv.cmd_verify_claims, dir=str(d), strict=True, refute_none="   ")
    assert rc == 1 and out["refute_warning"]
    assert "refute_none_reason" not in json.loads((d / "run_manifest.json").read_text())


def test_real_refute_waiver_still_waives(tmp_path):
    d = _five_support_only(tmp_path)
    rc, out = _run(rv.cmd_verify_claims, dir=str(d), strict=True, refute_none="searched recalls; none")
    assert rc == 0 and not out["refute_warning"]


# PR #27 finding 5 — legacy non-tier quality counts as secondary
def test_legacy_quality_counts_as_secondary():
    mix = rv._quality_mix([{"quality": "primary"}] + [{"quality": "official"}] * 4)
    assert mix == {"primary": 1, "secondary": 4, "tertiary": 0, "total": 5, "rating": "acceptable"}


# docs match the code
GATES_MD = (SKILL_DIR / "references" / "validation-gates.md").read_text(encoding="utf-8")


def test_gates_reference_no_longer_aliases_mm():
    assert "`mm` = `m`" not in GATES_MD and "`mln`/`mm`" not in GATES_MD
    assert "millimetre" in GATES_MD


def test_gates_reference_documents_supporting_snippets():
    lowered = GATES_MD.lower()
    assert "own snippet" in lowered, "[VERIFIED] needs each host's own snippet to support the claim"
    assert "paraphrase" in lowered, "document that paraphrased corroboration scores low"
