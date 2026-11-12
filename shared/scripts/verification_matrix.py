#!/usr/bin/env python3
"""Live-integration verification matrix — what has actually run, and where.

``workspace_capabilities.CAPABILITIES`` says what a provider *supports*. This
module says how far each supported action has been *proven*:

``live``
    Executed against a real account. Needs a date (``YYYY-MM-DD``, or
    ``YYYY-MM`` when only the month is recorded) and a sentence of evidence.
``catalog``
    The vendor's published tool catalog lists the tool with the arguments we
    send, but it has never been executed. Needs a ``source``.
``mocked``
    Code and unit tests against a mocked transport only. The default for a
    supported action with no entry here.
``unverified``
    Nothing at all. Should be rare; prefer turning the capability off.

A ``live`` label is a claim about the outside world, so it is only added by a
maintainer, in a reviewed change, from an evidence file. Evidence files are
plain, unsigned JSON: they are an input to that review, never a trust anchor. The opt-in harness
(``connect_workspace.py --verify-writes --record-evidence``) writes that file
under ``<project_root>/verify/evidence/``, and :func:`promotion_candidates`
lists what it could justify. Nothing here edits the matrix by itself, and
``tests/test_verification_matrix.py`` refuses a ``live`` entry without a date
and evidence.

Evidence uses neutral action keys (``mail.send``, not ``gmail.send``).
"""
from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import workspace_capabilities as _caps  # noqa: E402

LEVELS = ("live", "catalog", "mocked", "unverified")

# Providers that are another spelling of the same capability set.
PROVIDER_ALIASES = {
    "composio:mcp": "composio",
    "composio_google": "composio",
    "composio_microsoft:mcp": "composio_microsoft",
}

_GMAIL_CATALOG = ("GMAIL_LIST_LABELS / GMAIL_BATCH_MODIFY_MESSAGES slugs from the Composio Gmail "
                  "toolkit catalog (wired v0.7.9–v0.7.10); not executed live")


def _live(date: str, evidence: str) -> dict[str, str]:
    return {"level": "live", "date": date, "evidence": evidence}


def _catalog(source: str) -> dict[str, str]:
    return {"level": "catalog", "source": source}


_GMAIL_CYCLE = ("Composio Gmail: archive → unarchive → trash → untrash cycle plus label apply "
                "on real message ids (write_ready: yes, v0.3.14)")
_OUTLOOK_MOVE = ("Composio Outlook: archive → unarchive → trash → untrash → trash cycle on "
                 "OUTLOOK_MOVE_MESSAGE, cleaned up (v0.3.10)")
_OUTLOOK_P3 = "Composio Outlook Phase 3: folders listed, message moved, approval-gated send (v0.3.11)"
_OUTLOOK_P4 = "Composio Outlook Phase 4: CoS-Verify category created and applied (v0.3.12)"
_ONEDRIVE = ("Composio OneDrive: throwaway file downloaded, then trashed to the recycle bin "
             "(v0.3.12)")
_UPLOAD = ("throwaway text and binary (.pdf) files uploaded with only the MCP key "
           "(remote-sandbox staging), then trashed (v0.3.16)")
_CONTACTS = ("google_api.py contacts subcommands against a Google Workspace account; "
             "an install whose script has the write subcommands (v0.7.1)")

EVIDENCE: dict[str, dict[str, dict[str, str]]] = {
    "google_api": {
        "mail.draft": _live("2026-07-16", "Gmail users.drafts.create via the service account; "
                                          "the draft landed in the delegate's Drafts (v0.3.15)"),
        "contacts.list": _live("2026-09-25", _CONTACTS),
        "contacts.create": _live("2026-09-25", _CONTACTS),
        "contacts.update": _live("2026-09-25", _CONTACTS),
        "contacts.delete": _live("2026-09-25", _CONTACTS),
    },
    "composio": {
        "mail.send": _live("2026-07-16", "GMAIL_SEND_EMAIL sent and received (v0.3.13)"),
        "mail.list_tags": _live("2026-07-16", "GMAIL_LIST_LABELS against live Gmail (v0.3.13)"),
        "mail.create_tag": _live("2026-07-16", "GMAIL_CREATE_LABEL against live Gmail (v0.3.13)"),
        "mail.tag": _live("2026-07-16", _GMAIL_CYCLE),
        "mail.archive": _live("2026-07-16", _GMAIL_CYCLE),
        "mail.unarchive": _live("2026-07-16", _GMAIL_CYCLE),
        "mail.trash": _live("2026-07-16", _GMAIL_CYCLE),
        "mail.untrash": _live("2026-07-16", _GMAIL_CYCLE),
        "mail.list_folders": _catalog(_GMAIL_CATALOG),
        "mail.move": _catalog(_GMAIL_CATALOG),
        # files.download: the 2026-07-16 Drive run recorded trash only, so no live claim.
        "files.trash": _live("2026-07-16", "GOOGLEDRIVE_TRASH_FILE; file confirmed in Drive Trash (v0.3.15)"),
        "files.upload": _live("2026-07-17", "Composio Drive: " + _UPLOAD),
        "files.untrash": _live("2026-07-17", "GOOGLEDRIVE_UNTRASH_FILE: create → trash → untrash, "
                                             "then FIND confirmed it active (v0.3.17)"),
    },
    "composio_microsoft": {
        "mail.search": _live("2026-07", "OUTLOOK_QUERY_EMAILS against an active Outlook connection "
                                        "(v0.3.7 acceptance test)"),
        "calendar.list": _live("2026-07", "OUTLOOK_GET_CALENDAR_VIEW against an active Outlook "
                                          "connection (v0.3.7 acceptance test)"),
        "files.search": _catalog("ONE_DRIVE_SEARCH_ITEMS confirmed in the Composio catalog via "
                                 "COMPOSIO_SEARCH_TOOLS (v0.3.7); no dated live run recorded"),
        "mail.draft": _live("2026-07-16", "OUTLOOK_CREATE_DRAFT; drafts used by the move cycle (v0.3.10)"),
        "mail.send": _live("2026-07-16", _OUTLOOK_P3),
        "mail.list_folders": _live("2026-07-16", _OUTLOOK_P3),
        "mail.move": _live("2026-07-16", _OUTLOOK_P3),
        "mail.archive": _live("2026-07-16", _OUTLOOK_MOVE),
        "mail.unarchive": _live("2026-07-16", _OUTLOOK_MOVE),
        "mail.trash": _live("2026-07-16", _OUTLOOK_MOVE),
        "mail.untrash": _live("2026-07-16", _OUTLOOK_MOVE),
        "mail.list_tags": _live("2026-07-16", _OUTLOOK_P4),
        "mail.tag": _live("2026-07-16", _OUTLOOK_P4),
        "mail.create_tag": _live("2026-07-16", _OUTLOOK_P4),
        "calendar.create": _live("2026-07-16", "OUTLOOK_CALENDAR_CREATE_EVENT on live Outlook (v0.3.9)"),
        "calendar.update": _live("2026-07-16", "OUTLOOK_UPDATE_CALENDAR_EVENT on live Outlook (v0.3.9)"),
        "calendar.delete": _live("2026-07-16", "OUTLOOK_DELETE_CALENDAR_EVENT on live Outlook (v0.3.9)"),
        "files.download": _live("2026-07-16", _ONEDRIVE),
        "files.trash": _live("2026-07-16", _ONEDRIVE),
        "files.upload": _live("2026-07-17", "Composio OneDrive: " + _UPLOAD),
    },
    # Native Graph: code-complete, never run against a real Entra tenant.
    "m365": {},
}

# workspace_verify check → the actions a *pass* exercised. mail_send is never
# run by the harness, so it can never promote mail.send.
CHECK_ACTIONS: dict[str, tuple[str, ...]] = {
    "auth": (),
    "mail_read": ("mail.search",),
    "mail_folder_scoped": ("mail.search",),
    "mail_tags_list": ("mail.list_tags",),
    "calendar_read": ("calendar.list",),
    "files_read": ("files.search",),
    "mail_draft": ("mail.draft",),
    # Not mail.create_tag: the check passes when it reuses an existing label.
    "mail_tag_write": ("mail.tag",),
    "mail_move_write": ("mail.archive", "mail.unarchive", "mail.trash", "mail.untrash"),
    "files_write": ("files.upload", "files.download", "files.trash"),
    "mail_send": (),
    "calendar_write": ("calendar.create", "calendar.update", "calendar.delete"),
}

_AGENT_NOTE = (
    "The agent provider performs no workspace I/O itself: your connector does, under its "
    "own vendor's guarantees. What the plugin owns (claim → record-execution, approvals, "
    "audit) is covered by the test suite."
)
_UNVERIFIED_NOTE = (
    "This provider has never been run against a live account: every action below is "
    "proven only against a mocked transport. Treat it as unproven. A passing "
    "connect_workspace.py --verify --record-evidence run produces evidence that a "
    "maintainer reviews and adds to the matrix; recording alone changes nothing here."
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


def canonical_provider(provider: str) -> str:
    return PROVIDER_ALIASES.get(provider, provider)


def parse_date(text: Any) -> datetime.date | None:
    """``YYYY-MM-DD`` or month-precision ``YYYY-MM`` (read as the 1st)."""
    raw = str(text or "")
    if not _DATE_RE.match(raw):
        return None
    try:
        return datetime.date.fromisoformat(raw if len(raw) == 10 else f"{raw}-01")
    except ValueError:
        return None


def _supported_actions(provider: str) -> list[str]:
    table = _caps.CAPABILITIES.get(provider) or _caps.CAPABILITIES.get(canonical_provider(provider)) or {}
    return sorted(action for action, ok in table.items() if ok)


def level(provider: str, action: str) -> str:
    """The verification level of one action, or ``unsupported``."""
    neutral = _caps._resolve_action(action)
    if neutral not in _supported_actions(provider):
        return "unsupported"
    entry = EVIDENCE.get(canonical_provider(provider), {}).get(neutral)
    return entry["level"] if entry else "mocked"


def provider_summary(provider: str, supported: list[str] | None = None) -> dict[str, Any]:
    """Per-action levels, counts and a one-line verdict for one provider.

    ``supported`` narrows the actions to what this install really supports
    (e.g. google_api contacts writes depend on the installed script).
    """
    evidence = EVIDENCE.get(canonical_provider(provider), {})
    actions: dict[str, dict[str, Any]] = {}
    counts = {name: 0 for name in LEVELS}
    table = _supported_actions(provider)
    if supported is not None:
        allowed = {_caps._resolve_action(a) for a in supported}
        table = [a for a in table if a in allowed]
    for action in table:
        entry = dict(evidence.get(action) or {"level": "mocked"})
        actions[action] = entry
        counts[entry["level"]] += 1
    if canonical_provider(provider) == "agent":
        verified, note = True, _AGENT_NOTE
    else:
        verified = counts["live"] > 0
        note = "" if verified else _UNVERIFIED_NOTE
    return {
        "provider": provider,
        "provider_verified": verified,
        "counts": counts,
        "actions": actions,
        "note": note,
    }


def validate() -> list[str]:
    """Every problem with the matrix, as a sentence. Empty means clean."""
    problems: list[str] = []
    today = datetime.date.today()
    for provider, entries in EVIDENCE.items():
        if provider not in _caps.CAPABILITIES:
            problems.append(f"{provider}: not a provider in the capability table")
            continue
        for action, entry in entries.items():
            where = f"{provider}/{action}"
            if action in _caps.LEGACY_ACTION_ALIASES:
                problems.append(f"{where}: use the neutral key {_caps.LEGACY_ACTION_ALIASES[action]}")
                continue
            if not _caps.supports(provider, action):
                problems.append(f"{where}: evidence for an unsupported action")
            kind = entry.get("level")
            if kind not in LEVELS:
                problems.append(f"{where}: unknown level {kind!r}")
            elif kind == "live":
                date = parse_date(entry.get("date"))
                if date is None:
                    problems.append(f"{where}: live needs a date (YYYY-MM-DD or YYYY-MM)")
                elif date > today:
                    problems.append(f"{where}: live date {entry['date']} is in the future")
                if len(str(entry.get("evidence") or "").strip()) < 20:
                    problems.append(f"{where}: live needs a sentence of evidence")
            elif kind == "catalog" and not str(entry.get("source") or "").strip():
                problems.append(f"{where}: catalog needs a source")
    return problems


# ── evidence files from the opt-in harness ──────────────────────────────────

def _evidence_dir(config: Any) -> Path:
    from config_loader import get_project_root

    return Path(get_project_root(config)).expanduser() / "verify" / "evidence"


def record_evidence(config: Any, report: Mapping[str, Any], *, version: str) -> Path:
    """Write a verification run's outcome as a new evidence file.

    Statuses only: check details can quote provider errors or message text,
    so they stay in the console output and out of the file.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    provider = str(report.get("provider") or "unknown")
    data = {
        "provider": provider,
        "date": now.date().isoformat(),
        "recorded_at": now.isoformat(timespec="seconds"),
        "plugin_version": version,
        "read_ready": bool(report.get("read_ready")),
        "write_ready": report.get("write_ready"),
        "checks": {name: {"status": str((check or {}).get("status") or "not_tested")}
                   for name, check in (report.get("checks") or {}).items()},
    }
    folder = _evidence_dir(config)
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", provider)
    stamp = now.strftime("%Y%m%dT%H%M%S")
    for n in range(1000):
        path = folder / f"{safe}-{stamp}{'' if n == 0 else f'-{n}'}.json"
        try:
            with path.open("x", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            return path
        except FileExistsError:
            continue
    raise FileExistsError(f"could not pick a fresh evidence file name in {folder}")


def promotion_candidates(path: Path | str) -> list[dict[str, str]]:
    """Actions an evidence file could promote to ``live``. Never edits anything."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    provider = str(data.get("provider") or "")
    seen: dict[str, dict[str, str]] = {}
    for check, result in (data.get("checks") or {}).items():
        if (result or {}).get("status") != "pass":
            continue
        for action in CHECK_ACTIONS.get(check, ()):
            if level(provider, action) in ("live", "unsupported") or action in seen:
                continue
            seen[action] = {"provider": provider, "action": action, "check": check,
                            "date": str(data.get("date") or ""),
                            "plugin_version": str(data.get("plugin_version") or "")}
    return sorted(seen.values(), key=lambda c: c["action"])


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Verification matrix: what has run against a live account")
    parser.add_argument("--provider", help="One provider (default: all)")
    parser.add_argument("--validate", action="store_true", help="Exit 1 if the matrix has problems")
    parser.add_argument("--candidates", metavar="EVIDENCE_JSON",
                        help="List actions an evidence file could promote to live")
    args = parser.parse_args(argv)
    if args.validate:
        problems = validate()
        print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
        return 1 if problems else 0
    if args.candidates:
        print(json.dumps(promotion_candidates(args.candidates), indent=2))
        return 0
    providers = [args.provider] if args.provider else sorted(_caps.CAPABILITIES)
    print(json.dumps({p: provider_summary(p) for p in providers}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
