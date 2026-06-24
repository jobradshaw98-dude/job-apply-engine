"""Office / in-person commitment answer guard.

Locked rule (feedback_office_commitment_answer): in-office / hybrid / RTO / on-site
commitment questions are ALWAYS answered **Yes** unless the role is remote — these are
screen-out gates, and a "No" is a hard reject. So they must be auto-answered, never
escalated to Sam (live staging runs repeatedly escalated them, e.g. "Are you able to
come into the office four days per week?", "Are you open to working in-person ... 25% of
the time?").

This mirrors `work_auth.py`: a PURE classifier the orchestrator drives a VERIFIED answer
off of, halting only if the set fails. It is deliberately a TIGHT allow-list — a wrong
auto-Yes here is a serious error, so anything outside the allow-list returns UNRELATED
(the caller leaves it to escalate).

RELOCATION (policy change, Sam 2026-06-09): "Are you open to relocation?" is now ALWAYS
answered **Yes** — Sam is open to relocation; a "No"/escalate was screening him out wrongly.
It joins the same screen-clearing auto-Yes bucket as in-office/RTO (he decides for real at offer
stage). The NEGATION guard still catches inverted phrasings ("unable/unwilling to relocate?").

CRITICAL EXCLUSIONS (must NEVER be auto-answered as office-commitment):
  * Work-auth / visa / sponsorship / citizenship — owned by `classify_work_auth`; reused
                  here to skip, never re-answered.
  * EEO / demographic self-ID — owned by `questions._is_eeo`; left for Sam.
  * Travel / willingness to travel / shift work / overtime — ambiguous, NOT a fixed Yes;
                  escalate.
Default = UNRELATED (escalate). When in doubt, do not auto-answer.
"""
import re
from enum import Enum

from .work_auth import classify_work_auth, WorkAuthDecision


class OfficeCommitmentDecision(str, Enum):
    AUTO_YES = "auto_yes"      # true office/in-person/hybrid/RTO commitment -> answer Yes
    UNRELATED = "unrelated"    # not an office-commitment question -> caller handles normally


# DENY list — phrases that LOOK office-adjacent but must NEVER auto-Yes. Checked first.
# (Relocation was here; as of 2026-06-09 it is auto-Yes — see _ALLOW below. Nothing is currently
# denied, but the hook stays for future screen-clearing exceptions.)
_DENY = re.compile(
    r"(?!x)x",                       # matches nothing (placeholder; no current deny terms)
    re.IGNORECASE,
)

# Travel / shift / overtime — office-adjacent but NOT a fixed Yes. Escalate (UNRELATED).
_AMBIGUOUS = re.compile(
    r"\btravel\b|\btraveling\b|\btravelling\b|"
    r"\bshift\b|\bshifts\b|\bovertime\b|\bon[- ]?call\b|\bnight\b|\bweekend\b",
    re.IGNORECASE,
)

# ALLOW list — true office/in-person/hybrid/RTO commitment phrasings. Word-boundary anchored
# where a bare token could misfire (e.g. "office" inside "officer" — guarded by \b).
_ALLOW = re.compile(
    r"\bin[- ]?person\b|"                         # "work in-person", "in person"
    r"\bon[- ]?site\b|\bonsite\b|"                 # "on-site", "onsite"
    r"\bhybrid\b|"                                 # hybrid arrangements
    r"\bRTO\b|return[- ]to[- ]office|"             # RTO / return-to-office
    r"\bin[- ]?office\b|"                          # "work in-office"
    r"\bin the office\b|"                          # "in the office X% of the time"
    r"come in(?:to)? (?:the |our )?office|"        # "come into the office four days per week"
    r"work(?:ing)? (?:from|out of|in) (?:the |our |one of our )?(?:\w+ ){0,3}offices?|"  # "work from our [SF] office"
    r"days? per week (?:in|at|from)|"              # "X days per week in the office"
    r"\bcommute\b|\bcommuting\b|"                  # "able to commute to <city>"
    r"relocat|"                                    # relocation — now auto-Yes (Sam 2026-06-09)
    r"willing to move|open to (?:moving|relocating)",
    re.IGNORECASE,
)

# NEGATION / INVERSION guard — phrasings where the office keyword is present but a "Yes"
# would be the HARMFUL answer (telling the employer Sam CANNOT be on-site / prefers remote).
# These must NEVER auto-Yes; escalate to Sam. Over-matching here only causes a safe escalate,
# so it is deliberately broad. None of the real positive labels ("able to come into the office",
# "open to working in-person 25%", "able to commute to <city>") contain any of these tokens.
_NEGATION = re.compile(
    r"\bunable\b|\bnot\s+able\b|\bunwilling\b|\bunavailable\b|\bcannot\b|\bcan'?t\b|"   # inability
    r"\bobject\b|\bobjection\b|\boppos(?:e|ed)\b|"                                       # opposition
    r"\bprefer\s+not\b|\bprefer\s+to\s+not\b|\bprefer\b.{0,15}\bremote\b|"               # remote preference
    r"\bfully\s+remote\b|\bremote[- ]only\b|\bremote\s+role\b|\bwork\s+remotely\b|"      # remote-only ask
    r"\brequire\s+(?:a\s+)?(?:fully\s+)?remote\b|"                                       # "require a remote role"
    r"\bproblem\b|\bdeal[- ]?breaker\b",                                                 # "is on-site a problem?"
    re.IGNORECASE,
)

# A bare "X days per week" is only an office signal when paired with an office context.
# Guarded by requiring an office word elsewhere in the label (handled in classify).
_DAYS_PER_WEEK = re.compile(r"\b\d+\s*(?:\+\s*)?days?\s+(?:per|a)\s+week\b", re.IGNORECASE)
_OFFICE_CONTEXT = re.compile(
    r"\boffice\b|\bin[- ]?person\b|\bon[- ]?site\b|\bonsite\b|\bhybrid\b",
    re.IGNORECASE,
)


def classify_office_commitment(label: str) -> OfficeCommitmentDecision:
    """Classify a question label. AUTO_YES only for a genuine office/in-person/hybrid/RTO/
    days-per-week-in-office commitment; UNRELATED for everything else (caller escalates).

    Order matters: DENY (relocation) and the owned-by-another-guard checks run BEFORE the
    allow-list so an office-flavored phrase can never override them.
    """
    t = (label or "").strip()
    if not t:
        return OfficeCommitmentDecision.UNRELATED

    # 1. Hard DENY — relocation is owned by Sam, never auto-answered here.
    if _DENY.search(t):
        return OfficeCommitmentDecision.UNRELATED

    # 2. Owned by the work-auth guard (sponsorship/authorization/visa/citizenship) — skip.
    #    A label like "authorized to work on-site" must go to work-auth, not here.
    if classify_work_auth(t) != WorkAuthDecision.UNRELATED:
        return OfficeCommitmentDecision.UNRELATED

    # 3. EEO / demographic self-ID — left for Sam (lazy import avoids a cycle).
    try:
        from .questions import _is_eeo
        if _is_eeo("", t):
            return OfficeCommitmentDecision.UNRELATED
    except Exception:
        pass

    # 4. Ambiguous (travel / shift / overtime / on-call) — NOT a fixed Yes; escalate.
    if _AMBIGUOUS.search(t):
        return OfficeCommitmentDecision.UNRELATED

    # 4.5 NEGATION / INVERSION — an office keyword may be present, but a "Yes" would be the
    #     harmful answer ("Are you UNABLE to work on-site?", "Do you OBJECT to RTO?", "Do you
    #     require a FULLY REMOTE role?"). Escalate — never auto-Yes an inverted question.
    if _NEGATION.search(t):
        return OfficeCommitmentDecision.UNRELATED

    # 5. Allow-list — a true office/in-person/hybrid/RTO commitment.
    if _ALLOW.search(t):
        return OfficeCommitmentDecision.AUTO_YES

    # 6. "X days per week" only counts WITH an office context word present.
    if _DAYS_PER_WEEK.search(t) and _OFFICE_CONTEXT.search(t):
        return OfficeCommitmentDecision.AUTO_YES

    # 7. Default: not an office-commitment question.
    return OfficeCommitmentDecision.UNRELATED
