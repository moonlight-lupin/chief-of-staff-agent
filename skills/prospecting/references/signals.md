# Buying signals

A signal is a dated, sourced fact that suggests an account is in market *now*.
It is not a firmographic trait. "Is a payments company" is fit, and "is hiring
a Head of FinOps" is a signal.

## Vocabulary and weight

| `type` | Weight | Typical evidence | Good hook for |
|---|---|---|---|
| `hiring` | 0.95 | Careers page, ATS posting | The function they're building |
| `funding` | 0.95 | Press release, the company's own site | The pressure the raise creates |
| `leadership_change` | 0.90 | Announcement, the company's site | The new leader's first 90 days |
| `acquisition` | 0.90 | Press release | Integration and consolidation |
| `expansion` | 0.75 | New office, new market, new licence | Setting up in the new place |
| `product_launch` | 0.75 | Launch post | Go-to-market load |
| `tech_change` | 0.70 | Tool adopted or dropped | The gap the change leaves |
| `tech_gap` | 0.60 | Something clearly absent | The cost of the gap |
| `partnership` | 0.55 | Joint announcement | What the partnership needs |
| `tech_present` | 0.40 | Tool in their stack | Only if it really matters to them |
| `announcement` | 0.25 | Generic news | Weak; avoid as an opener |

**Strength** = weight × recency.

| Age of the event | Recency |
|---|---|
| 0–14 days | 1.0 |
| 15–30 days | 0.7 |
| 31–60 days | 0.4 |
| 61–90 days | 0.2 |
| Over 90 days | 0, unusable |

A signal under 0.7 confidence is never used. A strength of 0.5 or more counts
as a trigger.

## Recording rules

- `observed_at` is when the **event** happened (posting date, announcement
  date), not when you found it.
- `attribution` options:
  - `named`: the source names the company in its headline or opening line.
  - `inferred`: the company is named only further into the text.
  - `industry`: sector-wide news. It is refused.
- When sources disagree, for example on funding, prefer the dated event over a
  firmographic field. Note the gap.
- Record "looked, found nothing" in the wiki brief, so "absent" is not mistaken
  for "not checked".

## Hook rules

- **Hiring:**
  - Cite the role *as posted*.
  - Never guess salary, headcount or why the seat is open.
  - Past about 60 days, talk about the function, not "your new opening".
  - Use one role, not a list.
- **Funding:** name the pressure the raise creates (hiring, reporting,
  expansion). Don't just say congratulations.
- **Leadership change:** speak to the new person's mandate. Don't speculate
  about why their predecessor left.
- **Tech:** mention one tool at most, and only one that matters to them. "I
  noticed you use X" is not a reason to talk.
- **Any measurement** (a rank, a page-speed score): state exactly how and when
  you measured it, so they can check it. Measurements go stale fast.
- **Fallback:** if no signal clears the bar, use an honest observation about
  the role or industry, or wait.

## Which signal suits which reader

| Reader | Signals that land |
|---|---|
| CFO / finance | Funding, acquisition, expansion, finance hiring |
| CRO / sales / marketing | Hiring in go-to-market, product launch, expansion |
| CTO / engineering | Tech change, engineering hiring, launch |
| CEO / founder | Any strong signal |
| Ops / COO | Expansion, hiring surge, acquisition |
