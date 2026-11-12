# Hermes plugin catalog

How this plugin meets the [Hermes plugin catalog](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission)
admission rules, and how to submit or update the entry. The canonical rules
are in [`NousResearch/hermes-agent` → `plugin-catalog/README.md`](https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/README.md).
`tests/test_hermes_catalog.py` enforces the parts that can be checked in code.

## Rule by rule

| Rule | How this plugin meets it |
|---|---|
| 1, 4, 5: human-merged, SHA bumps re-reviewed, owner submits | The owner (`moonlight-lupin`) submits. Each release is a new SHA-bump PR. |
| 2: exact SHA pin | Pin the release commit, not the tag. |
| 3: no self-updating code | None. Updates arrive only through `hermes plugins update`. |
| 6: declared capabilities match | `plugin.yaml` declares the six read-only `cos_*` tools and the five hooks `register()` registers. Skills come through `register_skill`; the slash commands `/<assistant name>` and `/cos` through `register_command`. Tested. |
| 7: install scanner | Run `hermes plugins validate . --install-deps` at the commit you pin, and read every warning. Expect one `caution`: the repository has just over the scanner's 400-file soft limit (most are tests). It is a warning for the reviewer, not a failure. See "Validation status" below. |
| 8: Desktop SDK | No Desktop half. |
| 9: no core overrides | Only `register_skill`, `register_hook`, `register_session_env`, `register_tool` and `register_command`. Nothing is patched. |
| 10: dependency policy | Each dependency has a floor and an upper bound, declared in both `pyproject.toml` and `plugin.yaml` → `python_dependencies`. Tested. |
| 11: credentials through Hermes | Each secret is a `config_schema` `type: secret`, read in-process with `get_secret` and handed to the plugin's own commands per session by `register_session_env`. Under Hermes the plugin never reads a `.env` file or a Google key file: the service-account key is the `GOOGLE_SERVICE_ACCOUNT_JSON` secret. No OAuth tokens are written. Tested. |
| 12: approvals and unattended runs | Nothing routes around Hermes's approvals. The plugin's own review queue adds a further gate. No tool can approve. Approval is only `/<name> approve`, from the owner's own surfaces or listed chat approvers; anything else, including a chat command without a source, is refused. Without a terminal, every prompt refuses cleanly: guardrail confirmations, interactive onboarding, self-sign confirmation, and Microsoft 365 device-code sign-in. Tested. |
| 13: disclosure | README → "What it touches", repeated in the entry PR (below). |
| 14: truthful compatibility | `requires_hermes: ">=0.23.0"` in both `plugin.yaml` and the entry. Raise it only to a released version you have tested. |
| 15, 16 | Original work, not a fork or a skin of a bundled plugin. |
| 17: no unscannable payloads | No compiled binaries, native libraries, WebAssembly or committed bytecode: only readable source. Tested by file header, not by extension. Nothing is fetched at setup. |
| 18: password managers | No `browser_vault` login backend. |
| 19: infringing content | None. The paid data sources are licensed APIs used against approved budgets. |

## Validation status

Last checked on 2026-11-12, against `hermes-agent` at `72c880370f`, by
reading `hermes_cli/plugin_validate.py` and `tools/plugin_guard.py`.
`tests/test_hermes_catalog.py` mirrors these static checks:

| Check | Result |
|---|---|
| Manifest fields (`name`, `version`, `description`) | Pass |
| `requires_hermes` parses strictly | Pass (`>=0.23.0`) |
| `config_schema` types | Pass: every field is `type: secret`, which Hermes accepts |
| `requires_env` upper snake case | Pass (none declared) |
| Python dependencies are PEP 508 with bounds | Pass |
| Declared tools and hooks match `register()` | Pass (capability tests above) |
| Rule 17: no binaries or bytecode, no file over 1 MB | Pass |
| Install scanner file count | **Caution**: about 410 tracked files against a 400-file soft limit. A medium finding, so the verdict is `caution`, not `dangerous`. |
| Install scanner content patterns | **Not run here.** Run `hermes plugins validate` at the release SHA before submitting. |

Before submitting, run Hermes's own validator at the release commit, and
paste its report into the entry PR:

```bash
hermes plugins validate /path/to/chief-of-staff-agent --install-deps
```

## Setting secrets under Hermes

All secrets are optional, one per workspace provider. Set them in the Desktop
under **Settings → Plugins → Chief of Staff** (masked fields), where Hermes
stores them. For the Google service account, paste the **contents** of the key
JSON file into `GOOGLE_SERVICE_ACCOUNT_JSON`. Under Hermes a
`google.service_account_path` key file is refused.

Standalone and Claude Code installs keep working as before: the command
environment, plus the plugin-root `.env`.

## Entry: `plugin-catalog/chief-of-staff.yaml`

```yaml
name: chief-of-staff
repo: https://github.com/moonlight-lupin/chief-of-staff-agent
sha: "<40-char commit of the release>"
title: Chief of Staff
description: "Approval-gated AI chief of staff for SMEs, with read-only tools for the agent and a slash command named after your assistant (/ada, plus /cos) where you approve: daily briefings, statutory deadlines, CRM pipeline, bookkeeping and AR/AP, documents and e-signature, meeting prep, research, plus prospecting, SEO and competitor watch on budgets you approve. Several companies per install. Every consequential action goes propose → approve → execute → audit. Disclosure — calls only the services you configure (Google APIs, Microsoft Graph, Composio, your DocuSeal server; Monid and Notion through the agent's own connectors, paid calls only against approved budgets); runs hermes cron create/list for the briefings you approve, git for sync push to your private data repo, and the optional google-workspace skill's google_api.py; keeps your records as YAML/Markdown under the project root you choose plus a local state.db (review queue, audit log, spend ledger) and redacted run logs; no background processes by default (an optional webhook listener you start yourself only records events); credentials only through Hermes secrets; no telemetry."
maintainer: moonlight-lupin
tier: community
category: automation
requires_hermes: ">=0.23.0"
docs_url: https://github.com/moonlight-lupin/chief-of-staff-agent#readme
version: "0.9.0"
readme: true
capabilities:
  provides_tools: [cos_capabilities, cos_daily, cos_review_queue, cos_preview, cos_pipeline, cos_spend_status]
  provides_hooks: [on_session_start, pre_llm_call, post_llm_call, pre_tool_call, post_tool_call]
  provides_middleware: []
  requires_env: []
```

## Submitting

1. Tag the release. Note the full commit SHA, not the tag.
2. At that commit, run `hermes plugins validate /path/to/chief-of-staff-agent --install-deps`.
   Fix every failure and read every warning.
3. In `NousResearch/hermes-agent`, add `plugin-catalog/chief-of-staff.yaml`
   (above) with `sha` set to that commit.
4. In the PR description:
   - say what the plugin does and which Hermes surfaces it uses (skills via `register_skill`, six read-only tools, the `/<name>` and `/cos` slash commands, five hooks, `register_session_env`);
   - paste the README's "What it touches".
5. Wait for catalog CI, then answer review.

Updates: open a new PR bumping `sha` and `version` together.
