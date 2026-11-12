#!/usr/bin/env python3
"""Prospect list: ingest leads fetched by the agent, minimise, dedupe, propose.

The agent finds and enriches leads (Monid, paid calls through spend
envelopes) and passes them in. This module never fetches and never contacts
anyone. It:

* records where every lead came from (provider, endpoint, run, time) and why;
* keeps only business contact fields (PDPA data minimisation): anything else
  is dropped and reported by field name, and personal mailbox addresses
  (gmail.com and similar) are dropped;
* skips leads already in the pipeline, already listed, or opted out — opt-outs
  are kept as hashes in a suppression list that outlives purges;
* blocks phone/SMS contact until the number is checked against the Do Not
  Call registry;
* stores buying signals as observations with evidence, date, confidence and
  attribution, and refuses industry-wide news as a company signal;
* records a written fit verdict per prospect and derives a tier (A/B/C), the
  anchor signal and a one-line "why now" deterministically from it;
* turns qualified prospects into queued ``pipeline.deal.add`` actions, which a
  human approves like any other;
* records replies (never their text), lets opt-out wording override the
  agent's classification, and pauses the whole account when anyone replies.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

STORE = "prospects"
ALLOWED_FIELDS = frozenset({
    "company", "website", "domain", "contact_name", "contact_role", "email", "phone", "linkedin",
    "industry", "country", "city", "employees",
})
SOURCE_FIELDS = ("provider", "endpoint", "run_id", "fetched_at")
DNC_STATES = frozenset({"unchecked", "clear", "registered"})
PURGEABLE = frozenset({"new", "dropped", "do_not_contact", "not_interested", "wrong_person"})
CONTACT_FIELDS = frozenset({"contact_name", "contact_role", "email", "phone", "linkedin"})

# Buying-signal vocabulary and weight (how strongly it says "in market now").
SIGNAL_WEIGHTS = {
    "hiring": 0.95, "funding": 0.95, "leadership_change": 0.9, "acquisition": 0.9,
    "expansion": 0.75, "product_launch": 0.75, "tech_change": 0.7, "tech_gap": 0.6,
    "partnership": 0.55, "tech_present": 0.4, "announcement": 0.25,
}
SIGNAL_REQUIRED = ("type", "summary", "evidence_url", "observed_at", "confidence")
ATTRIBUTIONS = frozenset({"named", "inferred", "industry"})
MIN_CONFIDENCE = 0.7          # below this a signal is never used
STRONG_SIGNAL = 0.5           # strength that counts as a trigger
RECENCY = ((14, 1.0), (30, 0.7), (60, 0.4), (90, 0.2))   # days -> factor; older is 0

REPLY_KINDS = frozenset({"interested", "question", "objection", "not_interested", "wrong_person",
                         "out_of_office", "unsubscribe", "escalate"})
POSITIVE_REPLIES = frozenset({"interested", "question"})
NON_PAUSING_REPLIES = frozenset({"out_of_office", "wrong_person"})
REPLY_STATUS = {"interested": "replied", "question": "replied", "objection": "replied",
                "not_interested": "not_interested", "wrong_person": "wrong_person",
                "escalate": "escalated", "unsubscribe": "do_not_contact"}
OPT_OUT_WORDING = re.compile(
    r"\b(unsubscribe|remove me|take me off|opt[\s-]?out|stop (?:e-?mailing|contacting|sending|messaging)"
    r"|(?:do not|don't|dont) (?:e-?mail|contact|message))\b", re.IGNORECASE)


class ProspectError(ValueError):
    """A prospect operation is not allowed or the input is malformed."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _generic_domains() -> frozenset[str]:
    from notion_inputs import GENERIC_MAIL_DOMAINS

    return GENERIC_MAIL_DOMAINS


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _company_key(value: Any) -> str:
    text = _norm(value)
    for suffix in (" pte ltd", " pte. ltd.", " sdn bhd", " limited", " ltd", " inc", " llc", " co"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text.strip(" .,")


def _hash(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def _domain(lead: Mapping[str, Any]) -> str | None:
    site = str(lead.get("domain") or lead.get("website") or "").strip()
    if site:
        host = urlparse(site if "//" in site else f"//{site}").netloc.lower()
        host = host.split("@")[-1].split(":")[0]
        return host[4:] if host.startswith("www.") else host or None
    email = str(lead.get("email") or "")
    if "@" in email:
        dom = email.rsplit("@", 1)[1].lower()
        return None if dom in _generic_domains() else dom
    return None


def _icp_fit(config: Any, lead: Mapping[str, Any]) -> float | None:
    try:
        icp = (config.get("prospecting") or {}).get("icp") or {}
    except AttributeError:
        icp = {}
    checks: list[bool] = []
    if icp.get("industries") and lead.get("industry"):
        checks.append(_norm(lead.get("industry")) in {_norm(i) for i in icp["industries"]})
    if icp.get("countries") and lead.get("country"):
        checks.append(_norm(lead.get("country")) in {_norm(c) for c in icp["countries"]})
    if (icp.get("min_employees") is not None or icp.get("max_employees") is not None) \
            and lead.get("employees") is not None:
        size = lead.get("employees")
        lo, hi = icp.get("min_employees"), icp.get("max_employees")
        checks.append(isinstance(size, (int, float)) and (lo is None or size >= lo) and (hi is None or size <= hi))
    return round(sum(checks) / len(checks), 2) if checks else None


def _load(config: Any) -> dict[str, Any]:
    from state_db import _open_db

    with _open_db(config) as db:
        return db.get_kv(STORE) or {}


def _mutate(config: Any, fn):
    from state_db import _open_db

    with _open_db(config) as db:
        return db.mutate_kv(STORE, fn)


def _audit(config: Any, operation: str, status: str, extra: dict[str, Any]) -> None:
    try:
        from workspace_audit import audit_workspace_action

        audit_workspace_action(config, "prospecting", operation, "prospects", status=status, extra=extra)
    except Exception:
        pass


def list_prospects(config: Any, status: str | None = None) -> list[dict[str, Any]]:
    return [p for p in _load(config).get("prospects") or [] if status is None or p.get("status") == status]


def _account_key(domain: str | None, company: str) -> str:
    return f"acct-{_hash(domain or company)[:12]}"


def _account(p: Mapping[str, Any]) -> str:
    return p.get("account_id") or _account_key(p.get("domain"), _company_key(p.get("company")))


def _parse_date(value: Any) -> datetime:
    text = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _clean_signals(raw: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Validate signals; return (usable records, count excluded as industry-wide)."""
    out: list[dict[str, Any]] = []
    excluded = 0
    for sig in raw:
        missing = [f for f in SIGNAL_REQUIRED if sig.get(f) in (None, "")]
        if missing:
            raise ProspectError(f"signal is missing: {', '.join(missing)}")
        if sig["type"] not in SIGNAL_WEIGHTS:
            raise ProspectError(f"unknown signal type {sig['type']!r}; use one of {sorted(SIGNAL_WEIGHTS)}")
        if not str(sig["evidence_url"]).startswith(("https://", "http://")):
            raise ProspectError("signal evidence_url must be an http(s) link to the source")
        try:
            observed = _parse_date(sig["observed_at"]).date().isoformat()
            confidence = float(sig["confidence"])
        except (TypeError, ValueError) as exc:
            raise ProspectError(f"signal observed_at/confidence malformed: {exc}") from exc
        if not 0 <= confidence <= 1:
            raise ProspectError("signal confidence must be between 0 and 1")
        attribution = sig.get("attribution") or "inferred"
        if attribution not in ATTRIBUTIONS:
            raise ProspectError(f"signal attribution must be one of {sorted(ATTRIBUTIONS)}")
        if attribution == "industry":
            excluded += 1
            continue
        out.append({"id": f"sig-{uuid.uuid4().hex[:8]}", "type": sig["type"],
                    "summary": " ".join(str(sig["summary"]).split())[:240],
                    "evidence_url": str(sig["evidence_url"]), "observed_at": observed,
                    "confidence": round(confidence, 2), "attribution": attribution,
                    "recorded_at": _now().isoformat()})
    return out, excluded


def signal_strength(sig: Mapping[str, Any]) -> float:
    """Weight x recency. 0 when unusable: too old or below MIN_CONFIDENCE."""
    if float(sig.get("confidence") or 0) < MIN_CONFIDENCE or sig.get("type") not in SIGNAL_WEIGHTS:
        return 0.0
    age = (_now() - _parse_date(sig["observed_at"])).days
    factor = next((f for days, f in RECENCY if age <= days), 0.0)
    return round(SIGNAL_WEIGHTS[sig["type"]] * factor, 3)


def _paid_envelope(config: Any, source: Mapping[str, Any]) -> str | None:
    """The spend envelope a paid source was bought under, or None for a free source.

    A provider with a ``spend:`` section is paid. Its data is accepted only when
    the ledger holds a *recorded* call with this ``run_id`` on an approved (or
    since-closed) envelope of this company. A claimed ``envelope_id`` must be
    that envelope. This stops paid data entering the pipeline outside a budget,
    or under an invented or borrowed envelope id.
    """
    import spend
    from state_db import _config_company_id, get_pending_action

    provider = str(source.get("provider") or "").strip().lower()
    paid = {str(k).lower() for k, v in spend._spend_config(config).items() if hasattr(v, "get")}
    if provider not in paid:
        return None
    run_id = str(source.get("run_id") or "").strip()
    reservations = (spend._load_ledger(config).get("reservations") or {}).values()
    envelopes = sorted({str(r.get("envelope_id")) for r in reservations
                        if r.get("status") == "recorded" and r.get("provider") == provider
                        and str(r.get("run_id") or "") == run_id})
    if not envelopes:
        raise ProspectError(
            f"{provider} is a paid source (spend.{provider}), but run {run_id!r} has no recorded spend. "
            f"Reserve before the call and record it with --run-id {run_id} (spend.py), then ingest.")
    claimed = str(source.get("envelope_id") or "").strip()
    if claimed and claimed not in envelopes:
        raise ProspectError(f"envelope_id {claimed!r} is not the envelope that recorded run {run_id!r}")
    env_id = claimed or envelopes[0]
    env = get_pending_action(config, env_id) or {}
    if (env.get("type") != spend.ENVELOPE_TYPE or (env.get("payload") or {}).get("provider") != provider
            or (env.get("company_id") or None) != _config_company_id(config)
            or env.get("state") not in {"approved", "executed"}):
        raise ProspectError(f"envelope {env_id!r} is not an approved {provider} spend envelope of this company")
    return env_id


def ingest(config: Any, leads: Iterable[Mapping[str, Any]], source: Mapping[str, Any], purpose: str) -> dict[str, Any]:
    """Add leads to the prospect list. Local only; contacts no one."""
    from state_db import load_store

    missing = [f for f in SOURCE_FIELDS if not str((source or {}).get(f) or "").strip()]
    if missing:
        raise ProspectError(f"source must record {', '.join(SOURCE_FIELDS)}; missing: {', '.join(missing)}")
    if not str(purpose or "").strip():
        raise ProspectError("purpose is required: say why these leads were collected")
    envelope_id = _paid_envelope(config, source)

    deals = load_store("pipeline", config).get("deals") or []
    deal_emails = {_norm(d.get("contact_email")) for d in deals if d.get("contact_email")}
    deal_companies = {_company_key(d.get("client_name")) for d in deals if d.get("client_name")}
    deal_domains = {_domain({"email": d.get("contact_email")}) for d in deals} - {None}
    generic = _generic_domains()
    now = _now().isoformat()
    report = {"added": 0, "duplicates": 0, "in_pipeline": 0, "suppressed": 0, "rejected": 0,
              "dropped_fields": set(), "personal_emails_dropped": 0}

    def add(data: dict[str, Any]) -> None:
        prospects = data.setdefault("prospects", [])
        suppressed = set(data.get("suppressed") or [])
        for raw in leads:
            lead = {k: v for k, v in dict(raw).items() if k in ALLOWED_FIELDS and v not in (None, "")}
            report["dropped_fields"] |= {k for k in raw if k not in ALLOWED_FIELDS | {"signals"}}
            signals, _ = _clean_signals(raw.get("signals") or [])
            if not str(lead.get("company") or "").strip():
                report["rejected"] += 1
                continue
            email = str(lead.get("email") or "").strip().lower() or None
            if email and email.rsplit("@", 1)[-1] in generic:
                email = None
                report["personal_emails_dropped"] += 1
            lead.pop("email", None)
            domain = _domain(lead) or _domain({"email": email})
            if (email and _hash(email) in suppressed) or (domain and _hash(domain) in suppressed):
                report["suppressed"] += 1
                continue
            company = _company_key(lead.get("company"))
            if (email and email in deal_emails) or company in deal_companies or (domain and domain in deal_domains):
                report["in_pipeline"] += 1
                continue
            if any((email and p.get("email") == email)
                   or (domain and p.get("domain") == domain)
                   or _company_key(p.get("company")) == company for p in prospects):
                report["duplicates"] += 1
                continue
            record = {
                "id": f"pr-{uuid.uuid4().hex[:10]}", **lead, "email": email, "domain": domain,
                "source": {f: source[f] for f in SOURCE_FIELDS} | (
                    {"envelope_id": envelope_id} if envelope_id else {}),
                "purpose": str(purpose).strip(), "collected_at": now, "status": "new",
                "icp_fit": _icp_fit(config, lead), "account_id": _account_key(domain, company),
                "signals": signals,
                "dnc": {"phone": "unchecked"} if lead.get("phone") else {},
            }
            prospects.append({k: v for k, v in record.items() if v is not None or k in {"email", "icp_fit"}})
            report["added"] += 1

    _mutate(config, add)
    report["dropped_fields"] = sorted(report["dropped_fields"])
    _audit(config, "prospects.ingest", "success",
           {k: v for k, v in report.items()} | {"run_id": source.get("run_id"), "provider": source.get("provider")})
    return report


def _update_prospect(config: Any, prospect_id: str, fn) -> dict[str, Any]:
    def apply(data: dict[str, Any]) -> dict[str, Any]:
        for p in data.get("prospects") or []:
            if p.get("id") == prospect_id:
                fn(p)
                return dict(p)
        raise ProspectError(f"unknown prospect {prospect_id}")

    return _mutate(config, apply)


def _find(data: Mapping[str, Any], prospect_id: str) -> dict[str, Any]:
    for p in data.get("prospects") or []:
        if p.get("id") == prospect_id:
            return p
    raise ProspectError(f"unknown prospect {prospect_id}")


def observe(config: Any, prospect_id: str, signals: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Attach buying signals (with evidence) to a prospect. Duplicates are skipped."""
    clean, excluded = _clean_signals(signals)
    added = 0

    def apply(data: dict[str, Any]) -> None:
        nonlocal added
        p = _find(data, prospect_id)
        have = {(s["type"], s["evidence_url"]) for s in p.get("signals") or []}
        for sig in clean:
            if (sig["type"], sig["evidence_url"]) not in have:
                p.setdefault("signals", []).append(sig)
                added += 1

    _mutate(config, apply)
    _audit(config, "prospects.observe", "success",
           {"prospect_id": prospect_id, "added": added, "excluded_industry_wide": excluded})
    return {"id": prospect_id, "added": added, "excluded_industry_wide": excluded}


def _tier(fit: float | None, trigger: float) -> tuple[str, bool, list[str]]:
    flags: list[str] = []
    if fit is None:
        flags.append("fit_not_assessed")
        return ("B" if trigger >= STRONG_SIGNAL else "C"), False, flags
    if fit < 0.5:
        return "C", trigger >= STRONG_SIGNAL, flags
    if fit >= 0.75 and trigger >= STRONG_SIGNAL:
        return "A", False, flags
    if fit >= 0.75 or trigger >= STRONG_SIGNAL:
        return "B", False, flags
    return "C", False, flags


def assess(config: Any, prospect_id: str, verdict: str, reason: str) -> dict[str, Any]:
    """Record a written fit verdict; derive tier, anchor signal and why-now from stored facts."""
    if verdict not in {"fit", "no_fit"}:
        raise ProspectError("verdict must be 'fit' or 'no_fit'")
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise ProspectError("a written reason is required: say why, in a sentence a human can disagree with")

    def apply(data: dict[str, Any]) -> dict[str, Any]:
        p = _find(data, prospect_id)
        if p.get("status") not in {"new", "qualified", "dropped"}:
            raise ProspectError(f"{prospect_id} is {p.get('status')}; it can no longer be re-assessed")
        ranked = sorted(((signal_strength(s), s) for s in p.get("signals") or []),
                        key=lambda x: x[0], reverse=True)
        strength, anchor = ranked[0] if ranked and ranked[0][0] > 0 else (0.0, None)
        result: dict[str, Any] = {"verdict": verdict, "reason": reason, "assessed_at": _now().isoformat()}
        if verdict == "no_fit":
            result.update(tier=None, do_not_pursue=False, flags=[], anchor_signal=None, why_now=None)
            p["status"] = "dropped"
        else:
            tier, dnp, flags = _tier(p.get("icp_fit"), strength)
            why = None
            if anchor:
                age = (_now() - _parse_date(anchor["observed_at"])).days
                why = f"{anchor['summary']} ({age} days ago)"
            result.update(tier=tier, do_not_pursue=dnp, flags=flags, why_now=why,
                          anchor_signal={k: anchor[k] for k in ("id", "type", "summary", "evidence_url")}
                          if anchor else None)
            p["status"] = "qualified"
        p["assessment"] = result
        return {"id": prospect_id, "status": p["status"], **result}

    out = _mutate(config, apply)
    _audit(config, "prospects.assess", "success",
           {"prospect_id": prospect_id, "verdict": verdict, "tier": out["tier"]})
    return out


def add_contact(config: Any, prospect_id: str, contact: Mapping[str, Any]) -> dict[str, Any]:
    """Add another business contact at an existing prospect's account."""
    fields = {k: v for k, v in dict(contact).items() if k in CONTACT_FIELDS and v not in (None, "")}
    email = str(fields.pop("email", "") or "").strip().lower() or None
    if email and email.rsplit("@", 1)[-1] in _generic_domains():
        email = None

    def apply(data: dict[str, Any]) -> dict[str, Any]:
        base = _find(data, prospect_id)
        suppressed = set(data.get("suppressed") or [])
        if email and _hash(email) in suppressed:
            raise ProspectError("that address opted out; do not add it")
        if email and any(p.get("email") == email for p in data["prospects"]):
            raise ProspectError("that contact is already on the list")
        record = {k: base[k] for k in ("company", "website", "domain", "industry", "country", "city",
                                       "employees", "source", "purpose", "icp_fit") if k in base}
        record.update(fields, id=f"pr-{uuid.uuid4().hex[:10]}", email=email, account_id=_account(base),
                      signals=list(base.get("signals") or []), collected_at=_now().isoformat(), status="new",
                      dnc={"phone": "unchecked"} if fields.get("phone") else {})
        data["prospects"].append(record)
        return dict(record)

    out = _mutate(config, apply)
    _audit(config, "prospects.add_contact", "success", {"prospect_id": out["id"], "account_id": out["account_id"]})
    return out


def set_dnc(config: Any, prospect_id: str, state: str) -> dict[str, Any]:
    """Record the result of a Do Not Call registry check for the phone number."""
    if state not in DNC_STATES:
        raise ProspectError(f"DNC state must be one of {sorted(DNC_STATES)}")
    out = _update_prospect(config, prospect_id, lambda p: p.setdefault("dnc", {}).update(
        {"phone": state, "checked_at": _now().isoformat()}))
    _audit(config, "prospects.dnc", "success", {"prospect_id": prospect_id, "state": state})
    return out


def contact_allowed(config: Any, prospect_id: str, channel: str) -> bool:
    """Raise unless this prospect may be contacted on ``channel`` (email, phone, sms)."""
    p = next((x for x in list_prospects(config) if x.get("id") == prospect_id), None)
    if p is None:
        raise ProspectError(f"unknown prospect {prospect_id}")
    suppressed = set(_load(config).get("suppressed") or [])
    if p.get("status") == "do_not_contact" or (p.get("email") and _hash(p["email"]) in suppressed) \
            or (p.get("domain") and _hash(p["domain"]) in suppressed):
        raise ProspectError(f"{prospect_id} opted out; do not contact")
    status = p.get("status")
    if status == "escalated":
        raise ProspectError(f"{prospect_id}: reply escalated; held for a human to decide")
    if status in {"not_interested", "wrong_person", "dropped"}:
        raise ProspectError(f"{prospect_id} is {status}; do not contact")
    pause = (_load(config).get("accounts") or {}).get(_account(p)) or {}
    if pause.get("paused_by") and pause["paused_by"] != prospect_id:
        raise ProspectError(f"{prospect_id}: account paused, {pause['paused_by']} replied "
                            f"({pause.get('kind')}); work that thread, not a second contact")
    if channel in {"phone", "sms"}:
        if not p.get("phone"):
            raise ProspectError(f"{prospect_id} has no business phone number")
        if (p.get("dnc") or {}).get("phone") != "clear":
            raise ProspectError(
                f"{prospect_id}: phone not cleared against the DNC registry "
                f"({(p.get('dnc') or {}).get('phone', 'unchecked')}); check it, then prospects.py dnc")
    elif channel == "email":
        if not p.get("email"):
            raise ProspectError(f"{prospect_id} has no business email address")
    else:
        raise ProspectError(f"unknown channel {channel!r}")
    return True


def opt_out(config: Any, email: str | None = None, domain: str | None = None) -> dict[str, Any]:
    """Suppress an address or a whole domain permanently; matching prospects stop."""
    keys = [k.strip().lower() for k in (email, domain) if k and k.strip()]
    if not keys:
        raise ProspectError("give an email or a domain to opt out")
    hashes = {_hash(k) for k in keys}
    changed: list[str] = []

    def apply(data: dict[str, Any]) -> None:
        data["suppressed"] = sorted(set(data.get("suppressed") or []) | hashes)
        for p in data.get("prospects") or []:
            if (p.get("email") and _hash(p["email"]) in hashes) or (p.get("domain") and _hash(p["domain"]) in hashes):
                p["status"] = "do_not_contact"
                changed.append(p["id"])

    _mutate(config, apply)
    _audit(config, "prospects.opt_out", "success", {"suppressed": len(hashes), "prospects": changed})
    return {"suppressed": len(hashes), "prospects_marked": changed}


def reply(config: Any, prospect_id: str, kind: str, text: str | None = None) -> dict[str, Any]:
    """Record a reply. Opt-out wording in ``text`` overrides ``kind``. Text is never stored."""
    if kind not in REPLY_KINDS:
        raise ProspectError(f"unknown reply kind {kind!r}; use one of {sorted(REPLY_KINDS)}")
    overridden = bool(text and OPT_OUT_WORDING.search(text)) and kind != "unsubscribe"
    if overridden:
        kind = "unsubscribe"
    now = _now().isoformat()

    def apply(data: dict[str, Any]) -> dict[str, Any]:
        p = _find(data, prospect_id)
        p.setdefault("replies", []).append({"kind": kind, "at": now})
        if kind in REPLY_STATUS:
            p["status"] = REPLY_STATUS[kind]
        paused = kind not in NON_PAUSING_REPLIES
        if paused:
            data.setdefault("accounts", {})[_account(p)] = {"paused_by": prospect_id, "kind": kind, "at": now}
        return {"id": prospect_id, "kind": kind, "status": p.get("status"), "account_paused": paused,
                "overridden_to_unsubscribe": overridden, "_email": p.get("email")}

    out = _mutate(config, apply)
    email = out.pop("_email")
    if kind == "unsubscribe" and email:
        opt_out(config, email=email)   # permanent suppression, survives purges
    _audit(config, "prospects.reply", "success", {"prospect_id": prospect_id, "kind": kind,
                                                  "overridden": overridden})
    return out


def touch(config: Any, prospect_id: str, signal_id: str | None, step: int) -> dict[str, Any]:
    """Record that an approved message went out, and which signal it was built on."""
    contact_allowed(config, prospect_id, "email")

    def apply(data: dict[str, Any]) -> dict[str, Any]:
        p = _find(data, prospect_id)
        sig = next((s for s in p.get("signals") or [] if s["id"] == signal_id), None)
        if signal_id and sig is None:
            raise ProspectError(f"unknown signal {signal_id} for {prospect_id}")
        entry = {"step": int(step), "signal_id": signal_id, "signal_type": sig["type"] if sig else "none",
                 "at": _now().isoformat()}
        p.setdefault("touches", []).append(entry)
        return entry

    out = _mutate(config, apply)
    _audit(config, "prospects.touch", "success", {"prospect_id": prospect_id, **out})
    return out


def stats(config: Any) -> dict[str, Any]:
    """Reply rates by the signal the opener was built on, and by tier. Counts prospects."""
    by_signal: dict[str, dict[str, int]] = {}
    by_tier: dict[str, dict[str, int]] = {}
    for p in list_prospects(config):
        touches = p.get("touches") or []
        if not touches:
            continue
        kinds = [r["kind"] for r in p.get("replies") or [] if r["kind"] != "out_of_office"]
        tier = ((p.get("assessment") or {}).get("tier")) or "unassessed"
        for bucket, key in ((by_signal, touches[0]["signal_type"]), (by_tier, tier)):
            row = bucket.setdefault(key, {"touched": 0, "replied": 0, "positive": 0})
            row["touched"] += 1
            row["replied"] += bool(kinds)
            row["positive"] += any(k in POSITIVE_REPLIES for k in kinds)
    return {"by_signal": by_signal, "by_tier": by_tier}


def purge(config: Any, older_than_days: int, confirm: bool = False) -> dict[str, Any]:
    """Delete stale prospects (retention). Dry run unless ``confirm``; suppressions are kept."""
    cutoff = _now() - timedelta(days=int(older_than_days))

    def stale(p: Mapping[str, Any]) -> bool:
        return p.get("status") in PURGEABLE and datetime.fromisoformat(p["collected_at"]) <= cutoff

    if not confirm:
        return {"would_remove": sum(1 for p in list_prospects(config) if stale(p)), "confirmed": False}
    removed: list[str] = []

    def apply(data: dict[str, Any]) -> None:
        keep = []
        for p in data.get("prospects") or []:
            (removed.append(p["id"]) if stale(p) else keep.append(p))
        data["prospects"] = keep

    _mutate(config, apply)
    _audit(config, "prospects.purge", "success", {"removed": len(removed), "older_than_days": older_than_days})
    return {"removed": len(removed), "confirmed": True}


def propose(config: Any, prospect_ids: Iterable[str], stage: str | None = None) -> list[dict[str, Any]]:
    """Queue ``pipeline.deal.add`` for chosen prospects. Each needs human approval."""
    from state_db import create_pending_action, list_pending_actions

    open_targets = {a.get("target") for a in list_pending_actions(config)
                    if a.get("type") == "pipeline.deal.add" and a.get("state") in {"requested", "approved", "executing"}}
    by_id = {p["id"]: p for p in list_prospects(config)}
    created: list[dict[str, Any]] = []
    for pid in prospect_ids:
        p = by_id.get(pid)
        if p is None:
            raise ProspectError(f"unknown prospect {pid}")
        if p.get("status") != "qualified" or f"prospect:{pid}" in open_targets:
            continue
        src = p.get("source") or {}
        assessment = p.get("assessment") or {}
        why = f" Why now: {assessment['why_now']}." if assessment.get("why_now") else ""
        payload = {
            "client_name": p["company"], "contact_name": p.get("contact_name") or "",
            "contact_email": p.get("email") or "", "value": 0,
            "source": f"prospecting:{src.get('provider')}/{src.get('run_id')}",
            "notes": [{"at": _now().isoformat(),
                       "note": f"Tier {assessment.get('tier')}: {assessment.get('reason')}.{why} "
                               f"Prospect from {src.get('provider')} {src.get('endpoint')} for: {p.get('purpose')}"}],
        }
        if stage:
            payload["stage"] = stage
        action = create_pending_action(config, "pipeline.deal.add", "local", f"prospect:{pid}", payload,
                                       summary=f"Add {p['company']} to the pipeline (from prospecting)")
        if action:
            created.append(action)
            _update_prospect(config, pid, lambda rec, aid=action["id"]: rec.update(
                {"status": "proposed", "proposed_action": aid}))
    return created
