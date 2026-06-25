"""
Stage-0 enrichment gate (single source of truth for "is this stageable?").

A job is "enriched" (= stageable) only if it carries enough real content to build
a tailored, audited application package against it: a full job description AND a
URL that points at an actual posting (not a careers homepage). Anything that fails
is QUARANTINED — it never enters the stageable set.

This module is the ONE place those rules live. Both the apply gate and the qualify
pass call it, so the rule can never drift between "what we promote" and "what we
allow to stage".

Design intent:
  - missing / thin JD  -> needs enrichment (a thin JD can't drive real tailoring)
  - careers-homepage / empty URL -> needs enrichment (can't auto-apply a landing page)
  - when the URL is ambiguous, we DO NOT quarantine on the URL alone: a
    false-negative (letting a borderline real posting through) is far safer than a
    false-positive (quarantining a genuine job). The JD check catches most junk
    anyway, so the URL rule only fires on the OBVIOUS homepage cases.

Pure: no network, no keys, no disk.
"""

import re
from urllib.parse import urlparse

# Minimum JD length (stripped chars) to count as a "full" description.
# STARTING HEURISTIC — tune freely. A genuinely full JD runs ~3000-8000 chars while
# a truncated/boilerplate capture is ~600. 1200 sits above the noise floor but below
# a real JD, so it quarantines thin captures without rejecting legitimate short postings.
MIN_JD_CHARS = 1200


# ── URL classification ───────────────────────────────────────────────────────

# Path segments that, when the path ENDS there with nothing meaningful after,
# mark a careers landing page rather than a specific posting.
_HOMEPAGE_TAIL_SEGMENTS = {
    "careers", "career", "jobs", "job-openings", "openings", "vacancies",
    "join-us", "join", "work-with-us",
}

# A "job id-ish" path segment: a long run of digits, or a uuid. Either one is a
# strong signal that the URL targets one specific posting.
_DIGIT_RUN = re.compile(r"\d{4,}")  # 4+ consecutive digits (greenhouse/lever/jobvite ids)
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


def resolve_apply_url(job: dict) -> str:
    """The URL we'd actually drive: prefer `url`, fall back to `apply_url`. Stripped."""
    return (job.get("url") or job.get("apply_url") or "").strip()


def is_careers_homepage(url: str) -> bool:
    """True only for the OBVIOUS careers-homepage / landing-page cases.

    Returns False (treat as a real posting, do NOT quarantine) whenever we're
    unsure — false-negative-safe per the contract. We only return True when the
    path clearly has no posting identifier and bottoms out at a careers landing.
    """
    if not url:
        # Emptiness is handled separately (no application URL); not a "homepage".
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    path = (parsed.path or "").strip()

    # Empty path or bare root ("" or "/") with no posting -> homepage.
    # e.g. https://careers.example.com/  ,  https://company.com
    if path in ("", "/"):
        return True

    # Strong "this is a real posting" signals anywhere in the URL -> never a homepage.
    # Covers: greenhouse /jobs/<digits>, lever/ashby /<co>/<uuid>, workday .../job/...,
    # jobvite /<co>/job/<id>, any deep digit/uuid path.
    full = url.lower()
    if _UUID.search(full) or _DIGIT_RUN.search(parsed.path):
        return False
    # Workday and many ATSs put the posting under a literal "/job/" segment.
    if "/job/" in parsed.path.lower():
        return False

    # Split the path into meaningful segments (drop empties from leading/trailing /).
    segments = [s for s in parsed.path.lower().split("/") if s]

    # The last segment is a careers landing word (careers/jobs/openings/...) and
    # nothing posting-like follows -> homepage. Catches:
    #   /careers , /careers/ , /en/careers , /careers/professional  (only if last seg is the word)
    if segments and segments[-1] in _HOMEPAGE_TAIL_SEGMENTS:
        return True

    # A single-segment path that is itself a careers word -> homepage.
    if len(segments) == 1 and segments[0] in _HOMEPAGE_TAIL_SEGMENTS:
        return True

    # Workday landing pages: host is *.myworkdayjobs.com and the path is just the
    # tenant/site name with no /job/ segment (already excluded above).
    # e.g. acme.wd1.myworkdayjobs.com/External_Careers  (a site landing, not a job)
    host = (parsed.netloc or "").lower()
    if "myworkdayjobs.com" in host and "/job/" not in parsed.path.lower():
        return True

    # Anything else: a deep, specific-looking path we can't confidently call a
    # homepage. Be false-negative-safe — let it through.
    return False


# ── Enrichment status ────────────────────────────────────────────────────────

def enrichment_status(job: dict) -> tuple[bool, str | None]:
    """Return (is_enriched, reason).

    is_enriched True  => the job has a full JD and a real posting URL; stageable.
    is_enriched False => quarantine; `reason` is a short human string for the UI/log.

    Rules are checked in order and the FIRST failure is returned, so JD problems
    (the common case) surface before the URL heuristic.
    """
    # 1) No JD at all.
    jd = (job.get("jd_text") or "")
    jd = jd.strip() if isinstance(jd, str) else ""
    if not jd:
        return False, "no job description captured"

    # 2) Thin JD — present but too short to drive real tailoring.
    if len(jd) < MIN_JD_CHARS:
        return False, f"job description too thin ({len(jd)} chars) — needs full JD"

    # 2b) Placeholder/stub JD that PASSES the length check but is actually a bot
    # wall, access-denied page, or a "see the full description on the company
    # site" stub padded past MIN_JD_CHARS. Length alone can't catch these, so reuse
    # the canonical detector in jd_fetch — keeping the rule in one place so it can't
    # drift between sourcing and the stage gate.
    #
    # Guarded import: if jd_fetch can't be imported, skip ONLY this placeholder
    # check (the length gate above still stands) rather than crashing enrichment.
    try:
        from .jd_fetch import looks_like_placeholder as _looks_like_placeholder
    except Exception:
        _looks_like_placeholder = None
    if _looks_like_placeholder is not None and _looks_like_placeholder(jd):
        return False, "job description is a placeholder/stub — needs full JD"

    # 3) URL: empty, or an obvious careers homepage rather than a posting.
    url = resolve_apply_url(job)
    if not url:
        return False, "no application URL"
    if is_careers_homepage(url):
        return False, "URL is a careers homepage, not a direct posting"

    # Enriched.
    return True, None


def needs_enrichment(job: dict) -> bool:
    """Convenience inverse of enrichment_status: True when the job is quarantined."""
    ok, _ = enrichment_status(job)
    return not ok


def enrichment_fields(job: dict) -> dict:
    """Compute the enrichment fields for a job, live.

    {"needs_enrichment": bool, "enrichment_reason": str | None}
    These are deliberately NOT persisted (compute live — never cache a derived field).
    Retained as a convenience wrapper for tests/reporting.
    """
    ok, reason = enrichment_status(job)
    return {"needs_enrichment": not ok, "enrichment_reason": reason}
