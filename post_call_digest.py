#!/usr/bin/env python3
"""
Lightwork Follow-Up Tracker (7-Touch Cadence System)

Tracks Customer Leads through a 7-touch follow-up cadence after their
initial sales call. Pulls meetings from the last 45 days from Close.com,
counts outgoing emails to determine follow-up progress, and generates
cadence-appropriate draft emails via Claude for leads due today.

Each lead gets a tailored email matching their position in the cadence:
  FU1 (Day 1)  - Post-call value tip
  FU2 (Day 3)  - Second value drop
  FU3 (Day 6)  - Social proof + value
  FU4 (Day 10) - Educational content
  FU5 (Day 16) - New angle + soft ask
  FU6 (Day 25) - Availability + value
  FU7 (Day 35) - Graceful close

No external packages required - stdlib only.
"""

import argparse
import csv
import html as html_mod
import io
import json
import os
import re
import smtplib
import sqlite3
import ssl
import subprocess
import time
import base64
import hashlib
import secrets
import threading
import http.client
import http.server
import socket
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GranolaRateLimitError(Exception):
    """Raised when Granola MCP returns a rate limit error."""
    pass


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"

# Granola Google Sheet (primary transcript source)
GRANOLA_SHEET_ID = "1elFHP56RfLXffRAXUFoI-iCkuhVPSeU-FBl_1PthLC0"
GRANOLA_SHEET_URL = f"https://docs.google.com/spreadsheets/d/{GRANOLA_SHEET_ID}/export?format=csv&gid=0"

# Lightwork team emails (used to identify which attendees are "ours")
TEAM_EMAILS = {
    "jay@lightworkhome.com",
    "johnny@lightworkhome.com",
    "dom@lightworkhome.com",
    "josh@lightworkhome.com",
}

TEAM_EMAIL_TO_NAME = {
    "jay@lightworkhome.com": "Jay",
    "johnny@lightworkhome.com": "Johnny",
    "dom@lightworkhome.com": "Dom",
    "josh@lightworkhome.com": "Josh",
}

# Owner tabs should always be visible, even if one owner has zero due leads.
OWNER_TAB_ORDER = ["Jay", "Johnny", "Dom", "Unassigned"]

# Sender signatures by owner (used for draft sign-off).
OWNER_SIGNATURE = {
    "Jay": "Jay\nCo-founder | Lightwork Home Health",
    "Johnny": "Johnny Bowman\nGeneral Manager, Lightwork Home Health NY\n310.804.1305",
    "Dom": "Dom Francks\nGeneral Manager, Lightwork Home Health West Coast\n360.951.1330",
    "Josh": "Josh\nLightwork Home Health",
    "Unassigned": "Jay\nCo-founder | Lightwork Home Health",
}


# 7-Touch Follow-Up Cadence
# Key = FU number, Value = (day_offset, type_label, claude_instructions)
CADENCE = {
    1: (1, "Post-call recap + tip",
        "Use this template, inserting a personalized tip from the sales scripts based on the call:\n\n"
        "Hey {first_name},\n\n"
        "Pleasure speaking with you today! Thanks for reaching out.\n\n"
        "A couple followup items: here's an <a href=\"https://www.lightworkhome.com/examplereport\">example report</a> "
        "(password: homehealth), and attached is the deck I presented with additional information.\n\n"
        "Also, since you mentioned {topic from call}, one thing worth noting: {one specific actionable tip "
        "from the sales scripts that matches what they discussed}.\n\n"
        "Let me know if you'd like to move forward with the assessment. And feel free to reach out if you have any questions, "
        "on what I've sent or more generally on home health!\n\n"
        "Best,\n{sender_signature}\n\n"
        "If no relevant topic came up on the call, omit the tip paragraph entirely."),
    2: (3, "Social proof",
        "Use this EXACT template:\n\n"
        "Hey {first_name},\n\n"
        "Hope you're doing well! Just wanted to check if you'd like to move forward with the assessment.\n\n"
        "Also wanted to share this recent write-up that "
        "<a href=\"https://x.com/awilkinson\">Andrew Wilkinson</a> (co-founder of Tiny) "
        "did on our service. I thought you might find it interesting. "
        "Here's <a href=\"https://www.lightworkhome.com/blog-posts/wilkinson\">the link.</a>\n\n"
        "Let me know if you have any questions.\n\n"
        "Best,\n{sender_signature}"),
    3: (6, "Key findings",
        "Use this EXACT template:\n\n"
        "Hey {first_name},\n\n"
        "Hope things are going well. One thing worth mentioning: we find issues in every home we test, "
        "regardless of how new or well-maintained it is. Here are a few recent examples:\n\n"
        "- Hidden mold contamination despite multiple previous professional tests which gave the 'all clear.' "
        "We advised on a specialist manual inspection and coordinated next steps with our recommended inspector for the area.\n"
        "- Elevated trihalomethanes in shower water (which we continue to see in many homes we test). "
        "These can be vaporized in hot shower water and have carcinogenic risks from inhalation and dermal absorption. "
        "We recommended filtration designed to address these specific contaminants.\n"
        "- Significant indoor particulate matter, especially at the ultrafine size, which can penetrate deep into the lungs. "
        "We advised on an optimal filtration strategy as well as a VOC removal strategy to address exposure from recent furnishings.\n"
        "- Significant AC electrical fields impacting all bedrooms, including children's rooms. "
        "We advised on the tradeoffs between kill switches, shielding paint, and other solutions.\n\n"
        "This is why our clients trust us. Our assessments uncover hidden risks and provide clear, "
        "actionable solutions tailored to each home. Shall we schedule a follow-up to go over any questions?\n\n"
        "{sender_signature}"),
    4: (10, "Availability + Reviews",
        "Use this EXACT template:\n\n"
        "Hey {first_name},\n\n"
        "Just wanted to flag that we have some availability in {city} on {available_dates}, "
        "in case that works for you. As a reminder, the total quote was "
        "for {quote_amount} (including {referrer_name}'s {discount_percentage}% discount).\n\n"
        "Below are some of our reviews which gives a nice insight into the value our clients get from the service.\n\n"
        "\"Incredible service. After living in a house that nearly killed me, these guys were literally lifesavers.\" "
        "- Chris Williamson, Host of Modern Wisdom podcast\n\n"
        "\"Lightwork's home health audit was next level. They went deep into our lighting, air, water, EMFs, and more. "
        "We sleep easier knowing our home's health and having fixed the key issues.\" "
        "- Daymond John, Shark Tank Investor\n\n"
        "\"My experience with Lightwork was exceptional. Their team is incredibly knowledgeable and responsive. "
        "What stood out most was their actionable guidance. The results were immediate: I breathe easier, sleep better, "
        "and enjoy greater confidence in my home's health.\" - Matthew Wadiak, Founder of Blue Apron\n\n"
        "\"I genuinely thought I was doing everything right. But Lightwork found serious issues I never would have "
        "discovered on my own. They were incredibly thorough, tested every aspect of my home that could be impacting "
        "my family's health, and then helped us get everything resolved. We made the changes right away. Highly recommend.\" "
        "- Andrew Wilkinson, Co-founder of Tiny\n\n"
        "You can see more of our reviews <a href=\"https://www.lightworkhome.com/reviews\">here</a>.\n\n"
        "Let me know if you have any questions or would like to move forward with the assessment.\n\n"
        "{sender_signature}"),
    5: (16, "Clinical credibility",
        "Use this EXACT template:\n\n"
        "Hey {first_name},\n\n"
        "Wanted to share what a couple of our clinical partners have said about working with us:\n\n"
        "\"We regularly refer our patients to Lightwork Home Health because holistic care requires addressing "
        "the home environment. The science is clear: environmental factors have a major impact on health.\" "
        "- Dr. Robert Kachko, Director of Integrative Health, Atria Institute\n\n"
        "\"Lightwork's home health assessment was incredibly detailed and insightful. I actively recommend their "
        "service to all my patients who want a healthier home and lower environmental toxin exposure.\" "
        "- Dr. David Boyd MD, Concierge Medicine Physician & Founder, Blindspot Medical\n\n"
        "We work closely with physicians and functional medicine practitioners who see environmental "
        "factors as a key piece of their patients' health. Happy to answer any questions.\n\n"
        "{sender_signature}"),
    6: (25, "Graceful close",
        "Last active email. 2 sentences max. 'Just wanted to leave the door open. "
        "We're here whenever you're ready.' Do NOT include a resource or tip. Just be human."),
    7: (90, "3-month check-in",
        "Use this EXACT template:\n\n"
        "Hey {first_name},\n\n"
        "How's it been going with home health? Let me know if you have any questions or anything.\n\n"
        "{sender_signature}"),
}

# Long-term nurture cadence for "Lost" leads (said not interested now).
# Sends a value-only check-in every 60 days, up to 6 touches (1 year).
NURTURE_CADENCE = {
    1: (60, "Nurture check-in",
        "Pure value. Share a relevant article, tip, or resource related to their situation. "
        "No ask. No mention of scheduling. Just 'saw this and thought of you' energy. "
        "Keep it to 2-3 sentences max."),
    2: (120, "Nurture check-in",
        "Share a different resource or seasonal tip. Still zero ask. "
        "Reference their original concern if known (e.g. baby on the way, mold worry, EMF concern)."),
    3: (180, "Nurture check-in",
        "Share a case study, testimonial, or new blog post. "
        "One line like 'hope things are going well' but no pressure."),
    4: (240, "Nurture check-in",
        "Share new content or a relevant update about Lightwork. "
        "Can mention 'happy to chat if anything comes up' but keep it light."),
    5: (300, "Nurture check-in",
        "Value-driven check-in. New resource or seasonal relevance. "
        "Still no hard ask."),
    6: (360, "Nurture final",
        "Last nurture touch. Share one final resource. "
        "'Always here if you need us.' Leave the door open warmly."),
}

# No-show / canceled meeting cadence (rebook-focused, 5 touches)
NO_SHOW_CADENCE = {
    1: (1, "Acknowledge + rebook",
        "Write a short, warm email acknowledging that the call didn't happen. "
        "Reference the specific meeting context (e.g., 'I saw we missed each other for our call' or "
        "'Totally understand things come up'). Do NOT just say 'no worries.' "
        "Make it clear you noticed and you're easy to reschedule with. "
        "Include the booking link: {booking_link}\n\n"
        "Sign off with:\n{sender_signature}\n\n"
        "3-4 sentences max. No guilt, but do acknowledge the cancellation/no-show directly."),
    2: (3, "Presentation deck",
        "Send the presentation deck. Keep it casual and helpful.\n\n"
        "Use this EXACT template:\n\n"
        "Hey {first_name},\n\n"
        "Wanted to send over our presentation deck in case it's helpful. "
        "It covers what we test, how the process works, and what our clients typically find.\n\n"
        "Here's my link if you'd like to rebook a call: {booking_link}\n\n"
        "{sender_signature}"),
    3: (7, "Andrew Wilkinson write-up",
        "Use this EXACT template:\n\n"
        "Hey {first_name},\n\n"
        "Wanted to share this recent write-up that "
        "<a href=\"https://x.com/awilkinson\">Andrew Wilkinson</a> (co-founder of Tiny) "
        "did on our service. Here's <a href=\"https://www.lightworkhome.com/blog-posts/wilkinson\">the link.</a>\n\n"
        "Let me know if you'd like to rebook a call: {booking_link}\n\n"
        "{sender_signature}"),
    4: (14, "Example report",
        "Use this EXACT template:\n\n"
        "Hey {first_name},\n\n"
        "Thought you might find this useful. Here's an "
        "<a href=\"https://www.lightworkhome.com/examplereport\">example report</a> "
        "(password: homehealth) so you can see exactly what we deliver.\n\n"
        "Happy to jump on a call whenever works for you: {booking_link}\n\n"
        "{sender_signature}"),
    5: (30, "Graceful close",
        "Use this EXACT template:\n\n"
        "Hey {first_name},\n\n"
        "Just wanted to leave the door open. We're here whenever you're ready.\n\n"
        "{sender_signature}"),
}

# Owner booking links (Cal.com)
OWNER_BOOKING_LINK = {
    "Jay": "https://cal.com/lightworkhome/lightwork-home-health-test-call",
    "Johnny": "https://cal.com/lightworkhome/lightwork-home-health-nyc-testing-call",
    "Dom": "https://cal.com/lightworkhomesf/lightwork-home-health-test-call",
    "Josh": "https://cal.com/josh-ruben-1q4w0b/lightwork-home-health-test-call",
}

# Lead statuses that should be excluded from the digest entirely
SKIP_LEAD_STATUSES = {"unqualified", "not interested"}

# Opportunity statuses that put a lead into nurture instead of active cadence
NURTURE_OPP_STATUSES = {"lost"}


def _get_cadence(cadence_type):
    """Return the cadence dict for the given cadence type."""
    if cadence_type == "nurture":
        return NURTURE_CADENCE
    if cadence_type == "no_show":
        return NO_SHOW_CADENCE
    return CADENCE

# Only process leads whose first call was within this many days
CADENCE_LOOKBACK_DAYS = 45

# Max leads per owner per digest (prevents backlog flood on first run)
MAX_LEADS_PER_OWNER = 20

# Meeting matching
MATCH_THRESHOLD = 5  # minimum score to consider a transcript match valid

# Transcript/prompt size caps
TRANSCRIPT_CAP_SHEET = 6000
TRANSCRIPT_CAP_LOCAL = 4000
SENT_EMAIL_BODY_CAP = 1000

# Anthropic model (easy to swap back to haiku if needed)
ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"

# Network
HTTP_TIMEOUT_SECONDS = 30
RETRY_BACKOFF_FACTOR = 5
OAUTH_TIMEOUT_SECONDS = 180



def load_env(path):
    """Read a .env file into os.environ."""
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
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
SKIP_CLAUDE = os.environ.get("SKIP_CLAUDE", "").strip() == "1"
GRANOLA_CACHE = os.environ.get(
    "GRANOLA_CACHE",
    str(Path.home() / "Library/Application Support/Granola/cache-v3.json"),
)

# Granola MCP (OAuth + JSON-RPC over HTTP)
GRANOLA_MCP_URL = os.environ.get("GRANOLA_MCP_URL", "https://mcp.granola.ai/mcp")
GRANOLA_OAUTH_METADATA_URL = os.environ.get(
    "GRANOLA_OAUTH_METADATA_URL",
    "https://mcp.granola.ai/.well-known/oauth-authorization-server",
)
GRANOLA_MCP_TOKEN_PATH = Path(
    os.environ.get(
        "GRANOLA_MCP_TOKEN_PATH",
        str(Path.home() / ".config/lightwork-digest/granola_mcp_token.json"),
    )
)
GRANOLA_MCP_ENABLE = os.environ.get("GRANOLA_MCP_ENABLE", "").strip() == "1"

# SMTP (Gmail) for sending follow-up reminders to team members
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

# Map owner name -> email for sending reminders (inverse of TEAM_EMAIL_TO_NAME)
OWNER_TO_EMAIL = {v: k for k, v in TEAM_EMAIL_TO_NAME.items()}

# SQLite draft cache
DRAFT_DB_PATH = SCRIPT_DIR / "drafts.db"

# SQLite transcript cache (persistent across runs, avoids re-fetching from MCP)
TRANSCRIPT_DB_PATH = SCRIPT_DIR / "transcripts.db"

# AI provider
# - anthropic: existing behavior (default)
# - openai: use OpenAI Responses API (e.g. gpt-5-codex)
AI_PROVIDER = (os.environ.get("AI_PROVIDER", "anthropic") or "anthropic").strip().lower()
OPENAI_API_URL = os.environ.get("OPENAI_API_URL", "https://api.openai.com/v1/responses")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-codex")
OPENAI_REASONING_EFFORT = (os.environ.get("OPENAI_REASONING_EFFORT", "low") or "low").strip().lower()

# Link policy: avoid hallucinated links in outreach emails.
# Allowed set can be overridden via ALLOWED_URL_PREFIXES (comma-separated).
# Otherwise, it is derived from known-good reference docs (sales scripts + examples).
ALLOWED_URL_PREFIXES_ENV = os.environ.get("ALLOWED_URL_PREFIXES", "") or ""
DEFAULT_ALLOWED_URL_PREFIXES = [
    "https://www.lightworkhome.com/examplereport",
    "http://www.lightworkhome.com/examplereport",
    "https://www.lightworkhome.com/blog-posts/wilkinson",
    "https://www.lightworkhome.com/blog-posts/the-science-behind-lightwork",
    "https://www.lightworkhome.com/reviews",
]


def _extract_urls_for_allowlist(text: str) -> list:
    if not text:
        return []
    urls = re.findall(r"https?://[^\s<>()\"']+", text)
    out = []
    seen = set()
    for u in urls:
        u = u.strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _build_allowed_url_prefixes() -> list:
    # Priority 1: explicit env allowlist.
    if ALLOWED_URL_PREFIXES_ENV.strip():
        return [s.strip() for s in ALLOWED_URL_PREFIXES_ENV.split(",") if s.strip()]

    # Priority 2: derive from known reference docs (plus defaults).
    prefixes = []
    seen = set()
    for u in DEFAULT_ALLOWED_URL_PREFIXES:
        if u not in seen:
            seen.add(u)
            prefixes.append(u)

    ref_paths = [
        SCRIPT_DIR / "reference" / "sales-scripts.md",
        SCRIPT_DIR / "reference" / "follow-up-examples.md",
        SCRIPT_DIR / "reference" / "sales-tips-condensed.md",
        SCRIPT_DIR / "reference" / "voice-guide-condensed.md",
        Path("/Users/dillandevram/Downloads/[MOST RECENT] [2509] Sales Scripts (1).md"),
    ]
    for p in ref_paths:
        try:
            txt = p.read_text()
        except Exception:
            continue
        for u in _extract_urls_for_allowlist(txt):
            if u not in seen:
                seen.add(u)
                prefixes.append(u)

    return prefixes


ALLOWED_URL_PREFIXES = _build_allowed_url_prefixes()

# Message quality (anti "AI slop") and safety checks.
FORBIDDEN_PHRASES = [
    "just checking in",
    "circling back",
    "touching base",
    "reaching out to",
    "hope this finds you well",
    "per my last email",
]

# Keep this list short and high-signal; it triggers a rewrite if found.
FORBIDDEN_TONE_MARKERS = [
    "as an ai",
    "i can't",
    "i cannot",
    "i don't have access",
]

MAX_EXCLAMATIONS = 1
# These are soft caps; if exceeded we rewrite to be tighter.
MAX_SENTENCES_BY_FU = {1: 8, 2: 6, 3: 15, 4: 20, 5: 6, 6: 3, 7: 3}
MAX_WORDS_BY_FU = {1: 170, 2: 120, 3: 300, 4: 400, 5: 130, 6: 60, 7: 40}
NO_SHOW_MAX_SENTENCES_BY_FU = {1: 4, 2: 5, 3: 5, 4: 5, 5: 2}
NO_SHOW_MAX_WORDS_BY_FU = {1: 80, 2: 80, 3: 80, 4: 80, 5: 30}

# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------


def _request(method, url, headers=None, body=None, basic_auth=None):
    """Make an HTTP request, return parsed JSON.

    SAFETY: All requests to Close.com are forced to GET.
    This prevents any code path from creating, updating, or sending
    anything through Close (including emails to leads).
    """
    # Block any write operation to Close.com
    if "api.close.com" in url and method.upper() != "GET":
        raise RuntimeError(
            f"SAFETY: Blocked {method} request to Close.com. "
            f"This script is read-only. URL: {url}"
        )

    if headers is None:
        headers = {}
    if body is not None and isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")

    auth_header = None
    if basic_auth:
        cred = base64.b64encode(f"{basic_auth[0]}:{basic_auth[1]}".encode()).decode()
        auth_header = f"Basic {cred}"

    ctx = ssl.create_default_context()
    transient_codes = {400, 408, 425, 429, 500, 502, 503, 504}
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        if auth_header:
            req.add_header("Authorization", auth_header)
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else ""
            retryable = e.code in transient_codes
            if retryable and attempt < max_attempts:
                time.sleep(RETRY_BACKOFF_FACTOR * attempt)
                continue
            print(f"HTTP {e.code} for {url}: {error_body[:300]}")
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionResetError,
                ssl.SSLError, http.client.IncompleteRead):
            if attempt < max_attempts:
                time.sleep(RETRY_BACKOFF_FACTOR * attempt)
                continue
            raise


def _extract_urls(text: str) -> list:
    if not text:
        return []
    urls = re.findall(r"https?://[^\s<>()\"']+", text)
    # Also capture <a href="...">
    urls += re.findall(r'href="(https?://[^"]+)"', text)
    # Normalize + de-dupe preserving order
    out = []
    seen = set()
    for u in urls:
        u = u.strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _sentence_count(text: str) -> int:
    s = (text or "").strip()
    if not s:
        return 0
    # Approx: count ., ?, ! at end of clauses.
    parts = re.split(r"[.!?]+(?:\s+|$)", s)
    return len([p for p in parts if p.strip()])


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


def _lint_email_draft(draft: str, fu_number: int, days_since_call: int | None, prior_emails: list | None = None, cadence_type: str = "active") -> list:
    issues = []
    d = (draft or "").strip()
    dl = d.lower()

    # Forbidden phrases (AI/corporate slop)
    for p in FORBIDDEN_PHRASES:
        if p in dl:
            issues.append(f'Forbidden phrase: "{p}"')

    for p in FORBIDDEN_TONE_MARKERS:
        if p in dl:
            issues.append(f'Forbidden tone marker: "{p}"')

    # No assumptions: "today/yesterday" only if the call is actually recent.
    if days_since_call is not None and days_since_call > 2:
        if "today" in dl or "yesterday" in dl:
            issues.append('Avoid "today/yesterday" when the call was not recent')

    # Link allowlist
    urls = _extract_urls(d)
    disallowed = []
    for u in urls:
        if not any(u.startswith(pfx) for pfx in ALLOWED_URL_PREFIXES):
            disallowed.append(u)
    if disallowed:
        issues.append("Disallowed link(s): " + ", ".join(disallowed[:5]))

    # Tightness
    if cadence_type == "no_show":
        max_sent = NO_SHOW_MAX_SENTENCES_BY_FU.get(int(fu_number), 4)
        max_words = NO_SHOW_MAX_WORDS_BY_FU.get(int(fu_number), 80)
    else:
        max_sent = MAX_SENTENCES_BY_FU.get(int(fu_number), 6)
        max_words = MAX_WORDS_BY_FU.get(int(fu_number), 150)
    sc = _sentence_count(d)
    wc = _word_count(d)
    if sc > max_sent:
        issues.append(f"Too many sentences ({sc} > {max_sent})")
    if wc > max_words:
        issues.append(f"Too long ({wc} words > {max_words})")

    if d.count("!") > MAX_EXCLAMATIONS:
        issues.append(f"Too many exclamation points (> {MAX_EXCLAMATIONS})")

    # Em dashes (U+2014) are forbidden per style guide
    if "\u2014" in d:
        issues.append("Contains em dash(es). Replace with commas, periods, or semicolons.")

    # Duplicate resources: flag URLs/resources already sent in prior emails
    if prior_emails:
        prior_urls = set()
        prior_text_lower = ""
        for em in prior_emails:
            body = (em.get("body") or "")
            prior_urls.update(u.lower().rstrip("/") for u in _extract_urls(body))
            prior_text_lower += " " + body.lower()
        draft_urls = _extract_urls(d)
        for u in draft_urls:
            if u.lower().rstrip("/") in prior_urls:
                issues.append(f"Duplicate resource already sent in a prior email: {u}")
        # Also check for named resources mentioned by keyword in prior emails
        named_resources = {
            "wilkinson": "Wilkinson write-up",
            "examplereport": "example report",
            "the-science-behind-lightwork": "science video",
        }
        for keyword, label in named_resources.items():
            if keyword in dl and keyword in prior_text_lower:
                issues.append(f'Duplicate resource: "{label}" was already shared in a prior email. Use a different resource.')

    return issues


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def _save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))
    # Restrict token files to owner-only access
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _granola_oauth_metadata():
    return _request("GET", GRANOLA_OAUTH_METADATA_URL)


def _granola_dynamic_register(meta: dict, redirect_uri: str) -> dict:
    """Dynamic client registration. Stores a public client (no secret)."""
    reg_endpoint = meta.get("registration_endpoint")
    if not reg_endpoint:
        raise RuntimeError("Granola OAuth metadata missing registration_endpoint")
    body = {
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "client_name": "lightwork-digest",
    }
    return _request("POST", reg_endpoint, body=body)


def _granola_oauth_authorize_and_token(meta: dict) -> dict:
    """Interactive OAuth login via localhost redirect, using PKCE."""
    auth_endpoint = meta["authorization_endpoint"]
    token_endpoint = meta["token_endpoint"]

    # Bind an ephemeral localhost port.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    # Load or register client for this redirect_uri.
    token_state = _load_json(GRANOLA_MCP_TOKEN_PATH) or {}
    client = token_state.get("client") or {}
    if client.get("redirect_uri") != redirect_uri or not client.get("client_id"):
        reg = _granola_dynamic_register(meta, redirect_uri)
        client = {
            "client_id": reg.get("client_id"),
            "redirect_uri": redirect_uri,
        }
        token_state["client"] = client
        _save_json(GRANOLA_MCP_TOKEN_PATH, token_state)

    client_id = client["client_id"]

    # PKCE
    code_verifier = _b64url(secrets.token_bytes(32))
    code_challenge = _b64url(hashlib.sha256(code_verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(16))

    scopes = "openid profile email offline_access"
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    url = auth_endpoint + "?" + urllib.parse.urlencode(params)

    # Minimal localhost callback server. We must handle extra browser requests
    # (e.g. /favicon.ico) without prematurely stopping.
    result = {"code": None, "state": None, "error": None}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(parsed.query)
            code = (q.get("code") or [None])[0]
            st = (q.get("state") or [None])[0]
            err = (q.get("error") or [None])[0]

            if err:
                result["error"] = err
                done.set()
            elif code and st:
                result["code"] = code
                result["state"] = st
                done.set()

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if done.is_set():
                self.wfile.write(
                    b"<html><body><h2>Granola connected.</h2><p>You can close this tab and return to the terminal.</p></body></html>"
                )
            else:
                self.wfile.write(
                    b"<html><body><p>Waiting for Granola authorization...</p></body></html>"
                )

        def log_message(self, fmt, *args):
            return

    httpd = http.server.HTTPServer(("127.0.0.1", port), Handler)

    def serve():
        httpd.serve_forever(poll_interval=0.2)

    t = threading.Thread(target=serve, daemon=True)
    t.start()

    print("\nGranola MCP login required.")
    print("Opening browser for Granola authorization...")
    subprocess.run(["open", url])

    if not done.wait(timeout=OAUTH_TIMEOUT_SECONDS):
        try:
            httpd.shutdown()
        except Exception:
            pass
        httpd.server_close()
        raise RuntimeError("Granola OAuth timed out waiting for browser callback")

    try:
        httpd.shutdown()
    except Exception:
        pass
    httpd.server_close()
    t.join(timeout=5)

    if result["error"]:
        raise RuntimeError(f"Granola OAuth error: {result['error']}")
    if result["state"] != state or not result["code"]:
        raise RuntimeError("Granola OAuth callback missing code or state mismatch")

    token_resp = _request(
        "POST",
        token_endpoint,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=urllib.parse.urlencode(
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": result["code"],
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            }
        ).encode(),
    )
    # Persist tokens
    now = int(time.time())
    token_state["tokens"] = {
        "access_token": token_resp.get("access_token"),
        "refresh_token": token_resp.get("refresh_token"),
        "expires_at": now + int(token_resp.get("expires_in") or 0),
        "scope": token_resp.get("scope"),
        "token_type": token_resp.get("token_type"),
    }
    _save_json(GRANOLA_MCP_TOKEN_PATH, token_state)
    return token_state


def _granola_refresh_token(meta: dict, token_state: dict) -> dict:
    token_endpoint = meta["token_endpoint"]
    client = token_state.get("client") or {}
    tokens = token_state.get("tokens") or {}
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise RuntimeError("No Granola refresh_token available; re-auth required")
    resp = _request(
        "POST",
        token_endpoint,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "client_id": client.get("client_id"),
                "refresh_token": refresh,
            }
        ).encode(),
    )
    now = int(time.time())
    tokens["access_token"] = resp.get("access_token")
    if resp.get("refresh_token"):
        tokens["refresh_token"] = resp.get("refresh_token")
    tokens["expires_at"] = now + int(resp.get("expires_in") or 0)
    token_state["tokens"] = tokens
    _save_json(GRANOLA_MCP_TOKEN_PATH, token_state)
    return token_state


class GranolaMCPClient:
    def __init__(self, enable_interactive_login: bool = True):
        self.enable_interactive_login = enable_interactive_login
        self.meta = _granola_oauth_metadata()
        self.session_id = None
        self.token_state = _load_json(GRANOLA_MCP_TOKEN_PATH) or {}

    def _access_token(self) -> str:
        tokens = (self.token_state or {}).get("tokens") or {}
        tok = tokens.get("access_token")
        exp = tokens.get("expires_at") or 0
        if tok and exp and exp - int(time.time()) > 30:
            return tok
        # Refresh if possible
        if tokens.get("refresh_token"):
            self.token_state = _granola_refresh_token(self.meta, self.token_state)
            return (self.token_state.get("tokens") or {}).get("access_token")
        if not self.enable_interactive_login:
            raise RuntimeError("Granola MCP not authenticated. Set GRANOLA_MCP_ENABLE=1 and run again to login.")
        self.token_state = _granola_oauth_authorize_and_token(self.meta)
        return (self.token_state.get("tokens") or {}).get("access_token")

    def _post(self, payload: dict) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._access_token()}",
            "MCP-Protocol-Version": "2024-11-05",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["MCP-Session-Id"] = self.session_id
        req = urllib.request.Request(
            GRANOLA_MCP_URL,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            # Granola uses MCP-Session-Id header for streamable HTTP sessions.
            sid = resp.headers.get("MCP-Session-Id")
            if sid:
                self.session_id = sid
            ct = (resp.headers.get("Content-Type") or "").lower()
            body = resp.read().decode("utf-8", errors="replace")
            if "text/event-stream" in ct:
                # Streamable HTTP transport. Granola returns SSE frames like:
                # event: message\n data: {...}\n\n
                last = None
                for line in body.splitlines():
                    line = line.strip()
                    if line.startswith("data:"):
                        data = line[len("data:"):].strip()
                        if data:
                            last = json.loads(data)
                if last is None:
                    raise RuntimeError(f"MCP SSE response had no data frames: {body[:200]}")
                return last
            return json.loads(body)

    def initialize(self):
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "lightwork-digest", "version": "0.1"},
                "capabilities": {},
            },
        }
        resp = self._post(payload)
        if "error" in resp:
            raise RuntimeError(f"MCP initialize error: {resp['error']}")
        return resp.get("result")

    def tools_list(self):
        payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        resp = self._post(payload)
        if "error" in resp:
            raise RuntimeError(f"MCP tools/list error: {resp['error']}")
        return resp.get("result", {}).get("tools", [])

    def tools_call(self, name: str, arguments: dict):
        payload = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        resp = self._post(payload)
        if "error" in resp:
            raise RuntimeError(f"MCP tools/call error: {resp['error']}")
        return resp.get("result")


# ---------------------------------------------------------------------------
# Close.com
# ---------------------------------------------------------------------------


def close_get(endpoint, params=None):
    """GET from Close.com API with rate-limit protection.

    SAFETY: This is the ONLY Close.com function. The script is read-only.
    There is no close_post/close_put/close_delete. Do NOT add one.
    Writing to Close.com could send emails to leads.
    """
    url = f"https://api.close.com/api/v1{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    time.sleep(0.25)  # ~4 req/s to stay under Close.com rate limits
    return _request("GET", url, basic_auth=(CLOSE_API_KEY, ""))


def _fetch_all_meetings(since_date, until_date=None):
    """Fetch ALL meetings from Close.com within a date range (any status).

    This is the single source of meeting data. Other functions filter
    the result by status (completed, no-show, etc.) to avoid duplicate
    API calls.
    """
    now = datetime.now(timezone.utc)
    if until_date is None:
        until_date = now

    all_meetings = []
    has_more = True
    skip = 0

    while has_more:
        data = close_get(
            "/activity/meeting/",
            {
                "date_created__gte": (since_date - timedelta(days=7)).strftime(
                    "%Y-%m-%dT%H:%M:%S+00:00"
                ),
                "_limit": 100,
                "_skip": skip,
            },
        )
        meetings = data.get("data", [])
        all_meetings.extend(meetings)
        has_more = data.get("has_more", False)
        skip += len(meetings)

    return all_meetings


def _filter_meetings_in_range(all_meetings, since_date, until_date, statuses=None):
    """Filter meetings by date range and optional status set.

    Args:
        all_meetings: raw meeting list from _fetch_all_meetings()
        since_date: datetime, start of range (inclusive)
        until_date: datetime, end of range (exclusive)
        statuses: set of lowercase status strings to include, or None for all
    """
    result = []
    for m in all_meetings:
        if statuses is not None:
            status = (m.get("status") or "").lower().strip()
            if status not in statuses:
                continue
        starts_at = m.get("starts_at", "")
        if not starts_at:
            continue
        try:
            start_dt = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if since_date <= start_dt < until_date:
            result.append(m)
    return result


def get_meetings_in_range(since_date, until_date=None, all_meetings=None):
    """Pull completed meetings from Close.com within a date range.

    Args:
        since_date: datetime, start of range (inclusive)
        until_date: datetime, end of range (exclusive). Defaults to now.
        all_meetings: pre-fetched meeting list (skips API call if provided)

    Returns list of completed meeting dicts in [since_date, until_date).
    """
    if until_date is None:
        until_date = datetime.now(timezone.utc)
    if all_meetings is None:
        all_meetings = _fetch_all_meetings(since_date, until_date)
    return _filter_meetings_in_range(all_meetings, since_date, until_date, {"completed"})


def get_recent_customer_leads(all_meetings=None):
    """Get Customer Leads who had calls in the last CADENCE_LOOKBACK_DAYS days.

    Args:
        all_meetings: pre-fetched meeting list (skips API call if provided)

    Returns:
        dict: {lead_id: {"lead_info": dict, "first_call_date": datetime,
                         "meetings": [list], "owner_name": str}}
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=CADENCE_LOOKBACK_DAYS)

    print(f"Fetching meetings from last {CADENCE_LOOKBACK_DAYS} days...")
    meetings = get_meetings_in_range(since, all_meetings=all_meetings)
    print(f"  Found {len(meetings)} completed meetings")

    # Also fetch no-show meetings so leads with only no-shows are included
    no_show_meetings = _filter_meetings_in_range(
        all_meetings if all_meetings is not None else _fetch_all_meetings(since, now),
        since, now, NO_SHOW_STATUSES
    )
    print(f"  Found {len(no_show_meetings)} no-show/canceled meetings")
    all_relevant = meetings + no_show_meetings

    # Group by lead_id, track earliest meeting
    leads = {}  # lead_id -> {meetings, earliest_start, owner_id}
    for m in all_relevant:
        lead_id = m.get("lead_id", "")
        if not lead_id:
            continue

        starts_at = m.get("starts_at", "")
        try:
            start_dt = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue

        if lead_id not in leads:
            leads[lead_id] = {
                "meetings": [],
                "earliest_start": start_dt,
            }

        leads[lead_id]["meetings"].append(m)
        if start_dt < leads[lead_id]["earliest_start"]:
            leads[lead_id]["earliest_start"] = start_dt

    print(f"  {len(leads)} unique leads with meetings")

    # Filter to Customer Lead category
    result = {}
    for lead_id, info in leads.items():
        try:
            lead_details = get_lead_details(lead_id)
        except Exception as e:
            print(f"  Error fetching lead {lead_id}: {e}")
            continue

        category = lead_details.get("custom", {}).get("Category", [])
        if "Customer Lead" not in category:
            continue

        display_name = (lead_details.get("display_name") or "").lower()
        if "testing" in display_name:
            continue

        # Skip leads with unqualified/not interested status
        lead_status = (lead_details.get("status_label") or "").lower().strip()
        if lead_status in SKIP_LEAD_STATUSES:
            print(f"  Skipping {lead_details.get('display_name', '')} (lead status: {lead_status})")
            continue

        # Check opportunity status
        opp_status = _get_lead_opp_status(lead_id)
        if opp_status == "won":
            print(f"  Skipping {lead_details.get('display_name', '')} (won opportunity)")
            continue

        # Owner = the team member who attended the call
        owner_name = _get_owner_from_meetings(info["meetings"])

        result[lead_id] = {
            "lead_info": lead_details,
            "first_call_date": info["earliest_start"],
            "meetings": info["meetings"],
            "owner_name": owner_name,
            "cadence_type": "nurture" if opp_status == "nurture" else "active",
        }

    print(f"  {len(result)} are Customer Leads")
    return result


NO_SHOW_STATUSES = {"canceled", "declined-by-lead", "no-show", "noshow"}


def get_no_show_lead_ids(since_date, until_date=None, all_meetings=None):
    """Return lead_ids with at least one no-show style meeting in range.

    Args:
        all_meetings: pre-fetched meeting list (skips API call if provided)
    """
    if until_date is None:
        until_date = datetime.now(timezone.utc)
    if all_meetings is None:
        all_meetings = _fetch_all_meetings(since_date, until_date)

    no_show_meetings = _filter_meetings_in_range(
        all_meetings, since_date, until_date, NO_SHOW_STATUSES
    )
    return {m.get("lead_id", "") for m in no_show_meetings if m.get("lead_id")}


def get_lead_details(lead_id):
    """Fetch lead info from Close.com."""
    return close_get(f"/lead/{lead_id}/")


def _get_owner_from_meetings(meetings):
    """Determine lead owner from which team member attended the call."""
    for m in meetings:
        for a in m.get("attendees", []):
            email = (a.get("email") or "").lower()
            if email in TEAM_EMAIL_TO_NAME:
                return TEAM_EMAIL_TO_NAME[email]
    return "Unassigned"


# Opportunity statuses that mean the lead already bought the service.
# These are the "won" type statuses from the Close.com pipeline.
WON_OPP_STATUSES = {
    "booked assessment", "test completed", "report completed", "won",
    "free test booked", "free test completed", "referred first lead",
}


def _get_lead_opp_status(lead_id):
    """Check lead's opportunity status. Returns one of: 'won', 'nurture', 'active'."""
    # Use a higher limit so we don't miss an older "won" stage due to pagination.
    data = close_get("/opportunity/", {"lead_id": lead_id, "_limit": 100})
    for opp in data.get("data", []):
        status = (opp.get("status_label") or "").lower().strip()
        status_type = (opp.get("status_type") or "").lower().strip()
        # Close includes a normalized status_type ("won"/"lost"/"active") in many accounts.
        # Treat any won opportunity as "won" regardless of the label spelling.
        if status_type == "won" or status in WON_OPP_STATUSES:
            return "won"
        if status_type == "lost" or status in NURTURE_OPP_STATUSES:
            return "nurture"
    return "active"


def get_close_users():
    """Map user_id -> display name."""
    data = close_get("/user/")
    mapping = {}
    for u in data.get("data", []):
        uid = u.get("id", "")
        name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
        mapping[uid] = name
    return mapping


def get_followup_history(lead_id, first_call_date, debug=False):
    """Count follow-ups sent to a lead after first_call_date.

    A follow-up = a distinct outgoing email on a unique calendar day within
    a thread. Multiple emails on the same day in the same thread (e.g.
    Close.com quoted-reply artifacts) count as ONE follow-up. Emails on
    different days, even in the same thread, count as separate follow-ups.

    Returns (fu_count, email_summaries) where:
      - fu_count: number of distinct follow-ups (unique thread+day combos)
      - email_summaries: list of dicts with subject/body for the latest
        email per day per thread, sorted by date (for Claude context)
    """
    after_str = first_call_date.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    has_more = True
    skip = 0

    # Group by thread (normalized subject), then by calendar day within each
    # thread. Keep the latest email per day per thread.
    # threads: normalized_subject -> {day_str -> {subject, body, date}}
    threads = {}
    all_outgoing = []
    while has_more:
        data = close_get(
            "/activity/email/",
            {
                "lead_id": lead_id,
                "date_created__gte": after_str,
                "_limit": 100,
                "_skip": skip,
            },
        )
        emails = data.get("data", [])
        for e in emails:
            if e.get("direction") == "outgoing":
                subject = (e.get("subject") or "(no subject)").strip()
                if debug:
                    all_outgoing.append((e.get("date_created", ""), subject))
                subj_lower = subject.lower()
                email_date = e.get("date_created", "")
                email_day = email_date[:10]
                call_day = first_call_date.strftime("%Y-%m-%d")
                # Skip assessment emails always
                if "assessment" in subj_lower:
                    if debug:
                        print(f"    [SKIP] {subject} (contains 'assessment')")
                    continue
                # Skip the original scheduling/booking confirmation, but
                # count replies (Re:) in the same thread as follow-ups.
                # The team often replies to the booking thread with their
                # actual follow-up message.
                is_scheduling_thread = any(
                    p in subj_lower for p in (
                        "test call between", "testing call between",
                        "partner call between", "intro call between",
                    )
                )
                is_reply = subj_lower.startswith("re:") or subj_lower.startswith("fwd:")
                if is_scheduling_thread and not is_reply:
                    if debug:
                        print(f"    [SKIP] {subject} (original scheduling email)")
                    continue
                norm = _normalize_subject(subject)
                day_key = email_date[:10]  # "2026-01-21"
                if norm not in threads:
                    threads[norm] = {}
                existing = threads[norm].get(day_key)
                if existing is None or email_date > existing.get("date", ""):
                    body = (e.get("body_text") or e.get("body_text_quoted") or "").strip()
                    if len(body) > SENT_EMAIL_BODY_CAP:
                        body = body[:SENT_EMAIL_BODY_CAP] + "..."
                    threads[norm][day_key] = {"subject": subject, "body": body, "date": email_date}
        has_more = data.get("has_more", False)
        skip += len(emails)

    # Count: total distinct days across all threads
    fu_count = sum(len(days) for days in threads.values())

    # Flatten all day-latest emails, sorted by date (for Claude context)
    all_emails = []
    for days in threads.values():
        all_emails.extend(days.values())
    all_emails.sort(key=lambda x: x["date"])

    if debug:
        print(f"    First call date: {first_call_date.strftime('%Y-%m-%d')}")
        print(f"    Total outgoing emails after first call: {len(all_outgoing)}")
        for date, subj in sorted(all_outgoing):
            print(f"      {date[:10]} | {subj[:80]}")
        print(f"    Threads: {len(threads)}, Distinct follow-ups (thread+day): {fu_count}")
        for norm, days in threads.items():
            for day, t in sorted(days.items()):
                print(f"      [{norm[:40]}] {day} -> {t['subject'][:50]}")

    return fu_count, all_emails


def _normalize_subject(subject):
    """Strip Re:/Fwd:/[INT] prefixes to get the root thread subject."""
    s = subject.strip()
    # Repeatedly strip leading Re:, Fwd:, RE:, FW:, [INT], .Re: etc.
    while True:
        prev = s
        s = re.sub(r'^(Re:\s*|Fwd:\s*|FW:\s*|RE:\s*|\[INT\]\s*|\.Re:\s*)', '', s, flags=re.IGNORECASE).strip()
        if s == prev:
            break
    return s.lower()


def get_leads_due_today(customer_leads, debug_lead_name=""):
    """Check follow-up status for all customer leads.

    Returns:
        (due_leads, all_leads_status) where:
        - due_leads: list of leads due today/overdue (with sent_emails for Claude)
        - all_leads_status: list of ALL leads with their FU status (for tracker view)
    """
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    due_leads = []
    all_leads_status = []

    for lead_id, info in customer_leads.items():
        first_call = info["first_call_date"]
        lead_info = info["lead_info"]
        lead_name = lead_info.get("display_name", "Unknown")
        cadence_type = info.get("cadence_type", "active")
        transcript_label = info.get("transcript_label", "No")
        mcp_meeting_id = info.get("mcp_meeting_id")
        no_show = bool(info.get("no_show"))

        # Pick the right cadence
        cadence = _get_cadence(cadence_type)
        max_touches = len(cadence)
        label = "nurture" if cadence_type == "nurture" else ("rebook" if cadence_type == "no_show" else "FU")

        debug = bool(debug_lead_name and debug_lead_name.lower() in lead_name.lower())
        if debug:
            print(f"\n  === DEBUG: {lead_name} ===")
        fu_done, sent_emails = get_followup_history(lead_id, first_call, debug=debug)
        next_fu = fu_done + 1

        # Calculate due date and overdue status
        days_overdue = 0
        due_date = None
        if next_fu <= max_touches:
            day_offset = cadence[next_fu][0]
            due_date = (first_call + timedelta(days=day_offset)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            days_overdue = (today - due_date).days

        # Add to tracker (all leads)
        all_leads_status.append({
            "lead_id": lead_id,
            "lead_info": lead_info,
            "first_call_date": first_call,
            "fu_done": fu_done,
            "next_fu": next_fu,
            "due_date": due_date,
            "days_overdue": days_overdue,
            "owner_name": info["owner_name"],
            "cadence_type": cadence_type,
            "max_touches": max_touches,
            "transcript_label": transcript_label,
            "mcp_meeting_id": mcp_meeting_id,
            "no_show": no_show,
        })

        if next_fu > max_touches:
            print(f"  {lead_name}: {fu_done}/{max_touches} {label}s done (completed)")
            continue

        if days_overdue >= 0:  # due today or overdue
            due_leads.append({
                "lead_id": lead_id,
                "lead_info": lead_info,
                "first_call_date": first_call,
                "fu_done": fu_done,
                "sent_emails": sent_emails,
                "next_fu": next_fu,
                "due_date": due_date,
                "days_overdue": days_overdue,
                "meetings": info["meetings"],
                "owner_name": info["owner_name"],
                "cadence_type": cadence_type,
                "mcp_meeting_id": mcp_meeting_id,
                "no_show": no_show,
            })
            status = "DUE TODAY" if days_overdue == 0 else f"OVERDUE by {days_overdue}d"
            tag = " [NURTURE]" if cadence_type == "nurture" else (" [NO-SHOW]" if cadence_type == "no_show" else "")
            print(f"  {lead_name}: {label} {next_fu}/{max_touches} ({status}){tag}")
        else:
            tag = " [NURTURE]" if cadence_type == "nurture" else (" [NO-SHOW]" if cadence_type == "no_show" else "")
            print(f"  {lead_name}: {label} {next_fu}/{max_touches} due in {-days_overdue}d{tag}")

    # Sort: most overdue first so the cap doesn't drop urgent leads
    due_leads.sort(key=lambda x: x["days_overdue"], reverse=True)
    return due_leads, all_leads_status




# ---------------------------------------------------------------------------
# Granola Google Sheet (primary transcript source)
# ---------------------------------------------------------------------------


def load_granola_sheet():
    """Load call transcripts from the shared Granola Google Sheet.

    Returns a list of dicts with keys: Title, Attendees, Notes, Time, Transcript.
    """
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(GRANOLA_SHEET_URL)
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            text = resp.read().decode("utf-8")
    except Exception as e:
        print(f"  Warning: Could not load Granola sheet: {e}")
        return []

    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    return rows


def _get_close_meeting_context(meeting):
    """Extract non-team attendee emails, title, and date from a Close meeting."""
    close_attendees = set()
    for a in meeting.get("attendees", []):
        email = (a.get("email") or "").lower()
        if email and email not in TEAM_EMAILS:
            close_attendees.add(email)
    title = (meeting.get("title") or "").lower().strip()
    date_str = (meeting.get("date_created") or meeting.get("starts_at") or "")[:10]
    return close_attendees, title, date_str


def _score_meeting_match(close_attendees, close_title, close_date,
                         candidate_emails, candidate_title, candidate_date):
    """Score how well a candidate meeting matches a Close meeting.

    Scoring:
        +10 per overlapping non-team email
        +8 exact title match
        +5 partial title match (one contains the other)
        +2 same-date match
    """
    score = 0

    # Email overlap
    overlap = close_attendees & candidate_emails
    if overlap:
        score += 10 * len(overlap)

    # Title similarity
    if close_title and candidate_title:
        if close_title == candidate_title:
            score += 8
        elif close_title in candidate_title or candidate_title in close_title:
            score += 5

    # Date proximity
    if close_date and candidate_date and close_date == candidate_date:
        score += 2

    return score


def match_granola_sheet(meeting, sheet_rows):
    """Match a Close.com meeting to a Granola Sheet row.

    Match by attendee email overlap, title match, and date proximity.
    Returns the row dict if matched, None otherwise.
    """
    close_attendees, close_title, close_date = _get_close_meeting_context(meeting)

    best_match = None
    best_score = 0

    for row in sheet_rows:
        row_attendees = set()
        for email in (row.get("Attendees") or "").split(","):
            email = email.strip().lower()
            if email:
                row_attendees.add(email)

        row_title = (row.get("Title") or "").lower().strip()
        row_date = (row.get("Time") or "")[:10]

        score = _score_meeting_match(
            close_attendees, close_title, close_date,
            row_attendees, row_title, row_date,
        )

        if score > best_score:
            best_score = score
            best_match = row

    if best_score >= MATCH_THRESHOLD:
        return best_match
    return None


def extract_sheet_notes(row):
    """Extract notes + transcript text from a Google Sheet row."""
    if not row:
        return ""

    parts = []
    notes = (row.get("Notes") or "").strip()
    transcript = (row.get("Transcript") or "").strip()

    if notes:
        parts.append("MEETING NOTES:\n" + notes)

    if transcript:
        # Cap transcript to stay within prompt limits
        if len(transcript) > TRANSCRIPT_CAP_SHEET:
            transcript = transcript[:TRANSCRIPT_CAP_SHEET] + "\n[...transcript truncated]"
        parts.append("CALL TRANSCRIPT:\n" + transcript)

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Granola local cache (fallback)
# ---------------------------------------------------------------------------


def load_granola_cache():
    """Load full Granola cache, return (docs_dict, transcripts_dict).

    Returns ALL docs (not just the Lightwork Calls folder) because recent
    calls may not have been filed into the folder yet. The Close.com filter
    already ensures we only process sales calls, and matching is done by
    attendee email overlap, so personal Granola docs won't be used.
    """
    cache_path = Path(GRANOLA_CACHE)
    if not cache_path.exists():
        print(f"Granola cache not found at {cache_path}")
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
    """Match a Close.com meeting to a Granola document.

    Uses shared scoring via _score_meeting_match, with emails gathered
    from both google_calendar_event and people fields.
    """
    close_attendees, close_title, close_date = _get_close_meeting_context(meeting)

    best_match = None
    best_score = 0

    for doc_id, doc in granola_docs.items():
        # Gather candidate emails from calendar event and people field
        gcal = doc.get("google_calendar_event") or {}
        candidate_emails = set()
        for a in gcal.get("attendees", []):
            candidate_emails.add((a.get("email") or "").lower())

        people = doc.get("people") or {}
        if isinstance(people, dict):
            for p_list in people.values():
                if isinstance(p_list, list):
                    for p in p_list:
                        if isinstance(p, dict):
                            candidate_emails.add((p.get("email") or "").lower())

        # Use doc title or gcal summary, whichever is present
        doc_title = (doc.get("title") or "").lower().strip()
        gcal_title = (gcal.get("summary") or "").lower().strip()
        candidate_title = doc_title or gcal_title

        doc_date = (doc.get("created_at") or "")[:10]

        score = _score_meeting_match(
            close_attendees, close_title, close_date,
            candidate_emails, candidate_title, doc_date,
        )
        # Bonus: also check gcal title if different from doc title
        if gcal_title and gcal_title != candidate_title:
            alt_score = _score_meeting_match(
                close_attendees, close_title, close_date,
                candidate_emails, gcal_title, doc_date,
            )
            score = max(score, alt_score)

        if score > best_score:
            best_score = score
            best_match = doc

    if best_score >= MATCH_THRESHOLD:
        return best_match
    return None


def extract_granola_notes(doc, transcripts_dict=None):
    """Extract readable notes/transcript from a Granola document.

    Combines the user's typed notes with the spoken transcript if available.
    """
    if not doc:
        return ""

    parts = []

    # 1. User's typed notes (notes_markdown > notes_plain > notes JSON)
    typed_notes = (doc.get("notes_markdown") or doc.get("notes_plain") or "").strip()
    if not typed_notes:
        notes_json = doc.get("notes")
        if isinstance(notes_json, dict):
            typed_notes = _extract_text_from_prosemirror(notes_json).strip()
    if typed_notes:
        parts.append("MEETING NOTES:\n" + typed_notes)

    # 2. Spoken transcript from the transcripts cache
    doc_id = doc.get("id", "")
    if transcripts_dict and doc_id in transcripts_dict:
        entries = transcripts_dict[doc_id]
        if isinstance(entries, list) and entries:
            lines = []
            for entry in entries:
                speaker = entry.get("speaker_name") or entry.get("speaker", "")
                text = entry.get("text", "").strip()
                if text:
                    if speaker:
                        lines.append(f"{speaker}: {text}")
                    else:
                        lines.append(text)
            if lines:
                # Cap at ~4000 chars to stay within prompt limits
                transcript_text = "\n".join(lines)
                if len(transcript_text) > TRANSCRIPT_CAP_LOCAL:
                    transcript_text = transcript_text[:TRANSCRIPT_CAP_LOCAL] + "\n[...transcript truncated]"
                parts.append("CALL TRANSCRIPT:\n" + transcript_text)

    return "\n\n".join(parts)

def _granola_local_has_transcript(doc, transcripts_dict):
    doc_id = (doc or {}).get("id", "")
    if not doc_id or not transcripts_dict or doc_id not in transcripts_dict:
        return False
    entries = transcripts_dict.get(doc_id)
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if (entry.get("text") or "").strip():
            return True
    return False


def _granola_local_has_notes(doc):
    if not doc:
        return False
    typed_notes = (doc.get("notes_markdown") or doc.get("notes_plain") or "").strip()
    if typed_notes:
        return True
    notes_json = doc.get("notes")
    if isinstance(notes_json, dict):
        return bool(_extract_text_from_prosemirror(notes_json).strip())
    return False


def get_granola_match(meeting, sheet_rows, granola_docs):
    """Return (source, match_obj) where source is 'sheet', 'local', or None."""
    if sheet_rows:
        sheet_match = match_granola_sheet(meeting, sheet_rows)
        if sheet_match:
            return "sheet", sheet_match
    if granola_docs:
        local_match = match_granola(meeting, granola_docs)
        if local_match:
            return "local", local_match
    return None, None


def get_transcript_label(source, match_obj, granola_transcripts):
    """Human-friendly label for tracker view based on transcript presence."""
    if source == "sheet":
        transcript = ((match_obj or {}).get("Transcript") or "").strip()
        notes = ((match_obj or {}).get("Notes") or "").strip()
        if transcript:
            return "Yes (Sheet)"
        if notes:
            return "Notes only"
        return "No"
    if source == "local":
        if _granola_local_has_transcript(match_obj, granola_transcripts):
            return "Yes (Local)"
        if _granola_local_has_notes(match_obj):
            return "Notes only"
        # Many Granola docs are created from calendar events without a recording.
        if match_obj and match_obj.get("valid_meeting") and not match_obj.get("audio_file_handle"):
            return "No (not recorded)"
        return "No"
    return "No"


def _mcp_text_content(result: dict) -> str:
    """Extract the first text block payload from an MCP tool result."""
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text") or ""
    return ""


def mcp_list_meetings(client: "GranolaMCPClient", since_dt: datetime, until_dt: datetime):
    """Return a list of meetings with {id,title,date_str,emails:set}."""
    start = since_dt.date().isoformat()
    end = until_dt.date().isoformat()
    res = client.tools_call("list_meetings", {"time_range": "custom", "custom_start": start, "custom_end": end})
    text = _mcp_text_content(res)
    meetings = []
    if not text:
        return meetings
    # Split into <meeting ...> ... </meeting> blocks so we can extract participants.
    for m in re.finditer(r'<meeting id="([^"]+)" title="([^"]*)" date="([^"]*)">(.*?)</meeting>', text, flags=re.DOTALL):
        mid, title, date_str, inner = m.group(1), m.group(2), m.group(3), m.group(4)
        emails = set(e.lower() for e in re.findall(r"<([^>\\s]+@[^>\\s]+)>", inner))
        meetings.append({"id": mid, "title": title, "date_str": date_str, "emails": emails})
    return meetings


def _load_local_granola_meetings() -> list:
    """Load meetings from the local Granola cache file in the same format as mcp_list_meetings().

    Returns list of {id, title, date_str, emails} dicts that can be passed to mcp_match_meeting().
    """
    cache_path = Path.home() / "Library" / "Application Support" / "Granola" / "cache-v3.json"
    if not cache_path.exists():
        return []
    try:
        with open(cache_path) as f:
            raw = json.load(f)
        state = json.loads(raw["cache"])["state"]
    except Exception:
        return []

    docs = state.get("documents", {})
    # Only include docs from the Lightwork Calls folder
    lw_list_id = None
    for list_id, meta in state.get("documentListsMetadata", {}).items():
        if isinstance(meta, dict) and "lightwork" in (meta.get("title") or "").lower():
            lw_list_id = list_id
            break
    if not lw_list_id:
        return []

    lw_doc_ids = set(state.get("documentLists", {}).get(lw_list_id, []))
    meetings = []
    for did in lw_doc_ids:
        doc = docs.get(did, {})
        if not doc:
            continue
        title = doc.get("title") or ""
        created = doc.get("created_at") or ""
        # Format date_str to match what mcp_match_meeting expects: "Feb 10, 2026 2:00 PM"
        date_str = ""
        if created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                date_str = dt.strftime("%b %d, %Y %I:%M %p")
            except Exception:
                pass
        # Extract attendee emails from google_calendar_event
        emails = set()
        gcal = doc.get("google_calendar_event", {})
        if isinstance(gcal, dict):
            for att in gcal.get("attendees", []):
                if isinstance(att, dict) and att.get("email"):
                    emails.add(att["email"].lower())
        meetings.append({"id": did, "title": title, "date_str": date_str, "emails": emails})
    return meetings


def mcp_match_meeting(close_meeting: dict, mcp_meetings: list) -> dict | None:
    """Match a Close meeting to a Granola MCP meeting list."""
    close_attendees, close_title, _ = _get_close_meeting_context(close_meeting)

    # Parse Close date for comparison (MCP uses a different date format)
    starts_at = close_meeting.get("starts_at", "")
    close_date_obj = None
    if starts_at:
        try:
            close_date_obj = datetime.fromisoformat(starts_at.replace("Z", "+00:00")).date()
        except (ValueError, TypeError):
            pass

    best = None
    best_score = 0
    for gm in mcp_meetings:
        candidate_emails = gm.get("emails") or set()
        candidate_title = (gm.get("title") or "").lower().strip()

        # MCP dates are formatted as "Feb 10, 2026 2:00 PM", need special parsing
        candidate_date = ""
        if close_date_obj and gm.get("date_str"):
            try:
                gdt = datetime.strptime(gm["date_str"], "%b %d, %Y %I:%M %p").date()
                # +/-1 day tolerance for timezone differences
                if abs((gdt - close_date_obj).days) <= 1:
                    candidate_date = close_date_obj.isoformat()
            except Exception:
                pass

        close_date_str = close_date_obj.isoformat() if close_date_obj else ""

        score = _score_meeting_match(
            close_attendees, close_title, close_date_str,
            candidate_emails, candidate_title, candidate_date,
        )

        if score > best_score:
            best_score = score
            best = gm

    if best_score >= MATCH_THRESHOLD:
        return best
    return None


def mcp_get_transcript_text(client: "GranolaMCPClient", meeting_id: str) -> str:
    """Fetch transcript text from Granola MCP. Raises GranolaRateLimitError on rate limit."""
    res = client.tools_call("get_meeting_transcript", {"meeting_id": meeting_id})
    text = _mcp_text_content(res).strip()
    is_error = (res or {}).get("isError", False)

    # Detect rate limit: check for "rate limit" in the response text
    # (isError alone is not enough since it could be a different error like "not found")
    is_rate_limit = text and "rate limit" in text.lower()
    if is_rate_limit:
        raise GranolaRateLimitError(f"Rate limited fetching transcript for {meeting_id}: {text[:200]}")

    if not text:
        return ""
    # Tool returns a JSON object as text.
    try:
        obj = json.loads(text)
        return (obj.get("transcript") or "").strip()
    except Exception:
        return ""


def _extract_text_from_prosemirror(node):
    """Recursively extract text from a ProseMirror-style JSON doc."""
    if not isinstance(node, dict):
        return ""
    texts = []
    if node.get("type") == "text":
        texts.append(node.get("text", ""))
    for child in node.get("content", []):
        texts.append(_extract_text_from_prosemirror(child))
    return "\n".join(t for t in texts if t)


def _parse_ai_sections(text: str) -> dict:
    """Parse model output into draft/reasoning/priority sections if present."""
    out = {"draft": "", "reasoning": "", "priority": "", "raw": text or ""}
    s = (text or "").strip()
    if not s:
        return out

    # Normalize headings to make parsing a bit more resilient.
    # We keep original content, just locate markers.
    def grab_between(start_pat, end_pat):
        m = re.search(start_pat + r"\s*(.*?)\s*(?:" + end_pat + r"|$)", s, flags=re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    draft = grab_between(r"FOLLOW-UP\s*DRAFT:", r"VALUE\s*TIP\s*REASONING:")
    reasoning = grab_between(r"VALUE\s*TIP\s*REASONING:", r"PRIORITY:")
    prio = grab_between(r"PRIORITY:", r"$")

    # Fallback: if headings not present, treat whole thing as draft.
    if not draft and not reasoning and not prio:
        out["draft"] = s
        return out

    out["draft"] = draft
    out["reasoning"] = reasoning
    out["priority"] = prio.splitlines()[0].strip() if prio else ""
    return out


# ---------------------------------------------------------------------------
# SQLite draft cache
# ---------------------------------------------------------------------------


def _init_draft_db():
    """Create the drafts table if it doesn't exist, return a connection."""
    conn = sqlite3.connect(DRAFT_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS drafts (
            lead_id TEXT,
            fu_number INTEGER,
            cadence_type TEXT,
            draft_text TEXT,
            reasoning TEXT,
            priority TEXT,
            input_hash TEXT,
            created_at TEXT,
            PRIMARY KEY (lead_id, fu_number, cadence_type)
        )
    """)
    conn.commit()
    return conn


def _draft_input_hash(call_notes, prior_emails_text, fu_instructions):
    """SHA-256 hash of draft inputs so we regenerate when context changes."""
    blob = f"{call_notes}|{prior_emails_text}|{fu_instructions}"
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _get_cached_draft(conn, lead_id, fu_number, cadence_type, input_hash):
    """Return cached draft dict or None if miss/stale."""
    row = conn.execute(
        "SELECT draft_text, reasoning, priority, input_hash FROM drafts "
        "WHERE lead_id = ? AND fu_number = ? AND cadence_type = ?",
        (lead_id, fu_number, cadence_type),
    ).fetchone()
    if row is None:
        return None
    if row[3] != input_hash:
        return None  # inputs changed, regenerate
    return {"draft_text": row[0], "reasoning": row[1], "priority": row[2]}


def _save_draft(conn, lead_id, fu_number, cadence_type, raw_output, input_hash):
    """Upsert a draft into the cache."""
    parsed = _parse_ai_sections(raw_output)
    conn.execute(
        "INSERT OR REPLACE INTO drafts "
        "(lead_id, fu_number, cadence_type, draft_text, reasoning, priority, input_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            lead_id, fu_number, cadence_type,
            parsed.get("draft") or parsed.get("raw") or raw_output,
            (parsed.get("reasoning") or "").strip(),
            (parsed.get("priority") or "").strip(),
            input_hash,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# SQLite transcript cache
# ---------------------------------------------------------------------------


def _init_transcript_db():
    """Create the transcripts table if it doesn't exist, return a connection."""
    conn = sqlite3.connect(TRANSCRIPT_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transcripts (
            meeting_id TEXT PRIMARY KEY,
            transcript_text TEXT,
            meeting_notes TEXT,
            source TEXT,
            fetched_at TEXT
        )
    """)
    conn.commit()
    return conn


def _get_cached_transcript(conn, meeting_id):
    """Return cached transcript dict or None if not found."""
    row = conn.execute(
        "SELECT transcript_text, meeting_notes, source FROM transcripts "
        "WHERE meeting_id = ?",
        (meeting_id,),
    ).fetchone()
    if row is None:
        return None
    return {"transcript_text": row[0] or "", "meeting_notes": row[1] or "", "source": row[2] or ""}


def _save_transcript(conn, meeting_id, transcript_text="", meeting_notes="", source=""):
    """Upsert a transcript into the cache."""
    conn.execute(
        "INSERT OR REPLACE INTO transcripts "
        "(meeting_id, transcript_text, meeting_notes, source, fetched_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (meeting_id, transcript_text or "", meeting_notes or "", source,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# SMTP email reminders
# ---------------------------------------------------------------------------


# SAFETY: Only these addresses can receive reminder emails. Never send
# to leads, prospects, or any address outside this allowlist.
ALLOWED_RECIPIENT_EMAILS = {
    "jay@lightworkhome.com",
    "johnny@lightworkhome.com",
    "dom@lightworkhome.com",
}


def _send_owner_reminders(action_items_by_owner, date_str):
    """Send one reminder email per owner with all their leads' drafts.

    Skips gracefully if SMTP credentials are not configured.
    SAFETY: Only sends to addresses in ALLOWED_RECIPIENT_EMAILS.
    """
    if not SMTP_EMAIL or not SMTP_PASSWORD:
        print("\n  SMTP credentials not set (SMTP_EMAIL / SMTP_PASSWORD in .env). Skipping email delivery.")
        return

    for owner, items in action_items_by_owner.items():
        if not items:
            continue
        recipient = OWNER_TO_EMAIL.get(owner)
        if not recipient:
            print(f"  No email address for owner '{owner}', skipping reminder.")
            continue
        if recipient not in ALLOWED_RECIPIENT_EMAILS:
            print(f"  BLOCKED: {recipient} is not in the allowed recipient list. Skipping.")
            continue

        lead_count = len(items)
        subject = f"Lightwork Follow-Ups - {date_str} ({lead_count} lead{'s' if lead_count != 1 else ''})"

        # Build HTML body
        leads_html = ""
        for item in items:
            name = html_mod.escape(item.get("name", ""))
            fu = item.get("fu_number", "?")
            draft_raw = item.get("copy_draft", "")
            # Convert plain text draft to copy-paste-friendly HTML:
            # - Lines starting with "- " become <li> items
            # - Other newlines become <br>
            draft_lines = draft_raw.split("\n")
            draft_html_parts = []
            in_list = False
            for dl in draft_lines:
                stripped = dl.strip()
                if stripped.startswith("- "):
                    if not in_list:
                        draft_html_parts.append("<ul style='margin:8px 0; padding-left:20px;'>")
                        in_list = True
                    draft_html_parts.append(f"<li>{stripped[2:]}</li>")
                else:
                    if in_list:
                        draft_html_parts.append("</ul>")
                        in_list = False
                    draft_html_parts.append(dl + "<br>" if stripped else "<br>")
            if in_list:
                draft_html_parts.append("</ul>")
            draft = "\n".join(draft_html_parts)
            no_show_badge = ' <span style="color:#c0392b;">[NO-SHOW]</span>' if item.get("no_show") else ""
            overdue_badge = ' <span style="color:#E67E22;">[OVERDUE]</span>' if item.get("overdue") else ""

            # Call summary bullets (extract from call_notes)
            call_summary_html = ""
            call_notes_raw = item.get("call_notes", "")
            if call_notes_raw and call_notes_raw != "NO-SHOW: Meeting was canceled or marked no-show.":
                # Take first ~800 chars of notes, split into bullet-worthy lines
                notes_text = call_notes_raw[:800]
                # Strip headers like "MEETING NOTES:" and "CALL TRANSCRIPT:"
                notes_text = re.sub(r"^(MEETING NOTES|CALL TRANSCRIPT|CALL NOTES FROM GRANOLA):?\s*", "", notes_text, flags=re.MULTILINE)
                # Split into meaningful lines and take top 5
                lines = [l.strip() for l in notes_text.split("\n") if l.strip() and len(l.strip()) > 10]
                bullets = lines[:5]
                if bullets:
                    bullet_items = "".join(f"<li>{html_mod.escape(b[:150])}</li>" for b in bullets)
                    call_summary_html = f"""
                    <div style="margin-top:12px; border-top:1px solid #eee; padding-top:10px;">
                      <div style="font-size:11px; text-transform:uppercase; color:#888; font-weight:700; margin-bottom:6px;">Call Summary</div>
                      <ul style="margin:0; padding-left:20px; font-size:13px; color:#555; line-height:1.6;">{bullet_items}</ul>
                    </div>"""

            # Prior emails sent
            prior_emails_html = ""
            sent_emails = item.get("sent_emails") or []
            if sent_emails:
                email_items = ""
                for idx, em in enumerate(sent_emails, 1):
                    subj = html_mod.escape(em.get("subject", "(no subject)"))
                    body = html_mod.escape((em.get("body") or "")[:300])
                    if len(em.get("body", "")) > 300:
                        body += "..."
                    email_items += f"""
                    <div style="border-left:2px solid #ddd; padding:6px 10px; margin-bottom:6px; background:#fafafa;">
                      <div style="font-size:12px; font-weight:600; color:#2E5B88;">Email {idx}: {subj}</div>
                      <div style="font-size:12px; color:#666; white-space:pre-wrap; margin-top:4px;">{body}</div>
                    </div>"""
                prior_emails_html = f"""
                <div style="margin-top:12px; border-top:1px solid #eee; padding-top:10px;">
                  <div style="font-size:11px; text-transform:uppercase; color:#888; font-weight:700; margin-bottom:6px;">Prior Emails ({len(sent_emails)})</div>
                  {email_items}
                </div>"""

            close_url = item.get("close_url", "")
            close_link = f' <a href="{close_url}" style="color:#2E5B88; font-size:13px; font-weight:normal;">[Close]</a>' if close_url else ""
            leads_html += f"""
            <div style="border:1px solid #ddd; border-radius:8px; padding:16px; margin-bottom:16px; background:#fff;">
              <h3 style="margin:0 0 8px 0; color:#2E5B88;">{name} (FU #{fu}){no_show_badge}{overdue_badge}{close_link}</h3>
              <div style="background:#f9f9f9; border-left:3px solid #2E5B88; padding:12px; font-family:sans-serif; font-size:14px; line-height:1.6; color:#333;">{draft}</div>
              {call_summary_html}
              {prior_emails_html}
            </div>"""

        body_html = f"""
        <html><body style="font-family:sans-serif; max-width:700px; margin:0 auto; padding:20px;">
          <h2 style="color:#1a1a1a;">Follow-Up Reminders for {html_mod.escape(owner)}</h2>
          <p style="color:#666; font-size:14px;">{date_str} - {lead_count} lead{'s' if lead_count != 1 else ''} due</p>
          {leads_html}
          <p style="color:#999; font-size:12px; margin-top:24px;">Generated by Lightwork Follow-Up Tracker</p>
        </body></html>"""

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_EMAIL
        msg["To"] = recipient
        msg.attach(MIMEText(body_html, "html"))

        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_EMAIL, SMTP_PASSWORD)
                server.send_message(msg)
            print(f"  Reminder sent to {owner} ({recipient}): {lead_count} lead{'s' if lead_count != 1 else ''}")
        except Exception as e:
            print(f"  Error sending to {owner} ({recipient}): {e}")


# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------

SALES_SCRIPTS_PATH = SCRIPT_DIR / "reference" / "sales-scripts.md"
FOLLOWUP_EXAMPLES_PATH = SCRIPT_DIR / "reference" / "follow-up-examples.md"
SALES_TIPS_CONDENSED_PATH = SCRIPT_DIR / "reference" / "sales-tips-condensed.md"
VOICE_GUIDE_CONDENSED_PATH = SCRIPT_DIR / "reference" / "voice-guide-condensed.md"


def load_reference_file(path):
    """Load a reference file, return empty string if missing."""
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return ""


def _load_condensed_or_fallback(condensed_path, raw_path, fallback_cap=3000):
    """Load condensed reference file if it exists, else truncate the raw file."""
    condensed = load_reference_file(condensed_path)
    if condensed:
        return condensed
    raw = load_reference_file(raw_path)
    if len(raw) > fallback_cap:
        return raw[:fallback_cap] + "\n[...truncated]"
    return raw


def _extract_first_name(display_name):
    """Extract first name from Close.com display name.

    Handles: "John Smith" -> "John"
             "John & Sarah Smith" -> "John"
             "The Smiths" -> "The Smiths" (no change if no clear first name)
    """
    name = (display_name or "").strip()
    if not name:
        return name
    # Handle "John & Sarah Smith" or "John and Sarah Smith"
    parts = re.split(r"\s*[&]\s*|\s+and\s+", name, maxsplit=1)
    first_part = parts[0].strip()
    # Take the first word as the first name
    words = first_part.split()
    if not words:
        return name
    candidate = words[0]
    # Skip titles/articles that aren't real first names
    if candidate.lower() in ("the", "mr", "mrs", "ms", "dr", "mr.", "mrs.", "ms.", "dr."):
        return name
    return candidate


def generate_digest_for_call(lead_info, call_notes, meeting, owner_name="Jay",
                             fu_number=1, sent_emails=None, cadence_type="active",
                             no_show=False, sales_scripts=None, followup_examples=None):
    """Send lead context + call notes to Claude, get summary + follow-up draft.

    Uses a system prompt for stable context (voice, rules, reference material)
    and a user message for per-lead context (name, transcript, prior emails).

    Args:
        lead_info: Lead details from Close.com
        call_notes: Granola transcript/notes text
        meeting: Meeting dict from Close.com (used for context)
        fu_number: Which follow-up number (1-7) to generate
        sent_emails: List of dicts with subject/body of prior emails sent
        cadence_type: "active" for standard 7-touch, "nurture" for long-term
        no_show: True when the most recent meeting was canceled/no-show
    """
    custom = lead_info.get("custom", {})
    contacts = lead_info.get("contacts", [])
    addresses = lead_info.get("addresses", [])

    lead_name = lead_info.get("display_name", "Unknown")
    first_name = _extract_first_name(lead_name)
    lead_email = ""
    if contacts:
        emails = contacts[0].get("emails", [])
        if emails:
            lead_email = emails[0].get("email", "")

    city = ""
    if addresses:
        city = addresses[0].get("city", "")

    budget = custom.get("Budget", "N/A")
    health_spend = custom.get("Annual spend on health and wellness", "N/A")
    sq_footage = custom.get("Square Footage", "N/A")
    source = custom.get("How did you hear about us?", "N/A")
    why_reaching_out = custom.get("Why are you reaching out", "N/A")
    meeting_status = custom.get("Meeting Status", "N/A")

    # Load condensed reference files (fall back to raw + truncation if missing)
    if sales_scripts is None:
        sales_scripts = _load_condensed_or_fallback(
            SALES_TIPS_CONDENSED_PATH, SALES_SCRIPTS_PATH, fallback_cap=3000
        )
    if followup_examples is None:
        followup_examples = _load_condensed_or_fallback(
            VOICE_GUIDE_CONDENSED_PATH, FOLLOWUP_EXAMPLES_PATH, fallback_cap=2000
        )

    # Get cadence details for this FU number
    cadence = _get_cadence(cadence_type)
    max_touches = len(cadence)
    day_offset, fu_type, fu_instructions = cadence[fu_number]

    # Substitute booking link placeholder in no-show cadence instructions
    if cadence_type == "no_show" and "{booking_link}" in fu_instructions:
        sender = owner_name if owner_name in OWNER_SIGNATURE else "Jay"
        booking_link = OWNER_BOOKING_LINK.get(sender, OWNER_BOOKING_LINK["Jay"])
        fu_instructions = fu_instructions.replace("{booking_link}", booking_link)

    # Build cadence overview for context
    cadence_overview = "\n".join(
        f"  FU {n}: Day {d} - {t}" for n, (d, t, _) in cadence.items()
    )

    # Build prior emails section
    prior_emails_text = "(No prior emails sent)"
    if sent_emails:
        parts = []
        for i, em in enumerate(sent_emails, 1):
            parts.append(f"--- Email {i} ---\nSubject: {em['subject']}\n{em['body']}")
        prior_emails_text = "\n\n".join(parts)

    if cadence_type == "nurture":
        cadence_label = "LONG-TERM NURTURE"
    elif cadence_type == "no_show":
        cadence_label = "NO-SHOW REBOOK"
    else:
        cadence_label = "ACTIVE"
    message_mode = cadence_label

    meeting_starts_at = meeting.get("starts_at", "")
    days_since_call = None
    if meeting_starts_at:
        try:
            start_dt = datetime.fromisoformat(meeting_starts_at.replace("Z", "+00:00"))
            days_since_call = (datetime.now(timezone.utc) - start_dt).days
        except Exception:
            days_since_call = None

    sender = owner_name if owner_name in OWNER_SIGNATURE else "Jay"
    sender_signature = OWNER_SIGNATURE.get(sender, OWNER_SIGNATURE["Jay"])

    # Days-since-call context for the prompt
    days_since_text = ""
    if days_since_call is not None:
        days_since_text = f"Actual days since call: {days_since_call}."
        overdue_days = days_since_call - day_offset
        if overdue_days > 3:
            days_since_text += f" This follow-up is overdue by {overdue_days} days, so avoid language like 'the other day' or 'recently'."

    # ---------------------------------------------------------------
    # System prompt: stable per-run context (voice, rules, references)
    # ---------------------------------------------------------------
    system_prompt = f"""You write follow-up emails for {sender} at Lightwork Home Health, an environmental health consulting company.

VOICE & STYLE (match this exactly):
{followup_examples}

SALES TIPS (reference ONLY when a transcript topic matches a trigger below):
{sales_scripts}

FULL {max_touches}-TOUCH CADENCE:
{cadence_overview}

SERVICE AREA:
Lightwork travels to the client. Based on the lead's city:
- If in the US: we serve the entire US
- If in Canada: we serve Canada
- If in Europe: we serve Europe
When mentioning availability (FU6), frame it as "we'll be in [their city/area]" to confirm we come to them.

KEY RESOURCES YOU CAN REFERENCE:
- Example report: https://www.lightworkhome.com/examplereport (password: homehealth)
- Wilkinson write-up: https://www.lightworkhome.com/blog-posts/wilkinson
- Science video: https://www.lightworkhome.com/blog-posts/the-science-behind-lightwork

ALLOWED LINK PREFIXES (only include links that start with one of these):
{", ".join(ALLOWED_URL_PREFIXES)}

IMPORTANT RULES:
- BREVITY IS KING. Most emails should be 2-4 sentences. Only FU1 can be longer (up to 6 sentences).
- Do NOT force a tip or resource into every email. If the cadence step says "no tip," write a clean, short follow-up without one.
- Never say "home health assessment" or "assessment."
- When mentioning the example report, always hyperlink it: <a href="https://www.lightworkhome.com/examplereport">example report</a> (password: homehealth)
- Do NOT imply anything is scheduled or confirmed unless the transcript explicitly confirms it.
- Do NOT use "today" or "yesterday" unless the call was within the last 2 days.
- No generic filler ("just checking in", "circling back", "touching base").
- Do NOT use "thought you might find this interesting/helpful" or similar filler openers (unless it's part of a fixed template). Vary your openers.
- Never use em dashes.
- Do NOT repeat ANY resource or talking point from prior emails.
- Vary your openings. Not every email should start "Hey {{name}}, [reference to call]."
- Use the lead's first name ("{first_name}") in the greeting, not their full name.
- LINKS: Only use links from the allowlist above."""

    # ---------------------------------------------------------------
    # User message: per-lead context
    # ---------------------------------------------------------------
    nurture_note = ""
    if cadence_type == "nurture":
        nurture_note = "\nThis lead previously said they are not interested right now. This is a low-pressure, value-only nurture email. NO asks, NO scheduling mentions, NO pressure. Just share something genuinely useful."
    no_show_note = ""
    if cadence_type == "no_show":
        booking_link = OWNER_BOOKING_LINK.get(sender, OWNER_BOOKING_LINK["Jay"])
        no_show_note = f"\nThis lead has not yet had a call with us. They scheduled but did not attend. Do NOT reference any conversation or call topics. Booking link: {booking_link}"

    # Skip Wilkinson content for leads referred by Andrew Wilkinson
    referral_note = ""
    source_lower = source.lower() if source else ""
    if "wilkinson" in source_lower or "andrew" in source_lower:
        referral_note = "\nIMPORTANT: This lead was referred by Andrew Wilkinson. Do NOT include the Wilkinson write-up, Wilkinson blog post, Wilkinson testimonial, or any Andrew Wilkinson reference. They already know him and have likely read it. Use a different resource instead."

    # Johnny and Dom's emails occasionally include WhatsApp line
    phone_note = ""
    if sender == "Johnny":
        phone_note = "\nIn some emails (not all), naturally include this line: 'Always feel free to text/WhatsApp me at 310.804.1305 with any questions or home health interests as well.'"
    elif sender == "Dom":
        phone_note = "\nIn some emails (not all), naturally include this line: 'Always feel free to text/WhatsApp me at +1 (360) 951-1330 with any questions or home health interests as well.'"

    user_prompt = f"""CADENCE TYPE: {message_mode}{nurture_note}{no_show_note}{referral_note}{phone_note}

THIS IS FOLLOW-UP #{fu_number} ({fu_type})
Scheduled for Day {day_offset} after the call.
{days_since_text}

{"NO-SHOW: This lead never had a call. Do NOT reference any conversation." if cadence_type == "no_show" else ""}

SPECIFIC INSTRUCTIONS FOR THIS FOLLOW-UP:
{fu_instructions}

EMAIL FORMAT:
Write a short, natural follow-up email. Match the cadence step instructions above.
Sign off with this exact signature:
{sender_signature}

EMAILS ALREADY SENT TO THIS LEAD (do NOT repeat any tips, resources, or talking points from these):
{prior_emails_text}

LEAD CONTEXT:
- Name: {lead_name} (use "{first_name}" in greeting)
- Email: {lead_email}
- City: {city}
- Budget: {budget}
- Annual Health Spend: {health_spend}
- Home Size: {sq_footage}
- How they heard about us: {source}
- Why reaching out: {why_reaching_out}
- Meeting Status: {meeting_status}

CALL NOTES FROM GRANOLA:
{call_notes if call_notes else "(No transcript available - generate follow-up based on lead context only)"}

Generate exactly this output:

FOLLOW-UP DRAFT:
[Write a personalized follow-up email. Short, casual, friendly, low pressure. Sign off using the exact signature above.]

VALUE TIP REASONING:
[If you included a health tip or resource, explain in 1 sentence why it's relevant to this lead. If none, write "No transcript match for a specific tip."]

PRIORITY: [HIGH / MEDIUM / LOW - based on budget, urgency, engagement level]"""

    def _call_model(sys_prompt: str, usr_prompt: str) -> str:
        if AI_PROVIDER == "openai":
            if not OPENAI_API_KEY:
                raise RuntimeError("OPENAI_API_KEY missing (required for AI_PROVIDER=openai)")

            # OpenAI Responses API: prepend system context to the input
            combined = f"{sys_prompt}\n\n---\n\n{usr_prompt}"
            body = {
                "model": OPENAI_MODEL,
                "input": combined,
                "reasoning": {"effort": OPENAI_REASONING_EFFORT},
            }
            resp = _request(
                "POST",
                OPENAI_API_URL,
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                body=body,
            )

            # Prefer output_text if present; else parse message content blocks.
            if isinstance(resp, dict) and isinstance(resp.get("output_text"), str) and resp["output_text"].strip():
                return resp["output_text"].strip()

            parts = []
            for item in resp.get("output", []) if isinstance(resp, dict) else []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "message":
                    continue
                for c in item.get("content", []) or []:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") in ("output_text", "text"):
                        txt = c.get("text") or ""
                        if txt:
                            parts.append(txt)
            text = "\n".join(parts).strip()
            if not text:
                raise RuntimeError(f"OpenAI response missing text: keys={list(resp.keys()) if isinstance(resp, dict) else type(resp)}")
            return text

        # Default: Anthropic
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY missing (required for AI_PROVIDER=anthropic)")

        body = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 2048,
            "system": sys_prompt,
            "messages": [{"role": "user", "content": usr_prompt}],
        }

        resp = _request(
            "POST",
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            body=body,
        )

        # Extract text from response
        content = resp.get("content", [])
        text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
        return "\n".join(text_parts).strip()

    raw = _call_model(system_prompt, user_prompt)
    # Post-check and rewrite loop (avoid link hallucinations, em dashes, AI slop).
    max_rewrites = 3
    for attempt in range(max_rewrites):
        parsed = _parse_ai_sections(raw)
        draft = parsed.get("draft") or parsed.get("raw") or ""
        issues = _lint_email_draft(draft, int(fu_number), days_since_call, prior_emails=sent_emails, cadence_type=cadence_type)
        if not issues:
            return raw

        fix_user = (
            user_prompt
            + "\n\nCOMPLIANCE FIXES REQUIRED:\n"
            + "\n".join(f"- {i}" for i in issues)
            + "\n\nRewrite the email to fix the issues. Keep it short, specific, and human."
        )
        raw = _call_model(system_prompt, fix_user)

    # Log if issues persist after all rewrite attempts
    remaining = _lint_email_draft(
        (_parse_ai_sections(raw).get("draft") or raw or "").strip(),
        int(fu_number), days_since_call, prior_emails=sent_emails,
        cadence_type=cadence_type,
    )
    if remaining:
        print(f"  Warning: draft still has issues after {max_rewrites} rewrites: {remaining}")

    return raw


# ---------------------------------------------------------------------------
# Email builder
# ---------------------------------------------------------------------------


def build_progress_bar(fu_done, total=7, completed_color="#2E5B88"):
    """Build a visual progress bar showing follow-up completion."""
    cells = []
    for i in range(1, total + 1):
        if i <= fu_done:
            color = completed_color
        elif i == fu_done + 1:
            color = "#E67E22"  # current (due now)
        else:
            color = "#ddd"     # future
        cells.append(
            f'<span style="display:inline-block; width:24px; height:8px; '
            f'background:{color}; border-radius:2px; margin-right:2px;"></span>'
        )
    return "".join(cells)


def build_tracker_view(all_leads_status):
    """Build an HTML tracker table showing ALL active leads grouped by owner.

    Args:
        all_leads_status: list of dicts with owner_name from get_leads_due_today()
    """
    # Group by owner
    by_owner = {}
    for entry in all_leads_status:
        owner = entry.get("owner_name", "Unassigned")
        by_owner.setdefault(owner, []).append(entry)

    # Sort each owner's leads by next FU due date
    for owner in by_owner:
        by_owner[owner].sort(key=lambda x: x.get("due_date") or datetime.max.replace(tzinfo=timezone.utc))

    ordered_owners = []
    seen = set()
    for o in OWNER_TAB_ORDER:
        if o in by_owner:
            ordered_owners.append(o)
            seen.add(o)
    for o in sorted(by_owner.keys()):
        if o not in seen:
            ordered_owners.append(o)

    owner_tables = ""
    for owner in ordered_owners:
        leads = by_owner[owner]
        rows_html = ""
        for entry in leads:
            lead_info = entry["lead_info"]
            name = lead_info.get("display_name", "Unknown")
            close_url = lead_info.get("html_url", "")
            fu_done = entry["fu_done"]
            next_fu = entry["next_fu"]
            first_call = entry["first_call_date"]
            days_since = (datetime.now(timezone.utc) - first_call).days
            cadence_type = entry.get("cadence_type", "active")
            max_touches = entry.get("max_touches", 7)
            cadence = _get_cadence(cadence_type)
            transcript_label = entry.get("transcript_label", "No")
            no_show = bool(entry.get("no_show"))
            transcript_lower = transcript_label.lower()
            transcript_state = "yes" if (transcript_lower.startswith("yes") or transcript_lower.startswith("no-show")) else "no"

            # Cadence badge
            nurture_badge = ""
            if cadence_type == "nurture":
                nurture_badge = (
                    ' <span style="background:#8e44ad; color:white; font-size:9px; '
                    'padding:1px 4px; border-radius:2px; vertical-align:middle;">NURTURE</span>'
                )
            elif cadence_type == "no_show":
                nurture_badge = (
                    ' <span style="background:#e67e22; color:white; font-size:9px; '
                    'padding:1px 4px; border-radius:2px; vertical-align:middle;">REBOOK</span>'
                )
            no_show_badge = ""
            if no_show and cadence_type != "no_show":
                no_show_badge = (
                    ' <span style="background:#c0392b; color:white; font-size:9px; '
                    'padding:1px 4px; border-radius:2px; vertical-align:middle; margin-left:4px;">NO-SHOW</span>'
                )

            # Progress dots
            dots = ""
            for i in range(1, max_touches + 1):
                if i <= fu_done:
                    color = "#8e44ad" if cadence_type == "nurture" else ("#e67e22" if cadence_type == "no_show" else "#2E5B88")
                elif i == next_fu and next_fu <= max_touches:
                    color = "#E67E22"
                else:
                    color = "#ddd"
                dots += (
                    f'<span style="display:inline-block; width:18px; height:8px; '
                    f'background:{color}; border-radius:2px; margin-right:1px;"></span>'
                )

            # Status text
            if next_fu > max_touches:
                status = '<span style="color:#27ae60; font-weight:600;">Done</span>'
                next_due = ""
            else:
                days_overdue = entry.get("days_overdue", 0)
                _, fu_type, _ = cadence[next_fu]
                if days_overdue > 0:
                    status = f'<span style="color:#c0392b; font-weight:600;">Overdue {days_overdue}d</span>'
                elif days_overdue == 0:
                    status = '<span style="color:#E67E22; font-weight:600;">Due today</span>'
                else:
                    status = f'<span style="color:#666;">Due in {-days_overdue}d</span>'
                next_due = f'<span style="color:#888; font-size:11px;">{fu_type}</span>'

            name_link = f'<a href="{close_url}" style="color:#2E5B88; text-decoration:none;">{name}</a>' if close_url else name
            call_date_str = first_call.strftime("%b %-d")

            rows_html += f"""
            <tr class="lw-filter-item" data-owner="{html_mod.escape(owner)}" data-transcript="{transcript_state}" data-no-show={"1" if no_show else "0"} data-overdue={"1" if entry.get("days_overdue", 0) > 0 else "0"} style="border-bottom:1px solid #eee;">
              <td style="padding:8px 10px; font-size:13px;">{name_link}{nurture_badge}{no_show_badge}</td>
              <td style="padding:8px 6px; font-size:12px; text-align:center; color:#666;">{call_date_str}</td>
              <td style="padding:8px 6px; font-size:12px; text-align:center; color:#666;">{transcript_label}</td>
              <td style="padding:8px 6px; font-size:13px; text-align:center;">{fu_done}/{max_touches}</td>
              <td style="padding:8px 6px;">{dots}</td>
              <td style="padding:8px 6px; font-size:12px; text-align:center;">{status}<br>{next_due}</td>
              <td style="padding:8px 6px; font-size:12px; text-align:center; color:#888;">{days_since}d</td>
            </tr>"""

        owner_tables += f"""
        <div class="lw-owner-group" data-owner-group="{html_mod.escape(owner)}" style="margin-bottom:20px;">
          <h3 style="background:#f0f4f8; padding:8px 12px; border-radius:4px; margin:0 0 0 0;
                     font-size:14px; color:#2E5B88; border-left:3px solid #2E5B88;">
            {owner.upper()} ({len(leads)} lead{"s" if len(leads) != 1 else ""})
          </h3>
          <table style="width:100%; border-collapse:collapse;">
            <tr style="border-bottom:2px solid #ddd;">
              <th style="padding:6px 10px; text-align:left; font-size:11px; color:#888; text-transform:uppercase;">Lead</th>
              <th style="padding:6px 6px; text-align:center; font-size:11px; color:#888; text-transform:uppercase;">Call</th>
              <th style="padding:6px 6px; text-align:center; font-size:11px; color:#888; text-transform:uppercase;">Transcript</th>
              <th style="padding:6px 6px; text-align:center; font-size:11px; color:#888; text-transform:uppercase;">FU</th>
              <th style="padding:6px 6px; text-align:left; font-size:11px; color:#888; text-transform:uppercase;">Progress</th>
              <th style="padding:6px 6px; text-align:center; font-size:11px; color:#888; text-transform:uppercase;">Status</th>
              <th style="padding:6px 6px; text-align:center; font-size:11px; color:#888; text-transform:uppercase;">Age</th>
            </tr>
            {rows_html}
          </table>
        </div>"""

    total = len(all_leads_status)
    done = sum(1 for e in all_leads_status if e["next_fu"] > e.get("max_touches", 7))
    active = total - done

    return f"""
    <div style="margin-bottom:30px; border:1px solid #ddd; border-radius:8px; padding:16px; background:#fff;">
      <h2 style="margin:0 0 4px 0; font-size:18px; color:#1a1a1a;">Pipeline Tracker</h2>
      <p style="margin:0 0 16px 0; font-size:13px; color:#888;">
        {active} active &middot; {done} completed &middot; {total} total leads (last {CADENCE_LOOKBACK_DAYS} days)
      </p>
      {owner_tables}
    </div>"""


def build_lead_section(lead_info, claude_output, granola_found,
                       owner_name="Unassigned",
                       transcript_label="",
                       fu_number=1, fu_done=0, days_since_call=0,
                       days_overdue=0, sent_emails=None, cadence_type="active",
                       no_show=False):
    """Build one lead's section for the digest email."""
    custom = lead_info.get("custom", {})
    contacts = lead_info.get("contacts", [])
    addresses = lead_info.get("addresses", [])

    name = lead_info.get("display_name", "Unknown")
    city = addresses[0].get("city", "") if addresses else ""
    budget = custom.get("Budget", "N/A")
    health_spend = custom.get("Annual spend on health and wellness", "N/A")
    sq_footage = custom.get("Square Footage", "N/A")
    source = custom.get("How did you hear about us?", "N/A")
    meeting_status = custom.get("Meeting Status", "N/A")
    close_url = lead_info.get("html_url", "")

    cadence = _get_cadence(cadence_type)
    max_touches = len(cadence)

    location_str = f" ({city})" if city else ""
    transcript_badge = "" if granola_found else ' <span style="color:#c0392b; font-size:12px;">[No transcript]</span>'

    # Cadence badge
    nurture_badge = ""
    if cadence_type == "nurture":
        nurture_badge = (
            ' <span style="background:#8e44ad; color:white; font-size:11px; '
            'padding:2px 6px; border-radius:3px; margin-left:6px;">NURTURE</span>'
        )
    elif cadence_type == "no_show":
        nurture_badge = (
            ' <span style="background:#e67e22; color:white; font-size:11px; '
            'padding:2px 6px; border-radius:3px; margin-left:6px;">REBOOK</span>'
        )

    # FU type label from cadence
    _, fu_type, _ = cadence[fu_number]

    # Overdue badge
    overdue_badge = ""
    if days_overdue > 0:
        overdue_badge = (
            f' <span style="background:#c0392b; color:white; font-size:11px; '
            f'padding:2px 6px; border-radius:3px; margin-left:6px;">'
            f'OVERDUE {days_overdue}d</span>'
        )
    no_show_badge = ""
    if no_show and cadence_type != "no_show":
        no_show_badge = (
            ' <span style="background:#c0392b; color:white; font-size:11px; '
            'padding:2px 6px; border-radius:3px; margin-left:6px;">NO-SHOW</span>'
        )

    bar_color = "#8e44ad" if cadence_type == "nurture" else ("#e67e22" if cadence_type == "no_show" else "#2E5B88")
    progress_bar = build_progress_bar(fu_done, total=max_touches, completed_color=bar_color)

    # Build collapsible previous emails section
    prev_emails_html = ""
    if sent_emails:
        email_items = ""
        for i, em in enumerate(sent_emails, 1):
            subj = html_mod.escape(em.get("subject", "(no subject)"))
            body = html_mod.escape(em.get("body", ""))
            email_items += (
                f'<div style="border-left:3px solid #ddd; padding:8px 12px; margin-bottom:8px; background:#fafafa;">'
                f'<div style="font-size:12px; font-weight:600; color:#2E5B88; margin-bottom:4px;">Email {i}: {subj}</div>'
                f'<div style="font-size:12px; color:#555; white-space:pre-wrap; line-height:1.5;">{body}</div>'
                f'</div>'
            )
        prev_emails_html = (
            f'<details style="margin-top:12px; border-top:1px solid #eee; padding-top:8px;">'
            f'<summary style="cursor:pointer; font-size:13px; color:#2E5B88; font-weight:600;">'
            f'Previous Emails ({len(sent_emails)})</summary>'
            f'<div style="margin-top:8px;">{email_items}</div>'
            f'</details>'
        )

    accent_color = "#8e44ad" if cadence_type == "nurture" else ("#e67e22" if cadence_type == "no_show" else "#2E5B88")
    border_color = "#8e44ad" if cadence_type == "nurture" else ("#e67e22" if cadence_type == "no_show" else "#ddd")

    parsed = _parse_ai_sections(claude_output)
    draft_html = parsed["draft"] or parsed["raw"]
    reasoning = (parsed["reasoning"] or "").strip()
    priority = (parsed["priority"] or "").strip()

    reasoning_box = ""
    if reasoning:
        reasoning_box = f"""
        <details style="border:1px solid #eee; border-radius:8px; padding:10px 12px; background:#fbfbfb; margin-bottom:10px;">
          <summary style="cursor:pointer; font-size:11px; letter-spacing:0.02em; text-transform:uppercase; color:#888; font-weight:800;">
            Why This Tip/Resource
          </summary>
          <div style="margin-top:8px; font-size:13px; color:#333; line-height:1.5; white-space:pre-wrap;">{html_mod.escape(reasoning)}</div>
        </details>
        """

    priority_box = ""
    if priority:
        priority_box = f"""
        <div style="border:1px solid #eee; border-radius:8px; padding:10px 12px; background:#fbfbfb;">
          <div style="font-size:11px; letter-spacing:0.02em; text-transform:uppercase; color:#888; font-weight:700; margin-bottom:6px;">
            Priority
          </div>
          <div style="font-size:13px; color:#333; font-weight:700;">{html_mod.escape(priority)}</div>
        </div>
        """

    # Copy helpers (browser preview only; email clients will ignore JS).
    copy_subject = f"Rebook {fu_number}: {fu_type}" if cadence_type == "no_show" else f"Follow-up {fu_number}: {fu_type}"
    copy_body = (parsed["draft"] or parsed["raw"] or "").strip()
    copy_all = f"Subject: {copy_subject}\n\n{copy_body}".strip()
    transcript_chip = ""
    if transcript_label:
        transcript_chip = f'<span style="color:#666; font-size:12px; margin-left:10px;">Transcript: {html_mod.escape(transcript_label)}</span>'
    transcript_state = "yes" if granola_found else "no"

    return f"""
    <div class="lw-filter-item lw-lead-card" data-owner="{html_mod.escape(owner_name)}" data-transcript="{transcript_state}" data-no-show={"1" if no_show else "0"} data-overdue={"1" if days_overdue > 0 else "0"} style="border:1px solid {border_color}; border-radius:8px; padding:16px; margin-bottom:20px; background:#fff;">
      <h3 style="margin:0 0 4px 0; color:#1a1a1a;">
        <a href="{close_url}" style="color:{accent_color}; text-decoration:none;">{name}</a>{location_str}{nurture_badge}{transcript_badge}{overdue_badge}{no_show_badge}
      </h3>
      <div style="font-size:13px; color:#666; margin-bottom:8px;">
        <span style="font-weight:600; color:{accent_color};">Follow-up {fu_number} of {max_touches}</span>
        <span style="color:#999; margin:0 4px;">|</span>
        <span style="color:#E67E22;">{fu_type}</span>
        <span style="color:#999; margin:0 4px;">|</span>
        {days_since_call}d since call
        {transcript_chip}
      </div>
      <div style="display:flex; gap:8px; align-items:center; margin:10px 0 8px 0;">
        <button class="lw-copy-btn" data-copy-text={html_mod.escape(json.dumps(copy_body))} style="border:1px solid #ddd; background:#fff; border-radius:6px; padding:6px 10px; font-size:12px; cursor:pointer;">
          Copy draft
        </button>
        <button class="lw-copy-btn" data-copy-text={html_mod.escape(json.dumps(copy_all))} style="border:1px solid #ddd; background:#fff; border-radius:6px; padding:6px 10px; font-size:12px; cursor:pointer;">
          Copy subject + draft
        </button>
      </div>
      <div style="margin-bottom:10px;">{progress_bar}</div>
      <div style="font-size:13px; color:#666; margin-bottom:12px;">
        Budget: {budget} | Health Spend: {health_spend} | Home: {sq_footage}<br>
        Source: {source} | Status: {meeting_status}
      </div>
      <table style="width:100%; border-collapse:separate; border-spacing:0; margin-top:10px;">
        <tr>
          <td style="vertical-align:top; padding-right:14px; width:68%;">
            <div style="white-space:pre-wrap; font-size:14px; line-height:1.6; color:#1a1a1a;">{draft_html}</div>
            {prev_emails_html}
          </td>
          <td style="vertical-align:top; width:32%;">
            {reasoning_box}
            {priority_box}
          </td>
        </tr>
      </table>
    </div>"""


def build_digest_html(sections_by_owner, date_str, total_leads,
                      tracker_html="",
                      action_items_by_owner=None,
                      sections_by_owner_noshow=None,
                      action_items_by_owner_noshow=None,
                      run_meta=None):
    """Build the full HTML digest email."""
    if action_items_by_owner is None:
        action_items_by_owner = {}
    if sections_by_owner_noshow is None:
        sections_by_owner_noshow = {}
    if action_items_by_owner_noshow is None:
        action_items_by_owner_noshow = {}
    if run_meta is None:
        run_meta = {}

    dynamic_owners = set(
        list(action_items_by_owner.keys()) + list(sections_by_owner.keys())
        + list(action_items_by_owner_noshow.keys()) + list(sections_by_owner_noshow.keys())
    )
    owners = []
    for o in OWNER_TAB_ORDER:
        owners.append(o)
        dynamic_owners.discard(o)
    owners.extend(sorted(dynamic_owners))
    owner_counts = {
        o: len(sections_by_owner.get(o, [])) + len(sections_by_owner_noshow.get(o, []))
        for o in owners
    }

    # Compact action list at the top (split into completed and no-show)
    def _build_action_rows(items, owner):
        rows = ""
        for it in items:
            transcript_state = "yes" if str(it.get("transcript_state", "no")) == "yes" else "no"
            rows += f"""
            <tr class="lw-filter-item" data-owner="{html_mod.escape(owner)}" data-transcript="{transcript_state}" data-no-show={"1" if it.get('no_show') else "0"} data-overdue={"1" if it.get('overdue') else "0"} style="border-bottom:1px solid #eee;">
              <td style="padding:8px 10px; font-size:13px;">
                <a href="{it.get('close_url','')}" style="color:#2E5B88; text-decoration:none;">{html_mod.escape(it.get('name',''))}</a>
                <span style="color:#999; margin-left:6px; font-size:12px;">FU {it.get('fu_number')}</span>
                {'<span style="background:#c0392b; color:white; font-size:10px; padding:1px 5px; border-radius:3px; margin-left:6px;">NO-SHOW</span>' if it.get('no_show') else ''}
              </td>
              <td style="padding:8px 6px; font-size:12px; text-align:center; color:#666;">{html_mod.escape(it.get('transcript_label',''))}</td>
              <td style="padding:8px 6px; text-align:right; white-space:nowrap;">
                <button class="lw-copy-btn" data-copy-text={html_mod.escape(json.dumps(it.get('copy_draft','')))} style="border:1px solid #ddd; background:#fff; border-radius:6px; padding:6px 10px; font-size:12px; cursor:pointer;">
                  Copy
                </button>
                <button class="lw-copy-btn" data-copy-text={html_mod.escape(json.dumps(it.get('copy_all','')))} style="border:1px solid #ddd; background:#fff; border-radius:6px; padding:6px 10px; font-size:12px; cursor:pointer; margin-left:6px;">
                  Copy + Subject
                </button>
              </td>
            </tr>
            """
        return rows

    def _build_action_table(rows):
        return f"""
          <table style="width:100%; border-collapse:collapse; background:#fff; border:1px solid #eee; border-radius:8px; overflow:hidden;">
            <tr style="border-bottom:2px solid #ddd;">
              <th style="padding:6px 10px; text-align:left; font-size:11px; color:#888; text-transform:uppercase;">Lead</th>
              <th style="padding:6px 6px; text-align:center; font-size:11px; color:#888; text-transform:uppercase;">Transcript</th>
              <th style="padding:6px 6px; text-align:right; font-size:11px; color:#888; text-transform:uppercase;">Copy</th>
            </tr>
            {rows}
          </table>"""

    action_blocks = ""
    for owner in owners:
        completed_items = action_items_by_owner.get(owner, [])
        noshow_items = action_items_by_owner_noshow.get(owner, [])
        if not completed_items and not noshow_items:
            continue

        owner_slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in owner).strip("-")
        inner = ""

        if completed_items:
            rows = _build_action_rows(completed_items, owner)
            inner += f"""
            <div style="font-size:11px; color:#2E5B88; font-weight:700; letter-spacing:0.02em; text-transform:uppercase; margin:0 0 4px 0;">Calls Completed ({len(completed_items)})</div>
            {_build_action_table(rows)}"""

        if noshow_items:
            rows = _build_action_rows(noshow_items, owner)
            inner += f"""
            <div style="font-size:11px; color:#c0392b; font-weight:700; letter-spacing:0.02em; text-transform:uppercase; margin:{('12px' if completed_items else '0')} 0 4px 0;">Rebook ({len(noshow_items)})</div>
            {_build_action_table(rows)}"""

        action_blocks += f"""
        <div class="lw-owner-group lw-owner-scope" data-owner-group="{html_mod.escape(owner)}" data-owner-scope="{html_mod.escape(owner)}" style="margin-top:14px;">
          <div style="font-size:12px; color:#888; font-weight:800; letter-spacing:0.02em; text-transform:uppercase; margin:0 0 6px 0;">
            <a id="section-actions-{owner_slug}" style="color:inherit; text-decoration:none;">{html_mod.escape(owner)}: Action List</a>
          </div>
          {inner}
        </div>
        """

    owner_blocks = ""
    for owner in owners:
        completed_sections = sections_by_owner.get(owner, [])
        noshow_sections = sections_by_owner_noshow.get(owner, [])
        if not completed_sections and not noshow_sections:
            continue
        total_count = len(completed_sections) + len(noshow_sections)
        owner_slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in owner).strip("-")

        inner_blocks = ""
        if completed_sections:
            inner_blocks += f"""
      <div style="font-size:13px; color:#2E5B88; font-weight:700; letter-spacing:0.02em; text-transform:uppercase; margin:0 0 10px 0; padding:6px 10px; background:#eaf1f8; border-radius:4px;">Calls Completed ({len(completed_sections)})</div>
      {"".join(completed_sections)}"""

        if noshow_sections:
            inner_blocks += f"""
      <div style="font-size:13px; color:#c0392b; font-weight:700; letter-spacing:0.02em; text-transform:uppercase; margin:{('20px' if completed_sections else '0')} 0 10px 0; padding:6px 10px; background:#fdf0ef; border-radius:4px;">Rebook ({len(noshow_sections)})</div>
      {"".join(noshow_sections)}"""

        owner_blocks += f"""
    <div class="lw-owner-group lw-owner-scope" data-owner-group="{html_mod.escape(owner)}" data-owner-scope="{html_mod.escape(owner)}" style="margin-top:28px;">
      <h2 style="background:#2E5B88; color:white; padding:10px 16px; border-radius:6px; margin:0 0 16px 0; font-size:16px;">
        <a id="section-owner-{owner_slug}" style="color:inherit; text-decoration:none;">{owner.upper()}'S FOLLOW-UPS ({total_count})</a>
      </h2>
      {inner_blocks}
    </div>"""

    # Owner tabs
    tab_buttons = '<button class="lw-tab active" role="tab" aria-selected="true" tabindex="0" data-owner-tab="ALL">All</button>'
    for o in owners:
        tab_buttons += (
            f'<button class="lw-tab" role="tab" aria-selected="false" tabindex="-1" '
            f'data-owner-tab="{html_mod.escape(o)}">{html_mod.escape(o)} '
            f'<span class="lw-tab-count">{owner_counts.get(o, 0)}</span></button>'
        )

    jump_links = (
        '<a class="lw-jump" href="#section-today">Today</a>'
        '<a class="lw-jump" href="#section-pipeline">Pipeline</a>'
    )
    for owner in owners:
        owner_slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in owner).strip("-")
        if owner_counts.get(owner, 0) > 0:
            jump_links += (
                f'<a class="lw-jump" href="#section-owner-{owner_slug}">{html_mod.escape(owner)}</a>'
            )
        else:
            jump_links += (
                f'<span class="lw-jump lw-jump-disabled">{html_mod.escape(owner)}</span>'
            )

    last_run = run_meta.get("last_run", "")
    mcp_status = run_meta.get("mcp_status", "")
    missing_transcripts = int(run_meta.get("missing_transcripts", 0) or 0)

    banner = ""
    if mcp_status:
        banner = f"""
        <div style="border:1px solid #f1c40f; background:#fff8db; color:#7a5b00; border-radius:8px; padding:10px 12px; margin-bottom:14px; font-size:13px;">
          {html_mod.escape(mcp_status)}
        </div>
        """
    elif missing_transcripts:
        banner = f"""
        <div style="border:1px solid #e67e22; background:#fff3e8; color:#8a4b12; border-radius:8px; padding:10px 12px; margin-bottom:14px; font-size:13px;">
          {missing_transcripts} lead(s) are missing transcripts today.
        </div>
        """

    return f"""<!DOCTYPE html>
	<html>
	<head>
	  <meta charset="utf-8">
	  <style>
	    body {{
	      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
	      max-width:700px;
	      margin:0 auto;
	      padding:20px;
	      background:#FFFCF0;
	      color:#1a1a1a;
	    }}
	    .lw-hidden {{ display:none !important; }}
	    .lw-top {{
	      position:fixed;
	      top:0;
	      left:50%;
	      transform:translateX(-50%);
	      width:min(700px, calc(100vw - 40px));
	      background:#FFFCF0;
	      padding:10px 0 12px 0;
	      z-index:999;
	      border-bottom:1px solid #e9e2c9;
	    }}
	    .lw-top-title {{
	      display:flex;
	      align-items:flex-end;
	      justify-content:space-between;
	      gap:12px;
	    }}
	    .lw-tabs {{
	      display:flex;
	      gap:6px;
	      flex-wrap:nowrap;
	      overflow-x:auto;
	      padding-bottom:2px;
	    }}
	    .lw-tab {{
	      border:1px solid #ddd;
	      background:#fff;
	      border-radius:999px;
	      padding:6px 10px;
	      font-size:12px;
	      cursor:pointer;
	      white-space:nowrap;
	    }}
	    .lw-tab:focus-visible {{
	      outline:2px solid #2E5B88;
	      outline-offset:2px;
	    }}
	    .lw-tab.active {{
	      border-color:#2E5B88;
	      background:#2E5B88;
	      color:#fff;
	    }}
	    .lw-tab-count {{
	      display:inline-block;
	      margin-left:4px;
	      font-size:11px;
	      opacity:0.85;
	    }}
	    .lw-jumps {{
	      display:flex;
	      flex-wrap:nowrap;
	      overflow-x:auto;
	      gap:8px;
	      margin-top:8px;
	      padding-top:8px;
	      border-top:1px solid #ece3c8;
	    }}
	    .lw-jump {{
	      font-size:12px;
	      color:#2E5B88;
	      text-decoration:none;
	      border:1px solid #d6e3f0;
	      background:#f6fbff;
	      border-radius:999px;
	      padding:4px 10px;
	      white-space:nowrap;
	    }}
	    .lw-jump-disabled {{
	      color:#999;
	      border-color:#e5e5e5;
	      background:#f8f8f8;
	    }}
	    .lw-filters {{
	      display:flex;
	      gap:8px;
	      flex-wrap:wrap;
	      align-items:center;
	      margin-top:8px;
	    }}
	    .lw-filter-chip {{
	      border:1px solid #ddd;
	      background:#fff;
	      border-radius:999px;
	      padding:5px 10px;
	      font-size:12px;
	      cursor:pointer;
	    }}
	    .lw-filter-chip.active {{
	      border-color:#1e7f5c;
	      color:#fff;
	      background:#1e7f5c;
	    }}
	    .lw-count {{
	      margin-top:8px;
	      font-size:12px;
	      color:#666;
	    }}
	    @media (max-width: 760px) {{
	      body {{ padding:12px; }}
	      .lw-top {{ width:calc(100vw - 24px); }}
	      .lw-top-title {{ flex-direction:column; align-items:flex-start; }}
	    }}
	  </style>
	</head>
	<body>
	  <div id="lwMenu" class="lw-top">
	    <div class="lw-top-title">
	      <div>
	        <div style="font-size:20px; font-weight:800; color:#1a1a1a;">Lightwork Follow-Up Digest</div>
	        <div style="margin-top:2px; color:#666; font-size:13px;">
	          {date_str} &middot; {total_leads} lead{"s" if total_leads != 1 else ""} due
	          {f"&middot; Last run: {html_mod.escape(last_run)}" if last_run else ""}
	        </div>
	      </div>
	      <div class="lw-tabs" role="tablist" aria-label="Owner tabs">
	        {tab_buttons}
	      </div>
	    </div>
	    <div class="lw-filters">
	      <span style="font-size:12px; color:#666; font-weight:700;">Filters:</span>
	      <button class="lw-filter-chip active" data-filter-key="transcript" data-filter-value="all">Transcript: Any</button>
	      <button class="lw-filter-chip" data-filter-key="transcript" data-filter-value="yes">Transcript: Yes</button>
	      <button class="lw-filter-chip" data-filter-key="transcript" data-filter-value="no">Transcript: No</button>
	      <button class="lw-filter-chip active" data-filter-key="no_show" data-filter-value="all">No-show: Any</button>
	      <button class="lw-filter-chip" data-filter-key="no_show" data-filter-value="1">No-show only</button>
	      <button class="lw-filter-chip active" data-filter-key="overdue" data-filter-value="all">Overdue: Any</button>
	      <button class="lw-filter-chip" data-filter-key="overdue" data-filter-value="1">Overdue only</button>
	    </div>
	    <div class="lw-jumps">
	      {jump_links}
	    </div>
	    <div id="lwResultCount" class="lw-count"></div>
	  </div>
	  <div id="lwMenuSpacer" style="height:130px;"></div>
	  {banner}
	  <div id="section-today" style="margin-bottom:18px;">
	    <h2 style="margin:0 0 6px 0; font-size:18px; color:#1a1a1a;">Today</h2>
	    <div style="color:#666; font-size:13px;">Copy and send the drafts below. Use tabs to focus on one owner.</div>
	    {action_blocks}
	  </div>
	  <div id="section-pipeline">{tracker_html}</div>
	  {owner_blocks}
	  <div style="text-align:center; padding:20px 0; margin-top:30px; border-top:1px solid #ddd; color:#999; font-size:12px;">
	    Auto-generated by Lightwork Follow-Up Tracker. Drafts are suggestions, tweak as needed.
	  </div>
	  <script>
	  (function() {{
	    var state = {{
	      owner: 'ALL',
	      transcript: 'all',
	      no_show: 'all',
	      overdue: 'all'
	    }};

	    function syncMenuSpacer() {{
	      var menu = document.getElementById('lwMenu');
	      var spacer = document.getElementById('lwMenuSpacer');
	      if (menu && spacer) {{
	        spacer.style.height = menu.offsetHeight + 'px';
	      }}
	    }}

	    function applyVisibility() {{
	      var visibleCards = 0;
	      var visibleRows = 0;
	      document.querySelectorAll('.lw-filter-item').forEach(function(el) {{
	        var owner = el.getAttribute('data-owner') || 'Unassigned';
	        var transcript = el.getAttribute('data-transcript') || 'no';
	        var noShow = el.getAttribute('data-no-show') || '0';
	        var overdue = el.getAttribute('data-overdue') || '0';

	        var ownerMatch = state.owner === 'ALL' || owner === state.owner;
	        var transcriptMatch = state.transcript === 'all' || transcript === state.transcript;
	        var noShowMatch = state.no_show === 'all' || noShow === state.no_show;
	        var overdueMatch = state.overdue === 'all' || overdue === state.overdue;

	        var show = ownerMatch && transcriptMatch && noShowMatch && overdueMatch;
	        el.classList.toggle('lw-hidden', !show);
	        if (show && el.classList.contains('lw-lead-card')) visibleCards += 1;
	        if (show && el.tagName === 'TR') visibleRows += 1;
	      }});

	      document.querySelectorAll('.lw-owner-scope').forEach(function(scope) {{
	        var owner = scope.getAttribute('data-owner-scope') || 'Unassigned';
	        scope.classList.toggle('lw-hidden', !(state.owner === 'ALL' || owner === state.owner));
	      }});

	      document.querySelectorAll('.lw-owner-group').forEach(function(group) {{
	        var hasVisibleItems = !!group.querySelector('.lw-filter-item:not(.lw-hidden)');
	        if (group.classList.contains('lw-owner-scope') && group.classList.contains('lw-hidden')) {{
	          return;
	        }}
	        group.classList.toggle('lw-hidden', !hasVisibleItems);
	      }});

	      var count = document.getElementById('lwResultCount');
	      if (count) {{
	        count.textContent = 'Showing ' + visibleCards + ' draft card(s) and ' + visibleRows + ' table row(s).';
	      }}
	    }}

	    function setActiveOwner(owner) {{
	      state.owner = owner;
	      document.querySelectorAll('.lw-tab').forEach(function(btn) {{
	        var selected = btn.getAttribute('data-owner-tab') === owner;
	        btn.classList.toggle('active', selected);
	        btn.setAttribute('aria-selected', selected ? 'true' : 'false');
	        btn.setAttribute('tabindex', selected ? '0' : '-1');
	      }});
	      applyVisibility();
	    }}

	    document.querySelectorAll('.lw-tab').forEach(function(btn) {{
	      btn.addEventListener('click', function() {{
	        setActiveOwner(btn.getAttribute('data-owner-tab'));
	      }});
	      btn.addEventListener('keydown', function(ev) {{
	        if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(ev.key)) return;
	        ev.preventDefault();
	        var tabs = Array.from(document.querySelectorAll('.lw-tab'));
	        var idx = tabs.indexOf(btn);
	        if (ev.key === 'ArrowRight') idx = (idx + 1) % tabs.length;
	        if (ev.key === 'ArrowLeft') idx = (idx - 1 + tabs.length) % tabs.length;
	        if (ev.key === 'Home') idx = 0;
	        if (ev.key === 'End') idx = tabs.length - 1;
	        tabs[idx].focus();
	        setActiveOwner(tabs[idx].getAttribute('data-owner-tab'));
	      }});
	    }});

	    document.querySelectorAll('.lw-filter-chip').forEach(function(chip) {{
	      chip.addEventListener('click', function() {{
	        var key = chip.getAttribute('data-filter-key');
	        var val = chip.getAttribute('data-filter-value');
	        state[key] = val;
	        document.querySelectorAll('.lw-filter-chip[data-filter-key=\"' + key + '\"]').forEach(function(c) {{
	          c.classList.toggle('active', c === chip);
	        }});
	        applyVisibility();
	      }});
	    }});

	    document.querySelectorAll('.lw-copy-btn').forEach(function(btn) {{
	      btn.addEventListener('click', async function() {{
	        try {{
	          var raw = btn.getAttribute('data-copy-text');
	          var text = JSON.parse(raw);
	          await navigator.clipboard.writeText(text);
	          var prev = btn.textContent;
	          btn.textContent = 'Copied';
	          setTimeout(function() {{ btn.textContent = prev; }}, 900);
	        }} catch (e) {{
	          btn.textContent = 'Copy failed';
	          setTimeout(function() {{ btn.textContent = 'Copy'; }}, 900);
	        }}
	      }});
	    }});

	    window.addEventListener('resize', syncMenuSpacer);
	    syncMenuSpacer();
	    setActiveOwner('ALL');
	  }})();
	  </script>
	</body>
	</html>"""


# ---------------------------------------------------------------------------
# Transcript sync (one-time bulk pull from Granola MCP)
# ---------------------------------------------------------------------------


def _sync_transcripts_from_mcp(batch_size: int = 20):
    """Connect to Granola MCP, list all meetings, fetch only missing transcripts, save to DB.

    Uses exponential backoff on rate limits and stops after 3 consecutive failures.
    Limits new fetches per run to batch_size to avoid hitting rate limits.
    """
    print("Transcript sync: connecting to Granola MCP...")

    if not GRANOLA_MCP_ENABLE:
        print("Error: Set GRANOLA_MCP_ENABLE=1 to use MCP sync.")
        print("  Example: GRANOLA_MCP_ENABLE=1 python3 post_call_digest.py --sync-transcripts")
        return

    try:
        mcp_client = GranolaMCPClient(enable_interactive_login=True)
        mcp_client.initialize()
    except Exception as e:
        print(f"Error connecting to Granola MCP: {e}")
        return

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=730)  # 2 years back

    print("Listing meetings (last 2 years)...")
    try:
        meetings = mcp_list_meetings(mcp_client, since, now)
    except Exception as e:
        print(f"Error listing meetings: {e}")
        return
    print(f"  Found {len(meetings)} total meetings")

    # Filter to Lightwork calls only (by title or team email in attendees)
    lw_meetings = []
    for m in meetings:
        title = (m.get("title") or "").lower()
        emails = m.get("emails") or set()
        is_lightwork = "lightwork" in title or bool(emails & TEAM_EMAILS)
        if is_lightwork:
            lw_meetings.append(m)
    print(f"  {len(lw_meetings)} Lightwork calls (filtered)")

    transcript_db = _init_transcript_db()

    # Clean up false empties from previous rate-limited runs
    cleaned = transcript_db.execute(
        "DELETE FROM transcripts WHERE transcript_text = '' AND source = 'mcp'"
    ).rowcount
    transcript_db.commit()
    if cleaned:
        print(f"  Cleaned {cleaned} empty records from previous rate-limited runs")

    already_cached = 0
    newly_fetched = 0
    empty = 0
    errors = 0
    consecutive_rate_limits = 0
    backoff_seconds = 2.0  # Starting delay between requests
    max_backoff = 30.0
    rate_limit_backoffs = [10, 20, 30]  # Escalating waits on consecutive rate limits

    for i, m in enumerate(lw_meetings, 1):
        mid = m.get("id", "")
        if not mid:
            continue

        # Skip if already in DB with real content
        cached = _get_cached_transcript(transcript_db, mid)
        if cached is not None and cached["transcript_text"]:
            already_cached += 1
            continue

        # Batch limit: stop fetching new transcripts after batch_size
        if newly_fetched + empty >= batch_size:
            remaining = len(lw_meetings) - i - already_cached
            print(f"\n  Batch limit reached ({batch_size}). ~{remaining} meetings remain.")
            print("  Re-run to fetch the next batch.")
            break

        # Fetch transcript using the function that raises on rate limit
        try:
            text = mcp_get_transcript_text(mcp_client, mid)
            consecutive_rate_limits = 0  # Reset on success
            backoff_seconds = 2.0  # Reset backoff on success
        except GranolaRateLimitError:
            consecutive_rate_limits += 1
            if consecutive_rate_limits >= 3:
                remaining = len(lw_meetings) - i - already_cached
                print(f"\n  Stopped: 3 consecutive rate limits. ~{remaining} meetings remain.")
                print("  Wait a few minutes, then re-run to continue.")
                errors += consecutive_rate_limits
                break
            wait = rate_limit_backoffs[min(consecutive_rate_limits - 1, len(rate_limit_backoffs) - 1)]
            print(f"  [{i}/{len(lw_meetings)}] Rate limited, waiting {wait}s (attempt {consecutive_rate_limits}/3)...")
            time.sleep(wait)
            errors += 1
            continue
        except Exception as e:
            print(f"  [{i}/{len(lw_meetings)}] Error fetching {mid}: {e}")
            errors += 1
            time.sleep(backoff_seconds)
            continue

        if text:
            _save_transcript(transcript_db, mid, transcript_text=text, source="mcp")
            newly_fetched += 1
        else:
            # Genuinely empty transcript (not a rate limit error)
            _save_transcript(transcript_db, mid, transcript_text="", source="mcp")
            empty += 1

        if (newly_fetched + empty) % 10 == 0:
            print(f"  [{i}/{len(lw_meetings)}] {newly_fetched} new, {already_cached} cached, {empty} empty...")

        time.sleep(backoff_seconds)  # Pace requests

    transcript_db.close()
    print(f"\nSync complete:")
    print(f"  {already_cached} already cached (skipped)")
    print(f"  {newly_fetched} newly fetched with transcripts")
    print(f"  {empty} meetings with no transcript")
    if errors:
        print(f"  {errors} errors/rate-limits")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Lightwork Follow-Up Digest")
    parser.add_argument("--fresh", action="store_true", help="Ignore draft cache, regenerate all")
    parser.add_argument("--no-email", action="store_true", help="Skip sending reminder emails")
    parser.add_argument("--no-open", action="store_true", help="Don't open browser (for CI/server use)")
    parser.add_argument("--debug-lead", type=str, default="", help="Print detailed email history for a lead (by name substring)")
    parser.add_argument("--sync-transcripts", action="store_true",
                        help="Bulk-pull missing transcripts from Granola MCP into local DB, then exit")
    parser.add_argument("--batch-size", type=int, default=20,
                        help="Max new transcripts to fetch per sync run (default: 20)")
    args = parser.parse_args()

    # --sync-transcripts: one-time bulk pull, then exit
    if args.sync_transcripts:
        _sync_transcripts_from_mcp(batch_size=args.batch_size)
        return

    # Validate required env vars early
    if not CLOSE_API_KEY:
        print("Error: CLOSE_API_KEY not set. Add it to .env or export it.")
        return
    if not SKIP_CLAUDE and AI_PROVIDER == "anthropic" and not ANTHROPIC_API_KEY:
        print("Error: ANTHROPIC_API_KEY not set. Add it to .env or set SKIP_CLAUDE=1.")
        return
    if not SKIP_CLAUDE and AI_PROVIDER == "openai" and not OPENAI_API_KEY:
        print("Error: OPENAI_API_KEY not set. Add it to .env or set SKIP_CLAUDE=1.")
        return

    # Initialize caches
    draft_db = _init_draft_db()
    transcript_db = _init_transcript_db()

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%b %-d, %Y")
    last_run_str = now.astimezone().strftime("%Y-%m-%d %H:%M")

    print(f"Lightwork Follow-Up Digest - {date_str}")
    print("=" * 60)

    # 1. Fetch all meetings once, then filter for completed and no-show
    since = now - timedelta(days=CADENCE_LOOKBACK_DAYS)
    print(f"Fetching all meetings from last {CADENCE_LOOKBACK_DAYS} days...")
    all_raw_meetings = _fetch_all_meetings(since, now)
    print(f"  {len(all_raw_meetings)} total meetings fetched")

    customer_leads = get_recent_customer_leads(all_meetings=all_raw_meetings)

    if not customer_leads:
        print("No Customer Leads with recent calls. Nothing to digest.")
        return

    # Flag leads whose MOST RECENT meeting was a no-show/canceled.
    # (A lead with both a completed call and a later canceled reschedule
    #  should only be flagged if the canceled one is the latest.)
    no_show_meetings = _filter_meetings_in_range(all_raw_meetings, since, now, NO_SHOW_STATUSES)
    no_show_by_lead = {}  # lead_id -> latest no-show start time
    for m in no_show_meetings:
        lid = m.get("lead_id", "")
        if not lid:
            continue
        starts = m.get("starts_at", "")
        if starts > no_show_by_lead.get(lid, ""):
            no_show_by_lead[lid] = starts

    for lead_id, info in customer_leads.items():
        # Only flag as no-show if the latest no-show meeting is more recent
        # than the latest completed meeting for this lead.
        latest_completed = max(
            (m.get("starts_at", "") for m in info["meetings"]), default=""
        )
        latest_no_show = no_show_by_lead.get(lead_id, "")
        info["no_show"] = bool(latest_no_show and latest_no_show > latest_completed)
        # Override cadence_type for no-show leads (unless already nurture)
        if info["no_show"] and info.get("cadence_type") != "nurture":
            info["cadence_type"] = "no_show"

    # 2. Load Granola transcripts early so the tracker can show transcript status.
    # Prefer Granola MCP (authoritative transcripts), then fall back to Sheet/local cache.
    mcp_client = None
    mcp_meetings = []
    mcp_transcripts = {}  # meeting_id -> transcript_text
    mcp_status = ""

    if GRANOLA_MCP_ENABLE:
        try:
            print("\nConnecting to Granola MCP...")
            mcp_client = GranolaMCPClient(enable_interactive_login=True)
            mcp_client.initialize()
            mcp_meetings = mcp_list_meetings(mcp_client, since, now)
            print(f"  {len(mcp_meetings)} meetings from MCP (last {CADENCE_LOOKBACK_DAYS} days)")
        except Exception as e:
            print(f"  Warning: Granola MCP unavailable: {e}")
            mcp_client = None
            mcp_status = f"Granola MCP unavailable: {e}"
    else:
        mcp_status = ""

    # Load local Granola cache docs as matchable meetings (works without MCP)
    local_granola_meetings = _load_local_granola_meetings()
    if local_granola_meetings:
        print(f"  {len(local_granola_meetings)} meetings from local Granola cache")

    print("\nLoading Granola transcripts (fallback sources)...")
    sheet_rows = load_granola_sheet()
    print(f"  {len(sheet_rows)} rows from Google Sheet")

    granola_docs, granola_transcripts = load_granola_cache()
    print(f"  {len(granola_docs)} docs from local Granola cache")

    # Annotate each lead with transcript status for tracker view
    for lead_id, info in customer_leads.items():
        if info.get("no_show"):
            info["transcript_label"] = "No-show"
            continue

        earliest_meeting = min(info["meetings"], key=lambda m: m.get("starts_at", ""))

        # MCP first (match meeting, then check DB before live fetch)
        if mcp_client and mcp_meetings:
            m = mcp_match_meeting(earliest_meeting, mcp_meetings)
            if m:
                mid = m["id"]
                info["mcp_meeting_id"] = mid
                # Check transcript DB first
                cached = _get_cached_transcript(transcript_db, mid)
                if cached is not None and cached["transcript_text"]:
                    mcp_transcripts[mid] = cached["transcript_text"]
                    info["transcript_label"] = "Yes (cached)"
                    continue
                if cached is not None:
                    # Empty transcript in DB (meeting had no transcript)
                    mcp_transcripts[mid] = ""
                    info["transcript_label"] = "No (cached)"
                    continue
                # Not in DB: fetch from MCP and save
                if mid not in mcp_transcripts:
                    try:
                        mcp_transcripts[mid] = mcp_get_transcript_text(mcp_client, mid)
                    except GranolaRateLimitError:
                        # Don't save empty record on rate limit; skip so we retry later
                        mcp_transcripts[mid] = ""
                        info["transcript_label"] = "Rate limited"
                        continue
                    except Exception:
                        mcp_transcripts[mid] = ""
                    _save_transcript(transcript_db, mid,
                                     transcript_text=mcp_transcripts[mid], source="mcp")
                if mcp_transcripts[mid]:
                    info["transcript_label"] = "Yes (MCP)"
                    continue
                info["transcript_label"] = "No (MCP)"
                continue

        # Local Granola cache DB lookup (works without MCP API)
        if local_granola_meetings:
            m = mcp_match_meeting(earliest_meeting, local_granola_meetings)
            if m:
                mid = m["id"]
                info["mcp_meeting_id"] = mid
                cached = _get_cached_transcript(transcript_db, mid)
                if cached is not None and cached["transcript_text"]:
                    mcp_transcripts[mid] = cached["transcript_text"]
                    info["transcript_label"] = "Yes (cached)"
                    continue

        # Fallback: Sheet/local cache
        src, match_obj = get_granola_match(earliest_meeting, sheet_rows, granola_docs)
        info["transcript_label"] = get_transcript_label(src, match_obj, granola_transcripts)

    # 3. Determine which leads need follow-up today
    print(f"\nChecking follow-up status for {len(customer_leads)} leads...")
    due_leads, all_leads_status = get_leads_due_today(customer_leads, debug_lead_name=args.debug_lead)

    # Build tracker view (always, even if no leads due today)
    tracker_html = build_tracker_view(all_leads_status)

    if not due_leads:
        # Still output the tracker even with no drafts to generate
        html = build_digest_html({}, date_str, 0, tracker_html=tracker_html)
        output_path = SCRIPT_DIR / "digest_preview.html"
        output_path.write_text(html)
        print(f"\nNo follow-ups due today. Tracker saved to {output_path}")
        if not args.no_open:
            subprocess.run(["open", str(output_path)])
        return

    total_due = len(due_leads)
    # Cap per owner (e.g. max 20 leads per person per day)
    owner_counts = {}
    capped_leads = []
    skipped = 0
    for entry in due_leads:
        owner = entry["owner_name"]
        owner_counts[owner] = owner_counts.get(owner, 0) + 1
        if owner_counts[owner] <= MAX_LEADS_PER_OWNER:
            capped_leads.append(entry)
        else:
            skipped += 1
    due_leads = capped_leads
    if skipped:
        print(f"\n{total_due} leads due, capped to {len(due_leads)} ({MAX_LEADS_PER_OWNER}/owner max, {skipped} deferred)")
    else:
        print(f"\n{total_due} leads due for follow-up today")

    # 4. Process each due lead
    # Load reference files once for all leads
    cached_sales_scripts = _load_condensed_or_fallback(
        SALES_TIPS_CONDENSED_PATH, SALES_SCRIPTS_PATH, fallback_cap=3000
    )
    cached_followup_examples = _load_condensed_or_fallback(
        VOICE_GUIDE_CONDENSED_PATH, FOLLOWUP_EXAMPLES_PATH, fallback_cap=2000
    )

    sections_by_owner = {}          # owner -> list of HTML sections (completed calls)
    sections_by_owner_noshow = {}    # owner -> list of HTML sections (no-shows)
    action_items_by_owner = {}       # owner -> list of action dicts (completed calls)
    action_items_by_owner_noshow = {}  # owner -> list of action dicts (no-shows)
    missing_transcripts_count = 0

    for i, entry in enumerate(due_leads):
        lead_info = entry["lead_info"]
        lead_name = lead_info.get("display_name", "Unknown")
        fu_number = entry["next_fu"]
        fu_done = entry["fu_done"]
        days_overdue = entry["days_overdue"]
        no_show = bool(entry.get("no_show"))
        first_call = entry["first_call_date"]
        days_since_call = (now - first_call).days

        owner = entry["owner_name"]
        cadence_type = entry.get("cadence_type", "active")
        cadence = _get_cadence(cadence_type)
        max_touches = len(cadence)

        _, fu_type, _ = cadence[fu_number]
        overdue_str = f" (OVERDUE {days_overdue}d)" if days_overdue > 0 else ""
        nurture_tag = " [NURTURE]" if cadence_type == "nurture" else (" [NO-SHOW]" if cadence_type == "no_show" else "")
        print(f"\n[{i+1}/{len(due_leads)}] {lead_name}{nurture_tag}")
        print(f"  Owner: {owner} | FU {fu_number}/{max_touches} ({fu_type}){overdue_str}")

        # Use the earliest meeting for transcript matching
        earliest_meeting = min(
            entry["meetings"],
            key=lambda m: m.get("starts_at", ""),
        )

        # Match to Granola: MCP first, then Google Sheet, then local cache
        call_notes = ""
        transcript_present = False
        if no_show:
            print("  Meeting result: No-show")
            call_notes = "NO-SHOW: Meeting was canceled or marked no-show."
            transcript_present = True

        # Check transcript DB first, then MCP live
        if not no_show:
            mid = entry.get("mcp_meeting_id")
            if mid:
                # DB cache check
                cached = _get_cached_transcript(transcript_db, mid)
                if cached is not None and cached["transcript_text"]:
                    transcript_present = True
                    call_notes = "CALL TRANSCRIPT:\n" + cached["transcript_text"][:TRANSCRIPT_CAP_SHEET]
                    print("  Transcript: cached DB")
                elif cached is None and mcp_client:
                    # Not in DB, fetch from MCP
                    transcript = mcp_transcripts.get(mid)
                    if transcript is None:
                        try:
                            transcript = mcp_get_transcript_text(mcp_client, mid)
                        except GranolaRateLimitError:
                            transcript = ""
                            mcp_transcripts[mid] = ""
                            print("  Transcript: rate limited (will retry next run)")
                            # Don't save to DB so it gets retried
                        except Exception:
                            transcript = ""
                            mcp_transcripts[mid] = ""
                        else:
                            mcp_transcripts[mid] = transcript
                            _save_transcript(transcript_db, mid,
                                             transcript_text=transcript, source="mcp")
                    if transcript:
                        transcript_present = True
                        call_notes = "CALL TRANSCRIPT:\n" + transcript[:TRANSCRIPT_CAP_SHEET]
                        print("  Transcript: Granola MCP (live)")

        if (not no_show) and (not transcript_present) and (not call_notes) and sheet_rows:
            sheet_match = match_granola_sheet(earliest_meeting, sheet_rows)
            if sheet_match:
                call_notes = extract_sheet_notes(sheet_match)
                transcript_present = bool((sheet_match.get("Transcript") or "").strip())
                if transcript_present:
                    print(f"  Transcript: Google Sheet match")
                elif call_notes:
                    print(f"  Notes: Google Sheet match")

        if (not no_show) and (not transcript_present) and (not call_notes) and granola_docs:
            local_match = match_granola(earliest_meeting, granola_docs)
            if local_match:
                call_notes = extract_granola_notes(local_match, granola_transcripts)
                transcript_present = _granola_local_has_transcript(local_match, granola_transcripts)
                if transcript_present:
                    print(f"  Transcript: Local cache match")
                elif call_notes:
                    print(f"  Notes: Local cache match")

        if (not no_show) and (not transcript_present):
            print(f"  Transcript: None (will use Close.com data only)")
            missing_transcripts_count += 1

        # Generate FU-specific draft with Claude (or use cached version)
        sent_emails = entry.get("sent_emails", [])
        lead_id = entry["lead_id"]
        cadence_obj = _get_cadence(cadence_type)
        _, _, fu_instr = cadence_obj[fu_number]
        prior_text = "\n".join(f"{e['subject']}\n{e['body']}" for e in sent_emails) if sent_emails else ""
        input_hash = _draft_input_hash(call_notes, prior_text, fu_instr)

        if SKIP_CLAUDE:
            print("  SKIP_CLAUDE=1 set; skipping Claude generation")
            claude_output = "(Skipped Claude generation; set SKIP_CLAUDE=0 to enable.)"
        elif not args.fresh:
            cached = _get_cached_draft(draft_db, lead_id, fu_number, cadence_type, input_hash)
            if cached:
                print(f"  Using cached draft (FU #{fu_number})")
                # Reconstruct the raw output format from cached fields
                parts = [f"FOLLOW-UP DRAFT:\n{cached['draft_text']}"]
                if cached.get("reasoning"):
                    parts.append(f"VALUE TIP REASONING:\n{cached['reasoning']}")
                if cached.get("priority"):
                    parts.append(f"PRIORITY: {cached['priority']}")
                claude_output = "\n\n".join(parts)
            else:
                print(f"  Generating FU #{fu_number} draft ({len(sent_emails)} prior emails for context)...")
                try:
                    claude_output = generate_digest_for_call(
                        lead_info, call_notes, earliest_meeting,
                        owner_name=owner,
                        fu_number=fu_number, sent_emails=sent_emails,
                        cadence_type=cadence_type,
                        no_show=no_show,
                        sales_scripts=cached_sales_scripts,
                        followup_examples=cached_followup_examples,
                    )
                    _save_draft(draft_db, lead_id, fu_number, cadence_type, claude_output, input_hash)
                except Exception as e:
                    print(f"  Error from Claude: {e}")
                    claude_output = "(Error generating follow-up. Review this lead manually.)"
        else:
            print(f"  Generating FU #{fu_number} draft --fresh ({len(sent_emails)} prior emails for context)...")
            try:
                claude_output = generate_digest_for_call(
                    lead_info, call_notes, earliest_meeting,
                    owner_name=owner,
                    fu_number=fu_number, sent_emails=sent_emails,
                    cadence_type=cadence_type,
                    no_show=no_show,
                    sales_scripts=cached_sales_scripts,
                    followup_examples=cached_followup_examples,
                )
                _save_draft(draft_db, lead_id, fu_number, cadence_type, claude_output, input_hash)
            except Exception as e:
                print(f"  Error from Claude: {e}")
                claude_output = "(Error generating follow-up. Review this lead manually.)"

        # Build section
        transcript_label = (customer_leads.get(entry["lead_id"], {}) or {}).get("transcript_label", "")
        section = build_lead_section(
            lead_info, claude_output, transcript_present,
            owner_name=owner,
            transcript_label=transcript_label,
            fu_number=fu_number,
            fu_done=fu_done,
            days_since_call=days_since_call,
            days_overdue=days_overdue,
            sent_emails=sent_emails,
            cadence_type=cadence_type,
            no_show=no_show,
        )
        if cadence_type == "no_show":
            sections_by_owner_noshow.setdefault(owner, []).append(section)
        else:
            sections_by_owner.setdefault(owner, []).append(section)

        # Build compact action item for the top list (copy-friendly)
        parsed = _parse_ai_sections(claude_output)
        copy_subject = f"Rebook {fu_number}: {fu_type}" if cadence_type == "no_show" else f"Follow-up {fu_number}: {fu_type}"
        copy_body = (parsed.get("draft") or parsed.get("raw") or "").strip()
        action_dest = action_items_by_owner_noshow if cadence_type == "no_show" else action_items_by_owner
        action_dest.setdefault(owner, []).append(
            {
                "name": lead_name,
                "close_url": lead_info.get("html_url", ""),
                "fu_number": fu_number,
                "transcript_label": transcript_label or ("Yes" if transcript_present else "No"),
                "transcript_state": "yes" if transcript_present else "no",
                "copy_draft": copy_body,
                "copy_all": f"Subject: {copy_subject}\n\n{copy_body}".strip(),
                "no_show": no_show,
                "overdue": days_overdue > 0,
                "sent_emails": sent_emails,
                "call_notes": call_notes,
            }
        )

    # 6. Build digest HTML and save to file
    total_leads = (sum(len(s) for s in sections_by_owner.values())
                   + sum(len(s) for s in sections_by_owner_noshow.values()))
    html = build_digest_html(
        sections_by_owner,
        date_str,
        total_leads,
        tracker_html=tracker_html,
        action_items_by_owner=action_items_by_owner,
        sections_by_owner_noshow=sections_by_owner_noshow,
        action_items_by_owner_noshow=action_items_by_owner_noshow,
        run_meta={
            "last_run": last_run_str,
            "mcp_status": mcp_status,
            "missing_transcripts": missing_transcripts_count,
        },
    )

    output_path = SCRIPT_DIR / "digest_preview.html"
    output_path.write_text(html)
    print(f"\n{'=' * 60}")
    print(f"Digest saved to {output_path}")
    print(f"{total_leads} lead{'s' if total_leads != 1 else ''} due for follow-up")

    # Auto-open in browser (skip in CI)
    if not args.no_open:
        subprocess.run(["open", str(output_path)])

    # Send email reminders to each team member (merge completed + no-show)
    all_action_items = {}
    for o, items in action_items_by_owner.items():
        all_action_items.setdefault(o, []).extend(items)
    for o, items in action_items_by_owner_noshow.items():
        all_action_items.setdefault(o, []).extend(items)
    if not args.no_email and all_action_items:
        print("\nSending follow-up reminders...")
        _send_owner_reminders(all_action_items, date_str)

    # Close caches
    draft_db.close()
    transcript_db.close()


if __name__ == "__main__":
    main()
