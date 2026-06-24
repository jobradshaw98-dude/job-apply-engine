"""Grounded option-picker for constrained dropdown/select questions.

Mirrors answer_gen but for CHOICE questions: the LLM may only return one of the
provided options verbatim (or DECLINE). It can never invent an option, and the
chosen option is still run through the fabrication gate. Injected (mock) drafter +
gate — no network/LLM."""
from apply_engine.choice_gen import (
    resolve_choice, build_choice_prompt, make_resolver,
    resolve_multi_choice, build_multi_choice_prompt, make_multi_resolver,
)


def test_grounded_option_is_selected_when_clean():
    # FACTS support "5+ years"; the model returns the matching option verbatim.
    c = resolve_choice(
        "Years of simulation experience?",
        ["0-2 years", "3-5 years", "5+ years"],
        "FACTS: ~5 years simulation-led engineering.",
        llm_fn=lambda p: "5+ years",
        audit_fn=lambda t: [],
    )
    assert c.status == "answered"
    assert c.value == "5+ years"


def test_decline_when_no_factual_basis_is_escalated():
    # "How familiar are you with Illumina" has no basis in FACTS -> DECLINE -> leave for Sam.
    c = resolve_choice(
        "How familiar are you with Illumina's products?",
        ["Very familiar", "Somewhat", "Not at all"],
        "FACTS: simulation + AI automation. No Illumina history.",
        llm_fn=lambda p: "DECLINE",
        audit_fn=lambda t: [],
    )
    assert c.status == "declined"
    assert c.value == ""


def test_model_inventing_an_option_is_refused():
    # The model returns text that is NOT one of the offered options -> never select it.
    c = resolve_choice(
        "Preferred work location?",
        ["Remote", "Hybrid", "On-site"],
        "FACTS...",
        llm_fn=lambda p: "Fully remote from Mars",
        audit_fn=lambda t: [],
    )
    assert c.status == "declined"
    assert c.value == ""


def test_option_match_is_case_and_whitespace_insensitive():
    c = resolve_choice(
        "Authorized to work?",
        ["Yes", "No"],
        "FACTS: authorized to work in the US.",
        llm_fn=lambda p: "  yes  ",
        audit_fn=lambda t: [],
    )
    assert c.status == "answered"
    assert c.value == "Yes"  # canonical option text, not the model's casing


def test_blocked_when_gate_flags_the_choice():
    c = resolve_choice(
        "Describe your seniority",
        ["Principal-level world-class expert", "Mid-level engineer"],
        "FACTS...",
        llm_fn=lambda p: "Principal-level world-class expert",
        audit_fn=lambda t: ["Overstatement — 'world-class'"],
    )
    assert c.status == "blocked"
    assert "Overstatement" in c.reason


def test_empty_options_declines():
    c = resolve_choice("Q", [], "FACTS", llm_fn=lambda p: "anything", audit_fn=lambda t: [])
    assert c.status == "declined"


def test_llm_error_fails_safe_to_declined():
    def boom(p):
        raise RuntimeError("llm down")
    c = resolve_choice("Q", ["A", "B"], "F", llm_fn=boom, audit_fn=lambda t: [])
    assert c.status == "declined"


def test_audit_error_fails_safe_to_blocked():
    def bad_audit(t):
        raise RuntimeError("gate down")
    c = resolve_choice("Q", ["A", "B"], "F", llm_fn=lambda p: "A", audit_fn=bad_audit)
    assert c.status == "blocked"


def test_make_resolver_is_none_without_an_llm():
    # No drafter -> no resolver -> caller escalates every custom question (safe default).
    assert make_resolver("FACTS", None, lambda t: []) is None


def test_make_resolver_routes_question_and_options_to_resolve_choice():
    r = make_resolver("FACTS: ~5 years simulation.",
                      lambda p: "5+ years", lambda t: [])
    c = r("Years of experience?", ["0-2 years", "5+ years"])
    assert c.status == "answered"
    assert c.value == "5+ years"


def test_prompt_lists_options_verbatim_and_decline_and_facts():
    p = build_choice_prompt("Pick one", ["Alpha", "Beta"], "FACT-A")
    assert "Alpha" in p and "Beta" in p
    assert "DECLINE" in p
    assert "FACT-A" in p
    assert "ONLY" in p.upper()


# ---- resolve_multi_choice (checkbox-group "select all that apply") ----

def test_multi_grounded_subset_is_selected():
    # A ~language list; FACTS support only English -> only that box is checked.
    m = resolve_multi_choice(
        "Language Skill(s) (check all that apply)",
        ["English (ENG)", "French (FRA)", "Mandarin (MAN)", "Spanish (SPA)"],
        "FACTS: Sam is a native English speaker. No other languages.",
        llm_fn=lambda p: "English (ENG)",
        audit_fn=lambda t: [],
    )
    assert m.status == "answered"
    assert m.values == ["English (ENG)"]


def test_multi_accepts_several_grounded_options():
    m = resolve_multi_choice(
        "Which tools have you used?",
        ["ANSYS", "Python", "MATLAB", "Fortran"],
        "FACTS: FEA in ANSYS; automation in Python.",
        llm_fn=lambda p: "ANSYS\nPython",
        audit_fn=lambda t: [],
    )
    assert m.status == "answered"
    assert m.values == ["ANSYS", "Python"]  # order preserved from offered options


def test_multi_refuses_invented_options():
    # The model returns one real option plus one it invented -> the invented one is dropped.
    m = resolve_multi_choice(
        "Languages?",
        ["English (ENG)", "French (FRA)"],
        "FACTS: English only.",
        llm_fn=lambda p: "English (ENG)\nKlingon (KLI)",
        audit_fn=lambda t: [],
    )
    assert m.status == "answered"
    assert m.values == ["English (ENG)"]  # Klingon was never offered -> never returned


def test_multi_all_invented_declines():
    # Nothing the model returned matches an offered option -> check nothing, escalate.
    m = resolve_multi_choice(
        "Languages?",
        ["English (ENG)", "French (FRA)"],
        "FACTS: English only.",
        llm_fn=lambda p: "Klingon\nElvish",
        audit_fn=lambda t: [],
    )
    assert m.status == "declined"
    assert m.values == []


def test_multi_decline_when_no_basis():
    m = resolve_multi_choice(
        "Which certifications do you hold?",
        ["PMP", "Six Sigma", "PE License"],
        "FACTS: no certifications listed.",
        llm_fn=lambda p: "DECLINE",
        audit_fn=lambda t: [],
    )
    assert m.status == "declined"
    assert m.values == []


def test_multi_match_is_case_and_whitespace_insensitive():
    m = resolve_multi_choice(
        "Tools?",
        ["ANSYS", "Python"],
        "FACTS: ANSYS + Python.",
        llm_fn=lambda p: "  ansys \n PYTHON ",
        audit_fn=lambda t: [],
    )
    assert m.status == "answered"
    assert m.values == ["ANSYS", "Python"]  # canonical option text, not the model casing


def test_multi_blocked_when_gate_flags_any_chosen_option():
    # If the gate flags even one chosen option, the whole pick is blocked -> escalate.
    m = resolve_multi_choice(
        "Describe your levels",
        ["Mid-level engineer", "World-class expert"],
        "FACTS...",
        llm_fn=lambda p: "Mid-level engineer\nWorld-class expert",
        audit_fn=lambda t: (["Overstatement — 'world-class'"] if "World" in t else []),
    )
    assert m.status == "blocked"
    assert "Overstatement" in m.reason
    assert m.values == []  # nothing is checked on a block


def test_multi_empty_options_declines():
    m = resolve_multi_choice("Q", [], "FACTS",
                             llm_fn=lambda p: "anything", audit_fn=lambda t: [])
    assert m.status == "declined"
    assert m.values == []


def test_multi_llm_error_fails_safe_to_declined():
    def boom(p):
        raise RuntimeError("llm down")
    m = resolve_multi_choice("Q", ["A", "B"], "F", llm_fn=boom, audit_fn=lambda t: [])
    assert m.status == "declined"
    assert m.values == []


def test_multi_audit_error_fails_safe_to_blocked():
    def bad_audit(t):
        raise RuntimeError("gate down")
    m = resolve_multi_choice("Q", ["A", "B"], "F",
                             llm_fn=lambda p: "A", audit_fn=bad_audit)
    assert m.status == "blocked"
    assert m.values == []


def test_multi_prompt_lists_options_and_subset_instruction():
    p = build_multi_choice_prompt("Pick all", ["Alpha", "Beta"], "FACT-A")
    assert "Alpha" in p and "Beta" in p
    assert "DECLINE" in p
    assert "FACT-A" in p
    assert "ONLY" in p.upper()


def test_make_multi_resolver_is_none_without_an_llm():
    assert make_multi_resolver("FACTS", None, lambda t: []) is None


def test_make_multi_resolver_routes_to_resolve_multi_choice():
    r = make_multi_resolver("FACTS: ANSYS + Python.",
                            lambda p: "ANSYS\nPython", lambda t: [])
    m = r("Tools?", ["ANSYS", "Python", "MATLAB"])
    assert m.status == "answered"
    assert m.values == ["ANSYS", "Python"]
