# Post-Call Follow-Up Digest: Build Plan

## What It Does

A Python script runs every morning at 8am. It sends you one email with:

- Every sales call from yesterday
- Lead context from Close.com (name, budget, health spend, home size, how they heard, why reaching out)
- Call summary from Granola transcript (what was discussed, concerns raised, objections)
- A personalized follow-up email draft for each call
- Suggested follow-up timing

You read it, tweak if needed, copy/paste/send.

---

## Data Sources

| Source | What we pull | How |
|--------|-------------|-----|
| Close.com API | Yesterday's meetings tagged as "test call", "intro call", or "partner call" for ALL team members (Jay, Johnny, Dom). Lead details: name, budget, health spend, sq footage, referral source, why reaching out, heat level, meeting status | REST API (key confirmed working) |
| Granola via Zapier > Google Sheet | Meeting transcripts, notes, attendees, summary. Zapier triggers when meeting ends in Granola, saves row to a Google Sheet | Google Sheets API (read-only) |
| Claude API (Haiku for speed + cost) | Generates call summary + personalized follow-up draft | Anthropic API |
| Gmail SMTP | Sends the digest email to jay@lightworkhome.com | smtplib with existing app password |

---

## Script Flow

```
8:00 AM daily (launchd)
       |
       v
1. Pull yesterday's meetings from Close.com
   - GET /activity/meeting/?date_created__gt={yesterday}
   - Filter: title contains "test call", "intro call", or "partner call"
   - Include ALL team members (Jay, Johnny, Dom)
       |
       v
2. For each meeting, get lead details from Close.com
   - GET /lead/{lead_id}/
   - Extract: name, email, city, budget, health spend,
     sq footage, referral, why reaching out, heat level
       |
       v
3. Match meeting to Granola transcript
   - Read from Google Sheet (populated by Zapier when meetings end)
   - Match by: attendee email OR meeting title
   - Extract: full transcript text + Granola AI summary
       |
       v
4. For each call, send to Claude API:
   - Lead context (from Close)
   - Call transcript (from Granola)
   - Prompt: "Summarize this call and draft a follow-up email"
   - Returns: call summary + follow-up draft + suggested timing
       |
       v
5. Build HTML email grouped by team member:
   - JAY'S CALLS (Jay's section)
   - JOHNNY'S CALLS (Johnny's section)
   - DOM'S CALLS (Dom's section)
       |
       v
6. Send to jay@lightworkhome.com via Gmail SMTP
   Jay forwards relevant sections to Johnny/Dom
```

---

## What Claude Generates Per Call

For each call, Claude gets this context and produces:

**Input:**
- Lead name, city, home size, budget, health spend
- How they heard about Lightwork
- Why they're reaching out (from form)
- Full Granola transcript of the call

**Output:**

1. **Call Summary** (3-4 bullet points): What was discussed, their main concerns, objections/hesitations, outcome
2. **Follow-Up Email Draft**: Personalized based on what was discussed. References specific things from the call. Written in Lightwork's voice (practical, grounded, not salesy).
3. **Suggested Timing**: When to send (today, 2 days, 1 week) based on how the call went
4. **Priority**: High/Medium/Low based on budget, urgency, engagement level

---

## Files to Create

```
lightwork-digest/
  .env                          # API keys (already created)
  post_call_digest.py           # Main script (~200 lines)
  com.lightwork.digest.plist    # macOS launchd for daily 8am run
  README.md                     # Setup instructions
```

---

## Tech Stack

- Python 3 (already installed)
- No external packages needed: uses `urllib`, `json`, `smtplib`, `email` (all stdlib)
- Anthropic API called via raw HTTP (no pip install needed)
- Runs as macOS LaunchAgent (survives restarts, runs even if you're not logged in to terminal)

---

## What the Digest Email Looks Like

```
Subject: Lightwork Daily Digest - Feb 7, 2026 (5 calls)

============================================================
JAY'S CALLS (2)
============================================================

------------------------------------------------------------
Kyle Alwyn (San Francisco)
------------------------------------------------------------
Budget: $2,000-5,000 | Health Spend: <$2,000 | Home: <1,500 sqft
Source: Podcast | Status: Meeting Created

CALL SUMMARY:
- Discussed SF apartment assessment, interested in EMF + air quality
- Asked about timeline and pricing for smaller units
- Seemed ready to book but wanted to check with partner first

FOLLOW-UP DRAFT:
Hi Kyle, great speaking with you yesterday. To answer your question
about timeline, we can typically schedule SF assessments within
2 weeks. For a smaller apartment like yours, the process takes
about 3-4 hours on-site. Happy to lock in a date whenever you
and your partner are ready. Just reply here or book directly:
[cal link]

SEND: Tomorrow morning | PRIORITY: HIGH

------------------------------------------------------------
Ryan Carney (NYC)
------------------------------------------------------------
...

============================================================
JOHNNY'S CALLS (2)
============================================================

------------------------------------------------------------
Taryn Blank (Sag Harbor, NY)
------------------------------------------------------------
...

============================================================
DOM'S CALLS (1)
============================================================

------------------------------------------------------------
Matt Gleit (Menlo Park, CA)
------------------------------------------------------------
...
```

---

## Safety: This Will NEVER Email Leads

Four layers of protection:

1. **Hardcoded recipient**: The ONLY recipient is `jay@lightworkhome.com`. No variable, no config, no parameter accepts other emails.
2. **Send guard**: The send function checks the `to` address before sending. If it's not `jay@lightworkhome.com`, it throws an error and exits immediately.
3. **Read-only on Close.com**: The script only GETs data from Close. It never POSTs, PUTs, or sends through Close.
4. **No lead email addresses in the `to` field**: Lead emails only appear inside the digest body as text, never as recipients.

## Limitations / Things to Note

1. **Granola transcripts**: Delivered via Zapier to Google Sheets. If Zapier hasn't synced a meeting yet, the digest will note "No transcript available" and generate the follow-up based on Close.com data only (still useful, just less personalized).

2. **Meeting filtering**: The script filters for "test call", "intro call", and "partner call" in the meeting title. Internal meetings, hiring calls, etc. are excluded.

3. **Runs on your Mac**: Requires your Mac to be on at 8am. If it's asleep, launchd will run it when it wakes up.

## One-Time Setup Required

1. **Zapier**: Create the Granola > Google Sheets zap (5 min)
   - Trigger: Granola "Meeting note sent to Zapier"
   - Action: Google Sheets "Create Spreadsheet Row"
   - Map fields: title, date, attendees, transcript, summary
2. **Google Sheets API**: Create a service account key for read access (5 min)
3. **Install launchd plist**: One command to enable daily 8am runs

---

## Lead Tracking Dashboard (Google Sheet)

A shared Google Sheet that auto-updates daily. The team opens it, sees who needs follow-up, and checks them off.

### What the Sheet Looks Like

| Owner | Lead Name | City | Budget | Source | FU 1 | FU 2 | FU 3 | FU 4 | FU 5 | FU 6 | FU 7 | Progress | Last Activity | Days Since |
|-------|-----------|------|--------|--------|:----:|:----:|:----:|:----:|:----:|:----:|:----:|----------|---------------|-----------|
| Jay | Kyle Alwyn | SF | $2-5k | Podcast | [x] | [x] | [ ] | [ ] | [ ] | [ ] | [ ] | 2/7 | Email sent Feb 10 | 1 |
| Jay | Aaron O'Brien | -- | $2-5k | -- | [x] | [x] | [x] | [ ] | [ ] | [ ] | [ ] | 3/7 | Email sent Feb 8 | 3 |
| Johnny | Taryn Blank | Sag Harbor | $2-5k | Dr. Kachko | [x] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | 1/7 | Call completed Feb 5 | 6 |
| Dom | Matt Gleit | Menlo Park | $2-5k | Twitter | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | 0/7 | Call completed Feb 4 | 7 |

**Goal: Hit 7 follow-ups per lead.** Once all 7 are checked, the lead moves to a "Completed" tab.

### 7-Touch Value-Driven Follow-Up Cadence

Based on Jeremy Haynes' methodology: 80% pure value, 20% ask. Every email teaches something useful for their home. The value IS the follow-up. Every-other-day cadence in the early touches (converts 23% better than daily). Prospects who consume 5+ pieces of content convert at 3.4x the rate.

| Touch | Timing | Type | What | Example |
|-------|--------|------|------|---------|
| FU 1 | Day 1 | Post-call value | Personalized tip based on what they discussed on the call. Use the sales scripts: if they mentioned sleep, send the AC electrical field tip. If they mentioned a baby, send the baby monitor shield tip. Written INTO the email, not just a link. | "You mentioned sleep was a concern, so here's one quick thing you can do tonight..." |
| FU 2 | Day 3 | Second value drop | Different topic-specific tip from the call, OR the example report with context on why it's relevant to their situation. Still 100% value, zero ask. | "Also wanted to share our example report so you can see the kind of detail we go into. Given your interest in water quality, check out pages 30-35." |
| FU 3 | Day 6 | Social proof + value | Share a relevant testimonial or the Wilkinson case study, framed around their specific concern. Include one more actionable tip. | "Andrew Wilkinson had similar concerns about his home. Here's what we found and what he did about it. Also, one more quick tip on air filtration..." |
| FU 4 | Day 10 | Educational content | Share the most relevant Lightwork newsletter article for their situation (EMF guide, mold testing guide, air quality guide). Write the key takeaway into the email body. | "Thought you might find this interesting, we wrote a guide on how to test for mold the right way. The key insight is..." |
| FU 5 | Day 16 | New angle + soft ask | Share the science video or a different piece of content they haven't seen. First mention of "happy to continue the conversation whenever timing works." | "In case helpful, this video explains the science behind what we do. If you'd like to pick up where we left off, I'm here." |
| FU 6 | Day 25 | Availability + value | Share one final relevant resource. Mention specific upcoming availability in their city. Light ask. | "We have some availability in SF in early March. Also, thought you'd find this guide on air filtration useful for your apartment." |
| FU 7 | Day 35 | Graceful close | No pressure. "Totally understand if the timing isn't right. We're here whenever you're ready. In the meantime, here's one last resource." Keeps the door open. | "No worries at all if now isn't the right time. We're here whenever makes sense. In the meantime, thought this guide might be useful." |

**Key rules (from Haynes):**
- Write the value INTO the email. Don't just drop a link and say "check this out."
- Each email should be useful even if they never buy. That's what builds trust.
- Personalize based on the call transcript: reference their specific concerns, home type, city, health goals
- Never say "just checking in" or "just following up" without attaching value
- The sales scripts (EMF tips, air purifier recs, grounding product warnings) are your value ammunition

The daily digest email will tell you which leads are due for their next touch and draft the value-driven email for you, pulling from the transcript + sales scripts + relevant Lightwork content.

### How It Works

1. **Auto-updates daily at 8am** (same script that sends the digest email)
2. Pulls all active leads from Close.com (not yet converted)
3. **Preserves all checkboxes** when updating. Script only updates data columns (last activity, days since), never overwrites the FU checkboxes.
4. **Auto-detects follow-ups**: If Close.com shows a new outgoing email to that lead since last update, it auto-checks the next unchecked FU box
5. **Progress column** auto-calculates (e.g., "3/7")
6. Sorted by: fewest follow-ups first, then longest days since last activity
7. **Conditional formatting**:
   - Row RED: 0/7 follow-ups and 3+ days since call
   - Row YELLOW: Behind on cadence (next FU overdue)
   - Row GREEN: On track or completed
   - Progress bar in the Progress column

### Columns

- **Owner**: Jay, Johnny, or Dom (from Close.com)
- **Lead Name**: Full name, linked to Close.com lead page
- **City**: Where they're based
- **Budget**: From form ($2-5k, $5-10k, $10k+)
- **Source**: How they heard about Lightwork
- **FU 1 through FU 7**: Checkboxes for each follow-up touch (manual or auto-detected)
- **Progress**: Auto-calculated count (e.g., "3/7")
- **Last Activity**: Most recent email, call, or note in Close.com
- **Days Since**: Days since last activity

### Tabs

- **Active**: Leads with < 7 follow-ups (main working view)
- **Completed**: Leads with 7/7 follow-ups (archive)
- **Converted**: Leads who booked an assessment (win!)
- **Closed**: Leads who explicitly said no

### Access

- Jay, Johnny, and Dom all have edit access
- Anyone can check the "Followed Up" box
- Link pinned in Slack/bookmarked in browser

---

## Next Steps After This

Once this is working, we can add:
- Lead magnet downloaders who didn't book (from ConvertKit)
- "Going cold" alerts for leads with no activity in X days
- Automatic task creation in Close.com for each follow-up
- Weekly summary of pipeline health
