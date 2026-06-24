# -*- coding: utf-8 -*-
"""SLOW, opt-in behavioural test of the consolidated ledger prose rules against the REAL LLM judge.

Marked @pytest.mark.llm so it is NOT in the fast suite (it shells out to `claude -p`, ~minutes,
and is nondeterministic). The function names deliberately avoid the keywords
"refresh/audit/judge/draft/finish" so the fast `-k` subset never selects them.

WHAT THIS PROVES that the fast deterministic test cannot: that the WORDING of
LEDGER_PROSE_BLOCK_RULES actually steers a real model to the right verdict — both directions:
  * negative cases (a real violation of each prose rule) MUST come back BLOCK, and
  * positive cases (the correctly-rendered equivalent + a normal grounded bullet) MUST NOT.

NONDETERMINISM: an LLM judge is not deterministic. Each case is run 3x and we assert the
MAJORITY verdict (>=2/3) is correct, so a single off-sample run can't flake the suite.

Run explicitly:
  apply_engine/.venv/Scripts/python.exe -m pytest apply_engine/tests/test_ledger_prose_llm.py -m llm -q
"""
import shutil

import pytest

from apply_engine.refresh_audit import _judge_answer, audit_content_text, _ledger_text
from apply_engine.llm import make_claude_llm

pytestmark = pytest.mark.llm


def _have_claude() -> bool:
    return shutil.which("claude") is not None


# (question, answer, must_block) — each prose rule, both directions.
ANSWER_CASES = [
    # --- NEGATIVE: must BLOCK ---
    ("Tell us about ARIA.",
     "I've been running ARIA, my multi-agent platform, for two years now.", True),
    ("What was your thesis about?",
     "My MASc thesis was materials-science research on polymer material modeling and biomechanics "
     "of UHMWPE.", True),
    ("What makes you a fit?",
     "I bring a rare combination of physics engineering and AI skills that is uncommon in this field.",
     True),
    ("What languages do you use?",
     "I'm fluent in Python and very comfortable hand-writing MATLAB day to day.", True),
    ("Which tools did you use at Meridian?",
     "At Meridian I built all my R&D automation with Claude Code.", True),
    # --- POSITIVE: must NOT block (correctly-rendered equivalents + a normal grounded bullet) ---
    ("Tell us about ARIA.",
     "I've been running ARIA, my personal multi-agent platform on Claude Code, daily since early 2026.",
     False),
    ("Describe a Meridian win.",
     "My prototype-analysis agent drove roughly a 90% reduction in cross-team review effort.", False),
    # POLICY (2026-06-17): people-count framing is ALLOWED in ESSAY answers (concrete > vague).
    # The answer judge must NOT block it. (Resume/cover still blocks — see ELEMENT_CASES.)
    ("Describe a Meridian win.",
     "My prototype-analysis agent replaced a recurring review of 10+ engineers and a two-hour "
     "cross-team meeting.", False),
    ("What was your thesis about?",
     "My MASc thesis built an automated design-optimization framework — an ANSYS plus OptiSLang "
     "metamodel pipeline that searched surface-texture geometry against contact-mechanics FEA.", False),
    ("How do you build software?",
     "I orchestrate AI agents — Claude Code and Codex — to build and ship automation rather than "
     "hand-coding it myself.", False),
    ("Describe your Meridian role.",
     "As lead simulation analyst on the flagship woods line I drove a ~60% reduction in hands-on "
     "analysis effort with a test-analysis agent built in Codex.", False),
]

# Resume/cover element cases for the audit_content_text prompt (must_block).
ELEMENT_CASES = [
    ("current_bullets.0",
     "Built a root-cause agent that replaced a 10+ person, two-hour cross-team review.", True),
    ("para.0",
     "I have run ARIA, a production multi-agent platform, for over a year at platform scale.", True),
    ("current_bullets.0",
     "Drove a ~90% reduction in cross-team review effort with a prototype-analysis agent.", False),
    ("summary.0",
     "Lead simulation analyst bridging physics-based product engineering with AI-native systems "
     "building.", False),
]


def _majority_blocks(findings_runs) -> bool:
    """True iff a majority of runs produced at least one BLOCK-severity finding."""
    blocked = sum(1 for fs in findings_runs
                  if any((f.get("severity", "") or "").upper() == "BLOCK" for f in fs))
    return blocked >= 2  # >= 2 of 3


@pytest.mark.skipif(not _have_claude(), reason="claude CLI not on PATH; real-LLM test skipped")
@pytest.mark.parametrize("question,answer,must_block", ANSWER_CASES)
def test_answer_prose_rules_majority_verdict(question, answer, must_block):
    llm = make_claude_llm()
    ledger = _ledger_text()
    assert ledger, "ledger must be readable for the real-LLM test"
    runs = [_judge_answer(llm, question, answer, ledger) for _ in range(3)]
    blocked = _majority_blocks(runs)
    if must_block:
        assert blocked, f"expected BLOCK majority for {answer!r}; runs={runs}"
    else:
        assert not blocked, f"expected PASS majority (no BLOCK) for {answer!r}; runs={runs}"


@pytest.mark.skipif(not _have_claude(), reason="claude CLI not on PATH; real-LLM test skipped")
@pytest.mark.parametrize("element,text,must_block", ELEMENT_CASES)
def test_element_prose_rules_majority_verdict(element, text, must_block):
    llm = make_claude_llm()
    ledger = _ledger_text()
    assert ledger, "ledger must be readable for the real-LLM test"
    runs = [audit_content_text(text, element, gate_fn=None, llm=llm, ledger=ledger)
            for _ in range(3)]
    blocked = _majority_blocks(runs)
    if must_block:
        assert blocked, f"expected BLOCK majority for {text!r}; runs={runs}"
    else:
        assert not blocked, f"expected PASS majority (no BLOCK) for {text!r}; runs={runs}"
