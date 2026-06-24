# -*- coding: utf-8 -*-
"""Phase 4b — G2 form-constraint compliance (the CHECK half) + the live G1/G2 finish-hook bodies.

Two layers:
  1. `compliance.check_form_constraints` — pure, offline: a staged answer vs a captured FormSpec's
     stated length limits (word range / word min/max / char cap). Under-length / over-length FAIL.
  2. The finish-hook bodies `_g1_reconcile_ok` / `_g2_compliance_ok` now reading the Phase-4b
     `reconcile` / `compliance` (or `form_spec`-derived) data — the contract the convergence loop
     gates on. (test_verify_ready already pins the pass-when-absent + wiring contracts; these pin the
     ACTUAL length-logic + the list-driven G1 verdict.)

All deterministic — no browser, no LLM, no network.
"""
from apply_engine.form_spec import FormSpec, FieldSpec
from apply_engine.compliance import (check_form_constraints, count_words,
                                      check_record_compliance, form_spec_from_summary)
from apply_engine.finish import _g1_reconcile_ok, _g2_compliance_ok


def _spec(*fields) -> FormSpec:
    s = FormSpec(ats="greenhouse")
    s.fields = list(fields)
    return s


def _essay(key, label, constraints):
    return FieldSpec(key=key, label=label, required=True, widget_kind="textarea",
                     constraints=constraints)


# ---------------------------------------------------------------------------
# check_form_constraints — the pure length logic
# ---------------------------------------------------------------------------

def test_essay_under_word_range_is_violation():
    spec = _spec(_essay("why", "Why do you want to work here?", {"words": [200, 400]}))
    rec = {"custom_qs": [{"q": "Why do you want to work here?", "kind": "essay",
                          "value": "short answer " * 25}]}  # 50 words, under 200
    res = check_form_constraints(spec, rec)
    assert res.ok is False
    assert any(v.kind == "words_under" for v in res.violations)
    assert "under" in str(res.violations[0]).lower()


def test_essay_over_word_range_is_violation():
    spec = _spec(_essay("why", "Why do you want to work here?", {"words": [200, 400]}))
    rec = {"custom_qs": [{"q": "Why do you want to work here?", "kind": "essay",
                          "value": "word " * 500}]}  # 500 words, over 400
    res = check_form_constraints(spec, rec)
    assert res.ok is False
    assert any(v.kind == "words_over" for v in res.violations)


def test_essay_within_word_range_is_clean():
    spec = _spec(_essay("why", "Why do you want to work here?", {"words": [200, 400]}))
    rec = {"custom_qs": [{"q": "Why do you want to work here?", "kind": "essay",
                          "value": "word " * 300}]}  # 300 words, in range
    res = check_form_constraints(spec, rec)
    assert res.ok is True
    assert res.violations == []


def test_chars_max_overflow_is_violation():
    spec = _spec(_essay("p", "Describe a project", {"chars_max": 500}))
    rec = {"custom_qs": [{"q": "Describe a project", "kind": "essay", "value": "x" * 600}]}
    res = check_form_constraints(spec, rec)
    assert res.ok is False
    assert any(v.kind == "chars_over" for v in res.violations)


def test_words_min_and_max_standalone():
    spec = _spec(_essay("a", "Min field", {"words_min": 150}),
                 _essay("b", "Max field", {"words_max": 100}))
    rec = {"custom_qs": [
        {"q": "Min field", "kind": "essay", "value": "word " * 100},   # 100 < 150 -> under
        {"q": "Max field", "kind": "essay", "value": "word " * 200},   # 200 > 100 -> over
    ]}
    res = check_form_constraints(spec, rec)
    assert res.ok is False
    kinds = {v.kind for v in res.violations}
    assert kinds == {"words_min", "words_max"}


def test_unconstrained_field_never_violates():
    spec = _spec(_essay("open", "Anything else?", {}))
    rec = {"custom_qs": [{"q": "Anything else?", "kind": "essay", "value": "x " * 5000}]}
    res = check_form_constraints(spec, rec)
    assert res.ok is True


def test_select_answer_skipped_even_if_constraint_present():
    """An option-pick (select) answer can't breach a word/char limit — it's skipped, never a false
    violation, even if the field somehow carried a constraint."""
    spec = _spec(FieldSpec(key="exp", label="Years experience?", required=True,
                           widget_kind="native_select", constraints={"words_max": 1}))
    rec = {"custom_qs": [{"q": "Years experience?", "kind": "select", "value": "5+ years more text"}]}
    res = check_form_constraints(spec, rec)
    assert res.ok is True


def test_answer_with_no_live_field_is_not_a_length_violation():
    """G2 only judges length against a field that EXISTS — an orphan staged answer is G1's concern."""
    spec = _spec(_essay("why", "Why?", {"words": [200, 400]}))
    rec = {"custom_qs": [{"q": "Totally unrelated question", "kind": "essay", "value": "tiny"}]}
    res = check_form_constraints(spec, rec)
    assert res.ok is True


def test_count_words():
    assert count_words("one two three") == 3
    assert count_words("   ") == 0
    assert count_words("") == 0


# ---------------------------------------------------------------------------
# form_spec_from_summary + check_record_compliance — the record round-trip
# ---------------------------------------------------------------------------

def test_record_compliance_from_form_spec_summary():
    """A record carrying ONLY a captured form_spec summary (no compliance block) is still gated:
    check_record_compliance rebuilds the spec and finds the under-length essay."""
    spec = _spec(_essay("why", "Why do you want to work here?", {"words": [200, 400]}))
    rec = {"form_spec": spec.to_summary(),
           "custom_qs": [{"q": "Why do you want to work here?", "kind": "essay",
                          "value": "word " * 50}]}
    res = check_record_compliance(rec)
    assert res is not None
    assert res.ok is False


def test_record_compliance_none_without_form_spec():
    assert check_record_compliance({}) is None
    assert check_record_compliance({"form_spec": {"fields": []}}) is None


def test_form_spec_from_summary_roundtrips_constraints():
    spec = _spec(_essay("why", "Why?", {"words": [200, 400]}))
    rebuilt = form_spec_from_summary(spec.to_summary())
    assert rebuilt.by_key("why").constraints == {"words": [200, 400]}


# ---------------------------------------------------------------------------
# finish._g2_compliance_ok — the live gate body (beyond pass-when-absent)
# ---------------------------------------------------------------------------

def test_g2_fail_from_stored_violation():
    ok, reason = _g2_compliance_ok({"compliance": {"ok": False,
                                                   "violations": ["'Why?': 150 words, under 200"]}})
    assert ok is False
    assert "g2" in reason.lower()
    assert "200" in reason


def test_g2_recomputes_from_form_spec_when_no_compliance_block():
    """No `compliance` block but a captured form_spec with an under-length essay -> G2 FAILS by
    recomputing (the single-source-of-truth path)."""
    spec = _spec(_essay("why", "Why do you want to work here?", {"words": [200, 400]}))
    rec = {"form_spec": spec.to_summary(),
           "custom_qs": [{"q": "Why do you want to work here?", "kind": "essay",
                          "value": "word " * 30}]}
    ok, reason = _g2_compliance_ok(rec)
    assert ok is False
    assert "g2" in reason.lower()


def test_g2_pass_from_form_spec_when_compliant():
    spec = _spec(_essay("why", "Why do you want to work here?", {"words": [200, 400]}))
    rec = {"form_spec": spec.to_summary(),
           "custom_qs": [{"q": "Why do you want to work here?", "kind": "essay",
                          "value": "word " * 300}]}
    assert _g2_compliance_ok(rec)[0] is True


def test_g2_pass_when_no_compliance_and_no_form_spec():
    assert _g2_compliance_ok({})[0] is True
    assert _g2_compliance_ok({"custom_qs": [{"q": "x", "value": "y"}]})[0] is True


# ---------------------------------------------------------------------------
# finish._g1_reconcile_ok — list-driven verdict (beyond clean-bool stub)
# ---------------------------------------------------------------------------

def test_g1_fail_on_mismatched_list():
    rec = {"reconcile": {"clean": False,
                         "mismatches": [{"live_label": "Current employer",
                                         "staged_value": "long narrative ..."}]}}
    ok, reason = _g1_reconcile_ok(rec)
    assert ok is False
    assert "mis-mapped" in reason.lower() or "reconcil" in reason.lower()


def test_g1_fail_on_unfilled_required_live():
    rec = {"reconcile": {"clean": False,
                         "unfilled_required_live": [{"live_label": "Current title"}]}}
    ok, reason = _g1_reconcile_ok(rec)
    assert ok is False
    assert "required" in reason.lower() or "reconcil" in reason.lower()


def test_g1_pass_on_structural_missing_only():
    """A structural missing_live_field (cover with no cover field) does NOT fail G1 — clean stays
    True and there are no mismatched/unfilled entries."""
    rec = {"reconcile": {"clean": True, "n_missing_live_field": 1,
                         "missing_live_field": [{"staged_value": "cover", "structural": True}],
                         "mismatches": [], "unfilled_required_live": []}}
    assert _g1_reconcile_ok(rec)[0] is True


def test_g1_pass_when_absent():
    assert _g1_reconcile_ok({})[0] is True
    assert _g1_reconcile_ok({"reconcile": None})[0] is True
