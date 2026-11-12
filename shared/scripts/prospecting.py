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
* turns chosen prospects into queued ``pipeline.deal.add`` actions, which a
  human approves like any other.
"""

from __future__ import annotations

import hashlib
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
PURGEABLE = frozenset({"new", "dropped", "do_not_contact"})


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
    if icp.get("industries"):
        checks.append(_norm(lead.get("industry")) in {_norm(i) for i in icp["industries"]})
    if icp.get("countries"):
        checks.append(_norm(lead.get("country")) in {_norm(c) for c in icp["countries"]})
    if icp.get("min_employees") is not None or icp.get("max_employees") is not None:
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


def ingest(config: Any, leads: Iterable[Mapping[str, Any]], source: Mapping[str, Any], purpose: str) -> dict[str, Any]:
    """Add leads to the prospect list. Local only; contacts no one."""
    from state_db import load_store

    missing = [f for f in SOURCE_FIELDS if not str((source or {}).get(f) or "").strip()]
    if missing:
        raise ProspectError(f"source must record {', '.join(SOURCE_FIELDS)}; missing: {', '.join(missing)}")
    if not str(purpose or "").strip():
        raise ProspectError("purpose is required: say why these leads were collected")

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
            report["dropped_fields"] |= {k for k in raw if k not in ALLOWED_FIELDS}
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
                    {"envelope_id": source["envelope_id"]} if source.get("envelope_id") else {}),
                "purpose": str(purpose).strip(), "collected_at": now, "status": "new",
                "icp_fit": _icp_fit(config, lead),
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
        if p.get("status") != "new" or f"prospect:{pid}" in open_targets:
            continue
        src = p.get("source") or {}
        payload = {
            "client_name": p["company"], "contact_name": p.get("contact_name") or "",
            "contact_email": p.get("email") or "", "value": 0,
            "source": f"prospecting:{src.get('provider')}/{src.get('run_id')}",
            "notes": [{"at": _now().isoformat(),
                       "note": f"Prospect from {src.get('provider')} {src.get('endpoint')} for: {p.get('purpose')}"}],
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
