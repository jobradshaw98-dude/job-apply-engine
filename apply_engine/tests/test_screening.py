"""Conservative grounded Yes/No screening-qualifier classifier.

This is the one path that could auto-submit a FALSE qualification, so the discipline mirrors
work_auth.py: a VERIFIED decision (YES/NO mapped to a real offered option), excluded classes
short-circuit to ESCALATE WITHOUT calling the model, garbled/ambiguous model output fails
CLOSED to ESCALATE, and a YES is only ever produced when truthfully grounded in the
capabilities file. Injected (mock) llm_fn/audit_fn — no network.
"""
import pytest

from apply_engine.screening import (
    ScreeningDecision, is_yesno_screening, classify_screening, resolve_with_screening,
    load_capabilities,
)


# A llm_fn that RECORDS being called and would wrongly answer YES if reached. Using a tracker
# (not a raising stub) is deliberate: a raising stub's exception is swallowed by classify_
# screening's fail-closed `except`, so an exclusion-guard regression would still pass as ESCALATE
# for the wrong reason. Returning "YES" makes any leak produce a visible false-Yes, and the
# `.calls == 0` assertion proves the deterministic path never reached the model.
class _Tracker:
    def __init__(self):
        self.calls = 0

    def __call__(self, _prompt):
        self.calls += 1
        return "YES"


def _assert_excluded(question, options=("Yes", "No")):
    tr = _Tracker()
    r = classify_screening(question, list(options), CAPS, llm_fn=tr, audit_fn=lambda t: [])
    assert r.decision == ScreeningDecision.ESCALATE, f"{question!r} should ESCALATE, got {r.decision}"
    assert tr.calls == 0, f"{question!r} must NOT reach the model (deterministic exclude)"
    return r


CAPS = (
    "Designed agentic/LLM apps: YES (ARIA). Shipped/operated production software: YES. "
    "Deployed AI agents in production: YES (Meridian DevBot). "
    "3+ years relevant experience: YES (5+ effective). Master's degree: YES. "
    "Programming-language fluency (hand-coding): NEVER ASSERT — escalate. PhD: NO."
)


# ---------------------------------------------------------------------------
# is_yesno_screening — only a clean binary Yes/No qualifies for this path
# ---------------------------------------------------------------------------

def test_clean_yes_no_options_are_a_screening_question():
    assert is_yesno_screening(["Yes", "No"]) is True
    assert is_yesno_screening(["No", "Yes"]) is True
    assert is_yesno_screening([" yes ", "NO"]) is True


def test_non_binary_or_partial_options_are_not_screening():
    assert is_yesno_screening(["0-2 years", "3-5 years", "5+ years"]) is False
    assert is_yesno_screening(["Yes"]) is False               # both must be present
    assert is_yesno_screening(["Yes", "No", "Maybe"]) is False
    assert is_yesno_screening(["Yes, I do", "No"]) is False    # decorated -> generic picker
    assert is_yesno_screening([]) is False


# ---------------------------------------------------------------------------
# classify_screening — truthful answers
# ---------------------------------------------------------------------------

def test_truthful_yes_is_answered_and_mapped_to_the_offered_option():
    r = classify_screening(
        "Do you have 3+ years of relevant professional experience?",
        ["Yes", "No"], CAPS, llm_fn=lambda p: "YES", audit_fn=lambda t: [],
    )
    assert r.decision == ScreeningDecision.YES
    assert r.value == "Yes"


def test_truthful_no_is_answered_and_mapped_to_the_offered_option():
    r = classify_screening(
        "Do you have a PhD?",
        ["Yes", "No"], CAPS, llm_fn=lambda p: "NO", audit_fn=lambda t: [],
    )
    assert r.decision == ScreeningDecision.NO
    assert r.value == "No"


def test_yes_maps_even_when_option_casing_differs():
    # NB: a coding-fluency question ("Strong Python?") would now ESCALATE by hard rule, so this
    # casing test uses a legitimately-assertable capability instead.
    r = classify_screening(
        "Do you hold a Master's degree?", ["YES", "no"], CAPS,
        llm_fn=lambda p: "yes", audit_fn=lambda t: [],
    )
    assert r.decision == ScreeningDecision.YES
    assert r.value == "YES"   # returns the option AS THE FORM PRESENTS IT


# ---------------------------------------------------------------------------
# classify_screening — fail CLOSED (the safety contract)
# ---------------------------------------------------------------------------

def test_model_escalate_is_left_for_sam():
    r = classify_screening(
        "Have you led a team of 10+ engineers?",
        ["Yes", "No"], CAPS, llm_fn=lambda p: "ESCALATE", audit_fn=lambda t: [],
    )
    assert r.decision == ScreeningDecision.ESCALATE
    assert r.value == ""


def test_garbled_model_output_fails_closed_to_escalate():
    r = classify_screening(
        "Have you shipped production software?", ["Yes", "No"], CAPS,
        llm_fn=lambda p: "well, probably yes I think", audit_fn=lambda t: [],
    )
    assert r.decision == ScreeningDecision.ESCALATE


def test_empty_model_output_fails_closed():
    r = classify_screening("Have you shipped production software?", ["Yes", "No"], CAPS,
                           llm_fn=lambda p: "", audit_fn=lambda t: [])
    assert r.decision == ScreeningDecision.ESCALATE


def test_llm_error_fails_closed_to_escalate():
    def boom(_p):
        raise RuntimeError("claude down")
    r = classify_screening("Have you shipped production software?", ["Yes", "No"], CAPS,
                           llm_fn=boom, audit_fn=lambda t: [])
    assert r.decision == ScreeningDecision.ESCALATE


def test_audit_block_on_chosen_value_escalates():
    # defense in depth: if the fabrication gate flags the chosen answer, never select it.
    r = classify_screening("Have you shipped production software?", ["Yes", "No"], CAPS,
                           llm_fn=lambda p: "YES", audit_fn=lambda t: ["fabrication"])
    assert r.decision == ScreeningDecision.ESCALATE


# ---------------------------------------------------------------------------
# classify_screening — excluded classes short-circuit WITHOUT the model
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("q", [
    "Do you now or in the future require visa sponsorship?",
    "Will you require sponsorship now or in the future?",
    "Are you legally authorized to work in the United States?",
    "What is your citizenship?",
])
def test_work_auth_is_excluded_deterministically(q):
    _assert_excluded(q)


@pytest.mark.parametrize("q", [
    "Are you Hispanic or Latino?",
    "Do you identify as a member of an underrepresented gender?",
    "Are you a protected veteran?",
    "Do you have a disability?",          # stem 'disab' — must survive the regex fix
    "What is your ethnicity?",            # stem 'ethnic'
    "What are your preferred pronouns?",  # stem 'pronoun'
    "Please indicate your race.",
])
def test_eeo_demographic_is_excluded_deterministically(q):
    _assert_excluded(q)


@pytest.mark.parametrize("q", [
    "Are you willing to relocate?",       # stem 'relocat' — must survive the regex fix
    "Would you relocate for this role?",
    "Are you willing to travel up to 50%?",
    "Have you ever been convicted of a felony?",
    "Have you ever been convicted of a crime?",   # stem 'convict'
    "Have you ever been arrested?",
    "Do you have an active security clearance?",
    "Are you subject to a non-compete agreement?",
    "Are you bound by any NDA or restrictive covenant?",
    "Have you previously been employed by this company?",
    "When can you start?",
    "What is your desired salary?",
])
def test_sensitive_and_disqualifying_classes_are_excluded(q):
    _assert_excluded(q)


@pytest.mark.parametrize("q", [
    "Do you LACK 3+ years of relevant experience?",   # negated -> truthful answer flips to a harmful Yes
    "Are you UNABLE to work on-site?",
    "Are you unwilling to use Python daily?",
    "Do you NOT have production AI experience?",
    "Can you not work in a team environment?",
    # disqualifier / inverted vocabulary that grounds a HARMFUL Yes if it reaches the model
    "Do you have zero years of experience?",
    "Are you disqualified from this position?",
    "Are you unqualified for this role?",
    "Is your relevant experience insufficient?",
    "Are you missing any required certifications?",
    "Are you prohibited from working in this field?",
    "Aren't you ineligible for this role?",
    "Do you have none of the listed skills?",
])
def test_negated_questions_escalate_deterministically(q):
    # A negated qualifier inverts polarity: 'LACK 3+ years' is truthfully NO but a model grounded
    # on 'experience: YES' could answer the disqualifying YES. The audit gate only ever sees the
    # bare option text ("Yes"), never the question, so it cannot catch wrong polarity. The only
    # safe handling is a deterministic ESCALATE before the model is consulted.
    _assert_excluded(q)


# ---------------------------------------------------------------------------
# classify_screening — non-yes/no questions are UNRELATED (generic picker owns them)
# ---------------------------------------------------------------------------

def test_non_binary_question_is_unrelated():
    tr = _Tracker()
    r = classify_screening(
        "Years of simulation experience?",
        ["0-2 years", "3-5 years", "5+ years"], CAPS,
        llm_fn=tr, audit_fn=lambda t: [],
    )
    assert r.decision == ScreeningDecision.UNRELATED
    assert tr.calls == 0   # not a binary yes/no -> never consults the screening model


# ---------------------------------------------------------------------------
# resolve_with_screening — the wrapper that orchestrator callsites use
# ---------------------------------------------------------------------------

def test_wrapper_answers_a_truthful_yes_as_a_choice():
    c = resolve_with_screening(
        "Do you have 3+ years of relevant experience?", ["Yes", "No"],
        facts="(generic facts)", capabilities=CAPS,
        llm_fn=lambda p: "YES", audit_fn=lambda t: [],
    )
    assert c.status == "answered"
    assert c.value == "Yes"


def test_wrapper_escalates_as_a_declined_choice():
    c = resolve_with_screening(
        "Have you led a 10-person team?", ["Yes", "No"],
        facts="(generic facts)", capabilities=CAPS,
        llm_fn=lambda p: "ESCALATE", audit_fn=lambda t: [],
    )
    assert c.status == "declined"
    assert c.value == ""


def test_wrapper_delegates_non_yesno_to_the_generic_picker():
    # A constrained non-binary select must go through resolve_choice (which grounds on FACTS,
    # not capabilities). The generic picker returns the matching option verbatim.
    c = resolve_with_screening(
        "Years of simulation experience?", ["0-2 years", "3-5 years", "5+ years"],
        facts="FACTS: ~5 years simulation-led engineering.", capabilities=CAPS,
        llm_fn=lambda p: "5+ years", audit_fn=lambda t: [],
    )
    assert c.status == "answered"
    assert c.value == "5+ years"


# ---------------------------------------------------------------------------
# capabilities file
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("q", [
    # 2026-06-09: coding questions are ANSWERED truthfully (Python via LLM harnesses), not
    # escalated. They must NOT be deterministically excluded — capabilities.md grounds the answer.
    "Are you proficient in Python?",
    "Do you have experience with Python?",
    "Do you have strong software development skills?",
    "Are you a fluent Python programmer?",
    "Are you an expert software engineer?",
    # non-coding strength / outcome claims also reach the model
    "Do you have strong communication skills?",
    "Have you deployed AI agents in production?",
    "Have you shipped production software?",
    "Are you a strong mechanical engineer?",
])
def test_coding_and_strength_questions_reach_the_model(q):
    from apply_engine.screening import _is_excluded
    assert _is_excluded(q) is False


def test_latin_market_experience_is_not_over_excluded():
    # bare 'latin' stem must NOT swallow "Latin America" — 'latino'/'latinx' cover the EEO intent.
    from apply_engine.screening import _is_excluded
    assert _is_excluded("Do you have experience with Latin America markets?") is False


def test_load_capabilities_returns_grounding_text():
    caps = load_capabilities()
    assert isinstance(caps, str)
    assert caps.strip()
    assert "Python" in caps   # a known truthful capability is present
