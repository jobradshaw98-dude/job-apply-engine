# -*- coding: utf-8 -*-
"""Immigration / work-authorization DISCLOSURE guard — a DETERMINISTIC content gate.

WHY THIS EXISTS
A ledger-grounded regeneration can "converge" to an essay that is TRUTHFUL but POLICY-VIOLATING:
it volunteers the applicant's visa/citizenship/sponsorship/green-card status in FREE-TEXT application
content. The accuracy/fabrication gate cannot catch this — the statement is factually true, so the
ledger judge passes it. The hard policy is: NEVER volunteer immigration status in application
content. Work authorization is handled ONLY in the structured screening dropdowns
(sponsorship=No / authorized=Yes). The nuance is explained to a HUMAN later, never written into the
application.

A representative violation this must BLOCK (a free-text "Additional Information" essay):
    "I currently work in the United States on a work visa and may need sponsorship for this role.
     I raise this upfront..."
Any such first-person immigration disclosure must produce a BLOCK finding.

WHAT IT CATCHES (first-person self-disclosure only)
  * visa types: TN, H-1B/H1B, L-1, O-1, OPT, CPT, EAD, work permit/visa, employment visa,
    "on a ... visa".
  * green card / permanent resident / lawful permanent resident / PR status / adjustment of
    status / AOS.
  * citizenship/nationality in a work-auth context ("I am a Canadian/<nationality> citizen",
    "I am a citizen of ...").
  * volunteered sponsorship ("I require/need/would need sponsorship", "visa sponsorship").
  * marriage-based green-card disclosures ("marriage-based", "green card through marriage").

PRECISION
Deterministic regex/keyword, NO LLM. Anchored on FIRST-PERSON + immigration-term co-occurrence so
non-disclosure uses do NOT false-positive: "citizen science", "authorized users of the API",
"sponsor the event", "green dashboard". Each hit yields a BLOCK finding shaped exactly like the
fabrication gate's findings so it rides the SAME converge / verify_ready path. The fix is always
REMOVAL — the iterate-to-clean loop rewords the disclosure out.

PURE / OFFLINE — never calls the model, never touches the network.
"""
import re
from typing import List

# The standard BLOCK-finding text for every disclosure hit. Kept verbatim so the converge loop's
# feedback_clause threads a consistent "REMOVE the immigration sentence" instruction.
DISCLOSURE_ISSUE = (
    "volunteers immigration/work-auth status in free-text content — violates the work-auth "
    "policy; never disclose visa/citizenship/sponsorship/GC in application content"
)
DISCLOSURE_FIX = (
    "REMOVE the immigration/visa/citizenship/sponsorship sentence entirely; work authorization "
    "is handled only in the structured screening fields"
)

# First-person markers. A disclosure is the applicant SPEAKING ABOUT THEMSELVES — "I am on a TN visa",
# "my green card", "I require sponsorship". An employer's third-person policy text ("the company
# does not sponsor visas") or a neutral term ("citizen science") is NOT a self-disclosure.
_FP = r"(?:\bI\b|\bI'm\b|\bmy\b|\bmine\b|\bme\b|\bI've\b)"

# Sentence splitter — we report the offending SENTENCE/span, and anchor first-person + the
# immigration term WITHIN THE SAME sentence so a stray "I" three sentences away can't create a
# false co-occurrence.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def _split_sentences(text: str) -> List[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s.strip()]


# ---- category patterns (each matched WITHIN a single sentence) --------------------------------
#
# Every pattern is designed to fire ONLY on an immigration term. The first-person requirement is
# enforced separately (a sentence must ALSO contain a first-person marker, except for the few
# self-evidently first-person phrasings like "require sponsorship" / "marriage-based green card"
# which are disclosures regardless and carry their own first-person-ish anchor in `_ALWAYS`).

# Visa types. Word-boundary anchored. "TN" only as a standalone token in a visa context to avoid
# matching "TN" inside other words; we additionally require a visa/status/work-auth cue OR an
# explicit "TN visa"/"TN status" to keep it precise.
_VISA = re.compile(
    r"\b("
    r"H-?1-?B|L-?1|O-?1|E-?3|TN\s+(?:visa|status|holder|classification)|"
    r"\bOPT\b|\bCPT\b|\bEAD\b|employment\s+authorization\s+document|"
    r"work\s+(?:permit|visa)|employment\s+visa|"
    r"on\s+(?:an?\s+)?(?:[A-Za-z0-9-]+\s+)?visa|"
    r"under\s+(?:my\s+)?(?:TN|H-?1-?B|L-?1|O-?1|E-?3)\b(?:\s+status)?|"
    r"qualify\s+under\s+TN"
    r")\b",
    re.IGNORECASE,
)
# Bare "TN visa"/"TN status" already covered above; also catch "a TN" only when paired with a
# visa/status/work cue in the same sentence (handled by _TN_CONTEXT below).
_TN_TOKEN = re.compile(r"\bTN\b", re.IGNORECASE)
_TN_CONTEXT = re.compile(r"\b(visa|status|authoriz|work|immigrat|sponsor|classification)", re.IGNORECASE)

# Green card / permanent residence / AOS.
_GREENCARD = re.compile(
    r"\b("
    r"green\s*card|"
    r"(?:lawful\s+)?permanent\s+resident(?:ce|cy)?|"
    r"\bLPR\b|"
    r"\bPR\s+status\b|"
    r"adjustment\s+of\s+status|"
    r"\bAOS\b|"
    r"advance\s+parole|"
    r"\bI-?485\b|\bI-?130\b|\bI-?765\b"
    r")\b",
    re.IGNORECASE,
)

# Citizenship / nationality IN A WORK-AUTH/IMMIGRATION CONTEXT. We anchor on the explicit
# "I am a <nationality> citizen" / "I am a citizen of ..." / "I am NOT a US citizen" phrasings
# (self-disclosure), NOT on the bare word "citizen" (which appears in "citizen science",
# "senior citizen", "good corporate citizen").
_CITIZENSHIP = re.compile(
    r"\bI\s+am\s+(?:not\s+|currently\s+)?"
    r"(?:an?\s+)?"
    r"(?:[A-Za-z]+\s+)?"          # optional nationality adjective, e.g. "Canadian"
    r"citizen(?:ship)?\b"
    r"|\bI\s+am\s+a\s+citizen\s+of\b"
    r"|\bmy\s+citizenship\b"
    r"|\bmy\s+nationality\b"
    r"|\bI\s+hold\s+(?:[A-Za-z]+\s+)?citizenship\b"
    r"|\bI\s+am\s+(?:a\s+)?(?:foreign\s+national|non-?(?:US|U\.S\.)\s+citizen)\b",
    re.IGNORECASE,
)

# Volunteered sponsorship. "I require/need/would need sponsorship", "visa sponsorship", "require
# sponsorship to work". These read as self-disclosure even without a separate first-person marker
# when phrased as "require/need ... sponsorship", but we still keep them tied to a request/visa
# cue so "sponsor the event" / "sponsor a project" never match.
_SPONSORSHIP = re.compile(
    r"\b("
    r"(?:require|requiring|need|needing|would\s+need|will\s+need|seeking|seek)\s+"
    r"(?:(?:visa|immigration|employer|h-?1-?b)\s+)?sponsorship|"
    r"(?:visa|immigration|employment|h-?1-?b)\s+sponsorship|"
    r"sponsorship\s+(?:to\s+work|for\s+(?:a\s+)?(?:visa|work\s+authorization))"
    r")\b",
    re.IGNORECASE,
)

# PRIVATE marriage-based GC pathway — must NEVER appear in any external artifact.
_MARRIAGE_GC = re.compile(
    r"\b("
    r"marriage-?based(?:\s+(?:green\s*card|adjustment|aos|petition))?|"
    r"green\s*card\s+through\s+marriage|"
    r"(?:green\s*card|permanent\s+residence)\s+(?:via|through|by)\s+marriage|"
    r"marriage-?based\s+(?:green\s*card|aos|adjustment\s+of\s+status)|"
    r"I-?130\s+petition"
    r")\b",
    re.IGNORECASE,
)

# Phrasings that ARE self-disclosure regardless of a separate first-person pronoun: a job-
# application sentence that says "require visa sponsorship" or names a marriage-based green card is a
# disclosure no matter how it's framed (these are the highest-risk leaks).
_ALWAYS = (
    ("sponsorship", _SPONSORSHIP),
    ("marriage_gc", _MARRIAGE_GC),
)

# Categories that REQUIRE a first-person marker in the same sentence (so third-party / neutral
# uses don't trip the guard).
_FP_REQUIRED = (
    ("visa", _VISA),
    ("green_card", _GREENCARD),
    ("citizenship", _CITIZENSHIP),
)

_FP_RE = re.compile(_FP, re.IGNORECASE)


def _has_first_person(sentence: str) -> bool:
    return bool(_FP_RE.search(sentence))


def _tn_disclosure(sentence: str) -> bool:
    """Bare 'TN' token counts as a visa disclosure only with an immigration/work cue in the same
    sentence (avoids matching the abbreviation 'TN' used for any non-visa meaning)."""
    return bool(_TN_TOKEN.search(sentence) and _TN_CONTEXT.search(sentence))


def detect_immigration_disclosure(text: str) -> List[dict]:
    """Return a list of BLOCK findings for every first-person immigration/work-auth disclosure in
    `text`. DETERMINISTIC — regex only, no LLM, no network.

    Each finding:
        {
          "lens": "disclosure",
          "severity": "BLOCK",
          "category": <one of visa|green_card|citizenship|sponsorship|marriage_gc>,
          "offending_text": <the matched sentence/span>,
          "issue": DISCLOSURE_ISSUE,
          "fix": DISCLOSURE_FIX,
        }

    A clean professional essay (no first-person immigration disclosure) returns []. Near-misses
    ("citizen science", "authorized users of the API", "sponsor the event", "green dashboard")
    return []. One finding per (sentence, category) — a sentence that discloses two categories
    yields two findings; the same category twice in one sentence yields one."""
    if not text or not text.strip():
        return []

    findings: List[dict] = []
    for sentence in _split_sentences(text):
        hit_categories = set()

        # Categories that fire regardless of a separate first-person pronoun (highest-risk leaks).
        for category, pattern in _ALWAYS:
            if pattern.search(sentence):
                hit_categories.add(category)

        # Categories that require a first-person marker in the SAME sentence.
        fp = _has_first_person(sentence)
        if fp:
            for category, pattern in _FP_REQUIRED:
                if pattern.search(sentence):
                    hit_categories.add(category)
            # bare "TN" + immigration cue, first-person sentence
            if "visa" not in hit_categories and _tn_disclosure(sentence):
                hit_categories.add("visa")

        for category in sorted(hit_categories):
            findings.append({
                "lens": "disclosure",
                "severity": "BLOCK",
                "category": category,
                "offending_text": sentence,
                "issue": DISCLOSURE_ISSUE,
                "fix": DISCLOSURE_FIX,
            })
    return findings
