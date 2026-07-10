---
name: backup
description: Create scheduled tar.gz backups of key Hermes and Chief of Staff project data and upload them to Google Drive with retention pruning.
version: 0.1.0
author: moonlight-lupin
license: Apache-2.0
metadata:
  hermes:
    tags: [backup, google-drive, cron, hermes, chief-of-staff]
    related_skills: [drive-filer, google-workspace, weekly-review]
---

# Backup

## Overview

Backup protects the user's Hermes and Chief of Staff data by creating a timestamped `tar.gz`, uploading it to Google Drive, pruning old backup archives, and reporting the outcome. It is designed for weekly unattended execution through Hermes cron.

The `backup.py` script uploads and prunes archives through the file store. Today it drives the `google-workspace` skill's `google_api.py` wrapper directly (the Google/Drive dialect shown below); the same intent — upload archive, list backups, delete old archives — maps onto any workspace provider (`google_api` | `composio` | `m365`) or an equivalent native file connector when configured.

```bash
python ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py \
  --account {account} --as {delegate} drive {command}
```

## When to Use

Use this skill when the user asks to:

- Back up Hermes or Chief of Staff data.
- Configure or audit scheduled backups.
- Restore awareness of what is included/excluded.
- Run a weekly backup cron manually.

Do not use it to back up secrets unless the user explicitly changes the exclusion config and accepts the risk.

## What Is Included and Excluded

Included by default:

| Included | Notes |
|---|---|
| `~/.hermes/config.yaml` | Main non-secret Hermes configuration. |
| `~/.hermes/skills/` | User/custom skills. |
| `~/.hermes/projects/{company}/` | Company project data, YAML records, wiki, documents. |
| `~/.hermes/cron/` | Cron job definitions and scheduler config. |

Excluded by default:

| Excluded | Reason |
|---|---|
| `~/.hermes/.env` | Secrets and API keys. |
| `~/.hermes/auth.json` | OAuth tokens. |
| `~/.hermes/state.db` | Session database; large/rebuildable/sensitive. |
| `~/.hermes/sessions/` | Large transient transcripts. |
| `~/.hermes/logs/` | Operational logs, not core data. |

## Configuration

Read from `company.yaml`:

```yaml
company:
  name: "Acme Pte Ltd"

google:
  account: default
  delegate_email: founder@example.com

backup:
  enabled: true
  schedule: "0 3 * * 0"          # Sunday 03:00
  retention_weekly: 4
  retention_monthly: 12
  drive_folder: "09_Backups/"
  drive_folder_id: "..."          # preferred if available
  output_dir: "~/.hermes/backups"
  exclude:
    - ".env"
    - "auth.json"
    - "state.db"
    - "sessions/"
    - "logs/"
```

Defaults:

- `enabled`: true
- `schedule`: `0 3 * * 0`
- `retention_weekly`: 4
- `retention_monthly`: 12
- `drive_folder`: `09_Backups/`
- `exclude`: the default exclusions above

## Workflow

1. Load and validate `company.yaml`.
2. Build a timestamped archive name:

```text
chief-of-staff-{company_slug}-{YYYYMMDD-HHMMSS}.tar.gz
```

3. Create a `tar.gz` containing only the included files/directories.
4. Apply exclusions by basename and path segment.
5. Upload the archive to Drive `09_Backups/` or configured `backup.drive_folder_id`.
6. Prune old backups:
   - Keep the newest `retention_weekly` weekly backups.
   - Keep one monthly backup for each of the newest `retention_monthly` months.
   - Never delete the backup just created.
7. Report:
   - archive path,
   - backup size,
   - file count,
   - upload time,
   - Drive file ID/link if returned,
   - pruned count,
   - errors or skipped paths.

## Script Usage

```bash
python /root/.hermes/plugins/chief-of-staff/skills/backup/scripts/backup.py \
  --config /root/.hermes/plugins/chief-of-staff/shared/config/company.yaml

# Preview without writing archive/uploading/pruning
python /root/.hermes/plugins/chief-of-staff/skills/backup/scripts/backup.py \
  --config company.yaml --dry-run
```

Programmatic functions:

- `create_backup(config, output_path)` — creates archive and returns metadata.
- `prune_old_backups(drive_folder_id, retention_weekly, retention_monthly)` — prunes old Drive archives via `google_api.py`.

## Cron

Default schedule: weekly Sunday 03:00.

Create the Hermes cron job during onboarding:

```bash
hermes cron create "0 3 * * 0" \
  --title "chief-of-staff-weekly-backup" \
  --skills "chief-of-staff:backup" \
  --prompt "Run the Chief of Staff Backup skill using company.yaml. Create tar.gz backup, upload to Google Drive, prune old backups, and deliver a concise report."
```

The cron prompt must be self-contained and must not rely on prior conversation history.

## Google Drive Calls

The script uses command shapes like:

```bash
python ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py \
  --account {account} --as {delegate} drive upload \
  --file {archive_path} --parent-id {drive_folder_id}

python ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py \
  --account {account} --as {delegate} drive list \
  --folder-id {drive_folder_id}

python ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py \
  --account {account} --as {delegate} drive delete \
  --file-id {file_id}
```

If `backup.drive_folder_id` is absent, resolve `backup.drive_folder` through Drive Filer or ask the user to run onboarding. Do not upload to an unknown folder.

## Restore Notes

Backup skill creates archives; restore is intentionally manual in Phase 1:

1. Download the selected archive from Drive.
2. Inspect contents with `tar -tzf` before extracting.
3. Extract into a temporary directory.
4. Copy specific files back into `~/.hermes/` after confirming scope.

Never overwrite live config or skills blindly.

## Common Pitfalls

1. **Backing up secrets.** `.env` and `auth.json` are excluded by default; warn before changing this.
2. **Uploading without folder ID.** Ask for or resolve Drive backup folder ID first.
3. **Pruning before upload succeeds.** Only prune after a successful upload.
4. **Huge session/log archives.** `sessions/` and `logs/` stay excluded unless explicitly requested.
5. **Silent partial backup.** Report skipped missing paths and included file count.

## Verification Checklist

- [ ] `company.yaml` backup config was loaded.
- [ ] Archive was created with expected includes and excludes.
- [ ] `.env`, `auth.json`, `state.db`, `sessions/`, and `logs/` were excluded unless explicitly configured otherwise.
- [ ] Upload used an approved workspace access path (currently `google_api.py ... drive upload`; provider-neutral in intent).
- [ ] Pruning ran only after upload success.
- [ ] Final report includes size, file count, upload time, and pruned count.
