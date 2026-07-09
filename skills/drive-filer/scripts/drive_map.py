#!/usr/bin/env python3
"""Resolve Google Drive filing targets from Chief-of-Staff drive-map rules."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    print(f"PyYAML is required for drive_map.py: {exc}", file=sys.stderr)
    raise SystemExit(2)

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

try:
    from config_loader import load_config  # type: ignore
except Exception as exc:  # pragma: no cover
    print(
        f"Chief-of-Staff bootstrap incomplete: cannot import config_loader from {SHARED_SCRIPTS}: {exc}. "
        "Run plugin bootstrap first.",
        file=sys.stderr,
    )
    raise SystemExit(2)

QUARANTINE = "00_Inbox/"


def configure(path: str | None) -> Any:
    if path:
        os.environ["CHIEF_OF_STAFF_CONFIG"] = path
    cfg = load_config(path)
    if cfg is None:
        raise RuntimeError("Could not load company.yaml; pass --config or set CHIEF_OF_STAFF_CONFIG")
    return cfg


def drive_map_path(config: Any) -> Path:
    source = getattr(config, "source_path", None)
    candidates = []
    if source:
        candidates.append(Path(source).parent / "drive-map.yaml")
    candidates.append(PLUGIN_ROOT / "shared" / "config" / "drive-map.yaml")
    for path in candidates:
        if path.exists():
            return path.resolve()
    raise FileNotFoundError("drive-map.yaml not found beside company.yaml or in shared/config/")


def load_map(config: Any) -> dict[str, Any]:
    path = drive_map_path(config)
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a top-level mapping")
    loaded["_path"] = str(path)
    return loaded


def canonical_rule_name(rule: dict[str, Any]) -> str:
    if rule.get("name"):
        return str(rule["name"])
    patterns = [str(p).lower() for p in rule.get("pattern", []) or []]
    direction = str(rule.get("direction", "")).lower()
    if "invoice" in patterns and direction == "received":
        return "invoice_received"
    if "invoice" in patterns and direction == "sent":
        return "invoice_sent"
    if any("nda" == p or "non-disclosure" in p for p in patterns):
        return "nda"
    if any("sow" == p or "statement of work" in p for p in patterns):
        return "sow"
    if patterns:
        return re.sub(r"[^a-z0-9]+", "_", patterns[0]).strip("_")
    return "unnamed"


def folder_for_rule(rule: dict[str, Any], counterparty: str | None) -> str:
    target = str(rule.get("target") or rule.get("fallback") or QUARANTINE)
    value = (counterparty or "Unknown Counterparty").strip()
    return target.replace("{client}", value).replace("{counterparty}", value)


def resolve_folder(args: argparse.Namespace) -> dict[str, Any]:
    cfg = configure(args.config)
    drive_map = load_map(cfg)
    rules = drive_map.get("filing_rules", []) or []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        name = canonical_rule_name(rule)
        if name == args.rule:
            return {
                "rule": name,
                "counterparty": args.counterparty,
                "folder": folder_for_rule(rule, args.counterparty),
                "drive_map": drive_map.get("_path"),
            }
    raise KeyError(f"No filing rule named {args.rule!r}")


def confidence_for(rule: dict[str, Any], filename: str, sender: str | None) -> float:
    haystack = f"{filename} {sender or ''}".lower()
    score = 0.0
    for raw in rule.get("pattern", []) or []:
        pattern = str(raw)
        if pattern.lower() == "default":
            continue
        try:
            if re.search(pattern, haystack, flags=re.IGNORECASE):
                # Filename hits are stronger than sender-domain/context hits.
                if re.search(pattern, filename, flags=re.IGNORECASE):
                    score = max(score, 0.85)
                else:
                    score = max(score, 0.55)
        except re.error:
            # Treat malformed user patterns as plain literals for suggestion only;
            # validate-map reports them as invalid.
            if pattern.lower() in haystack:
                score = max(score, 0.75)
    return round(min(score, 0.99), 2)


def suggest_target(args: argparse.Namespace) -> dict[str, Any]:
    cfg = configure(args.config)
    drive_map = load_map(cfg)
    best_rule: dict[str, Any] | None = None
    best_score = 0.0
    for rule in drive_map.get("filing_rules", []) or []:
        if not isinstance(rule, dict):
            continue
        score = confidence_for(rule, args.filename, args.sender)
        if score > best_score:
            best_rule = rule
            best_score = score
    if best_rule is None or best_score < 0.7:
        return {
            "filename": args.filename,
            "sender": args.sender,
            "suggested_folder": QUARANTINE,
            "confidence": best_score,
            "rule": None,
            "quarantine": True,
            "reason": "confidence below 0.70; quarantined instead of guessing",
        }
    return {
        "filename": args.filename,
        "sender": args.sender,
        "suggested_folder": folder_for_rule(best_rule, args.counterparty),
        "confidence": best_score,
        "rule": canonical_rule_name(best_rule),
        "quarantine": False,
    }


def validate_map(args: argparse.Namespace) -> dict[str, Any]:
    cfg = configure(args.config)
    drive_map = load_map(cfg)
    errors: list[str] = []
    warnings: list[str] = []
    if not str(drive_map.get("drive_root_id", "")).strip():
        errors.append("drive_root_id is empty")
    folders = drive_map.get("folders", {}) or {}
    if not isinstance(folders, dict):
        errors.append("folders must be a mapping")
    else:
        for name, folder_id in folders.items():
            if not str(folder_id or "").strip():
                errors.append(f"folders.{name} is empty")
    rules = drive_map.get("filing_rules", []) or []
    if not isinstance(rules, list) or not rules:
        errors.append("filing_rules must be a non-empty list")
    elif isinstance(rules, list):
        seen_default = False
        for idx, rule in enumerate(rules, start=1):
            if not isinstance(rule, dict):
                errors.append(f"filing_rules[{idx}] must be a mapping")
                continue
            patterns = rule.get("pattern")
            if not isinstance(patterns, list) or not patterns:
                errors.append(f"filing_rules[{idx}].pattern must be a non-empty list")
            else:
                for pattern in patterns:
                    if not isinstance(pattern, str) or not pattern.strip():
                        errors.append(f"filing_rules[{idx}] has an empty/non-string pattern")
                        continue
                    if pattern.lower() == "default":
                        seen_default = True
                        continue
                    try:
                        re.compile(pattern)
                    except re.error as exc:
                        errors.append(f"filing_rules[{idx}] pattern {pattern!r} is invalid regex: {exc}")
            if not str(rule.get("target", "")).strip():
                errors.append(f"filing_rules[{idx}].target is empty")
        if not seen_default:
            warnings.append("no default quarantine rule found; scripts still quarantine to 00_Inbox/")
    return {"valid": not errors, "errors": errors, "warnings": warnings, "drive_map": drive_map.get("_path")}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve Chief-of-Staff Drive filing rules")
    parser.add_argument("--config", help="Path to company.yaml (or CHIEF_OF_STAFF_CONFIG)")
    parser.add_argument("--json", action="store_true", default=True, help="Print JSON output")
    sub = parser.add_subparsers(dest="command", required=True)

    resolve = sub.add_parser("resolve-folder")
    resolve.add_argument("--rule", required=True)
    resolve.add_argument("--counterparty", required=True)

    suggest = sub.add_parser("suggest-target")
    suggest.add_argument("--filename", required=True)
    suggest.add_argument("--sender")
    suggest.add_argument("--counterparty")

    sub.add_parser("validate-map")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "resolve-folder":
            result = resolve_folder(args)
        elif args.command == "suggest-target":
            result = suggest_target(args)
        elif args.command == "validate-map":
            result = validate_map(args)
        else:  # pragma: no cover
            parser.error("unknown command")
            return 2
    except KeyError as exc:
        print(str(exc).strip("'"), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"drive_map.py error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if not (isinstance(result, dict) and result.get("valid") is False) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
