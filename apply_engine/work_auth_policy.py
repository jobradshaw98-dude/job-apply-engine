# -*- coding: utf-8 -*-
"""G4 — work-authorization RESOLVER + geography blocker (design doc §8.2 / §4 `work_auth`).

The Scale-AI / Cresta live run (2026-06-11) exposed two distinct work-auth failure modes the
classifier alone could not cover:

  1. The engine trusted a STAGED `record.work_auth` value that was WRONG (sponsorship="Yes").
     The fix is to compute the answer from POLICY every time, never from a possibly-corrupt
     staged value.
  2. A role based in a DIFFERENT country (Cresta = "Australia (Remote)") was auto-answered "Yes,
     authorized" — but the applicant is authorized in their home country, NOT in Australia. Auto-clearing a
     foreign work-auth screen is a TRUTHFULNESS violation. A foreign role is a HUMAN-ONLY blocker
     (answerable tier), never an auto-Yes.

`resolve_work_auth(question_text, role_location)` is the single source of truth the fill site
calls. It first classifies the QUESTION via the existing `work_auth.classify_work_auth` (so the
locked policy answers stay in one place), then applies the GEOGRAPHY gate on top:

  * role is US-based (or location unknown/ambiguous, which defaults to the common US path) and the
    question is a sponsorship/authorization screen  -> the locked no-red-flag answer
    (authorized=Yes, sponsorship=No, combined=Yes).
  * role is based in a DIFFERENT country where US TN authorization does NOT apply (e.g. "Australia
    (Remote)", "London, UK", "Toronto, Canada")     -> NEEDS_HUMAN (geography mismatch): the
    engine must HALT the question into a `work_auth` human_blocker, NEVER auto-Yes.
  * citizenship / visa / ambiguous immigration question (classifier HALT, independent of geography)
    -> NEEDS_HUMAN.
  * the question is not a work-auth question                                 -> UNRELATED.

Policy invariants (feedback_work_auth_answer_policy): clear the US screen with no immigration red
flags; explain nuance to a human later; NEVER surface GC/marriage context anywhere. This module
emits only Yes/No decisions and a NEEDS_HUMAN signal — it never writes any private context.

PURE: no browser, no I/O, no clock. The caller (orchestrator) drives the widget + builds the
human_blocker; this module only decides WHAT the answer is (or that only the user can give it).
"""
from enum import Enum
from typing import Optional

from .work_auth import classify_work_auth, WorkAuthDecision


class WorkAuthResolution(str, Enum):
    """The decision the fill site acts on.

    AUTHORIZED_YES / SPONSORSHIP_NO / AUTHORIZED_NO_SPONSORSHIP mirror the no-red-flag answers in
    work_auth.WorkAuthDecision (click Yes / click No / click the affirmative combined option).
    NEEDS_HUMAN means HALT into a `work_auth` human_blocker — either a geography mismatch (the role
    is abroad and US authorization can't be assumed) or an ambiguous citizenship/visa question.
    UNRELATED means the question is not a work-auth question (caller handles normally)."""
    AUTHORIZED_YES = "authorized_yes"
    SPONSORSHIP_NO = "sponsorship_no"
    AUTHORIZED_NO_SPONSORSHIP = "authorized_no_sponsorship"
    NEEDS_HUMAN = "needs_human"
    UNRELATED = "unrelated"


# The applicant's authorized country (TN visa). A role based here gets the locked no-red-flag
# answer; a role based in any other identifiable country is a geography mismatch -> NEEDS_HUMAN.
_US_TOKENS = (
    "united states", "u.s.a", "u.s.", "usa", "us", "america",
)
# State / common-US-metro signals: a "City, ST" or a known US metro means US-based even when the
# word "United States" never appears. Two-letter US state codes + a handful of unambiguous metros.
_US_STATE_CODES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in", "ia",
    "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj",
    "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt",
    "va", "wa", "wv", "wi", "wy", "dc",
}
_US_METROS = (
    "new york", "san francisco", "los angeles", "san diego", "seattle", "boston", "austin",
    "chicago", "denver", "atlanta", "carlsbad", "austin", "bay area", "silicon valley",
    "mountain view", "palo alto", "sunnyvale", "cupertino", "redmond", "remote (us",
    "remote - us", "remote, us", "us remote", "remote us",
)
# Identifiable NON-US country / city signals -> geography mismatch (US TN does not authorize here).
# Kept explicit (not "anything that isn't US") so an unknown/blank location defaults to the common
# US path rather than wrongly halting every domestic role with a sparse location string.
_FOREIGN_TOKENS = (
    # countries
    "australia", "canada", "united kingdom", "england", "scotland", "wales", "ireland",
    "germany", "france", "spain", "portugal", "italy", "netherlands", "belgium", "switzerland",
    "sweden", "norway", "denmark", "finland", "poland", "austria", "czech", "romania",
    "india", "china", "japan", "singapore", "hong kong", "south korea", "korea", "taiwan",
    "israel", "brazil", "mexico", "argentina", "chile", "colombia", "new zealand",
    "united arab emirates", "uae", "dubai", "abu dhabi", "saudi arabia", "qatar",
    "south africa", "nigeria", "kenya", "egypt", "ukraine", "estonia", "lithuania", "latvia",
    # unambiguous foreign cities/metros that often omit the country
    "london", "manchester", "dublin", "berlin", "munich", "paris", "amsterdam", "barcelona",
    "madrid", "lisbon", "zurich", "geneva", "stockholm", "oslo", "copenhagen", "helsinki",
    "warsaw", "vienna", "prague", "toronto", "vancouver", "montreal", "ottawa", "calgary",
    "sydney", "melbourne", "brisbane", "perth", "auckland", "wellington",
    "bangalore", "bengaluru", "mumbai", "delhi", "hyderabad", "pune", "chennai",
    "beijing", "shanghai", "shenzhen", "tokyo", "osaka", "seoul", "tel aviv",
    "sao paulo", "mexico city", "buenos aires",
)


def classify_role_location(role_location: Optional[str]) -> str:
    """Classify a free-text role location into "us" | "foreign" | "unknown". PURE.

    Decision order (foreign signals win over a stray "us"/state-code substring so e.g.
    "Austin, TX or Sydney, Australia" is treated as ambiguous-foreign and halted, and a bare
    "Australia" never reads as US via the "us"-substring trap):
      1. an explicit FOREIGN country/city token present -> "foreign".
      2. else an explicit US token / US state code (as a comma-tail "..., CA") / US metro -> "us".
      3. else -> "unknown" (caller defaults unknown to the common US path; we do NOT halt a domestic
         role just because its location string was sparse — the geography halt requires a POSITIVE
         foreign signal)."""
    t = (role_location or "").strip().lower()
    if not t:
        return "unknown"

    # 1. positive foreign signal wins.
    for tok in _FOREIGN_TOKENS:
        if tok in t:
            return "foreign"

    # 2. positive US signal: a US metro, an explicit US token (word-ish), or a trailing state code.
    for metro in _US_METROS:
        if metro in t:
            return "us"
    # explicit US country tokens, matched against whitespace/comma/paren-delimited fields so a bare
    # "us" inside another word (e.g. "industrious") can't false-trigger.
    fields = {f.strip().strip("()").strip() for f in t.replace("/", ",").replace("|", ",").split(",")}
    fields |= set(t.replace("/", " ").replace("|", " ").replace(",", " ").split())
    for tok in _US_TOKENS:
        if tok in fields:
            return "us"
    # trailing 2-letter state code in a "City, ST" pattern (the last comma-separated field).
    parts = [p.strip() for p in t.split(",") if p.strip()]
    if parts:
        tail = parts[-1].split()[0] if parts[-1].split() else ""
        if tail in _US_STATE_CODES:
            return "us"

    return "unknown"


def resolve_work_auth(question_text: str, role_location: Optional[str]) -> WorkAuthResolution:
    """Resolve a single work-auth question to the answer the fill site should act on, applying the
    POLICY answer AND the geography gate. PURE — never trusts a staged value.

    See module docstring for the full decision table. Summary:
      * not a work-auth question                          -> UNRELATED
      * citizenship/visa/ambiguous (classifier HALT)      -> NEEDS_HUMAN
      * sponsorship/authorization question AND role is FOREIGN (US TN doesn't authorize there)
                                                          -> NEEDS_HUMAN (geography mismatch)
      * sponsorship/authorization question AND role is US/unknown -> the locked no-red-flag answer
        (sponsorship->SPONSORSHIP_NO, authorized->AUTHORIZED_YES, combined->AUTHORIZED_NO_SPONSORSHIP)
    """
    decision = classify_work_auth(question_text)

    if decision == WorkAuthDecision.UNRELATED:
        return WorkAuthResolution.UNRELATED

    # Ambiguous citizenship/visa always needs a human, regardless of geography.
    if decision == WorkAuthDecision.HALT:
        return WorkAuthResolution.NEEDS_HUMAN

    # This is a sponsorship/authorization SCREEN. The locked answer only applies where the applicant is
    # actually authorized (US, TN). For a role based abroad, auto-answering "Yes, authorized" is a
    # truthfulness violation -> hand the whole question to the user (answerable human_blocker).
    geo = classify_role_location(role_location)
    if geo == "foreign":
        return WorkAuthResolution.NEEDS_HUMAN

    # US-based or unknown (default to the common US path): emit the locked no-red-flag answer.
    if decision == WorkAuthDecision.SPONSORSHIP_NO:
        return WorkAuthResolution.SPONSORSHIP_NO
    if decision == WorkAuthDecision.AUTHORIZED_YES:
        return WorkAuthResolution.AUTHORIZED_YES
    if decision == WorkAuthDecision.AUTHORIZED_NO_SPONSORSHIP:
        return WorkAuthResolution.AUTHORIZED_NO_SPONSORSHIP

    # Defensive: any unmapped decision is treated as needs-human (never silently auto-answer).
    return WorkAuthResolution.NEEDS_HUMAN
