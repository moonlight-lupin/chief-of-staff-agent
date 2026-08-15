# Security Policy

Chief of Staff reads mail, calendars, and files from a real workspace, holds
provider credentials, and can be approved to send email and modify workspace
state. Security reports are taken seriously.

## Reporting a vulnerability

**Do not open a public issue for a security vulnerability.**

Report privately through GitHub's [private vulnerability
reporting](https://github.com/moonlight-lupin/chief-of-staff-agent/security/advisories/new)
on this repository. If that is unavailable to you, open a public issue
containing only the words "security report, please make contact" and no
technical detail, and a maintainer will arrange a private channel.

Please include:

- What the issue is and which files or commands are involved
- How to reproduce it, ideally against the bundled sample data (`chief_of_staff.py demo`)
- What an attacker gains — particularly whether it bypasses the approval gate,
  leaks credentials or message content, or writes to a workspace without an
  audit record
- Any provider (`google_api`, `composio`, `m365`, `agent`) it is specific to

Expect an acknowledgement within 7 days and an assessment within 30.

## Scope

In scope, and treated as high severity:

- **Approval-gate bypass** — any path that mutates a workspace without passing
  `workspace_guardrails.confirm_action` and a per-action grant, or that lets an
  action reach `executed` without having been `approved` and `executing`
- **Audit evasion** — a successful mutation that leaves no audit record, or
  tampering with the audit hash chain that the integrity check does not detect
- **Credential or content disclosure** — secrets, tokens, message bodies, or
  document contents reaching operational logs, support bundles, or
  `ActionResult` payloads at any log level
- **Webhook forgery** — defeating the Pub/Sub OIDC verification or the HMAC
  timestamp binding in `webhook_security.py`
- **State corruption or replay** — defeating the file locking or the replay
  reservation to double-execute an action
- **Path escape** — reading or writing outside the configured project root

Out of scope:

- Anything requiring the operator to deliberately set
  `CHIEF_OF_STAFF_AUTO_APPROVE=1` or `CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1`
  outside the documented approval flow. These are documented break-glass
  switches; misusing them is an operator decision, not a vulnerability. A path
  that causes them to be set *implicitly*, or that makes a block bypassable
  without them, **is** in scope.
- Native Microsoft 365 Graph findings that depend on a live Entra tenant. That
  path is code-complete but has never been live-verified — see the M365 section
  in `docs/PRODUCTION_ROADMAP.md`. Report them anyway; they will be tracked, but
  they are known-unverified rather than regressions.
- Third-party vulnerabilities in the packages listed in `requirements.txt`.
  Report those upstream; open a normal issue here if a version pin should move.

## Operator security notes

- **Secrets belong in `.env`, never in `company.yaml`.** Config files are
  designed to be readable and shareable; `.env` is gitignored.
- **Never put credentials in a cloud environment's variables.** Environment
  variables in a hosted Claude Code or Cowork environment are stored in plain
  text and are readable by anyone who uses that environment. Use the `agent`
  workspace provider there, which holds no credentials at all.
- **The daily loop is externally read-only.** It writes a local
  `.last_briefing` timestamp and nothing else. If you observe `daily` mutating
  a workspace, that is a reportable bug.
- **Support bundles are redacted by construction**, but skim one before
  attaching it to a public issue.
