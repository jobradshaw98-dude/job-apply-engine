# -*- coding: utf-8 -*-
"""TDD for the GROUNDED CALIBRATION GATE on the holistic quality judge (2026-06-10).

The fabrication gate catches FALSE claims. The four quality dimensions catch GENERIC / weak
packages. Neither catches MIS-TARGETING: true content aimed at the wrong audience. The exact
miss that shipped (JOB-210) was a resume pitching "healthcare and life sciences" domain fit for
an Enterprise/Applied-AI role — every claim TRUE, fabrication blind, and the advisory "fit" score
read a confidently-wrong-domain pitch as fine.

The calibration gate fixes that with DISCRETE, GROUNDED rules (not a fuzzy score): a non-empty
`calibration` violation list on the quality_audit forces a HARD FAIL (un-submittable), surfaced at
staging. These tests drive a FAKE llm (a CALL-TRACKING callable returning canned JSON) — never a
raising stub, which the judge's fail-closed except would swallow into a vacuous pass (the trap that
bit us before).
"""
import json


from apply_engine.quality_judge import judge_quality


# ---- fixtures: a clean package + JDs of different domains ----

_RESUME = {"headline": "AI engineer", "summary": "shipped five agents",
           "current_bullets": ["cut analysis time 60% with simulation"], "skills": ["LS-DYNA"]}
_COVER = {"salutation": "Dear Hiring Manager,", "paragraphs": ["I'm applying...", "I built..."]}
_ANSWERS = [{"q": "Why us?", "value": "Because I build agents.", "status": "answered"}]

_JD_ENTERPRISE = {"id": "JOB-210", "title": "Applied AI Engineer",
                  "jd_text": "Build enterprise applied-AI agents for B2B SaaS customers."}
_JD_LIFESCI = {"id": "JOB-LS", "title": "R&D Engineer, Medical Devices",
               "jd_text": "Design Class II medical devices; healthcare/life-sciences R&D."}


def _clean_scores(jd=5, fit=5, spec=5, voice=5):
    """The four-dimension block of the judge payload."""
    return {
        "jd_coverage": {"score": jd, "note": "jd note", "fix": ""},
        "fit": {"score": fit, "note": "fit note", "fix": ""},
        "specificity": {"score": spec, "note": "spec note", "fix": ""},
        "voice": {"score": voice, "note": "voice note", "fix": ""},
        "summary": "overall",
    }


def _payload(*, scores=None, calibration=None):
    """A full judge payload: four dimensions + summary + a calibration violation list."""
    obj = dict(scores or _clean_scores())
    obj["calibration"] = list(calibration or [])
    return json.dumps(obj)


class _Tracker:
    """A CALL-TRACKING fake llm: returns canned JSON and records every call. NEVER raises (a
    raising stub gets swallowed by the judge's fail-closed except, so the test would pass
    vacuously — this is the guard the brief calls out)."""
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        return self.payload


def _viol(vtype, where="resume", evidence="x", fix="y"):
    return {"type": vtype, "where": where, "evidence": evidence, "fix": fix}


# ---- 1. life-sciences pitch on a NON-life-sciences JD -> wrong_domain_pitch -> FAIL ----

def test_wrong_domain_pitch_on_non_lifesci_jd_fails():
    llm = _Tracker(_payload(calibration=[
        _viol("wrong_domain_pitch", where="resume",
              evidence="fluency in healthcare and life-sciences applications")]))
    out = judge_quality(_JD_ENTERPRISE, _RESUME, _COVER, _ANSWERS, llm=llm)
    assert llm.calls, "the calibration llm must actually be called"
    assert out["verdict"] == "FAIL"
    assert out["calibration"]
    assert out["calibration"][0]["type"] == "wrong_domain_pitch"


# ---- 2. SAME framing on a life-sciences JD -> NO violation (rule 1 is JD-aware) ----

def test_lifesci_framing_on_lifesci_jd_no_violation():
    # On a genuine life-sciences JD the model returns an EMPTY calibration list (it judged the
    # JD domain first). No violation -> verdict follows the four dimension scores (all 5 -> PASS).
    llm = _Tracker(_payload(calibration=[]))
    out = judge_quality(_JD_LIFESCI, _RESUME, _COVER, _ANSWERS, llm=llm)
    assert out["calibration"] == []
    assert out["verdict"] == "PASS"


# ---- 3. resume leading with CAD -> leads_with_cad violation -> FAIL ----

def test_leads_with_cad_fails():
    llm = _Tracker(_payload(calibration=[
        _viol("leads_with_cad", where="resume",
              evidence="Expert SolidWorks CAD modeler", fix="lead with simulation/optimization")]))
    out = judge_quality(_JD_ENTERPRISE, _RESUME, _COVER, _ANSWERS, llm=llm)
    assert out["verdict"] == "FAIL"
    assert out["calibration"][0]["type"] == "leads_with_cad"


# ---- 4. coding-fluency claim -> coding_fluency violation -> FAIL ----

def test_coding_fluency_claim_fails():
    llm = _Tracker(_payload(calibration=[
        _viol("coding_fluency", where="cover", evidence="proficient in Python")]))
    out = judge_quality(_JD_ENTERPRISE, _RESUME, _COVER, _ANSWERS, llm=llm)
    assert out["verdict"] == "FAIL"
    assert out["calibration"][0]["type"] == "coding_fluency"


# ---- 5. clean, well-targeted package -> no calibration -> verdict follows the four scores ----

def test_clean_package_verdict_follows_scores():
    # No calibration violation; a single 3 -> FLAG (the four-dimension logic is unchanged).
    llm = _Tracker(_payload(scores=_clean_scores(fit=3), calibration=[]))
    out = judge_quality(_JD_ENTERPRISE, _RESUME, _COVER, _ANSWERS, llm=llm)
    assert out["calibration"] == []
    assert out["verdict"] == "FLAG"


def test_clean_package_all_fives_passes():
    llm = _Tracker(_payload(calibration=[]))
    out = judge_quality(_JD_ENTERPRISE, _RESUME, _COVER, _ANSWERS, llm=llm)
    assert out["calibration"] == []
    assert out["verdict"] == "PASS"


# ---- 6. GUARD: the fake llm is a call-tracker, never a raising stub ----

def test_fake_llm_is_a_call_tracker_not_a_raising_stub():
    # If the judge ever silently swallowed the llm into a vacuous pass, .calls would be empty.
    # This pins the guard from the brief: a violation FAILs AND the llm was really invoked.
    llm = _Tracker(_payload(calibration=[_viol("coding_fluency", evidence="expert MATLAB")]))
    out = judge_quality(_JD_ENTERPRISE, _RESUME, _COVER, _ANSWERS, llm=llm)
    assert len(llm.calls) >= 1
    assert out["verdict"] == "FAIL"


# ---- calibration FAIL dominates even strong four-dimension scores ----

def test_calibration_fail_overrides_strong_scores():
    # All four dimensions 5 (would be PASS) but a calibration violation forces FAIL.
    llm = _Tracker(_payload(scores=_clean_scores(), calibration=[
        _viol("wrong_tool_attribution", where="resume",
              evidence="ANSYS at Meridian")]))
    out = judge_quality(_JD_ENTERPRISE, _RESUME, _COVER, _ANSWERS, llm=llm)
    assert out["verdict"] == "FAIL"
    assert out["calibration"][0]["type"] == "wrong_tool_attribution"


def test_excluded_project_violation_fails():
    llm = _Tracker(_payload(calibration=[
        _viol("excluded_project", where="cover", evidence="led the MobilityCo product")]))
    out = judge_quality(_JD_ENTERPRISE, _RESUME, _COVER, _ANSWERS, llm=llm)
    assert out["verdict"] == "FAIL"


def test_seniority_mismatch_violation_fails():
    llm = _Tracker(_payload(calibration=[
        _viol("seniority_mismatch", where="resume", evidence="Director of Engineering")]))
    out = judge_quality(_JD_ENTERPRISE, _RESUME, _COVER, _ANSWERS, llm=llm)
    assert out["verdict"] == "FAIL"


# ---- schema: each violation carries type/where/evidence/fix; unknown types are kept ----

def test_violation_schema_fields_preserved():
    llm = _Tracker(_payload(calibration=[
        {"type": "coding_fluency", "where": "cover",
         "evidence": "proficient in Python", "fix": "frame as AI-orchestrated"}]))
    out = judge_quality(_JD_ENTERPRISE, _RESUME, _COVER, _ANSWERS, llm=llm)
    v = out["calibration"][0]
    assert v["type"] == "coding_fluency"
    assert v["where"] == "cover"
    assert v["evidence"] == "proficient in Python"
    assert v["fix"] == "frame as AI-orchestrated"


def test_empty_or_garbled_calibration_entries_dropped():
    # Non-dict / type-less entries are dropped, never counted as a violation.
    llm = _Tracker(_payload(calibration=["just a string", {}, {"where": "resume"}]))
    out = judge_quality(_JD_ENTERPRISE, _RESUME, _COVER, _ANSWERS, llm=llm)
    assert out["calibration"] == []
    assert out["verdict"] == "PASS"


def test_missing_calibration_key_is_no_violation():
    # An older/sparse payload with NO calibration key -> treated as no violations (the four
    # dimensions still govern). Backward-compatible with any cached prompt shape.
    payload = json.dumps(_clean_scores())   # no "calibration" key at all
    llm = _Tracker(payload)
    out = judge_quality(_JD_ENTERPRISE, _RESUME, _COVER, _ANSWERS, llm=llm)
    assert out["calibration"] == []
    assert out["verdict"] == "PASS"
