#!/bin/bash
# Stop hook for Claude Code on the web: do not let the session end while
# Chief-of-Staff state is uncommitted or unpushed in the data repo — the VM is
# ephemeral. Exit 2 hands the remedy back to Claude. See docs/CLAUDE_CODE.md.
set -uo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
	exit 0
fi

plugin="$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)"
root="${CLAUDE_PROJECT_DIR:-$plugin}"
py="${CHIEF_OF_STAFF_PYTHON:-$plugin/.venv/bin/python}"
[ -x "$py" ] || py=python3

# Already reminded once this turn: never loop.
if "$py" -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("stop_hook_active") else 1)' 2>/dev/null; then
	exit 0
fi

data="${CHIEF_OF_STAFF_DATA_DIR:-}"
if [ -z "$data" ] && [ -f "$root/.claude/cos-data-dir.local" ]; then
	data="$(cat "$root/.claude/cos-data-dir.local")"
fi
if [ -z "$data" ] || [ ! -d "$data" ]; then
	exit 0
fi

exec "$py" "$plugin/shared/scripts/state_sync.py" stop-check --project-root "$data"
