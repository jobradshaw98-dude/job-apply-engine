# -*- coding: utf-8 -*-
"""Unit tests for apply_engine.iterate_fix — the feedback-threading + residual-classification helpers
that drive the engine-own iterate-to-clean loop. Pure functions; no claude -p, no network."""
from apply_engine import iterate_fix


def test_feedback_clause_names_previous_attempt_and_complaint():
    clause = iterate_fix.feedback_clause(
        prev_attempt_text="Reduced wear by 30 percent.",
        blocks=["fabricated wear claim"],
        findings=[{"offending_text": "30 percent", "issue": "metric not in ledger",
                   "fix": "remove the number"}],
        ledger_facts="Sam ran FEA contact-stress optimization.",
    )
    assert "PREVIOUS ATTEMPT WAS REJECTED" in clause
    assert "Reduced wear by 30 percent." in clause          # names the prior attempt
    assert "fabricated wear claim" in clause                # the gate complaint
    assert "30 percent" in clause and "metric not in ledger" in clause  # the ledger finding
    assert "FEA contact-stress optimization" in clause      # re-grounding facts
    # Converge by REMOVAL — never invent support.
    assert "REMOVE or REWORD" in clause


def test_feedback_clause_empty_when_nothing_specific():
    assert iterate_fix.feedback_clause("", [], [], ledger_facts="") == ""


def test_length_feedback_clause_under_says_lengthen_with_supported_detail():
    clause = iterate_fix.length_feedback_clause(
        prev_attempt_text="A short answer.", current_words=140,
        min_words=200, max_words=400,
        ledger_facts="Sam ships AI-native systems.")
    assert "WRONG LENGTH" in clause
    assert "140 words" in clause
    assert "at least 200" in clause and "at most 400" in clause
    assert "LENGTHEN" in clause
    assert "adding SPECIFIC, SUPPORTED detail" in clause
    assert "Do NOT pad" in clause and "invent" in clause
    assert "AI-native systems" in clause  # re-grounding facts


def test_length_feedback_clause_over_says_tighten():
    clause = iterate_fix.length_feedback_clause(
        prev_attempt_text="A very long answer.", current_words=600,
        min_words=200, max_words=400)
    assert "600 words" in clause
    assert "at most 400" in clause
    assert "TIGHTEN" in clause and "cut redundancy" in clause


def test_length_feedback_clause_empty_when_in_range_or_no_text():
    # in range -> nothing to thread
    assert iterate_fix.length_feedback_clause("text", 300, 200, 400) == ""
    # no previous text -> nothing to thread
    assert iterate_fix.length_feedback_clause("", 10, 200, 400) == ""


def test_summarize_blocks_merges_gate_and_findings():
    s = iterate_fix.summarize_blocks(
        ["too many em-dashes"],
        [{"offending_text": "ANSYS at Meridian", "issue": "wrong tool attribution", "fix": "use LS-DYNA"}],
    )
    assert "too many em-dashes" in s
    assert "ANSYS at Meridian" in s and "wrong tool attribution" in s


def test_classify_residual_heuristic_human_only():
    f = {"offending_text": "led a team of 12", "issue": "only Sam can confirm this", "fix": ""}
    assert iterate_fix.classify_residual(f, ledger_facts="", llm=None) == iterate_fix.HUMAN_ONLY


def test_classify_residual_heuristic_unsupportable_default():
    f = {"offending_text": "invented metric", "issue": "not in ledger", "fix": "remove it"}
    assert iterate_fix.classify_residual(f, ledger_facts="", llm=None) == iterate_fix.UNSUPPORTABLE


def test_classify_residual_uses_llm_when_provided():
    def _llm(prompt):
        assert "VETTED CLAIMS LEDGER" in prompt
        return '{"class":"human_only","why":"a fact only Sam has"}'
    f = {"offending_text": "x", "issue": "ambiguous", "fix": ""}
    assert iterate_fix.classify_residual(f, ledger_facts="L", llm=_llm) == iterate_fix.HUMAN_ONLY


def test_classify_residual_llm_garbage_falls_back_to_heuristic():
    def _llm(prompt):
        return "not json at all"
    # heuristic default for a non-human finding is unsupportable.
    f = {"offending_text": "x", "issue": "not in ledger", "fix": ""}
    assert iterate_fix.classify_residual(f, ledger_facts="L", llm=_llm) == iterate_fix.UNSUPPORTABLE
