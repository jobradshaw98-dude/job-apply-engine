# -*- coding: utf-8 -*-
"""Phase 1 (Feature B data model): structured halt classifier + build_record carry.

Pure unit tests — no browser. They pin the §4 tier/category/answer_target table of
`docs/superpowers/AUTONOMOUS_CONVERGENCE_AND_COMM_CHANNEL.md`, the live-dom rule that a failed
widget set is ALWAYS escalate (never answerable), the deterministic id derivation, qkey
normalization matching the /provide-answer route, and that build_record carries the blocker while
an old-style record (no blocker) still builds.
"""
import pytest

from apply_engine.halt_classifier import classify_halt, is_raw_field_key
from apply_engine.orchestrator import JobOutcome
from apply_engine.staged_manifest import build_record, recompute_status


class _Ctx:
    """Minimal RunContext stand-in: only run_dir is read (for the halt screenshot basename)."""
    def __init__(self, run_dir="no_such_dir"):
        self.run_dir = run_dir


def _server_qkey(s):
    """The EXACT normalization aria_server.py /provide-answer + regen_answer._qkey use. Tests
    assert the blocker's answer_target.qkey equals this so the answer maps back."""
    return "".join(c for c in (s or "").lower() if c.isalnum())[:70]


# (label, category, expected_tier, expected_target_kind, halt-site code line) — mirrors §4.
# ANSWERABLE tier: a fact/value only Sam has. ESCALATE tier: needs perception/improvisation.
_TABLE = [
    # category               tier         target_kind   has_question
    ("work_auth",            "answerable", "needs_sam", True),
    ("missing_value",        "answerable", "needs_sam", True),
    ("city",                 "answerable", "needs_sam", True),
    ("screening_yesno",      "answerable", "custom_q",     True),
    ("calibration_unfixable","answerable", "custom_q",     True),
    ("unverifiable_claim",   "answerable", "custom_q",     True),
    ("unknown_widget",       "escalate",   "none",         False),
    ("captcha",              "escalate",   "none",         False),
    ("file_upload",          "escalate",   "none",         False),
    ("zero_fields",          "escalate",   "none",         False),
    ("render_fail",          "escalate",   "none",         False),
]

_TS = "2026-06-11T20:14:03-07:00"


@pytest.mark.parametrize("category,tier,target_kind,has_q", _TABLE)
def test_classify_halt_tier_category_target(category, tier, target_kind, has_q):
    """Each §4 category yields the right tier + answer_target.kind. Answerable carries a real
    question + a qkey that maps back; escalate carries none + empty qkey."""
    out = JobOutcome(job_id="JOB-77", status="needs_sam",
                     halt_reason=f"halt for {category}")
    q = "Are you authorized to work in the US?" if has_q else ""
    blk = classify_halt(out, None, _Ctx(), category=category, halt_ts=_TS,
                        question=q, options=(["Yes", "No"] if has_q else None),
                        free_text_ok=has_q, answer_qkey_source=q,
                        code_source="orchestrator.py:1")
    assert blk["category"] == category
    assert blk["tier"] == tier
    assert blk["answer_target"]["kind"] == target_kind
    if tier == "escalate":
        # escalate never offers an answer box: kind=none, qkey empty, code_context populated.
        assert blk["answer_target"]["qkey"] == ""
        assert blk["code_context"]["source"] == "orchestrator.py:1"
    else:
        # answerable maps back via the EXACT server qkey normalization.
        assert blk["answer_target"]["qkey"] == _server_qkey(q)
    # blocking_reason mirrors the human halt sentence; created_at carries the halt timestamp.
    assert blk["blocking_reason"] == out.halt_reason
    assert blk["created_at"] == _TS
    assert blk["answered_at"] is None
    assert blk["notified"] == {"telegram": False, "dashboard_badge": False}


def test_failed_widget_set_is_always_escalate():
    """LIVE-DOM RULE (feedback_apply_engine_live_dom_and_empty_guard): a widget the engine could
    not drive is unknown_widget -> escalate, NEVER answerable. A value Sam types can't fix a
    DOM the engine can't drive."""
    out = JobOutcome(job_id="JOB-5", status="needs_sam",
                     halt_reason="could not set work-auth answer (authorized=Yes) on the form")
    blk = classify_halt(out, None, _Ctx(), category="unknown_widget", halt_ts=_TS,
                        code_source="orchestrator.py:384")
    assert blk["tier"] == "escalate"
    assert blk["answer_target"] == {"kind": "none", "qkey": ""}
    # no answer box: empty question/options
    assert blk["question"] == ""
    assert blk["options"] == []


def test_id_is_deterministic_from_job_and_timestamp():
    """id derives from job_id + the halt datetime digits (tz offset stripped) — NOT a fresh
    random — so it is stable across re-derivation and testable."""
    out = JobOutcome(job_id="JOB-210", status="needs_sam", halt_reason="x")
    a = classify_halt(out, None, _Ctx(), category="captcha", halt_ts=_TS, code_source="x")
    b = classify_halt(out, None, _Ctx(), category="captcha", halt_ts=_TS, code_source="x")
    assert a["id"] == b["id"] == "blk_JOB-210_20260611201403"
    # a different halt time -> a different id (a re-staged card's new blocker won't collide)
    c = classify_halt(out, None, _Ctx(), category="captcha",
                      halt_ts="2026-06-12T09:00:00-07:00", code_source="x")
    assert c["id"] == "blk_JOB-210_20260612090000"
    assert c["id"] != a["id"]


def test_qkey_matches_provide_answer_route_normalization():
    """The blocker's answer_target.qkey must equal the server's record-side _qkey so the
    /provide-answer route's qkey re-validation matches (a mismatch would 404)."""
    q = "Why do you want to work at Anthropic? (200-400 words, please!)"
    out = JobOutcome(job_id="JOB-1", status="needs_input", halt_reason="x")
    blk = classify_halt(out, None, _Ctx(), category="missing_value", halt_ts=_TS,
                        question=q, free_text_ok=True, answer_qkey_source=q, code_source="x")
    assert blk["answer_target"]["qkey"] == _server_qkey(q)
    # the normalization is alnum-only, lowercased, capped at 70
    assert blk["answer_target"]["qkey"] == "whydoyouwanttoworkatanthropic200400wordsplease"


def test_build_record_carries_the_blocker():
    """build_record must put human_blocker on the flat manifest record, and the existing
    needs_sam / halt_reason / outcome fields stay untouched (backward-compat)."""
    out = JobOutcome(job_id="JOB-210", status="needs_sam",
                     halt_reason="work-auth question needs Sam: country of citizenship?")
    blk = classify_halt(out, None, _Ctx(), category="work_auth", halt_ts=_TS,
                        question="What is your country of citizenship?",
                        options=["Yes", "No"], free_text_ok=True,
                        answer_qkey_source="What is your country of citizenship?",
                        code_source="orchestrator.py:378")
    out.human_blocker = blk
    rec = build_record(out, {"id": "JOB-210", "company": "Acme", "title": "Eng"}, "ts")
    assert rec["human_blocker"] == blk
    # existing fields untouched
    assert rec["halt_reason"] == out.halt_reason
    assert rec["needs_sam"] == [out.halt_reason]  # work-auth halt surfaces as a needs_sam row
    assert rec["status"] == "needs_sam"


def test_old_style_record_with_no_blocker_builds_and_fails_closed_without_audit():
    """BACKWARD-COMPAT for the human_blocker key + FAIL-CLOSED on a missing audit stamp.
    build_record still maps a no-human_blocker outcome (human_blocker=None) — that key is ignored by
    recompute_status. But build_record stamps NO `audit`, so under the 2026-06-22 reviewer fix a
    record whose deterministic gate never ran does NOT stay ready_to_submit — recompute downgrades
    it to needs_sam until the gate runs and stamps gate_blocks: 0."""
    out = JobOutcome(job_id="JOB-CLEAN", status="ready_to_submit", submitted=False)
    rec = build_record(out, {"id": "JOB-CLEAN", "company": "X", "title": "Y"}, "ts")
    assert rec["human_blocker"] is None
    # No audit stamp -> fail-closed -> downgraded (gate never ran).
    assert recompute_status(rec) == "needs_sam"
    # A record literally missing the audit key (legacy on-disk shape) also fails closed.
    legacy = {"job_id": "JOB-OLD", "status": "ready_to_submit", "submitted": False}
    assert recompute_status(legacy) == "needs_sam"
    # But once the deterministic gate has run clean (gate_blocks: 0), it recomputes to ready.
    rec["audit"] = {"gate_blocks": 0}
    assert recompute_status(rec) == "ready_to_submit"


def test_orchestrator_wires_blocker_on_citizenship_halt(fixture_server, tmp_path, monkeypatch):
    """END-TO-END wiring: a real orchestrator citizenship HALT must set out.human_blocker with
    tier=answerable / category=work_auth (not just the helper in isolation). Proves classify_halt
    is wired at the live halt site and that the blocker id derives from the run's halt timestamp."""
    from apply_engine.orchestrator import apply_to_job
    from apply_engine.source_data import Answers
    import apply_engine.adapters.greenhouse as gh
    from apply_engine.adapters.base import WorkAuthQuestion

    monkeypatch.setattr(gh.GreenhouseAdapter, "find_work_auth_questions",
                        lambda self, page: [WorkAuthQuestion(
                            label="What is your country of citizenship?",
                            selector="#q_sponsor", kind="select")])
    resume = tmp_path / "resume.pdf"; resume.write_bytes(b"%PDF-1.4")
    answers = Answers(values={"first_name": "Sam", "last_name": "Rivera",
                              "email": "sam.rivera@example.com", "phone": "555-555-0100"},
                      resume_pdf=resume, cover_pdf=None)
    job = {"id": "JOB-CIT", "company": "Acme", "title": "Eng",
           "url": f"{fixture_server}/greenhouse_form.html"}
    outcome = apply_to_job(job=job, answers=answers, runs_root=tmp_path / "runs",
                           profile_dir=tmp_path / "prof", headless=True, dry_run=True,
                           ats_override="greenhouse")
    assert outcome.status == "needs_sam"
    blk = outcome.human_blocker
    assert isinstance(blk, dict)
    assert blk["tier"] == "answerable"
    assert blk["category"] == "work_auth"
    assert blk["answer_target"]["kind"] == "needs_sam"
    assert blk["answer_target"]["qkey"] == _server_qkey("What is your country of citizenship?")
    assert blk["id"].startswith("blk_JOB-CIT_")
    # the halt screenshot basename was resolved from the run dir
    assert blk["screenshot"].endswith(".png")
    assert blk["page_state"]["ats"] == "greenhouse"


def test_is_raw_field_key_routes_bare_keys_to_escalate():
    """A bare field key / UUID (no human question) has no answer Sam can supply -> the unfilled
    halt routes it to escalate, not a missing_value answer box. Pins the helper the orchestrator
    uses to make that split."""
    assert is_raw_field_key("cards[7f62479b][field0]")
    assert is_raw_field_key("question_4573437009")
    assert is_raw_field_key("name--legalName--firstName")
    # real questions are NOT raw keys
    assert not is_raw_field_key("First name")
    assert not is_raw_field_key("Are you authorized to work?")
    assert not is_raw_field_key("")
