#!/usr/bin/env python3
"""Competitor watch: compare snapshots of competitors' pages over time.

The agent captures each competitor's key pages (URL, title, a plain-text
excerpt). Only a content hash, the title and the prices found in the text are
kept; page text itself is never stored. ``diff`` reports new and removed
pages, title changes, content changes and price changes since the last saved
snapshot.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

STORE = "competitor_snapshots"
# S$99, $1,200, US$5, USD 1,200, SGD 49.90, €20, £15 — with an optional /month etc.
PRICE_RE = re.compile(
    r"(?:(?:[A-Z]{1,3})?[$€£]\s?\d[\d,]*(?:\.\d+)?|\b(?:USD|SGD|EUR|GBP|AUD|MYR|HKD)\s?\d[\d,]*(?:\.\d+)?)"
)


def _prices(text: str) -> list[str]:
    return sorted({re.sub(r"\s+", " ", m.group(0)).strip() for m in PRICE_RE.finditer(text or "")})


def _fingerprint(page: Mapping[str, Any]) -> dict[str, Any]:
    text = " ".join(str(page.get("text") or "").split())
    return {"title": str(page.get("title") or "").strip(),
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None,
            "prices": _prices(text)}


def load_snapshots(config: Any) -> dict[str, Any]:
    from state_db import _open_db

    with _open_db(config) as db:
        return db.get_kv(STORE) or {}


def diff(config: Any, snapshot: Mapping[str, Any], save: bool = False) -> dict[str, Any]:
    from state_db import _open_db

    previous = (load_snapshots(config).get("competitors") or {})
    current: dict[str, dict[str, Any]] = {}
    changes: list[dict[str, Any]] = []
    for name, comp in (snapshot.get("competitors") or {}).items():
        pages = {str(p["url"]): _fingerprint(p) for p in comp.get("pages") or [] if p.get("url")}
        current[name] = {"pages": pages, "captured_at": snapshot.get("captured_at")}
        before = (previous.get(name) or {}).get("pages")
        if before is None:
            continue
        for url, fp in pages.items():
            old = before.get(url)
            if old is None:
                changes.append({"competitor": name, "kind": "new_page", "url": url, "title": fp["title"]})
                continue
            if old["title"] != fp["title"]:
                changes.append({"competitor": name, "kind": "title_changed", "url": url,
                                "from": old["title"], "to": fp["title"]})
            if old["prices"] != fp["prices"]:
                changes.append({"competitor": name, "kind": "prices_changed", "url": url,
                                "added": sorted(set(fp["prices"]) - set(old["prices"])),
                                "removed": sorted(set(old["prices"]) - set(fp["prices"]))})
            elif old["content_hash"] != fp["content_hash"]:
                changes.append({"competitor": name, "kind": "content_changed", "url": url})
        for url in before:
            if url not in pages:
                changes.append({"competitor": name, "kind": "page_removed", "url": url})

    baseline = not any(name in previous for name in current)
    if save:
        def store(data: dict[str, Any]) -> None:
            data.setdefault("competitors", {}).update(current)

        with _open_db(config) as db:
            db.mutate_kv(STORE, store)
    return {"baseline": baseline, "saved": save, "changes": changes,
            "competitors": sorted(current), "captured_at": snapshot.get("captured_at")}
