import pytest
from apply_engine.work_auth import classify_work_auth, WorkAuthDecision as D
from apply_engine.work_auth import verify_sponsorship_answer, WorkAuthVerify as V


@pytest.mark.parametrize("q,expected", [
    # sponsorship -> No
    ("Will you now or in the future require sponsorship for employment visa status?", D.SPONSORSHIP_NO),
    ("Do you require visa sponsorship?", D.SPONSORSHIP_NO),
    ("Will you require sponsorship now or in the future to work in the US?", D.SPONSORSHIP_NO),
    # authorized -> Yes
    ("Are you legally authorized to work in the United States?", D.AUTHORIZED_YES),
    ("Are you authorized to work in the US?", D.AUTHORIZED_YES),
    ("Do you have current work authorization in the United States?", D.AUTHORIZED_YES),
    # citizenship/nationality -> HALT (factual, even if yes/no)
    ("Are you a U.S. citizen?", D.HALT),
    ("What is your country of citizenship?", D.HALT),
    ("What is your nationality?", D.HALT),
    # free-text visa / ambiguous -> HALT
    ("Please describe your current visa status.", D.HALT),
    ("What is your visa status?", D.HALT),
    # not a work-auth question at all -> UNRELATED
    ("What is your expected salary?", D.UNRELATED),
    ("Years of FEA experience?", D.UNRELATED),
])
def test_classify_work_auth(q, expected):
    assert classify_work_auth(q) == expected


def test_citizenship_beats_authorized_keyword():
    # contains 'authorized to work' AND 'citizen' -> citizenship halt wins
    q = "Are you authorized to work as a US citizen or permanent resident?"
    assert classify_work_auth(q) == D.HALT


def test_combined_authorized_without_sponsorship_is_affirmative_not_halt():
    # combined question bundles authorized + sponsorship into one yes/no. Per the locked
    # policy this must resolve to the affirmative no-red-flag answer, NOT halt.
    q = "Are you authorized to work without requiring sponsorship?"
    assert classify_work_auth(q) == D.AUTHORIZED_NO_SPONSORSHIP


def test_combined_authorized_now_or_in_future_without_sponsorship():
    q = ("Are you legally authorized to work in the US without requiring sponsorship "
         "now or in the future?")
    assert classify_work_auth(q) == D.AUTHORIZED_NO_SPONSORSHIP


def test_pure_sponsorship_still_no():
    assert classify_work_auth("Do you require visa sponsorship?") == D.SPONSORSHIP_NO


def test_sponsorship_mentioning_authorization_is_no_not_combined():
    # REGRESSION (JOB-281 Together AI, 2026-06-18): a PURE sponsorship question that mentions
    # "work authorization" as the thing sponsorship retains contains both keywords but is NOT
    # the combined "without sponsorship" case — the no-red-flag answer is NO. Before the fix
    # this mis-classified as AUTHORIZED_NO_SPONSORSHIP and rendered the red-flag "Yes".
    q = ("Will you now or in the future require company sponsorship to retain or extend your "
         "work authorization in the country where the job is located?")
    assert classify_work_auth(q) == D.SPONSORSHIP_NO


def test_require_sponsorship_to_keep_authorization_is_no():
    q = "Do you require sponsorship to maintain your authorization to work here?"
    assert classify_work_auth(q) == D.SPONSORSHIP_NO


def test_pure_authorized_still_yes():
    assert classify_work_auth("Are you authorized to work in the US?") == D.AUTHORIZED_YES


def test_citizenship_still_halts_even_combined():
    # citizenship wins even if sponsorship/authorization keywords are also present
    q = "Are you a US citizen authorized to work without sponsorship?"
    assert classify_work_auth(q) == D.HALT


# ---- verify_sponsorship_answer: the review-page predicate (BLOCKER 1 / WARN 2) ----
# This replaces the old `"no" in wa_text` substring scan that PASSED on the "no" inside
# "now" and silently staged the wrong immigration answer.

def test_verify_fails_on_affirmative_sponsorship_answer():
    # the exact red-flag phrasing — must FAIL, never pass on the "now" substring
    ans = "Yes, I will need work authorization sponsorship now or in the future"
    assert verify_sponsorship_answer(ans) == V.FAIL


def test_verify_passes_on_genuine_no_answer():
    # the genuine no-red-flag answer — must PASS even though it contains "now"
    ans = "No, I do not need work authorization now or in the future"
    assert verify_sponsorship_answer(ans) == V.PASS


def test_verify_does_not_pass_on_now_substring_alone():
    # "now"/"Minnesota" contain the letters "no" but are NOT a negative answer
    assert verify_sponsorship_answer("I live in Minnesota now") == V.AMBIGUOUS


def test_verify_passes_on_review_snippet_with_no_answer():
    # a review-page snippet bundles the QUESTION (with verb "require") and its "No" answer;
    # the question verb must not be mistaken for an affirmative
    snippet = "Will you require sponsorship now or in the future? | No"
    assert verify_sponsorship_answer(snippet) == V.PASS


def test_verify_fails_on_review_snippet_with_yes_answer():
    snippet = "Will you require sponsorship now or in the future? | Yes"
    assert verify_sponsorship_answer(snippet) == V.FAIL


def test_verify_passes_without_no_keyword_via_do_not_need():
    # robust to a valid confirmation phrased without a leading bare "No"
    ans = "I do not require sponsorship to work; I am authorized to work in the US"
    assert verify_sponsorship_answer(ans) == V.PASS


def test_verify_ambiguous_on_empty_or_unanchored():
    assert verify_sponsorship_answer("") == V.AMBIGUOUS
    assert verify_sponsorship_answer(None) == V.AMBIGUOUS
    # a bare "No" with no sponsorship/auth context is not enough to confirm -> fail safe
    assert verify_sponsorship_answer("No") == V.AMBIGUOUS


def test_verify_ambiguous_on_mixed_yes_and_no():
    # contradictory snippet -> never guess which is the real answer
    snippet = "Yes I will need sponsorship | No I do not"
    assert verify_sponsorship_answer(snippet) == V.AMBIGUOUS
