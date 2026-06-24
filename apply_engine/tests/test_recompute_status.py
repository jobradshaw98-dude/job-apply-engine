# -*- coding: utf-8 -*-
"""TDD for the honest status recompute that flips a staged record from needs_input to
ready_to_submit once every STORED blocker the user answered is resolved.

Two layers:
  * recompute_status — the PURE decision (a matrix of blocker combinations + the one-way valve).
  * apply_recompute / regen_answer --provide — the persisted, atomic integration: answering the
    last open question flips the record's status in the manifest.

The whole point is the TRANSITION and its honesty: it never recomputes a submitted record, never
downgrades a more-review-ready status, and only sees stored state (the live form is re-verified by
finish before any submit). No browser, no LLM — the provide path constructs neither.
"""
import json

from apply_engine import config
from apply_engine import regen_answer
from apply_engine.staged_manifest import (REVIEW_READY, apply_recompute,
                                          recompute_status)


# --------------------------------------------------------------------------------------
# recompute_status — pure decision matrix
# --------------------------------------------------------------------------------------

def _rec(**over):
    """A needs_input record with NO outstanding blockers (would flip to review-ready)."""
    rec = {
        "job_id": "JOB-1",
        "status": "needs_input",
        "submitted": False,
        "needs_sam": [],
        "custom_qs": [
            {"q": "Why us?", "status": "drafted", "value": "Because mission."},
            {"q": "Office days?", "status": "answered", "value": "Yes", "answered_by": "sam"},
        ],
        "audit": {"verdict": "PASS"},
        # quality judge is the SECOND gate (mirrors finish.can_submit). A clean record carries a
        # PASS quality_audit; tests for the quality blocker override this key explicitly.
        "quality_audit": {"verdict": "PASS", "judge_ran": True},
    }
    rec.update(over)
    return rec


def test_all_clear_flips_to_review_ready():
    assert recompute_status(_rec()) == REVIEW_READY


def test_needs_input_custom_q_blocks():
    rec = _rec(custom_qs=[{"q": "Hard one", "status": "needs_input", "value": ""}])
    assert recompute_status(rec) == "needs_input"


def test_needs_sam_item_blocks():
    rec = _rec(needs_sam=["Country", "Some required field"])
    assert recompute_status(rec) == "needs_input"


def test_legacy_unfilled_required_blocks():
    # Older records used the unfilled_required key instead of needs_sam.
    rec = _rec(needs_sam=[], unfilled_required=["Cover letter"])
    assert recompute_status(rec) == "needs_input"


def test_declined_custom_q_does_not_block():
    # Declined questions are intentionally left for the user and never gated a clean form.
    rec = _rec(custom_qs=[
        {"q": "Salary expectation", "status": "declined", "value": ""},
        {"q": "Why us?", "status": "drafted", "value": "ok"},
    ])
    assert recompute_status(rec) == REVIEW_READY


def test_audit_blocked_blocks():
    rec = _rec(audit={"verdict": "BLOCKED", "gate_blocks": 1})
    assert recompute_status(rec) == "needs_input"


def test_audit_pass_with_flag_findings_is_fine():
    # A PASS verdict that carries advisory FLAG findings is still review-ready.
    rec = _rec(audit={"verdict": "PASS", "findings": [{"severity": "FLAG"}]})
    assert recompute_status(rec) == REVIEW_READY


def test_missing_audit_blocks_gate_never_ran():
    # FLIPPED (2026-06-22 reviewer fix). FAIL-CLOSED: a record with no `audit` stamp means the
    # deterministic gate NEVER RAN, so recompute must NOT promote it to review-ready — it stays
    # needs_input. Mirrors finish.can_submit's missing-stamp block.
    rec = _rec()
    rec.pop("audit")
    assert recompute_status(rec) == "needs_input"


def test_stamped_clean_audit_flips_to_ready():
    # The other side of the contract: an EXPLICIT clean deterministic stamp (gate ran, gate_blocks
    # == 0) DOES flip a needs_input record to review-ready.
    rec = _rec(audit={"gate_blocks": 0})
    assert recompute_status(rec) == REVIEW_READY


def test_degraded_fabrication_audit_now_flips_to_ready():
    # FLIPPED (2026-06-22 demotion): a legacy degraded LLM stamp (PASS-shaped, judge_ran False) with
    # a CLEAN deterministic gate (no gate_blocks) used to be held at needs_input. judge_ran is
    # advisory now; recompute mirrors the deterministic-gate-only can_submit, so it FLIPS to ready.
    rec = _rec(audit={"verdict": "PASS", "judge_ran": False})
    assert recompute_status(rec) == REVIEW_READY


# ---- DEMOTED quality gate (2026-06-22): quality_audit no longer gates recompute ----
# The quality judge was demoted to advisory/on-demand. recompute upgrades when custom_qs have no
# needs_input, no unresolved required field, AND audit.gate_blocks == 0 — independent of the
# holistic quality verdict. These were flipped from their old "stays needs_input" assertions.

def test_quality_fail_now_flips_to_ready():
    # OLD: test_quality_fail_does_not_flip_to_ready — a FAIL quality verdict no longer blocks the
    # status flip (clean deterministic gate). The quality judge is advisory display only now.
    rec = _rec(quality_audit={"verdict": "FAIL", "judge_ran": True,
                              "summary": "doesn't cover the JD"})
    assert recompute_status(rec) == REVIEW_READY


def test_quality_missing_now_flips_to_ready():
    # OLD: test_quality_missing_does_not_flip_to_ready — no quality_audit at all no longer blocks.
    rec = _rec()
    rec.pop("quality_audit")
    assert recompute_status(rec) == REVIEW_READY


def test_quality_judge_did_not_run_now_flips_to_ready():
    # OLD: test_quality_judge_did_not_run_does_not_flip_to_ready — quality judge_ran False is advisory.
    rec = _rec(quality_audit={"verdict": "PASS", "judge_ran": False})
    assert recompute_status(rec) == REVIEW_READY


def test_quality_unknown_verdict_now_flips_to_ready():
    # OLD: test_quality_unknown_verdict_does_not_flip_to_ready — unknown quality verdict is advisory.
    rec = _rec(quality_audit={"verdict": "garbage", "judge_ran": True})
    assert recompute_status(rec) == REVIEW_READY


def test_quality_flag_is_review_ready():
    # FLAG was always advisory; still review-ready (unchanged).
    rec = _rec(quality_audit={"verdict": "flag", "judge_ran": True})
    assert recompute_status(rec) == REVIEW_READY


# --------------------------------------------------------------------------------------
# one-way valve: never downgrade, never touch submitted
# --------------------------------------------------------------------------------------

def test_submitted_record_untouched_even_with_blockers():
    rec = _rec(submitted=True, status="submitted", needs_sam=["still open"])
    assert recompute_status(rec) == "submitted"


def test_ready_to_submit_downgraded_when_blocker_appears():
    # TWO-WAY VALVE (2026-06-16): a record already at ready_to_submit MUST be downgraded to
    # needs_sam when a blocker is present in its stored state — otherwise the dashboard shows a
    # green "ready" while finish.can_submit refuses the submit (the JOB-293 Future false-ready). The
    # blocker check mirrors can_submit, so a downgrade here means the submit gate would refuse too.
    rec = _rec(status="ready_to_submit", needs_sam=["a blocker"])
    assert recompute_status(rec) == "needs_sam"


def test_ready_to_submit_NOT_downgraded_on_llm_blocked_only():
    # FLIPPED (2026-06-22 demotion): a ready record whose LLM audit is re-stamped BLOCKED but with a
    # CLEAN deterministic gate (no gate_blocks) is NO LONGER downgraded — the LLM verdict is advisory.
    # Only a deterministic gate_blocks>0 downgrades (asserted in
    # test_ready_to_submit_downgraded_on_deterministic_gate_block below).
    rec = _rec(status="ready_to_submit")
    rec["audit"] = {"verdict": "BLOCKED"}
    assert recompute_status(rec) == "ready_to_submit"


def test_ready_to_submit_downgraded_on_deterministic_gate_block():
    # SAFETY (kept/strengthened): a ready record with a live deterministic gate block (gate_blocks>0)
    # MUST still be downgraded — the deterministic gate is the one content gate that still blocks.
    rec = _rec(status="ready_to_submit")
    rec["audit"] = {"verdict": "BLOCKED", "gate_blocks": 1}
    assert recompute_status(rec) == "needs_sam"


def test_ready_to_submit_kept_when_no_blocker():
    # No spurious downgrade: a genuinely clean ready record stays ready (the valve only downgrades
    # when a real blocker is present).
    rec = _rec(status="ready_to_submit")
    assert recompute_status(rec) == "ready_to_submit"


def test_error_status_can_recompute_to_ready():
    rec = _rec(status="error")
    assert recompute_status(rec) == REVIEW_READY


def test_needs_sam_status_can_recompute_to_ready():
    rec = _rec(status="needs_sam")
    assert recompute_status(rec) == REVIEW_READY


def test_non_dict_returns_empty():
    assert recompute_status(None) == ""


# --------------------------------------------------------------------------------------
# apply_recompute — atomic persisted single-record update
# --------------------------------------------------------------------------------------

def test_apply_recompute_persists_flip(tmp_path):
    mp = tmp_path / "staged_applications.json"
    mp.write_text(json.dumps([_rec(job_id="JOB-1")], indent=2), encoding="utf-8")
    new = apply_recompute(mp, "JOB-1")
    assert new == REVIEW_READY
    assert json.loads(mp.read_text())[0]["status"] == REVIEW_READY


def test_apply_recompute_noop_when_blocked(tmp_path):
    mp = tmp_path / "staged_applications.json"
    rec = _rec(job_id="JOB-1", needs_sam=["open item"])
    mp.write_text(json.dumps([rec], indent=2), encoding="utf-8")
    new = apply_recompute(mp, "JOB-1")
    assert new == "needs_input"
    assert json.loads(mp.read_text())[0]["status"] == "needs_input"


def test_apply_recompute_unknown_job_returns_none(tmp_path):
    mp = tmp_path / "staged_applications.json"
    mp.write_text(json.dumps([_rec(job_id="JOB-1")]), encoding="utf-8")
    assert apply_recompute(mp, "JOB-NOPE") is None


def test_apply_recompute_missing_manifest_returns_none(tmp_path):
    assert apply_recompute(tmp_path / "nope.json", "JOB-1") is None


def test_apply_recompute_leaves_other_records_untouched(tmp_path):
    mp = tmp_path / "staged_applications.json"
    other = {"job_id": "JOB-2", "status": "needs_input", "needs_sam": ["still open"]}
    mp.write_text(json.dumps([_rec(job_id="JOB-1"), other], indent=2), encoding="utf-8")
    apply_recompute(mp, "JOB-1")
    data = json.loads(mp.read_text())
    j2 = next(r for r in data if r["job_id"] == "JOB-2")
    assert j2["status"] == "needs_input"  # untouched


# --------------------------------------------------------------------------------------
# integration: --provide on the last open question flips the record's status
# --------------------------------------------------------------------------------------

def _seed_one_open(tmp_path):
    """A record that is one --provide away from review-ready: a single open needs_sam item,
    all custom_qs drafted/answered, audit PASS."""
    apps = [{
        "job_id": "JOB-900",
        "company": "TestCo",
        "status": "needs_input",
        "submitted": False,
        "needs_sam": ["Are you able to commit to being in the office 3x per week?"],
        "custom_qs": [
            {"q": "Why do you want to work here?", "kind": "essay",
             "status": "drafted", "value": "A solid drafted answer."},
        ],
        "audit": {"verdict": "PASS"},
        "quality_audit": {"verdict": "PASS", "judge_ran": True},
    }]
    mp = tmp_path / "staged_applications.json"
    mp.write_text(json.dumps(apps, indent=2), encoding="utf-8")
    (tmp_path / "jobs.json").write_text(json.dumps([{"id": "JOB-900"}]), encoding="utf-8")
    return mp


def _wire_no_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")
    monkeypatch.setattr(regen_answer, "make_claude_llm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM on provide")))
    monkeypatch.setattr(regen_answer, "make_audit_fn",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no audit on provide")))
    monkeypatch.setattr(regen_answer, "load_facts",
                        lambda job=None, **k: (_ for _ in ()).throw(AssertionError("no facts on provide")))


def test_provide_last_open_question_flips_status(tmp_path, monkeypatch):
    mp = _seed_one_open(tmp_path)
    _wire_no_llm(tmp_path, monkeypatch)

    rc = regen_answer.main([
        "JOB-900",
        "--question", "Are you able to commit to being in the office 3x per week?",
        "--provide", "Yes",
    ])
    assert rc == 0
    app = next(a for a in json.loads(mp.read_text(encoding="utf-8"))
               if a.get("job_id") == "JOB-900")
    # needs_sam pruned to empty AND status recomputed to review-ready in the same write.
    assert app["needs_sam"] == []
    assert app["status"] == REVIEW_READY


def test_provide_with_another_open_item_stays_needs_input(tmp_path, monkeypatch):
    mp = _seed_one_open(tmp_path)
    # add a SECOND open item that this provide won't answer
    data = json.loads(mp.read_text(encoding="utf-8"))
    data[0]["needs_sam"].append("Country")
    mp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _wire_no_llm(tmp_path, monkeypatch)

    regen_answer.main([
        "JOB-900",
        "--question", "Are you able to commit to being in the office 3x per week?",
        "--provide", "Yes",
    ])
    app = next(a for a in json.loads(mp.read_text(encoding="utf-8"))
               if a.get("job_id") == "JOB-900")
    assert "Country" in app["needs_sam"]
    assert app["status"] == "needs_input"  # still blocked by the remaining item
