"""
jd_fetch.py — shared, stdlib-only job-description fetcher + placeholder detector.

Two jobs:
  fetch_jd(url) -> str | None
      For the three deterministic ATSs (Ashby / Greenhouse / Lever), pull the
      REAL job description from their public posting APIs. Returns the cleaned
      plain-text JD (capped at 8000 chars), or None for any non-ATS URL, any
      failure, a 404, or an empty body. NEVER raises.

  looks_like_placeholder(text) -> bool
      True when `text` is an obvious JS-error stub, access-denied / bot wall,
      or otherwise garbage that should never be stored as a job description.

Why this module exists: the HTML scraper used to store Ashby's "You need to
enable JavaScript to run this app" stub (Ashby renders client-side) as the JD,
and a cp1252 decode default produced mojibake. Routing every ATS write through
the posting APIs here, plus refusing placeholder text at every write site,
makes that whole class of bad data impossible.

stdlib only (urllib), 20s timeouts, real User-Agent, returns None on failure.
"""
from __future__ import annotations

import html as _html
import json as _json
import re as _re
import urllib.error as _urlerror
import urllib.request as _urlrequest

_TIMEOUT = 20
_MAX_JD = 8000
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Substrings that mark a JS-error stub, bot wall, or access-denied page.
# Case-insensitive. Any hit => the text is NOT a real job description.
_PLACEHOLDER_MARKERS = (
    "enable javascript",
    "you need to enable",
    "please turn on javascript",
    "please enable javascript",
    "javascript is required",
    "javascript is disabled",
    "access denied",
    "just a moment",          # Cloudflare interstitial
    "checking your browser",  # Cloudflare interstitial
    "attention required",     # Cloudflare block
    "unusual traffic",
    "are you a robot",
    "verify you are human",
    "captcha",
    "this page requires javascript",
    # LinkedIn auth-wall stub (the scraper grabs the login chrome, not the JD).
    "sign in with apple",
    "sign in with a passkey",
    "join linkedin",
    "new to linkedin?",
)


def looks_like_placeholder(text: str | None) -> bool:
    """True when `text` is a JS-error stub / bot wall / access-denied / empty
    garbage that must never be stored as a job description."""
    if not text:
        return True
    t = text.strip()
    if not t:
        return True
    low = t.lower()
    for marker in _PLACEHOLDER_MARKERS:
        if marker in low:
            return True
    # Very short bodies are almost always nav chrome or a stub, not a real JD.
    if len(t) < 150:
        return True
    return False


def _http_get(url: str) -> str | None:
    """GET a URL, return decoded utf-8 body, or None on any failure. Never raises."""
    try:
        req = _urlrequest.Request(
            url,
            headers={
                "User-Agent": _UA,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with _urlrequest.urlopen(req, timeout=_TIMEOUT) as resp:
            code = resp.getcode()
            if code and code >= 400:
                return None
            return resp.read().decode("utf-8", errors="replace")
    except (_urlerror.HTTPError, _urlerror.URLError, TimeoutError, ValueError):
        return None
    except Exception:
        return None


def _strip_html(raw: str) -> str:
    """Strip tags + unescape entities + collapse whitespace into plain text."""
    raw = _re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=_re.DOTALL | _re.IGNORECASE)
    raw = _re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=_re.DOTALL | _re.IGNORECASE)
    raw = _re.sub(r"<br\s*/?>", "\n", raw, flags=_re.IGNORECASE)
    raw = _re.sub(r"</p>", "\n\n", raw, flags=_re.IGNORECASE)
    raw = _re.sub(r"</li>", "\n", raw, flags=_re.IGNORECASE)
    raw = _re.sub(r"<[^>]+>", " ", raw)
    raw = _html.unescape(raw)
    # Normalize whitespace: collapse runs of spaces, cap blank-line runs.
    raw = _re.sub(r"[ \t]{2,}", " ", raw)
    raw = _re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def _cap(text: str) -> str:
    return text[:_MAX_JD].strip()


# ── Ashby ───────────────────────────────────────────────────────────────────
# URL forms:
#   https://jobs.ashbyhq.com/<org>/<job-uuid>
#   https://jobs.ashbyhq.com/<org>/<job-uuid>/application
# API: https://api.ashbyhq.com/posting-api/job-board/<org>  -> {jobs:[{id, ...}]}
def _fetch_ashby(url: str) -> str | None:
    m = _re.search(r"ashbyhq\.com/([^/?#]+)/([0-9a-fA-F-]{16,})", url)
    if not m:
        return None
    org, job_id = m.group(1), m.group(2).lower()
    body = _http_get(f"https://api.ashbyhq.com/posting-api/job-board/{org}")
    if not body:
        return None
    try:
        data = _json.loads(body)
    except ValueError:
        return None
    for j in (data.get("jobs") or []) if isinstance(data, dict) else []:
        if str(j.get("id", "")).lower() == job_id:
            plain = (j.get("descriptionPlain") or "").strip()
            if plain:
                return _cap(plain)
            htmltext = (j.get("descriptionHtml") or "").strip()
            if htmltext:
                return _cap(_strip_html(htmltext))
            return None
    return None


# ── Greenhouse ──────────────────────────────────────────────────────────────
# URL forms:
#   https://boards.greenhouse.io/<org>/jobs/<id>
#   https://job-boards.greenhouse.io/<org>/jobs/<id>
#   https://<org>.greenhouse.io/...jobs/<id>
# API: https://boards-api.greenhouse.io/v1/boards/<org>/jobs/<id>
#      -> {content: "<HTML-escaped HTML>"}
def _fetch_greenhouse(url: str) -> str | None:
    m_org = _re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([^/?#]+)", url)
    m_id = _re.search(r"jobs/(\d+)", url) or _re.search(r"[?&]gh_jid=(\d+)", url)
    if not m_org or not m_id:
        return None
    org, job_id = m_org.group(1), m_id.group(1)
    body = _http_get(f"https://boards-api.greenhouse.io/v1/boards/{org}/jobs/{job_id}")
    if not body:
        return None
    try:
        data = _json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    content = data.get("content") or ""
    if not content:
        return None
    # `content` is HTML-escaped HTML: unescape once to real HTML, then strip.
    real_html = _html.unescape(content)
    text = _strip_html(real_html)
    return _cap(text) if text else None


# ── Lever ───────────────────────────────────────────────────────────────────
# URL forms:
#   https://jobs.lever.co/<org>/<uuid>
#   https://jobs.lever.co/<org>/<uuid>/apply
# API: https://api.lever.co/v0/postings/<org>/<id>
#      -> {descriptionPlain, lists:[{text, content}], ...}
def _fetch_lever(url: str) -> str | None:
    m = _re.search(r"lever\.co/([^/?#]+)/([0-9a-fA-F-]{16,})", url)
    if not m:
        return None
    org, job_id = m.group(1), m.group(2)
    body = _http_get(f"https://api.lever.co/v0/postings/{org}/{job_id}")
    if not body:
        return None
    try:
        data = _json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    parts: list[str] = []
    plain = (data.get("descriptionPlain") or "").strip()
    if plain:
        parts.append(plain)
    for sec in data.get("lists") or []:
        if not isinstance(sec, dict):
            continue
        heading = (sec.get("text") or "").strip()
        content = (sec.get("content") or "").strip()
        if heading:
            parts.append(heading)
        if content:
            parts.append(_strip_html(content))
    closing = (data.get("additionalPlain") or "").strip()
    if closing:
        parts.append(closing)
    text = "\n\n".join(p for p in parts if p).strip()
    return _cap(text) if text else None


def fetch_jd(url: str) -> str | None:
    """Fetch the real job description for one of the three deterministic ATSs.

    Returns cleaned plain text (<=8000 chars) on success, or None for any
    non-ATS URL, any fetch/parse failure, a 404, or an empty body. Never raises.
    """
    if not url or not isinstance(url, str) or not url.startswith("http"):
        return None
    low = url.lower()
    try:
        if "ashbyhq.com" in low:
            return _fetch_ashby(url)
        if "greenhouse.io" in low:
            return _fetch_greenhouse(url)
        if "lever.co" in low:
            return _fetch_lever(url)
    except Exception:
        return None
    return None
