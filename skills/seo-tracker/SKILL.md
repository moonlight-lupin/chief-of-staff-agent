---
name: seo-tracker
description: "Use when the user wants the company website's search performance looked after over time: which SEO work to run (routing to the OpenSEO skills when OpenSEO is connected), keeping paid SEO data within an approved budget, and tracking rankings and site issues between audits. Falls back to built-in on-page checks when OpenSEO is not connected."
version: 0.2.0
author: moonlight-lupin
license: Apache-2.0
metadata:
  hermes:
    tags: [chief-of-staff, seo, website, marketing, openseo, monid]
    related_skills: [competitor-watch, prospecting, deep-research, note-taker]
---

# SEO Tracker

## Overview

The analysis itself belongs to **OpenSEO** ([every-app/open-seo], MIT): its
skills crawl the site, research keywords, compare competitors and write the
report. This skill adds what a chief of staff needs on top:

1. **Routing:** pick the right OpenSEO skill for what the user asked.
2. **Budget:** OpenSEO's live data (SERPs, keyword research, Lighthouse) uses
   credits. Spend it only against an approved spend envelope.
3. **Tracking:** record rankings and issue counts after each audit, so the
   next audit, the weekly review and the briefing can show what moved.
4. **Fallback:** built-in on-page checks when OpenSEO is not connected.

Website changes are always the user's to make. Never edit the site.

[every-app/open-seo]: https://github.com/every-app/open-seo

## Is OpenSEO connected?

Call OpenSEO's `whoami` tool. If it answers, OpenSEO is connected and it also
shows the credits left. If the OpenSEO tools or skills are missing, tell the
user how to add them, then use the fallback below:

- **Claude plugin:** OpenSEO's Claude plugin, which bundles the MCP and the
  skills (paid Claude plan, hosted OpenSEO).
- **Other agents:** connect the OpenSEO MCP (https://openseo.so/docs/mcp), then
  `npx skills add https://github.com/every-app/open-seo/tree/main/plugins/openseo/skills`.
  Self-hosted OpenSEO needs the user's own DataForSEO API key.

## Routing (OpenSEO connected)

| The user wants… | Run OpenSEO skill |
|---|---|
| Doesn't know where to start | `seo-coach` |
| A site set up for the first time | `seo-project-setup` |
| An audit with recommendations ("what should we fix?") | `seo-audit` |
| Keywords to target / topic groups | `keyword-research`, then `keyword-clustering` |
| Who they compete with in search | `competitive-landscape`; one rival in depth: `competitor-analysis` |
| Local visibility (Google Business Profile, maps, "near me") | `local-seo` |
| Backlinks | `link-prospecting` |
| Visibility in AI answers (ChatGPT, AI Overviews) | `ai-prompt-research`, then `ai-visibility-audit` |
| A shareable report | `seo-report` (OpenSEO's audit saves through it) |

Follow the OpenSEO skill's own steps. These Chief-of-Staff rules sit on top:

- **Budget first.** Before a run that uses credits, check `whoami`, agree the
  scope with the user, and make sure an approved envelope covers it (provider
  `openseo`, topic such as `seo-acme-2026-11`). Reserve once per OpenSEO
  skill run, sized to the expected cost:
  `spend.py reserve --envelope <id> --topic seo-acme-2026-11 --estimate <cost> --endpoint seo-audit`.
  Afterwards, record the actual cost from the change in `whoami` credits:
  `spend.py record --reservation <id> --actual <cost>`. If `reserve`
  refuses, stop and tell the user. The OpenSEO skills also say to respect an
  explicit budget and to state what it ruled out; do that.
- **Record costs in the spend currency.** If OpenSEO reports credits rather
  than money, convert using the user's plan price and say which rate you
  used. If the rate is unknown, ask rather than guess.
- **Outreach from `link-prospecting`** (emails to site owners) follows the
  `prospecting` rules: one approved `mail.send` per recipient, an opt-out
  line, and `prospects.py opt-out` when someone asks.
- **Keep the deliverable.** Give the user OpenSEO's report. If they want it
  in the knowledge base, save the summary (the next SEO move and the
  recommendations) through `note-taker`, or publish it with `notion-wiki`.

## Tracking (after any audit)

Normalise what the audit read into the tracker's input and record it:

- `pages`: from OpenSEO's audit pages and issues (`get_audit_pages`,
  `get_audit_issues`). Fields: `url`, `status`, `title`, `meta_description`,
  `h1` (a list), `canonical`, `word_count`, `images_missing_alt`, `load_ms`,
  `noindex`. Leave out any field you don't have.
- `serp`: from `get_serp_results` or `get_ranked_keywords`, for the agreed
  target keywords. Each entry is `{"keyword", "results": [{"position",
  "url"}]}`.

```bash
.venv/bin/python skills/seo-tracker/scripts/seo_tracker.py analyze --input crawl.json --record --markdown
```

`--record` keeps only rankings and issue counts, no page content. The next
run reports rank changes ("sme accounting singapore: 4 → 2") and the change
in issues at each severity. Use this in the weekly review. Use OpenSEO's
report for the recommendations themselves.

## Fallback (OpenSEO not connected)

Collect the same `pages` and `serp` input with your own fetch tools. Use
Monid SERP or crawl endpoints only under a spend envelope (provider `monid`).
Then run the command above. The tracker's built-in checks and its "Fix
first" list are a basic audit:

| Severity | Checks |
|---|---|
| High | HTTP errors, noindex, missing title |
| Medium | Slow (over 3 s), duplicate titles, missing meta description, H1 count not 1, title not 30–60 characters, under 300 words |
| Low | Meta description over 160 characters, missing canonical, images without alt text |

Say plainly that this is the basic audit, and suggest connecting OpenSEO for
keyword demand, backlinks and competitor depth.
