#!/usr/bin/env python3
"""Prospect list CLI: ingest, observe signals, assess, propose, record replies, opt out, purge."""

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
import prospecting as pr  # noqa: E402


def _emit(payload) -> int:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        argv = apply_company_flag(argv if argv is not None else sys.argv[1:])
    except RegistryError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to company.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    ing = sub.add_parser("ingest", help='Add leads: {"source": {...}, "purpose": "...", "leads": [...]}')
    ing.add_argument("--input", required=True)
    ls = sub.add_parser("list", help="List prospects (no contact details unless --full)")
    ls.add_argument("--status")
    ls.add_argument("--full", action="store_true")
    prop = sub.add_parser("propose", help="Queue pipeline.deal.add for prospects (needs approval)")
    prop.add_argument("ids", nargs="+")
    prop.add_argument("--stage")
    chk = sub.add_parser("can-contact", help="Check a prospect may be contacted on a channel")
    chk.add_argument("id")
    chk.add_argument("--channel", required=True, choices=["email", "phone", "sms"])
    dnc = sub.add_parser("dnc", help="Record the DNC registry result for a prospect's phone")
    dnc.add_argument("id")
    dnc.add_argument("--state", required=True, choices=sorted(pr.DNC_STATES))
    opt = sub.add_parser("opt-out", help="Suppress an email or domain permanently")
    opt.add_argument("--email")
    opt.add_argument("--domain")
    pur = sub.add_parser("purge", help="Delete stale prospects (dry run unless --confirm)")
    pur.add_argument("--older-than-days", type=int, required=True)
    pur.add_argument("--confirm", action="store_true", help="Only when the user asked for the purge")
    obs = sub.add_parser("observe", help="Attach buying signals (JSON list, with evidence) to a prospect")
    obs.add_argument("id")
    obs.add_argument("--input", required=True)
    asx = sub.add_parser("assess", help="Record a written fit verdict; returns tier, anchor signal, why-now")
    asx.add_argument("id")
    asx.add_argument("--verdict", required=True, choices=["fit", "no_fit"])
    asx.add_argument("--reason", required=True)
    addc = sub.add_parser("add-contact", help="Add another business contact at a prospect's account")
    addc.add_argument("id")
    addc.add_argument("--input", required=True, help='JSON {"contact_name", "contact_role", "email", ...}')
    rep = sub.add_parser("reply", help="Record a reply's category (text is checked for opt-out, never stored)")
    rep.add_argument("id")
    rep.add_argument("--kind", required=True, choices=sorted(pr.REPLY_KINDS))
    rep.add_argument("--text-file", help="Local file with the reply text, for the opt-out wording check")
    tch = sub.add_parser("touch", help="Record an approved message that went out, and its anchor signal")
    tch.add_argument("id")
    tch.add_argument("--signal-id")
    tch.add_argument("--step", type=int, required=True)
    sub.add_parser("stats", help="Reply rates by opener signal and by tier")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if config is None:
        return 1
    try:
        if args.command == "ingest":
            data = json.loads(Path(args.input).read_text(encoding="utf-8"))
            return _emit(pr.ingest(config, data.get("leads") or [], data.get("source") or {}, data.get("purpose") or ""))
        if args.command == "list":
            rows = pr.list_prospects(config, args.status)
            if not args.full:
                rows = [{**{k: r.get(k) for k in ("id", "account_id", "company", "domain", "contact_role", "industry",
                                                  "country", "icp_fit", "status")},
                         "signals": len(r.get("signals") or []),
                         **{k: (r.get("assessment") or {}).get(k) for k in ("tier", "why_now", "do_not_pursue")}}
                        for r in rows]
                rank = {"A": 0, "B": 1, "C": 2}
                rows.sort(key=lambda r: (rank.get(r["tier"], 3), -(r["icp_fit"] or 0)))
            return _emit(rows)
        if args.command == "observe":
            return _emit(pr.observe(config, args.id, json.loads(Path(args.input).read_text(encoding="utf-8"))))
        if args.command == "assess":
            return _emit(pr.assess(config, args.id, args.verdict, args.reason))
        if args.command == "add-contact":
            rec = pr.add_contact(config, args.id, json.loads(Path(args.input).read_text(encoding="utf-8")))
            return _emit({"id": rec["id"], "account_id": rec["account_id"], "status": rec["status"]})
        if args.command == "reply":
            text = Path(args.text_file).read_text(encoding="utf-8") if args.text_file else None
            return _emit(pr.reply(config, args.id, args.kind, text=text))
        if args.command == "touch":
            return _emit(pr.touch(config, args.id, args.signal_id, args.step))
        if args.command == "stats":
            return _emit(pr.stats(config))
        if args.command == "propose":
            return _emit([{"action_id": a["id"], "summary": a["summary"]} for a in pr.propose(config, args.ids, args.stage)])
        if args.command == "can-contact":
            return _emit({"id": args.id, "channel": args.channel, "allowed": pr.contact_allowed(config, args.id, args.channel)})
        if args.command == "dnc":
            pr.set_dnc(config, args.id, args.state)
            return _emit({"id": args.id, "dnc": args.state})
        if args.command == "opt-out":
            return _emit(pr.opt_out(config, args.email, args.domain))
        return _emit(pr.purge(config, args.older_than_days, confirm=args.confirm))
    except (pr.ProspectError, OSError, ValueError) as exc:
        print(f"prospects: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
