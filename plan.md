# Lightwork Digest Plan (Messages + Workflow)

## Current Goal
1. Get daily reminders for who to follow up with, per owner.
2. Make follow-ups good and as relevant as possible to the transcript, with minimal manual work.

## Scope (Near-Term)
- Keep the system read-only for Close.
- Generate drafts and a clean daily view.
- Prioritize transcript correctness and message quality gates before any email automation.

## Phase 1: Reliable Inputs
1. Make Granola MCP the default transcript source (`GRANOLA_MCP_ENABLE=1`), with Sheet/local cache as fallback.
2. Add a "Missing Transcripts" report for due leads:
   - Close lead name + link
   - Close meeting title/date + attendee email(s)
   - MCP match status (matched/unmatched) + MCP meeting id
   - Transcript status (present/missing)
3. Harden meeting matching:
   - Prefer attendee email overlap, then title similarity, then date proximity.
   - If multiple candidates, pick highest score and closest date.

## Phase 2: Reminder Experience (No Email Yet)
1. Owner-only view:
   - Generate a top navigation bar with tabs: `Jay`, `Johnny`, `Dom`, `All`.
   - Default to `All`, but each owner tab shows only their leads due/overdue.
2. Separate trackers:
   - `Due/Overdue` list (action list).
   - `Pipeline Tracker` list (context).
3. Add a "Today" summary block per owner:
   - Count due today
   - Count overdue
   - Count missing transcript

## Phase 3: Draft Quality (Transcript-Driven)
1. Topic extraction and gating:
   - Detect the top topics from transcript (EMF, mold, air, water, lighting/circadian, sleep, pregnancy, baby monitor, etc.).
   - Only allow tips that match detected topics.
   - If no transcript: no tip, only resource or short check-in.
2. Approved links only:
   - Allowed links are derived from reference docs (sales scripts and examples).
   - No other links allowed.
3. "No assumptions" guardrails:
   - No implied booking unless transcript confirms.
   - No "today/yesterday" unless call was within 2 days.
   - No generic filler phrases (just checking in, circling back, touching base).
4. Anti-slop lint and auto-rewrite:
   - Enforce length caps by FU number.
   - Enforce simple sentence caps.
   - Rewrite draft up to 2 times if it fails lint.
5. Repetition control across FU1-FU7:
   - Track used resources and tip buckets per lead.
   - Prevent repeats until buckets are exhausted.

## Phase 4: Special Tracks
1. No-show cadence:
   - Include Close meetings with `canceled` / `declined-by-lead` in lookback.
   - Split into a separate "No-show" section with different copy rules and a different cadence.
2. Partner or physician filtering:
   - Apply your existing physician/clinic filters upstream.
   - Add a visible tag in the digest when a lead matches those categories (for routing and tone).

## Phase 5: Automation (After Everything Above Is Solid)
1. Daily schedule:
   - Use the existing LaunchAgent `.plist` to generate and open the digest at a consistent time.
2. Email delivery:
   - One email per owner with only their leads.
   - Safety: recipient allowlist + dry-run mode + send only if there are due/overdue leads.

## Definition Of Done
- Daily digest loads fast.
- Each owner can instantly see who to follow up with.
- Missing transcripts are obvious and actionable.
- Drafts are short, specific, and transcript-driven.
- Drafts never contain unapproved links or implied booking language.

## 10 Flow + Design Ideas (Accessibility)
1. Add a sticky top header with `Owner tabs`, `Due`, `Overdue`, `Missing transcript`, and `Last run time`.
2. Add quick filters as pill buttons: `Due today`, `Overdue`, `No transcript`, `Nurture`, `No-show`.
3. Add a compact "Action list" at the very top:
   - Only due/overdue leads with 1-line context and a "Copy draft" button.
4. Add a "Copy" and "Copy + Subject" button for each draft for quick paste into Gmail.
5. Add a "Transcript status" icon next to the lead name with a hover tooltip showing source: `MCP`, `Sheet`, `Local`, `None`.
6. Make the RHS reasoning collapsible:
   - Default collapsed for speed; expand on click when needed.
7. Add lead context chips below the name:
   - `City`, `Budget`, `Source`, `Days since call`, `FU type`.
8. Add an "Error banner" section:
   - MCP failures, missing keys, rate limit warnings, and transcript mismatch count.
9. Add a "Next best action" line under each draft:
   - e.g. "Send FU2" or "Fix transcript" or "No-show follow-up".
10. Improve typography hierarchy:
   - Larger lead name, smaller metadata, consistent spacing, and a single accent color per owner.

---

## Next Task: Categorize Pipeline Tracker into Call Completed vs No-Show

### What
Split the pipeline tracker table rows into two labeled sub-groups per owner:
1. **Calls Completed** - leads whose call actually happened
2. **No-Shows** - leads whose meeting was canceled/no-show

### Where
`post_call_digest.py`, function `build_tracker_view()` (line ~2297)

### How
Inside the `for owner in ordered_owners` loop (line ~2324), split the leads list into two:

```python
completed = [e for e in leads if not e.get("no_show")]
noshows = [e for e in leads if e.get("no_show")]
```

Then render each group's rows with a sub-header row in the table:
- **"Calls Completed (X)"** row in blue before completed leads
- **"No-Shows (Y)"** row in red before no-show leads
- Skip the sub-header if a group is empty

### Verification
Run `python3 post_call_digest.py --no-email` and check that the pipeline tracker table shows two labeled groups per owner.

---

## Bug: Follow-ups sent but not detected (Romeo Ju, Gilbert Garza, Lance)

### Problem
Emails drafted by the digest were copied and sent by the user, but the system still shows these leads as needing follow-up. The same issue affects Romeo Ju, Gilbert Garza, and Lance.

### Root Cause (likely)
`get_followup_history()` (line ~1079) counts follow-ups by querying **Close.com's outgoing email API** (`/activity/email/` with `direction == "outgoing"`). If the user copies the draft and sends it from Gmail instead of through Close.com, the email only shows up in Close.com if:
1. The sender's Gmail is connected to Close.com via email sync
2. Close.com's sync has had time to pick it up (can take minutes to hours)

### Investigation Steps
1. Check if the team's Gmail accounts are synced to Close.com (Settings > Connected Accounts)
2. Run `python3 post_call_digest.py --debug-lead "Romeo"` to see what outgoing emails Close.com has for this lead
3. Check Close.com manually: open Romeo Ju's lead page and look for the sent email in the activity feed

### Root Cause (confirmed)
The scheduling thread filter (`get_followup_history`, line ~1130) was skipping ALL emails in booking threads, including follow-up replies. The team replies to the cal.com booking confirmation with their actual follow-up, so these were never counted.

### Fix (applied)
Changed the filter to only skip the **original** booking email (no `Re:`/`Fwd:` prefix). Replies in booking threads now count as follow-ups.

### Impact
Multiple leads were affected (not just Romeo). After the fix:
- Romeo Ju: FU 1 -> FU 2
- Avital Ferd: FU 1 -> FU 2
- Asif Nazerally: FU 1 -> FU 2
- Lance Loveday: FU 1 -> FU 2
- Jonathan Cronstedt: FU 1 -> FU 4
- Ryan Moran: FU 1 -> FU 2
- Eduardo Serrano: FU 1 -> FU 2
