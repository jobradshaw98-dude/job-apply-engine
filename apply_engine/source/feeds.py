"""
feeds.py — new-posting discovery via public ATS feeds.

Hits Greenhouse / Lever / Ashby job-board JSON APIs for each company in your
watchlist (`ARIA_DATA/ats_watchlist.json`), filters titles by configurable
keywords (the DEFAULT set targets AI/forward-deployed/applied-ML roles — edit
KEYWORD_PATTERNS for your own search), dedupes against jobs.json, and surfaces new
candidates as a markdown table + a JSON review queue.

Does NOT auto-write to jobs.json. Review the queue and merge selectively.

The watchlist is a JSON doc shaped either as a bare list of entries or
`{"entries": [...]}`, where each entry is `{"company", "ats", "slug"}` and `ats`
is one of greenhouse / lever / ashby. See examples/watchlist.example.json.

Network lives behind the injectable `_http_json` seam (passed as `http=` into the
fetchers / scan) so tests run fully offline.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .. import config

# Paths are resolved LIVE from config (via the helpers below), not bound at import
# time, so tests that monkeypatch config.ARIA_DATA — and any runtime ARIA_CORE_DATA
# override — are honored.
def _watchlist_path() -> Path:
    return config.ARIA_DATA / "ats_watchlist.json"

def _jobs_path() -> Path:
    return config.ARIA_DATA / "jobs.json"

def _queue_dir() -> Path:
    return config.ARIA_DATA

# Title-match keywords. Case-insensitive. Compound matches preferred to avoid false
# positives (plain "engineer" alone catches everything). This DEFAULT set targets
# AI / forward-deployed / applied-ML roles — replace it with your own search terms.
KEYWORD_PATTERNS = [
    r"forward[- ]deployed",
    r"applied ai",
    r"\bagentic\b",
    r"\bai agent\b",
    r"\bai engineer\b",
    r"ai solutions (engineer|architect)",
    r"\bllm engineer\b",
    r"founding (forward|engineer.{0,30}(ai|agent|llm))",
    r"customer[- ]facing.{0,30}engineer",
    r"prompt engineer",
    r"ml engineer.{0,40}applied",
    r"applied.{0,20}ml engineer",
    r"ai (automation|implementation|deployment) engineer",
    r"solutions? engineer.{0,30}\bai\b",
]
KEYWORD_RE = [re.compile(p, re.IGNORECASE) for p in KEYWORD_PATTERNS]

UA = "Mozilla/5.0 (Career ATS Scanner)"
TIMEOUT = 15


def _http_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}", "_url": url}
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}", "_url": url}


def title_match(title: str) -> Optional[str]:
    """Return the matched keyword pattern (or None)."""
    for pat in KEYWORD_RE:
        if pat.search(title or ""):
            return pat.pattern
    return None


def fetch_greenhouse(slug: str, http: Callable = _http_json) -> list:
    """Greenhouse boards API. Returns list of {title, url, location, dept}."""
    data = http(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false")
    if not isinstance(data, dict) or "_error" in (data or {}):
        return data or []
    out = []
    for j in data.get("jobs", []):
        out.append({
            "title": j.get("title", ""),
            "url": j.get("absolute_url", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "dept": ", ".join(d.get("name", "") for d in (j.get("departments") or [])),
        })
    return out


def fetch_lever(slug: str, http: Callable = _http_json) -> list:
    data = http(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        return data if isinstance(data, dict) else []
    out = []
    for j in data:
        out.append({
            "title": j.get("text", ""),
            "url": j.get("hostedUrl", ""),
            "location": (j.get("categories") or {}).get("location", ""),
            "dept": (j.get("categories") or {}).get("team", ""),
        })
    return out


def fetch_ashby(slug: str, http: Callable = _http_json) -> list:
    data = http(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not isinstance(data, dict) or "_error" in (data or {}):
        return data or []
    out = []
    for j in data.get("jobs", []):
        out.append({
            "title": j.get("title", ""),
            "url": j.get("jobUrl") or j.get("applicationUrl") or "",
            "location": j.get("locationName") or (j.get("address") or {}).get("postalAddress", {}).get("addressLocality", ""),
            "dept": j.get("department", "") or j.get("team", ""),
        })
    return out


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


def load_existing_urls(jobs_file: Optional[Path] = None) -> set:
    """Normalized set of apply URLs already in jobs.json (for dedupe). Missing or
    unreadable file -> empty set (nothing to dedupe against)."""
    if jobs_file is None:
        jobs_file = _jobs_path()
    try:
        existing = json.loads(jobs_file.read_text(encoding="utf-8"))
    except Exception:
        return set()
    out = set()
    for j in existing:
        if isinstance(j, dict) and j.get("url"):
            out.add(j["url"].split("?")[0].rstrip("/"))
    return out


def scan(watchlist: list, existing_urls: set, http: Callable = _http_json) -> tuple:
    """Returns (candidates, fetch_errors). Pure given `http` — no disk, no jobs.json
    write. Each candidate is a keyword-matched, not-already-known posting."""
    candidates = []
    errors = []
    for entry in watchlist:
        ats, slug, company = entry.get("ats"), entry.get("slug"), entry.get("company")
        fetcher = FETCHERS.get(ats)
        if not fetcher:
            errors.append({"company": company, "reason": f"unknown ats: {ats}"})
            continue
        res = fetcher(slug, http)
        if isinstance(res, dict) and "_error" in res:
            errors.append({"company": company, "ats": ats, "slug": slug, "reason": res["_error"]})
            continue
        if not res:
            errors.append({"company": company, "ats": ats, "slug": slug, "reason": "empty (slug may be wrong)"})
            continue
        for j in res:
            kw = title_match(j["title"])
            if not kw:
                continue
            url_norm = (j.get("url") or "").split("?")[0].rstrip("/")
            if url_norm and url_norm in existing_urls:
                continue
            candidates.append({
                "company": company, "title": j["title"], "location": j.get("location", ""),
                "dept": j.get("dept", ""), "url": j["url"], "matched_keyword": kw,
                "ats": ats,
            })
    return candidates, errors


def render_table(cands: list) -> str:
    if not cands:
        return "_No new candidates found._"
    rows = ["| Company | Title | Location | URL |", "|---|---|---|---|"]
    for c in sorted(cands, key=lambda x: (x["company"], x["title"])):
        title = c["title"][:60]
        loc = (c["location"] or "")[:35]
        rows.append(f"| {c['company']} | {title} | {loc} | {c['url']} |")
    return "\n".join(rows)


def load_watchlist(path: Optional[Path] = None) -> list:
    """Read the watchlist doc; accept a bare list or `{"entries": [...]}`."""
    if path is None:
        path = _watchlist_path()
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc.get("entries", []) if isinstance(doc, dict) else doc


def run_scan(watchlist: Optional[list] = None, http: Callable = _http_json,
             write_queue: bool = True) -> dict:
    """Orchestrate a full scan: load the watchlist (if not supplied), dedupe against
    jobs.json, and OPTIONALLY write a timestamped JSON review queue to ARIA_DATA.
    NEVER writes jobs.json. Returns {candidates, errors, queue_path}."""
    if watchlist is None:
        watchlist = load_watchlist()
    existing = load_existing_urls()
    cands, errs = scan(watchlist, existing, http)
    queue_path = None
    if write_queue:
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        queue_path = _queue_dir() / f"ats_scan_{ts}.json"
        queue_path.write_text(
            json.dumps({"candidates": cands, "errors": errs, "ts": ts}, indent=2),
            encoding="utf-8")
    return {"candidates": cands, "errors": errs, "queue_path": queue_path}
