#!/usr/bin/env python3
"""Queue wiki pages for publishing to Notion (agent performs the write)."""

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
from notion_mirror import NotionConfigError, queue_wiki_page  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    try:
        argv = apply_company_flag(argv if argv is not None else sys.argv[1:])
    except RegistryError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to company.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    pub = sub.add_parser("publish", help="Queue one or more wiki Markdown files")
    pub.add_argument("files", nargs="+", help="Paths inside the wiki")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if config is None:
        return 1
    out = {"queued": [], "unchanged": [], "refused": []}
    for f in args.files:
        try:
            action = queue_wiki_page(config, f)
        except NotionConfigError as exc:
            out["refused"].append({"file": f, "reason": str(exc)})
            continue
        if action is None:
            out["unchanged"].append(f)
        else:
            # Never echo page content: id, type and title only.
            out["queued"].append({"action_id": action["id"], "type": action["type"],
                                  "title": action["payload"]["title"]})
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 1 if out["refused"] and not out["queued"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
