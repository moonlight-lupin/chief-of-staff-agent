"""Hermes-native surface: read-only tools for the agent, a slash command for you.

The plugin's invariant (Observe → Understand → Suggest → Approve → Execute →
Audit) decides the split:

* **Tools** (the model can call them) only observe: capabilities, the daily
  briefing, the review queue, a preview, the pipeline and spend status. No
  tool can approve, dismiss, execute or claim anything.
* **The slash command** (only a person can type it) is where decisions are
  made: ``/<assistant name>`` (e.g. ``/ada``) and the stable alias ``/cos``.
  ``approve`` and ``dismiss`` run only from the owner's own surfaces (CLI,
  TUI, Desktop, local) or for chat users listed in
  ``approvals.chat_approvers`` (``{"telegram:<user id>": "<name>"}``).
  Everything else is refused: Hermes passes transport identity, not
  authorization, so a shared chat must never be able to approve.

Both run the plugin's existing scripts as child processes, which stay the
single source of truth. Secrets are read through Hermes (``plugin_secrets``)
and passed to the child. Handlers never raise: tools return JSON, the slash
command returns text.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import plugin_secrets

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
TOOLSET = "chief-of-staff"
DEFAULT_TIMEOUT = 180
OWNER_SURFACES = frozenset({"cli", "tui", "desktop", "local"})
_ACTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}")
_MAX_TEXT = 12_000          # cap for plain-text output only; JSON is never cut


# ------------------------------------------------------------ running scripts


def run_script(script: str, args: list[str], timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Run ``shared/scripts/<script>`` with the plugin's secrets in its env."""
    env = dict(os.environ)
    for name in plugin_secrets.SECRETS:
        value = plugin_secrets.get(name)
        if value:
            env[name] = value
    proc = subprocess.run([sys.executable, str(SCRIPTS / script), *args], capture_output=True, text=True,
                          timeout=timeout, env=env, cwd=str(PLUGIN_ROOT), check=False)
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
            "stdout": proc.stdout or "", "stderr": _redact((proc.stderr or "")[-2000:])}


def _redact(text: str) -> str:
    """Error text goes to the model: never with a secret in it."""
    try:
        from runtime_log import redact

        return str(redact(text))
    except Exception:
        return text


def _cap(text: str) -> str:
    if len(text) <= _MAX_TEXT:
        return text
    return text[:_MAX_TEXT] + f"\n... [truncated {len(text) - _MAX_TEXT} characters]"


def _valid_id(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if _ACTION_ID.fullmatch(text) else None


# ---------------------------------------------------------------------- tools


@dataclass(frozen=True)
class ToolSpec:
    name: str
    schema: dict[str, Any]
    build: Callable[[Mapping[str, Any]], tuple[str, list[str]]]


def _schema(name: str, description: str, properties: dict | None = None, required: list | None = None) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": required or []}}


def _preview_argv(args: Mapping[str, Any]) -> tuple[str, list[str]]:
    action_id = _valid_id(args.get("action_id"))
    if action_id is None:
        raise ValueError("action_id is required: an id from cos_review_queue (letters, digits, - _ . :)")
    return "review_queue.py", ["preview", "--action-id", action_id]


def _queue_argv(args: Mapping[str, Any]) -> tuple[str, list[str]]:
    argv = ["list"]
    state = str(args.get("state") or "").strip()
    if state:
        if not re.fullmatch(r"[a-z_]{1,20}", state):
            raise ValueError("state must be a queue state such as requested, approved or suggested")
        argv += ["--state", state]
    return "review_queue.py", argv


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("cos_capabilities", _schema(
        "cos_capabilities",
        "Chief of Staff operating envelope: the active workspace provider, which actions it supports or "
        "refuses and why, where state lives and whether it survives. Call this before planning any "
        "Chief-of-Staff work. Read-only."),
        lambda a: ("chief_of_staff.py", ["capabilities"])),
    ToolSpec("cos_daily", _schema(
        "cos_daily",
        "Chief of Staff daily briefing: urgent items, calendar, deadlines, pipeline, finance, to-dos and "
        "pending approvals, with recommended next steps. Read-only: it reports and recommends, never acts."),
        lambda a: ("chief_of_staff.py", ["daily", "--json"])),
    ToolSpec("cos_review_queue", _schema(
        "cos_review_queue",
        "List actions waiting in the Chief of Staff review queue (proposed emails, filings, deal changes, "
        "spend budgets...). Read-only. Approving is the user's decision: tell them to run the slash "
        "command; never approve yourself.",
        {"state": {"type": "string", "description": "Optional filter: requested, approved, suggested, dismissed"}}),
        _queue_argv),
    ToolSpec("cos_preview", _schema(
        "cos_preview",
        "Explain one review-queue action before the user decides: what it will do, to whom, the risk, and "
        "how it is reversed. Read-only.",
        {"action_id": {"type": "string", "description": "Action id from cos_review_queue"}}, ["action_id"]),
        _preview_argv),
    ToolSpec("cos_pipeline", _schema(
        "cos_pipeline",
        "Chief of Staff CRM pipeline summary: deals by stage, values, stale opportunities. Read-only."),
        lambda a: ("chief_of_staff.py", ["pipeline", "--json"])),
    ToolSpec("cos_spend_status", _schema(
        "cos_spend_status",
        "Approved spend budgets for paid data calls (e.g. Monid): month-to-date spend against caps and "
        "what each budget has left. Read-only; check before proposing paid work."),
        lambda a: ("spend.py", ["status"])),
)
TOOL_NAMES = tuple(t.name for t in TOOLS)


def tool_handler(name: str) -> Callable[..., str]:
    spec = next(t for t in TOOLS if t.name == name)

    def handler(args: dict | None = None, **_kwargs: Any) -> str:
        try:
            script, argv = spec.build(args or {})
            run = run_script(script, argv)
            try:
                result: Any = json.loads(run["stdout"])
            except ValueError:
                result = _cap(run["stdout"])
            out = {"ok": run["ok"], "result": result}
            if not run["ok"]:
                out["error"] = run["stderr"].strip() or f"exit code {run['exit_code']}"
            return json.dumps(out, default=str)
        except Exception as exc:  # handlers never raise
            return json.dumps({"ok": False, "error": str(exc)})

    handler.__name__ = f"{name}_handler"
    return handler


# -------------------------------------------------------------- slash command


def _load_config() -> Mapping[str, Any]:
    try:
        from config_loader import load_config

        return load_config(None) or {}
    except Exception:
        return {}


def command_names(config: Mapping[str, Any] | None) -> list[str]:
    """``[<assistant name>, "cos"]``, or ``["cos"]`` for a default or unusable name."""
    from config_loader import is_default_assistant_name, validate_assistant_name

    raw = str(((config or {}).get("assistant") or {}).get("name") or "")
    names: list[str] = []
    if not is_default_assistant_name(raw):
        try:
            name = validate_assistant_name(raw)
        except ValueError:
            return ["cos"]          # doctor warns; /cos keeps working
        slug = re.sub(r"[ _-]+", "_", name.lower())
        if slug != "cos":
            names.append(slug)
    return names + ["cos"]


def _usage(command: str) -> str:
    c = f"/{command}"
    return (f"{c}                       today's briefing\n"
            f"{c} review                what is waiting for your decision\n"
            f"{c} preview <id>          what an action will do, to whom, and how it is reversed\n"
            f"{c} approve <id> [reason] approve it (your own sessions or listed chat approvers)\n"
            f"{c} dismiss <id> <reason> dismiss it\n"
            f"{c} help                  this list")


def _owner_name(config: Mapping[str, Any]) -> str:
    return str(((config.get("user") or {}).get("name")) or "").strip()


def _approver(platform: str, source: Any, config: Mapping[str, Any]) -> tuple[str | None, str]:
    """(approver name, refusal). Owner surfaces, or a listed chat user; else refused."""
    platform = str(platform or "").strip().lower()
    if platform in OWNER_SURFACES:
        name = _owner_name(config)
        return (name or f"operator ({platform})"), ""
    if not platform or source is None:
        return None, "not allowed: approvals need your own CLI/Desktop session or a listed chat approver"
    user_id = str(getattr(source, "user_id", "") or "").strip()
    approvers = ((config.get("approvals") or {}).get("chat_approvers")) or {}
    name = approvers.get(f"{platform}:{user_id}") if user_id and isinstance(approvers, Mapping) else None
    if not name:
        return None, (f"not allowed: {platform} user {user_id or '(unknown)'} is not in "
                      f"approvals.chat_approvers")
    return str(name), ""


def _text(run: Mapping[str, Any]) -> str:
    body = _cap((run.get("stdout") or "").strip())
    if run.get("ok"):
        return body or "Done."
    return (body + "\n" if body else "") + (run.get("stderr") or "").strip() or f"Failed (exit {run.get('exit_code')})."


# ------------------------------------------------------------ signed decisions
#
# The gateway's signing key exists only in this process's memory, created when
# Hermes loads the plugin (register). Approve/dismiss sign here, in-process;
# nothing is handed to a child process, and a copy of this module imported by
# any other process has no key and cannot decide anything.

_SIGNER: Any = None


def activate_gateway_signer(*configs: Any) -> None:
    global _SIGNER
    import approval_signing

    if _SIGNER is None:
        _SIGNER = approval_signing.GatewaySigner.generate()
    for config in configs:
        if config:
            _SIGNER.enrol(config)


def deactivate_gateway_signer() -> None:
    global _SIGNER
    _SIGNER = None


def _registry_configs() -> list[Any]:
    """Every company and group config this gateway serves (or the single config)."""
    try:
        from company_registry import load_registry
        from config_loader import load_config

        registry = load_registry()
    except Exception:
        registry = None
    if not registry:
        config = _load_config()
        return [config] if config else []
    out = []
    for scope in [*registry.companies.values(), *registry.groups.values()]:
        path = getattr(scope, "config_path", None)
        if path:
            try:
                config = load_config(path, quiet=True)
            except Exception:
                config = None
            if config:
                out.append(config)
    return out


def _audit_refusal(config: Any, decision: str, action_id: str, platform: str, source: Any, detail: str) -> None:
    try:
        from workspace_audit import audit_workspace_action

        audit_workspace_action(config, "hermes", f"cos.{decision}", "slash", target=action_id, status="blocked",
                               extra={"reason": "slash_approval_refused", "detail": detail,
                                      "platform": platform or "(unknown)",
                                      "user_id": str(getattr(source, "user_id", "") or "")})
    except Exception:
        pass


def _decide(config: Any, decision: str, action_id: str, approver: str, reason: str, command: str) -> str:
    from state_db import approve_pending_action, dismiss_pending_action, get_pending_action

    if _SIGNER is None:
        return (f"Not {decision}d: approvals are made from your Hermes session with /{command} {decision}. "
                f"This process has no gateway signer.")
    action = get_pending_action(config, action_id)
    if not action:
        return f"No action {action_id} in the review queue."
    _SIGNER.enrol(config)
    signature = _SIGNER.sign_decision(action, decision, approver)
    if decision == "approve":
        done = approve_pending_action(config, action_id, approver=approver, reason=reason, signature=signature)
    else:
        done = dismiss_pending_action(config, action_id, reason=reason, approver=approver, signature=signature)
    if done:
        verb = "Approved" if decision == "approve" else "Dismissed"
        return f"{verb} {action_id}: {action.get('summary') or action.get('type')}"
    current = get_pending_action(config, action_id) or {}
    return (f"Not {decision}d: {action_id} is {current.get('state', 'unknown')} "
            f"(only requested actions can be {decision}d; an expired one needs a new proposal).")


def cos_command(raw_args: str = "", *, platform: str = "", source: Any = None, session_id: str = "",
                command: str = "cos", **_kwargs: Any) -> str:
    try:
        try:
            words = shlex.split(raw_args or "")
        except ValueError:
            return _usage(command)
        sub = words[0].lower() if words else "briefing"
        rest = words[1:]
        if sub in {"briefing", "daily", "today"}:
            return _text(run_script("chief_of_staff.py", ["daily", "--summary"]))
        if sub == "review":
            return _text(run_script("review_queue.py", ["summary"]))
        if sub in {"preview", "approve", "dismiss"}:
            action_id = _valid_id(rest[0]) if rest else None
            if action_id is None:
                return f"Give an action id (from /{command} review).\n\n" + _usage(command)
            if sub == "preview":
                return _text(run_script("review_queue.py", ["preview", "--action-id", action_id]))
            reason = " ".join(rest[1:]).strip()
            if sub == "dismiss" and not reason:
                return f"Say why: /{command} dismiss {action_id} <reason>"
            config = _load_config()
            approver, refusal = _approver(platform, source, config)
            if approver is None:
                _audit_refusal(config, sub, action_id, platform, source, refusal)
                return refusal
            if sub == "approve":
                reason = reason or f"Approved via /{command} on {platform}"
            return _decide(config, sub, action_id, approver, reason, command)
        return _usage(command)
    except Exception as exc:  # handlers never raise
        return f"/{command} failed: {exc}"


def make_command_handler(command: str) -> Callable[..., str]:
    def handler(raw_args: str = "", *, session_id: str = "", platform: str = "", source: Any = None) -> str:
        if command != "cos":
            # Hermes cannot un-register a command, so a renamed assistant's old
            # command stays wired until restart. It must do nothing but redirect.
            current = command_names(_load_config())
            if command not in current:
                return (f"/{command} is retired: the assistant is now /{current[0]}. "
                        f"/cos always works. Restart Hermes (or reload the plugin) to register "
                        f"/{current[0]}.")
        return cos_command(raw_args, platform=platform, source=source, session_id=session_id, command=command)

    handler.__name__ = f"{command}_command"
    return handler


# ---------------------------------------------------------------- registration


def register(ctx: Any) -> None:
    """Register tools and slash commands where this Hermes supports them."""
    register_tool = getattr(ctx, "register_tool", None)
    if callable(register_tool):
        for spec in TOOLS:
            register_tool(name=spec.name, toolset=TOOLSET, schema=spec.schema, handler=tool_handler(spec.name))
    register_command = getattr(ctx, "register_command", None)
    if callable(register_command):
        names = command_names(_load_config())
        for name in names:
            register_command(name, make_command_handler(name),
                             description=f"/{name}: Chief of Staff briefing, review queue and approvals",
                             args_hint="[review|preview|approve|dismiss|help] [id] [reason]")
        # This process is now the only one that can approve: enrol every store
        # it serves for signed approvals (approval_signing).
        try:
            activate_gateway_signer(*_registry_configs())
        except Exception as exc:  # never break plugin loading; doctor reports enrolment
            print(f"[CoS] approval signing not enabled: {exc}", file=sys.stderr)
