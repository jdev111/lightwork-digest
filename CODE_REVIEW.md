# Code Review: post_call_digest.py

## Security: PASS

| Check | Status | Details |
|-------|--------|---------|
| Close.com write access | BLOCKED | Line 134: `_request()` raises RuntimeError on any non-GET to `api.close.com` |
| Close.com function scope | SAFE | Only `close_get()` exists. No POST/PUT/DELETE functions. |
| SMTP / email sending | REMOVED | No `smtplib`, no `MIMEMultipart`, no send function, no SMTP creds in .env |
| Lead email usage | SAFE | `lead_email` (line 689) only appears as text in Claude prompt (line 726). Never in any recipient field. |
| Outbound connections | 3 total | Close.com (GET only), Google Sheets (GET only), Anthropic API (POST, generates text) |
| Can this script contact a lead? | NO | Impossible. No email sending capability. No Close.com write access. |

## Logic: 2 Issues Found

### Issue 1: `GRANOLA_LIST_ID` is unused (line 46)

```python
GRANOLA_LIST_ID = "77691bba-7fa9-471c-b9a1-f6a953ef27c4"
```

Dead code. The local cache loads the full document store, not by list ID. Can be removed.

### Issue 2: Blank line / extra whitespace at line 114

```python
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


GRANOLA_CACHE = os.environ.get(
```

Extra blank line from the SMTP removal. Cosmetic only.

## Architecture: Clean

- Single file, stdlib only, no dependencies
- Clear separation: Config > API > Granola > Claude > HTML > Main
- Rate limiting on Close.com calls (0.25s delay)
- Backlog cap at 8 leads per digest
- Fresh-email-only counting (filters Re:/Fwd:/[INT])
- Test leads excluded ("testing" in name)

## Performance

- ~62 Customer Leads with meetings in the 45-day window
- Each lead requires 1 API call to check email count (in `get_leads_due_today`)
- Total runtime: ~2.5 min for status checks + ~30s per lead for Claude generation
- With 8-lead cap: ~6.5 min total runtime. Acceptable for a daily batch job.

## Potential Improvement (not a bug)

The `_is_fresh_followup` filter (line 314) uses subject prefix matching. This correctly filters:
- "Re: Water Testing" (thread reply, not a follow-up)
- "Re: Accepted: Lightwork Home Health test call" (calendar reply)
- "Fwd: Water Testing" (forward)

But it would incorrectly count internal transfer emails like "Jordan Huelskamp <=> David Silver" as follow-ups since they don't start with Re:/Fwd:. Low impact since these are rare.

## Summary

The script is clean, secure, and correctly implements the 7-touch cadence system. The two issues found are cosmetic (dead constant, extra blank line). No functional bugs.
