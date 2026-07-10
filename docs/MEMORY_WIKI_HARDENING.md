# Memory & Wiki Hardening Guide (v0.2.7)

## Overview

The agent can maintain the private wiki and memory store automatically, but all changes are linted, logged, reversible, and visible in the Daily Briefing.

## Wiki lint

```bash
# Full lint report
python skills/note-taker/scripts/wiki_curator.py lint

# Summary counts only
python skills/note-taker/scripts/wiki_curator.py lint --summary

# Alias (same as lint)
python skills/note-taker/scripts/wiki_curator.py validate
```

Checks:
- **Broken wikilinks**: `[[Page Name]]` pointing to non-existent pages
- **Missing frontmatter**: Pages without YAML frontmatter or missing `type` field
- **Duplicate pages**: Same title across different paths
- **Stale pages**: `updated` field older than 90 days
- **Orphan pages**: Pages not linked from index.md
- **Low confidence**: Pages with `confidence < 0.5` in frontmatter
- **Contested pages**: Pages with `status: contested` in frontmatter

## Memory lint

```bash
# Full lint report
python shared/scripts/memory.py lint

# Summary counts only
python shared/scripts/memory.py lint --summary
```

Checks:
- **Stale records**: `last_seen_at` older than 30 days
- **Low confidence**: `confidence < 0.5`
- **Contested**: Status not in `draft`, `observed`, `operator_confirmed`
- **Uncited**: `source_ids` is empty
- **Duplicates**: Same name (case-insensitive) across multiple records
- **Missing required fields**: `id`, `type`, `name`

## Memory backup

```bash
# Backup memory.json and memory_changes.json
python shared/scripts/memory.py backup
```

Creates timestamped copies in `.knowledge/`:
- `memory-backup-TIMESTAMP.json`
- `changes-backup-TIMESTAMP.json`

## Memory rollback

```bash
# Dry-run (show what would be rolled back)
python shared/scripts/memory.py rollback --change-id memchg_001 --dry-run

# Execute rollback
python shared/scripts/memory.py rollback --change-id memchg_001
```

Rollback restores the `before` state of a change:
- `memory_create` → deletes the created record
- `memory_update` → restores previous record values

Only reversible changes can be rolled back.

## Wiki auto-backup

When `wiki_curator.py run` processes more than 5 items, it automatically creates a backup of the entire wiki directory before making changes:

```
{wiki_path}/.wiki-backup-{TIMESTAMP}/
```

## Safer change logs

Every memory change includes:
- `before` and `after` snapshots (for rollback)
- `reversible` flag
- `source_ids` (provenance)
- `risk` level (low/medium/high)
- `mode` (autonomous/manual)

## Daily Briefing integration

The Daily Briefing knowledge maintenance section now includes lint warnings:

```
Knowledge maintenance:
  Updated 4 wiki page(s).
  Created 2 memory record(s).
  Memory records: 15 total.
  Lint warnings: 3 stale, 1 low-confidence, 2 broken wiki links
  Run: python shared/scripts/memory.py lint --summary
  Run: python skills/note-taker/scripts/wiki_curator.py lint --summary
```