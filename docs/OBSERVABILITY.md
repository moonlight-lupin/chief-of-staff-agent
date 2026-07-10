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
`success`, `degraded` (any warnings were logged), or `failed` (non-zero exit or
an exception).

**Propagation to child processes.** When `chief_of_staff` shells out to a helper
(for example `daily_briefing.py` or `deadlines.py`), it exports
`CHIEF_OF_STAFF_RUN_ID` (and the project root). The child calls `init_run()`,
sees the env var, and **joins** the same run — its events append to the parent's
`events.jsonl` instead of starting a new run. The `logs` subcommands are the one
exception: they are pure inspection and deliberately do **not** create a run
(this avoids recursion and log noise).

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

`error_class` is one of: `throttled`, `retry_deferred`, `ambiguous_write`,
`permission_denied`, `not_found`, `auth`, `network`, `other`. Graph errors also
carry human hint text (admin consent, user principal, OneDrive provisioning,
calendar access, expired secret) inside `message`.

## Log levels and controls

Levels: `debug` < `info` < `warning` < `error`. Events below the active
threshold are dropped.

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
  Refresh/reconnect the workspace credentials, then re-run the operation.
    $ python shared/scripts/connect_workspace.py --reconnect
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
| `events.jsonl` | The run's events (already scrubbed of credentials) |
| `summary.json` | The run summary (outcome, counts, first error) |
| `diagnosis.json` | A fresh `analyse_run` result |
| `readiness.json` | The readiness report (or a stub if config is unavailable) |
| `meta.json` | Plugin version, Python version, platform, provider, capability report |
| `config_shape.json` | Config **structure only** — every leaf replaced by its type name |

`config_shape.json` never contains values, and any key matching
`secret`/`token`/`password` is dropped entirely (subtree and all).

**Never included:** state files, wiki, token caches, message bodies, audit
before/after payloads, environment values.

> Attach `cos-support.zip` to your bug report. It is safe to share by default.

## Retention

`logs prune` (and `runtime_log.prune_runs`) deletes run directories older than
`logging.retention_days` (default 30) and keeps at most `logging.max_runs`
(default 200), newest first. Only run-id-shaped directories are ever touched —
other entries under `.runs/` are left alone.
