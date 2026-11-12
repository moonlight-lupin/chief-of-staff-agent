#!/usr/bin/env python3
"""The flagship demo: the whole Chief-of-Staff loop on sample data, in seven steps.

    1. Morning briefing        read-only, from the bundled sample data
    2. Proposal                the agent queues a follow-up email (nothing sent)
    3. Preview                 what it will do, to whom, and how it is reversed
    4. Approve                 a *signed* approval, exactly as /cos approve does
    5. Execute (simulated)     the real claim → check → grant → record path, and
                               the real @guarded provider wrapper (which consumes
                               the single-use grant), around a stand-in mailbox:
                               no mail leaves the machine
    6. Audit                   the action's timeline and the audit log
    7. Next briefing           the queue afterwards

Everything happens in a throwaway directory, which is deleted at the end, so
every run starts fresh. ``--keep DIR`` keeps the state for inspection (DIR gets a
marker file), and ``--reset DIR`` deletes it, refusing a directory without the
marker. The user's real Hermes home, workspace and credentials are never touched.

What the demo does NOT show: in Hermes the signing key lives only in the gateway
process, and ``/cos approve`` also checks who typed it and where. Here the demo
plays the gateway's part itself, in this process, against its own throwaway
store. See "Limits we state plainly" in docs/SECURITY_MODEL.md.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

PLUGIN_ROOT = _SCRIPT_DIR.parents[1]
MARKER = ".cos-demo"
MARKER_TEXT = "Created by chief_of_staff.py demo --flow. Safe to delete.\n"
# Everything the demo writes at the top of its directory. --keep re-runs and
# --reset only ever delete a directory holding nothing else.
DEMO_ENTRIES = frozenset({
    MARKER, ".hermes-demo", ".audit", ".runs", ".knowledge", ".integrations", ".last_briefing",
    "company.yaml", "pipeline.yaml", "invoices.yaml", "expenses.yaml", "todos.yaml", "wiki",
    # state.db imports the sample YAML and leaves the originals beside it.
    "pipeline.yaml.migrated", "invoices.yaml.migrated", "expenses.yaml.migrated", "todos.yaml.migrated",
    "state.db", "state.db-wal", "state.db-shm", "state.db-journal",
})
APPROVER = "demo-user"
_ISOLATED_ENV = ("CHIEF_OF_STAFF_HERMES_HOME", "CHIEF_OF_STAFF_AUTO_APPROVE",
                 "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", "CHIEF_OF_STAFF_CONFIG")


def _demo_mailbox_class():
    from workspace_guardrails import guarded

    class DemoMailbox:
        """A stand-in workspace client: the real @guarded wrapper, no network."""

        provider_name = "demo"

        def __init__(self, config: Any) -> None:
            self.config = config
            self.sent: list[dict[str, Any]] = []

        def supports(self, action: str) -> bool:
            return action in {"gmail.send", "mail.send"}

        def acting_identity(self, family: str) -> str | None:
            return None

        @guarded("gmail.send", target_arg="to", audit_provider="demo", audit_tool="demo-mailbox")
        def mail_send(self, to: str, subject: str, body: str, cc: str | None = None) -> dict[str, Any]:
            self.sent.append({"to": to, "subject": subject})
            return {"message_id": f"demo-{len(self.sent):04d}", "simulated": True}

    return DemoMailbox


@contextmanager
def _isolated_env(hermes_home: Path) -> Iterator[None]:
    """Point Hermes home at the demo directory and clear break-glass, then restore."""
    saved = {k: os.environ.get(k) for k in _ISOLATED_ENV}
    try:
        for k in _ISOLATED_ENV:
            os.environ.pop(k, None)
        os.environ["CHIEF_OF_STAFF_HERMES_HOME"] = str(hermes_home)
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _prepare(root: Path) -> Path:
    import yaml
    from chief_of_staff import _DEMO_CONFIG

    examples = PLUGIN_ROOT / "examples"
    cfg = dict(_DEMO_CONFIG)
    cfg["paths"] = {"project_root": str(root), "wiki_path": str(root / "wiki"),
                    "templates": str(PLUGIN_ROOT / "shared" / "templates")}
    for name in ("pipeline.yaml", "invoices.yaml", "expenses.yaml", "todos.yaml"):
        if (examples / name).exists():
            shutil.copyfile(examples / name, root / name)
    path = root / "company.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


def _briefing(cfg_path: Path) -> str:
    import briefing_renderer
    import chief_of_staff
    import daily_briefing

    envelope = daily_briefing.load_workspace_input(str(PLUGIN_ROOT / "examples" / "sample-workspace.json"))
    envelope = chief_of_staff._reanchor_demo_envelope(envelope)
    return briefing_renderer.render(daily_briefing.build_briefing(str(cfg_path), workspace_input=envelope), "text")


def _follow_up(root: Path) -> dict[str, Any]:
    import yaml

    deals = (yaml.safe_load((root / "pipeline.yaml").read_text()) or {}).get("deals") or []
    deal = next((d for d in deals if d.get("contact_email") and d.get("status") == "active"), None)
    if deal is None:
        raise RuntimeError("examples/pipeline.yaml has no active deal with a contact email to follow up")
    return deal


def _steps(root: Path, out: Callable[[str], None]) -> int:
    import config_loader
    import review_queue
    import state_db
    from action_risk import get_action_risk, get_risk_explanation
    from action_execution import run_approved
    from approval_signing import GatewaySigner

    cfg_path = _prepare(root)
    config = config_loader.load_config(cfg_path)
    signer = GatewaySigner.generate()
    signer.enrol(config)          # this demo store only: its Hermes home is the demo directory

    def step(n: int, title: str) -> None:
        out("")
        out(f"Step {n}/7 — {title}")
        out("─" * 70)

    step(1, "Morning briefing (read-only, sample data)")
    for line in _briefing(cfg_path).rstrip().splitlines():
        out(line)

    step(2, "Proposal: the agent suggests a follow-up (nothing is sent)")
    deal = _follow_up(root)
    subject = f"Following up: {deal['client_name']} proposal"
    action = state_db.create_pending_action(
        config, "gmail.send", "demo", deal["contact_email"],
        {"to": deal["contact_email"], "subject": subject,
         "body": f"Hi {deal.get('contact_name', '')}, checking in on the proposal. Happy to answer questions."},
        summary=f"Follow up with {deal['contact_name']} ({deal['client_name']})",
    )
    out(f"Queued action {action['id']}: email {deal['contact_email']} — \"{subject}\"")
    out("It waits in the review queue. The agent cannot approve its own proposal.")

    step(3, "Preview: what it will do, to whom, and how it is reversed")
    out(f"To:              {deal['contact_email']}")
    out(f"Subject:         {subject}")
    out(f"Risk:            {get_action_risk('gmail.send')} — {get_risk_explanation('gmail.send', get_action_risk('gmail.send'))}")
    out(f"Expected effect: {review_queue._expected_effect('gmail.send', deal['contact_email'], action['payload'])}")
    out(f"Reversal:        {review_queue._reversal_hint('gmail.send')}")

    step(4, "Approve: you decide (a signed decision, as with /cos approve)")
    out(f"In Hermes you would type:  /cos approve {action['id']}")
    out("There, the gateway checks who typed it and signs with a key the agent never holds.")
    out("Here the demo plays the gateway's part itself, in this process, against its own throwaway store.")
    signature = signer.sign_decision(state_db.get_pending_action(config, action["id"]), "approve", APPROVER)
    approved = state_db.approve_pending_action(config, action["id"], approver=APPROVER,
                                               reason="demo", signature=signature)
    if not approved:
        out("Approval was refused — the demo cannot continue.")
        return 1
    out(f"Approved by {APPROVER} — signed (key {signer.key_id}).")

    step(5, "Execute (simulated): the real guarded path, a stand-in mailbox")
    mailbox = _demo_mailbox_class()(config)
    current = state_db.get_pending_action(config, action["id"])
    result = run_approved(config, current, mailbox,
                          lambda client, a: client.mail_send(**{k: a["payload"][k] for k in ("to", "subject", "body")}))
    if not result.ok:
        out(f"Execution refused at {result.stage}: {result.error}")
        return 1
    out("Claimed → account checked → capability checked → single-use grant consumed by the guarded "
        "send → recorded.")
    out(f"Simulated: message {result.result['data']['message_id']} to "
        f"{mailbox.sent[0]['to']}. No mail left this machine.")

    step(6, "Audit: what happened, when, and who decided")
    final = state_db.get_pending_action(config, action["id"])
    out(f"State: {final['state']}")
    for label, key in (("requested", "created_at"), ("approved", "approved_at"),
                       ("executing", "executing_at"), ("executed", "executed_at")):
        out(f"  {label:<10} {final.get(key) or '-'}")
    out(f"Approver: {final.get('approver')} (signature recorded: {'yes' if final.get('approval_signature') else 'no'})")
    log = Path(root) / ".audit" / "workspace.log"
    entries = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()] if log.exists() else []
    mine = [e for e in entries if e.get("action_id") == action["id"]]
    out(f"Audit log ({log.relative_to(root)}): {len(mine)} entries for this action")
    for entry in mine:
        out(f"  {entry.get('timestamp')}  {entry.get('operation')}  {entry.get('status')}")

    step(7, "Next briefing: the approval queue afterwards")
    summary = state_db.get_pending_summary(config)
    waiting = summary["by_state"].get("requested", 0) + summary["by_state"].get("approved", 0)
    out(f"Waiting for you: {waiting}. Executed: {summary['by_state'].get('executed', 0)}.")
    out(f"Done: the follow-up to {deal['contact_name']} ({deal['client_name']}) is executed (simulated) "
        f"and on the record.")
    out("")
    out("── End of demo ──")
    return 0


def _unsafe_root(root: Path) -> str | None:
    """Why ``root`` must never be (re)created or deleted by the demo, if so."""
    protected = {Path("/"), Path.home().resolve(), PLUGIN_ROOT.resolve(), Path.cwd().resolve()}
    if root in protected or any(root in p.parents for p in protected):
        return f"{root} is the filesystem root, your home, the current directory, the plugin, or a parent of one"
    return None


def _owned_by_demo(root: Path) -> str | None:
    """None when ``root`` holds only what the demo writes, under its exact marker."""
    marker = root / MARKER
    if marker.is_symlink() or not marker.is_file() or marker.read_text(errors="replace") != MARKER_TEXT:
        return f"{root} has no {MARKER} marker written by the demo"
    extra = sorted(p.name for p in root.iterdir() if p.name not in DEMO_ENTRIES)
    if extra:
        return f"{root} holds files the demo did not write: {', '.join(extra[:5])}"
    return None


def run_flow(out: Callable[[str], None] = print, keep: Path | str | None = None) -> int:
    """Run the seven steps. Returns 0 on success."""
    if keep is not None:
        given = Path(keep).expanduser()
        if given.is_symlink():
            out(f"Refusing: {given} is a symlink. Pick a plain directory.")
            return 2
        root = given.resolve()
        if root.exists() and not root.is_dir():
            out(f"Refusing: {root} exists and is not a directory.")
            return 2
        unsafe = _unsafe_root(root)
        if unsafe:
            out(f"Refusing: {unsafe}.")
            return 2
        if root.exists() and any(root.iterdir()):
            why = _owned_by_demo(root)
            if why:
                out(f"Refusing: {why}. Pick a new directory, or remove it yourself.")
                return 2
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
        (root / MARKER).write_text(MARKER_TEXT)
        with _isolated_env(root / ".hermes-demo"):
            rc = _steps(root, out)
        out(f"Demo state kept in {root}. Remove it with: chief_of_staff.py demo --reset {root}")
        return rc
    with tempfile.TemporaryDirectory(prefix="cos-demo-") as tmp:
        root = Path(tmp)
        with _isolated_env(root / ".hermes-demo"):
            rc = _steps(root, out)
    out("Demo state discarded. Run it again for a fresh start.")
    return rc


def reset(path: Path | str, out: Callable[[str], None] = print) -> int:
    """Delete a kept demo directory — only one carrying the demo marker."""
    given = Path(path).expanduser()
    if given.is_symlink():
        out(f"Refusing: {given} is a symlink.")
        return 2
    root = given.resolve()
    if not root.is_dir():
        out(f"Refusing: {root} is not a directory.")
        return 2
    why = _unsafe_root(root) or _owned_by_demo(root)
    if why:
        out(f"Refusing: {why}.")
        return 2
    shutil.rmtree(root)
    out(f"Removed demo state in {root}.")
    return 0
