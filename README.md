# Lightwork Follow-Up Tracker

A 7-touch follow-up cadence system that tracks Customer Leads after their initial sales call and generates personalized draft emails via Claude.

## What It Does

1. Pulls meetings from the last 45 days from Close.com
2. Identifies Customer Leads who need follow-up today
3. Matches each lead to their Granola call transcript
4. Generates a cadence-appropriate draft email via Claude (Haiku 4.5)
5. Outputs an HTML digest with a pipeline tracker and draft emails, grouped by owner (Jay, Johnny, Dom)

## 7-Touch Cadence

| Touch | Day | Type |
|-------|-----|------|
| FU 1 | Day 1 | Post-call value tip |
| FU 2 | Day 3 | Second value drop |
| FU 3 | Day 6 | Social proof + value |
| FU 4 | Day 10 | Educational content |
| FU 5 | Day 16 | New angle + soft ask |
| FU 6 | Day 25 | Availability + value |
| FU 7 | Day 35 | Graceful close |

## How Follow-Up Count Works

- Counts **distinct outgoing email threads** (not individual emails)
- Multiple replies in the same thread (Re:/Fwd:) count as ONE follow-up
- Emails with "assessment" in the subject are excluded from the count

## Long-Term Nurture Cadence

Leads with a "Lost" opportunity status in Close.com are moved to a separate nurture track: value-only check-ins every 60 days, up to 6 touches over 1 year. No asks, no pressure.

| Touch | Day | Type |
|-------|-----|------|
| Nurture 1 | Day 60 | Value check-in |
| Nurture 2 | Day 120 | Value check-in |
| Nurture 3 | Day 180 | Value check-in |
| Nurture 4 | Day 240 | Value check-in |
| Nurture 5 | Day 300 | Value check-in |
| Nurture 6 | Day 360 | Final nurture |

To move a lead to nurture: set their opportunity status to "Lost" in Close.com.

## Lead Filtering

Leads are excluded from the cadence if:

- **Won opportunity** in Close.com (Booked Assessment, Test Completed, Report Completed, Won, Free Test Booked/Completed)
- **"testing" in the lead name** (test leads)
- **All follow-ups completed** (7 for active, 6 for nurture)

## Owner Assignment

Determined by which team member attended the call (from meeting attendees), not from a custom field.

## Setup

### Requirements

- Python 3.10+ (stdlib only, no pip packages)
- Close.com API key (read-only)
- Anthropic API key
- Granola local cache (for transcript matching)

### Environment Variables

Create a `.env` file in the project directory:

```
CLOSE_API_KEY=your_close_api_key
ANTHROPIC_API_KEY=your_anthropic_api_key
GRANOLA_CACHE=/path/to/Granola/cache-v3.json
```

### Reference Files

Place these in the `reference/` directory:

- `follow-up-examples.md` - Example emails for tone matching
- `sales-scripts.md` - Health tips and product links for email content

## Usage

```bash
cd /Users/dillandevram/Desktop/claude-projects/lightwork-digest
python3 post_call_digest.py
```

The script outputs `digest_preview.html` and opens it in the browser automatically.

## Output

The HTML digest contains:

1. **Pipeline Tracker** - All active leads grouped by owner, showing progress bars, FU status, and days since call
2. **Draft Emails** - Up to 8 leads due today (warmest first), each with a Claude-generated follow-up matching their cadence position

## Safety

- **Read-only**: The script cannot send emails or write to Close.com
- Close.com API calls are GET-only (non-GET requests raise RuntimeError)
- No SMTP imports or email sending capability
- All drafts are suggestions for manual review and sending
- See `SAFETY_VERIFICATION.md` for the full test suite

## Configuration

| Setting | Value | Location |
|---------|-------|----------|
| Lookback window | 45 days | `CADENCE_LOOKBACK_DAYS` |
| Max leads per digest | 8 | `MAX_LEADS_PER_DIGEST` |
| API rate limit | 0.25s between Close.com calls | `close_get()` |
| Claude model | claude-haiku-4-5-20251001 | `generate_digest_for_call()` |
| Transcript cap | 6,000 chars (Sheet) / 4,000 chars (local) | extract functions |
