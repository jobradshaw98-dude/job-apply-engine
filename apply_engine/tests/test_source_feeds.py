"""Tests for the source ATS-feed scanner. The network seam (`_http_json` / the `http=`
argument) is mocked with fixture board JSON — these run fully offline: no network,
no browser, no real data hub. The autouse `_isolate_manifest` fixture (conftest.py)
already redirects config.ARIA_DATA to a throwaway dir for every test."""
import json

from apply_engine import config
from apply_engine.source import feeds


# ── fixture boards, keyed by the URL each fetcher hits ────────────────────────
# Greenhouse board (content=false): a target-keyword role + an off-target role.
_GH_BOARD = {
    "jobs": [
        {"title": "Forward Deployed Engineer",
         "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/100",
         "location": {"name": "Remote - US"},
         "departments": [{"name": "Engineering"}]},
        {"title": "Office Manager",
         "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/101",
         "location": {"name": "NYC"},
         "departments": [{"name": "Operations"}]},
    ]
}
# Lever board is a bare LIST of postings.
_LEVER_BOARD = [
    {"text": "Applied AI Researcher",
     "hostedUrl": "https://jobs.lever.co/globex/abc-123",
     "categories": {"location": "SF", "team": "Research"}},
    {"text": "Staff Accountant",
     "hostedUrl": "https://jobs.lever.co/globex/def-456",
     "categories": {"location": "SF", "team": "Finance"}},
]


def _fake_http(url: str):
    """Stand-in for feeds._http_json: route by URL to the right fixture board."""
    if "boards-api.greenhouse.io/v1/boards/acme/" in url:
        return _GH_BOARD
    if "api.lever.co/v0/postings/globex" in url:
        return _LEVER_BOARD
    return {"_error": "HTTP 404", "_url": url}


_WATCHLIST = [
    {"company": "Acme", "ats": "greenhouse", "slug": "acme"},
    {"company": "Globex", "ats": "lever", "slug": "globex"},
]


# ── keyword filtering ────────────────────────────────────────────────────────

def test_scan_keeps_only_keyword_matches():
    cands, errs = feeds.scan(_WATCHLIST, existing_urls=set(), http=_fake_http)
    titles = {c["title"] for c in cands}
    # the two on-target roles are kept...
    assert "Forward Deployed Engineer" in titles
    assert "Applied AI Researcher" in titles
    # ...the off-target ones are filtered out
    assert "Office Manager" not in titles
    assert "Staff Accountant" not in titles
    assert errs == []

def test_scan_records_matched_keyword_and_ats():
    cands, _ = feeds.scan(_WATCHLIST, existing_urls=set(), http=_fake_http)
    fde = next(c for c in cands if c["title"] == "Forward Deployed Engineer")
    assert fde["ats"] == "greenhouse"
    assert fde["matched_keyword"]  # the pattern that matched is recorded
    assert fde["location"] == "Remote - US"


# ── dedupe against an existing jobs list ──────────────────────────────────────

def test_scan_dedupes_against_existing_urls():
    # the FDE posting is already known (normalized, no query/trailing slash) -> dropped
    existing = {"https://job-boards.greenhouse.io/acme/jobs/100"}
    cands, _ = feeds.scan(_WATCHLIST, existing_urls=existing, http=_fake_http)
    titles = {c["title"] for c in cands}
    assert "Forward Deployed Engineer" not in titles      # deduped
    assert "Applied AI Researcher" in titles               # still new

def test_load_existing_urls_normalizes(tmp_path):
    jobs = [{"url": "https://job-boards.greenhouse.io/acme/jobs/100?utm=x"},
            {"url": "https://jobs.lever.co/globex/abc-123/"},
            {"no_url": "skip me"}]
    jp = tmp_path / "jobs.json"
    jp.write_text(json.dumps(jobs), encoding="utf-8")
    out = feeds.load_existing_urls(jp)
    # query string and trailing slash are stripped for stable comparison
    assert "https://job-boards.greenhouse.io/acme/jobs/100" in out
    assert "https://jobs.lever.co/globex/abc-123" in out
    assert len(out) == 2


# ── fail-closed on network / bad slug ─────────────────────────────────────────

def test_scan_network_error_becomes_fetch_error_not_crash():
    def boom(url):
        return {"_error": "URLError: connection refused", "_url": url}
    cands, errs = feeds.scan(_WATCHLIST, existing_urls=set(), http=boom)
    assert cands == []                       # nothing surfaced...
    assert len(errs) == 2                     # ...both companies reported as errors
    assert all("_error" not in e for e in errs)  # error reason normalized into the dict
    assert {e["company"] for e in errs} == {"Acme", "Globex"}

def test_scan_unknown_ats_reported_not_fetched():
    wl = [{"company": "Initech", "ats": "taleo", "slug": "initech"}]
    cands, errs = feeds.scan(wl, existing_urls=set(),
                             http=lambda u: (_ for _ in ()).throw(AssertionError("must not fetch")))
    assert cands == []
    assert errs and "unknown ats" in errs[0]["reason"]

def test_scan_empty_board_reported_as_likely_wrong_slug():
    def empty(url):
        return {"jobs": []} if "greenhouse" in url else [] if "lever" in url else {"_error": "x"}
    cands, errs = feeds.scan(_WATCHLIST, existing_urls=set(), http=empty)
    assert cands == []
    assert all("empty" in e["reason"] for e in errs)


# ── run_scan: writes a review queue, NEVER writes jobs.json ───────────────────

def test_run_scan_writes_queue_but_not_jobs_json():
    # config.ARIA_DATA is the per-test sandbox (autouse conftest fixture).
    jobs_path = config.ARIA_DATA / "jobs.json"
    assert not jobs_path.exists()
    result = feeds.run_scan(watchlist=_WATCHLIST, http=_fake_http, write_queue=True)
    # a timestamped review queue was written to the data dir...
    qp = result["queue_path"]
    assert qp is not None and qp.exists()
    assert qp.parent == config.ARIA_DATA
    queue = json.loads(qp.read_text(encoding="utf-8"))
    assert {c["title"] for c in queue["candidates"]} == {
        "Forward Deployed Engineer", "Applied AI Researcher"}
    # ...and jobs.json was NEVER written — scanning is read-only w.r.t. the pipeline state
    assert not jobs_path.exists()

def test_run_scan_no_queue_when_disabled():
    result = feeds.run_scan(watchlist=_WATCHLIST, http=_fake_http, write_queue=False)
    assert result["queue_path"] is None
    # no stray files written to the data dir
    assert list(config.ARIA_DATA.glob("ats_scan_*.json")) == []
    assert not (config.ARIA_DATA / "jobs.json").exists()


# ── render_table ──────────────────────────────────────────────────────────────

def test_render_table_empty_is_friendly():
    assert "No new candidates" in feeds.render_table([])

def test_render_table_lists_candidates():
    cands, _ = feeds.scan(_WATCHLIST, existing_urls=set(), http=_fake_http)
    table = feeds.render_table(cands)
    assert "Forward Deployed Engineer" in table and "Applied AI Researcher" in table
    assert table.startswith("| Company | Title | Location | URL |")
