#!/usr/bin/env python3
"""SEO audit: on-page checks, keyword rankings and competitors, prioritised fixes.

Pure computation over what the agent fetched: crawled page facts (status,
title, meta description, headings, canonical, word count, images without alt
text, load time, noindex) and search results per keyword. Fetching may cost
money (Monid SERP/scrape endpoints); that goes through spend envelopes, not
here. ``record`` keeps a summary (no page content) so the next audit can show
what moved.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

STORE = "seo_audits"
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# code → (severity, what to do)
CHECKS: dict[str, tuple[str, str]] = {
    "http_error": ("high", "Fix or redirect (301) pages that return an error"),
    "noindex": ("high", "Remove noindex from pages that should appear in search"),
    "missing_title": ("high", "Give every page a unique title"),
    "slow_page": ("medium", "Speed up pages that take over 3 seconds to load"),
    "duplicate_title": ("medium", "Make duplicated titles unique"),
    "missing_meta_description": ("medium", "Write a meta description (120–160 characters)"),
    "h1_count": ("medium", "Use exactly one H1 per page"),
    "title_length": ("medium", "Keep titles between 30 and 60 characters"),
    "thin_content": ("medium", "Expand pages with under 300 words or merge them"),
    "meta_description_length": ("low", "Shorten meta descriptions to 160 characters"),
    "missing_canonical": ("low", "Add a canonical URL"),
    "images_missing_alt": ("low", "Add alt text to images"),
}


def _host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _issues(pages: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    def add(code: str, url: str, detail: str = "") -> None:
        issues.append({"code": code, "url": url, "severity": CHECKS[code][0], "detail": detail})

    title_count = Counter(str(p.get("title") or "").strip().casefold() for p in pages if p.get("title"))
    for p in pages:
        url = str(p.get("url"))
        status = int(p.get("status") or 200)
        title = str(p.get("title") or "").strip()
        if status >= 400:
            add("http_error", url, f"HTTP {status}")
            continue
        if p.get("noindex"):
            add("noindex", url)
        if not title:
            add("missing_title", url)
        else:
            if not 30 <= len(title) <= 60:
                add("title_length", url, f"{len(title)} characters")
            if title_count[title.casefold()] > 1:
                add("duplicate_title", url, f"'{title}' is used {title_count[title.casefold()]} times")
        meta = p.get("meta_description")
        if not meta:
            add("missing_meta_description", url)
        elif len(str(meta)) > 160:
            add("meta_description_length", url, f"{len(str(meta))} characters")
        h1 = p.get("h1")
        if h1 is not None and len(h1) != 1:
            add("h1_count", url, f"{len(h1)} H1 headings")
        if "canonical" in p and not p.get("canonical"):
            add("missing_canonical", url)
        words = p.get("word_count")
        if words is not None and int(words) < 300:
            add("thin_content", url, f"{words} words")
        if int(p.get("images_missing_alt") or 0) > 0:
            add("images_missing_alt", url, f"{p['images_missing_alt']} image(s)")
        if int(p.get("load_ms") or 0) > 3000:
            add("slow_page", url, f"{p['load_ms']} ms")
    return issues


def analyze(pages: list[Mapping[str, Any]], serp: list[Mapping[str, Any]] | None, site: str) -> dict[str, Any]:
    site = site.lower().removeprefix("www.")
    issues = _issues(pages)

    grouped: dict[str, list[str]] = defaultdict(list)
    for i in issues:
        grouped[i["code"]].append(i["url"])
    fixes = sorted(
        ({"code": code, "severity": CHECKS[code][0], "action": CHECKS[code][1], "pages": len(urls),
          "urls": urls[:10]} for code, urls in grouped.items()),
        key=lambda f: (SEVERITY_ORDER[f["severity"]], -f["pages"]),
    )

    rankings: list[dict[str, Any]] = []
    rivals: dict[str, dict[str, Any]] = {}
    for entry in serp or []:
        results = sorted(entry.get("results") or [], key=lambda r: r.get("position") or 999)
        ours = next((r for r in results if _host(str(r.get("url"))) == site), None)
        rankings.append({"keyword": entry["keyword"], "position": ours.get("position") if ours else None,
                         "url": ours.get("url") if ours else None})
        seen: set[str] = set()
        for r in results:
            host = _host(str(r.get("url")))
            if host == site or host in seen or (r.get("position") or 99) > 10:
                continue
            seen.add(host)
            rival = rivals.setdefault(host, {"domain": host, "keywords": 0, "best_position": 99})
            rival["keywords"] += 1
            rival["best_position"] = min(rival["best_position"], r.get("position") or 99)
    competitors = sorted(rivals.values(), key=lambda c: (-c["keywords"], c["best_position"]))
    opportunities = [
        {"keyword": r["keyword"], "position": r["position"],
         "why": "not in the top 10" if r["position"] is None or r["position"] > 10 else "on page 1 but not top 3"}
        for r in rankings if r["position"] is None or r["position"] > 3
    ]
    return {
        "site": site, "generated_at": datetime.now(timezone.utc).isoformat(), "pages_audited": len(pages),
        "issues": issues, "fixes": fixes, "rankings": rankings,
        "competitors": competitors[:10], "opportunities": opportunities,
        "counts": {s: sum(1 for i in issues if i["severity"] == s) for s in SEVERITY_ORDER},
    }


def _load(config: Any) -> dict[str, Any]:
    from state_db import _open_db

    with _open_db(config) as db:
        return db.get_kv(STORE) or {}


def record(config: Any, report: Mapping[str, Any]) -> None:
    """Keep a content-free summary of this audit for the next comparison."""
    from state_db import _open_db

    summary = {"generated_at": report["generated_at"], "counts": report["counts"],
               "rankings": {r["keyword"]: r["position"] for r in report["rankings"]}}

    def add(data: dict[str, Any]) -> None:
        history = data.setdefault("history", {}).setdefault(report["site"], [])
        history.append(summary)
        del history[:-12]

    with _open_db(config) as db:
        db.mutate_kv(STORE, add)


def compare(config: Any, report: Mapping[str, Any]) -> dict[str, Any]:
    history = (_load(config).get("history") or {}).get(report["site"]) or []
    if not history:
        return {"previous": None, "rank_changes": [], "issue_changes": {}}
    prev = history[-1]
    changes = []
    for r in report["rankings"]:
        before = prev["rankings"].get(r["keyword"], "absent")
        if before != "absent" and before != r["position"]:
            changes.append({"keyword": r["keyword"], "from": before, "to": r["position"]})
    issue_changes = {s: report["counts"].get(s, 0) - prev["counts"].get(s, 0) for s in SEVERITY_ORDER}
    return {"previous": prev["generated_at"], "rank_changes": changes, "issue_changes": issue_changes}


def render_markdown(report: Mapping[str, Any], delta: Mapping[str, Any] | None = None) -> str:
    c = report["counts"]
    lines = [f"# SEO audit — {report['site']}", "",
             f"{report['pages_audited']} pages audited: {c['high']} high, {c['medium']} medium, {c['low']} low issues.", ""]
    lines += ["## Fix first", ""]
    for i, f in enumerate(report["fixes"], 1):
        lines.append(f"{i}. **{f['action']}** — {f['severity']}, {f['pages']} page(s): {', '.join(f['urls'][:3])}")
    if not report["fixes"]:
        lines.append("No on-page issues found.")
    if report["rankings"]:
        lines += ["", "## Rankings", "", "| Keyword | Position |", "|---|---|"]
        lines += [f"| {r['keyword']} | {r['position'] or 'not in results'} |" for r in report["rankings"]]
    if report["opportunities"]:
        lines += ["", "## Keyword opportunities", ""]
        lines += [f"- {o['keyword']}: {o['why']}" for o in report["opportunities"]]
    if report["competitors"]:
        lines += ["", "## Who outranks you", ""]
        lines += [f"- {c['domain']}: top 10 for {c['keywords']} keyword(s), best #{c['best_position']}"
                  for c in report["competitors"][:5]]
    if delta and delta.get("previous"):
        lines += ["", f"## Since the last audit ({str(delta['previous'])[:10]})", ""]
        lines += [f"- {d['keyword']}: {d['from'] or '—'} → {d['to'] or '—'}" for d in delta["rank_changes"]]
        lines += [f"- {s} issues: {n:+d}" for s, n in delta["issue_changes"].items() if n]
    return "\n".join(lines) + "\n"


def issues_for(report: Mapping[str, Any], codes: Iterable[str]) -> list[dict[str, Any]]:
    wanted = set(codes)
    return [i for i in report["issues"] if i["code"] in wanted]
