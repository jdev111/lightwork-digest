#!/usr/bin/env python3
"""
Daily Missing Transcripts Report

Compares yesterday's customer calls from Close.com against the
local Granola cache. Any call without a matching transcript is flagged.
Sends a summary email to jay@lightworkhome.com every morning.

Uses the same .env and Close.com API as post_call_digest.py.
"""

import json
import os
import ssl
import smtplib
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ---------------------------------------------------------------------------
# Config (shared with post_call_digest.py)
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"

CALL_KEYWORDS = ["test call", "intro call", "partner call"]

TEAM_EMAILS = {
    "jay@lightworkhome.com",
    "johnny@lightworkhome.com",
    "dom@lightworkhome.com",
    "josh@lightworkhome.com",
}

ALLOWED_RECIPIENTS = {
    "jay@lightworkhome.com",
    "johnny@lightworkhome.com",
    "dom@lightworkhome.com",
}

# Map first name to email for per-person notifications
OWNER_NAME_TO_EMAIL = {
    "Jay": "jay@lightworkhome.com",
    "Johnny": "johnny@lightworkhome.com",
    "Dom": "dom@lightworkhome.com",
    "Josh": "jay@lightworkhome.com",  # Josh's missing transcripts go to Jay
}


def load_env(path):
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


load_env(ENV_PATH)

CLOSE_API_KEY = os.environ.get("CLOSE_API_KEY", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
GRANOLA_CACHE = os.environ.get(
    "GRANOLA_CACHE",
    str(Path.home() / "Library/Application Support/Granola/cache-v3.json"),
)

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _request(method, url, headers=None, body=None, basic_auth=None):
    if headers is None:
        headers = {}
    if body is not None and isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")

    auth_header = None
    if basic_auth:
        import base64
        cred = base64.b64encode(f"{basic_auth[0]}:{basic_auth[1]}".encode()).decode()
        auth_header = f"Basic {cred}"

    ctx = ssl.create_default_context()
    import time as _time
    transient_codes = {400, 408, 425, 429, 500, 502, 503, 504}
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        if auth_header:
            req.add_header("Authorization", auth_header)
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else ""
            if e.code in transient_codes and attempt < max_attempts:
                _time.sleep(5 * attempt)
                continue
            print(f"HTTP {e.code} for {url}: {error_body[:300]}")
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionResetError, ssl.SSLError):
            if attempt < max_attempts:
                _time.sleep(5 * attempt)
                continue
            raise


# ---------------------------------------------------------------------------
# Close.com
# ---------------------------------------------------------------------------


def close_get(endpoint, params=None):
    url = f"https://api.close.com/api/v1{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _request("GET", url, basic_auth=(CLOSE_API_KEY, ""))


def get_meetings_for_range(start_dt, end_dt):
    """Pull all meetings that started between start_dt and end_dt."""
    lookback_start = start_dt - timedelta(days=7)

    all_meetings = []
    has_more = True
    skip = 0

    while has_more:
        data = close_get(
            "/activity/meeting/",
            {
                "date_created__gte": lookback_start.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                "_limit": 100,
                "_skip": skip,
            },
        )
        meetings = data.get("data", [])
        all_meetings.extend(meetings)
        has_more = data.get("has_more", False)
        skip += len(meetings)

    completed = []
    for m in all_meetings:
        starts_at = m.get("starts_at", "")
        if not starts_at:
            continue
        try:
            s_dt = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if start_dt <= s_dt < end_dt:
            completed.append(m)

    return completed


def get_lead_details(lead_id):
    return close_get(f"/lead/{lead_id}/")


def get_close_users():
    data = close_get("/user/")
    mapping = {}
    for u in data.get("data", []):
        uid = u.get("id", "")
        name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
        mapping[uid] = name
    return mapping


def is_lead_no_show(lead_id):
    """Check if any opportunity on this lead has a 'No Show' pipeline status."""
    try:
        data = close_get("/opportunity/", {"lead_id": lead_id, "_limit": 50})
        for opp in data.get("data", []):
            if (opp.get("status_label") or "").lower() == "no show":
                return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Granola local cache
# ---------------------------------------------------------------------------


def load_granola_cache():
    """Load the local Granola cache, return (docs_dict, transcripts_dict)."""
    cache_path = Path(GRANOLA_CACHE)
    if not cache_path.exists():
        print(f"  Granola cache not found at {cache_path}")
        return {}, {}

    with open(cache_path) as f:
        raw = json.load(f)

    cache = raw.get("cache", {})
    if isinstance(cache, str):
        cache = json.loads(cache)

    state = cache.get("state", {})
    all_docs = state.get("documents", {})
    all_transcripts = state.get("transcripts", {})
    return all_docs, all_transcripts


def match_granola(meeting, granola_docs):
    """Match a Close.com meeting to a Granola document by attendee email or title."""
    close_attendees = set()
    for a in meeting.get("attendees", []):
        email = (a.get("email") or "").lower()
        if email and email not in TEAM_EMAILS:
            close_attendees.add(email)

    meeting_title = (meeting.get("title") or "").lower().strip()
    meeting_date = meeting.get("date_created", "")[:10]

    best_match = None
    best_score = 0

    for doc_id, doc in granola_docs.items():
        score = 0

        gcal = doc.get("google_calendar_event") or {}
        gcal_attendees = set()
        for a in gcal.get("attendees", []):
            gcal_attendees.add((a.get("email") or "").lower())

        people = doc.get("people") or {}
        if isinstance(people, dict):
            for p_list in people.values():
                if isinstance(p_list, list):
                    for p in p_list:
                        if isinstance(p, dict):
                            gcal_attendees.add((p.get("email") or "").lower())

        overlap = close_attendees & gcal_attendees
        if overlap:
            score += 10 * len(overlap)

        doc_title = (doc.get("title") or "").lower().strip()
        gcal_title = (gcal.get("summary") or "").lower().strip()
        if meeting_title and (meeting_title in doc_title or meeting_title in gcal_title):
            score += 5
        elif doc_title and doc_title in meeting_title:
            score += 3

        doc_date = (doc.get("created_at") or "")[:10]
        if doc_date == meeting_date:
            score += 2

        if score > best_score:
            best_score = score
            best_match = doc

    if best_score >= 5:
        return best_match
    return None


def has_granola_content(doc, transcripts_dict):
    """Check if a Granola doc has any notes or transcript content."""
    if not doc:
        return False

    # Check typed notes
    typed_notes = (doc.get("notes_markdown") or doc.get("notes_plain") or "").strip()
    if not typed_notes:
        notes_json = doc.get("notes")
        if isinstance(notes_json, dict):
            typed_notes = _extract_text_from_prosemirror(notes_json).strip()
    if typed_notes:
        return True

    # Check spoken transcript
    doc_id = doc.get("id", "")
    if transcripts_dict and doc_id in transcripts_dict:
        entries = transcripts_dict[doc_id]
        if isinstance(entries, list) and entries:
            for entry in entries:
                if (entry.get("text") or "").strip():
                    return True

    return False


def _extract_text_from_prosemirror(node):
    if not isinstance(node, dict):
        return ""
    texts = []
    if node.get("type") == "text":
        texts.append(node.get("text", ""))
    for child in node.get("content", []):
        texts.append(_extract_text_from_prosemirror(child))
    return "\n".join(t for t in texts if t)


# ---------------------------------------------------------------------------
# Email builder
# ---------------------------------------------------------------------------


def build_report_html(all_calls, matched_count, missing_count, week_start, week_end):
    """Build the HTML email showing all calls with transcript status."""
    total_count = matched_count + missing_count
    date_range = f"{week_start.strftime('%b %-d')} - {week_end.strftime('%b %-d, %Y')}"

    if missing_count == 0:
        status_color = "#27ae60"
        status_text = "All calls have transcripts"
        status_icon = "&#10003;"
    else:
        status_color = "#c0392b"
        status_text = f"{missing_count} call{'s' if missing_count != 1 else ''} missing transcripts"
        status_icon = "&#9888;"

    rows_html = ""
    for call in all_calls:
        cs = call["call_status"]
        if cs == "matched":
            badge = '<span style="background:#27ae60; color:white; padding:2px 8px; border-radius:10px; font-size:12px;">Matched</span>'
            row_bg = ""
        elif cs == "cancelled":
            badge = '<span style="background:#95a5a6; color:white; padding:2px 8px; border-radius:10px; font-size:12px;">Cancelled</span>'
            row_bg = ' style="background:#f5f5f5;"'
        elif cs == "no_show":
            badge = '<span style="background:#e67e22; color:white; padding:2px 8px; border-radius:10px; font-size:12px;">No Show</span>'
            row_bg = ' style="background:#fef5ec;"'
        else:
            badge = '<span style="background:#c0392b; color:white; padding:2px 8px; border-radius:10px; font-size:12px;">Missing</span>'
            row_bg = ' style="background:#fdf2f2;"'

        rows_html += f"""
        <tr{row_bg}>
          <td style="padding:10px 12px; border-bottom:1px solid #eee;">{badge}</td>
          <td style="padding:10px 12px; border-bottom:1px solid #eee;">{call['date']}</td>
          <td style="padding:10px 12px; border-bottom:1px solid #eee;">{call['owner']}</td>
          <td style="padding:10px 12px; border-bottom:1px solid #eee;">
            <a href="{call['close_url']}" style="color:#2E5B88; text-decoration:none;">{call['lead_name']}</a>
          </td>
          <td style="padding:10px 12px; border-bottom:1px solid #eee;">{call['title']}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; max-width:750px; margin:0 auto; padding:20px; background:#FFFCF0; color:#1a1a1a;">
  <div style="text-align:center; padding:16px 0; border-bottom:2px solid #2E5B88; margin-bottom:20px;">
    <h1 style="margin:0; font-size:22px; color:#1a1a1a;">Daily Transcript Report</h1>
    <p style="margin:4px 0 0 0; color:#666; font-size:14px;">{date_range}</p>
  </div>

  <div style="background:#fff; border:1px solid #ddd; border-radius:8px; padding:20px; margin-bottom:20px;">
    <div>
      <span style="font-size:28px; color:{status_color}; margin-right:8px;">{status_icon}</span>
      <span style="font-size:16px; font-weight:600; color:{status_color};">{status_text}</span>
    </div>
    <div style="margin-top:12px; font-size:14px; color:#666;">
      {matched_count} of {total_count} customer calls matched to Granola transcripts
    </div>
  </div>

  <table style="width:100%; border-collapse:collapse; font-size:14px;">
    <thead>
      <tr style="background:#f5f5f5; text-align:left;">
        <th style="padding:10px 12px; border-bottom:2px solid #ddd;">Status</th>
        <th style="padding:10px 12px; border-bottom:2px solid #ddd;">Date</th>
        <th style="padding:10px 12px; border-bottom:2px solid #ddd;">Owner</th>
        <th style="padding:10px 12px; border-bottom:2px solid #ddd;">Lead</th>
        <th style="padding:10px 12px; border-bottom:2px solid #ddd;">Call Title</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>

  <div style="background:#fff; border:1px solid #ddd; border-radius:8px; padding:16px; margin-top:20px; font-size:13px; color:#666;">
    <strong>How to fix missing transcripts:</strong> Make sure Granola is running during calls so transcripts are saved locally.
    If a call was recorded but isn't matching, check that attendee emails match between Close.com and Granola.
  </div>

  <div style="text-align:center; padding:20px 0; margin-top:30px; border-top:1px solid #ddd; color:#999; font-size:12px;">
    Auto-generated daily by Lightwork Digest.
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Email sender
# ---------------------------------------------------------------------------


def send_report(html_body, subject, recipients):
    """Send report to one or more recipients. All must be in ALLOWED_RECIPIENTS."""
    if isinstance(recipients, str):
        recipients = [recipients]

    for r in recipients:
        if r.lower() not in ALLOWED_RECIPIENTS:
            raise RuntimeError(
                f"SAFETY: Refusing to send to {r}. Only {ALLOWED_RECIPIENTS} are allowed."
            )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_EMAIL
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.sendmail(SMTP_EMAIL, recipients, msg.as_string())
        print(f"Report sent to {', '.join(recipients)}")
    except Exception as e:
        print(f"Error sending to {', '.join(recipients)}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(dry_run=False):
    now = datetime.now(timezone.utc)
    week_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = week_end - timedelta(days=1)

    print(f"Missing Transcripts Report")
    print(f"Checking: {week_start.strftime('%b %-d')} - {week_end.strftime('%b %-d, %Y')}")
    print("=" * 60)

    # 1. Get team members
    print("Fetching Close.com users...")
    user_map = get_close_users()

    # 2. Pull the week's meetings
    print("Fetching meetings from the past 7 days...")
    all_meetings = get_meetings_for_range(week_start, week_end)
    print(f"  Found {len(all_meetings)} total meetings")

    if not all_meetings:
        print("No meetings this week. Nothing to report.")
        return

    # 3. Filter to customer leads only
    print("Filtering to customer leads...")
    customer_meetings = []
    lead_cache = {}

    for m in all_meetings:
        lead_id = m.get("lead_id", "")
        if not lead_id:
            continue
        if lead_id not in lead_cache:
            try:
                lead_cache[lead_id] = get_lead_details(lead_id)
            except Exception as e:
                print(f"  Error fetching lead {lead_id}: {e}")
                continue

        lead_info = lead_cache[lead_id]
        category = lead_info.get("custom", {}).get("Category", [])
        if "Customer Lead" in category:
            customer_meetings.append(m)

    print(f"  {len(customer_meetings)} customer lead calls")

    if not customer_meetings:
        print("No customer lead calls this week. Nothing to report.")
        return

    # 4. Load local Granola cache
    print("Loading local Granola cache...")
    granola_docs, granola_transcripts = load_granola_cache()
    print(f"  {len(granola_docs)} docs, {len(granola_transcripts)} transcripts in cache")

    # 5. Check each call for a matching transcript
    all_calls = []
    matched_count = 0

    for m in customer_meetings:
        title = m.get("title", "Untitled")
        lead_id = m.get("lead_id", "")
        # Find which team member attended (for per-person notifications)
        owner_email = ""
        for a in m.get("attendees", []):
            email = (a.get("email") or "").lower()
            if email in TEAM_EMAILS:
                owner_email = email
                break

        # Derive owner name from attendee email, fall back to Close user_id
        TEAM_EMAIL_TO_NAME = {
            "jay@lightworkhome.com": "Jay",
            "johnny@lightworkhome.com": "Johnny",
            "dom@lightworkhome.com": "Dom",
            "josh@lightworkhome.com": "Josh",
        }
        owner_first = TEAM_EMAIL_TO_NAME.get(owner_email, "")
        if not owner_first:
            user_id = m.get("user_id", "")
            owner_name = user_map.get(user_id, "Unknown")
            owner_first = owner_name.split()[0] if owner_name else "Unknown"
        lead_info = lead_cache.get(lead_id, {})
        lead_name = lead_info.get("display_name", "Unknown")
        close_url = lead_info.get("html_url", "")

        starts_at = m.get("starts_at", "")
        try:
            call_date = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
            date_str = call_date.strftime("%a %b %-d")
        except (ValueError, TypeError):
            date_str = "Unknown"

        # Determine call status: cancelled, no-show, matched, or missing
        meeting_status = (m.get("status") or "").lower()
        is_cancelled = meeting_status in ("canceled", "declined-by-lead")
        lead_meeting_status = (lead_info.get("custom", {}).get("Meeting Status") or "").lower()
        if lead_meeting_status == "meeting cancelled":
            is_cancelled = True

        no_show = False
        if not is_cancelled:
            no_show = is_lead_no_show(lead_id)

        has_transcript = False
        if not is_cancelled and not no_show and granola_docs:
            granola_match = match_granola(m, granola_docs)
            if granola_match and has_granola_content(granola_match, granola_transcripts):
                has_transcript = True
                matched_count += 1

        if is_cancelled:
            call_status = "cancelled"
            status_label = "CANCELLED"
        elif no_show:
            call_status = "no_show"
            status_label = "NO SHOW"
        elif has_transcript:
            call_status = "matched"
            status_label = "MATCH"
        else:
            call_status = "missing"
            status_label = "MISSING"

        print(f"  [{status_label}] {title} - {lead_name}")

        all_calls.append({
            "date": date_str,
            "owner": owner_first,
            "owner_email": owner_email,
            "lead_name": lead_name,
            "title": title,
            "close_url": close_url,
            "call_status": call_status,
        })

    # Sort: missing first, then no-show, cancelled, matched
    status_order = {"missing": 0, "no_show": 1, "cancelled": 2, "matched": 3}
    all_calls.sort(key=lambda c: (status_order.get(c["call_status"], 99), c["date"]))

    # 6. Check for missing transcripts
    missing_calls = [c for c in all_calls if c["call_status"] == "missing"]

    if not missing_calls:
        print("\nNo missing transcripts. No emails to send.")
        return

    # Group missing calls by owner email
    from collections import defaultdict
    missing_by_owner = defaultdict(list)
    for c in missing_calls:
        email = c.get("owner_email", "")
        if email and email in ALLOWED_RECIPIENTS:
            missing_by_owner[email].append(c)
        else:
            # Fallback: send to Jay if owner unknown
            missing_by_owner["jay@lightworkhome.com"].append(c)

    print(f"\n{len(missing_calls)} missing transcript(s) across {len(missing_by_owner)} team member(s)")

    if dry_run:
        for owner_email, calls in missing_by_owner.items():
            owner_name = calls[0]["owner"]
            html = build_report_html(calls, 0, len(calls), week_start, week_end)
            output_path = SCRIPT_DIR / f"missing_transcripts_preview_{owner_name}.html"
            output_path.write_text(html)
            print(f"DRY RUN: {owner_name}'s report saved to {output_path} ({len(calls)} missing)")
        return

    # 7. Send individual emails to each team member with their missing calls
    for owner_email, calls in missing_by_owner.items():
        owner_name = calls[0]["owner"]
        html = build_report_html(calls, 0, len(calls), week_start, week_end)
        subject = f"Missing Transcript{'s' if len(calls) > 1 else ''} - {week_start.strftime('%b %-d')} ({len(calls)} call{'s' if len(calls) > 1 else ''})"
        print(f"\nSending to {owner_email} ({len(calls)} missing)...")
        send_report(html, subject, [owner_email])

    print(f"\nDone. Notified {len(missing_by_owner)} team member(s) about {len(missing_calls)} missing transcript(s).")


if __name__ == "__main__":
    import sys

    dry = "--dry-run" in sys.argv or "--preview" in sys.argv
    main(dry_run=dry)
