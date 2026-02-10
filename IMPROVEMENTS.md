# Follow-Up Tracker: Improvements

## Problem 1: Email Overcounting (Critical)

The system counts ALL outgoing emails after the first call as "follow-ups." This includes:
- Automated calendar invite replies ("Re: Lightwork Home Health test call...")
- Scheduling confirmations
- Replies to lead questions
- Internal Close.com system emails

**Result:** Jack Smith shows 23/7, Amnon shows 14/7. The system thinks they're done when they may have received zero actual follow-up emails.

**Fix options (pick one):**

A) **Filter by subject line** - Exclude emails with subjects starting with "Re:" or containing "test call", "calendar", "invite." Only count emails that look like fresh outreach. Cheapest to implement but fragile.

B) **Tag-based tracking** - Add a Close.com custom field (e.g., "FU Count") that gets updated when Jay actually sends a follow-up. The digest would read this field instead of counting emails. Most accurate, but requires manual discipline.

C) **Local state file** - Track follow-up progress in a JSON file (e.g., `fu_state.json`). When Jay confirms a follow-up was sent, mark it. The system reads from this file instead of counting emails. No Close.com overhead, fast, but lives outside CRM.

**Recommendation:** Option A as a quick win now, option B or C as the long-term solution.

---

## Problem 2: First-Run Backlog (High Priority)

39 leads are "due" right now, most overdue by 10-39 days. These are leads from before the system existed. Generating 39 drafts every day until the backlog clears is noisy and slow.

**Fix:** Add a "backlog mode" for the first run:
- Cap the digest at 5-8 leads per day, prioritizing the most recent/highest value
- OR add a `--seed` flag that marks all existing leads as "caught up" to start fresh
- OR let Jay manually set a starting point: "only track leads with calls after [date]"

---

## Problem 3: Speed (Medium Priority)

The full run takes ~3 minutes just for status checks (62 leads x 0.25s rate limit + API response time), plus Claude generation time for each due lead. Total runtime could be 10-15 minutes.

**Fix:** Cache follow-up status in a local JSON file:
```
{
  "lead_abc": {"fu_done": 3, "last_checked": "2026-02-07", "first_call": "2026-01-15"},
  ...
}
```
- Only re-check a lead's email count if it wasn't checked today
- Cuts API calls from ~62/day to ~5-10/day (only new or recently due leads)
- Reduces runtime from 3 min to under 30 seconds

---

## Problem 4: Stale Cadence for Old Overdue Leads (Medium Priority)

JP Newman is overdue by 36 days for FU2 (a "second value drop"). But sending a Day 3 style email 39 days after the call doesn't make sense. The cadence tone should adapt.

**Fix:** If a lead is overdue by more than 2x the gap to the next touch, skip to a "re-engagement" template instead:
- "Hey [name], been a little while since we chatted. Wanted to share [resource]..."
- Warmer, acknowledges the gap, doesn't pretend it's Day 3

---

## Problem 5: No Tracking of What Was Actually Sent (Medium Priority)

The system generates drafts, but it has no idea whether Jay sent them, edited them, or ignored them. The next day it just re-counts outgoing emails.

**Fix:** After the digest is sent, save the due leads and their FU numbers to `fu_state.json`. Then provide a simple way for Jay to mark "sent" (reply to the digest email, click a link, or just trust that if a new outgoing email appears in Close.com the next day, it was the follow-up).

---

## Problem 6: Duplicate Leads (Low Priority)

"Maya Castle" appeared twice in the output. This means two separate Close.com lead records exist for the same person.

**Fix:** Deduplicate by primary email address when building the customer leads list. If two lead_ids share the same contact email, merge them (use the one with more activity).

---

## Suggested Implementation Order

1. **Email overcounting fix** (Problem 1) - without this, all FU numbers are wrong
2. **First-run backlog cap** (Problem 2) - otherwise Jay gets 39 drafts tomorrow
3. **Local state cache** (Problem 3) - makes the system fast enough to feel responsive
4. **Stale cadence adaptation** (Problem 4) - improves quality of overdue drafts
5. **Send tracking** (Problem 5) - closes the loop
6. **Deduplication** (Problem 6) - nice to have
