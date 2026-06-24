# -*- coding: utf-8 -*-
"""G4 — resolve_work_auth (policy + geography) + classify_role_location.

The resolver is the single source of truth the orchestrator's work-auth fill site uses, REPLACING
a trust-the-staged-value path (the Scale-AI bug: staged sponsorship="Yes"). It folds the locked
policy answers (work_auth.classify_work_auth) together with a GEOGRAPHY gate:

  * US-based (or unknown/sparse) role -> the no-red-flag answer (authorized=Yes, sponsorship=No).
  * a role based in a DIFFERENT country (Cresta "Australia (Remote)", London, Toronto) -> NEEDS_HUMAN
    (Sam is authorized in the US under TN, NOT abroad — auto-Yes there is a truthfulness lie).
  * citizenship/visa/ambiguous -> NEEDS_HUMAN regardless of geography.

These tests PIN: the US no-red-flag answers, the sponsorship=No regression (the Scale bug), the
foreign-role NEEDS_HUMAN gate, and the location classifier's edges.
"""
import pytest

from apply_engine.work_auth_policy import (resolve_work_auth, classify_role_location,
                                           WorkAuthResolution as R)


# ======================================================================================
# classify_role_location: us | foreign | unknown
# ======================================================================================

@pytest.mark.parametrize("loc,expected", [
    # US: explicit token, state code tail, metro, US-remote
    ("Carlsbad, CA", "us"),
    ("San Francisco, CA", "us"),
    ("Austin, TX", "us"),
    ("New York, NY", "us"),
    ("United States", "us"),
    ("Remote (US)", "us"),
    ("US Remote", "us"),
    ("Seattle, WA (Hybrid)", "us"),
    # foreign: country or unambiguous city
    ("Australia (Remote)", "foreign"),
    ("London", "foreign"),
    ("London, UK", "foreign"),
    ("Toronto, Canada", "foreign"),
    ("Berlin, Germany", "foreign"),
    ("Bengaluru, India", "foreign"),
    ("Sydney", "foreign"),
    ("United Kingdom", "foreign"),
    # unknown / sparse -> defaults to the common US path downstream (NOT halted on its own)
    ("", "unknown"),
    ("Remote", "unknown"),
    ("Anywhere", "unknown"),
])
def test_classify_role_location(loc, expected):
    assert classify_role_location(loc) == expected


def test_foreign_signal_beats_us_substring():
    # a mixed posting that includes a US metro AND a foreign country -> foreign wins (don't auto-Yes)
    assert classify_role_location("Austin, TX or Sydney, Australia") == "foreign"


def test_bare_us_substring_does_not_false_trigger():
    # "industrious" contains the letters "us" but is not a US location signal
    assert classify_role_location("Industrious coworking space") == "unknown"


# ======================================================================================
# resolve_work_auth: the US no-red-flag answers (common path, must not regress)
# ======================================================================================

def test_us_sponsorship_returns_no():
    """The Scale-AI regression: a sponsorship question for a US role -> SPONSORSHIP_NO (never Yes),
    computed from policy regardless of any staged value."""
    r = resolve_work_auth("Will you now or in the future require sponsorship?", "Carlsbad, CA")
    assert r == R.SPONSORSHIP_NO


def test_us_authorized_returns_yes():
    r = resolve_work_auth("Are you legally authorized to work in the United States?", "San Diego, CA")
    assert r == R.AUTHORIZED_YES


def test_us_combined_returns_authorized_no_sponsorship():
    r = resolve_work_auth("Are you authorized to work without requiring sponsorship?", "Austin, TX")
    assert r == R.AUTHORIZED_NO_SPONSORSHIP


def test_unknown_location_defaults_to_us_path():
    """A sparse/blank location must NOT halt a domestic role — it defaults to the common US path."""
    assert resolve_work_auth("Do you require visa sponsorship?", "") == R.SPONSORSHIP_NO
    assert resolve_work_auth("Are you authorized to work in the US?", "Remote") == R.AUTHORIZED_YES


def test_unrelated_question_is_unrelated_regardless_of_location():
    assert resolve_work_auth("What is your expected salary?", "Australia") == R.UNRELATED


# ======================================================================================
# resolve_work_auth: the GEOGRAPHY gate -> NEEDS_HUMAN (never auto-Yes for a foreign country)
# ======================================================================================

def test_foreign_authorization_question_needs_human():
    """The Cresta case: an authorization question for an Australia-based role -> NEEDS_HUMAN, NOT
    an auto-Yes (Sam's US TN does not authorize work in Australia)."""
    r = resolve_work_auth("Are you authorized to work in this country?", "Australia (Remote)")
    assert r == R.NEEDS_HUMAN


def test_foreign_sponsorship_question_needs_human():
    r = resolve_work_auth("Do you require visa sponsorship?", "London")
    assert r == R.NEEDS_HUMAN


def test_foreign_combined_question_needs_human():
    r = resolve_work_auth("Are you authorized to work without sponsorship?", "Toronto, Canada")
    assert r == R.NEEDS_HUMAN


def test_citizenship_needs_human_even_for_us_role():
    # citizenship/visa is ambiguous -> always NEEDS_HUMAN, independent of geography
    assert resolve_work_auth("Are you a US citizen?", "Carlsbad, CA") == R.NEEDS_HUMAN
    assert resolve_work_auth("What is your visa status?", "San Francisco, CA") == R.NEEDS_HUMAN
