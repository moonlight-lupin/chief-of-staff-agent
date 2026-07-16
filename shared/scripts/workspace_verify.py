#!/usr/bin/env python3
"""Per-capability verification of a configured workspace provider.

Replaces the single ``health_check()`` acceptance signal with a per-capability
probe.  On real Microsoft Entra (or Google Workspace) tenants, permissions and
admin consent can be PARTIALLY configured — mail may read fine while OneDrive
403s, or drafts work while categories are blocked — so a single boolean health
check is not enough to certify a provider is usable.  :func:`run_verification`
runs each capability in isolation and reports pass / fail / not_tested per check.

Report schema (``run_verification`` return value)::

    {
      "provider": <client.provider_name>,
      "checks": {
        <check name>: {"status": "pass"|"fail"|"not_tested", "detail": <short str>},
        ...
      },
      "read_ready": <bool>,          # see read_ready semantics below
      "write_ready": "yes"|"partial"|"no",
    }

``read_ready`` semantics (the reads the product actually depends on):
  ``read_ready`` is True iff ALL of ``auth``, ``mail_read``, ``mail_folder_scoped``,
  ``calendar_read`` and ``files_read`` pass.  The bundled daily queries rely on
  folder-scoped search (``in:inbox`` / ``label:INBOX``) and on OneDrive/Drive
  reads, so folder-scoped mail search and files_read are REQUIRED, not optional.
  ``mail_tags_list`` is explicitly OPTIONAL: its failure does NOT block
  ``read_ready`` — email organisation features are merely degraded — and its fail
  detail carries that wording.  Required checks: auth, mail_read,
  mail_folder_scoped, calendar_read, files_read.  Optional: mail_tags_list.

``write_ready`` semantics:
  * ``"yes"``     — ``include_writes`` was True AND at least one representative
                    write check actually ran AND every write check that ran
                    passed AND all cleanup succeeded.
  * ``"partial"`` — writes were not requested (``include_writes=False``), OR every
                    write check was unsupported / not tested, OR cleanup could not
                    be verified.
  * ``"no"``      — any tested write check failed OR any cleanup failed.
``mail_send`` is ALWAYS ``not_tested``: verification never auto-sends mail.
``calendar_write`` is ``not_tested`` unless ``include_calendar_writes=True``
(opt-in create → update → delete of a marked ``[CoS verify]`` event).

Read-check warning capture
--------------------------
The provider read methods (``mail_search``, ``mail_list_tags``, ``calendar_list``,
``files_search``) follow the "warn + return ``[]`` on error" convention, so an
empty list is ambiguous — genuinely empty vs a swallowed provider error.  Each
read is therefore run inside ``warnings.catch_warnings(record=True)``: if any
warning is emitted the check FAILS carrying the warning text; if no warning is
emitted the check PASSES even when the list is empty.

Write smoke checks (only when ``include_writes=True``)
------------------------------------------------------
Non-destructive by construction:
  * ``mail_draft``     — create a draft addressed to the operator/self address.
  * ``mail_tag_write`` — create (or reuse) the ``CoS-Verify`` tag/category and
                         apply it to the draft, then TRASH the draft to clean up.
  * ``files_write``    — upload a tiny temp file, then TRASH it.

CLEANUP-CAPABILITY PRECONDITIONS.  A write check is only run when the provider can
also clean up after it: ``mail_draft`` (and, transitively, ``mail_tag_write``,
which tags the draft) requires ``mail.trash`` support; ``files_write`` requires
``files.trash`` support.  When the cleanup capability is missing the check is
``not_tested`` (skipped to avoid leaving artefacts) rather than attempted.

CLEANUP FAILURE IS A FAILURE.  If a write itself succeeds but removing the
verification artefact (trashing the draft / trashing the uploaded file) fails, the
affected check becomes ``fail`` with a detail beginning "Write succeeded, but
verification artefact cleanup failed. Manual removal required." (naming the draft
subject marker / uploaded filename), and ``write_ready`` becomes ``"no"``.

The SAFE_WRITE auto-approve env var (``CHIEF_OF_STAFF_AUTO_APPROVE``) is set for
the duration of the write checks and restored in a ``finally`` block.
``CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE`` is NEVER set — destructive actions stay
blocked.  NOTE: the ``CoS-Verify`` category/label is created once and intentionally
PERSISTS on the mailbox (categories are reused across runs); only the verification
draft and uploaded file are cleaned up.
"""
from __future__ import annotations

import datetime
import json as _json
import os
import tempfile
import warnings
from typing import Any, Callable, Mapping

from workspace_client import get_workspace_client

try:  # Structured operational logging (safe no-op with no active run).
    from runtime_log import log_event as _log_event
    from runtime_log import sanitize_provider_error_detail as _sanitize_provider_error_detail
except Exception:  # pragma: no cover - runtime_log always ships alongside this
    def _log_event(*_args: Any, **_kwargs: Any) -> None:  # type: ignore
        return None
    def _sanitize_provider_error_detail(value: Any, limit: int = 240) -> str:  # type: ignore
        text = str(value or "").replace("\n", " ").replace("\r", " ")
        return text if len(text) <= limit else text[: limit - 3] + "..."

# Exact, ordered public contract — other modules code against this list.
CHECK_NAMES = [
    "auth",
    "mail_read",
    "mail_folder_scoped",
    "mail_tags_list",
    "calendar_read",
    "files_read",
    "mail_draft",
    "mail_tag_write",
    "files_write",
    "mail_send",
    "calendar_write",
]

# The SAFE_WRITE auto-approve env var (see workspace_guardrails._is_auto_approved).
# Setting it to "1" lets the @guarded safe-write methods proceed non-interactively;
# the destructive gate (CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE) is deliberately untouched.
AUTO_APPROVE_ENV = "CHIEF_OF_STAFF_AUTO_APPROVE"

# The persistent verification tag/category (Gmail label ≈ Outlook category).
VERIFY_TAG = "CoS-Verify"

# Write checks that count toward write_ready (mail_send / calendar_write are
# always not_tested and never contribute a failure).
_TESTED_WRITE_CHECKS = ("mail_draft", "mail_tag_write", "files_write")

# Human-report section layout.
_SECTIONS: list[tuple[str, list[str]]] = [
    ("Authentication", ["auth"]),
    ("Mail", ["mail_read", "mail_folder_scoped", "mail_tags_list"]),
    ("Calendar", ["calendar_read"]),
    ("OneDrive/Files", ["files_read"]),
    ("Writes", ["mail_draft", "mail_tag_write", "files_write", "mail_send", "calendar_write"]),
]

# Checks that are NOT required for read_ready (surfaced visibly in the report).
_OPTIONAL_CHECKS = frozenset({"mail_tags_list"})


# ── small helpers ──────────────────────────────────────────────────────────

def _mk(status: str, detail: str) -> dict[str, str]:
    return {"status": status, "detail": detail}


def _short(value: Any, limit: int = 32) -> str:
    s = str(value)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _supported(client: Any, action: str) -> bool:
    try:
        return bool(client.supports(action))
    except Exception:
        return False


def _result_dict(res: Any) -> dict[str, Any]:
    """Coerce a write result (ActionResult, its .to_dict(), or a bare value)
    into an ActionResult-shaped dict with at least success/data/error keys."""
    if hasattr(res, "to_dict") and callable(getattr(res, "to_dict")):
        try:
            res = res.to_dict()
        except Exception:
            pass
    if isinstance(res, Mapping):
        d = dict(res)
        d.setdefault("success", False)
        d.setdefault("data", {})
        d.setdefault("error", None)
        return d
    return {"success": bool(res), "data": {}, "error": None}


def _already_exists(msg: Any) -> bool:
    if not msg:
        return False
    m = str(msg).lower()
    return "already exist" in m or "already-exist" in m or "duplicate" in m or "exists" in m


def _self_address(config: Any) -> str:
    """Resolve an operator/self address to draft verification mail to."""
    if isinstance(config, Mapping):
        m365 = config.get("m365") or {}
        if isinstance(m365, Mapping) and m365.get("user_principal"):
            return str(m365["user_principal"])
        google = config.get("google") or {}
        if isinstance(google, Mapping) and google.get("delegate_email"):
            return str(google["delegate_email"])
        user = config.get("user") or {}
        if isinstance(user, Mapping) and user.get("email"):
            return str(user["email"])
        esign = config.get("esign") or {}
        if isinstance(esign, Mapping) and esign.get("provider_email"):
            return str(esign["provider_email"])
        if isinstance(esign, Mapping) and esign.get("admin_email"):
            return str(esign["admin_email"])
    return "operator@example.com"


def _marker() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ── read checks ────────────────────────────────────────────────────────────

def _auth_check(client: Any) -> dict[str, str]:
    try:
        ok = client.health_check()
    except Exception as exc:  # noqa: BLE001
        return _mk("fail", str(exc))
    return _mk("pass", "authenticated") if ok else _mk("fail", "health_check() returned False")


def _read_check(fn: Callable[[], Any]) -> dict[str, str]:
    """Run a provider read, capturing warnings. A warning (the provider's
    warn+return[] failure signal) => fail; otherwise pass even if empty."""
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = fn()
        if caught:
            return _mk("fail", "; ".join(str(w.message) for w in caught))
        try:
            n = len(result)
        except TypeError:
            n = 0
        return _mk("pass", f"{n} result(s)")
    except Exception as exc:  # noqa: BLE001
        return _mk("fail", str(exc))


# ── write checks ───────────────────────────────────────────────────────────

_CLEANUP_FAIL_PREFIX = (
    "Write succeeded, but verification artefact cleanup failed. Manual removal required."
)


def _check_mail_draft(
    client: Any, config: Any
) -> tuple[dict[str, str], str | None, str | None]:
    if not _supported(client, "mail.draft"):
        return _mk("not_tested", "provider does not support mail.draft"), None, None
    # Cleanup-capability precondition: never create a draft we cannot trash.
    if not _supported(client, "mail.trash"):
        return (
            _mk(
                "not_tested",
                "cleanup capability mail.trash unsupported — skipped to avoid leaving artefacts",
            ),
            None,
            None,
        )
    subject = f"[CoS verify] {_marker()}"
    try:
        addr = _self_address(config)
        res = client.mail_create_draft(
            to=addr,
            subject=subject,
            body="Chief-of-Staff workspace verification draft. Safe to delete.",
        )
    except NotImplementedError as exc:
        return _mk("not_tested", str(exc)), None, None
    except Exception as exc:  # noqa: BLE001
        return _mk("fail", str(exc)), None, None
    d = _result_dict(res)
    draft_id = (d.get("data") or {}).get("id")
    if d.get("success") and draft_id:
        return _mk("pass", f"draft created (id={_short(draft_id)})"), draft_id, subject
    return _mk("fail", d.get("error") or "draft creation returned no id in data"), None, None


def _trash_verify_draft(
    client: Any, draft_id: str, subject: str | None
) -> dict[str, str] | None:
    """Trash a verification draft. Return a fail check dict on cleanup failure."""
    marker = f" Draft subject: {subject}." if subject else ""
    try:
        trashed = _result_dict(client.mail_trash(draft_id))
        if not trashed.get("success"):
            return _mk(
                "fail",
                f"{_CLEANUP_FAIL_PREFIX}{marker} (cleanup error: {trashed.get('error')})",
            )
    except Exception as exc:  # noqa: BLE001
        return _mk("fail", f"{_CLEANUP_FAIL_PREFIX}{marker} (cleanup error: {exc})")
    return None


def _check_mail_tag_write(
    client: Any, draft_check: Mapping[str, str], draft_id: str | None, subject: str | None
) -> dict[str, str]:
    if not (_supported(client, "mail.create_tag") and _supported(client, "mail.tag")):
        return _mk("not_tested", "provider does not support mail tag write")
    # Inherit the mail_draft precondition: no draft was created (unsupported /
    # cleanup capability missing) -> nothing to tag, so this is not_tested too.
    if draft_check.get("status") == "not_tested":
        return _mk(
            "not_tested",
            f"mail_draft not tested ({draft_check.get('detail', 'no draft')}) — nothing to tag",
        )
    try:
        tag_id = VERIFY_TAG
        # Create the category/label; an already-exists error counts as pass (reuse).
        try:
            cr = _result_dict(client.mail_create_tag(VERIFY_TAG))
            if cr.get("success"):
                tag_id = (cr.get("data") or {}).get("id") or VERIFY_TAG
            elif _already_exists(cr.get("error")):
                tag_id = VERIFY_TAG
            else:
                return _mk("fail", cr.get("error") or "mail_create_tag failed")
        except Exception as exc:  # noqa: BLE001
            if not _already_exists(exc):
                return _mk("fail", str(exc))

        if not draft_id:
            return _mk("fail", "no verification draft available to tag")

        applied = _result_dict(client.mail_tag(draft_id, tag_id))
        if applied.get("success"):
            check = _mk("pass", f"tag '{VERIFY_TAG}' created/reused and applied")
        else:
            check = _mk("fail", applied.get("error") or "mail_tag failed")

        # Clean up the draft regardless of tag-apply outcome. A cleanup failure is
        # a FAILURE: the artefact still exists and needs manual removal.
        cleanup_fail = _trash_verify_draft(client, draft_id, subject)
        if cleanup_fail is not None:
            return cleanup_fail
        return check
    except Exception as exc:  # noqa: BLE001
        return _mk("fail", str(exc))


def _check_files_write(client: Any) -> dict[str, str]:
    if not _supported(client, "files.upload"):
        return _mk("not_tested", "provider does not support files.upload")
    # Cleanup-capability precondition: never upload a file we cannot trash.
    if not _supported(client, "files.trash"):
        return _mk(
            "not_tested",
            "cleanup capability files.trash unsupported — skipped to avoid leaving artefacts",
        )
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="cos-verify-", suffix=".txt")
        with os.fdopen(fd, "w") as fh:
            fh.write("Chief-of-Staff workspace verification. Safe to delete.\n")
        filename = os.path.basename(tmp_path)
        try:
            res = client.files_upload(tmp_path)
        except NotImplementedError as exc:
            return _mk("not_tested", str(exc))
        d = _result_dict(res)
        file_id = (d.get("data") or {}).get("id")
        if not (d.get("success") and file_id):
            return _mk("fail", d.get("error") or "upload returned no id in data")
        # Clean up the upload. A cleanup failure is a FAILURE.
        try:
            trashed = _result_dict(client.files_trash(file_id))
            if not trashed.get("success"):
                return _mk(
                    "fail",
                    f"{_CLEANUP_FAIL_PREFIX} Uploaded file: {filename}. "
                    f"(cleanup error: {trashed.get('error')})",
                )
        except Exception as exc:  # noqa: BLE001
            return _mk(
                "fail",
                f"{_CLEANUP_FAIL_PREFIX} Uploaded file: {filename}. (cleanup error: {exc})",
            )
        return _mk("pass", f"uploaded (id={_short(file_id)}); trashed")
    except Exception as exc:  # noqa: BLE001
        return _mk("fail", str(exc))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _emit_check_failures(provider: Any, checks: Mapping[str, dict[str, str]]) -> None:
    """For every check that FAILED, emit a structured ``verify_check_failed``
    error event so the failure lands in the run's events.jsonl (driving the
    observed error counters and the log_analyser matchers).

    ``runtime_log.log_event`` redacts every value and silently no-ops when no
    run is active, so this is safe for standalone/console use. Provider failure
    details are classified/truncated before logging so raw auth blobs are not
    written to support bundles.
    """
    for name, check in checks.items():
        if not isinstance(check, Mapping):
            continue
        if str(check.get("status", "")).lower() != "fail":
            continue
        _log_event(
            "verify_check_failed",
            level="error",
            component="workspace_verify",
            check=str(name),
            provider=str(provider),
            message=_sanitize_provider_error_detail(check.get("detail", "") or ""),
        )


def _check_calendar_write(client: Any) -> dict[str, str]:
    """Opt-in: create → update → delete a marked verify event.

    Requires ``calendar.create``, ``calendar.update``, and a ``calendar_delete``
    method. Delete is destructive; caller must allow
    ``CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE`` for the duration (set by
    :func:`_run_calendar_write_check`).
    """
    if not (_supported(client, "calendar.create") and _supported(client, "calendar.update")):
        return _mk("not_tested", "provider does not support calendar create/update")
    if not hasattr(client, "calendar_delete"):
        return _mk(
            "not_tested",
            "provider has no calendar_delete cleanup method — skipped to avoid leaving events",
        )
    day = (datetime.date.today() + datetime.timedelta(days=14)).isoformat()
    title = f"[CoS verify] {_marker()}"
    event_id: str | None = None
    try:
        created = _result_dict(
            client.calendar_create(
                title, day, day,
                description="Chief-of-Staff workspace verification event. Safe to delete.",
            )
        )
        event_id = (created.get("data") or {}).get("id")
        if not (created.get("success") and event_id):
            return _mk("fail", created.get("error") or "calendar create returned no id")

        updated = _result_dict(
            client.calendar_update(event_id, title=f"{title} (updated)")
        )
        if not updated.get("success"):
            update_err = updated.get("error") or "calendar update failed"
            deleted = _result_dict(client.calendar_delete(event_id))
            if not deleted.get("success"):
                return _mk(
                    "fail",
                    f"{update_err}; {_CLEANUP_FAIL_PREFIX} Event: {title}. "
                    f"(cleanup error: {deleted.get('error')})",
                )
            return _mk("fail", update_err)

        deleted = _result_dict(client.calendar_delete(event_id))
        if not deleted.get("success"):
            return _mk(
                "fail",
                f"{_CLEANUP_FAIL_PREFIX} Event: {title}. "
                f"(cleanup error: {deleted.get('error')})",
            )
        return _mk("pass", f"created/updated/deleted (id={_short(event_id)})")
    except NotImplementedError as exc:
        return _mk("not_tested", str(exc))
    except Exception as exc:  # noqa: BLE001
        if event_id:
            try:
                client.calendar_delete(event_id)
            except Exception:  # noqa: BLE001
                pass
        return _mk("fail", str(exc))


def _run_write_checks(client: Any, config: Any, checks: dict[str, dict[str, str]]) -> None:
    """Run the opt-in write smoke checks with the auto-approve env var set for
    the duration (restored in finally). ALLOW_DESTRUCTIVE is never touched here
    (calendar delete uses a separate opt-in path)."""
    saved = os.environ.get(AUTO_APPROVE_ENV)
    os.environ[AUTO_APPROVE_ENV] = "1"
    try:
        draft_check, draft_id, subject = _check_mail_draft(client, config)
        checks["mail_draft"] = draft_check
        tag_check = _check_mail_tag_write(client, draft_check, draft_id, subject)
        checks["mail_tag_write"] = tag_check
        # When tags are unsupported the tag check does not trash the draft —
        # clean it up here so Composio MS (and similar) leave no artefacts.
        if draft_id and tag_check.get("status") == "not_tested" and _supported(client, "mail.trash"):
            cleanup_fail = _trash_verify_draft(client, draft_id, subject)
            if cleanup_fail is not None:
                checks["mail_draft"] = cleanup_fail
        checks["files_write"] = _check_files_write(client)
    finally:
        if saved is None:
            os.environ.pop(AUTO_APPROVE_ENV, None)
        else:
            os.environ[AUTO_APPROVE_ENV] = saved


def _run_calendar_write_check(client: Any, checks: dict[str, dict[str, str]]) -> None:
    """Opt-in calendar create/update/delete probe.

    Temporarily enables AUTO_APPROVE and ALLOW_DESTRUCTIVE so the delete cleanup
    of the artefact *we just created* can proceed non-interactively. Both env
    vars are restored in ``finally``.
    """
    destructive_env = "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"
    saved_auto = os.environ.get(AUTO_APPROVE_ENV)
    saved_destr = os.environ.get(destructive_env)
    os.environ[AUTO_APPROVE_ENV] = "1"
    os.environ[destructive_env] = "1"
    try:
        checks["calendar_write"] = _check_calendar_write(client)
    finally:
        if saved_auto is None:
            os.environ.pop(AUTO_APPROVE_ENV, None)
        else:
            os.environ[AUTO_APPROVE_ENV] = saved_auto
        if saved_destr is None:
            os.environ.pop(destructive_env, None)
        else:
            os.environ[destructive_env] = saved_destr


# ── public API ─────────────────────────────────────────────────────────────

def run_verification(
    config: Any,
    include_writes: bool = False,
    include_calendar_writes: bool = False,
) -> dict[str, Any]:
    """Run per-capability verification of the configured workspace provider.

    See the module docstring for the full report schema and check semantics.
    """
    client = get_workspace_client(config)
    checks: dict[str, dict[str, str]] = {
        name: _mk("not_tested", "") for name in CHECK_NAMES
    }

    # Reads (neutral methods only).
    checks["auth"] = _auth_check(client)
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    checks["mail_read"] = _read_check(lambda: client.mail_search("is:unread", max_results=1))
    checks["mail_folder_scoped"] = _read_check(lambda: client.mail_search("in:inbox", max_results=1))
    checks["mail_tags_list"] = _read_check(lambda: client.mail_list_tags())
    # mail_tags_list is OPTIONAL for read_ready: on failure the product still
    # runs, but email-organisation features are degraded — say so in the detail.
    if checks["mail_tags_list"]["status"] == "fail":
        detail = checks["mail_tags_list"]["detail"]
        checks["mail_tags_list"]["detail"] = (
            f"{detail}; email organisation features will be degraded"
            if detail
            else "email organisation features will be degraded"
        )
    checks["calendar_read"] = _read_check(
        lambda: client.calendar_list(today.isoformat(), tomorrow.isoformat())
    )
    checks["files_read"] = _read_check(lambda: client.files_search("a", max_results=1))

    # Writes.
    if include_writes:
        _run_write_checks(client, config, checks)
    # mail_send is ALWAYS not_tested — never auto-send.
    checks["mail_send"] = _mk("not_tested", "verification never sends mail")
    if include_calendar_writes:
        _run_calendar_write_check(client, checks)
    else:
        checks["calendar_write"] = _mk(
            "not_tested",
            "verification never creates calendar events "
            "(pass include_calendar_writes / --verify-calendar-writes to opt in)",
        )

    # Emit a structured error event for every failed check (observability): these
    # become error events in the active run's events.jsonl and feed the analyser.
    _emit_check_failures(client.provider_name, checks)

    # read_ready gates the reads the product actually depends on: auth, mail_read,
    # folder-scoped mail search, calendar_read and files_read. mail_tags_list is
    # OPTIONAL and deliberately excluded.
    read_ready = all(
        checks[name]["status"] == "pass"
        for name in ("auth", "mail_read", "mail_folder_scoped", "calendar_read", "files_read")
    )
    tested_writes = list(_TESTED_WRITE_CHECKS)
    if include_calendar_writes:
        tested_writes.append("calendar_write")
    writes_requested = include_writes or include_calendar_writes
    if not writes_requested:
        write_ready = "partial"
    elif any(checks[name]["status"] == "fail" for name in tested_writes):
        write_ready = "no"
    elif any(checks[name]["status"] == "pass" for name in tested_writes):
        write_ready = "yes"
    else:
        write_ready = "partial"

    return {
        "provider": client.provider_name,
        "checks": checks,
        "read_ready": read_ready,
        "write_ready": write_ready,
    }


# ── reporting ──────────────────────────────────────────────────────────────

def _symbol(status: str) -> str:
    return {"pass": "✓", "fail": "✗", "not_tested": "—"}.get(status, "?")


def _format_human(report: Mapping[str, Any]) -> str:
    checks = report.get("checks", {})
    lines: list[str] = []
    lines.append(f"Workspace verification — provider: {report.get('provider', 'unknown')}")
    lines.append(f"Read ready:  {'yes' if report.get('read_ready') else 'no'}")
    lines.append(f"Write ready: {report.get('write_ready', 'partial')}")
    for title, names in _SECTIONS:
        lines.append("")
        lines.append(title)
        for name in names:
            check = checks.get(name, {"status": "not_tested", "detail": ""})
            detail = check.get("detail", "")
            suffix = f" — {detail}" if detail else ""
            label = f"{name} (optional)" if name in _OPTIONAL_CHECKS else name
            lines.append(f"  {_symbol(check.get('status', 'not_tested'))} {label}{suffix}")
    return "\n".join(lines)


def _format_markdown(report: Mapping[str, Any]) -> str:
    checks = report.get("checks", {})
    lines: list[str] = []
    lines.append(f"# Workspace verification — provider: {report.get('provider', 'unknown')}")
    lines.append("")
    lines.append(f"- **Read ready:** {'yes' if report.get('read_ready') else 'no'}")
    lines.append(f"- **Write ready:** {report.get('write_ready', 'partial')}")
    lines.append("")
    lines.append("| Check | Status | Detail |")
    lines.append("| --- | --- | --- |")
    for name in CHECK_NAMES:
        check = checks.get(name, {"status": "not_tested", "detail": ""})
        detail = str(check.get("detail", "")).replace("|", "\\|")
        lines.append(f"| {name} | {check.get('status', 'not_tested')} | {detail} |")
    return "\n".join(lines)


def format_report(report: Mapping[str, Any], fmt: str = "human") -> str:
    """Render a verification report. fmt: 'human' | 'json' | 'markdown'."""
    if fmt == "json":
        return _json.dumps(report, indent=2)
    if fmt == "markdown":
        return _format_markdown(report)
    return _format_human(report)


if __name__ == "__main__":  # pragma: no cover - convenience entrypoint
    import sys

    cfg: dict[str, Any] = {}
    if len(sys.argv) > 1:
        try:
            import yaml
            with open(sys.argv[1]) as f:
                cfg = yaml.safe_load(f) or {}
        except Exception as exc:  # noqa: BLE001
            print(f"Error loading config: {exc}", file=sys.stderr)
            sys.exit(2)
    rep = run_verification(cfg, include_writes="--writes" in sys.argv)
    print(format_report(rep, "human"))
    sys.exit(0 if rep["read_ready"] else 1)
