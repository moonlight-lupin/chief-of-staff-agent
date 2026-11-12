---
name: competitor-watch
description: "Use when tracking competitors' websites over time: new or removed pages, title and content changes, and price changes on pricing pages, compared against the last saved snapshot."
version: 0.1.0
author: moonlight-lupin
license: Apache-2.0
metadata:
  hermes:
    tags: [chief-of-staff, competitors, pricing, monitoring, monid]
    related_skills: [seo-audit, news-monitoring, entity-research]
---

# Competitor Watch

## Overview

This skill compares a fresh snapshot of competitors' key pages with the last
saved one, and reports new pages, removed pages, title changes, content
changes and price changes. Only titles, a content hash and the prices found
are stored. Page text is never kept.

## Workflow

1. **Keep a short watch list** of competitors and pages (pricing, product,
   about, careers), agreed with the user.
2. **Capture** each page as `{"url", "title", "text"}`, where `text` is a
   plain-text excerpt of the main content. Wrap them as `{"captured_at": "…",
   "competitors": {"rival.example": {"pages": […]}}}`. Paid scraping goes
   through a spend envelope, as in `prospecting`.
3. **Compare:**

   ```bash
   .venv/bin/python skills/competitor-watch/scripts/competitor_watch_cli.py diff --input snapshot.json --save
   ```

   The first run sets the baseline. `--save` makes this snapshot the new
   baseline; leave it off for a trial comparison.
4. **Report** price changes first, then new pages, then title and content
   changes. Suggest follow-ups such as a pricing review or a `deep-research`
   brief, but do not act on them.

Scheduled weekly runs fit well. Use one envelope per month (topic e.g.
`competitor-watch-2026-11`), so the budget stays bounded.
