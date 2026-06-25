"""
Stage-0 apply-URL resolver.

THE PROBLEM THIS SOLVES
Sourced job leads often arrive as un-driveable links: a login-walled job-board
URL or a company careers homepage that never exposes the real apply page
anonymously. The apply engine can only drive a DIRECT posting on a supported ATS
(Greenhouse / Lever / Ashby), so a lead without a direct URL is a dead end.

THE FIX
Every supported ATS publishes a FREE, no-auth JSON board of all open postings.
Given a job's company + title, we fetch that company's board and title-match to the
single best open posting, returning its exact direct apply URL. Deterministic, free,
and high-hit-rate because most target companies use GH / Lever / Ashby.

CONTRACT — FAIL CLOSED ON AMBIGUITY, NEVER FABRICATE A URL.
  * Return a URL only on a CONFIDENT title match against a real board posting.
  * A weak/ambiguous match returns None (the job stays quarantined, surfaced for
    manual resolution) — we never attach a guessed URL the engine would then
    drive blindly. A wrong apply URL is far worse than no URL.
  * Network error / unknown company slug / empty board -> None (fail closed).

This module is PURE except `_http_get` (the one network seam, injectable for tests).
"""
from __future__ import annotations

import json
import re
import urllib.request
from difflib import SequenceMatcher
from typing import Callable, Optional

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# A real ATS board API answers in <2s; a non-existent slug 404s immediately. 6s is
# generous headroom without letting one slow/hung endpoint stall a backfill — we
# probe several slug variants per company, so each probe must be cheap.
_TIMEOUT = 6.0

# Confidence floor for accepting a title match. Tuned: an exact role title vs the
# board's exact title usually scores >0.8; 0.72 admits minor wording drift
# ("Forward Deployed Engineer" vs "Forward-Deployed Engineer (FDE)") without
# admitting a different role.
_MATCH_FLOOR = 0.72


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return resp.read().decode("utf-8", "ignore")


# ── company -> board slug ────────────────────────────────────────────────────
# Known overrides where the slug differs from a naive normalization. Extend as new
# companies appear; the guesser below handles the common case.
_SLUG_OVERRIDES = {
    "mistral ai": "mistral",
    "arize ai": "arizeai",
    "lightning ai": "lightningai",
    "fireworks ai": "fireworksai",
    "scale ai": "scaleai",
    "llamaindex": "llamaindex",
    "d. e. shaw": None,          # proprietary apply system — not a supported ATS
    "d.e. shaw": None,
}

# Trailing corporate words to strip when guessing a slug.
_SUFFIXES = re.compile(r"\b(inc|llc|ltd|corp|co|company|labs?|technologies|technology|ai|"
                       r"the|group|holdings?)\b", re.IGNORECASE)


def company_slug(company: str) -> Optional[str]:
    """Best single-guess board slug. None => explicitly unsupported. (Kept for
    callers/tests that want the primary guess; resolve() uses candidate_slugs.)"""
    key = (company or "").strip().lower()
    if key in _SLUG_OVERRIDES:
        return _SLUG_OVERRIDES[key]
    base = _SUFFIXES.sub(" ", key)
    base = re.sub(r"[^a-z0-9]+", "", base)
    return base or None


def candidate_slugs(company: str) -> list:
    """Ordered, de-duped list of plausible board slugs to try. A company's real
    board slug varies ("Mistral AI"->mistral, "Arize AI"->arizeai, "Marble
    Health"->marble or marblehealth), so we probe a few cheap variants instead of
    one guess. An explicit None override (proprietary ATS) returns []."""
    key = (company or "").strip().lower()
    if key in _SLUG_OVERRIDES:
        ov = _SLUG_OVERRIDES[key]
        return [] if ov is None else [ov]
    if not key:
        return []
    words_all = re.sub(r"[^a-z0-9 ]+", " ", key).split()
    words_nosuf = _SUFFIXES.sub(" ", key)
    words_nosuf = re.sub(r"[^a-z0-9 ]+", " ", words_nosuf).split()
    cands = []
    def add(s):
        if s and s not in cands:
            cands.append(s)
    # MOST-specific first (full name joined) -> least. resolve() prefers a match
    # from an earlier (more specific) slug, so a same-named bare-word board can't
    # outrank the real full-name board.
    add("".join(words_all))            # mistralai (everything joined — arizeai, fireworksai)
    add("".join(words_nosuf))          # mistral  (suffixes dropped, joined)
    add("-".join(words_all))           # marble-health-inc
    add("-".join(words_nosuf))         # marble-health
    # A bare first word ("marble" from "Marble Labs") collides with unrelated
    # boards, so emit it ONLY when the company is genuinely a single word — then
    # it's the real slug, not a hijack vector. resolve() also demands a STRICT
    # title match for any single-token slug as a second guard.
    if len(words_nosuf) == 1:
        add(words_nosuf[0])
    elif len(words_all) == 1:
        add(words_all[0])
    return cands


def _norm_title(t: str) -> str:
    t = (t or "").lower()
    t = re.sub(r"[\(\[].*?[\)\]]", " ", t)          # drop parentheticals "(FDE)"
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _score(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm_title(a), _norm_title(b)).ratio()


# Generic role words that, alone, must NOT carry a match — "AI Engineer" vs "Sales
# Engineer" share "engineer" and score 0.80 on raw ratio, which would false-accept a
# different role. The token gate below requires agreement on the DISTINCTIVE words.
# Seniority words live here too (senior/staff/lead/principal): "Staff Engineer" and
# "Engineer" are the same role FAMILY — fine to match.
_GENERIC_TOKENS = {"engineer", "engineering", "senior", "staff", "lead", "principal",
                   "ii", "iii", "iv", "sr", "jr", "of", "and", "the"}

# FUNCTION-changing words: if one title has one of these and the other does NOT, they
# are DIFFERENT roles even when every other token agrees ("Software Engineer" vs
# "Software Engineering MANAGER"; "FDE" vs "FDE INTERN"). A subset/Jaccard gate alone
# misses this — the candidate just adds a token — so we reject on a one-sided
# disqualifier regardless of the ratio.
_DISQUALIFYING_TOKENS = {"manager", "director", "head", "vp", "president", "chief",
                         "intern", "internship", "apprentice", "contract",
                         "contractor", "fellow", "fellowship"}


def _match_score(source: str, cand: str) -> Optional[float]:
    """Confidence that `cand` (a board posting title) IS the same role as `source`
    (the sourced job title). Returns a score in [0,1] only when BOTH a token-set
    gate AND the sequence-ratio floor pass; else None (NOT a match).

    The token gate is what stops generic-word false-accepts: the source's
    DISTINCTIVE tokens (non-generic) must be a subset of the candidate's tokens, OR
    the two token sets must overlap heavily (Jaccard >= 0.6). Proven cases the raw
    ratio got wrong and this rejects: 'AI Engineer' vs 'Sales Engineer' (0.80),
    'Applied AI' vs 'Applied ML' (0.895)."""
    st = set(_norm_title(source).split())
    ct = set(_norm_title(cand).split())
    if not st or not ct:
        return None
    # A one-sided function word (Manager/Director/Intern/...) means different roles.
    if (st ^ ct) & _DISQUALIFYING_TOKENS:
        return None
    distinctive = st - _GENERIC_TOKENS
    subset = distinctive <= ct if distinctive else (st <= ct)
    union = st | ct
    jaccard = len(st & ct) / len(union) if union else 0.0
    if not (subset or jaccard >= 0.6):
        return None
    ratio = SequenceMatcher(None, _norm_title(source), _norm_title(cand)).ratio()
    return ratio if ratio >= _MATCH_FLOOR else None


# ── per-ATS board fetch -> list[(title, url, jd_text)] ───────────────────────
# Each board returns the full job description in the SAME list call (Greenhouse needs
# ?content=true), so we capture it for free — the matched posting's real title + JD
# overwrite the thin sourced values so the record reflects what we actually apply to
# (and fit re-scores off the accurate JD on the next sourcing pass).

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]*\n[ \t\n]*")


def _strip_html(s: str) -> str:
    """Greenhouse JD `content` is HTML-escaped HTML. Unescape -> drop tags -> tidy."""
    import html
    s = html.unescape(s or "")
    s = _TAG_RE.sub(" ", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return _WS_RE.sub("\n", s).strip()


def _greenhouse(slug: str, get: Callable[[str], str]):
    d = json.loads(get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"))
    # .get() so one malformed posting can't KeyError and discard the WHOLE board.
    return [(j.get("title", ""), j.get("absolute_url", ""), _strip_html(j.get("content", "")))
            for j in d.get("jobs", []) if j.get("absolute_url")]


def _lever(slug: str, get: Callable[[str], str]):
    d = json.loads(get(f"https://api.lever.co/v0/postings/{slug}?mode=json"))
    return [(j.get("text", ""), j.get("hostedUrl", ""), (j.get("descriptionPlain") or "").strip())
            for j in d if j.get("hostedUrl")]


def _ashby(slug: str, get: Callable[[str], str]):
    d = json.loads(get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}"))
    out = []
    for j in d.get("jobs", []):
        url = j.get("jobUrl") or j.get("applyUrl") or ""
        if url:
            jd = (j.get("descriptionPlain") or j.get("description") or "").strip()
            out.append((j.get("title", ""), url, jd))
    return out


_ATS = [("greenhouse", _greenhouse), ("lever", _lever), ("ashby", _ashby)]


def resolve(job: dict, get: Callable[[str], str] = _http_get) -> Optional[dict]:
    """Return a dict for the best confident match, else None (fail closed):
        {url, ats, score, title, jd}
    `title` and `jd` are the MATCHED board posting's real title + full description —
    callers overwrite the thin sourced values with these so the record matches what
    we actually apply to.

    Tries each supported ATS board for the company's slug; picks the highest-scoring
    title match across all boards that clears the floor. None on no slug / no board /
    no confident match."""
    slugs = candidate_slugs(job.get("company", ""))
    if not slugs:
        return None
    title = job.get("title", "")
    if not title:
        return None
    # A SINGLE-WORD company (Future, Marble, Scale) yields a bare dictionary-word
    # slug that collides with unrelated same-named boards. For those we demand a
    # STRICT (near-exact) title match; a miss simply falls through to a manual
    # resolution path (which verifies company identity by reading the page).
    # Multi-word companies produce joined/hyphenated slugs (mistralai, marble-health)
    # that are specific enough for the normal floor.
    company_words = re.sub(r"[^a-z0-9 ]+", " ", (job.get("company") or "").lower())
    company_words = _SUFFIXES.sub(" ", company_words).split()
    strict = len(company_words) <= 1
    # MODERATE strict floor (not 0.90): the token gate already rejects wrong ROLES,
    # so the only residual single-word-company risk is a same-named different-company
    # board carrying a LOOSELY similar title. 0.82 blocks those ("Data Engineer" vs
    # "Data Analyst" ~0.73) while still admitting legit decoration ("Forward Deployed
    # Engineer" vs "Founding Forward Deployed Engineer" ~0.85) — a 0.90 floor was
    # neutering most real single-word targets. A genuine miss falls through to a
    # manual resolution path (which verifies company identity).
    floor = 0.82 if strict else _MATCH_FLOOR
    # Slugs are ordered most-specific -> least (candidate_slugs). Accept the FIRST
    # slug that yields a gated title match, so the real full-name board outranks a
    # coincidentally same-named bare-word board.
    for slug in slugs:
        best = None  # (score, url, ats, ptitle, jd)
        for ats, fetch in _ATS:
            try:
                postings = fetch(slug, get)
            except Exception:
                continue  # board/slug not found on this ATS -> try next
            for ptitle, purl, jd in postings or []:
                s = _match_score(title, ptitle)
                if s is not None and s >= floor and (best is None or s > best[0]):
                    best = (s, purl, ats, ptitle, jd)
        if best:
            return {"url": best[1], "ats": best[2], "score": round(best[0], 3),
                    "title": best[3], "jd": best[4]}
    return None


# Minimum matched-JD length to overwrite the sourced jd_text. Below this the board
# JD is likely a stub; keep whatever was sourced rather than replacing it with less.
_MIN_JD_OVERWRITE = 400


def apply_to_job(job: dict, r: dict) -> None:
    """Write a resolved match `r` (from resolve()) into `job`, IN PLACE. The single
    place callers write a resolved match, so backfill and live sourcing can never
    drift. Writes the engine-driven field (`url`, read before `apply_url`),
    preserves the original link, and overwrites the thin sourced title/JD with the
    matched posting's real ones so fit re-scores accurately next sourcing pass."""
    direct = r["url"]
    orig = job.get("url") or job.get("apply_url") or ""
    if orig and orig != direct and not job.get("source_url"):
        job["source_url"] = orig
    job["url"] = direct
    job["apply_url"] = direct
    if r.get("title"):
        job["title"] = r["title"]            # matched posting's real title is authoritative
    if r.get("jd") and len(r["jd"]) >= _MIN_JD_OVERWRITE:
        job["jd_text"] = r["jd"]
        job["fit_stale"] = True              # signal: re-score fit off the new JD
    job["apply_ats"] = r.get("ats")          # so the dashboard can route auto vs manual
    job.pop("needs_url_resolution", None)
