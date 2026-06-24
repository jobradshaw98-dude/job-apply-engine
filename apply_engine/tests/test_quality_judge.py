# -*- coding: utf-8 -*-
"""TDD for the HOLISTIC QUALITY JUDGE (Stage-3) — the second mandatory gate.

The judge scores a tailored package (resume + cover + answers) against the JD on four 1-5
dimensions and maps them to PASS/FLAG/FAIL. These tests use a FAKE llm (a plain callable that
returns canned JSON) — never a real claude -p call — so the verdict mapping, strict-JSON parsing,
retry/raise behaviour, and the can_submit second-gate wiring are all exercised deterministically.
"""
import json

import pytest

from apply_engine.quality_judge import (judge_quality, degraded_quality_audit,
                                         _verdict_for, _FAIL_FLOOR, _FLAG_CEILING)


# ---- fixtures ----

_JOB = {"id": "JOB-1", "jd_text": "Build production agents. FEA a plus.", "title": "Engineer"}
_RESUME = {"headline": "AI engineer", "summary": "shipped five agents",
           "current_bullets": ["cut analysis time 60%"], "skills": ["Python", "LS-DYNA"]}
_COVER = {"salutation": "Dear Hiring Manager,", "paragraphs": ["I'm applying...", "I built..."]}
_ANSWERS = [{"q": "Why us?", "value": "Because I build agents.", "status": "answered"}]


def _scores(jd=5, fit=5, spec=5, voice=5, summary="overall"):
    """A fake-llm payload: strict JSON with the four dimensions + summary."""
    return json.dumps({
        "jd_coverage": {"score": jd, "note": "jd note"},
        "fit": {"score": fit, "note": "fit note"},
        "specificity": {"score": spec, "note": "spec note"},
        "voice": {"score": voice, "note": "voice note"},
        "summary": summary,
    })


def _fake_llm(payload):
    return lambda prompt: payload


# ---- happy path ----

def test_all_fives_pass():
    out = judge_quality(_JOB, _RESUME, _COVER, _ANSWERS, llm=_fake_llm(_scores()))
    assert out["verdict"] == "PASS"
    assert out["judge_ran"] is True
    assert set(out["dimensions"]) == {"jd_coverage", "fit", "specificity", "voice"}
    for d in out["dimensions"].values():
        assert d["score"] == 5 and d["note"]
    assert out["summary"] == "overall"
    assert "refreshed_at" in out


def test_all_fours_pass():
    # the PASS floor is >= 4 on every dimension
    out = judge_quality(_JOB, _RESUME, _COVER, _ANSWERS, llm=_fake_llm(_scores(4, 4, 4, 4)))
    assert out["verdict"] == "PASS"


# ---- FAIL: hard-floor breach on jd_coverage or specificity ----

def test_jd_coverage_two_fails():
    out = judge_quality(_JOB, _RESUME, _COVER, _ANSWERS, llm=_fake_llm(_scores(jd=2)))
    assert out["verdict"] == "FAIL"


def test_specificity_two_fails():
    out = judge_quality(_JOB, _RESUME, _COVER, _ANSWERS, llm=_fake_llm(_scores(spec=2)))
    assert out["verdict"] == "FAIL"


def test_jd_coverage_one_fails():
    out = judge_quality(_JOB, _RESUME, _COVER, _ANSWERS, llm=_fake_llm(_scores(jd=1)))
    assert out["verdict"] == "FAIL"


def test_fail_beats_flag_when_both_present():
    # specificity=2 (FAIL floor) AND voice=3 (FLAG ceiling) -> FAIL wins (the hard floor dominates)
    out = judge_quality(_JOB, _RESUME, _COVER, _ANSWERS, llm=_fake_llm(_scores(spec=2, voice=3)))
    assert out["verdict"] == "FAIL"


# ---- FLAG: any dimension <= 3 with no hard-floor breach ----

def test_single_three_flags():
    # fit=3, others 4-5 -> FLAG (advisory, not a block). fit/voice <=3 is FLAG, not FAIL.
    out = judge_quality(_JOB, _RESUME, _COVER, _ANSWERS, llm=_fake_llm(_scores(fit=3)))
    assert out["verdict"] == "FLAG"


def test_voice_three_flags():
    out = judge_quality(_JOB, _RESUME, _COVER, _ANSWERS, llm=_fake_llm(_scores(voice=3)))
    assert out["verdict"] == "FLAG"


def test_jd_coverage_three_flags_not_fail():
    # 3 is above the FAIL floor (2) but at/below the FLAG ceiling (3) -> FLAG, not FAIL.
    out = judge_quality(_JOB, _RESUME, _COVER, _ANSWERS, llm=_fake_llm(_scores(jd=3)))
    assert out["verdict"] == "FLAG"


# ---- pure verdict mapping (constants documented) ----

def test_verdict_for_constants():
    assert _FAIL_FLOOR == 2 and _FLAG_CEILING == 3
    mk = lambda jd, fit, spec, voice: {
        "jd_coverage": {"score": jd}, "fit": {"score": fit},
        "specificity": {"score": spec}, "voice": {"score": voice}}
    assert _verdict_for(mk(5, 5, 5, 5)) == "PASS"
    assert _verdict_for(mk(4, 4, 4, 4)) == "PASS"
    assert _verdict_for(mk(3, 5, 5, 5)) == "FLAG"
    assert _verdict_for(mk(5, 3, 5, 5)) == "FLAG"
    assert _verdict_for(mk(2, 5, 5, 5)) == "FAIL"
    assert _verdict_for(mk(5, 5, 2, 5)) == "FAIL"
    # fit/voice at the FAIL floor is NOT a hard fail (only jd_coverage/specificity are)
    assert _verdict_for(mk(5, 2, 5, 5)) == "FLAG"
    assert _verdict_for(mk(5, 5, 5, 2)) == "FLAG"


# ---- score coercion / clamping ----

def test_scores_clamped_and_coerced():
    payload = json.dumps({
        "jd_coverage": {"score": 7, "note": "x"},     # clamps to 5
        "fit": {"score": "4", "note": "x"},           # string -> 4
        "specificity": {"score": 4.6, "note": "x"},   # rounds to 5
        "voice": {"score": 4, "note": "x"},
        "summary": "ok"})
    out = judge_quality(_JOB, _RESUME, _COVER, _ANSWERS, llm=_fake_llm(payload))
    assert out["dimensions"]["jd_coverage"]["score"] == 5
    assert out["dimensions"]["fit"]["score"] == 4
    assert out["dimensions"]["specificity"]["score"] == 5
    assert out["verdict"] == "PASS"


def test_garbled_score_fails_closed_to_one():
    # a non-numeric score on a hard-floor dim -> coerced to 1 (worst) -> FAIL (fails closed)
    payload = json.dumps({
        "jd_coverage": {"score": "n/a", "note": "x"},
        "fit": {"score": 5, "note": "x"}, "specificity": {"score": 5, "note": "x"},
        "voice": {"score": 5, "note": "x"}, "summary": "ok"})
    out = judge_quality(_JOB, _RESUME, _COVER, _ANSWERS, llm=_fake_llm(payload))
    assert out["dimensions"]["jd_coverage"]["score"] == 1
    assert out["verdict"] == "FAIL"


# ---- strict-JSON parse: one retry, then raise ----

def test_malformed_json_retries_then_raises():
    calls = {"n": 0}

    def flaky(prompt):
        calls["n"] += 1
        return "this is not json"  # never parseable on either attempt

    with pytest.raises(ValueError):
        judge_quality(_JOB, _RESUME, _COVER, _ANSWERS, llm=flaky)
    assert calls["n"] == 2  # initial attempt + exactly one retry


def test_malformed_then_good_succeeds_on_retry():
    calls = {"n": 0}

    def flaky(prompt):
        calls["n"] += 1
        return "garbage" if calls["n"] == 1 else _scores()

    out = judge_quality(_JOB, _RESUME, _COVER, _ANSWERS, llm=flaky)
    assert out["verdict"] == "PASS"
    assert calls["n"] == 2


def test_missing_dimension_is_a_parse_failure():
    # a JSON object missing a required dimension -> parse failure -> retry -> raise
    payload = json.dumps({"jd_coverage": {"score": 5}, "fit": {"score": 5},
                          "specificity": {"score": 5}})  # no "voice"
    with pytest.raises(ValueError):
        judge_quality(_JOB, _RESUME, _COVER, _ANSWERS, llm=_fake_llm(payload))


def test_llm_call_failure_propagates():
    # an LLM-CALL failure (not a parse failure) surfaces out of judge_quality so the orchestration
    # layer (refresh) can catch it and stamp judge_ran False. Documented: refresh degrades on this.
    def boom(prompt):
        raise RuntimeError("claude -p died")

    with pytest.raises(RuntimeError):
        judge_quality(_JOB, _RESUME, _COVER, _ANSWERS, llm=boom)


# ---- degraded stamp ----

def test_degraded_quality_audit_fails_closed_on_verdict_too():
    # Defense in depth (2026-06-11): the degraded stamp must read as un-submittable on the
    # verdict VALUE itself, not only via judge_ran, so a consumer that checks the verdict
    # without judge_ran also fails closed.
    d = degraded_quality_audit("CLI missing")
    assert d["judge_ran"] is False
    assert d["verdict"] == "FAIL"
    assert "did NOT run" in d["summary"]
    assert set(d["dimensions"]) == {"jd_coverage", "fit", "specificity", "voice"}


# ======================================================================================
# can_submit: the quality gate was DEMOTED (2026-06-22). quality_audit is advisory/on-demand
# now and NO LONGER gates can_submit — only the DETERMINISTIC gate (audit.gate_blocks > 0) does.
# judge_quality's OWN scoring logic (above) is UNCHANGED; only its use as a submit gate is removed.
# ======================================================================================

from apply_engine.finish import can_submit  # noqa: E402


def _ready(**over):
    """A record that clears the DETERMINISTIC gate (no gate_blocks); override quality_audit per test."""
    rec = {
        "job_id": "JOB-1", "status": "ready_to_submit", "submitted": False,
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "work_auth": [{"field": "sponsor", "answer": "No"}],
        "custom_qs": [], "unfilled_required": [], "needs_sam": [],
        "audit": {"verdict": "PASS", "judge_ran": True, "gate_blocks": 0, "findings": []},
        "quality_audit": {"verdict": "PASS", "judge_ran": True, "dimensions": {}},
    }
    rec.update(over)
    return rec


def test_can_submit_quality_pass_allows():
    ok, reason = can_submit(_ready())
    assert ok is True, reason


def test_can_submit_quality_flag_allows():
    # FLAG is advisory — it does NOT block submit (unchanged).
    ok, reason = can_submit(_ready(quality_audit={"verdict": "FLAG", "judge_ran": True,
                                                  "summary": "weak voice", "dimensions": {}}))
    assert ok is True, reason


def test_can_submit_quality_fail_no_longer_blocks():
    # FLIPPED: a FAIL quality verdict no longer blocks submit (clean deterministic gate). The
    # quality judge is advisory/on-demand now; the user's review is the quality gate.
    ok, reason = can_submit(_ready(quality_audit={"verdict": "FAIL", "judge_ran": True,
                                                  "summary": "doesn't cover the JD",
                                                  "dimensions": {}}))
    assert ok is True, reason


def test_can_submit_quality_missing_no_longer_blocks():
    # FLIPPED: a record with NO quality_audit now submits (the quality judge is on-demand).
    rec = _ready()
    del rec["quality_audit"]
    ok, reason = can_submit(rec)
    assert ok is True, reason


def test_can_submit_quality_degraded_judge_no_longer_blocks():
    # FLIPPED: quality judge_ran False (degraded LLM judge) is advisory now -> submits.
    ok, reason = can_submit(_ready(quality_audit={"verdict": "FLAG", "judge_ran": False,
                                                  "summary": "judge unavailable", "dimensions": {}}))
    assert ok is True, reason


def test_can_submit_real_degraded_stamp_no_longer_blocks():
    # FLIPPED: the REAL degraded quality stamp (verdict FAIL, judge_ran False) no longer blocks —
    # the quality judge is not a submit gate anymore.
    ok, reason = can_submit(_ready(quality_audit=degraded_quality_audit("CLI missing")))
    assert ok is True, reason


def test_can_submit_deterministic_gate_block_still_blocks():
    # SAFETY (kept): the DETERMINISTIC gate is the one content gate that still hard-blocks. A
    # gate_blocks>0 record is refused regardless of the (advisory) quality verdict.
    rec = _ready(audit={"verdict": "PASS", "judge_ran": True, "gate_blocks": 1, "findings": []},
                 quality_audit={"verdict": "FAIL", "judge_ran": True, "dimensions": {}})
    ok, reason = can_submit(rec)
    assert ok is False
    assert "deterministic gate" in reason.lower()
