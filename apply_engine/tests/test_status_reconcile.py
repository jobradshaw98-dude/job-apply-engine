# -*- coding: utf-8 -*-
"""Bug #4 — status=ready_to_submit must be COUPLED to "the mandatory audit ran and passed".

The audit lives INSIDE converge.converge_quality. converge has skip paths (rec missing / status !=
ready_to_submit / edit-in-flight / converge_lock LockTimeout) and can return error/paused/exhausted/
blocked. When it skips/errors, the staged record keeps status=ready_to_submit but has NO PASS audit
verdict — so the dashboard shows green "ready" while finish.can_submit (the authority, FAILS CLOSED
on verdict != PASS) would refuse the submit. The STATUS lies.

THE INVARIANT (reconciliation): a staged record may carry status `ready_to_submit` ONLY if
finish.can_submit(record) returns (True, ""). cli._reconcile_ready_status downgrades a lying
ready_to_submit to needs_sam (a status the dashboard already treats as not-review-ready) using
finish.can_submit as the single source of truth — it must NEVER touch can_submit's fail-closed logic.

  (a) converge "skipped" AND no PASS audit  -> status downgraded OFF ready_to_submit
  (b) converge "converged" with PASS fab+quality audits -> ready_to_submit STANDS
  (c) a BLOCKED verdict -> not ready (can_submit already refuses; reconciliation respects it)
  (d) the downgrade NEVER upgrades a non-ready status, and never touches a submitted record
"""
import json
from pathlib import Path

import apply_engine.cli as cli
from apply_engine import config as _cfg


def _record(appdir: Path, **over) -> dict:
    rec = {
        "job_id": "JOB-246",
        "company": "Acme",
        "status": "ready_to_submit",
        "submitted": False,
        "uploaded_docs": [
            {"doc": "resume", "path": str(appdir / "SAM_RIVERA_Resume.pdf"),
             "name": "SAM_RIVERA_Resume.pdf"},
            {"doc": "cover", "path": str(appdir / "SAM_RIVERA_Cover_Letter.pdf"),
             "name": "SAM_RIVERA_Cover_Letter.pdf"},
        ],
        "work_auth": [{"field": "sponsor", "answer": "No"}],
        "custom_qs": [{"q": "Why us?", "kind": "essay", "status": "answered", "value": "x"}],
        "unfilled_required": [],
        "needs_sam": [],
        "audit": {"verdict": "PASS", "judge_ran": True, "gate_blocks": 0,
                  "block_findings": 0, "findings": []},
        "quality_audit": {"verdict": "PASS", "judge_ran": True, "calibration": []},
    }
    rec.update(over)
    return rec


def _setup(tmp_path, monkeypatch, rec):
    appdir = tmp_path / "career" / "applications" / "APP-001-Acme"
    appdir.mkdir(parents=True)
    (appdir / "SAM_RIVERA_Resume.pdf").write_text("R", encoding="utf-8")
    (appdir / "SAM_RIVERA_Cover_Letter.pdf").write_text("C", encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(_cfg, "ARIA_DATA", data_dir)
    monkeypatch.setattr(cli.config, "ARIA_DATA", data_dir)
    mp = data_dir / "staged_applications.json"
    mp.write_text(json.dumps([rec], indent=2), encoding="utf-8")
    return appdir, mp


def _status(mp: Path) -> str:
    return json.loads(mp.read_text(encoding="utf-8"))[0]["status"]


# fix the appdir path on the record's uploaded_docs to the real tmp tree
def _rec_for(tmp_path, **over):
    appdir = tmp_path / "career" / "applications" / "APP-001-Acme"
    return _record(appdir, **over)


# ======================================================================================
# (a) DEMOTION (2026-06-22): an absent/None LLM verdict with a CLEAN deterministic gate is no
# longer a reason to downgrade — can_submit gates on the deterministic gate only, so the record
# STANDS. The downgrade now fires ONLY on a positive deterministic gate block (see (c)).
# ======================================================================================

def test_skipped_with_no_llm_verdict_but_clean_gate_stands(tmp_path, monkeypatch):
    # FLIPPED from test_skipped_with_no_pass_audit_downgrades. The JOB-246 LockTimeout repro:
    # converge skipped, LLM verdict wiped to None — but gate_blocks == 0. can_submit passes now
    # (LLM verdict is advisory), so reconciliation must NOT downgrade.
    rec = _rec_for(tmp_path, audit={"verdict": None, "judge_ran": False, "gate_blocks": 0})
    _, mp = _setup(tmp_path, monkeypatch, rec)
    new = cli._reconcile_ready_status("JOB-246", converge_tag="skipped")
    assert _status(mp) == "ready_to_submit"
    assert new == "ready_to_submit"


def test_error_with_no_audit_downgrades_gate_never_ran(tmp_path, monkeypatch):
    # FLIPPED back (2026-06-22 reviewer fail-closed fix). No audit at all means the deterministic
    # gate NEVER RAN — not that it passed. can_submit now BLOCKS a missing stamp, so reconciliation
    # must DOWNGRADE the review-ready record off green rather than leave an unchecked package
    # showing submittable. Contrast test_skipped_with_no_llm_verdict_but_clean_gate_stands above,
    # which carries an EXPLICIT gate_blocks: 0 stamp and correctly STANDS.
    rec = _rec_for(tmp_path)
    rec.pop("audit")  # no audit at all -> gate never ran
    _, mp = _setup(tmp_path, monkeypatch, rec)
    cli._reconcile_ready_status("JOB-246", converge_tag="error")
    assert _status(mp) == "needs_sam"


# ======================================================================================
# (b) converge converged with PASS fab+quality -> ready_to_submit STANDS
# ======================================================================================

def test_converged_with_pass_audit_stands(tmp_path, monkeypatch):
    rec = _rec_for(tmp_path)  # default record passes can_submit
    _, mp = _setup(tmp_path, monkeypatch, rec)
    new = cli._reconcile_ready_status("JOB-246", converge_tag="converged")
    assert _status(mp) == "ready_to_submit"  # a real PASS is NOT downgraded
    assert new == "ready_to_submit"


# ======================================================================================
# (c) a BLOCKED verdict -> not ready (can_submit refuses; reconciliation respects it)
# ======================================================================================

def test_blocked_verdict_not_ready(tmp_path, monkeypatch):
    rec = _rec_for(tmp_path, audit={"verdict": "BLOCKED", "judge_ran": True,
                                    "gate_blocks": 1, "block_findings": 1, "findings": []})
    _, mp = _setup(tmp_path, monkeypatch, rec)
    cli._reconcile_ready_status("JOB-246", converge_tag="blocked")
    assert _status(mp) != "ready_to_submit"


# ======================================================================================
# (d) one-way safety: never upgrades a non-ready status, never touches submitted
# ======================================================================================

def test_does_not_upgrade_non_ready(tmp_path, monkeypatch):
    rec = _rec_for(tmp_path, status="needs_input")
    _, mp = _setup(tmp_path, monkeypatch, rec)
    cli._reconcile_ready_status("JOB-246", converge_tag="skipped")
    assert _status(mp) == "needs_input"  # untouched — reconciliation only DOWNGRADES from ready


def test_does_not_touch_submitted(tmp_path, monkeypatch):
    rec = _rec_for(tmp_path, status="ready_to_submit", submitted=True,
                   audit={"verdict": None, "judge_ran": False})
    _, mp = _setup(tmp_path, monkeypatch, rec)
    cli._reconcile_ready_status("JOB-246", converge_tag="skipped")
    assert _status(mp) == "ready_to_submit"  # a submitted record is never rewritten
