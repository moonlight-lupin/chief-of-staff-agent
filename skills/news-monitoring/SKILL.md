---
name: news-monitoring
description: "Use when the user wants recurring news or topic monitoring — a headline digest on a schedule, a watch on a subject, or a cron-delivered news roundup. Handles multi-language search, source selection, and digest delivery. For a one-off written report use deep-research; for a company or person profile use entity-research."
version: 2.0.0
license: MIT
metadata:
  hermes:
    related_skills: [deep-research, entity-research, daily-briefing]
---

# News Monitoring & Digest Delivery

Set up recurring topic/news monitoring that searches the web for recent articles, compiles a headline digest, and delivers it to a messaging channel or document pipeline.

## Workflow

### Step 1 — Define Topic

Work with the user to define:
- **Subject**: what to monitor (e.g. PBSA in Hong Kong, AI regulation, self-hosting tools)
- **Geography**: which region(s) matter (e.g. Hong Kong, Singapore, global)
- **Angles**: which aspects to track — investment/deals, policy/regulation, supply/pipeline, occupancy/rental, general commentary
- **Velocity**: how fast-moving the field is — determines acceptable article age (see Step 5)
- **Cadence**: how often to run (daily, every 2–3 days, weekly, biweekly)

**Done when:** subject, geography, ≥2 angles, velocity, and cadence are confirmed.

### Step 2 — Create Search Keywords

Build search queries from the topic definition:
- **Multi-angle**: one query per angle (investment, policy, supply, etc.)
- **Multi-language**: English keywords + native language keywords for each geography
- **Time-bounded**: specify recency in the query where possible (e.g. "{current year}", "latest")
- Keep queries concise — 3-7 terms works best. Overly long queries return thin results.

**Done when:** one query per angle-language pair is written down.

### Step 3 — Select Sources

Identify which sources matter for the topic and geography:
- **English-language**: international outlets, local English press, industry publications
- **Native-language**: local press in the geography's primary language(s) — these often break news first
- **Industry-specific**: trade publications, research houses, REIT/sector-specific sites
- **Closed-ecosystem platforms** (小红书, 抖音, 微信公众号): content is available only inside their apps. Be upfront — options are manual monitoring or third-party analytics tools.

Record the source list in the topic's reference file (see Reference Files).

**Done when:** source list is recorded and closed platforms are flagged.

### Step 4 — Run Searches

**Verify the current date (mandatory).** Before any search, ground the model in the real current date. Inject:

> Today's date is {current date as "DD Month YYYY"}. When a search query needs a year or refers to "latest"/"current"/"this year", use {current year} or relative wording — derive years from the runtime date.

**Search using web_search.** Use the harness `web_search` tool. Run one call per query from Step 2. Backend comes from `config.yaml` (e.g. SearXNG → DDGS fallback); this skill does not prescribe the engine.

```
web_search(query="Hong Kong PBSA student accommodation investment {current year}", limit=10)
```

**Advanced option: SearXNG curl** when you need `categories=news`, `language`, or `time_range` that `web_search` does not expose. `format=json` is mandatory (omitting returns 403). Results include direct source URLs.

```bash
curl -sL "${SEARXNG_URL}/search?q=ENCODED_QUERY&format=json&categories=news&language=en&time_range=month"
# language=zh-Hant for Chinese; time_range=day|week|month|year
```

**RSS feeds** for must-track sources: `curl -sL "https://example.com/feed/"` and parse `<item>` for `<title>`, `<pubDate>`, `<link>`, `<description>`.

**Done when:** every query has been run and results recorded (including zero-result queries).

### Step 5 — Process Results

Filter and score search results. Authoritative pipeline order: **validate/extract → filter → deduplicate → score → sort → select**. Run these stages once, in that order.

1. **Filter** — discard thin/irrelevant/duplicate/non-article results (landing pages, stubs, <100 words, substring false-matches, video-only, login walls). Apply the velocity window from Step 1: fast-moving fields (AI, crypto, tech regulation) typically 2–3 months; slow-moving fields (real estate, infrastructure, policy) up to 6 months.
2. **Deduplicate** — match by title similarity across queries
3. **Score** — assign priority based on recency × relevance × source tier
4. **Order** — sort by score, highest first

Then **select** the top N items (default 5). Every surviving article must carry a publish date and a direct publisher URL from the extract stage.

**Done when:** every surviving article has a publish date, a direct URL, and a priority score, and count ≥1.

### Step 6 — Compare With Previous Digests

If this is a recurring digest, inject the previous run via `context_from` (see Step 9), match by **title similarity**, and remove articles already covered. High overlap: broaden scope or reduce frequency.

**Done when:** no article from the previous digest remains in the candidate set.

### Step 7 — Compile Digest

```
📰 [TOPIC] — Headline Digest
🗓 [Today's date as DD Mon YYYY]

1. [Date] — Source
   **Headline**
   1-2 line summary. Key stat or implication.
   🔗 Direct source URL

---
🔑 Key themes:
- Theme 1: brief summary
- Theme 2: brief summary
```

**Formatting checklist:** publish date on every item; headline + 1-2 lines; sort by score; 🔗 + **direct publisher URL**; 🆕 for last 7 days; max 5 items (default); key themes at the bottom.

**URL rules:** every link must resolve to the publisher's site. Google News RSS (`news.google.com/rss/articles/CBMi...`) is a protobuf proxy — resolve via SearXNG title-search or `web_search` first. Bing `apiclick.aspx` — extract the real URL from `url=`. If resolution fails: `⚠️ Direct link unavailable — search [SOURCE] for: [title]`.

**Done when:** the digest passes the formatting checklist (dates, direct URLs, max items, key themes).

### Step 8 — Deliver Digest

- **Chat channel** (Telegram, WhatsApp) — most common. Cron `deliver=telegram` or `deliver=whatsapp:<HANDLE>`. WhatsApp requires an explicit target — bare `deliver=whatsapp` fails.
- **Document generation** — output the digest as markdown for a downstream document skill.

One-off: deliver in the current chat. Recurring: set up a cron job (Step 9).

**Done when:** delivery is confirmed (message sent or document produced).

### Step 9 — Set Up Cron Job

Optional — one-off digests skip this. Create a scheduled job with the `cronjob` tool.

**Key parameters:**
- `schedule`: cron expression (e.g. `0 9 1,15 * *` biweekly 9am, `0 3 */3 * *` every 3 days)
- `prompt`: self-contained instructions (topic, queries, sources, digest format, output language, output discipline)
- `deliver`: target channel (`telegram`, `whatsapp:<HANDLE>`, etc.)
- `context_from`: job IDs whose previous output is injected for dedup (typically the job's own ID)
- `enabled_toolsets`: restrict to `["web", "terminal"]` to reduce token overhead

See `references/cron-digest-template.md` for the full prompt template.

**Dedup:** set `context_from: ["<job_id>"]` and add to the prompt: *"Review the previous digest output (provided as context). Skip any article that already appeared — match by title similarity."*

**Frequency:** high-volume daily or every 2-3 days; medium weekly; niche biweekly (1st and 15th). Confirm with a dry-run or first-run result.

**Done when:** the cron job is created with job ID, schedule, target, and a dry-run or first-run result.

## Pitfalls

- **Output duplication — digest rendered twice.** Some models write the digest during "thinking" then write it again. Fix: "OUTPUT DISCIPLINE: Output EXACTLY ONE copy." Output starts with 📰 and ends with the last theme bullet.
- **Output language drift.** Native-language search queries can pull output into that language. Specify output language at both top and bottom of the prompt.
- **"Must-include" RSS sources crowded out by discovery results.** Use quotas (e.g. 5 discovery + 5 RSS) or guarantee minimum slots per must-include source.
- **WhatsApp cron delivery requires explicit target.** `deliver=whatsapp` fails. Use `deliver=whatsapp:<HANDLE>` or an explicit chat ID.

## Reference Files

- `references/cron-digest-template.md` — Full cron prompt template. Load when creating a recurring digest job.
- `references/web-search-fallback.md` — Search backend dispatch (SearXNG → DDGS), fallback patch, env troubleshooting. Load when debugging search.
- `references/pbsa-hk-search-config.md` — PBSA Hong Kong queries, source tiers, themes, delivery. Load for the PBSA HK scan.
- `references/selfhosting-digest-config.md` — Self-hosting digest queries, RSS must-track sources, delivery. Load for the Self-Hosting weekly digest.
