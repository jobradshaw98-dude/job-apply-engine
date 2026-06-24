# -*- coding: utf-8 -*-
"""Tests for the generalized uploaded_docs backfill (2026-06-11).

A tailored cover (or resume) built AFTER the original apply run is never written into a staged
record's uploaded_docs — the run only records what attached live. The backfill registers EVERY
tailored doc that exists on disk for every NON-submitted staged record, so finish._resolve_pdfs
attaches the tailored cover instead of silently dropping it.

These tests drive the PURE planning core (`plan_backfill`) over synthetic records, so no live
manifest / applications.json is touched. They pin: cover-only backfill, idempotency, the
submitted-record skip, and that a master path is never registered.
"""

import os

from apply_engine.backfill_uploaded_docs import plan_backfill


def _appdir(tmp_path, name, *, resume=True, cover=True):
    """Make applications/<name>/ with the tailored PDFs requested. Returns the dir Path."""
    d = tmp_path / "applications" / name
    d.mkdir(parents=True)
    if resume:
        (d / "SAM_RIVERA_Resume.pdf").write_text("R", encoding="utf-8")
    if cover:
        (d / "SAM_RIVERA_Cover_Letter.pdf").write_text("C", encoding="utf-8")
    return d


def test_cover_registered_when_present_on_disk(tmp_path):
    """JOB-226 shape: uploaded_docs has resume only, but a tailored cover sits beside it. The plan
    must ADD a cover entry pointing at the on-disk tailored cover, keeping the existing resume."""
    appdir = _appdir(tmp_path, "APP-031-Cresta")
    rec = {
        "job_id": "JOB-226", "submitted": False,
        "uploaded_docs": [
            {"doc": "resume", "path": str(appdir / "SAM_RIVERA_Resume.pdf"),
             "name": "SAM_RIVERA_Resume.pdf"},
        ],
    }
    changed, new_docs = plan_backfill(rec)
    assert changed is True
    docs = {d["doc"]: d["path"] for d in new_docs}
    assert docs["resume"] == str(appdir / "SAM_RIVERA_Resume.pdf")
    assert docs["cover"] == str(appdir / "SAM_RIVERA_Cover_Letter.pdf")


def test_resume_registered_when_uploaded_docs_empty(tmp_path):
    """A record with EMPTY uploaded_docs but tailored resume+cover on disk (resolved via the cover
    sibling or a recorded run_dir) — here we seed a cover entry so the resume is found via sibling."""
    appdir = _appdir(tmp_path, "APP-041-Databricks")
    rec = {
        "job_id": "JOB-214", "submitted": False,
        "uploaded_docs": [
            {"doc": "cover", "path": str(appdir / "SAM_RIVERA_Cover_Letter.pdf"),
             "name": "SAM_RIVERA_Cover_Letter.pdf"},
        ],
    }
    changed, new_docs = plan_backfill(rec)
    assert changed is True
    docs = {d["doc"]: d["path"] for d in new_docs}
    assert docs["resume"] == str(appdir / "SAM_RIVERA_Resume.pdf")


def test_idempotent_no_change_when_both_registered(tmp_path):
    """Both tailored docs already registered and on disk → plan reports NO change (running the
    backfill twice never duplicates entries)."""
    appdir = _appdir(tmp_path, "APP-031-Cresta")
    rec = {
        "job_id": "JOB-226", "submitted": False,
        "uploaded_docs": [
            {"doc": "resume", "path": str(appdir / "SAM_RIVERA_Resume.pdf"),
             "name": "SAM_RIVERA_Resume.pdf"},
            {"doc": "cover", "path": str(appdir / "SAM_RIVERA_Cover_Letter.pdf"),
             "name": "SAM_RIVERA_Cover_Letter.pdf"},
        ],
    }
    changed, new_docs = plan_backfill(rec)
    assert changed is False
    # exactly one resume + one cover, no dupes
    assert sorted(d["doc"] for d in new_docs) == ["cover", "resume"]


def test_submitted_record_is_skipped(tmp_path):
    """A submitted record must never be rewritten — the plan reports no change regardless of disk."""
    _appdir(tmp_path, "APP-031-Cresta")
    rec = {"job_id": "JOB-216", "submitted": True, "uploaded_docs": []}
    changed, _ = plan_backfill(rec)
    assert changed is False


def test_no_cover_on_disk_leaves_resume_only(tmp_path):
    """If only a tailored resume exists (no cover anywhere), the plan keeps resume-only — it never
    invents a cover entry for a role with no tailored cover."""
    appdir = _appdir(tmp_path, "APP-050-Acme", cover=False)
    rec = {
        "job_id": "JOB-900", "submitted": False,
        "uploaded_docs": [
            {"doc": "resume", "path": str(appdir / "SAM_RIVERA_Resume.pdf"),
             "name": "SAM_RIVERA_Resume.pdf"},
        ],
    }
    changed, new_docs = plan_backfill(rec)
    assert changed is False
    assert [d["doc"] for d in new_docs] == ["resume"]


def test_master_path_never_registered(tmp_path):
    """A uploaded_docs resume entry that is the MASTER must not be carried forward as a tailored
    resume — the plan drops it (the sibling cover dir still yields the tailored resume if present)."""
    appdir = _appdir(tmp_path, "APP-031-Cresta")
    master = tmp_path / "Sam_Rivera_Resume_Master.pdf"
    master.write_text("M", encoding="utf-8")
    rec = {
        "job_id": "JOB-226", "submitted": False,
        "uploaded_docs": [
            {"doc": "resume", "path": str(master), "name": "Sam_Rivera_Resume_Master.pdf"},
            {"doc": "cover", "path": str(appdir / "SAM_RIVERA_Cover_Letter.pdf"),
             "name": "SAM_RIVERA_Cover_Letter.pdf"},
        ],
    }
    changed, new_docs = plan_backfill(rec)
    docs = {d["doc"]: d["path"] for d in new_docs}
    # the tailored resume beside the cover replaces the master entry
    assert docs["resume"] == str(appdir / "SAM_RIVERA_Resume.pdf")
    assert "master" not in os.path.basename(docs["resume"]).lower()
    assert changed is True
