"""Work-authorization / sponsorship answer guard.

Locked rules (2026-05-31, combined-question hardening 2026-06-01):
  - "require sponsorship?"        -> SPONSORSHIP_NO         (answer No)
  - "authorized to work?"         -> AUTHORIZED_YES         (answer Yes)
  - combined "authorized WITHOUT  -> AUTHORIZED_NO_SPONSORSHIP
     requiring sponsorship?"          (answer the no-red-flag option: Yes to
                                       "authorized without sponsorship" / No to
                                       "do you require sponsorship") — do NOT halt
  - citizenship / nationality     -> HALT (factual, Canadian; hand to Sam)
  - free-text visa / ambiguous    -> HALT
  - unrelated                     -> UNRELATED (caller handles normally)

Per feedback_work_auth_answer_policy: always clear the initial screen with no
immigration red flags; the nuance is explained to a human later. Pure function — the
caller halts the run on HALT and surfaces the exact question to Sam.
"""
import re
from enum import Enum


class WorkAuthDecision(str, Enum):
    SPONSORSHIP_NO = "sponsorship_no"
    AUTHORIZED_YES = "authorized_yes"
    AUTHORIZED_NO_SPONSORSHIP = "authorized_no_sponsorship"
    HALT = "halt"
    UNRELATED = "unrelated"


class WorkAuthVerify(str, Enum):
    PASS = "pass"          # staged answer is a genuine no-red-flag "No / do not need"
    FAIL = "fail"          # staged answer is an affirmative "Yes ... sponsorship / will need"
    AMBIGUOUS = "ambiguous"  # cannot tell from the text -> caller must escalate (fail-safe)


# A genuine no-red-flag ANSWER: an explicit negative. Word-boundary anchored so "no"
# inside "now"/"Minnesota"/"cannot" can NEVER trigger a PASS. Note we deliberately do NOT
# treat bare "require"/"need"/"will" as signal here — those words live in the QUESTION
# ("...will you REQUIRE sponsorship...") and would otherwise poison a review-page snippet
# that contains both the question and its "No" answer.
_NEGATIVE_ANSWER = re.compile(
    r"\b(no|not|don't|doesn't|won't|do not|does not|will not|"
    r"do not need|will not require|without)\b",
    re.IGNORECASE,
)
# An affirmative red-flag ANSWER: an explicit "Yes", or a FIRST-PERSON statement that
# sponsorship IS / WILL BE needed. First-person ("I will need", "I require") is required so
# the bare question verb ("Will you require...") is not mistaken for the applicant's answer.
_AFFIRMATIVE_ANSWER = re.compile(
    r"\byes\b|\bi will need\b|\bi require\b|\bi need\b|\bi do require\b|\bi will require\b",
    re.IGNORECASE,
)
_SPONSOR_OR_AUTH = re.compile(
    r"sponsor|work authorization|authorized to work|authorization to work",
    re.IGNORECASE,
)


def verify_sponsorship_answer(answer_text: str) -> WorkAuthVerify:
    """Verify a STAGED sponsorship / work-authorization answer is the no-red-flag one.

    PURE (no browser). This is the predicate the review-page verifier MUST use instead of a
    bare ``"no" in text`` substring scan — that scan PASSED on the word "now" inside
    "...sponsorship now or in the future...", silently staging the WRONG immigration answer.

    The input may be either a clean answer ("No, I do not need ...") or a review-page snippet
    that bundles the question with its answer ("Will you require sponsorship ...? No"). The
    matching is therefore word-boundary anchored and only treats explicit "Yes" / first-person
    "I will need" / "I require" as affirmative — never the bare question verb "require"/"need".

    Rules:
      * FAIL  if there is an affirmative answer ("Yes", "I will need", "I require") and NO
              explicit negative. An explicit affirmative always loses.
      * PASS  if there is an explicit negative ("No", "do not need", "will not require",
              "without") tied to a sponsorship/auth context, and no bare "Yes" answer.
      * AMBIGUOUS otherwise — the caller escalates (needs_sam), never false-passes.
    """
    t = (answer_text or "").strip()
    if not t:
        return WorkAuthVerify.AMBIGUOUS

    affirmative = bool(_AFFIRMATIVE_ANSWER.search(t))
    negative = bool(_NEGATIVE_ANSWER.search(t))

    if affirmative and not negative:
        return WorkAuthVerify.FAIL
    if affirmative and negative:
        # mixed signal in the same snippet (e.g. a "Yes" answer plus "do not" elsewhere) —
        # fail safe; never guess which one is the real answer.
        return WorkAuthVerify.AMBIGUOUS

    # No affirmative. An explicit negative tied to a sponsorship/auth context is a clean PASS.
    if negative and _SPONSOR_OR_AUTH.search(t):
        return WorkAuthVerify.PASS
    if negative:
        # negative present but no nearby sponsorship/auth keyword to anchor it — be cautious.
        return WorkAuthVerify.AMBIGUOUS

    return WorkAuthVerify.AMBIGUOUS


_CITIZEN = ("citizen", "citizenship", "nationality")
_SPONSOR = ("sponsor", "sponsorship")
# The COMBINED "authorized WITHOUT sponsorship" case requires sponsorship to be explicitly
# NEGATED near the word "sponsor" — a negation token within ~30 chars before it. This is what
# separates the affirmative combined question (answer Yes) from a pure "require sponsorship?"
# question that merely mentions work authorization (answer No). Word-boundaried so "now" ≠ "no".
_SPONSOR_NEGATED = re.compile(
    r"\b(?:without|no|not|never|don'?t|do not|free of|don't)\b[^.?!]{0,30}\bsponsor", re.IGNORECASE)
_AUTHORIZED = ("authorized to work", "legally authorized", "work authorization",
               "authorization to work", "eligible to work")
_VISA = ("visa", "immigration status", "work permit")


def classify_work_auth(question: str) -> WorkAuthDecision:
    t = (question or "").lower()

    has_citizen = any(k in t for k in _CITIZEN)
    has_sponsor = any(k in t for k in _SPONSOR)
    has_auth = any(k in t for k in _AUTHORIZED)
    has_visa = any(k in t for k in _VISA)

    # 1. citizenship/nationality always halts (factual, not a fixed yes/no for Sam)
    if has_citizen:
        return WorkAuthDecision.HALT

    # 2. COMBINED "authorized to work WITHOUT requiring sponsorship?" — a single yes/no
    #    that bundles both signals. Per policy this resolves to the affirmative no-red-flag
    #    answer (authorized=yes / sponsorship=no), rendered as "Yes". The caller picks the
    #    option that means "authorized, no sponsorship needed".
    #    CRITICAL (JOB-281 Together AI, 2026-06-18): match the combined case ONLY when
    #    sponsorship is explicitly NEGATED ("without / not requiring / no sponsorship"). Mere
    #    CO-OCCURRENCE of "sponsorship" + "work authorization" is NOT combined — a pure
    #    "Will you require company sponsorship to retain or extend your work authorization?"
    #    contains both keywords but is a sponsorship question whose no-red-flag answer is "No".
    #    Without this gate it rendered the red-flag "Yes". So: combined ⇒ auth + a sponsor-negation.
    if has_auth and _SPONSOR_NEGATED.search(t):
        return WorkAuthDecision.AUTHORIZED_NO_SPONSORSHIP

    # 3. clean sponsorship question (incl. one that merely mentions authorization) -> No
    if has_sponsor:
        return WorkAuthDecision.SPONSORSHIP_NO

    # 4. clean authorization question -> Yes
    if has_auth:
        return WorkAuthDecision.AUTHORIZED_YES

    # 5. any other visa/immigration phrasing -> halt for human
    if has_visa:
        return WorkAuthDecision.HALT

    # 6. not a work-auth question
    return WorkAuthDecision.UNRELATED
