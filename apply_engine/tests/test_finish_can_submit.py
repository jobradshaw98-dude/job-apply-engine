"""Unit tests for the PURE pieces of finish.py: the can_submit safety gate (every
refusal branch + the happy path) and the deterministic custom-question label matcher.

These are the safety-critical pure functions — the SUBMIT gate. They are tested
exhaustively because a false PASS here is the worst failure mode in the whole engine.
The browser parts (replay / finish_job) cannot be unit-tested without a live form and
are exercised only by live runs (noted in the summary)."""
import pytest

from apply_engine.finish import can_submit, match_custom_entry, _norm_label


def _ready_record(**over):
    """A minimal record that PASSES can_submit; override fields per test.

    DEMOTION CONTRACT (2026-06-22): the two LLM gates were demoted from required
    submit-blockers to advisory/on-demand. can_submit now blocks on the DETERMINISTIC gate
    (audit.gate_blocks > 0), submitted, non-review-ready status, unfilled-required, and work-auth
    red flags — NOT on the LLM audit verdict / judge_ran or the holistic quality_audit. The default
    record carries gate_blocks == 0 (clean deterministic gate). The PASS audit / quality_audit are
    retained as benign defaults (advisory display) so they don't perturb the branch under test."""
    rec = {
        "job_id": "JOB-1",
        "status": "ready_to_submit",
        "submitted": False,
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "filled_fields": ["first_name", "last_name", "email"],
        "work_auth": [{"field": "sponsor", "q": "Require sponsorship?", "answer": "No"}],
        "custom_qs": [],
        "unfilled_required": [],
        "needs_sam": [],
        "audit": {"verdict": "PASS", "judge_ran": True, "findings": []},
        "quality_audit": {"verdict": "PASS", "judge_ran": True, "dimensions": {}},
    }
    rec.update(over)
    return rec


# ---- happy path ----

def test_happy_path_passes():
    ok, reason = can_submit(_ready_record())
    assert ok is True
    assert reason == ""


def test_happy_path_authorized_yes():
    rec = _ready_record(work_auth=[{"field": "authorized", "answer": "Yes"}])
    ok, _ = can_submit(rec)
    assert ok is True


def test_happy_path_authorized_no_sponsorship_yes():
    rec = _ready_record(work_auth=[{"field": "authorized_no_sponsorship", "answer": "Yes"}])
    ok, _ = can_submit(rec)
    assert ok is True


def test_happy_path_multistep_review_snippet():
    # Workday-style: field=work_auth, answer is a review-page snippet verified by predicate.
    rec = _ready_record(work_auth=[{"field": "work_auth",
                                    "answer": "Will you require sponsorship? No, I do not "
                                              "require sponsorship for employment"}])
    ok, _ = can_submit(rec)
    assert ok is True


def test_happy_path_no_work_auth_questions_at_all():
    # A form with no work-auth questions and no work-auth halt is fine.
    rec = _ready_record(work_auth=[])
    ok, _ = can_submit(rec)
    assert ok is True


# ---- refusal: already submitted ----

def test_refuse_already_submitted():
    ok, reason = can_submit(_ready_record(submitted=True))
    assert ok is False
    assert "already submitted" in reason


# ---- DETERMINISTIC gate: gate_blocks > 0 STILL blocks (the hard backstop) ----

def test_deterministic_gate_block_still_blocks():
    # The deterministic gate is the ONE content gate that still hard-blocks submit (2026-06-22).
    # A record with audit.gate_blocks > 0 must be refused, naming the fabrication-class finding(s).
    rec = _ready_record(audit={"verdict": "PASS", "judge_ran": True, "gate_blocks": 2,
                               "findings": []})
    ok, reason = can_submit(rec)
    assert ok is False
    assert "deterministic gate" in reason.lower()
    assert "fabrication-class finding" in reason.lower()


def test_pass_audit_verdict_pass_is_fine():
    rec = _ready_record(audit={"verdict": "PASS", "judge_ran": True, "findings": []})
    ok, _ = can_submit(rec)
    assert ok is True


# ---- DEMOTED gates (2026-06-22): the LLM verdict / judge_ran / quality_audit no longer block ----
# the user's explicit call: the two LLM gates were demoted from required submit-blockers to
# advisory/on-demand. With a clean DETERMINISTIC gate (gate_blocks == 0), a clean work-auth answer,
# and a review-ready status, can_submit now PASSES regardless of the LLM verdict / quality verdict.
# These tests were flipped from their old "refuse because LLM not PASS / quality FAIL" assertions.

def test_blocked_llm_verdict_no_longer_blocks():
    # OLD: test_refuse_blocked_audit — a BLOCKED *LLM* verdict (no deterministic gate_blocks) used
    # to refuse. The LLM verdict is advisory now, so with gate_blocks == 0 this SUBMITS.
    rec = _ready_record(audit={"verdict": "BLOCKED", "gate_blocks": 0, "findings": []})
    ok, reason = can_submit(rec)
    assert ok is True, reason


def test_missing_audit_none_blocks_gate_never_ran():
    # FAIL-CLOSED (2026-06-22 reviewer fix): a record with audit=None means the DETERMINISTIC gate
    # NEVER RAN — not that it passed. Reading gate_blocks as 0 here would let an unchecked package
    # auto-submit (the SEV-HIGH hole). It must now BLOCK, naming that the gate hasn't run.
    rec = _ready_record(audit=None)
    ok, reason = can_submit(rec)
    assert ok is False
    assert "hasn't run" in reason.lower()


def test_missing_audit_key_blocks_gate_never_ran():
    # Same fail-closed contract when the `audit` key is entirely absent (not a dict) — BLOCK.
    rec = _ready_record()
    del rec["audit"]
    ok, reason = can_submit(rec)
    assert ok is False
    assert "hasn't run" in reason.lower()


def test_stamped_clean_audit_submits():
    # The other side of the fail-closed contract: a record carrying an EXPLICIT clean deterministic
    # stamp (audit={"gate_blocks": 0}) — the gate RAN and found nothing — DOES submit, given clean
    # work-auth + review-ready status. Proves the fix blocks only the MISSING stamp, not a clean one.
    rec = _ready_record(audit={"gate_blocks": 0})
    ok, reason = can_submit(rec)
    assert ok is True, reason


def test_stamped_clean_but_blocked_llm_verdict_still_submits():
    # Whole point of the demotion: a clean deterministic stamp with a BAD/missing LLM verdict and
    # judge_ran=False STILL submits — the LLM stays advisory even under the fail-closed fix.
    rec = _ready_record(audit={"gate_blocks": 0, "verdict": "BLOCKED", "judge_ran": False})
    ok, reason = can_submit(rec)
    assert ok is True, reason


def test_audit_verdict_not_pass_no_longer_blocks():
    # OLD: test_refuse_audit_verdict_not_pass — a non-PASS LLM verdict (with a clean deterministic
    # gate) is advisory now, so it SUBMITS.
    rec = _ready_record(audit={"verdict": "UNKNOWN", "gate_blocks": 0, "findings": []})
    ok, reason = can_submit(rec)
    assert ok is True, reason


def test_pass_but_judge_did_not_run_no_longer_blocks():
    # OLD: test_refuse_pass_but_judge_did_not_run — judge_ran=False (degraded LLM judge) used to be
    # treated like no audit. judge_ran is advisory now; with gate_blocks == 0 this SUBMITS.
    rec = _ready_record(audit={"verdict": "PASS", "judge_ran": False, "gate_blocks": 0,
                               "findings": []})
    ok, reason = can_submit(rec)
    assert ok is True, reason


def test_degraded_blocked_stamp_no_longer_blocks_but_real_gate_block_does():
    # OLD: test_refuse_degraded_blocked_stamp_names_the_judge_not_fabrication.
    # The fail-closed degraded LLM stamp (BLOCKED, judge_ran False, ZERO deterministic gate_blocks)
    # is advisory now -> SUBMITS. A real deterministic gate_blocks>0 still blocks (asserted above
    # in test_deterministic_gate_block_still_blocks). Prove the degraded-only stamp clears here.
    rec = _ready_record(audit={"verdict": "BLOCKED", "judge_ran": False, "findings": [],
                               "gate_blocks": 0, "block_findings": 0})
    ok, reason = can_submit(rec)
    assert ok is True, reason


def test_pass_audit_without_judge_ran_key_is_allowed():
    # Still allowed (unchanged): a PASS audit with no judge_ran field and a clean deterministic gate.
    rec = _ready_record(audit={"verdict": "PASS", "findings": []})
    ok, _ = can_submit(rec)
    assert ok is True


# ---- DEMOTED quality gate (2026-06-22): quality_audit no longer blocks can_submit ----

def test_quality_fail_no_longer_blocks():
    # OLD: test_refuse_quality_verdict_fail — a FAIL holistic-quality verdict used to block. The
    # quality judge is advisory/on-demand now, so with a clean deterministic gate this SUBMITS.
    rec = _ready_record(quality_audit={"verdict": "FAIL", "judge_ran": True,
                                       "summary": "doesn't cover the JD"})
    ok, reason = can_submit(rec)
    assert ok is True, reason


def test_quality_garbage_verdict_no_longer_blocks():
    # OLD: test_refuse_quality_verdict_garbage — an unrecognized quality verdict no longer blocks.
    rec = _ready_record(quality_audit={"judge_ran": True, "verdict": "garbage"})
    ok, reason = can_submit(rec)
    assert ok is True, reason


def test_quality_missing_verdict_no_longer_blocks():
    # OLD: test_refuse_quality_verdict_missing — a quality_audit with no verdict key no longer blocks.
    rec = _ready_record(quality_audit={"judge_ran": True})
    ok, reason = can_submit(rec)
    assert ok is True, reason


def test_quality_missing_entirely_no_longer_blocks():
    # A record with NO quality_audit at all now SUBMITS — the quality judge is advisory/on-demand.
    rec = _ready_record()
    del rec["quality_audit"]
    ok, reason = can_submit(rec)
    assert ok is True, reason


def test_quality_flag_clears():
    # FLAG was always advisory; still clears (unchanged).
    rec = _ready_record(quality_audit={"verdict": " flag ", "judge_ran": True})
    ok, _ = can_submit(rec)
    assert ok is True


# ---- refusal: non-review-ready status ----

@pytest.mark.parametrize("status", ["needs_input", "needs_sam", "error"])
def test_refuse_not_review_ready_status(status):
    # clear unrelated refusal sources so status is the cause under test
    rec = _ready_record(status=status, needs_sam=[], unfilled_required=[])
    ok, reason = can_submit(rec)
    assert ok is False
    assert status in reason


# ---- refusal: unfilled required fields ----

def test_refuse_unfilled_required():
    rec = _ready_record(unfilled_required=["Cover letter", "Relocation?"])
    ok, reason = can_submit(rec)
    assert ok is False
    assert "required field" in reason


def test_refuse_unfilled_required_via_needs_sam_key():
    # records written by the manifest carry needs_sam, not unfilled_required
    rec = _ready_record(unfilled_required=[], needs_sam=["Cover letter"])
    ok, reason = can_submit(rec)
    assert ok is False
    assert "required field" in reason


# ---- refusal: bad work-auth answer ----

def test_refuse_sponsor_yes():
    rec = _ready_record(work_auth=[{"field": "sponsor", "answer": "Yes"}])
    ok, reason = can_submit(rec)
    assert ok is False
    assert "work-auth" in reason.lower()


def test_refuse_authorized_no():
    rec = _ready_record(work_auth=[{"field": "authorized", "answer": "No"}])
    ok, reason = can_submit(rec)
    assert ok is False
    assert "work-auth" in reason.lower()


def test_refuse_work_auth_missing_answer():
    rec = _ready_record(work_auth=[{"field": "sponsor", "answer": ""}])
    ok, reason = can_submit(rec)
    assert ok is False
    assert "work-auth" in reason.lower()


def test_refuse_work_auth_unknown_field_non_no_red_flag_snippet():
    rec = _ready_record(work_auth=[{"field": "work_auth",
                                    "answer": "Yes, I will need sponsorship"}])
    ok, reason = can_submit(rec)
    assert ok is False
    assert "work-auth" in reason.lower()


def test_refuse_work_auth_ambiguous_snippet():
    # a snippet with no clear negative is ambiguous -> not a verified PASS -> refuse
    rec = _ready_record(work_auth=[{"field": "work_auth", "answer": "maybe later"}])
    ok, _ = can_submit(rec)
    assert ok is False


def test_refuse_unanswered_work_auth_left_for_sam():
    # a work-auth question slipped through to the user: classified halt text in needs_sam.
    # Use a status that is NOT in the not-ready set so this branch is the proven cause,
    # and keep unfilled_required empty so it isn't the earlier refusal.
    rec = _ready_record(status="ready_to_submit", unfilled_required=[], work_auth=[],
                        needs_sam=["Are you a U.S. citizen?"])
    ok, reason = can_submit(rec)
    assert ok is False
    # citizenship classifies as a work-auth HALT
    assert "work-auth" in reason.lower() or "required field" in reason.lower()


# ---- refusal: malformed record ----

def test_refuse_non_dict():
    ok, reason = can_submit(None)
    assert ok is False
    assert reason


def test_refuse_work_auth_entry_not_a_dict():
    rec = _ready_record(work_auth=["No"])
    ok, _ = can_submit(rec)
    assert ok is False


# ---- ordering: submitted beats everything ----

def test_submitted_refusal_takes_precedence_over_block():
    rec = _ready_record(submitted=True, audit={"verdict": "BLOCKED"})
    ok, reason = can_submit(rec)
    assert ok is False
    assert "already submitted" in reason


# ======================================================================================
# match_custom_entry / _norm_label
# ======================================================================================

def test_norm_label_collapses_and_strips_marker():
    assert _norm_label("  Why  us? *") == "why us?"
    assert _norm_label("WHY US?") == _norm_label("why us?")


def _stored():
    return [
        {"q": "Why do you want to work here?", "kind": "essay",
         "status": "drafted", "value": "Because mission."},
        {"q": "Years of FEA experience", "kind": "select",
         "status": "answered", "value": "5-7"},
        {"q": "Languages", "kind": "checkbox_group",
         "status": "answered", "values": ["Python", "C++"]},
        {"q": "Salary expectation", "kind": "short_text",
         "status": "declined", "reason": "judgment call"},
        {"q": "Sensitive thing", "kind": "essay",
         "status": "blocked", "value": "x"},
    ]


def test_match_exact_normalized():
    e = match_custom_entry("Why do you want to work here? *", _stored())
    assert e is not None and e["value"] == "Because mission."


def test_match_containment_fallback():
    # live label carries extra helper text around the stored question
    e = match_custom_entry("Years of FEA experience (approximate)", _stored())
    assert e is not None and e["value"] == "5-7"


def test_match_declined_entry_never_returned():
    assert match_custom_entry("Salary expectation", _stored()) is None


def test_match_blocked_entry_never_returned():
    assert match_custom_entry("Sensitive thing", _stored()) is None


def test_match_no_value_not_returned():
    stored = [{"q": "Empty", "status": "answered"}]  # answered but no value/values
    assert match_custom_entry("Empty", stored) is None


def test_match_checkbox_values_eligible():
    e = match_custom_entry("Languages", _stored())
    assert e is not None and e["values"] == ["Python", "C++"]


def test_match_unknown_label_returns_none():
    assert match_custom_entry("Totally different question", _stored()) is None


def test_match_empty_label_returns_none():
    assert match_custom_entry("", _stored()) is None


def test_office_commitment_yes_is_not_a_work_auth_red_flag():
    """An office_commitment 'Yes' (correct RTO answer) stored in the work_auth list must NOT be
    judged by the sponsorship verifier — it would falsely block submit. Regression: live batch
    2026-06-08 JOB-237/242/248 ready-but-blocked on office_commitment=Yes."""
    rec = _ready_record()
    rec["work_auth"] = [
        {"field": "sponsor", "q": "Do you require visa sponsorship?", "answer": "No"},
        {"field": "office_commitment", "q": "Open to in-person 3x/week?", "answer": "Yes"},
    ]
    ok, reason = can_submit(rec)
    assert ok is True, reason


def test_authorized_without_sponsorship_yes_plus_office_yes_clears():
    """The combined 'authorized without sponsorship' = Yes (a no-red-flag answer) alongside an
    office_commitment Yes must clear — not block."""
    rec = _ready_record()
    rec["work_auth"] = [
        {"field": "authorized", "answer": "Yes"},
        {"field": "authorized_no_sponsorship", "answer": "Yes"},
        {"field": "office_commitment", "answer": "Yes"},
    ]
    ok, reason = can_submit(rec)
    assert ok is True, reason
