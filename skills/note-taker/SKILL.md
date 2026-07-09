---
name: note-taker
description: Maintain a Chief of Staff second-brain wiki for business and personal knowledge using a three-layer markdown architecture with OKF v0.1 frontmatter.
version: 0.1.0
author: Chief of Staff Project
license: MIT
metadata:
  hermes:
    tags: [wiki, notes, knowledge-base, okf, chief-of-staff]
    related_skills: [meeting-prep, deep-research, entity-research, travel-itinerary]
---

# Note Taker

## Overview

Note Taker is the Chief of Staff second brain. It captures and organizes all durable knowledge — business and personal — into a plain Markdown wiki that compounds over time. It uses a three-layer architecture inspired by LLM wiki patterns, but this skill is self-contained and operational for the Chief of Staff plugin.

The wiki is not a database and not a vector store. It is a directory of Markdown files with YAML frontmatter, raw source preservation, cross-links, and a schema that keeps the knowledge base navigable by humans and agents.

## When to Use

Use this skill when the user wants to:

- Capture meeting notes, decisions, lessons, articles, research, trip learnings, or personal insights.
- Ask a question that may be answered from the wiki.
- Initialize or lint the second-brain wiki.
- Ingest outputs from Meeting Prep, Deep Research, Entity Research, or Travel Itinerary.

Scope is **all stuff**: business and personal knowledge share one wiki and are separated by tags, not by separate databases.

## Wiki Location

Read the path from `company.yaml`:

```yaml
paths:
  wiki_path: ~/.hermes/projects/{company}/wiki/
```

Default if absent:

```text
~/.hermes/projects/{company}/wiki/
```

Resolve `~` and create the path during onboarding if it does not exist.

## Architecture: Three Layers + Purpose

```text
wiki/
├── SCHEMA.md           # Conventions, structure rules, domain config
├── purpose.md          # Goals, key questions, scope — read on every ingest/query
├── index.md            # Content catalog with one-line summaries
├── overview.md         # Auto-regenerated global summary
├── log.md              # Chronological action log (append-only)
├── raw/                # Layer 1: Immutable source material
│   ├── articles/
│   ├── papers/
│   ├── transcripts/
│   └── assets/
├── entities/           # Layer 2: Entity pages (people, orgs, products, clients)
├── concepts/           # Layer 2: Concept/topic pages
├── comparisons/        # Layer 2: Side-by-side analyses
└── queries/            # Layer 2: Filed query results worth keeping
```

- **Layer 1 — Raw:** immutable source material. Once captured, do not edit except to add missing frontmatter during initial ingestion.
- **Layer 2 — Curated Wiki:** agent-maintained entity, concept, comparison, and query pages.
- **Layer 3 — Schema:** `SCHEMA.md` and `purpose.md` govern structure, taxonomy, and intent.

Always read `purpose.md`, `SCHEMA.md`, `index.md`, and recent `log.md` before ingesting or answering from an existing wiki.

## OKF v0.1 Conformance

Every curated `.md` page must have parseable YAML frontmatter with a non-empty `type` field. This aligns the wiki with Open Knowledge Format v0.1 expectations while allowing Chief of Staff extensions.

Curated page frontmatter:

```yaml
---
type: entity | concept | comparison | query | note | decision
title: Page Title
description: One-line summary
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags: [clients, contracts]
sources: [raw/transcripts/acme-kickoff-2026-07-09.md]
confidence: high | medium | low
contested: false
---
```

Raw source frontmatter:

```yaml
---
type: raw
source_url: https://example.com/source
source_kind: article | paper | transcript | email | report | travel
ingested: YYYY-MM-DD
sha256: <hash-of-body>
---
```

Root `index.md` must declare:

```yaml
---
type: index
okf_version: "0.1"
updated: YYYY-MM-DD
---
```

Reserved files `purpose.md`, `SCHEMA.md`, `overview.md`, and `log.md` should also include frontmatter with `type` values such as `purpose`, `schema`, `overview`, and `log`.

## Onboarding Seed

During onboarding, generate `purpose.md` and `SCHEMA.md` from:

- `company.name`
- `company.business_type`
- `company.industry` if present
- user-stated goals and interests
- default taxonomy for the business type

### `purpose.md` Seed

```markdown
---
type: purpose
title: Wiki Purpose
updated: YYYY-MM-DD
---

# Wiki Purpose

## Goal
Maintain a compounding second brain for {company.name}: client work, decisions, research, operations, personal learning, travel, and reusable insights.

## Key Questions
- What needs follow-up with clients, partners, vendors, or personal commitments?
- What have we learned that should change future decisions?
- What facts, contacts, and context should be easy to retrieve before meetings?
- Which recurring problems deserve a reusable playbook?

## Scope
- In scope: business, clients, deals, documents, research, meetings, travel, personal learning, decisions, and reference notes.
- Out of scope: transient reminders better stored as to-dos unless they contain reusable context.

## Evolving Thesis
This wiki should reduce repeated context gathering and preserve judgment over time.
```

### `SCHEMA.md` Seed for `professional_services`

```markdown
---
type: schema
title: Wiki Schema
updated: YYYY-MM-DD
---

# Wiki Schema

## Domain
Professional services business and personal second brain.

## Taxonomy
Business:
- clients
- deals
- proposals
- contracts
- invoices
- deliverables
- vendors
- operations
- finance
- regulation
- market
- competitors

Entities:
- people
- organizations
- products
- services

Personal:
- books
- courses
- learning
- insights
- health
- family
- travel

Meta:
- comparison
- decision
- reference
- meeting
- open-question
```

## Operation 1 — Ingest (Two-Step Chain-of-Thought)

Use a two-phase ingest to avoid dumping notes without synthesis.

### Step 1: Capture and Analyze

1. Capture the source into `raw/`:
   - meeting notes → `raw/transcripts/`
   - research reports/articles → `raw/articles/`
   - PDFs/papers → `raw/papers/`
   - trip learnings → `raw/transcripts/` or `raw/articles/` depending on format
   - supporting images/files → `raw/assets/`
2. Add raw frontmatter with `type: raw`, `source_kind`, `ingested`, and `sha256` of the body.
3. Read `purpose.md` and `SCHEMA.md`.
4. Search `index.md` and existing pages for mentioned entities/concepts.
5. Produce structured analysis:
   - key entities,
   - key concepts,
   - decisions or action-relevant facts,
   - contradictions or changed facts,
   - recommended new pages vs updates.

In interactive mode, share this analysis before writing many pages. In cron/automation mode, proceed if the source is routine and low risk.

### Step 2: Generate and Link

1. Create or update entity/concept/comparison/query pages only when they meet threshold:
   - central to the source, or
   - appears across multiple sources, or
   - likely useful for future meeting prep/research.
2. Add or update YAML frontmatter with `type`, `title`, `description`, `created`, `updated`, `tags`, and `sources`.
3. Add at least two relevant `[[wikilinks]]` where possible. If there are not two real links, add one and note that the page is a connection gap.
4. Update `index.md` with one-line summaries.
5. Regenerate `overview.md` with current themes, recent changes, and page counts.
6. Append to `log.md` with action, source, and files changed.

Completion criterion: raw source is preserved, all curated changes are indexed, and the log lists every created/updated file.

## Operation 2 — Query (4-Signal Relevance)

When answering from the wiki:

1. Read `purpose.md` and `index.md`.
2. Identify seed pages by keyword, entity name, tag, and source references.
3. Expand and rank candidate pages using four signals:

| Signal | Weight | Use |
|---|---:|---|
| Direct link | ×3.0 | Pages linked from seed pages via `[[wikilinks]]`. |
| Source overlap | ×4.0 | Pages sharing raw sources in frontmatter. |
| Adamic-Adar | ×1.5 | Pages sharing uncommon neighbors; useful for hidden links. |
| Type affinity | ×1.0 | Same type as the question target, e.g. entity-to-entity. |

4. Read top-ranked pages, not the whole wiki unless it is small.
5. Synthesize an answer with citations to wiki pages: `[[page-name]]`.
6. If the answer is a durable synthesis, file it under `queries/` or `comparisons/` and update index/log.

Completion criterion: answer cites the pages used and distinguishes wiki-backed facts from assumptions.

## Operation 3 — Lint

Lint checks the wiki for structural decay:

- Missing or invalid frontmatter.
- Missing required `type` field.
- Broken `[[wikilinks]]`.
- Orphan pages with no inbound links.
- Pages absent from `index.md`.
- Tags not listed in `SCHEMA.md` taxonomy.
- Raw source hash drift.
- Pages over ~200 lines that should be split.
- `confidence: low` or `contested: true` pages needing review.
- Duplicate or near-duplicate pages.
- Stale pages not updated in 90+ days when related sources changed.

Report findings by severity and append a lint entry to `log.md`.

## Integrations

| Source Skill | Note Taker Action |
|---|---|
| Meeting Prep | Ingest final meeting notes/action summaries into `raw/transcripts/`; update people, client, and concept pages. |
| Deep Research | Ingest reports into `raw/articles/`; create concepts, comparisons, and market/industry pages. |
| Entity Research | Ingest dossiers as entity source material; update organization/person pages. |
| Travel Itinerary | Ingest trip learnings, vendor notes, personal preferences, and recurring travel lessons. |
| Daily Briefing | Can surface new raw items waiting for ingest or note pages recently updated. |

## File Naming

- Use lowercase hyphenated slugs: `acme-corp.md`, `proposal-follow-up-pattern.md`.
- Prefix raw files with descriptive source/date: `acme-kickoff-2026-07-09.md`.
- Avoid generic titles like `notes.md` or `meeting.md`.

## Common Pitfalls

1. **Skipping orientation.** Always read purpose/schema/index/recent log first.
2. **Creating duplicate pages.** Search before creating entity or concept pages.
3. **Dumping raw notes as curated knowledge.** Preserve raw, then synthesize into pages.
4. **No frontmatter.** OKF conformance requires `type` on every page.
5. **Tag sprawl.** Add tags to taxonomy before using them.
6. **No links.** Unlinked pages are forgotten; add real `[[wikilinks]]`.
7. **Over-filing every query.** File only durable answers worth reusing.

## Verification Checklist

- [ ] Wiki path was loaded from `company.yaml`.
- [ ] Existing wiki orientation files were read before changes.
- [ ] Raw source was preserved with hash frontmatter.
- [ ] Curated pages have YAML frontmatter with `type`.
- [ ] Index, overview, and log were updated.
- [ ] Query answers cite wiki pages.
- [ ] Lint reports actionable file paths and severity.
