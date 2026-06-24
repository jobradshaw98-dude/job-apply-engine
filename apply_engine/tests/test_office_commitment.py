"""Unit truth-table for the office-commitment classifier (pure, no browser).

Locks feedback_office_commitment_answer: in-office/hybrid/RTO/on-site/days-per-week
commitment Qs -> AUTO_YES (screen-out gate); relocation/work-auth/EEO/travel/ambiguous ->
UNRELATED (escalate). A wrong AUTO_YES here is a serious error, so the allow-list is tight
and the default is UNRELATED. Positive examples are the REAL labels captured on the live
JOB-227/237/238 staging runs."""
import pytest

from apply_engine.office_commitment import (classify_office_commitment,
                                            OfficeCommitmentDecision)

YES = OfficeCommitmentDecision.AUTO_YES
NO = OfficeCommitmentDecision.UNRELATED


# ---- positive: every one of these MUST auto-Yes ---------------------------------------
AUTO_YES_LABELS = [
    # live-captured labels (JOB-227/237/238 audit.jsonl)
    "Are you able to come into the office four days per week?",
    "Are you open to working in-person in one of our offices 25% of the time?",
    # the brief's other canonical examples
    "Are you able to commute to Austin?",
    "Are you comfortable working on-site?",
    "Are you comfortable working on-site/hybrid?",
    # phrasing variants across the allow-list
    "Are you willing to work from our office 3 days per week?",
    "This is a hybrid role — are you comfortable with a hybrid schedule?",
    "Are you able to work in-office Monday through Thursday?",
    "Do you agree to our return-to-office policy?",
    "Are you on board with our RTO expectations?",
    "Are you able to be onsite as needed?",
    "Can you commute to our New York office daily?",
    "Are you willing to work out of our San Francisco office?",
    "Are you able to be in the office 25% of the time?",
    # RELOCATION — policy flip 2026-06-09: the applicant is open to relocation -> always Yes
    "Are you open to relocation for this role?",
    "Are you willing to relocate to Austin for this role?",
    "Would you relocate for this position?",
    "Are you open to relocating to be near the office?",
]


@pytest.mark.parametrize("label", AUTO_YES_LABELS)
def test_auto_yes_labels(label):
    assert classify_office_commitment(label) == YES, label


# ---- negative: every one of these MUST be left to escalate (UNRELATED) -----------------
UNRELATED_LABELS = [
    # WORK-AUTH / visa / sponsorship / citizenship (owned by work_auth.py)
    "Are you legally authorized to work in the United States?",
    "Will you now or in the future require sponsorship for an employment visa?",
    "Are you authorized to work in the United States without requiring sponsorship?",
    "What is your citizenship?",
    # EEO / demographic
    "Do you identify as a member of an underrepresented gender?",
    "Are you a protected veteran?",
    "Please self-identify your race/ethnicity.",
    # AMBIGUOUS — travel / shift / overtime / on-call (NOT a fixed Yes)
    "Are you willing to travel up to 50% of the time?",
    "Are you able to travel for this role?",
    "Are you willing to work night shifts?",
    "Are you available for on-call rotations?",
    "Are you comfortable working overtime and weekends?",
    # INVERSION / NEGATION — office keyword present but "Yes" is the HARMFUL answer. These
    # MUST escalate, never auto-Yes (else the engine affirms the applicant can't be on-site).
    "Are you unable to work on-site?",
    "Are you not able to come into the office?",
    "Do you object to our return-to-office policy?",
    "Are you opposed to working in-person?",
    "Is working on-site a problem for you?",
    "Do you require a fully remote role?",
    "Are you looking for a remote-only position?",
    "Do you prefer to work remotely rather than in the office?",
    "Would being unable to commute be a deal-breaker?",
    "Are you unwilling to work in a hybrid arrangement?",
    "Are you unable to relocate for this role?",            # inverted relocation -> escalate
    "Are you unwilling to relocate?",
    # genuinely unrelated custom questions
    "Why do you want to work here?",
    "How many years of Python experience do you have?",
    "Are you comfortable working in a fast-paced environment?",
    "",
    "   ",
]


@pytest.mark.parametrize("label", UNRELATED_LABELS)
def test_unrelated_labels(label):
    assert classify_office_commitment(label) == NO, label


# ---- targeted edge cases --------------------------------------------------------------

def test_relocation_is_auto_yes_but_inverted_relocation_escalates():
    """Policy 2026-06-09: relocation is auto-Yes (the applicant is open to it); the negation guard still
    catches an inverted phrasing so the engine never affirms he CAN'T relocate."""
    assert classify_office_commitment("Are you open to relocating closer to the office?") == YES
    assert classify_office_commitment("Are you open to relocation for this role?") == YES
    assert classify_office_commitment("Are you unable to relocate?") == NO


def test_days_per_week_needs_office_context():
    """A bare 'X days per week' with NO office/on-site/hybrid word is ambiguous -> UNRELATED;
    the same phrasing WITH an office word is AUTO_YES."""
    assert classify_office_commitment("Can you work 4 days per week?") == NO
    assert classify_office_commitment(
        "Can you work 4 days per week in the office?") == YES


def test_travel_not_auto_yes_even_with_office_word():
    """'travel to the office' is ambiguous (travel %) — the travel DENY/ambiguous rule must
    keep it UNRELATED rather than the office word forcing a Yes."""
    assert classify_office_commitment(
        "Are you willing to travel to the office occasionally?") == NO


def test_officer_does_not_falsely_match_office():
    """Word-boundary safety: 'officer' must not trip the bare 'office' family."""
    assert classify_office_commitment(
        "Have you ever served as a corporate officer?") == NO
