#!/usr/bin/env python3
"""Generate all 7 follow-up drafts for a single lead with a transcript.

Usage: python3 generate_7fu_demo.py
Output: 7fu_demo.html (auto-opens in browser)
"""
import html as html_mod
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Import everything from the main script
from post_call_digest import (
    CADENCE, CADENCE_LOOKBACK_DAYS, NO_SHOW_STATUSES, OWNER_SIGNATURE, SCRIPT_DIR,
    TEAM_EMAIL_TO_NAME, TRANSCRIPT_CAP_SHEET,
    _extract_first_name, _fetch_all_meetings, _filter_meetings_in_range,
    _load_condensed_or_fallback,
    _parse_ai_sections, close_get, extract_granola_notes, extract_sheet_notes,
    generate_digest_for_call, get_followup_history, get_recent_customer_leads,
    load_granola_cache, load_granola_sheet, match_granola, match_granola_sheet,
    _granola_local_has_transcript,
    SALES_TIPS_CONDENSED_PATH, SALES_SCRIPTS_PATH,
    VOICE_GUIDE_CONDENSED_PATH, FOLLOWUP_EXAMPLES_PATH,
)

now = datetime.now(timezone.utc)
since = now - timedelta(days=CADENCE_LOOKBACK_DAYS)

print("Fetching meetings...")
all_raw = _fetch_all_meetings(since, now)
print(f"  {len(all_raw)} meetings")

print("Getting customer leads...")
leads = get_recent_customer_leads(all_meetings=all_raw)
print(f"  {len(leads)} customer leads")

print("\nLoading transcripts...")
sheet_rows = load_granola_sheet()
granola_docs, granola_transcripts = load_granola_cache()
print(f"  {len(sheet_rows)} sheet rows, {len(granola_docs)} local docs")

# Find a lead with a transcript (skip previously demoed leads)
SKIP_NAMES = {"MacLane Wilkison"}  # Already demoed
chosen = None
chosen_notes = ""
for lead_id, info in leads.items():
    display_name = info['lead_info'].get('display_name', '')
    if display_name in SKIP_NAMES:
        continue
    earliest = min(info["meetings"], key=lambda m: m.get("starts_at", ""))

    # Try sheet first
    sheet_match = match_granola_sheet(earliest, sheet_rows) if sheet_rows else None
    if sheet_match:
        transcript = (sheet_match.get("Transcript") or "").strip()
        if transcript:
            chosen = (lead_id, info)
            chosen_notes = extract_sheet_notes(sheet_match)
            print(f"\nFound lead with Sheet transcript: {display_name}")
            break

    # Try local cache
    local_match = match_granola(earliest, granola_docs) if granola_docs else None
    if local_match and _granola_local_has_transcript(local_match, granola_transcripts):
        chosen = (lead_id, info)
        chosen_notes = extract_granola_notes(local_match, granola_transcripts)
        print(f"\nFound lead with local transcript: {display_name}")
        break

if not chosen:
    print("\nNo leads with transcripts found. Picking first lead with notes...")
    for lead_id, info in leads.items():
        earliest = min(info["meetings"], key=lambda m: m.get("starts_at", ""))
        sheet_match = match_granola_sheet(earliest, sheet_rows) if sheet_rows else None
        if sheet_match and (sheet_match.get("Notes") or "").strip():
            chosen = (lead_id, info)
            chosen_notes = extract_sheet_notes(sheet_match)
            print(f"  Using: {info['lead_info'].get('display_name')} (notes only)")
            break
        local_match = match_granola(earliest, granola_docs) if granola_docs else None
        if local_match:
            notes = extract_granola_notes(local_match, granola_transcripts)
            if notes:
                chosen = (lead_id, info)
                chosen_notes = notes
                print(f"  Using: {info['lead_info'].get('display_name')} (notes only)")
                break

if not chosen:
    print("No leads with any transcript or notes. Exiting.")
    sys.exit(1)

lead_id, info = chosen
lead_info = info["lead_info"]
lead_name = lead_info.get("display_name", "Unknown")
first_name = _extract_first_name(lead_name)
owner = info["owner_name"]
earliest = min(info["meetings"], key=lambda m: m.get("starts_at", ""))

# Detect no-show: check if the latest meeting for this lead was canceled/no-show
no_show_meetings = _filter_meetings_in_range(all_raw, since, now, NO_SHOW_STATUSES)
latest_completed = max(
    (m.get("starts_at", "") for m in info["meetings"]
     if (m.get("status") or "").lower().strip() == "completed"), default=""
)
latest_no_show = ""
for m in no_show_meetings:
    if m.get("lead_id") == lead_id:
        s = m.get("starts_at", "")
        if s > latest_no_show:
            latest_no_show = s
is_no_show = bool(latest_no_show and latest_no_show > latest_completed)

# Load reference files once
sales_scripts = _load_condensed_or_fallback(SALES_TIPS_CONDENSED_PATH, SALES_SCRIPTS_PATH, 3000)
followup_examples = _load_condensed_or_fallback(VOICE_GUIDE_CONDENSED_PATH, FOLLOWUP_EXAMPLES_PATH, 2000)

print(f"\nGenerating all 7 follow-ups for: {lead_name}")
print(f"  Owner: {owner}")
print(f"  First call: {info['first_call_date'].strftime('%b %d, %Y')}")
print(f"  No-show: {'YES' if is_no_show else 'No'}")
print(f"  Transcript length: {len(chosen_notes)} chars")
print()

# For demo purposes, simulate cumulative sent_emails
# FU1 has none, FU2 sees FU1's draft, etc.
cumulative_emails = []
drafts = []

for fu_num in range(1, 8):
    day_offset, fu_type, fu_instructions = CADENCE[fu_num]
    print(f"  [{fu_num}/7] FU #{fu_num} - {fu_type} (Day {day_offset})...")

    raw = generate_digest_for_call(
        lead_info, chosen_notes, earliest,
        owner_name=owner,
        fu_number=fu_num,
        sent_emails=list(cumulative_emails),  # copy
        cadence_type="active",
        no_show=is_no_show,
        sales_scripts=sales_scripts,
        followup_examples=followup_examples,
    )

    parsed = _parse_ai_sections(raw)
    draft_text = parsed.get("draft") or parsed.get("raw") or raw
    reasoning = (parsed.get("reasoning") or "").strip()
    priority = (parsed.get("priority") or "").strip()

    drafts.append({
        "fu_num": fu_num,
        "fu_type": fu_type,
        "day_offset": day_offset,
        "draft": draft_text,
        "reasoning": reasoning,
        "priority": priority,
        "raw": raw,
    })

    # Add this draft to cumulative emails for next FU's context
    cumulative_emails.append({
        "subject": f"Follow-up {fu_num}: {fu_type}",
        "body": draft_text[:1000],
    })

    print(f"    Done ({len(draft_text)} chars)")

# Build HTML
sections = ""
for d in drafts:
    reasoning_html = ""
    if d["reasoning"]:
        reasoning_html = f"""
        <div style="background:#f0f4f8; border-radius:6px; padding:10px 12px; margin-top:10px; font-size:12px; color:#555;">
          <strong>Why this tip:</strong> {html_mod.escape(d['reasoning'])}
        </div>"""
    priority_html = ""
    if d["priority"]:
        p = d["priority"].upper()
        color = "#c0392b" if "HIGH" in p else "#E67E22" if "MED" in p else "#27ae60"
        priority_html = f' <span style="background:{color}; color:white; font-size:10px; padding:2px 6px; border-radius:3px; margin-left:8px;">{html_mod.escape(d["priority"])}</span>'

    sections += f"""
    <div style="border:1px solid #ddd; border-radius:8px; padding:20px; margin-bottom:20px; background:#fff;">
      <h3 style="margin:0 0 4px 0; color:#2E5B88;">
        FU #{d['fu_num']}: {html_mod.escape(d['fu_type'])} (Day {d['day_offset']}){priority_html}
      </h3>
      <div style="background:#f9f9f9; border-left:3px solid #2E5B88; padding:14px; margin-top:10px;
                  white-space:pre-wrap; font-family:sans-serif; font-size:14px; line-height:1.7; color:#333;">
{html_mod.escape(d['draft'])}</div>
      {reasoning_html}
    </div>"""

# Call notes preview
notes_preview = chosen_notes[:1500]
if len(chosen_notes) > 1500:
    notes_preview += "\n[...truncated]"

custom = lead_info.get("custom", {})
addresses = lead_info.get("addresses", [])
city = addresses[0].get("city", "") if addresses else ""

html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>7-Touch Demo: {html_mod.escape(lead_name)}</title></head>
<body style="font-family:sans-serif; max-width:750px; margin:0 auto; padding:20px; background:#f5f5f5;">

<div style="background:#2E5B88; color:white; padding:20px; border-radius:8px 8px 0 0;">
  <h1 style="margin:0; font-size:22px;">7-Touch Cadence Demo</h1>
  <p style="margin:6px 0 0 0; opacity:0.85; font-size:14px;">{html_mod.escape(lead_name)} ({html_mod.escape(city)}) | Owner: {html_mod.escape(owner)}</p>
</div>

<div style="background:#fff; border:1px solid #ddd; border-top:none; padding:16px; margin-bottom:20px; border-radius:0 0 8px 8px;">
  <div style="display:flex; gap:24px; font-size:13px; color:#555;">
    <div><strong>Budget:</strong> {html_mod.escape(str(custom.get('Budget', 'N/A')))}</div>
    <div><strong>Source:</strong> {html_mod.escape(str(custom.get('How did you hear about us?', 'N/A')))}</div>
    <div><strong>Why:</strong> {html_mod.escape(str(custom.get('Why are you reaching out', 'N/A')))}</div>
  </div>
</div>

{sections}

<details style="border:1px solid #ddd; border-radius:8px; padding:16px; background:#fff; margin-top:30px;">
  <summary style="cursor:pointer; font-size:14px; font-weight:600; color:#2E5B88;">Call Notes / Transcript</summary>
  <pre style="white-space:pre-wrap; font-size:12px; color:#555; margin-top:10px; line-height:1.5;">{html_mod.escape(notes_preview)}</pre>
</details>

<p style="color:#999; font-size:11px; text-align:center; margin-top:24px;">Generated {datetime.now().strftime('%b %d, %Y %H:%M')} by Lightwork Follow-Up Tracker</p>
</body></html>"""

output_path = SCRIPT_DIR / "7fu_demo.html"
output_path.write_text(html)
print(f"\nSaved to {output_path}")
subprocess.run(["open", str(output_path)])
