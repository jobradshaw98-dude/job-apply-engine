# -*- coding: utf-8 -*-
"""TDD for the consolidated ledger PROSE/FORMAT block-criteria shared by BOTH refresh_audit
judge prompts (_judge_answer for answers, audit_content_text for resume/cover elements).

WHY THIS EXISTS
The recurring bug class (feedback_apply_judge_ledger_format_rules: bugs #2 and #5) was a
ledger-grounded fact rendered in a ledger-FORBIDDEN FORM shipping as "ready" because the LLM
judge's BLOCK criteria didn't enforce the ledger's PROSE rules. Two instances (impact-as-counts,
thesis-as-materials-science) were fixed by hand by editing BOTH judge prompts. The hand-fix had
two failure modes this test locks down:
  1. DRIFT — a fix landed in one prompt but not the other (the two prompts were independent
     string literals). The fix: a SINGLE module-level constant LEDGER_PROSE_BLOCK_RULES that
     BOTH prompts must embed. This test asserts both prompts embed the SAME constant.
  2. INCOMPLETENESS — only the two surfaced rules were enforced; the other ledger prose rules
     (ARIA timeline, Codex/Claude-Code attribution, NEVER-CLAIM puffery, no coding fluency) were
     not. This test asserts every enumerated prose rule is present in the shared constant.

These tests are FAST and DETERMINISTIC: they inject a stub llm (no real claude -p call) and
assert (a) the prompt-assembly path embeds the consolidated rules and (b) the
severity-normalization path turns a canned BLOCK finding into a BLOCKED verdict. The slow,
real-LLM behavioural test lives in test_ledger_prose_llm.py (marked @pytest.mark.llm, opt-in).
"""
import json

from apply_engine.refresh_audit import (
    LEDGER_PROSE_BLOCK_RULES,
    LEDGER_PROSE_BLOCK_RULES_ANSWERS,
    _judge_answer,
    audit_content_text,
    audit_answers,
)


# Each enumerated prose rule must be expressed in the single shared constant. We assert on a
# distinctive, stable phrase from each rule's wording so a future edit that drops a rule fails here.
REQUIRED_RULE_MARKERS = [
    "since early 2026",          # ARIA timeline: never "over a year"/"two years"
    "over a year",               # the forbidden ARIA-age phrasing is named explicitly
    "Codex",                     # Codex = Meridian only
    "Claude Code",               # Claude Code = ARIA only
    "PERCENTAGE",                # Meridian impact as a percentage, never people/time/meeting counts
    "design-optimization",       # thesis = automated design-optimization framework
    "materials",                 # ...never materials-science / material-modeling / biomechanics
    "platform scale",            # NEVER-CLAIM puffery: production / at platform scale
    "rare",                      # NEVER-CLAIM: rare/uncommon/unusual combination puffery
    "Python",                    # never claim coding-language (Python/MATLAB) fluency
    "MATLAB",
]


# ---------------------------------------------------------------------------
# 1. The shared constant carries EVERY enumerated prose rule (incompleteness guard).
# ---------------------------------------------------------------------------

def test_shared_constant_contains_every_enumerated_prose_rule():
    for marker in REQUIRED_RULE_MARKERS:
        assert marker in LEDGER_PROSE_BLOCK_RULES, f"prose rule marker missing: {marker!r}"


# ---------------------------------------------------------------------------
# 2. BOTH judge prompts embed the SAME shared constant (drift guard). We capture the prompt by
#    injecting a stub llm that records what it was handed, instead of calling claude -p.
# ---------------------------------------------------------------------------

def _capturing_llm(captured: list):
    def _fn(prompt: str) -> str:
        captured.append(prompt)
        return "[]"
    return _fn


def test_judge_answer_prompt_embeds_shared_constant():
    captured = []
    _judge_answer(_capturing_llm(captured), "Why us?", "some answer", ledger="LEDGER-TEXT")
    assert len(captured) == 1
    # POLICY (2026-06-17): the ANSWER judge uses the variant that DROPS impact-form (people-count
    # framing is allowed in essays). It must embed the ANSWERS variant, not the resume/cover one.
    assert LEDGER_PROSE_BLOCK_RULES_ANSWERS in captured[0], \
        "_judge_answer must embed LEDGER_PROSE_BLOCK_RULES_ANSWERS (impact-form dropped)"
    assert "IMPACT FORM" not in captured[0], \
        "answer judge must NOT carry the impact-form (people-count) block rule"


def test_content_text_prompt_embeds_shared_constant():
    captured = []
    audit_content_text("some resume bullet text", "current_bullets.0",
                       gate_fn=None, llm=_capturing_llm(captured), ledger="LEDGER-TEXT")
    assert len(captured) == 1
    assert LEDGER_PROSE_BLOCK_RULES in captured[0], \
        "audit_content_text must embed the shared LEDGER_PROSE_BLOCK_RULES constant verbatim"


def test_prompts_differ_only_on_impact_form():
    # POLICY (2026-06-21): the impact-form (people/time-count) rule is RESUME-ONLY. The judges split
    # by DOC, not answer-vs-element: a RESUME element keeps impact-form (clean percentage bullets);
    # the ANSWER judge AND a COVER element (para.N) both DROP it (prose — a concrete people-count is
    # stronger). All three must share the rest verbatim, so no OTHER rule can drift between them.
    cap_ans, cap_cover, cap_resume = [], [], []
    _judge_answer(_capturing_llm(cap_ans), "Q", "A", ledger="L")
    audit_content_text("E", "para.0", gate_fn=None, llm=_capturing_llm(cap_cover), ledger="L")
    audit_content_text("E", "current_bullets.0", gate_fn=None, llm=_capturing_llm(cap_resume), ledger="L")
    # answer + cover: impact-form dropped
    assert LEDGER_PROSE_BLOCK_RULES_ANSWERS in cap_ans[0]
    assert LEDGER_PROSE_BLOCK_RULES_ANSWERS in cap_cover[0]
    assert "IMPACT FORM" not in cap_ans[0] and "IMPACT FORM" not in cap_cover[0]
    # resume element: impact-form kept
    assert LEDGER_PROSE_BLOCK_RULES in cap_resume[0]
    assert "IMPACT FORM" in cap_resume[0]
    # the shared prefix (fabrication rule) is identical in all three — no silent drift
    assert all("(a) FABRICATION" in c[0] for c in (cap_ans, cap_cover, cap_resume))


# ---------------------------------------------------------------------------
# 3. Severity-normalization path: a stub llm returning a canned BLOCK finding flips the verdict
#    to BLOCKED; a canned FLAG finding rides along on a PASS. No real call. This proves the
#    prompt-assembly -> parse -> severity-normalization wiring works end-to-end deterministically.
# ---------------------------------------------------------------------------

def _drafts(*answers):
    return [{"question": f"Q{i}", "answer": a, "kind": "essay"} for i, a in enumerate(answers)]


def test_canned_block_finding_flips_verdict_blocked():
    judged = json.dumps([{"offending_text": "ARIA, which I've run for two years",
                          "issue": "ledger forbids the timeline framing", "fix": "since early 2026",
                          "severity": "BLOCK"}])
    out = audit_answers(_drafts("ARIA, which I've run for two years"),
                        gate_fn=lambda t: [], llm=lambda p: judged, ledger="LEDGER")
    assert out["verdict"] == "BLOCKED"
    assert out["block_findings"] == 1
    assert out["findings"][0]["severity"] == "BLOCK"


def test_canned_flag_finding_rides_along_on_pass():
    judged = json.dumps([{"offending_text": "deeply passionate", "issue": "tone",
                          "fix": "tighten", "severity": "FLAG"}])
    out = audit_answers(_drafts("deeply passionate about the mission"),
                        gate_fn=lambda t: [], llm=lambda p: judged, ledger="LEDGER")
    assert out["verdict"] == "PASS"
    assert out["flag_findings"] == 1


def test_content_text_canned_block_normalizes_severity():
    # audit_content_text returns findings (not a verdict); a canned BLOCK must normalize to BLOCK.
    judged = json.dumps([{"offending_text": "fluent in Python", "issue": "ledger forbids coding fluency",
                          "fix": "frame as AI-native orchestration", "severity": "BLOCK"}])
    findings = audit_content_text("fluent in Python and MATLAB", "skills.0",
                                  gate_fn=None, llm=lambda p: judged, ledger="LEDGER")
    assert len(findings) == 1
    assert findings[0]["severity"] == "BLOCK"
    assert findings[0]["doc"] == "resume"
