#!/bin/bash
# SessionStart hook for Claude Code on the web.
#
# A cloud session starts from a fresh clone: no .venv, no company.yaml
# (gitignored), and no project data. This hook:
#   1. builds the plugin venv from requirements.txt (plus pytest/ruff);
#   2. locates the private data repo — $CHIEF_OF_STAFF_DATA_DIR, else a clone of
#      $CHIEF_OF_STAFF_DATA_REPO next to the plugin, else a sibling
#      chief-of-staff-data/ checkout — and fast-forwards it;
#   3. links shared/config/{company,drive-map,queries}.yaml into <data>/config/;
#   4. exports CHIEF_OF_STAFF_PROJECT_ROOT for the session.
# It never fails the session: problems are reported and the session continues.
# See docs/CLAUDE_CODE.md.
set -uo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
	exit 0
fi

root="${CLAUDE_PROJECT_DIR:-$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)}"
env_file="${CLAUDE_ENV_FILE:-/dev/null}"
say() { echo "chief-of-staff: $*"; }

# 1. Plugin venv. pip install is idempotent and the container is cached after
#    this hook, so later sessions reuse it.
if [ "${CHIEF_OF_STAFF_SKIP_VENV:-}" != "1" ]; then
	if [ ! -x "$root/.venv/bin/python" ]; then
		python3 -m venv "$root/.venv" || say "could not create .venv"
	fi
	if "$root/.venv/bin/python" -m pip install -q -r "$root/requirements.txt" pytest 'ruff==0.16.3' >&2; then
		say "venv ready ($root/.venv)"
	else
		say "dependency install failed; run: .venv/bin/python -m pip install -r requirements.txt"
	fi
fi

# 2. Data repo.
data="${CHIEF_OF_STAFF_DATA_DIR:-}"
repo="${CHIEF_OF_STAFF_DATA_REPO:-}"
if [ -z "$data" ]; then
	if [ -n "$repo" ]; then
		name="$(basename "${repo%/}")"
		data="$(dirname "$root")/${name%.git}"
	elif [ -d "$(dirname "$root")/chief-of-staff-data/.git" ]; then
		data="$(dirname "$root")/chief-of-staff-data"
	fi
fi

if [ -n "$data" ] && [ ! -d "$data/.git" ] && [ -n "$repo" ]; then
	case "$repo" in
	*://* | /* | git@*) url="$repo" ;;
	*) url="https://github.com/$repo" ;;
	esac
	if git clone -q "$url" "$data" >&2; then
		say "cloned data repo into $data"
	else
		say "could not clone the data repo ($repo). Add it to this environment's repositories and check access."
	fi
fi

if [ -z "$data" ] || [ ! -d "$data/.git" ]; then
	say "no data repo configured — project state is ephemeral and will be lost when this session ends. Set CHIEF_OF_STAFF_DATA_REPO (see docs/CLAUDE_CODE.md)."
	exit 0
fi

if [ -z "$(git -C "$data" status --porcelain)" ]; then
	branch="$(git -C "$data" symbolic-ref --short HEAD 2>/dev/null || echo main)"
	if git -C "$data" fetch -q origin >&2 && git -C "$data" rev-parse -q --verify "origin/$branch" >/dev/null; then
		git -C "$data" merge -q --ff-only "origin/$branch" >&2 ||
			say "data repo has diverged from origin; resolve by hand before syncing"
	fi
else
	say "data repo has uncommitted changes; skipped pull"
fi

# 3. Live config lives in the data repo; never replace a real local file.
mkdir -p "$data/config"
for f in company.yaml drive-map.yaml queries.yaml; do
	link="$root/shared/config/$f"
	if [ -e "$link" ] && [ ! -L "$link" ]; then
		say "keeping existing shared/config/$f (not linked to the data repo)"
		continue
	fi
	ln -sfn "$data/config/$f" "$link"
done

# 4. Export for the session, and record for the Stop hook.
echo "export CHIEF_OF_STAFF_PROJECT_ROOT=$data" >>"$env_file"
echo "export CHIEF_OF_STAFF_DATA_DIR=$data" >>"$env_file"
mkdir -p "$root/.claude"
echo "$data" >"$root/.claude/cos-data-dir.local"

if [ -e "$data/config/company.yaml" ]; then
	say "data repo $data is git-synced; run 'chief_of_staff.py sync push' to save state"
else
	say "data repo $data has no config yet — bootstrap with --provider agent --project-root $data"
fi
exit 0
