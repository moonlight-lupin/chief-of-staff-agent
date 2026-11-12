# Messaging

Every message is drafted here and sent only as its own approved `mail.send`.

## First email

In this order:

1. **The finding.** One specific, checkable fact from the anchor signal.
2. **What it likely costs them.** One plain sentence.
3. **Who we are.** Only if the offer makes no sense without it.
4. **The ask.** The offer as the fix, framed as a yes/no question, for example
   "Want me to send it over?"

Constraints:

- 50–90 words. Plain text, no links, no attachments, no tracking pixels.
- Subject: 2–5 words, lower case, specific. Never "quick question".
- No product name or feature list in email 1. One question only.
- Ask for an easy yes, such as an audit, a sample or a short teardown. Don't
  open by asking for a meeting.
- Match the reader's level:
  - Executive: outcome first, with a number if it is true.
  - Manager: problem, approach, outcome.
  - Individual contributor: the friction in their day.

## Facts and claims

- Every claim traces to a stored signal (`prospects.py list --full` shows
  them). If you can't point to it, cut it.
- Name customers, use figures or offer peer proof only if they are true and
  public.
- The `reason` and `why_now` fields are internal working notes. Never paste
  either into a message.
- Close with: who you are, your company, and how to opt out ("Reply 'no' and I
  won't write again").

## Follow-ups

Three touches over about 10 days by default: day 0, day 3 to 4, day 9 to 10.
Each follow-up goes in the same thread.

- **Touch 2:** a new angle from a banked signal, or one proof point, then the
  offer again.
- **Touch 3:** a short, polite close that removes any guilt. No "just bumping
  this".

Stop at once on any reply, an opt-out, a bounce, or a reply from anyone else
at the account. The store enforces the last one through the account pause.

## Replies

| Kind | What to do |
|---|---|
| `interested` | Answer first, then one next step. Offer to hand over to a meeting. |
| `question` | Answer plainly. Don't make anything up. Give one next step. |
| `objection` | Acknowledge it, give one proof point, ask one question. No pressure. |
| `wrong_person` | One approved message asking who the right person is. |
| `not_interested` | No reply. |
| `out_of_office` | Re-time the next touch after their return date. |
| `unsubscribe` | Suppress now. No reply, except a confirmation if the law requires one. |
| `escalate` | Anger, a legal threat or a privacy-law mention. Hand it to the user. |

Automate at most the first exchange. Pricing, negotiation and commitments
belong to a human.
