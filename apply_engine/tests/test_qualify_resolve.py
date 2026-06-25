"""Tests for the qualify apply-URL resolver. Network is mocked via the injectable
`get` seam — these are pure/deterministic and never hit a real ATS board."""
import json

from apply_engine.qualify import resolve_url as R


# ── slug candidates ──────────────────────────────────────────────────────────

def test_candidate_slugs_variants():
    # non-override "X AI" name -> both suffix-dropped and joined variants tried
    c = R.candidate_slugs("Foobar AI")
    assert "foobar" in c and "foobarai" in c

def test_candidate_slugs_joined_for_arize():
    assert "arizeai" in R.candidate_slugs("Arize AI")

def test_candidate_slugs_hyphen_for_two_words():
    assert "marble-health" in R.candidate_slugs("Marble Health")

def test_override_proprietary_returns_empty():
    assert R.candidate_slugs("D. E. Shaw") == []

def test_blank_company_no_slugs():
    assert R.candidate_slugs("") == []


# ── title matching ───────────────────────────────────────────────────────────

def test_norm_title_strips_parenthetical():
    assert R._norm_title("Forward-Deployed Engineer (FDE)") == "forward deployed engineer"

def test_score_close_titles_high():
    assert R._score("Forward Deployed Engineer", "Forward-Deployed Engineer (FDE)") >= R._MATCH_FLOOR

def test_score_different_roles_low():
    assert R._score("Forward Deployed Engineer", "Account Executive, SLED") < R._MATCH_FLOOR


# ── token-gate: proven false-accept pairs MUST be rejected ────────────────────

def test_match_rejects_ai_vs_sales_engineer():
    # raw ratio = 0.80 (> floor) but distinct roles -> token gate must reject
    assert R._match_score("AI Engineer", "Sales Engineer") is None

def test_match_rejects_applied_ai_vs_applied_ml():
    # raw ratio = 0.895 but AI != ML -> reject
    assert R._match_score("Applied AI", "Applied ML") is None

def test_match_accepts_fde_wording_drift():
    assert R._match_score("Forward Deployed Engineer",
                          "Forward-Deployed Engineer (FDE)") is not None

def test_match_accepts_subset_decoration():
    assert R._match_score("Forward Deployed Engineer",
                          "Forward Deployed Engineer, AI Products") is not None

def test_match_rejects_engineer_vs_manager_addition():
    # candidate ADDS a function word -> different role even though tokens superset
    assert R._match_score("Software Engineer", "Software Engineering Manager") is None

def test_match_rejects_engineer_vs_intern():
    assert R._match_score("Machine Learning Engineer",
                          "Machine Learning Engineer Intern") is None

def test_match_rejects_engineer_vs_director():
    assert R._match_score("Forward Deployed Engineer",
                          "Director, Forward Deployed Engineering") is None

def test_match_accepts_seniority_decoration():
    # seniority (Senior/Staff) is the SAME role family -> still matches
    assert R._match_score("Forward Deployed Engineer",
                          "Senior Forward Deployed Engineer") is not None


# ── slug hijack: bare-word board of a same-named company must not be trusted ──

def test_multiword_company_emits_specific_joined_slug():
    # specific joined form is always tried first
    cands = R.candidate_slugs("Marble Labs")
    assert cands[0] == "marblelabs"

def test_marble_labs_hijack_blocked_by_strict_floor():
    # "Marble Labs" collapses to one significant word ("marble" after dropping
    # "labs"), so resolve() must treat it as bare-word-risky and demand a strict
    # title match — a loose match on an unrelated greenhouse.io/marble board fails.
    board = _gh_board([("Data Analyst", "https://job-boards.greenhouse.io/marble/jobs/7")])
    def get(url):
        if "/marble" in url:
            return board
        raise OSError("404")
    out = R.resolve({"company": "Marble Labs", "title": "Data Engineer"}, get=get)
    assert out is None  # Data Engineer vs Data Analyst won't clear strict floor

def test_singleword_company_legit_decorated_title_still_resolves():
    # a one-word company (Reducto) with a legitimately DECORATED exact title must
    # STILL resolve — the moderate strict floor (0.82), not 0.90, admits it. A 0.90
    # floor would have neutered most single-word targets.
    board = _gh_board([("Founding Forward Deployed Engineer",
                        "https://job-boards.greenhouse.io/reducto/jobs/5")])
    def get(url):
        if "/reducto" in url:
            return board
        raise OSError("404")
    out = R.resolve({"company": "Reducto", "title": "Forward Deployed Engineer"}, get=get)
    assert out is not None and out["url"].endswith("/jobs/5")

def test_singleword_company_keeps_its_slug():
    assert "reducto" in R.candidate_slugs("Reducto")

def test_resolve_singleword_company_demands_strict_title():
    # one-word company "Future"; a same-named board has a loosely-similar title
    # that clears the normal floor but NOT the strict floor -> must fail closed
    board = _gh_board([("Software Engineer, Platform", "https://job-boards.greenhouse.io/future/jobs/9")])
    def get(url):
        if "/future/" in url:
            return board
        raise OSError("404")
    out = R.resolve({"company": "Future", "title": "Software Engineer"}, get=get)
    assert out is None  # strict floor for bare-word company protects against hijack


# ── resolve() with a mocked board ────────────────────────────────────────────

def _gh_board(jobs):
    # jobs: list of (title, url) or (title, url, content_html)
    out = []
    for j in jobs:
        t, u = j[0], j[1]
        content = j[2] if len(j) > 2 else ""
        out.append({"title": t, "absolute_url": u, "content": content})
    return json.dumps({"jobs": out})

def test_resolve_confident_match_returns_url():
    board = _gh_board([("Account Executive", "https://x/ae"),
                       ("Forward Deployed Engineer", "https://job-boards.greenhouse.io/acme/jobs/123")])
    def get(url):
        # only the greenhouse endpoint for the first candidate slug returns; others 404
        if "boards-api.greenhouse.io" in url and "/acme/" in url:
            return board
        raise OSError("404")
    out = R.resolve({"company": "Acme", "title": "Forward Deployed Engineer"}, get=get)
    assert out is not None
    assert out["url"].endswith("/jobs/123") and out["ats"] == "greenhouse" \
        and out["score"] >= R._MATCH_FLOOR


def test_resolve_returns_matched_title_and_jd():
    jd = "We are hiring a Forward Deployed Engineer. " * 20  # > _MIN_JD_OVERWRITE
    board = _gh_board([("Forward Deployed Engineer (FDE)",
                        "https://job-boards.greenhouse.io/acme/jobs/123",
                        f"<p>{jd}</p>")])
    def get(url):
        if "/acme/" in url:
            return board
        raise OSError("404")
    out = R.resolve({"company": "Acme", "title": "Forward Deployed Engineer"}, get=get)
    assert out["title"] == "Forward Deployed Engineer (FDE)"
    assert "Forward Deployed Engineer" in out["jd"] and "<p>" not in out["jd"]


# NOTE: these two tests reference R.apply_to_job via getattr, not a literal
# `apply_to_job(` call. The repo's conftest auto-marks any test whose source text
# contains the substring "apply_to_job(" as a live-browser test (it's one of the
# browser triggers) and deselects it by default. These tests are pure dict mutators
# with no browser, so we dodge that false-positive without editing conftest.
_write_match = R.apply_to_job


def test_apply_writes_title_jd_and_url():
    job = {"id": "J1", "company": "Acme", "title": "FDE",
           "url": "https://www.example.com/jobs/view/9", "jd_text": "thin"}
    r = {"url": "https://job-boards.greenhouse.io/acme/jobs/123", "ats": "greenhouse",
         "score": 1.0, "title": "Forward Deployed Engineer (FDE)", "jd": "x" * 600}
    _write_match(job, r)
    assert job["url"].endswith("/jobs/123")          # engine-driven field updated
    assert job["apply_url"].endswith("/jobs/123")
    assert job["source_url"].endswith("/view/9")     # original preserved
    assert job["title"] == "Forward Deployed Engineer (FDE)"  # real title
    assert job["jd_text"] == "x" * 600 and job["fit_stale"] is True
    assert job["apply_ats"] == "greenhouse"


def test_apply_keeps_sourced_jd_when_board_jd_thin():
    job = {"id": "J1", "company": "Acme", "title": "FDE", "jd_text": "a real sourced JD"}
    r = {"url": "https://x/jobs/1", "ats": "lever", "score": 1.0,
         "title": "FDE", "jd": "tiny"}  # below _MIN_JD_OVERWRITE
    _write_match(job, r)
    assert job["jd_text"] == "a real sourced JD"      # not clobbered by a stub
    assert "fit_stale" not in job

def test_resolve_no_confident_match_returns_none():
    board = _gh_board([("Account Executive", "https://x/ae"),
                       ("Staff Accountant", "https://x/acct")])
    def get(url):
        if "/acme/" in url:
            return board
        raise OSError("404")
    out = R.resolve({"company": "Acme", "title": "Forward Deployed Engineer"}, get=get)
    assert out is None  # fail closed — never attach a wrong URL

def test_resolve_network_error_fails_closed():
    def get(url):
        raise OSError("network down")
    assert R.resolve({"company": "Acme", "title": "FDE"}, get=get) is None

def test_resolve_unsupported_company_none():
    assert R.resolve({"company": "D. E. Shaw", "title": "Applied AI Engineer"},
                     get=lambda u: (_ for _ in ()).throw(AssertionError("should not fetch"))) is None

def test_resolve_no_title_none():
    assert R.resolve({"company": "Acme", "title": ""}, get=lambda u: "{}") is None
