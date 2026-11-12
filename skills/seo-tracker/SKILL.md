---
name: seo-tracker
description: "Use for the company's search visibility: where to start, site audits, keyword research, search competitors, local/Maps SEO, backlinks and AI-search visibility, plus tracking rankings and issues over time. SEO data comes from DataForSEO through Monid, inside an approved spend budget."
version: 0.3.0
author: moonlight-lupin
license: Apache-2.0
metadata:
  hermes:
    tags: [chief-of-staff, seo, website, marketing, dataforseo, monid]
    related_skills: [competitor-watch, prospecting, note-taker, notion-wiki, deep-research]
---

# SEO Tracker

## Overview

These workflows are adapted from OpenSEO's skills ([every-app/open-seo], MIT;
see NOTICE). This skill uses OpenSEO's approach only; it does not need the
OpenSEO app or its MCP server. **SEO data comes from DataForSEO through
Monid.** Every paid call goes through a spend envelope. Findings and the
research log live in the wiki, and rankings and issue counts are tracked over
time with `seo_tracker.py`.

The skill reads, recommends and drafts. Website, listing and Google Business
Profile changes are the user's to make. Never edit them.

[every-app/open-seo]: https://github.com/every-app/open-seo

## Data: DataForSEO through Monid

Find endpoints with `monid_discover` (for example "DataForSEO SERP Google
organic"). Before any spend, check each endpoint's input and price with
`monid_inspect`. Both are free. Endpoint names change, so discover them each
time rather than reusing a remembered name. If Monid has no DataForSEO
endpoint for a need, use another Monid endpoint that does the same job and
say which one you used. If nothing fits, tell the user and leave that part
out. Never fill a gap with guessed numbers.

| Need | DataForSEO API family (discover query) |
|---|---|
| Keyword ideas from seeds | Labs: keyword ideas / suggestions / related keywords |
| Search volume, CPC, difficulty | Keywords Data (Google Ads search volume); Labs keyword overview / difficulty |
| What a domain or page already ranks for | Labs: ranked keywords |
| Live results for a query | SERP: Google organic (live, advanced) |
| Who competes in search | Labs: SERP competitors / competitors domain |
| Domain size and traffic estimate | Labs: domain rank overview |
| Backlinks | Backlinks: summary, referring domains |
| Site crawl and issues; page speed | On-Page: task crawl, pages, summary; Lighthouse |
| Find a business; profile; reviews | Business Data: Google My Business info, Google reviews |
| Maps and local pack results | SERP: Google Maps / local finder (with location or coordinates) |
| Mentions in AI answers | AI Optimization: LLM mentions / responses |

If the user connects Google Search Console through their own connector, read
it first. It is free and measured, whereas DataForSEO traffic figures are
estimates.

**Location defaults to the company's market** (`company.jurisdiction`, e.g.
Singapore, in English), not the US. Say which location and language you
used.

## Budget rules

- **Reuse first.** If the research log for this site has a result under 30
  days old, reuse it and say so.
- **Get an envelope.** One approved envelope per piece of work (provider
  `monid`, topic such as `seo-audit-acme-2026-11`). Size it from the
  `monid_inspect` prices multiplied by the planned number of calls. If none
  exists, `spend.py propose`, then stop for approval.
- **Reserve and record.** `spend.py reserve` before every `monid_run`, and
  `spend.py record` with the actual cost afterwards. A refusal means stop and
  tell the user.
- **Confirm big fan-outs** before running them: a Maps rank grid (a 5×5 grid
  is 25 searches), many keywords × many locations, or a full crawl of a large
  site.
- **Respect the budget** and state in the report what it ruled out.

## Project context and research log

Keep one wiki page per site (`seo/<domain>.md`, via `note-taker`). It holds:
- a business overview and goal;
- the target market and location;
- business competitors and SEO competitors, kept apart;
- key pages and saved target keywords;
- a dated research log, one line per run, e.g.
  `2026-11-12 Site audit: acme.example. Verdict: <conclusion>`.

Read the page before any workflow and update it after. If the page is
missing, run **Project setup** first.

## Choosing the workflow

Pick one and ask only for the next input you need:

1. No project page → **Project setup**.
2. "What should we fix?" / general health → **Site audit**.
3. Seed topics, "what should we write about" → **Keyword research**.
4. "Who are we up against?" → **Competitive landscape**. One named rival →
   the same workflow, narrowed to that domain.
5. Maps, "near me", a physical location → **Local SEO**.
6. Links and mentions → **Backlinks**.
7. ChatGPT / AI Overviews → **AI visibility**.

## Workflows

**Project setup.** Ask for or confirm:
- what the business sells;
- its goal (leads, sales, bookings);
- the market and location;
- three to five business competitors;
- key pages.

Then write the project page. This step needs no paid data.

**Site audit.**
1. *Orient:* start an On-Page crawl. While it runs, pull the domain
   overview, backlinks summary and a ranked-keywords sample, then group the
   site's pages into families.
2. *Investigate:* for each page family that could bring buyers, compare a top
   page and a typical page with what leading results offer for the same
   query. Check status, canonicals, indexing and internal links.
3. *Shortlist* 5–10 candidates from at least three kinds of opportunity, each
   with its evidence, effort and uncertainty.
4. *Choose* one to three recommendations. Fixes for demonstrated problems
   come first; access, indexing or measurable traffic losses outrank
   everything else. Every other candidate goes in "What else we checked".
5. *Size the benefit* by naming how it would help (a new ranking, a better
   position, or more clicks). Label scenarios as hypothetical. Never invent
   conversion rates or revenue.
6. *Review:* challenge the draft yourself before writing it.

**Keyword research.**
1. Run 1–5 seeds through keyword ideas, about 150 results each.
2. Score them with volume, CPC and difficulty, plus ranked keywords if a
   domain is in scope.
3. Remove irrelevant, duplicate, brand-only and off-intent terms.
4. Check live results for ambiguous or high-potential terms.
5. Rank by business fit, intent fit and reasonable difficulty, not by volume
   alone.
6. Present a shortlist and a longer table. Save target keywords to the
   project page only after the user confirms.

**Competitive landscape.**
1. Use the saved competitors, or infer a shortlist and confirm it.
2. Build 5–10 mixed-intent queries, adding locations for local markets.
3. Check demand and recurring domains, then inspect live results.
4. Group recurring domains by type: product, publisher, marketplace,
   community or documentation.
5. Profile the top 3–5 and pull their ranked keywords.
6. Report patterns, gaps and local-pack signals.

Keep SEO competitors separate from business competitors, and don't treat
publishers as product competitors. A small query set is directional; say so.

**Local SEO.**
1. Find the business and record its place ID or CID. Always match listings
   by ID, never by name.
2. Pull local results for the main keyword and note the top 3–5.
3. Compare categories, reviews, hours, photos and claim status, and check
   that the website link goes to the right location page.
4. Pull reviews for the business and its strongest rival.
5. Run a Maps rank grid only after confirming its cost.

Category and claim problems outrank posting cadence. For chains, take a
snapshot of every location and look deeply at up to five.

**Backlinks.** Read the backlinks summary and referring domains for the site
and its top competitors, and list linking domains the competitors have that
the site lacks. Any outreach follows the `prospecting` rules: one approved
`mail.send` per recipient, an opt-out line, and `prospects.py opt-out` on
request.

**AI visibility.** Draft the prompts a buyer would ask an AI assistant,
confirm them with the user, then use the LLM mentions/responses endpoints to
see whether the brand and its competitors are mentioned or cited. Report
gaps, not certainties: AI answers vary from run to run.

## Report

Write it in Markdown, lead with the conclusion, and save it to the wiki (or
Notion via `notion-wiki`) only when the user wants it kept. Use these
sections:
1. **Your next SEO move**: one sentence.
2. **Recommendations**: one to three, each with evidence, effort and the
   expected mechanism.
3. **What else we checked**: a table.
4. **How this report was made**: the endpoints used, location and language,
   the date, the cost from the spend ledger, and caveats. Traffic and volume
   figures are estimates, and missing values are written as `unknown`.

## Tracking

After an audit or ranking check, normalise what you read into the tracker's
input and record it:
- `pages` from the On-Page items: `url`, `status`, `title`,
  `meta_description`, `h1` (a list), `canonical`, `word_count`,
  `images_missing_alt`, `load_ms`, `noindex`. Leave out what you don't have.
- `serp` from the organic results for the target keywords, as `{"keyword",
  "results": [{"position", "url"}]}`.

```bash
.venv/bin/python skills/seo-tracker/scripts/seo_tracker.py analyze --input crawl.json --record --markdown
```

`--record` keeps rankings and issue counts only, no page content. The next
run shows what moved, for the weekly review. The tracker's built-in checks
also serve as a quick health check when no paid crawl is wanted:

| Severity | Checks |
|---|---|
| High | HTTP errors, noindex, missing title |
| Medium | Slow (over 3 s), duplicate titles, missing meta description, H1 count not 1, title not 30–60 characters, under 300 words |
| Low | Meta description over 160 characters, missing canonical, images without alt text |
