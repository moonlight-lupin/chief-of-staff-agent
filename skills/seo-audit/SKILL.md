---
name: seo-audit
description: "Use when auditing a company website's search visibility: on-page issues (titles, meta descriptions, headings, broken or noindexed pages, thin or slow pages), keyword rankings, who outranks the site, and a prioritised fix list."
version: 0.1.0
author: moonlight-lupin
license: Apache-2.0
metadata:
  hermes:
    tags: [chief-of-staff, seo, website, marketing, monid]
    related_skills: [competitor-watch, prospecting, deep-research]
---

# SEO Audit

## Overview

This skill turns a crawl of the site and search results for target keywords
into a prioritised fix list, a rankings table, keyword opportunities, and the
domains that outrank the site. It is read-only: it changes nothing on the
site.

## Workflow

1. **Agree the scope:** the site domain, the pages that matter (or a crawl
   limit), and 5–20 target keywords with the country.
2. **Fetch:**
   - **Pages:** for each page, record `url`, `status`, `title`,
     `meta_description`, `h1` (a list), `canonical`, `word_count`,
     `images_missing_alt`, `load_ms` and `noindex`. Use your own fetch tool
     for the operator's own site where you can.
   - **Search results:** per keyword, the top 10 as `{"keyword", "results":
     [{"position", "url"}]}`.
   - **Paid fetching:** if this uses paid Monid endpoints (SERP, crawl),
     follow the budget steps in `prospecting` (`spend.py propose`, then
     `reserve`, then the call, then `record`). The topic is something like
     `seo-acme-nov`.
3. **Analyse:**

   ```bash
   .venv/bin/python skills/seo-audit/scripts/seo_audit_cli.py analyze --input crawl.json --markdown --record
   ```

   `--record` keeps a summary with no page content, so the next audit shows
   rank and issue changes.
4. **Report** the "Fix first" list. Each fix names the pages affected.
   Changes to the website are the user's to make. You can draft copy (titles,
   meta descriptions) for them to review.

## Checks

| Severity | Checks |
|---|---|
| High | HTTP errors, noindex, missing title |
| Medium | Slow (over 3 s), duplicate titles, missing meta description, H1 count not 1, title not 30–60 characters, under 300 words |
| Low | Meta description over 160 characters, missing canonical, images without alt text |
