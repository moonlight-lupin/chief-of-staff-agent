#!/usr/bin/env python3
"""Weekly review CLI. Implementation lives in shared/scripts/weekly_summary.py."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SHARED = Path(__file__).resolve().parents[3] / "shared" / "scripts"
_TARGET = _SHARED / "weekly_summary.py"
_spec = importlib.util.spec_from_file_location("cos_weekly_summary", _TARGET)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load weekly summary from {_TARGET}")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["cos_weekly_summary"] = _mod
_spec.loader.exec_module(_mod)

build_weekly_summary = _mod.build_weekly_summary
build_weekly_summary_from_config = _mod.build_weekly_summary_from_config
WEEKLY_TITLE = _mod.WEEKLY_TITLE
main = _mod.main

if __name__ == "__main__":
    raise SystemExit(main())
