#!/usr/bin/env python3
"""Contacts writes for the google_api provider: which path can do them, and the People API path.

The google_api provider historically shelled out to the optional
google-workspace skill's ``google_api.py contacts create|update|delete``. The
shipped skill offers only ``contacts list``, so those writes failed at the
last step of an approved action. This module:

* probes what the installed script actually offers (``contacts --help``, no
  credentials, cached per script version);
* performs writes through the People API with the service account and
  delegate the provider already uses for drafts and calendar events;
* explains, in one sentence, why a write is impossible on this install.

Preference: People API when a service account is configured, else the CLI
when the probe shows the subcommand (or cannot read the surface — an
unreadable ``--help`` is not proof of absence).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

CONTACT_WRITES = {"contacts.create": "create", "contacts.update": "update", "contacts.delete": "delete"}
PEOPLE_SCOPE = "https://www.googleapis.com/auth/contacts"
PEOPLE_API = "https://people.googleapis.com/v1"
PERSON_FIELDS = "names,emailAddresses,phoneNumbers,organizations,biographies"
PROBE_TIMEOUT = 20

_PROBE_CACHE: dict[tuple[str, int, int], frozenset[str] | None] = {}
_CHOICES = re.compile(r"\bcontacts\b[^\n{]*\{([\w,-]+)\}")


# ─── what the installed script offers ────────────────────────────────────────

def probe_cli_contacts(script: Path | str) -> frozenset[str] | None:
    """Return the ``contacts`` subcommands ``script`` offers, or None if unreadable."""
    path = Path(script)
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    try:
        proc = subprocess.run(
            [sys.executable, str(path), "contacts", "--help"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False,
        )
        match = _CHOICES.search((proc.stdout or "") + "\n" + (proc.stderr or ""))
        surface = frozenset(match.group(1).split(",")) if match else None
    except (OSError, subprocess.SubprocessError):
        surface = None
    _PROBE_CACHE[key] = surface
    return surface


# ─── which path performs a write ─────────────────────────────────────────────

def service_account_settings(config: Any) -> tuple[str, str]:
    """(service_account_path, delegate_email) as the provider resolves them."""
    google = config.get("google", {}) if isinstance(config, Mapping) else {}
    google = google if isinstance(google, Mapping) else {}
    sa_path = str(google.get("service_account_path") or "").strip()
    if not sa_path:
        sa_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_PATH", "").strip()
    return sa_path, str(google.get("delegate_email") or "").strip()


def _rest_unavailable(config: Any) -> str | None:
    sa_path, delegate = service_account_settings(config)
    if not sa_path:
        return "no google.service_account_path is configured"
    if not Path(sa_path).expanduser().is_file():
        return f"the service account file {sa_path} does not exist"
    if not delegate:
        return "no google.delegate_email is configured"
    return None


def contacts_write_backend(config: Any, script: Path | str | None, action: str) -> tuple[str | None, str]:
    """Return ("rest" | "cli" | None, reason). The reason explains a None."""
    op = CONTACT_WRITES[action]
    rest_gap = _rest_unavailable(config)
    if rest_gap is None:
        return "rest", ""
    surface = probe_cli_contacts(script) if script else None
    if script and (surface is None or op in surface):
        return "cli", ""
    return None, _explain([action], script, surface, rest_gap)


def _explain(actions: list[str], script: Path | str | None, surface: frozenset[str] | None,
             rest_gap: str) -> str:
    ops = [CONTACT_WRITES[a] for a in actions]
    subs = ", ".join(f"`contacts {op}`" for op in ops)
    noun = "subcommand" if len(ops) == 1 else "subcommands"
    label = actions[0] if len(actions) == 1 else f"contacts writes ({', '.join(ops)})"
    if script:
        offered = ", ".join(sorted(surface or ())) or "nothing"
        cli_gap = f"the installed google_api.py ({script}) has no {subs} {noun} (it offers: {offered})"
    else:
        cli_gap = f"no google_api.py is installed to run {subs}"
    return (
        f"{label} cannot run on this install: {cli_gap}, and the People API path is unavailable "
        f"because {rest_gap}. Set google.service_account_path and google.delegate_email "
        f"(domain-wide delegation with the {PEOPLE_SCOPE} scope) to write contacts through the People API."
    )


def _installed_script() -> Path | None:
    try:
        from providers.google_workspace import _find_google_api_script
        return _find_google_api_script()
    except Exception:
        return None


def write_backends(config: Any) -> dict[str, tuple[str | None, str]]:
    """Backend and reason for every contacts write, for reports and doctor."""
    script = _installed_script()
    return {action: contacts_write_backend(config, script, action) for action in CONTACT_WRITES}


def unavailable_summary(config: Any) -> str | None:
    """One sentence covering every contacts write this install cannot do, or None."""
    script = _installed_script()
    missing = [a for a in CONTACT_WRITES if contacts_write_backend(config, script, a)[0] is None]
    if not missing:
        return None
    surface = probe_cli_contacts(script) if script else None
    return _explain(missing, script, surface, _rest_unavailable(config) or "")


# ─── People API ──────────────────────────────────────────────────────────────

def resource_name(person_id: str) -> str:
    person_id = person_id.strip()
    return person_id if person_id.startswith("people/") else f"people/{person_id}"


def merge_emails(email: str = "", emails: Any = None) -> list[str]:
    """``email`` first, then ``emails``, blanks and duplicates dropped."""
    extra = [emails] if isinstance(emails, str) else list(emails or [])
    out: list[str] = []
    for value in [email, *extra]:
        value = str(value or "").strip()
        if value and value not in out:
            out.append(value)
    return out


def _clean(entry: Any) -> dict[str, Any]:
    """Copy a People API entry without its read-only metadata."""
    return {k: v for k, v in dict(entry).items() if k != "metadata"} if isinstance(entry, Mapping) else {}


def _request(method: str, url: str, token: str, **kwargs: Any) -> dict[str, Any]:
    import requests

    call = getattr(requests, method)
    resp = call(url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                timeout=45, **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(f"People API {method.upper()} {url.rsplit('/', 1)[-1]} failed "
                           f"({resp.status_code}): {resp.text[:300]}")
    data = resp.json() if resp.content else {}
    return dict(data) if isinstance(data, Mapping) else {}


def rest_create(token: str, *, given_name: str, family_name: str = "", emails: list[str] | None = None,
                phone: str = "", organization: str = "", note: str = "") -> dict[str, Any]:
    name = {"givenName": given_name}
    if family_name:
        name["familyName"] = family_name
    body: dict[str, Any] = {"names": [name]}
    if emails:
        body["emailAddresses"] = [{"value": e} for e in emails]
    if phone:
        body["phoneNumbers"] = [{"value": phone}]
    if organization:
        body["organizations"] = [{"name": organization}]
    if note:
        body["biographies"] = [{"value": note, "contentType": "TEXT_PLAIN"}]
    return _request("post", f"{PEOPLE_API}/people:createContact", token,
                    params={"personFields": PERSON_FIELDS}, json=body)


def _replace_first(entries: Any, **fields: Any) -> list[dict[str, Any]]:
    """Set ``fields`` on the first entry (keeping its other keys) and keep the rest."""
    cleaned = [_clean(e) for e in (entries or [])]
    first = cleaned[0] if cleaned else {}
    first.update(fields)
    return [first, *cleaned[1:]]


def rest_update(token: str, person_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    """Merge-update: only the supplied field types change; others are untouched.

    ``email``/``phone``/``organization`` replace the first entry of their type
    and keep the rest; ``emails`` replaces the whole address list.
    """
    name = resource_name(person_id)
    current = _request("get", f"{PEOPLE_API}/{name}", token, params={"personFields": PERSON_FIELDS})
    body: dict[str, Any] = {"etag": current.get("etag", "")}
    touched: list[str] = []
    names = {k: fields[k] for k in ("given_name", "family_name") if fields.get(k)}
    if names:
        body["names"] = _replace_first(current.get("names"), **{
            {"given_name": "givenName", "family_name": "familyName"}[k]: v for k, v in names.items()})
        touched.append("names")
    if fields.get("emails"):
        body["emailAddresses"] = [{"value": e} for e in merge_emails(fields.get("email", ""), fields["emails"])]
        touched.append("emailAddresses")
    elif fields.get("email"):
        body["emailAddresses"] = _replace_first(current.get("emailAddresses"), value=fields["email"])
        touched.append("emailAddresses")
    if fields.get("phone"):
        body["phoneNumbers"] = _replace_first(current.get("phoneNumbers"), value=fields["phone"])
        touched.append("phoneNumbers")
    if fields.get("organization"):
        body["organizations"] = _replace_first(current.get("organizations"), name=fields["organization"])
        touched.append("organizations")
    if fields.get("note"):
        body["biographies"] = [{"value": fields["note"], "contentType": "TEXT_PLAIN"}]
        touched.append("biographies")
    return _request("patch", f"{PEOPLE_API}/{name}:updateContact", token,
                    params={"updatePersonFields": ",".join(touched), "personFields": PERSON_FIELDS},
                    json=body)


def rest_delete(token: str, person_id: str) -> dict[str, Any]:
    name = resource_name(person_id)
    _request("delete", f"{PEOPLE_API}/{name}:deleteContact", token)
    return {"status": "deleted", "person_id": name}
