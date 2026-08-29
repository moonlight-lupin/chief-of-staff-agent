#!/bin/sh
set -eu

dir=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
py="$dir/.venv/bin/python"

if [ ! -x "$py" ]; then
	echo "error: $py not found; create the plugin venv with: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt" >&2
	exit 127
fi

exec "$py" "$@"
