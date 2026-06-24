# -*- coding: utf-8 -*-
"""Phase 4b — stage-time live-form CAPTURE in apply_to_job (the G1/G2 inputs).

After fields are filled and BEFORE the brink, the orchestrator calls _capture_form_model, which
runs adapter.enumerate_fields -> reconcile_form -> check_form_constraints and stores COMPACT
summaries on the outcome (-> the manifest record): `form_spec`, `reconcile`, `compliance`.

Two contracts pinned here:
  1. On a clean stage (lever_form fixture reaches ready_to_submit), the outcome carries a populated
     form_spec + reconcile + compliance.
  2. BEST-EFFORT: a forced enumerate_fields exception leaves the stage SUCCEEDING (ready_to_submit)
     with form_spec/reconcile/compliance all None — capture never breaks a stage.

Runs against the live fixture DOM (Playwright), like the other orchestrator tests.
"""
from pathlib import Path

import pytest

from apply_engine.orchestrator import apply_to_job
from apply_engine.source_data import Answers


@pytest.fixture
def answers(tmp_path):
    r = tmp_path / "r.pdf"
    r.write_bytes(b"%PDF")
    return Answers(values={"first_name": "Sam", "last_name": "Rivera",
                           "full_name": "Sam Rivera",
                           "email": "sam.rivera@example.com", "phone": "555-555-0100"},
                   resume_pdf=r, cover_pdf=None)


def test_capture_populates_form_spec_and_reconcile(fixture_server, answers, tmp_path):
    """A clean stage captures the live-form model: form_spec (fields + flags), reconcile (clean
    bool + counts), compliance (ok bool)."""
    job = {"id": "JOB-CAP", "company": "Acme", "title": "Eng",
           "url": f"{fixture_server}/lever_form.html"}
    out = apply_to_job(job=job, answers=answers, runs_root=tmp_path / "runs",
                       profile_dir=tmp_path / "p", headless=True, dry_run=True,
                       ats_override="lever")
    assert out.status == "ready_to_submit"

    # form_spec captured + non-trivial
    assert isinstance(out.form_spec, dict)
    assert out.form_spec.get("n_fields", 0) >= 1
    assert isinstance(out.form_spec.get("fields"), list)
    assert out.form_spec.get("has_resume_field") is True   # lever_form has a file input

    # reconcile captured with the ReconcileResult.to_record() shape the G1 gate reads
    assert isinstance(out.reconcile, dict)
    assert "clean" in out.reconcile
    assert isinstance(out.reconcile.get("clean"), bool)
    assert "mismatches" in out.reconcile
    assert "unfilled_required_live" in out.reconcile

    # compliance captured with the ok bool the G2 gate reads
    assert isinstance(out.compliance, dict)
    assert "ok" in out.compliance
    assert isinstance(out.compliance.get("ok"), bool)


def test_capture_lands_on_manifest_record(fixture_server, answers, tmp_path):
    """The captured summaries flow through build_record onto the flat manifest record (additive)."""
    from apply_engine.staged_manifest import build_record
    job = {"id": "JOB-CAP2", "company": "Acme", "title": "Eng",
           "url": f"{fixture_server}/lever_form.html"}
    out = apply_to_job(job=job, answers=answers, runs_root=tmp_path / "runs",
                       profile_dir=tmp_path / "p", headless=True, dry_run=True,
                       ats_override="lever")
    rec = build_record(out, job, "2026-06-11T12:00:00-07:00")
    assert isinstance(rec["form_spec"], dict)
    assert isinstance(rec["reconcile"], dict)
    assert isinstance(rec["compliance"], dict)


def test_capture_best_effort_exception_does_not_break_stage(fixture_server, answers, tmp_path,
                                                            monkeypatch):
    """A forced enumerate_fields exception must NOT change the stage: it still reaches
    ready_to_submit, and form_spec/reconcile/compliance are all None (capture skipped)."""
    import apply_engine.adapters.lever as lev

    def _boom(self, page):
        raise RuntimeError("forced enumerate failure")

    monkeypatch.setattr(lev.LeverAdapter, "enumerate_fields", _boom)

    job = {"id": "JOB-CAP-BOOM", "company": "Acme", "title": "Eng",
           "url": f"{fixture_server}/lever_form.html"}
    out = apply_to_job(job=job, answers=answers, runs_root=tmp_path / "runs",
                       profile_dir=tmp_path / "p", headless=True, dry_run=True,
                       ats_override="lever")
    # stage SUCCEEDS exactly as before — capture failure is swallowed
    assert out.status == "ready_to_submit"
    assert out.submitted is False
    # no model captured (best-effort proven)
    assert out.form_spec is None
    assert out.reconcile is None
    assert out.compliance is None


def test_capture_exception_logged_not_raised(fixture_server, answers, tmp_path, monkeypatch):
    """The best-effort path logs a 'capture skipped' line and never raises out of apply_to_job."""
    import apply_engine.adapters.lever as lev
    monkeypatch.setattr(lev.LeverAdapter, "enumerate_fields",
                        lambda self, page: (_ for _ in ()).throw(ValueError("nope")))
    job = {"id": "JOB-CAP-LOG", "company": "Acme", "title": "Eng",
           "url": f"{fixture_server}/lever_form.html"}
    out = apply_to_job(job=job, answers=answers, runs_root=tmp_path / "runs",
                       profile_dir=tmp_path / "p", headless=True, dry_run=True,
                       ats_override="lever")
    assert out.status == "ready_to_submit"
    log = Path(out.run_dir) / "audit.jsonl"
    assert log.exists()
    assert "capture" in log.read_text(encoding="utf-8")
