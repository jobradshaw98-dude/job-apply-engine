# -*- coding: utf-8 -*-
"""Phase 4a — tests for verify_ready, the SINGLE readiness authority (M4), and its three G-hooks.

verify_ready is a STRICT SUPERSET of verify_submittable:
    verify_submittable PASS AND can_submit (True,"") AND zero fab/calibration BLOCKs
    AND G1 reconcile clean AND G2 compliance PASS AND G3 cover-length PASS.

The G-hooks are PASS-WHEN-ABSENT stubs in Phase 4a (G1/G2/G3 data lands in Phase 4b). These tests
PIN the two contracts that matter:
  1. BACKWARD-SAFE: a record that passes verify_submittable+can_submit with NO G-data passes
     verify_ready unchanged — no existing ready card regresses (the pass-when-absent guarantee).
  2. WIRED: each G-hook fails verify_ready when its 4b data is present-and-failing (and a stubbed
     failing hook flips verify_ready to FAIL), proving 4b only has to fill the hook bodies.

PURE assertions — no browser, no LLM, no live form. Reuses the on-disk tailored/master PDF tree
from the submit-integrity contract test so the PDF-integrity invariant is exercised for real.
"""
from pathlib import Path

import pytest

from apply_engine.finish import (verify_ready, verify_submittable, can_submit,
                                  _g1_reconcile_ok, _g2_compliance_ok, _g3_cover_ok)


class _CfgStub:
    def __init__(self, pkg_dir: Path):
        self.PKG_DIR = pkg_dir


@pytest.fixture()
def career_tree(tmp_path):
    """Same throwaway career tree as test_submit_integrity: a master + a tailored resume/cover on
    disk under applications/APP-001-Acme/."""
    root = tmp_path / "career"
    pkg = root / "apply_engine"
    pkg.mkdir(parents=True)
    master = root / "Sam_Rivera_Resume_Master.pdf"
    master.write_text("MASTER", encoding="utf-8")
    appdir = root / "applications" / "APP-001-Acme"
    appdir.mkdir(parents=True)
    tailored_resume = appdir / "SAM_RIVERA_Resume.pdf"
    tailored_resume.write_text("TAILORED", encoding="utf-8")
    tailored_cover = appdir / "SAM_RIVERA_Cover_Letter.pdf"
    tailored_cover.write_text("COVER", encoding="utf-8")
    return _CfgStub(pkg), {"master": master, "tailored_resume": tailored_resume,
                           "tailored_cover": tailored_cover, "appdir": appdir}


def _ready_record(paths, **over):
    """A record that PASSES verify_submittable + can_submit and carries NO G-data — the baseline
    'already ready today' card. With pass-when-absent G-hooks this must also pass verify_ready."""
    rec = {
        "job_id": "JOB-001",
        "status": "ready_to_submit",
        "submitted": False,
        "uploaded_docs": [
            {"doc": "resume", "path": str(paths["tailored_resume"]),
             "name": "SAM_RIVERA_Resume.pdf"},
            {"doc": "cover", "path": str(paths["tailored_cover"]),
             "name": "SAM_RIVERA_Cover_Letter.pdf"},
        ],
        "work_auth": [{"field": "sponsor", "answer": "No"}],
        "custom_qs": [],
        "unfilled_required": [],
        "needs_sam": [],
        "audit": {"verdict": "PASS", "judge_ran": True, "findings": [],
                  "gate_blocks": 0, "block_findings": 0},
        "quality_audit": {"verdict": "PASS", "judge_ran": True},
    }
    rec.update(over)
    return rec


# ======================================================================================
# BACKWARD-SAFE: the superset never regresses an existing ready card
# ======================================================================================

def test_ready_record_with_no_g_data_passes(career_tree):
    """The core backward-compat guarantee: a record that passes verify_submittable + can_submit and
    has NO G1/G2/G3 data passes verify_ready unchanged (pass-when-absent hooks)."""
    cfg, paths = career_tree
    rec = _ready_record(paths)
    # sanity: it really does pass the two base gates
    assert verify_submittable(rec, cfg)[0] is True
    assert can_submit(rec) == (True, "")
    ok, reason = verify_ready(rec, cfg)
    assert ok is True, reason
    assert reason == ""


def test_verify_ready_is_superset_not_weaker(career_tree):
    """verify_ready can NEVER pass a record verify_submittable fails — a record failing the base
    gate fails verify_ready, with a reason. (Updated 2026-06-22: the quality-audit invariant was
    demoted, so this uses a deterministic gate block — the surviving content gate — to make
    verify_submittable fail.)"""
    cfg, paths = career_tree
    rec = _ready_record(paths, audit={"verdict": "PASS", "judge_ran": True, "gate_blocks": 1,
                                      "findings": []})  # fails verify_submittable (deterministic gate)
    assert verify_submittable(rec, cfg)[0] is False
    ok, reason = verify_ready(rec, cfg)
    assert ok is False
    assert "deterministic gate" in reason.lower()


def test_verify_ready_fails_on_master_resume(career_tree):
    """The master-attach class still blocks through verify_ready (it inherits the PDF invariant)."""
    cfg, paths = career_tree
    rec = _ready_record(paths)
    rec["uploaded_docs"] = [{"doc": "resume", "path": str(paths["master"]),
                             "name": "Sam_Rivera_Resume_Master.pdf"}]
    ok, reason = verify_ready(rec, cfg)
    assert ok is False
    assert "master" in reason.lower()


def test_verify_ready_fails_on_fab_block_count(career_tree):
    """A record with outstanding fabrication BLOCK findings fails verify_ready even if (hypothet-
    ically) the verdict slipped — the explicit zero-BLOCK superset check. Here the BLOCKED verdict
    also trips verify_submittable; the reason names the block class either way."""
    cfg, paths = career_tree
    rec = _ready_record(paths, audit={"verdict": "BLOCKED", "judge_ran": True,
                                      "gate_blocks": 2, "block_findings": 0, "findings": []})
    ok, reason = verify_ready(rec, cfg)
    assert ok is False
    assert "block" in reason.lower() or "fabrication" in reason.lower()


# ======================================================================================
# G-HOOKS: pass-when-absent, and fail-when-present-and-failing (the 4b wiring)
# ======================================================================================

def test_g1_pass_when_absent():
    assert _g1_reconcile_ok({})[0] is True
    assert _g1_reconcile_ok({"reconcile": None})[0] is True


def test_g1_pass_when_clean():
    assert _g1_reconcile_ok({"reconcile": {"clean": True}})[0] is True


def test_g1_fail_when_not_clean():
    ok, reason = _g1_reconcile_ok({"reconcile": {"clean": False, "escalations": ["x", "y"]}})
    assert ok is False
    assert "g1" in reason.lower() or "reconcil" in reason.lower()


def test_g2_pass_when_absent():
    assert _g2_compliance_ok({})[0] is True
    assert _g2_compliance_ok({"compliance": None})[0] is True


def test_g2_pass_when_ok():
    assert _g2_compliance_ok({"compliance": {"ok": True}})[0] is True


def test_g2_fail_when_violation():
    ok, reason = _g2_compliance_ok({"compliance": {"ok": False,
                                                   "violations": ["essay 150 < 200-400"]}})
    assert ok is False
    assert "g2" in reason.lower() or "compliance" in reason.lower()


def test_g3_pass_when_absent():
    assert _g3_cover_ok({})[0] is True
    assert _g3_cover_ok({"cover": {"paragraphs": ["x"]}})[0] is True  # no autofit key -> absent


def test_g3_pass_when_zero_adjustments():
    assert _g3_cover_ok({"cover": {"autofit_adjustments": 0}})[0] is True


def test_g3_fail_when_shrunk():
    ok, reason = _g3_cover_ok({"cover": {"autofit_adjustments": 3}})
    assert ok is False
    assert "g3" in reason.lower() or "cover" in reason.lower()


# ======================================================================================
# verify_ready WIRING: a present-and-failing G-gate flips verify_ready to FAIL
# ======================================================================================

def test_verify_ready_fails_when_g1_present_and_failing(career_tree):
    cfg, paths = career_tree
    rec = _ready_record(paths, reconcile={"clean": False, "escalations": ["wrong work-auth field"]})
    ok, reason = verify_ready(rec, cfg)
    assert ok is False
    assert "g1" in reason.lower() or "reconcil" in reason.lower()


def test_verify_ready_fails_when_g2_present_and_failing(career_tree):
    cfg, paths = career_tree
    rec = _ready_record(paths, compliance={"ok": False, "violations": ["essay under length"]})
    ok, reason = verify_ready(rec, cfg)
    assert ok is False
    assert "g2" in reason.lower() or "compliance" in reason.lower()


def test_verify_ready_fails_when_g3_present_and_failing(career_tree):
    cfg, paths = career_tree
    rec = _ready_record(paths)
    rec["cover"] = {"paragraphs": ["..."], "autofit_adjustments": 2}
    ok, reason = verify_ready(rec, cfg)
    assert ok is False
    assert "g3" in reason.lower() or "cover" in reason.lower()


def test_verify_ready_fails_when_stubbed_g_hook_fails(career_tree, monkeypatch):
    """Proves the WIRING independent of any specific G-data shape: monkeypatch a G-hook to FAIL and
    verify_ready must propagate that failure. This is the 4b contract — fill a hook body, the gate
    tightens, no call-site change."""
    cfg, paths = career_tree
    rec = _ready_record(paths)
    assert verify_ready(rec, cfg)[0] is True  # passes before the stub
    import apply_engine.finish as fin
    monkeypatch.setattr(fin, "_g2_compliance_ok", lambda r: (False, "STUBBED G2 FAIL"))
    ok, reason = verify_ready(rec, cfg)
    assert ok is False
    assert "stubbed g2 fail" in reason.lower()


def test_verify_ready_all_g_present_and_passing(career_tree):
    """All three G-gates present AND passing -> verify_ready passes (the fully-4b-populated clean
    case, ahead of 4b)."""
    cfg, paths = career_tree
    rec = _ready_record(paths,
                        reconcile={"clean": True},
                        compliance={"ok": True})
    rec["cover"] = {"paragraphs": ["..."], "autofit_adjustments": 0}
    ok, reason = verify_ready(rec, cfg)
    assert ok is True, reason


# ======================================================================================
# Phase 4b END-TO-END: a captured record with a real G1 mismatch / G2 under-length is NOT ready;
# a clean captured record IS ready. Uses the actual ReconcileResult/ComplianceResult shapes the
# orchestrator capture stores (not hand-stubbed bools) so the gates are proven live.
# ======================================================================================

def _captured_clean_record(paths):
    """A ready record carrying a CLEAN captured live-form model (the orchestrator-capture shape)."""
    from apply_engine.form_spec import FormSpec, FieldSpec
    from apply_engine.reconcile import reconcile_form
    from apply_engine.compliance import check_form_constraints

    spec = FormSpec(ats="greenhouse")
    spec.fields = [
        FieldSpec(key="why", label="Why do you want to work here?", required=True,
                  widget_kind="textarea", constraints={"words": [200, 400]}),
    ]
    rec = _ready_record(
        paths,
        custom_qs=[{"q": "Why do you want to work here?", "kind": "essay",
                    "value": "word " * 300, "status": "answered"}],
    )
    rec["form_spec"] = spec.to_summary()
    rec["reconcile"] = reconcile_form(spec, rec).to_record()
    rec["compliance"] = check_form_constraints(spec, rec).to_record()
    return rec, spec


def test_verify_ready_clean_captured_record_is_ready(career_tree):
    cfg, paths = career_tree
    rec, _ = _captured_clean_record(paths)
    assert rec["reconcile"]["clean"] is True
    assert rec["compliance"]["ok"] is True
    ok, reason = verify_ready(rec, cfg)
    assert ok is True, reason


def test_verify_ready_not_ready_on_g2_under_length(career_tree):
    """A 50-word essay against a stated 200-400 range -> G2 violation -> NOT ready (proves the gate
    is live end-to-end, computed from the real compliance check)."""
    from apply_engine.form_spec import FormSpec, FieldSpec
    from apply_engine.reconcile import reconcile_form
    from apply_engine.compliance import check_form_constraints

    cfg, paths = career_tree
    spec = FormSpec(ats="greenhouse")
    spec.fields = [FieldSpec(key="why", label="Why do you want to work here?", required=True,
                             widget_kind="textarea", constraints={"words": [200, 400]})]
    rec = _ready_record(
        paths,
        custom_qs=[{"q": "Why do you want to work here?", "kind": "essay",
                    "value": "word " * 50, "status": "answered"}],  # 50 < 200
    )
    rec["form_spec"] = spec.to_summary()
    rec["reconcile"] = reconcile_form(spec, rec).to_record()
    rec["compliance"] = check_form_constraints(spec, rec).to_record()
    assert rec["compliance"]["ok"] is False
    ok, reason = verify_ready(rec, cfg)
    assert ok is False
    assert "g2" in reason.lower() or "compliance" in reason.lower()


def test_verify_ready_not_ready_on_g1_mismatch(career_tree):
    """A 253-char narrative staged for a SHORT 'Current employer' field -> G1 mismatched -> NOT
    ready (the §8.1 live defect, caught end-to-end via the real reconcile diff)."""
    from apply_engine.form_spec import FormSpec, FieldSpec
    from apply_engine.reconcile import reconcile_form

    cfg, paths = career_tree
    narrative = ("At Meridian Devices I led R&D product development across multiple programs, owning "
                 "FEA validation, prototyping, and cross-functional execution from concept through "
                 "to manufacturing handoff over several seasons of work here and there.")
    assert len(narrative) > 120
    spec = FormSpec(ats="greenhouse")
    spec.fields = [FieldSpec(key="employer", label="Current employer", required=True,
                             widget_kind="text")]
    rec = _ready_record(
        paths,
        custom_qs=[{"q": "Current employer", "kind": "short_text",
                    "value": narrative, "status": "answered"}],
    )
    rec["form_spec"] = spec.to_summary()
    rec["reconcile"] = reconcile_form(spec, rec).to_record()
    assert rec["reconcile"]["clean"] is False
    assert rec["reconcile"]["mismatches"]  # the actionable list G1 reads
    ok, reason = verify_ready(rec, cfg)
    assert ok is False
    assert "g1" in reason.lower() or "reconcil" in reason.lower()
