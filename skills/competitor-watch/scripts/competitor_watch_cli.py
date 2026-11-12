#!/usr/bin/env python3
"""Competitor watch CLI: diff an agent-captured snapshot against the last one."""

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
import competitor_watch as cw  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    try:
        argv = apply_company_flag(argv if argv is not None else sys.argv[1:])
    except RegistryError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to company.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("diff", help='Input: {"captured_at": ..., "competitors": {"x.com": {"pages": [...]}}}')
    d.add_argument("--input", required=True)
    d.add_argument("--save", action="store_true", help="Make this snapshot the new baseline")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if config is None:
        return 1
    try:
        snapshot = json.loads(Path(args.input).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"competitor-watch: cannot read input: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(cw.diff(config, snapshot, save=args.save), indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
