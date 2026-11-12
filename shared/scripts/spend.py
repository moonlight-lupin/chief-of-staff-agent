#!/usr/bin/env python3
"""Spend envelopes: pre-approved budgets for paid data calls (e.g. Monid).

Design: docs/MULTI_COMPANY.md, decision D3. A human approves an envelope once
— provider, purpose, topic, cap, currency, expiry — through the normal review
queue (``data.spend_envelope``, high risk). After that, each paid call on the
same topic reserves its estimated cost against the envelope *before* it runs:

* within the cap, on the topic, envelope approved, unexpired and untampered
  → the reservation is approved by the envelope (audited as ``envelope:<id>``);
* anything else → refused with a reason code, and the agent must come back to
  the human with a new envelope. The agent never approves its own spend.

Reservations are atomic (one SQLite write transaction), so concurrent callers
cannot overshoot the cap. Actual cost is recorded after the call; an overrun
is recorded truthfully and exhausts the envelope. A company-wide monthly cap
(``spend.<provider>.monthly_cap``) bounds all envelopes together.

    spend.py propose  --provider monid --purpose "..." --topic sg-fintech-leads --cap 50 --currency USD
    spend.py reserve  --envelope <id> --topic sg-fintech-leads --estimate 2.5 --endpoint <name>
    spend.py record   --reservation <id> --actual 2.1 [--run-id ...] [--status failure]
    spend.py release  --reservation <id>
    spend.py status   [--envelope <id>]
    spend.py stale    # open reservations older than a day, with the two ways out
    spend.py close    --envelope <id>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

ENVELOPE_TYPE = "data.spend_envelope"
LEDGER = "spend_ledger"
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
OPEN_ENVELOPE_STATES = frozenset({"requested", "approved"})
STALE_RESERVATION_HOURS = 24
LOCAL_ESTIMATE_NOTE = (
    "These figures are a local estimate built from the costs recorded with spend.py; "
    "they are not the provider's invoice. Reconcile against the provider's billing."
)


class SpendError(ValueError):
    """A spend request is malformed or not allowed."""


class SpendRefused(SpendError):
    """A reservation was refused; ``code`` says why. Do not make the call."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dec(value: Any, label: str = "amount") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SpendError(f"{label} {value!r} is not a number") from exc


def _num(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(round(value, 4))


def _spend_config(config: Any) -> dict[str, Any]:
    try:
        raw = config.get("spend") or {}
    except AttributeError:
        raw = {}
    return raw if hasattr(raw, "get") else {}


def _monthly_cap(config: Any, provider: str) -> Decimal | None:
    section = _spend_config(config).get(provider) or {}
    cap = section.get("monthly_cap") if hasattr(section, "get") else None
    return _dec(cap, "monthly_cap") if cap not in (None, "") else None


def _audit(config: Any, provider: str, operation: str, target: str, status: str, extra: dict[str, Any]) -> None:
    try:
        from workspace_audit import audit_workspace_action

        audit_workspace_action(config, provider, operation, "spend", target=target, status=status, extra=extra)
    except Exception:
        pass


# ------------------------------------------------------------------ ledger


def _load_ledger(config: Any) -> dict[str, Any]:
    from state_db import _open_db

    with _open_db(config) as db:
        return db.get_kv(LEDGER) or {}


def _mutate_ledger(config: Any, fn):
    from state_db import _open_db

    with _open_db(config) as db:
        return db.mutate_kv(LEDGER, fn)


def _envelope_totals(ledger: Mapping[str, Any], envelope_id: str) -> tuple[Decimal, Decimal, int]:
    spent = reserved = Decimal(0)
    calls = 0
    for r in (ledger.get("reservations") or {}).values():
        if r.get("envelope_id") != envelope_id:
            continue
        if r.get("status") == "open":
            reserved += _dec(r["estimate"])
        elif r.get("status") == "recorded":
            spent += _dec(r.get("actual") or 0)
            calls += 1
    return spent, reserved, calls


def _month_total(ledger: Mapping[str, Any], provider: str, month: str) -> Decimal:
    total = Decimal(0)
    for r in (ledger.get("reservations") or {}).values():
        if r.get("provider") != provider or r.get("month") != month:
            continue
        if r.get("status") == "open":
            total += _dec(r["estimate"])
        elif r.get("status") == "recorded":
            total += _dec(r.get("actual") or 0)
    return total


# ------------------------------------------------------------------ envelopes


def _envelopes(config: Any) -> list[dict[str, Any]]:
    from state_db import list_pending_actions

    return [a for a in list_pending_actions(config) if a.get("type") == ENVELOPE_TYPE]


def propose(config: Any, provider: str, purpose: str, topic: str, cap: Any, currency: str,
            days: int = 30, endpoints: list[str] | None = None) -> dict[str, Any]:
    """Queue a spend envelope for human approval. Spends nothing."""
    from state_db import create_pending_action

    provider = str(provider).strip().lower()
    topic = str(topic).strip()
    if not TOPIC_RE.match(topic):
        raise SpendError(f"topic {topic!r} must be a lowercase slug, e.g. 'sg-fintech-leads'")
    if not str(purpose).strip():
        raise SpendError("purpose is required: say what the money is for")
    amount = _dec(cap, "cap")
    if amount <= 0:
        raise SpendError("cap must be positive")
    configured = str(_spend_config(config).get("currency") or "").upper()
    currency = str(currency).strip().upper()
    if configured and currency != configured:
        raise SpendError(f"currency {currency} does not match spend.currency {configured}")
    for env in _envelopes(config):
        p = env.get("payload") or {}
        other = str(p.get("currency") or "").upper()
        if (p.get("provider") == provider and other and other != currency
                and env.get("state") in OPEN_ENVELOPE_STATES | {"executed"}):
            raise SpendError(f"currency {currency} differs from {other} on {provider} envelope {env['id']}: "
                             f"one provider's budgets must share a currency (set spend.currency)")
    monthly = _monthly_cap(config, provider)
    if monthly is not None and amount > monthly:
        raise SpendError(f"cap {amount} exceeds the monthly cap of {monthly} for {provider}")
    if int(days) < 1 or int(days) > 92:
        raise SpendError("days must be between 1 and 92")
    for env in _envelopes(config):
        p = env.get("payload") or {}
        if env.get("state") in OPEN_ENVELOPE_STATES and p.get("provider") == provider and p.get("topic") == topic:
            raise SpendError(f"an envelope for {provider}/{topic} is already open: {env['id']}")

    expires = (_now() + timedelta(days=int(days))).isoformat()
    payload = {"provider": provider, "purpose": str(purpose).strip(), "topic": topic, "cap": _num(amount),
               "currency": currency, "expires_at": expires,
               "endpoints": sorted({str(e) for e in endpoints}) if endpoints else None}
    return create_pending_action(
        config, ENVELOPE_TYPE, "local", f"spend:{provider}/{topic}", payload,
        summary=f"Budget up to {_num(amount)} {currency} of {provider} calls for: {payload['purpose']}",
    )


def _valid_envelope(config: Any, envelope_id: str) -> dict[str, Any]:
    from state_db import _config_company_id, approval_hash, get_pending_action

    env = get_pending_action(config, envelope_id)
    if not env or env.get("type") != ENVELOPE_TYPE:
        raise SpendRefused("unknown_envelope", f"{envelope_id} is not a spend envelope")
    if (env.get("company_id") or None) != _config_company_id(config):
        raise SpendRefused("company_mismatch", "this envelope belongs to another company")
    if env.get("state") != "approved":
        raise SpendRefused("not_approved", f"envelope is {env.get('state')}, not approved")
    if env.get("approval_hash") != approval_hash(env):
        raise SpendRefused("integrity", "envelope changed after approval")
    from approval_signing import enforced, verify_decision

    if enforced(config):   # signed approvals (docs/SECURITY_MODEL.md): budgets too
        reason = verify_decision(config, env, "approve", str(env.get("approver") or "").strip(),
                                 env.get("approval_signature"))
        if reason:
            raise SpendRefused(reason, "envelope approval is not signed: re-approve it with /cos approve")
    expires = env["payload"].get("expires_at")
    if expires and datetime.fromisoformat(expires) <= _now():
        raise SpendRefused("expired", f"envelope expired at {expires}")
    return env


def reserve(config: Any, envelope_id: str, topic: str, estimate: Any, endpoint: str,
            note: str = "", currency: str | None = None) -> dict[str, Any]:
    """Reserve the estimated cost of one paid call before making it."""

    provider = "unknown"
    try:
        amount = _dec(estimate, "estimate")
        if amount < 0:
            raise SpendRefused("invalid_amount", "estimate cannot be negative")
        env = _valid_envelope(config, envelope_id)
        p = env["payload"]
        provider = p["provider"]
        if currency and str(currency).strip().upper() != str(p.get("currency") or "").upper():
            raise SpendRefused("currency_mismatch",
                               f"estimate is in {str(currency).upper()}, the envelope is in {p.get('currency')}")
        if topic != p["topic"]:
            raise SpendRefused("topic_mismatch",
                               f"envelope covers '{p['topic']}', not '{topic}'; a new topic needs a new envelope")
        if p.get("endpoints") and endpoint not in p["endpoints"]:
            raise SpendRefused("endpoint_not_allowed",
                               f"envelope allows {', '.join(p['endpoints'])}, not '{endpoint}'")
        cap = _dec(p["cap"])
        monthly = _monthly_cap(config, provider)
        now = _now()
        month = now.strftime("%Y-%m")
        res_id = f"res_{uuid.uuid4().hex[:10]}"

        def take(ledger: dict[str, Any]) -> Decimal:
            spent, reserved, _ = _envelope_totals(ledger, envelope_id)
            if (ledger.get("exhausted") or {}).get(envelope_id) or spent + reserved + amount > cap:
                raise SpendRefused("cap_exceeded",
                                   f"{spent} spent + {reserved} reserved + {amount} would exceed the cap of {cap}")
            if monthly is not None and _month_total(ledger, provider, month) + amount > monthly:
                raise SpendRefused("monthly_cap_exceeded", f"would exceed the {provider} monthly cap of {monthly}")
            ledger.setdefault("reservations", {})[res_id] = {
                "envelope_id": envelope_id, "provider": provider, "topic": topic, "endpoint": endpoint,
                "note": note, "estimate": _num(amount), "actual": None, "status": "open",
                "created_at": now.isoformat(), "month": month,
            }
            return cap - spent - reserved - amount

        remaining = _mutate_ledger(config, take)
    except SpendRefused as exc:
        _audit(config, provider, "spend.reserve", f"{envelope_id}/{topic}", "refused",
               {"envelope_id": envelope_id, "estimate": str(estimate), "endpoint": endpoint,
                "reason": exc.code})
        raise
    _audit(config, provider, "spend.reserve", f"{envelope_id}/{topic}", "success",
           {"envelope_id": envelope_id, "reservation_id": res_id, "estimate": _num(amount),
            "endpoint": endpoint, "approver": f"envelope:{envelope_id}"})
    return {"reservation_id": res_id, "envelope_id": envelope_id, "estimate": _num(amount),
            "remaining": _num(remaining), "currency": p["currency"]}


def record(config: Any, reservation_id: str, actual: Any, status: str = "success",
           run_id: str | None = None) -> dict[str, Any]:
    """Record what a reserved call actually cost (0 if it was not charged)."""
    from state_db import get_pending_action

    amount = _dec(actual, "actual")
    if amount < 0:
        raise SpendError("actual cannot be negative")
    # Read the envelope BEFORE taking the ledger's write lock: opening another
    # connection inside the transaction would wait on that same lock.
    pending = (_load_ledger(config).get("reservations") or {}).get(reservation_id)
    if pending is None:
        raise SpendError(f"unknown reservation {reservation_id}")
    env = get_pending_action(config, pending["envelope_id"]) or {}
    cap = _dec((env.get("payload") or {}).get("cap") or 0)
    monthly = _monthly_cap(config, str(pending.get("provider") or ""))
    out: dict[str, Any] = {}

    def settle(ledger: dict[str, Any]) -> None:
        res = (ledger.get("reservations") or {}).get(reservation_id)
        if res is None:
            raise SpendError(f"unknown reservation {reservation_id}")
        if res.get("status") != "open":
            raise SpendError(f"reservation {reservation_id} is already {res.get('status')}")
        res.update({"status": "recorded", "actual": _num(amount), "call_status": status,
                    "run_id": run_id, "recorded_at": _now().isoformat()})
        spent, _, _ = _envelope_totals(ledger, res["envelope_id"])
        month_total = _month_total(ledger, res["provider"], res.get("month") or "")
        out.update({"envelope_id": res["envelope_id"], "provider": res["provider"], "spent": _num(spent),
                    "over_cap": spent > cap,
                    "over_monthly_cap": monthly is not None and month_total > monthly})
        if spent > cap:
            ledger.setdefault("exhausted", {})[res["envelope_id"]] = True

    _mutate_ledger(config, settle)
    _audit(config, out["provider"], "spend.record", out["envelope_id"],
           "over_cap" if out["over_cap"] else "over_monthly_cap" if out["over_monthly_cap"] else "success",
           {"reservation_id": reservation_id, "actual": _num(amount), "run_id": run_id, "call_status": status})
    return {"reservation_id": reservation_id, "actual": _num(amount), **out}


def release(config: Any, reservation_id: str) -> dict[str, Any]:
    """Cancel a reservation whose call was never made."""

    def free(ledger: dict[str, Any]) -> str:
        res = (ledger.get("reservations") or {}).get(reservation_id)
        if res is None or res.get("status") != "open":
            raise SpendError(f"reservation {reservation_id} is not open")
        res.update({"status": "released", "actual": 0, "recorded_at": _now().isoformat()})
        return res["provider"]

    provider = _mutate_ledger(config, free)
    _audit(config, provider, "spend.release", reservation_id, "success", {"reservation_id": reservation_id})
    return {"reservation_id": reservation_id, "status": "released"}


def status(config: Any, envelope_id: str | None = None) -> list[dict[str, Any]]:
    ledger = _load_ledger(config)
    out: list[dict[str, Any]] = []
    for env in _envelopes(config):
        if envelope_id and env["id"] != envelope_id:
            continue
        if not envelope_id and env.get("state") not in OPEN_ENVELOPE_STATES:
            continue
        p = env.get("payload") or {}
        spent, reserved, calls = _envelope_totals(ledger, env["id"])
        cap = _dec(p.get("cap") or 0)
        out.append({
            "envelope_id": env["id"], "state": env.get("state"), "provider": p.get("provider"),
            "topic": p.get("topic"), "purpose": p.get("purpose"), "cap": _num(cap),
            "currency": p.get("currency"), "spent": _num(spent), "reserved": _num(reserved),
            "remaining": _num(max(cap - spent - reserved, Decimal(0))), "calls": calls,
            "expires_at": p.get("expires_at"),
            "exhausted": bool((ledger.get("exhausted") or {}).get(env["id"])),
        })
    return out


def close(config: Any, envelope_id: str) -> dict[str, Any]:
    """End an envelope; no further calls can reserve against it."""
    from state_db import get_pending_action, mark_executed, mark_executing

    env = get_pending_action(config, envelope_id)
    if not env or env.get("type") != ENVELOPE_TYPE:
        raise SpendError(f"{envelope_id} is not a spend envelope")
    ledger = _load_ledger(config)
    spent, reserved, calls = _envelope_totals(ledger, envelope_id)
    if reserved:
        raise SpendError(f"{envelope_id} has open reservation(s) worth {reserved}; record or release them "
                         f"first (spend.py stale lists them with both commands)")
    summary = {"envelope_id": envelope_id, "spent": _num(spent), "calls": calls,
               "cap": (env.get("payload") or {}).get("cap"), "currency": (env.get("payload") or {}).get("currency")}
    if env.get("state") == "approved" and mark_executing(config, envelope_id) is not None:
        mark_executed(config, envelope_id, {"success": True, "closed": True, **summary})
    return summary


def month_to_date(config: Any) -> dict[str, Any]:
    ledger = _load_ledger(config)
    month = _now().strftime("%Y-%m")
    providers = {r.get("provider") for r in (ledger.get("reservations") or {}).values()}
    providers |= {k for k, v in _spend_config(config).items() if hasattr(v, "get")}
    return {p: _num(_month_total(ledger, p, month)) for p in sorted(x for x in providers if x)}


def stale_reservations(config: Any, hours: int = STALE_RESERVATION_HOURS) -> list[str]:
    cutoff = _now() - timedelta(hours=hours)
    return sorted(
        rid for rid, r in (_load_ledger(config).get("reservations") or {}).items()
        if r.get("status") == "open" and datetime.fromisoformat(r["created_at"]) < cutoff
    )


def stale_details(config: Any, hours: int = STALE_RESERVATION_HOURS) -> list[dict[str, Any]]:
    """Stale open reservations, each with the two honest ways to close it.

    Never resolved automatically: only the provider's records say whether the
    call ran (and was charged), so the operator or agent must check first.
    """
    now = _now()
    reservations = _load_ledger(config).get("reservations") or {}
    out = []
    for rid in stale_reservations(config, hours):
        r = reservations[rid]
        age = (now - datetime.fromisoformat(r["created_at"])).total_seconds() / 3600
        out.append({
            "reservation_id": rid, "envelope_id": r.get("envelope_id"), "provider": r.get("provider"),
            "topic": r.get("topic"), "endpoint": r.get("endpoint"), "estimate": r.get("estimate"),
            "created_at": r.get("created_at"), "age_hours": round(age, 1),
            "if_the_call_ran": f"spend.py record --reservation {rid} --actual <cost from the provider>",
            "if_it_did_not": f"spend.py release --reservation {rid}",
        })
    return out


def month_over_caps(config: Any) -> dict[str, dict[str, Any]]:
    """Providers whose month-to-date total is over their monthly cap."""
    over = {}
    for provider, total in month_to_date(config).items():
        cap = _monthly_cap(config, provider)
        if cap is not None and _dec(total) > cap:
            over[provider] = {"spent": total, "cap": _num(cap)}
    return over


def briefing_summary(config: Any) -> dict[str, Any] | None:
    """Spend section for the daily briefing, or None when nothing is configured or used."""
    envelopes = [e for e in status(config) if e["state"] == "approved"]
    mtd = month_to_date(config)
    if not envelopes and not any(mtd.values()) and not _spend_config(config):
        return None
    caps = {p: _num(c) for p in mtd if (c := _monthly_cap(config, p)) is not None}
    return {"envelopes": envelopes, "month_to_date": mtd, "monthly_caps": caps,
            "currency": _spend_config(config).get("currency"),
            "stale_reservations": stale_reservations(config),
            "over_monthly_caps": month_over_caps(config), "note": LOCAL_ESTIMATE_NOTE}


# ------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    from company_registry import RegistryError, apply_company_flag
    from config_loader import load_config

    try:
        argv = apply_company_flag(argv if argv is not None else sys.argv[1:])
    except RegistryError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Spend envelopes for paid data calls")
    parser.add_argument("--config", help="Path to company.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("propose", help="Queue a budget for human approval")
    p.add_argument("--provider", default="monid")
    p.add_argument("--purpose", required=True)
    p.add_argument("--topic", required=True)
    p.add_argument("--cap", required=True)
    p.add_argument("--currency", required=True)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--endpoint", action="append", default=None, help="Restrict to these endpoints")
    r = sub.add_parser("reserve", help="Reserve the estimated cost of one call BEFORE making it")
    r.add_argument("--envelope", required=True)
    r.add_argument("--topic", required=True)
    r.add_argument("--estimate", required=True)
    r.add_argument("--endpoint", required=True)
    r.add_argument("--note", default="")
    r.add_argument("--currency", help="Currency of the estimate; refused if it differs from the envelope's")
    rec = sub.add_parser("record", help="Record the actual cost after the call")
    rec.add_argument("--reservation", required=True)
    rec.add_argument("--actual", required=True)
    rec.add_argument("--run-id")
    rec.add_argument("--status", choices=["success", "failure"], default="success")
    rel = sub.add_parser("release", help="Release a reservation whose call was not made")
    rel.add_argument("--reservation", required=True)
    st = sub.add_parser("status", help="Envelopes, spend and remaining budget")
    st.add_argument("--envelope")
    sub.add_parser("stale", help="Open reservations older than a day, and how to close each")
    cl = sub.add_parser("close", help="End an envelope")
    cl.add_argument("--envelope", required=True)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if config is None:
        return 1
    try:
        if args.command == "propose":
            env = propose(config, args.provider, args.purpose, args.topic, args.cap, args.currency,
                          days=args.days, endpoints=args.endpoint)
            out: Any = {"envelope_id": env["id"], "state": env["state"], "summary": env["summary"],
                        "next_step": "Ask the user to approve it: review_queue.py preview / approve"}
        elif args.command == "reserve":
            out = reserve(config, args.envelope, args.topic, args.estimate, args.endpoint, args.note,
                          currency=args.currency)
        elif args.command == "record":
            out = record(config, args.reservation, args.actual, status=args.status, run_id=args.run_id)
        elif args.command == "release":
            out = release(config, args.reservation)
        elif args.command == "status":
            out = {"envelopes": status(config, args.envelope), "month_to_date": month_to_date(config),
                   "over_monthly_caps": month_over_caps(config), "note": LOCAL_ESTIMATE_NOTE}
        elif args.command == "stale":
            out = {"stale": stale_details(config),
                   "note": ("Check the provider's run history before choosing: record the actual cost if the "
                            "call ran, release only if it did not.")}
        else:
            out = close(config, args.envelope)
    except SpendRefused as exc:
        if exc.code == "not_approved" and getattr(args, "envelope", None):
            nxt = (f"Ask the user to review and approve envelope {args.envelope} "
                   f"(review_queue.py preview --action-id {args.envelope}).")
        else:
            nxt = "If the work should go on, propose a new envelope (spend.py propose ...) and ask the user to approve it."
        print(f"Spend refused ({exc.code}): {exc.message}. Do NOT make the call. {nxt}", file=sys.stderr)
        return 3
    except SpendError as exc:
        print(f"Spend error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
