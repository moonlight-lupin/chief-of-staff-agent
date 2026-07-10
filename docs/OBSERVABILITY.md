# Observability and self-diagnosis (v0.3.4)

Chief-of-Staff keeps **two** independent logs. Keep them straight:

| | Audit log | Operational (runtime) log |
| --- | --- | --- |
| Question it answers | What *changed*? | What was *attempted*? |
| Written by | `audit_log.py` / `workspace_audit.py` | `shared/scripts/runtime_log.py` |
| Content | before/after state mutations | provider calls, retries, throttling, timings, outcomes |
| Location | `<project_root>/.audit/` | `<project_root>/.runs/<run-id>/` |
| Safe to attach to a bug report? | No (contains payloads) | **Yes** — redacted by construction |

The operational log is what powers `logs recent/show/diagnose/bundle` and the
deterministic diagnosis engine (`shared/scripts/log_analyser.py`). It never
records message bodies, audit payloads, tokens, or secrets.

## Run ids and propagation

Every operational command runs under a **run**. A run id looks like:

```
20260710T140355Z-9f3ac1        (YYYYMMDDTHHMMSSZ-<6 hex>)
```

Each run gets a directory `<project_root>/.runs/<run-id>/` containing:

- `events.jsonl` — one JSON object per line, appended as the run proceeds
- `summary.json` — written when the run finishes (outcome + counts + first error)

The lifecycle is wired into the entrypoints: `chief_of_staff.py` calls
`init_run()` at dispatch and `finish_run()` in a `finally` block. The outcome is
`success`, `degraded` (any warnings were **observed** — see counters below), or
`failed` (non-zero exit or an exception).

`log_event()` with **no active run is a silent no-op** — no file, no console. A
stray event with no owning run would be orphaned and misleading, so it is
dropped. (Console-only mode is reserved for the case where `init_run()` created a
context but the project root was unresolvable: events still print to stderr but
are never written to disk.)

**Propagation to child processes.** When `chief_of_staff` shells out to a helper
(for example `daily_briefing.py` or `deadlines.py`), it exports
`CHIEF_OF_STAFF_RUN_ID` (and the project root). The child calls `init_run()`,
sees the env var, and **joins** the same run — its events append to the parent's
`events.jsonl` instead of starting a new run. The `logs` subcommands are the one
exception: they are pure inspection and deliberately do **not** create a run
(this avoids recursion and log noise).

**Run ownership — who writes `summary.json`.** Exactly one context OWNS a run:
the one that CREATED the run directory (the first `init_run()` with no
`CHIEF_OF_STAFF_RUN_ID` set). Only the owner writes `summary.json` on
`finish_run()` and clears the env var it set. Any later `init_run()` that sees
the env var already set is a **joiner** (a child process, or a same-process
nested call). A joiner appends events only; its `finish_run()` emits a
`child_completed` (outcome success/degraded) or `child_failed` event carrying its
own observed counters, **never** writes or overwrites `summary.json`, and
restores the previous context (nested parents become active again after a child
finishes). This is what stops a helper from clobbering the parent's summary.

The owner computes the summary's final counts, `first_error`, and `warnings[]` by
reading `events.jsonl` (the single source of truth across processes), merged with
its own observed counters (see below).

## Event vocabulary

The instrumentation emits a fixed set of events. Diagnosis rules match against
these:

| Event | Key fields |
| --- | --- |
| `provider_request_started` / `_completed` / `_failed` | `provider, operation, method, endpoint_category, status_code, duration_ms, attempt, error_class, message` |
| `provider_retry` | `status_code, attempt, wait_s, reason` |
| `retry_deferred` | `retry_after_s` |
| `ambiguous_write` | `status_code, method` |
| `pagination_truncated` | `cap, pages_followed` |
| `query_compiled` | `dialect, has_filter, has_search, folder` |
| `guardrail_blocked` | `action, reason` |
| `action_requested` / `_executed` / `_failed` | `action_id, action_type, provider, error` |
| `state_loaded` / `state_saved` | `store` |
| `audit_write_failed` | `reason` |
| `run_started` / `run_completed` / `run_failed` | `outcome` |
| `child_completed` / `child_failed` | `outcome, counts` (a joiner's observed counters) |

`error_class` is one of: `throttled` (HTTP 429 client rate-limiting),
`provider_unavailable` (HTTP 503/504 server-side outage), `permission_denied`,
`not_found`, `auth`, `network` (transport failures — timeouts, connection/TLS
errors — which also carry `exception_type`), or `other`. A 503/504 is an outage,
**not** throttling, so it is classed `provider_unavailable` — mislabelling it
`throttled` produced misleading rate-limit diagnoses for real outages. Graph
errors also carry human hint text (admin consent, user principal, OneDrive
provisioning, calendar access, expired secret) inside `message`.

## Log levels and controls

Levels: `debug` < `info` < `warning` < `error`. Events below the active
threshold are dropped **from the file/console** — but they are still counted.

**Observed vs emitted counters (outcome is level-independent).** Every
`log_event()` call increments an *observed* severity counter regardless of the
active level; the threshold gates only what is *emitted* to `events.jsonl` /
console. Run outcome (success/degraded/failed) derives from OBSERVED
warnings/errors, so `--log-level error` can no longer flip a genuinely degraded
run to "success" by hiding its warnings. Because below-threshold events never
reach `events.jsonl`, the owner's summary counts are, per level,
`max(count in events.jsonl, owner observed + children's reported observed)` — the
file term captures every process's emitted events, the observed term captures
what was seen but not written.

| Control | Effect |
| --- | --- |
| `--log-level {debug,info,warning,error}` | Set the file/console level for this invocation |
| `--quiet` | Silence console (stderr) logging; file logging is unaffected |
| `CHIEF_OF_STAFF_LOG_LEVEL` (env) | Default level when the flag is absent |
| `logging.level` (in `company.yaml`) | Config default level |

```yaml
# company.yaml
logging:
  level: INFO           # default level
  retention_days: 30    # prune deletes run dirs older than this
  max_runs: 200         # prune keeps at most this many run dirs
```

## `logs` commands

All commands are read-only and honor `--json` where applicable.

```bash
# List recent runs (newest first): run id, command, outcome, err/warn, age.
python shared/scripts/chief_of_staff.py logs recent --limit 20

# Pretty-print one run's events, optionally filtered by level.
python shared/scripts/chief_of_staff.py logs show --run-id 20260710T140355Z-9f3ac1 --level warning

# Deterministic, rule-based diagnosis (no LLM).
python shared/scripts/chief_of_staff.py logs diagnose --run-id 20260710T140355Z-9f3ac1
python shared/scripts/chief_of_staff.py logs diagnose --latest-failed

# Delete old runs per retention config.
python shared/scripts/chief_of_staff.py logs prune

# Write a redacted support archive for a bug report.
python shared/scripts/chief_of_staff.py logs bundle --latest-failed --output cos-support.zip
```

### Example diagnosis output

```
Run: 20260710T140355Z-9f3ac1
Command: chief_of_staff daily
Status: failed

Primary finding: auth_expired (error)

Evidence:
  - provider_request_failed m365 mail_read GET mail status 401 auth

Likely cause: The access token was expired or rejected (HTTP 401) and a token
refresh is indicated before the operation can succeed.

Recommended action:
  Re-run the operation: the next token request re-authenticates automatically
  (device_code auth triggers an interactive sign-in).
    $ python shared/scripts/connect_workspace.py --status
    $ python shared/scripts/connect_workspace.py --provider m365 --verify
    $ python shared/scripts/chief_of_staff.py readiness --summary
  Retry safe: yes

No configuration change is currently indicated.
```

The diagnosis engine ships ~18 classifications, each mapping evidence to a plain
explanation, safe remediation, exact next commands, and a `retry_safe` flag.
Notably, an `ambiguous_write` (a write whose outcome is unknown) is
`retry_safe: false` — verify in the external system before retrying.

### Readiness integration

`readiness` runs under the lifecycle like any operational command. When any
readiness row is **FAIL**, the summary/markdown output appends a pointer:

```
  Run ID: 20260710T140355Z-9f3ac1
  Diagnose:
    python shared/scripts/chief_of_staff.py logs diagnose --run-id 20260710T140355Z-9f3ac1
```

The `--json` form gains a top-level `run_id` field.

## Support bundle contents and exclusions

`logs bundle` writes a zip containing **exactly** these files, all redacted:

| File | Contents |
| --- | --- |
| `events.jsonl` | The run's events (already scrubbed of credentials at write time) |
| `summary.json` | The run summary (outcome, counts, first error — scrubbed at write time) |
| `diagnosis.json` | A fresh `analyse_run` result (redacted before archiving) |
| `readiness.json` | The readiness report, or a stub if config is unavailable (redacted before archiving) |
| `meta.json` | Plugin version, Python version, platform, provider, capability report (redacted before archiving) |
| `config_shape.json` | Config **structure only** — every leaf replaced by its type name |

`events.jsonl` and `summary.json` are redaction-safe by construction (scrubbed as
they are written). `diagnosis.json`, `readiness.json`, and `meta.json` are freshly
built at bundle time and may echo config or error text, so every one is passed
through the public `runtime_log.redact()` helper before it is zipped — Bearer
tokens, JWTs, and `client_secret=`/`password=`/`api_key=` pairs are scrubbed and
sensitive-named keys replaced. `config_shape.json` never contains values, and any
key matching `secret`/`token`/`password` is dropped entirely (subtree and all).

**Never included:** state files, wiki, token caches, message bodies, audit
before/after payloads, environment values.

> Attach `cos-support.zip` to your bug report. It is safe to share by default.

## Retention

`logs prune` (and `runtime_log.prune_runs`) deletes run directories older than
`logging.retention_days` (default 30) and keeps at most `logging.max_runs`
(default 200), newest first. Only run-id-shaped directories are ever touched —
other entries under `.runs/` are left alone.
