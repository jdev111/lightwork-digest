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

