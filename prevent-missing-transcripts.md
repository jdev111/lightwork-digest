# Plan: Prevent Missing Granola Transcripts

## The Problem

Customer calls happen but Granola doesn't capture them. This week: 1 out of 10 real calls (excluding cancels/no-shows) had no Granola doc at all.

**Root cause for this week's gap:** The Matthew Petre call (Feb 6) was run by Johnny. No Granola doc was created, meaning Granola either wasn't running on Johnny's machine or didn't pick up the calendar event.

## Why Transcripts Go Missing

1. **Granola app not running** on the team member's machine when the call starts
2. **Granola not connected** to the right Google Calendar
3. **Call happens on a different device** (phone, iPad) than the one with Granola
4. **Calendar event missing or mismatched** so Granola doesn't auto-detect the meeting
5. **Manual join** (someone shares a Zoom/Meet link in Slack instead of using the calendar invite)

## Fixes

### 1. Granola Auto-Launch on Login (5 min per person)

Make sure Granola starts automatically when each team member logs into their Mac. No one should need to remember to open it.

- Open **System Settings > General > Login Items**
- Add Granola to the list
- Verify: restart the Mac and confirm Granola appears in the menu bar

**Who:** Jay, Johnny, Dom

### 2. Verify Calendar Connections (5 min per person)

Each team member should confirm Granola is connected to their Google Calendar.

- Open Granola > Settings > Calendar
- Confirm the correct Google account is connected (jay@, johnny@, dom@lightworkhome.com)
- Make sure "Auto-record meetings" is ON
- Make sure "Record external meetings" is ON (not just internal)

**Who:** Jay, Johnny, Dom

### 3. Weekly Missing Transcripts Email (already built)

The `missing_transcripts_report.py` script runs every Monday at 8am and emails Jay a report of every call from the past week, flagging which ones are missing from Granola.

This catches gaps within a week so they don't pile up.

**Status:** Built and ready to schedule.

### 4. Team Rule: Always Join Calls from the Mac

If someone takes a call from their phone or iPad, Granola won't capture it. Simple rule:

> "Always join customer calls from your Mac so Granola picks it up. If you have to take it from your phone, manually add notes in Granola after."

Add this to the team's call SOP or pin it in Slack.

### 5. Spot-Check After Each Call (optional, low effort)

After finishing a customer call, glance at the Granola menu bar icon. If it shows the meeting was captured, you're good. If not, open Granola and manually add notes while the call is fresh.

This takes 5 seconds and catches issues same-day instead of waiting for the weekly report.

## Immediate Action Items

- [ ] Johnny: Check that Granola is set to auto-launch and calendar is connected (this was the gap this week)
- [ ] Dom: Same check
- [ ] Jay: Same check
- [ ] Jay: Install the weekly missing transcripts report (`launchctl load` the plist)
- [ ] Pin the "always join from Mac" rule in team Slack
