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

import csv
import io
import json
import os
import ssl
import time
import base64
import hashlib
import secrets
import threading
import http.server
import socket
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

# Sender signatures by owner (used for draft sign-off).
OWNER_SIGNATURE = {
    "Jay": "Jay\nCo-founder | Lightwork Home Health",
    "Johnny": "Johnny\nCo-founder | Lightwork Home Health",
    "Dom": "Dom\nCo-founder | Lightwork Home Health",
    "Josh": "Josh\nLightwork Home Health",
    "Unassigned": "Jay\nCo-founder | Lightwork Home Health",
}


# 7-Touch Follow-Up Cadence
# Key = FU number, Value = (day_offset, type_label, claude_instructions)
CADENCE = {
    1: (1, "Post-call value",
        "Personalized tip from transcript. Reference specific things discussed on the call. "
        "If there's a relevant topic from the sales scripts (EMFs, air quality, mold, sleep, etc.), "
        "include one actionable tip with product links. Keep it short and natural. "
        "Do not assume an on-site visit is booked; keep next steps optional unless the transcript explicitly confirms a date."),
    2: (3, "Second value drop",
        "Different topic from FU1 OR share the example report with context. "
        "100% value, zero ask. Do NOT repeat the same tip from FU1. "
        "If sharing the report, explain what they'll find relevant based on their situation."),
    3: (6, "Social proof + value",
        "Share the Wilkinson write-up (https://www.lightworkhome.com/blog-posts/wilkinson) or a relevant testimonial. "
        "Include one more quick tip on a different topic. Frame the social proof naturally."),
    4: (10, "Educational content",
        "Share a relevant newsletter article or the science video "
        "(https://www.lightworkhome.com/blog-posts/the-science-behind-lightwork). "
        "Write the key takeaway into the email so they get value without clicking."),
    5: (16, "New angle + soft ask",
        "Share new content or a different angle on their situation. "
        "First soft mention of continuing the conversation, e.g. 'happy to chat more if helpful.' "
        "Still primarily value-driven."),
    6: (25, "Availability + value",
        "Final resource share. Mention specific upcoming availability in their city if known. "
        "Frame as 'we'll be in [city] on [dates]' if applicable."),
    7: (35, "Graceful close",
        "No pressure. 'We're here whenever you're ready.' One last useful resource. "
        "Make it clear this is the last follow-up but leave the door open warmly."),
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

# Opportunity statuses that put a lead into nurture instead of active cadence
NURTURE_OPP_STATUSES = {"lost"}

# Only process leads whose first call was within this many days
CADENCE_LOOKBACK_DAYS = 45

# Max leads per digest (prevents backlog flood on first run)
MAX_LEADS_PER_DIGEST = 8



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
    import re
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
MAX_SENTENCES_BY_FU = {1: 6, 2: 5, 3: 7, 4: 6, 5: 6, 6: 6, 7: 6}
MAX_WORDS_BY_FU = {1: 140, 2: 120, 3: 170, 4: 140, 5: 150, 6: 160, 7: 150}

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

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    if basic_auth:
        import base64
        cred = base64.b64encode(f"{basic_auth[0]}:{basic_auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {cred}")

    ctx = ssl.create_default_context()
    transient_codes = {408, 425, 429, 500, 502, 503, 504}
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else ""
            retryable = e.code in transient_codes
            if retryable and attempt < max_attempts:
                time.sleep(0.8 * attempt)
                continue
            print(f"HTTP {e.code} for {url}: {error_body[:300]}")
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionResetError, ssl.SSLError):
            if attempt < max_attempts:
                time.sleep(0.8 * attempt)
                continue
            raise


def _extract_urls(text: str) -> list:
    import re
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
    import re
    s = (text or "").strip()
    if not s:
        return 0
    # Approx: count ., ?, ! at end of clauses.
    parts = re.split(r"[.!?]+(?:\s+|$)", s)
    return len([p for p in parts if p.strip()])


def _word_count(text: str) -> int:
    import re
    return len(re.findall(r"\b\w+\b", text or ""))


def _lint_email_draft(draft: str, fu_number: int, days_since_call: int | None) -> list:
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
    import subprocess
    subprocess.run(["open", url])

    if not done.wait(timeout=180):
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
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
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


def get_meetings_in_range(since_date, until_date=None):
    """Pull completed meetings from Close.com within a date range.

    Args:
        since_date: datetime, start of range (inclusive)
        until_date: datetime, end of range (exclusive). Defaults to now.

    Returns list of meeting dicts whose starts_at falls in [since_date, until_date).
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

    # Filter to meetings that actually started within the range AND were completed.
    # Close returns other statuses here (e.g. canceled, declined-by-lead). Those
    # should never enter the follow-up cadence.
    completed = []
    for m in all_meetings:
        status = (m.get("status") or "").lower()
        if status != "completed":
            continue
        starts_at = m.get("starts_at", "")
        if not starts_at:
            continue
        try:
            start_dt = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if since_date <= start_dt < until_date:
            completed.append(m)

    return completed


def get_recent_customer_leads():
    """Get Customer Leads who had calls in the last CADENCE_LOOKBACK_DAYS days.

    Returns:
        dict: {lead_id: {"lead_info": dict, "first_call_date": datetime,
                         "meetings": [list], "owner_name": str}}
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=CADENCE_LOOKBACK_DAYS)

    print(f"Fetching meetings from last {CADENCE_LOOKBACK_DAYS} days...")
    meetings = get_meetings_in_range(since)
    print(f"  Found {len(meetings)} completed meetings")

    # Group by lead_id, track earliest meeting
    leads = {}  # lead_id -> {meetings, earliest_start, owner_id}
    for m in meetings:
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


def get_followup_history(lead_id, first_call_date):
    """Get distinct follow-up threads sent to a lead after first_call_date.

    A follow-up = a unique outgoing email thread (grouped by subject, ignoring
    Re:/Fwd: prefixes). Multiple replies in the same thread count as ONE
    follow-up, not multiple.

    Returns (thread_count, thread_summaries) where:
      - thread_count: number of distinct outgoing email threads
      - thread_summaries: list of dicts with subject/body for the latest
        email in each thread (for Claude context)
    """
    after_str = first_call_date.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    has_more = True
    skip = 0

    # Fetch follow-up emails after first call date, group by thread
    # threads dict: normalized_subject -> {subject, body} (keeps latest email)
    threads = {}
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
                if "assessment" in subject.lower():
                    continue
                # Normalize subject to group thread replies together
                norm = _normalize_subject(subject)
                if norm not in threads:
                    body = (e.get("body_text") or e.get("body_text_quoted") or "").strip()
                    if len(body) > 500:
                        body = body[:500] + "..."
                    threads[norm] = {"subject": subject, "body": body}
        has_more = data.get("has_more", False)
        skip += len(emails)

    thread_list = list(threads.values())
    return len(thread_list), thread_list


def _normalize_subject(subject):
    """Strip Re:/Fwd:/[INT] prefixes to get the root thread subject."""
    import re
    s = subject.strip()
    # Repeatedly strip leading Re:, Fwd:, RE:, FW:, [INT], .Re: etc.
    while True:
        prev = s
        s = re.sub(r'^(Re:\s*|Fwd:\s*|FW:\s*|RE:\s*|\[INT\]\s*|\.Re:\s*)', '', s, flags=re.IGNORECASE).strip()
        if s == prev:
            break
    return s.lower()


def get_leads_due_today(customer_leads):
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

        # Pick the right cadence
        cadence = NURTURE_CADENCE if cadence_type == "nurture" else CADENCE
        max_touches = len(cadence)
        label = "nurture" if cadence_type == "nurture" else "FU"

        fu_done, sent_emails = get_followup_history(lead_id, first_call)
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
            })
            status = "DUE TODAY" if days_overdue == 0 else f"OVERDUE by {days_overdue}d"
            tag = " [NURTURE]" if cadence_type == "nurture" else ""
            print(f"  {lead_name}: {label} {next_fu}/{max_touches} ({status}){tag}")
        else:
            tag = " [NURTURE]" if cadence_type == "nurture" else ""
            print(f"  {lead_name}: {label} {next_fu}/{max_touches} due in {-days_overdue}d{tag}")

    # Sort: due today first, then least overdue (warmest leads first)
    due_leads.sort(key=lambda x: x["days_overdue"])
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


def match_granola_sheet(meeting, sheet_rows):
    """Match a Close.com meeting to a Granola Sheet row.

    Match by attendee email overlap or title match.
    Returns the row dict if matched, None otherwise.
    """
    # Non-team attendee emails from Close meeting
    close_attendees = set()
    for a in meeting.get("attendees", []):
        email = (a.get("email") or "").lower()
        if email and email not in TEAM_EMAILS:
            close_attendees.add(email)

    meeting_title = (meeting.get("title") or "").lower().strip()

    best_match = None
    best_score = 0

    for row in sheet_rows:
        score = 0

        # Attendee email overlap
        row_attendees = set()
        for email in (row.get("Attendees") or "").split(","):
            email = email.strip().lower()
            if email:
                row_attendees.add(email)

        overlap = close_attendees & row_attendees
        if overlap:
            score += 10 * len(overlap)

        # Title match
        row_title = (row.get("Title") or "").lower().strip()
        if meeting_title and meeting_title == row_title:
            score += 8
        elif meeting_title and (meeting_title in row_title or row_title in meeting_title):
            score += 5

        if score > best_score:
            best_score = score
            best_match = row

    if best_score >= 5:
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
        if len(transcript) > 6000:
            transcript = transcript[:6000] + "\n[...transcript truncated]"
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
    """
    Match a Close.com meeting to a Granola document.

    Strategy:
    1. Match by attendee email overlap (non-team emails)
    2. Fallback: match by meeting title similarity
    """
    # Get non-team attendee emails from the Close meeting
    close_attendees = set()
    for a in meeting.get("attendees", []):
        email = (a.get("email") or "").lower()
        if email and email not in TEAM_EMAILS:
            close_attendees.add(email)

    meeting_title = (meeting.get("title") or "").lower().strip()
    meeting_date = meeting.get("date_created", "")[:10]  # YYYY-MM-DD

    best_match = None
    best_score = 0

    for doc_id, doc in granola_docs.items():
        score = 0

        # Check attendee email overlap via google_calendar_event
        gcal = doc.get("google_calendar_event") or {}
        gcal_attendees = set()
        for a in gcal.get("attendees", []):
            gcal_attendees.add((a.get("email") or "").lower())

        # Also check the people field
        people = doc.get("people") or {}
        if isinstance(people, dict):
            for p_list in people.values():
                if isinstance(p_list, list):
                    for p in p_list:
                        if isinstance(p, dict):
                            gcal_attendees.add((p.get("email") or "").lower())

        # Score: email overlap with non-team attendees
        overlap = close_attendees & gcal_attendees
        if overlap:
            score += 10 * len(overlap)

        # Score: title similarity
        doc_title = (doc.get("title") or "").lower().strip()
        gcal_title = (gcal.get("summary") or "").lower().strip()
        if meeting_title and (meeting_title in doc_title or meeting_title in gcal_title):
            score += 5
        elif doc_title and doc_title in meeting_title:
            score += 3

        # Score: date proximity
        doc_date = (doc.get("created_at") or "")[:10]
        if doc_date == meeting_date:
            score += 2

        if score > best_score:
            best_score = score
            best_match = doc

    if best_score >= 5:
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
                if len(transcript_text) > 4000:
                    transcript_text = transcript_text[:4000] + "\n[...transcript truncated]"
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
    import re
    # Split into <meeting ...> ... </meeting> blocks so we can extract participants.
    for m in re.finditer(r'<meeting id="([^"]+)" title="([^"]*)" date="([^"]*)">(.*?)</meeting>', text, flags=re.DOTALL):
        mid, title, date_str, inner = m.group(1), m.group(2), m.group(3), m.group(4)
        emails = set(e.lower() for e in re.findall(r"<([^>\\s]+@[^>\\s]+)>", inner))
        meetings.append({"id": mid, "title": title, "date_str": date_str, "emails": emails})
    return meetings


def mcp_match_meeting(close_meeting: dict, mcp_meetings: list) -> dict | None:
    """Match a Close meeting to a Granola MCP meeting list."""
    close_attendees = set()
    for a in close_meeting.get("attendees", []):
        email = (a.get("email") or "").lower()
        if email and email not in TEAM_EMAILS:
            close_attendees.add(email)

    meeting_title = (close_meeting.get("title") or "").lower().strip()
    # Compare on date only with +/-1 day tolerance (timezone differences).
    starts_at = close_meeting.get("starts_at", "")
    close_date = None
    if starts_at:
        try:
            close_date = datetime.fromisoformat(starts_at.replace("Z", "+00:00")).date()
        except (ValueError, TypeError):
            close_date = None

    best = None
    best_score = 0
    for gm in mcp_meetings:
        score = 0
        overlap = close_attendees & (gm.get("emails") or set())
        if overlap:
            score += 10 * len(overlap)

        gtitle = (gm.get("title") or "").lower().strip()
        if meeting_title and gtitle:
            if meeting_title == gtitle:
                score += 8
            elif meeting_title in gtitle or gtitle in meeting_title:
                score += 5

        if close_date and gm.get("date_str"):
            try:
                gdt = datetime.strptime(gm["date_str"], "%b %d, %Y %I:%M %p").date()
                if abs((gdt - close_date).days) <= 1:
                    score += 2
            except Exception:
                pass

        if score > best_score:
            best_score = score
            best = gm

    if best_score >= 5:
        return best
    return None


def mcp_get_transcript_text(client: "GranolaMCPClient", meeting_id: str) -> str:
    res = client.tools_call("get_meeting_transcript", {"meeting_id": meeting_id})
    text = _mcp_text_content(res).strip()
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
    import re
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
# Claude API
# ---------------------------------------------------------------------------

SALES_SCRIPTS_PATH = SCRIPT_DIR / "reference" / "sales-scripts.md"
FOLLOWUP_EXAMPLES_PATH = SCRIPT_DIR / "reference" / "follow-up-examples.md"


def load_reference_file(path):
    """Load a reference file, return empty string if missing."""
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return ""


def generate_digest_for_call(lead_info, call_notes, meeting, owner_name="Jay",
                             fu_number=1, sent_emails=None, cadence_type="active"):
    """Send lead context + call notes to Claude, get summary + follow-up draft.

    Args:
        lead_info: Lead details from Close.com
        call_notes: Granola transcript/notes text
        meeting: Meeting dict from Close.com (used for context)
        fu_number: Which follow-up number (1-7) to generate
        sent_emails: List of dicts with subject/body of prior emails sent
        cadence_type: "active" for standard 7-touch, "nurture" for long-term
    """
    custom = lead_info.get("custom", {})
    contacts = lead_info.get("contacts", [])
    addresses = lead_info.get("addresses", [])

    lead_name = lead_info.get("display_name", "Unknown")
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

    sales_scripts = load_reference_file(SALES_SCRIPTS_PATH)
    followup_examples = load_reference_file(FOLLOWUP_EXAMPLES_PATH)

    # Get cadence details for this FU number
    cadence = NURTURE_CADENCE if cadence_type == "nurture" else CADENCE
    max_touches = len(cadence)
    day_offset, fu_type, fu_instructions = cadence[fu_number]

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

    cadence_label = "LONG-TERM NURTURE" if cadence_type == "nurture" else "ACTIVE"

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

    prompt = f"""You are writing follow-up #{fu_number} of {max_touches} for {sender} at Lightwork Home Health, an environmental health consulting company.

CADENCE TYPE: {cadence_label}
{"This lead previously said they are not interested right now. This is a low-pressure, value-only nurture email. NO asks, NO scheduling mentions, NO pressure. Just share something genuinely useful." if cadence_type == "nurture" else ""}

THIS IS FOLLOW-UP #{fu_number} ({fu_type})
Day {day_offset} after the initial call.

EMAIL STRUCTURE (default):
1) 1 line referencing something specific from the call
2) 1 useful resource or actionable tip (ONLY if it matches what they discussed)
3) Soft, optional next step (questions, or confirm they want to move forward)
4) Short sign-off using this exact signature:
{sender_signature}

SPECIFIC INSTRUCTIONS FOR THIS FOLLOW-UP:
{fu_instructions}

FULL {max_touches}-TOUCH CADENCE (for context on where this fits):
{cadence_overview}

EMAILS ALREADY SENT TO THIS LEAD (do NOT repeat any tips, resources, or talking points from these):
{prior_emails_text}

LEAD CONTEXT:
- Name: {lead_name}
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

SALES SCRIPTS (reference these ONLY if a specific topic from the transcript matches):
{sales_scripts[:3000] if sales_scripts else "(Not available)"}

FOLLOW-UP EMAIL EXAMPLES (match this voice exactly):
{followup_examples[:2000] if followup_examples else "(Not available)"}

KEY RESOURCES YOU CAN REFERENCE:
- Example report: https://www.lightworkhome.com/examplereport (password: homehealth)
- Wilkinson write-up: https://www.lightworkhome.com/blog-posts/wilkinson
- Science video: https://www.lightworkhome.com/blog-posts/the-science-behind-lightwork

IMPORTANT RULES:
- Never say "home health assessment" or "assessment." Just reference the service naturally.
- When mentioning the example report, always hyperlink it: <a href="https://www.lightworkhome.com/examplereport">example report</a> (password: homehealth)
- Only include a value-driven health tip if the transcript explicitly mentions a related topic (e.g., they talked about sleep, EMFs, air quality, mold, baby monitors, etc.). If there's no transcript or the transcript doesn't touch a topic from the sales scripts, do NOT force a tip. Just write a clean follow-up without one.
- Do NOT imply anything is scheduled or confirmed (no "looking forward to our visit", no specific dates) unless the transcript explicitly confirms it. Use optional next-step language instead (e.g., "If you'd like, we can...").
- Do NOT use "today" or "yesterday" unless the call was actually within the last 2 days.
- Do NOT use generic sales-email filler ("just checking in", "circling back", "touching base").
- LINKS: Only include links from this allowlist. Otherwise, do not include a link.
  Allowed prefixes: {", ".join(ALLOWED_URL_PREFIXES)}
- Never use em dashes.
- This is follow-up #{fu_number}. Do NOT repeat tips or resources from earlier follow-ups. Provide fresh value.

Generate exactly this output:

FOLLOW-UP DRAFT:
[Write a personalized follow-up email in {sender}'s voice matching the FU type instructions above. Short, casual, friendly, low pressure. Sign off using the exact signature above.]

VALUE TIP REASONING:
[If you included a health tip or resource, explain in 1 sentence why it's relevant to this lead. If none, write "No transcript match for a specific tip."]

PRIORITY: [HIGH / MEDIUM / LOW - based on budget, urgency, engagement level]"""

    def _call_model(p: str) -> str:
        if AI_PROVIDER == "openai":
            if not OPENAI_API_KEY:
                raise RuntimeError("OPENAI_API_KEY missing (required for AI_PROVIDER=openai)")

            body = {
                "model": OPENAI_MODEL,
                "input": p,
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
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": p}],
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

    raw = _call_model(prompt)
    # Post-check and rewrite loop (avoid link hallucinations + generic "AI slop").
    for attempt in range(2):
        parsed = _parse_ai_sections(raw)
        draft = parsed.get("draft") or parsed.get("raw") or ""
        issues = _lint_email_draft(draft, int(fu_number), days_since_call)
        if not issues:
            return raw

        fix_prompt = (
            prompt
            + "\n\nCOMPLIANCE FIXES REQUIRED:\n"
            + "\n".join(f"- {i}" for i in issues)
            + "\n\nRewrite the email to fix the issues. Keep it short, specific, and human."
        )
        raw = _call_model(fix_prompt)

    return raw

    if AI_PROVIDER == "openai":
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY missing (required for AI_PROVIDER=openai)")

        body = {
            "model": OPENAI_MODEL,
            "input": prompt,
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

    # Unreachable (kept to reduce diff churn if you need to revert pieces).
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

    owner_tables = ""
    for owner in sorted(by_owner.keys()):
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
            cadence = NURTURE_CADENCE if cadence_type == "nurture" else CADENCE
            transcript_label = entry.get("transcript_label", "No")

            # Nurture badge
            nurture_badge = ""
            if cadence_type == "nurture":
                nurture_badge = (
                    ' <span style="background:#8e44ad; color:white; font-size:9px; '
                    'padding:1px 4px; border-radius:2px; vertical-align:middle;">NURTURE</span>'
                )

            # Progress dots
            dots = ""
            for i in range(1, max_touches + 1):
                if i <= fu_done:
                    color = "#8e44ad" if cadence_type == "nurture" else "#2E5B88"
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

            rows_html += f"""
            <tr style="border-bottom:1px solid #eee;">
              <td style="padding:8px 10px; font-size:13px;">{name_link}{nurture_badge}</td>
              <td style="padding:8px 6px; font-size:12px; text-align:center; color:#666;">{transcript_label}</td>
              <td style="padding:8px 6px; font-size:13px; text-align:center;">{fu_done}/{max_touches}</td>
              <td style="padding:8px 6px;">{dots}</td>
              <td style="padding:8px 6px; font-size:12px; text-align:center;">{status}<br>{next_due}</td>
              <td style="padding:8px 6px; font-size:12px; text-align:center; color:#888;">{days_since}d</td>
            </tr>"""

        owner_tables += f"""
        <div style="margin-bottom:20px;">
          <h3 style="background:#f0f4f8; padding:8px 12px; border-radius:4px; margin:0 0 0 0;
                     font-size:14px; color:#2E5B88; border-left:3px solid #2E5B88;">
            {owner.upper()} ({len(leads)} lead{"s" if len(leads) != 1 else ""})
          </h3>
          <table style="width:100%; border-collapse:collapse;">
            <tr style="border-bottom:2px solid #ddd;">
              <th style="padding:6px 10px; text-align:left; font-size:11px; color:#888; text-transform:uppercase;">Lead</th>
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
                       days_overdue=0, sent_emails=None, cadence_type="active"):
    """Build one lead's section for the digest email."""
    import html as html_mod
    import json as json_mod

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

    cadence = NURTURE_CADENCE if cadence_type == "nurture" else CADENCE
    max_touches = len(cadence)

    location_str = f" ({city})" if city else ""
    transcript_badge = "" if granola_found else ' <span style="color:#c0392b; font-size:12px;">[No transcript]</span>'

    # Nurture badge
    nurture_badge = ""
    if cadence_type == "nurture":
        nurture_badge = (
            ' <span style="background:#8e44ad; color:white; font-size:11px; '
            'padding:2px 6px; border-radius:3px; margin-left:6px;">NURTURE</span>'
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

    bar_color = "#8e44ad" if cadence_type == "nurture" else "#2E5B88"
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

    accent_color = "#8e44ad" if cadence_type == "nurture" else "#2E5B88"
    border_color = "#8e44ad" if cadence_type == "nurture" else "#ddd"

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
    copy_subject = f"Follow-up {fu_number}: {fu_type}"
    copy_body = (parsed["draft"] or parsed["raw"] or "").strip()
    copy_all = f"Subject: {copy_subject}\n\n{copy_body}".strip()
    transcript_chip = ""
    if transcript_label:
        transcript_chip = f'<span style="color:#666; font-size:12px; margin-left:10px;">Transcript: {html_mod.escape(transcript_label)}</span>'

    return f"""
    <div data-owner="{html_mod.escape(owner_name)}" style="border:1px solid {border_color}; border-radius:8px; padding:16px; margin-bottom:20px; background:#fff;">
      <h3 style="margin:0 0 4px 0; color:#1a1a1a;">
        <a href="{close_url}" style="color:{accent_color}; text-decoration:none;">{name}</a>{location_str}{nurture_badge}{transcript_badge}{overdue_badge}
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
        <button class="lw-copy-btn" data-copy-text={html_mod.escape(json_mod.dumps(copy_body))} style="border:1px solid #ddd; background:#fff; border-radius:6px; padding:6px 10px; font-size:12px; cursor:pointer;">
          Copy draft
        </button>
        <button class="lw-copy-btn" data-copy-text={html_mod.escape(json_mod.dumps(copy_all))} style="border:1px solid #ddd; background:#fff; border-radius:6px; padding:6px 10px; font-size:12px; cursor:pointer;">
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
                      run_meta=None):
    """Build the full HTML digest email."""
    import html as html_mod
    import json as json_mod

    if action_items_by_owner is None:
        action_items_by_owner = {}
    if run_meta is None:
        run_meta = {}

    # Collapsible: helpful, but shouldn't clutter the daily workflow.
    how_it_works = f"""
    <details style="margin:0 0 28px 0; border:1px solid #ddd; border-radius:8px; padding:16px; background:#fff;">
      <summary style="cursor:pointer; font-weight:800; font-size:18px; color:#1a1a1a;">How It Works</summary>
      <p style="margin:10px 0 14px 0; color:#666; font-size:13px; line-height:1.5;">
        This digest keeps post-call follow-ups moving without guessing. It pulls the right leads, pulls transcripts, and generates value-driven drafts.
      </p>

      <!-- Table layout for email/client compatibility -->
      <table style="width:100%; border-collapse:separate; border-spacing:10px;">
        <tr>
          <td style="vertical-align:top; width:33.33%; border:1px solid #eee; border-radius:10px; padding:12px; background:#fbfbfb;">
            <div style="font-size:11px; letter-spacing:0.02em; text-transform:uppercase; color:#888; font-weight:800; margin-bottom:6px;">Stage 1</div>
            <div style="font-size:14px; font-weight:800; color:#1a1a1a; margin-bottom:6px;">Pick The Right Leads</div>
            <div style="font-size:13px; color:#444; line-height:1.5;">
              Pulls Customer Leads with completed calls in the last {CADENCE_LOOKBACK_DAYS} days, and excludes anyone already in a won stage (e.g. Booked Assessment).
            </div>
          </td>

          <td style="vertical-align:top; width:33.33%; border:1px solid #eee; border-radius:10px; padding:12px; background:#fbfbfb;">
            <div style="font-size:11px; letter-spacing:0.02em; text-transform:uppercase; color:#888; font-weight:800; margin-bottom:6px;">Stage 2</div>
            <div style="font-size:14px; font-weight:800; color:#1a1a1a; margin-bottom:6px;">Attach Transcript Context</div>
            <div style="font-size:13px; color:#444; line-height:1.5;">
              Matches each call to Granola (MCP) to pull the transcript. If MCP is unavailable, it falls back to the shared Sheet or local Granola cache.
            </div>
          </td>

          <td style="vertical-align:top; width:33.33%; border:1px solid #eee; border-radius:10px; padding:12px; background:#fbfbfb;">
            <div style="font-size:11px; letter-spacing:0.02em; text-transform:uppercase; color:#888; font-weight:800; margin-bottom:6px;">Stage 3</div>
            <div style="font-size:14px; font-weight:800; color:#1a1a1a; margin-bottom:6px;">Draft The Next Touch</div>
            <div style="font-size:13px; color:#444; line-height:1.5;">
              Generates the next follow-up in Jay's voice with value tied to what they discussed. The right-hand column shows the reasoning for the tip/resource.
            </div>
          </td>
        </tr>
      </table>
    </details>
    """

    owners = sorted(set(list(action_items_by_owner.keys()) + list(sections_by_owner.keys())))

    # Compact action list at the top
    action_blocks = ""
    for owner in owners:
        items = action_items_by_owner.get(owner, [])
        if not items:
            continue
        rows = ""
        for it in items:
            rows += f"""
            <tr style="border-bottom:1px solid #eee;">
              <td style="padding:8px 10px; font-size:13px;">
                <a href="{it.get('close_url','')}" style="color:#2E5B88; text-decoration:none;">{html_mod.escape(it.get('name',''))}</a>
                <span style="color:#999; margin-left:6px; font-size:12px;">FU {it.get('fu_number')}</span>
              </td>
              <td style="padding:8px 6px; font-size:12px; text-align:center; color:#666;">{html_mod.escape(it.get('transcript_label',''))}</td>
              <td style="padding:8px 6px; text-align:right; white-space:nowrap;">
                <button class="lw-copy-btn" data-copy-text={html_mod.escape(json_mod.dumps(it.get('copy_draft','')))} style="border:1px solid #ddd; background:#fff; border-radius:6px; padding:6px 10px; font-size:12px; cursor:pointer;">
                  Copy
                </button>
                <button class="lw-copy-btn" data-copy-text={html_mod.escape(json_mod.dumps(it.get('copy_all','')))} style="border:1px solid #ddd; background:#fff; border-radius:6px; padding:6px 10px; font-size:12px; cursor:pointer; margin-left:6px;">
                  Copy + Subject
                </button>
              </td>
            </tr>
            """

        action_blocks += f"""
        <div data-owner="{html_mod.escape(owner)}" style="margin-top:14px;">
          <div style="font-size:12px; color:#888; font-weight:800; letter-spacing:0.02em; text-transform:uppercase; margin:0 0 6px 0;">
            {html_mod.escape(owner)}: Action List
          </div>
          <table style="width:100%; border-collapse:collapse; background:#fff; border:1px solid #eee; border-radius:8px; overflow:hidden;">
            <tr style="border-bottom:2px solid #ddd;">
              <th style="padding:6px 10px; text-align:left; font-size:11px; color:#888; text-transform:uppercase;">Lead</th>
              <th style="padding:6px 6px; text-align:center; font-size:11px; color:#888; text-transform:uppercase;">Transcript</th>
              <th style="padding:6px 6px; text-align:right; font-size:11px; color:#888; text-transform:uppercase;">Copy</th>
            </tr>
            {rows}
          </table>
        </div>
        """

    owner_blocks = ""
    for owner, sections in sections_by_owner.items():
        count = len(sections)
        owner_blocks += f"""
    <div data-owner="{html_mod.escape(owner)}" style="margin-top:28px;">
      <h2 style="background:#2E5B88; color:white; padding:10px 16px; border-radius:6px; margin:0 0 16px 0; font-size:16px;">
        {owner.upper()}'S FOLLOW-UPS ({count})
      </h2>
      {"".join(sections)}
    </div>"""

    # Owner tabs
    tab_buttons = '<button class="lw-tab active" data-owner="ALL">All</button>'
    for o in owners:
        tab_buttons += f'<button class="lw-tab" data-owner="{html_mod.escape(o)}">{html_mod.escape(o)}</button>'

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
	<head><meta charset="utf-8"></head>
	<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; max-width:700px; margin:0 auto; padding:20px; background:#FFFCF0; color:#1a1a1a;">
	  <div style="position:sticky; top:0; background:#FFFCF0; padding:10px 0 12px 0; z-index:10; border-bottom:1px solid #e9e2c9; margin-bottom:14px;">
	    <div style="display:flex; align-items:flex-end; justify-content:space-between; gap:12px;">
	      <div>
	        <div style="font-size:20px; font-weight:800; color:#1a1a1a;">Lightwork Follow-Up Digest</div>
	        <div style="margin-top:2px; color:#666; font-size:13px;">
	          {date_str} &middot; {total_leads} lead{"s" if total_leads != 1 else ""} due
	          {f"&middot; Last run: {html_mod.escape(last_run)}" if last_run else ""}
	        </div>
	      </div>
	      <div class="lw-tabs" style="display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end;">
	        {tab_buttons}
	      </div>
	    </div>
	  </div>
	  {banner}
	  <div style="margin-bottom:18px;">
	    <h2 style="margin:0 0 6px 0; font-size:18px; color:#1a1a1a;">Today</h2>
	    <div style="color:#666; font-size:13px;">Copy and send the drafts below. Use tabs to focus on one owner.</div>
	    {action_blocks}
	  </div>
	  {tracker_html}
	  {how_it_works}
	  {owner_blocks}
	  <div style="text-align:center; padding:20px 0; margin-top:30px; border-top:1px solid #ddd; color:#999; font-size:12px;">
	    Auto-generated by Lightwork Follow-Up Tracker. Drafts are suggestions, tweak as needed.
	  </div>
	  <script>
	  (function() {{
	    function setActiveOwner(owner) {{
	      document.querySelectorAll('.lw-tab').forEach(function(btn) {{
	        btn.classList.toggle('active', btn.getAttribute('data-owner') === owner);
	      }});
	      document.querySelectorAll('[data-owner]').forEach(function(el) {{
	        var o = el.getAttribute('data-owner');
	        el.style.display = (owner === 'ALL' || o === owner) ? '' : 'none';
	      }});
	    }}

	    document.querySelectorAll('.lw-tab').forEach(function(btn) {{
	      btn.addEventListener('click', function() {{
	        setActiveOwner(btn.getAttribute('data-owner'));
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

	    var style = document.createElement('style');
	    style.textContent = `
	      .lw-tab {{
	        border:1px solid #ddd;
	        background:#fff;
	        border-radius:999px;
	        padding:6px 10px;
	        font-size:12px;
	        cursor:pointer;
	      }}
	      .lw-tab.active {{
	        border-color:#2E5B88;
	        background:#2E5B88;
	        color:#fff;
	      }}
	    `;
	    document.head.appendChild(style);

	    setActiveOwner('ALL');
	  }})();
	  </script>
	</body>
	</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%b %-d, %Y")
    last_run_str = now.astimezone().strftime("%Y-%m-%d %H:%M")

    print(f"Lightwork Follow-Up Digest - {date_str}")
    print("=" * 60)

    # 1. Get Customer Leads with calls in last 45 days
    customer_leads = get_recent_customer_leads()

    if not customer_leads:
        print("No Customer Leads with recent calls. Nothing to digest.")
        return

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
            since = now - timedelta(days=CADENCE_LOOKBACK_DAYS)
            mcp_meetings = mcp_list_meetings(mcp_client, since, now)
            print(f"  {len(mcp_meetings)} meetings from MCP (last {CADENCE_LOOKBACK_DAYS} days)")
        except Exception as e:
            print(f"  Warning: Granola MCP unavailable: {e}")
            mcp_client = None
            mcp_status = f"Granola MCP unavailable: {e}"
    else:
        mcp_status = ""

    print("\nLoading Granola transcripts (fallback sources)...")
    sheet_rows = load_granola_sheet()
    print(f"  {len(sheet_rows)} rows from Google Sheet")

    granola_docs, granola_transcripts = load_granola_cache()
    print(f"  {len(granola_docs)} docs from local Granola cache")

    # Annotate each lead with transcript status for tracker view
    for lead_id, info in customer_leads.items():
        earliest_meeting = min(info["meetings"], key=lambda m: m.get("starts_at", ""))

        # MCP first
        if mcp_client and mcp_meetings:
            m = mcp_match_meeting(earliest_meeting, mcp_meetings)
            if m:
                mid = m["id"]
                info["mcp_meeting_id"] = mid
                if mid not in mcp_transcripts:
                    try:
                        mcp_transcripts[mid] = mcp_get_transcript_text(mcp_client, mid)
                    except Exception:
                        mcp_transcripts[mid] = ""
                if mcp_transcripts[mid]:
                    info["transcript_label"] = "Yes (MCP)"
                    continue
                info["transcript_label"] = "No (MCP)"
                continue

        # Fallback: Sheet/local cache
        src, match_obj = get_granola_match(earliest_meeting, sheet_rows, granola_docs)
        info["transcript_label"] = get_transcript_label(src, match_obj, granola_transcripts)

    # 3. Determine which leads need follow-up today
    print(f"\nChecking follow-up status for {len(customer_leads)} leads...")
    due_leads, all_leads_status = get_leads_due_today(customer_leads)

    # Build tracker view (always, even if no leads due today)
    tracker_html = build_tracker_view(all_leads_status)

    if not due_leads:
        # Still output the tracker even with no drafts to generate
        html = build_digest_html({}, date_str, 0, tracker_html=tracker_html)
        output_path = SCRIPT_DIR / "digest_preview.html"
        output_path.write_text(html)
        print(f"\nNo follow-ups due today. Tracker saved to {output_path}")
        import subprocess
        subprocess.run(["open", str(output_path)])
        return

    total_due = len(due_leads)
    if total_due > MAX_LEADS_PER_DIGEST:
        print(f"\n{total_due} leads due, capping to {MAX_LEADS_PER_DIGEST} (warmest first)")
        due_leads = due_leads[:MAX_LEADS_PER_DIGEST]
    else:
        print(f"\n{total_due} leads due for follow-up today")

    # 4. Process each due lead
    sections_by_owner = {}
    action_items_by_owner = {}
    missing_transcripts_count = 0

    for i, entry in enumerate(due_leads):
        lead_info = entry["lead_info"]
        lead_name = lead_info.get("display_name", "Unknown")
        fu_number = entry["next_fu"]
        fu_done = entry["fu_done"]
        days_overdue = entry["days_overdue"]
        first_call = entry["first_call_date"]
        days_since_call = (now - first_call).days

        owner = entry["owner_name"]
        cadence_type = entry.get("cadence_type", "active")
        cadence = NURTURE_CADENCE if cadence_type == "nurture" else CADENCE
        max_touches = len(cadence)

        _, fu_type, _ = cadence[fu_number]
        overdue_str = f" (OVERDUE {days_overdue}d)" if days_overdue > 0 else ""
        nurture_tag = " [NURTURE]" if cadence_type == "nurture" else ""
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

        # MCP transcript
        if mcp_client:
            mid = entry.get("mcp_meeting_id")
            if mid:
                transcript = mcp_transcripts.get(mid)
                if transcript is None:
                    try:
                        transcript = mcp_get_transcript_text(mcp_client, mid)
                    except Exception:
                        transcript = ""
                    mcp_transcripts[mid] = transcript
                if transcript:
                    transcript_present = True
                    call_notes = "CALL TRANSCRIPT:\n" + transcript[:6000]
                    print("  Transcript: Granola MCP")

        if sheet_rows:
            sheet_match = match_granola_sheet(earliest_meeting, sheet_rows)
            if sheet_match:
                call_notes = extract_sheet_notes(sheet_match)
                transcript_present = bool((sheet_match.get("Transcript") or "").strip())
                if transcript_present:
                    print(f"  Transcript: Google Sheet match")
                elif call_notes:
                    print(f"  Notes: Google Sheet match")

        if not transcript_present and not call_notes and granola_docs:
            local_match = match_granola(earliest_meeting, granola_docs)
            if local_match:
                call_notes = extract_granola_notes(local_match, granola_transcripts)
                transcript_present = _granola_local_has_transcript(local_match, granola_transcripts)
                if transcript_present:
                    print(f"  Transcript: Local cache match")
                elif call_notes:
                    print(f"  Notes: Local cache match")

        if not transcript_present:
            print(f"  Transcript: None (will use Close.com data only)")
            missing_transcripts_count += 1

        # Generate FU-specific draft with Claude
        sent_emails = entry.get("sent_emails", [])
        if SKIP_CLAUDE:
            print("  SKIP_CLAUDE=1 set; skipping Claude generation")
            claude_output = "(Skipped Claude generation; set SKIP_CLAUDE=0 to enable.)"
        else:
            print(f"  Generating FU #{fu_number} draft ({len(sent_emails)} prior emails for context)...")
            try:
                claude_output = generate_digest_for_call(
                    lead_info, call_notes, earliest_meeting,
                    owner_name=owner,
                    fu_number=fu_number, sent_emails=sent_emails,
                    cadence_type=cadence_type,
                )
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
        )
        sections_by_owner.setdefault(owner, []).append(section)

        # Build compact action item for the top list (copy-friendly)
        parsed = _parse_ai_sections(claude_output)
        copy_subject = f"Follow-up {fu_number}: {fu_type}"
        copy_body = (parsed.get("draft") or parsed.get("raw") or "").strip()
        action_items_by_owner.setdefault(owner, []).append(
            {
                "name": lead_name,
                "close_url": lead_info.get("html_url", ""),
                "fu_number": fu_number,
                "transcript_label": transcript_label or ("Yes" if transcript_present else "No"),
                "copy_draft": copy_body,
                "copy_all": f"Subject: {copy_subject}\n\n{copy_body}".strip(),
            }
        )

    # 6. Build digest HTML and save to file
    total_leads = sum(len(s) for s in sections_by_owner.values())
    html = build_digest_html(
        sections_by_owner,
        date_str,
        total_leads,
        tracker_html=tracker_html,
        action_items_by_owner=action_items_by_owner,
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

    # Auto-open in browser
    import subprocess
    subprocess.run(["open", str(output_path)])


if __name__ == "__main__":
    main()
