# -*- coding: utf-8 -*-
"""TDD for the CALIBRATION-ONLY recheck on the quality judge (BLOCK #2).

A resume/cover EDIT can introduce a calibration violation (wrong-domain pitch, CAD-lead,
coding-fluency, MobilityCo/Signal Intel, tool-misattribution, seniority). The full quality judge
must NOT re-run on edits (that is the advisory "treadmill" we deliberately killed — see
feedback_apply_quality_once_and_calibration). So content edits run ONLY the calibration
sub-check: it recomputes the `calibration` array + its FAIL contribution while leaving the four
POLISH dimension scores (jd_coverage/fit/specificity/voice) BYTE-FROZEN from the staging pass.

These tests drive a CALL-TRACKING fake llm (returns canned JSON, never raises — a raising stub
gets swallowed by a fail-closed except and passes vacuously, the trap that bit us before).
"""
import json

from apply_engine.quality_judge import recheck_calibration


_RESUME = {"headline": "AI engineer", "summary": "shipped five agents",
           "current_bullets": ["cut analysis time 60% with simulation"], "skills": ["LS-DYNA"]}
_COVER = {"salutation": "Dear Hiring Manager,", "paragraphs": ["I'm applying...", "I built..."]}
_ANSWERS = [{"q": "Why us?", "value": "Because I build agents.", "status": "answered"}]
_JD = {"id": "JOB-210", "title": "Applied AI Engineer",
       "jd_text": "Build enterprise applied-AI agents for B2B SaaS customers."}

# A stored quality_audit from the ONE staging pass: four polish dims (one is a 3 -> the staging
# verdict was FLAG). The recheck must preserve these scores EXACTLY.
_PRIOR_QUALITY = {
    "verdict": "FLAG",
    "dimensions": {
        "jd_coverage": {"score": 5, "note": "covers the asks", "fix": ""},
        "fit": {"score": 3, "note": "could be sharper", "fix": "name the team"},
        "specificity": {"score": 4, "note": "concrete", "fix": ""},
        "voice": {"score": 4, "note": "authentic", "fix": ""},
    },
    "calibration": [],
    "judge_ran": True,
    "summary": "quality review: FLAG",
    "refreshed_at": "2026-06-10T09:00:00-07:00",
}


class _Tracker:
    """Call-tracking fake llm: returns canned JSON, records calls, NEVER raises."""
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        return self.payload


def _viol(vtype, where="resume", evidence="x", fix="y"):
    return {"type": vtype, "where": where, "evidence": evidence, "fix": fix}


def _cal_payload(violations):
    """The calibration-only recheck returns ONLY a calibration array (no dimension scores)."""
    return json.dumps({"calibration": list(violations)})


# ---- 1. an introduced violation -> FAIL, polish dims preserved ----

def test_introduced_violation_fails_and_freezes_polish_dims():
    llm = _Tracker(_cal_payload([_viol("coding_fluency", where="resume",
                                       evidence="proficient in Python")]))
    out = recheck_calibration(_JD, _RESUME, _COVER, _ANSWERS, _PRIOR_QUALITY, llm=llm)
    assert llm.calls, "the calibration recheck must actually call the llm"
    assert out["verdict"] == "FAIL"
    assert out["calibration"] and out["calibration"][0]["type"] == "coding_fluency"
    # POLISH DIMS FROZEN: byte-identical to the staging pass (the treadmill never re-scores them).
    assert out["dimensions"] == _PRIOR_QUALITY["dimensions"]
    assert out["judge_ran"] is True


# ---- 2. a CLEAN edit -> calibration empty -> verdict falls BACK to the frozen-dim verdict ----

def test_clean_edit_returns_to_frozen_dim_verdict():
    # No violation. The four dims carry a single 3 -> the frozen verdict is FLAG (NOT re-judged,
    # just recomputed from the preserved scores). No wedge, no treadmill.
    llm = _Tracker(_cal_payload([]))
    out = recheck_calibration(_JD, _RESUME, _COVER, _ANSWERS, _PRIOR_QUALITY, llm=llm)
    assert out["calibration"] == []
    assert out["verdict"] == "FLAG"            # follows the frozen dims (one 3), not FAIL
    assert out["dimensions"] == _PRIOR_QUALITY["dimensions"]


def test_clean_edit_all_fives_prior_returns_pass():
    prior = dict(_PRIOR_QUALITY)
    prior["dimensions"] = {n: {"score": 5, "note": "", "fix": ""}
                           for n in ("jd_coverage", "fit", "specificity", "voice")}
    prior["verdict"] = "PASS"
    llm = _Tracker(_cal_payload([]))
    out = recheck_calibration(_JD, _RESUME, _COVER, _ANSWERS, prior, llm=llm)
    assert out["calibration"] == []
    assert out["verdict"] == "PASS"


# ---- 3. calibration FAIL dominates even all-5 frozen dims ----

def test_calibration_fail_overrides_strong_frozen_dims():
    prior = dict(_PRIOR_QUALITY)
    prior["dimensions"] = {n: {"score": 5, "note": "", "fix": ""}
                           for n in ("jd_coverage", "fit", "specificity", "voice")}
    prior["verdict"] = "PASS"
    llm = _Tracker(_cal_payload([_viol("wrong_tool_attribution", evidence="ANSYS at Meridian")]))
    out = recheck_calibration(_JD, _RESUME, prior_quality=prior, cover=_COVER,
                              answers=_ANSWERS, llm=llm)
    assert out["verdict"] == "FAIL"
    assert out["dimensions"] == prior["dimensions"]


# ---- 4. degraded recheck (llm unavailable) does NOT wedge: it preserves the prior verdict ----

def test_degraded_recheck_preserves_prior_quality():
    # If the recheck llm can't run, we must NOT invent a FAIL (that would wedge a good submit) and
    # must NOT clear the staging verdict. Returns the prior quality_audit essentially intact, with
    # judge_ran preserved (it is NOT a degradation of the staging judge — only the calibration
    # recheck couldn't run; the prior pass still stands).
    def _raising(*a, **k):
        raise RuntimeError("claude CLI down")
    out = recheck_calibration(_JD, _RESUME, _COVER, _ANSWERS, _PRIOR_QUALITY, llm=_raising)
    assert out["dimensions"] == _PRIOR_QUALITY["dimensions"]
    # The prior verdict (FLAG) is preserved; the recheck did not manufacture a FAIL.
    assert out["verdict"] == "FLAG"
    assert out["calibration"] == _PRIOR_QUALITY["calibration"]


# ---- 5. GUARD: the fake llm is a call-tracker, never a raising stub ----

def test_recheck_fake_llm_is_call_tracker():
    llm = _Tracker(_cal_payload([_viol("coding_fluency", evidence="expert MATLAB")]))
    out = recheck_calibration(_JD, _RESUME, _COVER, _ANSWERS, _PRIOR_QUALITY, llm=llm)
    assert len(llm.calls) >= 1
    assert out["verdict"] == "FAIL"


# ---- 6. no em-dash in the FAIL summary (the user's hard ban on new user-facing strings) ----

def test_recheck_summary_has_no_emdash():
    llm = _Tracker(_cal_payload([_viol("leads_with_cad", evidence="Expert SolidWorks modeler")]))
    out = recheck_calibration(_JD, _RESUME, _COVER, _ANSWERS, _PRIOR_QUALITY, llm=llm)
    assert "—" not in out["summary"]
    assert "--" not in out["summary"]
