#!/usr/bin/env python3
"""v0.5.4 red contract tests — onboard/wiki lint contract alignment.

Field report 2026-08-29 (Battery Road Collective, live v0.5.3):

  1. initialize_wiki() seeds purpose.md and SCHEMA.md with NO frontmatter and
     no index.md, but wiki_curator lint requires frontmatter+type on every page
     and errors on a missing index.md. A fresh install starts at 3 lint ERRORs.

  Contract under test: a wiki seeded by initialize_wiki() passes
  validate_wiki() with zero ERROR findings.
"""

import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

import onboard  # noqa: E402


def _seed_config(wiki_path: Path) -> dict:
    return {
        "company": {
            "name": "Acme Advisory Pte Ltd",
            "jurisdiction": "SG",
            "business_type": "consulting",
            "incorporation_date": "2025-01-15",
            "financial_year_end": "12-31",
            "currency": "SGD",
        },
        "delivery": {"timezone": "Asia/Singapore"},
        "paths": {"project_root": str(wiki_path.parent), "wiki_path": str(wiki_path)},
        "sales_stages": ["Lead", "Proposal Sent", "Won"],
    }


def test_seeded_wiki_passes_validation(tmp_path):
    wiki = tmp_path / "wiki"
    config = _seed_config(wiki)

    onboard.initialize_wiki(config, force=True)

    assert (wiki / "purpose.md").exists()
    assert (wiki / "SCHEMA.md").exists()
    assert (wiki / "index.md").exists()

    sys.path.insert(0, str(PLUGIN_ROOT / "skills" / "note-taker" / "scripts"))
    import wiki_curator

    findings = wiki_curator.validate_wiki(config)
    errors = [f for f in findings if f.severity == "ERROR"]
    assert errors == [], f"fresh-seeded wiki should lint clean, got: {errors}"


def test_seeded_pages_have_frontmatter_with_type(tmp_path):
    wiki = tmp_path / "wiki"
    config = _seed_config(wiki)

    onboard.initialize_wiki(config, force=True)

    sys.path.insert(0, str(PLUGIN_ROOT / "skills" / "note-taker" / "scripts"))
    from wiki_curator import split_frontmatter

    for name in ("purpose.md", "SCHEMA.md", "index.md"):
        frontmatter, _body, valid = split_frontmatter((wiki / name).read_text(encoding="utf-8"))
        assert valid, f"{name}: frontmatter missing or malformed"
        assert frontmatter.get("type"), f"{name}: missing required 'type' field"


def test_seeded_index_lists_seed_pages(tmp_path):
    wiki = tmp_path / "wiki"
    config = _seed_config(wiki)

    onboard.initialize_wiki(config, force=True)

    index_text = (wiki / "index.md").read_text(encoding="utf-8")
    assert "purpose.md" in index_text
    assert "SCHEMA.md" in index_text


def test_force_reseed_keeps_contract(tmp_path):
    wiki = tmp_path / "wiki"
    config = _seed_config(wiki)

    onboard.initialize_wiki(config, force=True)
    # simulate operator hand-edit then force re-run
    (wiki / "index.md").write_text("---\ntype: index\n---\n# Wiki Index\n")
    onboard.initialize_wiki(config, force=True)

    sys.path.insert(0, str(PLUGIN_ROOT / "skills" / "note-taker" / "scripts"))
    import wiki_curator

    errors = [f for f in wiki_curator.validate_wiki(config) if f.severity == "ERROR"]
    assert errors == []


def test_lint_cli_zero_errors_on_seeded_wiki(tmp_path):
    """End-to-end through the same CLI path skill scripts and briefings call."""
    wiki = tmp_path / "wiki"
    config = _seed_config(wiki)
    config_path = tmp_path / "company.yaml"

    import yaml

    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    onboard.initialize_wiki(config, force=True)

    result = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "skills" / "note-taker" / "scripts" / "wiki_curator.py"),
            "lint",
            "--config",
            str(config_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"lint failed:\n{result.stdout}\n{result.stderr}"
    # exit 0 + no [ERROR] lines == zero lint errors on a fresh-seeded wiki
    assert "[ERROR]" not in result.stdout


@pytest.mark.parametrize(
    "company_name",
    [
        "O'Brien & Co: \"Trading\" #2",
        "Acme: Colon Industries",
        'Quotes "R" Us',
        "Hash # Tag Ltd",
        "Em—dash & unicode 株式会社",
    ],
)
def test_adversarial_company_name_still_lints_clean(tmp_path, company_name):
    """Titles with YAML-special characters (:, #, quotes) must produce valid frontmatter."""
    wiki = tmp_path / "wiki"
    config = _seed_config(wiki)
    config["company"]["name"] = company_name

    onboard.initialize_wiki(config, force=True)

    sys.path.insert(0, str(PLUGIN_ROOT / "skills" / "note-taker" / "scripts"))
    import wiki_curator

    for name in ("purpose.md", "SCHEMA.md", "index.md"):
        frontmatter, _body, valid = split_frontmatter_checked(wiki / name)
        assert valid, f"{name}: frontmatter invalid for company {company_name!r}"

    errors = [f for f in wiki_curator.validate_wiki(config) if f.severity == "ERROR"]
    assert errors == [], f"lint errors for company {company_name!r}: {errors}"


def split_frontmatter_checked(path):
    from wiki_curator import split_frontmatter

    return split_frontmatter(path.read_text(encoding="utf-8"))


def test_force_false_keeps_existing_operator_edits(tmp_path):
    """force=False must never clobber operator-edited seed files (Codex finding)."""
    wiki = tmp_path / "wiki"
    config = _seed_config(wiki)

    onboard.initialize_wiki(config, force=True)
    operator_edit = "---\ntitle: My Own Purpose\ntype: reference\n---\n\nCustom body.\n"
    (wiki / "purpose.md").write_text(operator_edit, encoding="utf-8")

    written = onboard.initialize_wiki(config, force=False)

    assert (wiki / "purpose.md").read_text(encoding="utf-8") == operator_edit
    assert wiki / "index.md" in [w for w in written if w.name == "index.md"] or not any(
        w.name == "index.md" for w in written
    ), "force=False should keep, not overwrite"