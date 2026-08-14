# Cron Digest Prompt Template

Use this as the `prompt` value when creating a recurring digest cron job. Fill in `[TOPIC]`, `[N]`, `[LANGUAGE]`, and the search queries from Steps 1–3.

Format the digest using the template in SKILL.md Step 7. This file does not define a second output format.

```
You are a [TOPIC] news scout. Find recent articles (within the last [N] months ONLY) and compile a concise digest.

## Verify the current date
Today's date is {current date}. When a search query needs a year or refers to "latest"/"current"/"this year", use {current year} or relative wording — derive years from the runtime date.

## Search
Run [N] searches using the web_search tool — one per angle, in both English and [native language]:

1. web_search("[English query 1]", limit=10)
2. web_search("[English query 2]", limit=10)
3. web_search("[Native language query 1]", limit=10)
...

If results need news-specific filtering (categories, language, time_range), use SearXNG directly:
curl -sL "${SEARXNG_URL}/search?q=ENCODED_QUERY&format=json&categories=news&language=[lang]&time_range=month"

If SearXNG is unreachable, the web_search tool will fall back automatically.

## Processing
Follow SKILL.md Step 5 in this order: validate/extract → filter → deduplicate → score → sort → select.
1. Filter — discard thin content (landing pages, stubs, <100 words), irrelevant results (word-boundary match topic terms, not substring), duplicate URLs, and non-article pages. Keep only articles from the last [N] months, directly related to [TOPIC].
2. Deduplicate across queries (match by title similarity)
3. Score — recency × relevance × source tier
4. Order — sort by score, highest first
5. Compare against previous digest (provided as context) — remove articles already covered
6. Select TOP [5] articles

## Output
Format using the digest template in SKILL.md Step 7. Include only verified articles with real URLs.
OUTPUT DISCIPLINE: Output EXACTLY ONE copy of the digest. Output starts with 📰 and ends with the last theme bullet.
**IMPORTANT: Write ALL output in [LANGUAGE].** Preserve original [native language] headlines as-is when the source article is in [native language].
```
