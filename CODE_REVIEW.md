# Code Review: post_call_digest.py

**Reviewed:** Feb 11, 2026 | **Commit:** ac327eb

---

## Critical Issues

### 1. Dead code after `return` (lines 1778-1820)
Lines 1778-1820 are completely unreachable. `generate_digest_for_call()` returns at line 1776, but there's a duplicate copy of the OpenAI API call logic sitting below it that can never execute. Looks like a merge artifact. Delete it.

### 2. Duplicate Close.com API call for meetings
`get_meetings_in_range()` (line 723) and `get_no_show_lead_ids()` (line 854) both make the same paginated API call to `/activity/meeting/` with nearly identical pagination logic. This doubles your Close.com API usage for meetings.

**Fix:** Fetch all meetings once, then filter by status (completed vs. canceled/no-show) downstream.

### 3. Reference files loaded per-lead, not per-run
`load_reference_file()` is called twice inside `generate_digest_for_call()` (lines 1582-1583), which runs once per lead. With 8 leads, that's 16 file reads of the same two files.

**Fix:** Load `sales-scripts.md` and `follow-up-examples.md` once in `main()` and pass them in.

---

## Performance

### 4. Sequential transcript lookups
In `main()` (lines 2599-2625), transcript matching and MCP fetches happen sequentially for every lead. Each MCP transcript fetch is a network call. With 20 leads, that's 20 back-to-back HTTP requests.

**Fix:** Use `concurrent.futures.ThreadPoolExecutor` to parallelize. You already import `threading` for the OAuth flow.

### 5. `import re` scattered inside functions
`import re` appears at lines 206, 328, 346, 1002, 1417, 1502. Same for `import html` (lines 1851, 1988, 2145) and `import subprocess` (lines 529, 2640, 2807). Move all to top-level imports. Python caches them after the first import, but it's messy.

---

## Code Quality

### 6. Three nearly identical meeting-matching functions
- `match_granola_sheet()` (line 1127): matches Close meeting to Sheet row
- `match_granola()` (line 1227): matches Close meeting to local cache doc
- `mcp_match_meeting()` (line 1426): matches Close meeting to MCP meeting

All three use the same scoring logic: 10 points per email overlap, 5-8 for title match, 2 for date proximity, threshold of 5.

**Fix:** Extract a shared `score_meeting_match()` function. Each caller just needs to extract emails/title/date from its source format, then call the shared scorer.

### 7. `main()` is 260+ lines
It handles fetching leads, matching transcripts, generating drafts, building HTML, and opening the browser. Hard to test or modify one piece.

**Fix:** Break into named functions:
- `fetch_and_annotate_leads()` - Close API + transcript annotation
- `generate_drafts(due_leads)` - Claude calls + section building
- `output_digest(sections, tracker)` - HTML assembly + file write

### 8. ~800 lines of inline HTML/CSS/JS
The `build_tracker_view()`, `build_lead_section()`, and `build_digest_html()` functions are mostly HTML string literals with f-string interpolation and brace escaping (`{{`/`}}`). Any UI change requires editing Python strings.

**Fix:** Extract the HTML template to a separate file (`template.html`) and use `string.Template` or simple `str.replace()` for placeholders. Or at minimum, move the CSS/JS to separate strings at module level.

### 9. Magic numbers
Scattered constants with no names:
- Transcript caps: `6000`, `4000`, `3000`, `2000`, `500` chars
- Match threshold: `5` (used in three places)
- OAuth timeout: `180` seconds
- Retry backoff: `0.8 * attempt`

Make these named constants at the top.

---

## Reliability

### 10. No early validation of required env vars
If `.env` is missing or doesn't have `CLOSE_API_KEY`, the script fails deep inside an API call with a confusing urllib error. Add a check at startup:

```python
if not CLOSE_API_KEY:
    print("Error: CLOSE_API_KEY not set.")
    sys.exit(1)
```

### 11. Google Sheet matching doesn't use date proximity
`match_granola_sheet()` scores by email overlap and title, but not by date. The local cache matcher (`match_granola`) and MCP matcher both include date proximity scoring. If the same lead had two calls, the Sheet matcher might pick the wrong one.

### 12. OAuth token file has no permission restrictions
`_save_json()` writes tokens as world-readable JSON. Add `os.chmod(path, 0o600)` after writing.

---

## Feature Ideas

### 13. `argparse` for CLI options
Add flags like:
- `--dry-run`: skip Claude, just show which leads are due
- `--owner Jay`: only process one owner's leads
- `--no-open`: don't auto-launch browser
- `--max N`: override MAX_LEADS_PER_DIGEST

### 14. Skip-lead mechanism
A `skip_leads.txt` file (one lead ID or name per line) would let users exclude specific leads without touching code. Useful when someone says "I'll handle this one manually."

### 15. Run deduplication
If the script runs twice in a day, it regenerates the same drafts and burns API credits. A lightweight `last_run.json` mapping `lead_id -> {fu_number, date, draft_hash}` could skip leads that already have today's draft.

### 16. Summary stats after run
Print a quick summary: how many drafts generated with vs. without transcripts, how many overdue vs. due today, total API calls made, runtime. Helps gauge output quality at a glance.

---

## Priority Order

If I had to pick the top 5 changes to make:

1. Delete the dead code (lines 1778-1820)
2. Deduplicate the meetings API call
3. Cache reference files per-run instead of per-lead
4. Add `argparse` with `--dry-run` and `--owner`
5. Extract shared meeting-matching function
