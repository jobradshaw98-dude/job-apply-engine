"""TDD for the PURE scoring + reporting core of the apply-engine benchmark.

No browser, no live calls — outcomes are injected as plain dicts (the same shape
run_targets produces from a JobOutcome). The harness exists to catch two failures
that must NEVER happen in production:
  - the engine submitting an application (CRITICAL: SUBMITTED)
  - a "ready_to_submit" with zero filled fields (FALSE SUCCESS)
Both are treated as can-never-happen criticals regardless of what was expected.
"""
from apply_engine.bench import score_outcome, summarize, render_report


def _outcome(**kw):
    """A JobOutcome-as-dict with safe defaults; override per test."""
    base = {
        "job_id": "JOB-X", "ats": "greenhouse", "status": "ready_to_submit",
        "submitted": False, "verify_ok": True, "filled_fields": ["first_name"],
        "unfilled_required": [], "halt_reason": "", "error": "",
    }
    base.update(kw)
    return base


# ---- HARD INVARIANT: submitted is the worst possible failure ----

def test_submitted_is_critical_even_when_status_matches_expect():
    # status matches the expectation AND fields were filled, but it submitted ->
    # nothing else matters, this is the can-never-happen failure.
    r = score_outcome(_outcome(submitted=True, status="ready_to_submit"),
                      expect="ready_to_submit")
    assert r["ok"] is False
    assert r["verdict"] == "CRITICAL: SUBMITTED"


def test_submitted_is_critical_even_for_error_status():
    r = score_outcome(_outcome(submitted=True, status="error", error="boom"),
                      expect="error")
    assert r["ok"] is False
    assert r["verdict"] == "CRITICAL: SUBMITTED"


# ---- FALSE SUCCESS: ready_to_submit but nothing was filled ----

def test_ready_with_empty_filled_fields_is_false_success():
    r = score_outcome(_outcome(status="ready_to_submit", filled_fields=[]),
                      expect="ready_to_submit")
    assert r["ok"] is False
    assert r["verdict"] == "FALSE SUCCESS (0 fields filled)"


def test_submitted_takes_precedence_over_false_success():
    # both bad conditions present -> SUBMITTED wins (it's strictly worse).
    r = score_outcome(_outcome(status="ready_to_submit", filled_fields=[], submitted=True),
                      expect="ready_to_submit")
    assert r["verdict"] == "CRITICAL: SUBMITTED"


# ---- PASS / MISMATCH / ERROR ----

def test_status_matches_expect_is_pass():
    r = score_outcome(_outcome(status="needs_sam", filled_fields=["first_name"]),
                      expect="needs_sam")
    assert r["ok"] is True
    assert r["verdict"] == "PASS"


def test_needs_sam_with_no_fields_still_passes():
    # empty filled_fields only triggers FALSE SUCCESS for ready_to_submit; a
    # needs_sam halt legitimately has zero filled fields.
    r = score_outcome(_outcome(status="needs_sam", filled_fields=[]),
                      expect="needs_sam")
    assert r["ok"] is True
    assert r["verdict"] == "PASS"


def test_status_mismatch_is_mismatch_with_got_and_expected():
    r = score_outcome(_outcome(status="needs_input"), expect="ready_to_submit")
    assert r["ok"] is False
    assert r["verdict"] == "MISMATCH (got needs_input, expected ready_to_submit)"


def test_error_status_surfaces_the_error():
    r = score_outcome(_outcome(status="error", error="Timeout 30000ms exceeded"),
                      expect="ready_to_submit")
    assert r["ok"] is False
    assert r["verdict"] == "ERROR"
    assert any("Timeout 30000ms exceeded" in reason for reason in r["reasons"])


def test_error_status_when_error_was_expected_is_still_not_ok():
    # expecting an error is not a thing we reward — an errored run is never "ok".
    r = score_outcome(_outcome(status="error", error="boom"), expect="error")
    assert r["ok"] is False
    assert r["verdict"] == "ERROR"


def test_result_carries_job_id():
    r = score_outcome(_outcome(job_id="JOB-212"), expect="ready_to_submit")
    assert r["job_id"] == "JOB-212"


# ---- summarize ----

def test_summarize_counts_and_pass_rate():
    results = [
        score_outcome(_outcome(job_id="A", status="ready_to_submit"), "ready_to_submit"),
        score_outcome(_outcome(job_id="B", status="needs_sam"), "needs_sam"),
        score_outcome(_outcome(job_id="C", status="needs_input"), "ready_to_submit"),
    ]
    s = summarize(results)
    assert s["total"] == 3
    assert s["counts"]["PASS"] == 2
    assert s["counts"]["MISMATCH (got needs_input, expected ready_to_submit)"] == 1
    assert abs(s["pass_rate"] - (2 / 3)) < 1e-9


def test_summarize_collects_criticals():
    results = [
        score_outcome(_outcome(job_id="A", submitted=True), "ready_to_submit"),
        score_outcome(_outcome(job_id="B", status="ready_to_submit", filled_fields=[]),
                      "ready_to_submit"),
        score_outcome(_outcome(job_id="C", status="needs_sam"), "needs_sam"),
    ]
    s = summarize(results)
    crit_ids = {c["job_id"] for c in s["critical"]}
    assert crit_ids == {"A", "B"}


def test_summarize_empty_is_zero_pass_rate_not_crash():
    s = summarize([])
    assert s["total"] == 0
    assert s["pass_rate"] == 0.0
    assert s["critical"] == []


# ---- render_report ----

def test_render_report_has_a_row_per_result():
    results = [
        score_outcome(_outcome(job_id="JOB-1", ats="greenhouse"), "ready_to_submit"),
        score_outcome(_outcome(job_id="JOB-2", ats="lever", status="needs_sam"),
                      "needs_sam"),
    ]
    report = render_report(summarize(results), results)
    assert "JOB-1" in report
    assert "JOB-2" in report
    # ats appears in the row
    assert "greenhouse" in report
    assert "lever" in report


def test_render_report_flags_criticals_in_footer():
    results = [score_outcome(_outcome(job_id="JOB-BAD", submitted=True), "ready_to_submit")]
    report = render_report(summarize(results), results)
    assert "JOB-BAD" in report
    assert "CRITICAL" in report
