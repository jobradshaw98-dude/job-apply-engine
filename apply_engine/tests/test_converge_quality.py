# -*- coding: utf-8 -*-
"""Phase 4d — tests for converge_quality, the autonomous quality-convergence loop (Feature A core).

These pin the HARD contract (feedback_apply_autonomous_quality_loop) + the §6 failure modes the
brief requires (#2 treadmill, #3 quota/quality-once, #10 fabricating fix, #11 false-converged via
verify_ready, #13 degraded judge). Every test INJECTS audit_fn/fix_fn — NO real claude -p, NO
network. The audit is a stub that mutates the on-disk manifest record so verify_ready / the BLOCK
collectors read real state; fixes are stubs that shrink (or don't) the stubbed findings.

The seam:
  * audit_fn(job_id, include_quality=…, recheck_calibration=…) — stubbed to write the `audit` /
    `quality_audit` blocks onto the record per a scripted per-round plan.
  * fix_fn(job_id, finding) — stubbed; the test's audit plan decides what the NEXT round sees, so a
    "fabricating fix" is modelled by an audit plan whose blocks never shrink.
  * notify_fn(record_or_blocker) — a recorder so we assert it fired exactly once, no network.
"""
import json
from pathlib import Path

import pytest

from apply_engine import converge
from apply_engine import config as _cfg
from apply_engine.finish import verify_ready


# --------------------------------------------------------------------------------------
# fixtures: a tailored career tree on disk + a ready-shaped staged record
# --------------------------------------------------------------------------------------

@pytest.fixture()
def career_tree(tmp_path, monkeypatch):
    """A throwaway career tree (master + tailored resume/cover on disk) so verify_submittable's
    PDF-integrity invariant passes for real, plus config.ARIA_DATA pointed at the manifest dir."""
    root = tmp_path / "career"
    pkg = root / "apply_engine"
    pkg.mkdir(parents=True)
    (root / "APPLICANT_Resume_Master.pdf").write_text("MASTER", encoding="utf-8")
    appdir = root / "applications" / "APP-001-Acme"
    appdir.mkdir(parents=True)
    (appdir / "APPLICANT_Resume.pdf").write_text("TAILORED", encoding="utf-8")
    (appdir / "APPLICANT_Cover_Letter.pdf").write_text("COVER", encoding="utf-8")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # converge + finish both read config.* — point them at our throwaway tree.
    monkeypatch.setattr(_cfg, "ARIA_DATA", data_dir)
    monkeypatch.setattr(_cfg, "PKG_DIR", pkg)
    return {"appdir": appdir, "data_dir": data_dir}


def _base_record(appdir: Path, **over) -> dict:
    """A staged record that PASSES verify_ready when its audit/quality blocks are clean — the
    'ready to converge' baseline. status ready_to_submit so the loop's stage-success guard passes."""
    rec = {
        "job_id": "JOB-001",
        "company": "Acme",
        "role": "Applied AI Engineer",
        "status": "ready_to_submit",
        "submitted": False,
        "uploaded_docs": [
            {"doc": "resume", "path": str(appdir / "APPLICANT_Resume.pdf"),
             "name": "APPLICANT_Resume.pdf"},
            {"doc": "cover", "path": str(appdir / "APPLICANT_Cover_Letter.pdf"),
             "name": "APPLICANT_Cover_Letter.pdf"},
        ],
        "work_auth": [{"field": "sponsor", "answer": "No"}],
        "custom_qs": [{"q": "Why us?", "kind": "essay", "status": "answered", "value": "x"}],
        "unfilled_required": [],
        "needs_sam": [],
        "audit": {"verdict": "PASS", "judge_ran": True, "findings": [],
                  "gate_blocks": 0, "block_findings": 0},
        "quality_audit": {"verdict": "PASS", "judge_ran": True, "calibration": []},
    }
    rec.update(over)
    return rec


def _write(data_dir: Path, rec: dict) -> Path:
    mp = data_dir / "staged_applications.json"
    mp.write_text(json.dumps([rec], indent=2), encoding="utf-8")
    return mp


def _read(mp: Path) -> dict:
    return json.loads(mp.read_text(encoding="utf-8"))[0]


def _fab_block(question="Why us?"):
    return {"doc": "essay_answer", "lens": "fabrication", "severity": "BLOCK",
            "question": question, "offending_text": "an invented metric",
            "issue": "claim not in ledger", "fix": "remove the metric"}


def _patch_audit(mp: Path, *, audit=None, quality=None):
    """In-place mutate the on-disk record's audit/quality blocks — what an audit_fn stub does."""
    data = json.loads(mp.read_text(encoding="utf-8"))
    if audit is not None:
        data[0]["audit"] = audit
    if quality is not None:
        data[0]["quality_audit"] = quality
    mp.write_text(json.dumps(data, indent=2), encoding="utf-8")


class _Recorder:
    """A notify_fn recorder — captures each call, never touches the network."""
    def __init__(self):
        self.calls = []

    def __call__(self, record_or_blocker):
        self.calls.append(record_or_blocker)
        return True


# ======================================================================================
# 1. Converges by REMOVAL — stubbed findings shrink each round -> "converged" within the cap,
#    and verify_ready GATES it (§6 #11 — the converged assertion goes through verify_ready).
# ======================================================================================

def test_converges_by_removal(career_tree):
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    # Round 1 audit finds ONE fabrication BLOCK; the fix shrinks it so round 2 is clean.
    mp = _write(data_dir, _base_record(appdir,
                audit={"verdict": "BLOCKED", "judge_ran": True, "gate_blocks": 0,
                       "block_findings": 1, "findings": [_fab_block()]}))

    rounds = {"n": 0}

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        rounds["n"] += 1
        if rounds["n"] == 1:
            # round 1 quality pass — the BLOCK is already on disk from staging; leave it.
            assert include_quality is True and recheck_calibration is False
        else:
            # later rounds: fab + calibration only (quality-once). The fix removed the block.
            assert include_quality is False and recheck_calibration is True
            _patch_audit(mp, audit={"verdict": "PASS", "judge_ran": True, "gate_blocks": 0,
                                    "block_findings": 0, "findings": []})

    fixes = []

    def fix_fn(job_id, finding):
        fixes.append(finding)
        return "answer:ok"

    notify = _Recorder()
    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn, fix_fn=fix_fn,
                                    notify_fn=notify, manifest_path=mp)
    assert tag == "converged"
    assert len(fixes) == 1  # one fix applied in round 1
    rec = _read(mp)
    assert rec["convergence"]["state"] == "converged"
    assert rec["convergence"]["rounds"] == 2
    # the converged record must ACTUALLY pass verify_ready (the gate, not a fab-only check).
    assert verify_ready(rec, _cfg)[0] is True
    assert notify.calls == []  # a clean converge never notifies


# ======================================================================================
# 6 (brief). Quality FLAG only (no BLOCKs) + verify_ready PASS -> "converged" round 1, no fix.
#    A FLAG never drives a round (quality-once, §3).
# ======================================================================================

def test_quality_flag_only_converges_round1_no_fix(career_tree):
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    # quality_audit is FLAG (advisory) — never blocks; audit clean; verify_ready passes.
    mp = _write(data_dir, _base_record(appdir,
                quality={"verdict": "FLAG", "judge_ran": True, "calibration": [],
                         "summary": "could be tighter"}))

    rounds = {"n": 0}

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        rounds["n"] += 1  # round 1 leaves the already-clean blocks as-is

    def fix_fn(job_id, finding):
        raise AssertionError("a FLAG must never drive a fix")

    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn, fix_fn=fix_fn, manifest_path=mp)
    assert tag == "converged"
    assert rounds["n"] == 1  # converged on the first audit, no extra rounds
    assert _read(mp)["convergence"]["state"] == "converged"


# ======================================================================================
# 2 (brief)/§4. Human-only blocker (work-auth geography) -> "blocked" + human_blocker + notify once.
# ======================================================================================

def test_human_only_blocker_blocks_and_notifies_once(career_tree):
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    # A work-auth question left for the user: verify_ready fails via can_submit's work-auth reason.
    # No unfilled-required (needs_sam empty) so the work-auth gate — not the unfilled gate — is
    # what fires; the unanswered work-auth is signalled by the halt_reason (can_submit's
    # _had_unanswered_work_auth reads halt_reason and classifies it as a real work-auth question).
    rec = _base_record(appdir)
    rec["work_auth"] = [{"field": "sponsor", "answer": "No"}]
    rec["halt_reason"] = "work-auth question needs Sam: are you authorized to work in Australia"
    mp = _write(data_dir, rec)

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        pass  # audit stays clean — the block is the human-only work-auth fact, not a fabrication

    def fix_fn(job_id, finding):
        raise AssertionError("a human-only blocker must never be auto-fixed")

    notify = _Recorder()
    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn, fix_fn=fix_fn,
                                    notify_fn=notify, manifest_path=mp)
    assert tag == "blocked"
    rec2 = _read(mp)
    assert rec2["convergence"]["state"] == "blocked"
    blk = rec2["human_blocker"]
    assert isinstance(blk, dict) and blk["id"].startswith("blk_JOB-001_")
    assert blk["category"] == "work_auth"
    assert len(notify.calls) == 1  # fired exactly once


# ======================================================================================
# 13. DEMOTED (2026-06-22): a degraded LLM judge (BLOCKED, judge_ran=False) with a CLEAN
# deterministic gate is ADVISORY now. converge keys off can_submit / verify_ready, which no longer
# refuse on the LLM verdict / judge_ran — so this CONVERGES rather than blocking. A real
# deterministic gate block (gate_blocks > 0) still blocks (covered elsewhere in this file).
# ======================================================================================

def test_degraded_judge_no_longer_blocks_converges(career_tree):
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    mp = _write(data_dir, _base_record(appdir))

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        # the LLM judge was unavailable -> degraded stamp: BLOCKED verdict + judge_ran False with
        # ZERO deterministic gate blocks. Advisory now -> must converge (clean deterministic gate).
        _patch_audit(mp, audit={"verdict": "BLOCKED", "judge_ran": False, "gate_blocks": 0,
                                "block_findings": 0, "findings": []})

    def fix_fn(job_id, finding):
        raise AssertionError("a degraded judge has no routable finding to fix")

    notify = _Recorder()
    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn, fix_fn=fix_fn,
                                    notify_fn=notify, manifest_path=mp)
    assert tag == "converged", tag
    rec = _read(mp)
    assert rec["convergence"]["state"] == "converged"
    # the record now reads ready: verify_ready/can_submit gate on the deterministic gate only.
    assert verify_ready(rec, _cfg)[0] is True
    assert notify.calls == []  # a clean converge never notifies (no human-attention blocker)


# ======================================================================================
# 10. A fabricating "fix" (blocks never shrink) -> never converges -> exhausted, NOT converged.
# ======================================================================================

def test_fabricating_fix_never_converges(career_tree):
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    # Every round the audit shows a fabrication BLOCK that the "fix" fails to remove (it re-gates as a
    # BLOCK on its own output). Modelled by an audit plan whose block set never shrinks.
    mp = _write(data_dir, _base_record(appdir,
                audit={"verdict": "BLOCKED", "judge_ran": True, "gate_blocks": 0,
                       "block_findings": 1, "findings": [_fab_block()]}))

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        # the block persists every round (the fabricating fix never clears it)
        _patch_audit(mp, audit={"verdict": "BLOCKED", "judge_ran": True, "gate_blocks": 0,
                                "block_findings": 1, "findings": [_fab_block()]})

    applied = {"n": 0}

    def fix_fn(job_id, finding):
        applied["n"] += 1
        return "answer:ok"  # claims success but the next audit still shows the block

    notify = _Recorder()
    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn, fix_fn=fix_fn,
                                    notify_fn=notify, manifest_path=mp, max_rounds=3)
    assert tag == "exhausted", tag           # NEVER converged
    rec = _read(mp)
    assert rec["convergence"]["state"] == "exhausted"
    assert verify_ready(rec, _cfg)[0] is False
    assert len(notify.calls) == 1


# ======================================================================================
# Iterate residual: when an engine-own fix exhausts still-blocked, its enriched tag carries the
# classified residual; the exhausted blocker names the right class (unsupportable -> rewrite/drop).
# ======================================================================================

def test_exhausted_blocker_names_classified_residual(career_tree):
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    mp = _write(data_dir, _base_record(appdir,
                audit={"verdict": "BLOCKED", "judge_ran": True, "gate_blocks": 0,
                       "block_findings": 1, "findings": [_fab_block()]}))

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        # block never shrinks -> strict-shrink exhaust after two non-shrinking rounds.
        _patch_audit(mp, audit={"verdict": "BLOCKED", "judge_ran": True, "gate_blocks": 0,
                                "block_findings": 1, "findings": [_fab_block()]})

    def fix_fn(job_id, finding):
        # the regen iterated K attempts and exhausted as 'unsupportable'.
        return "answer:fail:unsupportable"

    notify = _Recorder()
    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn, fix_fn=fix_fn,
                                    notify_fn=notify, manifest_path=mp, max_rounds=3)
    assert tag == "exhausted", tag
    rec = _read(mp)
    blk = rec["human_blocker"]
    # The blunt "convergence stalled" is replaced by the residual-aware rewrite-or-drop message.
    assert "rewrite or drop" in (blk.get("blocking_reason") or "")
    assert blk["category"] == "calibration_unfixable"


# ======================================================================================
# 3. Cap reached still dirty -> "exhausted" (the bounded backstop). Distinct from the strict-shrink
#    early-exit: here each round SHRINKS but never reaches clean within the cap.
# ======================================================================================

def test_cap_reached_still_dirty_exhausted(career_tree):
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    # Round-scripted audit: each round STRICTLY SHRINKS by one block (5,4,3,...) but never reaches
    # zero within the cap (max_rounds=3) -> exhausted at the cap, not converged. This exercises the
    # bounded backstop distinctly from the strict-shrink early-exit (which needs a NON-shrinking round).
    rounds = {"n": 0}

    def _audit_with(n):
        finds = [_fab_block(question=f"Q{i}") for i in range(n)]
        return {"verdict": "BLOCKED" if n else "PASS", "judge_ran": True, "gate_blocks": 0,
                "block_findings": n, "findings": finds}

    mp = _write(data_dir, _base_record(appdir, audit=_audit_with(5)))

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        rounds["n"] += 1
        # round 1 sees 5, round 2 sees 4, round 3 sees 3, the post-cap re-audit sees 2 — always dirty.
        _patch_audit(mp, audit=_audit_with(max(2, 6 - rounds["n"])))

    fixes = {"n": 0}

    def fix_fn(job_id, finding):
        fixes["n"] += 1
        return "answer:ok"

    notify = _Recorder()
    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn, fix_fn=fix_fn,
                                    notify_fn=notify, manifest_path=mp, max_rounds=3)
    assert tag == "exhausted", tag
    rec = _read(mp)
    assert rec["convergence"]["state"] == "exhausted"
    assert rec["convergence"]["rounds"] == 3  # the cap


# ======================================================================================
# Lock: converge refuses to start while a user edit is mid-flight (is_edit_in_flight True).
# ======================================================================================

def test_refuses_when_edit_in_flight(career_tree, monkeypatch):
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    from datetime import datetime
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    rec = _base_record(appdir)
    rec["custom_qs"] = [{"q": "Why us?", "edit_request": "tighten", "edit_request_at": now}]
    mp = _write(data_dir, rec)

    def audit_fn(job_id, **k):
        raise AssertionError("converge must not start while an edit is in flight")

    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn,
                                    fix_fn=lambda *a: None, manifest_path=mp)
    assert tag == "skipped"
    rec2 = _read(mp)
    # no convergence block written — the loop never started.
    assert rec2.get("convergence") in (None, {}) or "state" not in (rec2.get("convergence") or {})


# ======================================================================================
# Non-raising: fix_fn raising -> state="error" + blocker, stage NOT crashed.
# ======================================================================================

def test_fix_fn_raising_yields_error_state(career_tree):
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    mp = _write(data_dir, _base_record(appdir,
                audit={"verdict": "BLOCKED", "judge_ran": True, "gate_blocks": 0,
                       "block_findings": 1, "findings": [_fab_block()]}))

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        pass  # leave the block on disk so a fix is attempted

    def fix_fn(job_id, finding):
        raise RuntimeError("regen exploded")

    notify = _Recorder()
    # must NOT raise out
    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn, fix_fn=fix_fn,
                                    notify_fn=notify, manifest_path=mp)
    assert tag == "error"
    rec = _read(mp)
    assert rec["convergence"]["state"] == "error"
    assert isinstance(rec.get("human_blocker"), dict)
    assert len(notify.calls) == 1


# ======================================================================================
# Guard: a non-success stage (needs_sam) is skipped — nothing to converge.
# ======================================================================================

def test_skips_non_success_stage(career_tree):
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    mp = _write(data_dir, _base_record(appdir, status="needs_sam"))

    def audit_fn(job_id, **k):
        raise AssertionError("must not audit a non-success stage")

    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn,
                                    fix_fn=lambda *a: None, manifest_path=mp)
    assert tag == "skipped"


# ======================================================================================
# Non-raising: an audit_fn crash -> state="error", stage NOT crashed.
# ======================================================================================

def test_audit_fn_raising_yields_error_state(career_tree):
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    mp = _write(data_dir, _base_record(appdir))

    def audit_fn(job_id, **k):
        raise RuntimeError("claude -p down")

    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn,
                                    fix_fn=lambda *a: None, manifest_path=mp)
    assert tag == "error"
    assert _read(mp)["convergence"]["state"] == "error"


# ======================================================================================
# QUALITY-DIMENSION DRIVE — REASONED CONVERGENCE to the BEST HONEST package (2026-06-14).
#
# REVISED from the prior "exactly one pass" spec. Once the BLOCK floor is clear and verify_ready
# PASSES, the drive LOOPS applying grounded quality-dim fixes (dimensions[*] score<=3 + concrete
# `fix`), RE-JUDGING after each round, UNTIL one of FOUR stop conditions: (1) CONVERGED, (2) GROUNDED
# CEILING (every remaining fix is ungroundable -> residual honest FLAG left, submit NOT blocked),
# (3) DIMINISHING RETURNS (a re-judge raised no score), (4) CAP (MAX_QUALITY_ROUNDS=3). A residual
# FLAG NEVER blocks submit. The marker (converged/quality_converged) means quality-converged so a
# re-stage skips the drive. Stops ARE the anti-treadmill guarantee, not a one-pass cap.
# ======================================================================================


def _q_dim_flag(**over):
    """A quality_audit with ONE grounded dimension FLAG (specificity score<=3 + concrete fix).
    verdict FLAG so the package is submittable; calibration empty so no BLOCK drives the loop."""
    q = {
        "verdict": "FLAG", "judge_ran": True, "calibration": [],
        "summary": "specificity could be tighter",
        "dimensions": {
            "jd_coverage": {"score": 4, "note": "ok", "fix": ""},
            "fit": {"score": 4, "note": "ok", "fix": ""},
            "specificity": {"score": 3, "note": "bullets are vague",
                            "fix": "quantify the Meridian driver-face outcome with the real metric"},
            "voice": {"score": 4, "note": "ok", "fix": ""},
        },
    }
    q.update(over)
    return q


def _q_dims(scores: dict, fixes: dict = None):
    """Build a quality_audit from a {dim: score} map (+ optional {dim: fix}). verdict FLAG when any
    dim <=3, else PASS — the drive reads dimensions, not the verdict."""
    fixes = fixes or {}
    dims = {d: {"score": scores.get(d, 4), "note": "",
                "fix": fixes.get(d, "")} for d in ("jd_coverage", "fit", "specificity", "voice")}
    verdict = "FLAG" if any(scores.get(d, 4) <= 3 for d in scores) else "PASS"
    return {"verdict": verdict, "judge_ran": True, "calibration": [], "summary": "", "dimensions": dims}


def test_quality_drive_loops_multiple_low_dims_until_converged(career_tree):
    """(a) MULTIPLE groundable low dimensions: the drive LOOPS, improving them across rounds (not a
    single fixed pass) until no low dim remains, then converges. Two low dims that take two rounds to
    clear prove it is a LOOP, not a one-pass cap, and the record is marked converged."""
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    start = _q_dims({"jd_coverage": 4, "fit": 3, "specificity": 2, "voice": 4},
                    {"fit": "name the team's real domain overlap", "specificity": "quantify the metric"})
    mp = _write(data_dir, _base_record(appdir, quality_audit=start))

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        pass  # BLOCK floor already clean -> hands straight to the quality drive

    rejudge = {"n": 0}

    def quality_judge_fn(job_id):
        # round-1 re-judge lifts specificity 2->4 (fit still 3); round-2 re-judge lifts fit 3->4.
        rejudge["n"] += 1
        if rejudge["n"] == 1:
            _patch_audit(mp, quality=_q_dims({"jd_coverage": 4, "fit": 3, "specificity": 4, "voice": 4},
                                             {"fit": "name the team's real domain overlap"}))
        else:
            _patch_audit(mp, quality=_q_dims({"jd_coverage": 4, "fit": 4, "specificity": 4, "voice": 4}))

    qfixes = []

    def quality_fix_fn(job_id, finding):
        qfixes.append(finding.get("dimension"))
        return "content:ok"

    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn, quality_fix_fn=quality_fix_fn,
                                    quality_judge_fn=quality_judge_fn, manifest_path=mp)
    assert tag == "converged"
    assert rejudge["n"] == 2                       # LOOPED two quality rounds (not one fixed pass)
    assert "specificity" in qfixes and "fit" in qfixes
    rec = _read(mp)
    assert rec["convergence"]["state"] == "converged"
    assert verify_ready(rec, _cfg)[0] is True


def test_quality_drive_grounded_ceiling_preserves_flag_submit_unblocked(career_tree):
    """(b) An UNGROUNDABLE low dimension (the stubbed fab-gate rejects the fix): the drive stops at
    the GROUNDED CEILING, original content preserved, the residual honest FLAG left in place, and
    submit is NOT blocked."""
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    mp = _write(data_dir, _base_record(appdir, quality_audit=_q_dim_flag()))

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        pass

    fixes = {"n": 0}

    def quality_fix_fn(job_id, finding):
        # regen re-gated the rewrite as fabrication and rejected it -> the dim can't be lifted.
        fixes["n"] += 1
        return "content:fail:unsupportable"

    rejudge = {"n": 0}

    def quality_judge_fn(job_id):
        rejudge["n"] += 1  # must NOT be reached — nothing landed to re-judge

    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn, quality_fix_fn=quality_fix_fn,
                                    quality_judge_fn=quality_judge_fn, manifest_path=mp)
    assert tag == "quality_converged"                  # an honest stop, not a failure
    assert fixes["n"] == 1                             # tried the fix once, hit the ceiling
    assert rejudge["n"] == 0                            # no re-judge when nothing landed
    rec = _read(mp)
    assert rec["convergence"]["state"] == "quality_converged"
    assert rec["convergence"]["quality_stop"] == "grounded_ceiling"
    assert rec["quality_audit"]["verdict"] == "FLAG"   # residual honest FLAG preserved
    assert verify_ready(rec, _cfg)[0] is True           # submit NOT blocked by the FLAG


def test_quality_drive_diminishing_returns_halts_before_cap(career_tree):
    """(c) DIMINISHING RETURNS: a fix lands but the re-judge raises NO score (same low dim, same
    score). The drive halts BEFORE the cap rather than churning."""
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    mp = _write(data_dir, _base_record(appdir, quality_audit=_q_dim_flag()))

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        pass

    def quality_fix_fn(job_id, finding):
        return "content:ok"  # the fix "applies" but doesn't move the needle

    rejudge = {"n": 0}

    def quality_judge_fn(job_id):
        rejudge["n"] += 1
        _patch_audit(mp, quality=_q_dim_flag())  # specificity stays 3 -> no net improvement

    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn, quality_fix_fn=quality_fix_fn,
                                    quality_judge_fn=quality_judge_fn, manifest_path=mp)
    assert tag == "quality_converged"
    # round 1: apply + re-judge (rejudge=1); round 2: scores unchanged -> diminishing-returns STOP
    # BEFORE the cap (which would be 3 quality rounds).
    assert rejudge["n"] == 1
    rec = _read(mp)
    assert rec["convergence"]["quality_stop"] == "diminishing_returns"
    assert verify_ready(rec, _cfg)[0] is True


def test_quality_drive_cap_bounds_pathological_judge(career_tree):
    """(d) The CAP (MAX_QUALITY_ROUNDS=3) bounds a pathological judge that raises a NEW dim each round
    (so diminishing-returns never fires) yet ALWAYS leaves one low dim. The cap is the backstop."""
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    mp = _write(data_dir, _base_record(appdir,
                quality_audit=_q_dims({"jd_coverage": 1, "fit": 1, "specificity": 1, "voice": 3},
                                {d: f"fix {d}" for d in ("jd_coverage", "fit", "specificity", "voice")})))

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        pass

    rejudge = {"n": 0}

    def quality_judge_fn(job_id):
        # raise one MORE dim to 4 each round (jd@r1, fit@r2, spec@r3) so every round shows net
        # progress, but keep 'voice' low with a fix so a low dim is always outstanding.
        rejudge["n"] += 1
        n = rejudge["n"]
        scores = {"jd_coverage": 4 if n >= 1 else 1, "fit": 4 if n >= 2 else 1,
                  "specificity": 4 if n >= 3 else 1, "voice": 3}
        _patch_audit(mp, quality=_q_dims(scores, {d: f"fix {d}" for d in
                     ("jd_coverage", "fit", "specificity", "voice") if scores[d] <= 3}))

    def quality_fix_fn(job_id, finding):
        return "content:ok"

    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn, quality_fix_fn=quality_fix_fn,
                                    quality_judge_fn=quality_judge_fn, manifest_path=mp)
    assert tag == "quality_converged"
    rec = _read(mp)
    assert rec["convergence"]["quality_stop"] == "cap"        # bounded by the cap, not converged
    assert rejudge["n"] == converge.MAX_QUALITY_ROUNDS        # exactly the cap of quality rounds
    assert verify_ready(rec, _cfg)[0] is True                  # still submittable


def test_quality_drive_skipped_on_already_converged_record(career_tree):
    """(e) A re-stage of a record ALREADY in a clean terminal convergence state (quality_converged)
    does NOT re-run the drive — re-judging would re-spawn the treadmill the stops exist to prevent."""
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    rec0 = _base_record(appdir, quality_audit=_q_dim_flag(),
                        convergence={"state": "quality_converged", "rounds": 2,
                                     "quality_stop": "grounded_ceiling"})
    mp = _write(data_dir, rec0)

    calls = {"qfix": 0, "rejudge": 0}

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        raise AssertionError("an already-converged record must not be re-audited")

    tag = converge.converge_quality(
        "JOB-001", audit_fn=audit_fn,
        quality_fix_fn=lambda *a: calls.__setitem__("qfix", calls["qfix"] + 1) or "content:ok",
        quality_judge_fn=lambda *a: calls.__setitem__("rejudge", calls["rejudge"] + 1),
        manifest_path=mp)
    assert tag == "quality_converged"          # returns the existing terminal state
    assert calls == {"qfix": 0, "rejudge": 0}  # drive never re-ran


def test_quality_drive_noop_on_clean_pass(career_tree):
    """(f) A clean quality PASS record (no dimension <=3) -> the drive is a no-op: no fix, no
    re-judge, converges directly with state=converged."""
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    mp = _write(data_dir, _base_record(appdir,
                quality_audit=_q_dims({"jd_coverage": 4, "fit": 4, "specificity": 4, "voice": 4})))

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        pass

    calls = {"qfix": 0, "rejudge": 0}

    tag = converge.converge_quality(
        "JOB-001", audit_fn=audit_fn,
        quality_fix_fn=lambda *a: calls.__setitem__("qfix", calls["qfix"] + 1) or "content:ok",
        quality_judge_fn=lambda *a: calls.__setitem__("rejudge", calls["rejudge"] + 1),
        manifest_path=mp)
    assert tag == "converged"
    assert calls == {"qfix": 0, "rejudge": 0}  # nothing <=3 with a fix -> no regen, no re-judge
    assert _read(mp)["convergence"]["state"] == "converged"


def test_quality_drive_skipped_when_not_stage_success(career_tree):
    """The drive stays OFF the path when the record is not stage-success (the loop skips before it)."""
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    mp = _write(data_dir, _base_record(appdir, status="needs_sam", quality_audit=_q_dim_flag()))

    calls = {"qfix": 0, "rejudge": 0}

    tag = converge.converge_quality(
        "JOB-001", audit_fn=lambda *a, **k: None,
        quality_fix_fn=lambda *a: calls.__setitem__("qfix", calls["qfix"] + 1) or "content:ok",
        quality_judge_fn=lambda *a: calls.__setitem__("rejudge", calls["rejudge"] + 1),
        manifest_path=mp)
    assert tag == "skipped"
    assert calls == {"qfix": 0, "rejudge": 0}


# ======================================================================================
# KEEP-HIGHER-SCORING-DRAFT GUARD (reviewer gap, hit live on JOB-307). The fab re-gate guarantees a
# round's rewrite is GROUNDED, NOT that it scores BETTER — a grounded-but-worse rewrite could silently
# replace a better draft. The drive now snapshots the draft + its score set before a round, re-judges
# after, and REVERTS to the snapshot if the round didn't STRICTLY improve (beyond a +/-1 noise band).
# Invariant: the drive NEVER hands back a draft worse than staging/the prior round produced.
# ======================================================================================


def test_quality_drive_reverts_when_round_lowers_score(career_tree):
    """(a) A round whose grounded rewrite LOWERS a score (specificity 3 -> 1, no dim rises): the drive
    REVERTS to the pre-round snapshot (the better/prior draft) and STOPS. The landed quality_audit
    must equal the pre-round snapshot — proof the worse draft was rolled back, not left in place."""
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    pre = _q_dims({"jd_coverage": 4, "fit": 4, "specificity": 3, "voice": 4},
                  {"specificity": "quantify the Meridian driver-face metric"})
    pre["summary"] = "PRE-ROUND DRAFT"   # a marker that uniquely identifies the snapshotted block
    mp = _write(data_dir, _base_record(appdir, quality_audit=pre))
    snapshot = json.loads(json.dumps(pre))  # what the landed block must equal after revert

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        pass

    qfixes = []

    def quality_fix_fn(job_id, finding):
        qfixes.append(finding.get("dimension"))
        return "content:ok"  # grounded (re-gate passed) but, per the re-judge below, it scored WORSE

    rejudge = {"n": 0}

    def quality_judge_fn(job_id):
        # the grounded rewrite re-judged WORSE: specificity 3 -> 1, nothing rose. A LOWERED draft.
        rejudge["n"] += 1
        worse = _q_dims({"jd_coverage": 4, "fit": 4, "specificity": 1, "voice": 4},
                        {"specificity": "quantify the Meridian driver-face metric"})
        worse["summary"] = "WORSE REWRITE"
        _patch_audit(mp, quality=worse)

    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn, quality_fix_fn=quality_fix_fn,
                                    quality_judge_fn=quality_judge_fn, manifest_path=mp)
    assert tag == "quality_converged"
    assert qfixes == ["specificity"]        # tried the round once
    assert rejudge["n"] == 1                 # re-judged once, saw the worse draft
    rec = _read(mp)
    assert rec["convergence"]["quality_stop"] == "diminishing_returns"
    # THE INVARIANT: the landed draft is the pre-round snapshot, NOT the worse rewrite.
    assert rec["quality_audit"] == snapshot
    assert rec["quality_audit"]["summary"] == "PRE-ROUND DRAFT"
    assert verify_ready(rec, _cfg)[0] is True   # still submittable


def test_quality_drive_keeps_strictly_improved_draft_and_continues(career_tree):
    """(b) A round that STRICTLY improves beyond the noise band (specificity 2 -> 4, +2): the new
    draft is KEPT and the drive CONTINUES to the next round (here clearing the last low dim ->
    converged). Proves a real, beyond-noise gain is not mistaken for noise and reverted."""
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    start = _q_dims({"jd_coverage": 4, "fit": 3, "specificity": 2, "voice": 4},
                    {"fit": "name the team's real domain overlap", "specificity": "quantify the metric"})
    mp = _write(data_dir, _base_record(appdir, quality_audit=start))

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        pass

    rejudge = {"n": 0}

    def quality_judge_fn(job_id):
        # round-1 re-judge lifts specificity 2->4 (+2, beyond noise) AND fit 3->4; round 2 all clear.
        rejudge["n"] += 1
        if rejudge["n"] == 1:
            _patch_audit(mp, quality=_q_dims(
                {"jd_coverage": 4, "fit": 4, "specificity": 4, "voice": 4}))
        else:
            _patch_audit(mp, quality=_q_dims(
                {"jd_coverage": 4, "fit": 4, "specificity": 4, "voice": 4}))

    qfixes = []

    def quality_fix_fn(job_id, finding):
        qfixes.append(finding.get("dimension"))
        return "content:ok"

    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn, quality_fix_fn=quality_fix_fn,
                                    quality_judge_fn=quality_judge_fn, manifest_path=mp)
    assert tag == "converged"               # the kept improvement cleared the low dims
    assert rejudge["n"] == 1                  # round 1 improved+cleared -> converged, no extra round
    rec = _read(mp)
    assert rec["convergence"]["state"] == "converged"
    assert rec["quality_audit"]["dimensions"]["specificity"]["score"] == 4  # the BETTER draft is kept
    assert verify_ready(rec, _cfg)[0] is True


def test_quality_drive_jitter_within_tolerance_reverts_no_churn(career_tree):
    """(c) NONDETERMINISM TOLERANCE: a round whose re-judge only JITTERS within +/-1 (specificity
    3 -> 4, a single point = noise, while a low dim still remains) is NOT counted as improvement. The
    drive reverts to the snapshot and STOPS rather than churning on noise — and does not freeze the
    noisily-changed draft. One round only; the landed draft is the pre-round snapshot."""
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    # specificity 3 (low, fix) and fit 3 (low, fix) so a low dim still remains after a +1 spec jitter.
    pre = _q_dims({"jd_coverage": 4, "fit": 3, "specificity": 3, "voice": 4},
                  {"fit": "name the domain overlap", "specificity": "quantify the metric"})
    pre["summary"] = "PRE-ROUND"
    mp = _write(data_dir, _base_record(appdir, quality_audit=pre))
    snapshot = json.loads(json.dumps(pre))

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        pass

    rejudge = {"n": 0}

    def quality_judge_fn(job_id):
        # +1 jitter on specificity (3->4) but fit stays 3 (low). No dim rose BEYOND the +/-1 band.
        rejudge["n"] += 1
        jittered = _q_dims({"jd_coverage": 4, "fit": 3, "specificity": 4, "voice": 4},
                           {"fit": "name the domain overlap"})
        jittered["summary"] = "JITTERED"
        _patch_audit(mp, quality=jittered)

    qfixes = []

    def quality_fix_fn(job_id, finding):
        qfixes.append(finding.get("dimension"))
        return "content:ok"

    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn, quality_fix_fn=quality_fix_fn,
                                    quality_judge_fn=quality_judge_fn, manifest_path=mp)
    assert tag == "quality_converged"
    assert rejudge["n"] == 1                  # exactly one round — no churn on the +1 noise
    rec = _read(mp)
    assert rec["convergence"]["quality_stop"] == "diminishing_returns"
    # reverted to the snapshot: the +1-jitter draft was treated as noise, not frozen.
    assert rec["quality_audit"] == snapshot
    assert rec["quality_audit"]["summary"] == "PRE-ROUND"
    assert verify_ready(rec, _cfg)[0] is True
