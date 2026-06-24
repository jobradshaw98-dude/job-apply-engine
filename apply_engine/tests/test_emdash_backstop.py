# -*- coding: utf-8 -*-
"""FIX 2 — deterministic em-dash backstop in apply_engine.llm.make_audit_fn.

An ANSWER with more than two em-dashes is an AI tell and must be blocked by the answer gate.
This is the answer-path gate (drafting + dashboard regen + refresh_audit all route through
make_audit_fn). It must NOT change audit_gate.py's resume/cover rules — those are gated by
audit_file() directly, not this wrapper.

Proven here:
  1. Three or more em-dashes -> a block note mentioning the count + "AI tell".
  2. Exactly two em-dashes -> no em-dash block note (threshold is > 2).
  3. Zero em-dashes on a clean answer -> no blocks at all.
"""
from apply_engine.llm import make_audit_fn


def _dash_notes(notes):
    return [n for n in notes if "em-dash" in (n or "")]


def test_three_emdashes_blocks():
    gate = make_audit_fn()
    text = "I build things — I ship them — and I learn fast — every single day."
    notes = gate(text)
    dash = _dash_notes(notes)
    assert dash, f"expected an em-dash block note, got {notes!r}"
    assert "(3)" in dash[0]
    assert "AI tell" in dash[0]


def test_two_emdashes_does_not_block():
    gate = make_audit_fn()
    text = "I build things — I ship them, and I learn fast — every single day."
    assert _dash_notes(gate(text)) == []


def test_clean_answer_no_blocks():
    gate = make_audit_fn()
    text = "I build things, I ship them, and I learn fast every single day."
    assert gate(text) == []
