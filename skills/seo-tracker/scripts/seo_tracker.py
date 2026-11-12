#!/usr/bin/env python3
"""SEO tracker CLI: built-in checks, rankings and history over an agent-fetched crawl and SERP results."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

from company_registry import RegistryError, apply_company_flag  # noqa: E402
from config_loader import load_config  # noqa: E402
import seo_audit as seo  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    try:
        argv = apply_company_flag(argv if argv is not None else sys.argv[1:])
    except RegistryError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to company.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    an = sub.add_parser("analyze", help='Input: {"site": "acme.example", "pages": [...], "serp": [...]}')
    an.add_argument("--input", required=True)
    an.add_argument("--markdown", action="store_true", help="Human report instead of JSON")
    an.add_argument("--record", action="store_true", help="Keep a summary for the next comparison")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if config is None:
        return 1
    try:
        data = json.loads(Path(args.input).read_text(encoding="utf-8"))
        report = seo.analyze(data.get("pages") or [], data.get("serp") or [], site=str(data["site"]))
    except (OSError, ValueError, KeyError) as exc:
        print(f"seo-tracker: cannot read input: {exc}", file=sys.stderr)
        return 1
    delta = seo.compare(config, report)
    if args.record:
        seo.record(config, report)
    if args.markdown:
        print(seo.render_markdown(report, delta), end="")
    else:
        print(json.dumps({**report, "since_last": delta}, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
