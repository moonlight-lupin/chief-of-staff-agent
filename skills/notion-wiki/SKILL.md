---
name: notion-wiki
description: "Use when publishing Chief-of-Staff wiki or meeting notes (Markdown under the wiki path) to Notion pages, or republishing a note that changed. One-way: the wiki is the source; Notion receives copies."
version: 0.1.0
author: moonlight-lupin
license: Apache-2.0
metadata:
  hermes:
    tags: [chief-of-staff, notion, wiki, notes, publishing]
    related_skills: [note-taker, meeting-prep, notion-sync]
---

# Notion Wiki

## Overview

Publishes Markdown files from the plugin wiki (`paths.wiki_path`) as pages
under one Notion parent page. The first publish creates a page
(`notion.page.create`, medium risk). Later publishes of a changed file
replace that page's content (`notion.page.update`). Publishing an unchanged
file queues nothing. Files outside the wiki are refused.

## Setup

```yaml
notion:
  wiki_parent_page: <page id>
accounts:
  notion: <the Notion workspace your connector should act as>
```

## Workflow

1. Queue the files:

   ```bash
   .venv/bin/python skills/notion-wiki/scripts/notion_wiki.py publish <wiki>/meetings/2026-11-12-acme.md
   ```

   The output lists action IDs and titles, never page content.

2. The user approves each action (`review_queue.py preview` shows the title
   and target first).

3. Run `review_queue.py claim --action-id <id>`, then create or update the
   page with your Notion connector. The title and Markdown are in the claimed
   payload. Then run `review_queue.py record-execution --action-id <id>
   --status success --result-json '{"page_id": "…"}' --executor-account
   <account>`.

## Rules

- Notes can hold client details. Never paste page content into chat; refer
  to notes by title.
- Edits made in Notion to a published page are not brought back. If the user
  wants that, they edit the wiki file and republish.
