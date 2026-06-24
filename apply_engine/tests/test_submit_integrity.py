# -*- coding: utf-8 -*-
"""CONTRACT TEST — the deterministic submit-integrity invariant gate.

WHY THIS FILE EXISTS (the "catch it earlier" guard, 2026-06-10)
A live audit found that every "submittable" job was attaching the GENERIC MASTER resume, not
the tailored one: `_resolve_pdfs` recomputed a per-job path (`applications/<job_id>/resume.pdf`)
that NEVER exists, then silently fell back to the master. The dashboard greened the tailored
content while the engine shipped the master. The whole tailor/audit/calibration effort was
discarded at attach time, invisibly.

The fix is a single deterministic invariant gate — `verify_submittable(record, config)` — that
asserts the package that would ACTUALLY be attached is the tailored one (and every other
pre-submit invariant), and BLOCKS loudly when it is not. This contract test pins that gate so the
bug-class can never silently return: it runs `verify_submittable` over synthetic records covering
each failure mode, and would have caught the master-attach bug against the OLD `_resolve_pdfs`.

These are PURE assertions — no browser, no LLM, no live form. A false PASS here is the worst
failure mode in the engine (a fabricated/master package submitted as if tailored), so each
invariant gets its own named test.
"""
from pathlib import Path

import pytest

from apply_engine.finish import verify_submittable, _resolve_pdfs


# --------------------------------------------------------------------------------------
# test scaffolding: a tiny config stub + on-disk tailored/master PDFs in a tmp tree
# --------------------------------------------------------------------------------------

class _CfgStub:
    """Minimal stand-in for apply_engine.config: only PKG_DIR is read by _resolve_pdfs /
    verify_submittable. PKG_DIR.parent is the career root (where the master + applications/ live),
    mirroring the real layout (apply_engine/ under career/)."""
    def __init__(self, pkg_dir: Path):
        self.PKG_DIR = pkg_dir


@pytest.fixture()
def career_tree(tmp_path):
    """Build a throwaway career tree:
        <root>/APPLICANT_Resume_Master.pdf        (the master — must NEVER be the attach)
        <root>/apply_engine/                            (PKG_DIR)
        <root>/applications/APP-001-Acme/APPLICANT_Resume.pdf   (a real tailored resume)
        <root>/applications/APP-001-Acme/APPLICANT_Cover_Letter.pdf
    Returns (cfg, paths-dict)."""
    root = tmp_path / "career"
    pkg = root / "apply_engine"
    pkg.mkdir(parents=True)
    master = root / "APPLICANT_Resume_Master.pdf"
    master.write_text("MASTER", encoding="utf-8")
    appdir = root / "applications" / "APP-001-Acme"
    appdir.mkdir(parents=True)
    tailored_resume = appdir / "APPLICANT_Resume.pdf"
    tailored_resume.write_text("TAILORED", encoding="utf-8")
    tailored_cover = appdir / "APPLICANT_Cover_Letter.pdf"
    tailored_cover.write_text("COVER", encoding="utf-8")
    cfg = _CfgStub(pkg)
    return cfg, {
        "master": master,
        "tailored_resume": tailored_resume,
        "tailored_cover": tailored_cover,
        "appdir": appdir,
    }


def _valid_record(paths, **over):
    """A record that PASSES verify_submittable: tailored uploaded_docs resume+cover that exist on
    disk, both audits PASS with judge_ran True, clean work-auth, no unfilled, no edit_request."""
    rec = {
        "job_id": "JOB-001",
        "status": "ready_to_submit",
        "submitted": False,
        "uploaded_docs": [
            {"doc": "resume", "path": str(paths["tailored_resume"]),
             "name": "APPLICANT_Resume.pdf"},
            {"doc": "cover", "path": str(paths["tailored_cover"]),
             "name": "APPLICANT_Cover_Letter.pdf"},
        ],
        "work_auth": [{"field": "sponsor", "answer": "No"}],
        "custom_qs": [],
        "unfilled_required": [],
        "needs_sam": [],
        "audit": {"verdict": "PASS", "judge_ran": True, "findings": []},
        "quality_audit": {"verdict": "PASS", "judge_ran": True},
    }
    rec.update(over)
    return rec


# ======================================================================================
# INVARIANT 1 — the tailored-resume-not-master invariant (would have caught FINDING #1)
# ======================================================================================

def test_valid_record_is_submittable(career_tree):
    cfg, paths = career_tree
    ok, reasons = verify_submittable(_valid_record(paths), cfg)
    assert ok is True, reasons
    assert reasons == []


def test_master_resume_in_uploaded_docs_is_not_submittable(career_tree):
    """The exact shape of the live legacy records (JOB-216/JOB-212): uploaded_docs resume path IS
    the master file. verify_submittable must BLOCK and the reason must name the PDF-integrity
    failure. This is the invariant that catches FINDING #1."""
    cfg, paths = career_tree
    rec = _valid_record(paths)
    rec["uploaded_docs"] = [
        {"doc": "resume", "path": str(paths["master"]),
         "name": "APPLICANT_Resume_Master.pdf"},
    ]
    ok, reasons = verify_submittable(rec, cfg)
    assert ok is False
    blob = " ".join(reasons).lower()
    assert "master" in blob and "resume" in blob


def test_missing_resume_file_on_disk_is_not_submittable(career_tree):
    """uploaded_docs points at a tailored path that no longer exists on disk → BLOCK (never fall
    back to the master)."""
    cfg, paths = career_tree
    rec = _valid_record(paths)
    rec["uploaded_docs"] = [
        {"doc": "resume", "path": str(paths["appdir"] / "GONE_Resume.pdf"),
         "name": "GONE_Resume.pdf"},
    ]
    ok, reasons = verify_submittable(rec, cfg)
    assert ok is False
    blob = " ".join(reasons).lower()
    assert "resume" in blob


def test_no_uploaded_docs_resume_entry_is_not_submittable(career_tree):
    """No resume entry in uploaded_docs at all → BLOCK (cannot prove a tailored resume attaches)."""
    cfg, paths = career_tree
    rec = _valid_record(paths)
    rec["uploaded_docs"] = []
    ok, reasons = verify_submittable(rec, cfg)
    assert ok is False
    blob = " ".join(reasons).lower()
    assert "resume" in blob


# ======================================================================================
# INVARIANT 3/4 — DEMOTED (2026-06-22). The LLM audit verdict / judge_ran and the holistic
# quality_audit were demoted from required submit-blockers to advisory/on-demand. verify_submittable
# delegates content-gating to can_submit, which now blocks ONLY on the DETERMINISTIC gate
# (audit.gate_blocks > 0). The LLM-verdict / quality tests below were flipped to the new contract;
# the deterministic-gate block test is kept as the surviving content gate.
# ======================================================================================

def test_quality_audit_none_no_longer_blocks_submit(career_tree):
    # FLIPPED from test_quality_audit_none_is_not_submittable (FINDING #3 wedge). A record with no
    # quality_audit and a clean deterministic gate now PASSES verify_submittable — the quality judge
    # is advisory/on-demand. (PDF integrity + work-auth + status invariants still hold.)
    cfg, paths = career_tree
    rec = _valid_record(paths, quality_audit=None)
    ok, reasons = verify_submittable(rec, cfg)
    assert ok is True, reasons


def test_llm_blocked_verdict_no_longer_blocks_submit(career_tree):
    # FLIPPED from test_fabrication_blocked_is_not_submittable. An LLM-BLOCKED verdict with a CLEAN
    # deterministic gate (no gate_blocks) is advisory now -> submittable.
    cfg, paths = career_tree
    rec = _valid_record(paths, audit={"verdict": "BLOCKED", "gate_blocks": 0, "findings": []})
    ok, reasons = verify_submittable(rec, cfg)
    assert ok is True, reasons


def test_deterministic_gate_block_is_not_submittable(career_tree):
    # SAFETY (kept): the DETERMINISTIC gate is the one content gate that still hard-blocks. A
    # gate_blocks>0 record must NOT be submittable, with the deterministic-gate reason.
    cfg, paths = career_tree
    rec = _valid_record(paths, audit={"verdict": "PASS", "judge_ran": True, "gate_blocks": 1,
                                      "findings": []})
    ok, reasons = verify_submittable(rec, cfg)
    assert ok is False
    assert any("deterministic gate" in r.lower() for r in reasons)


def test_stale_audit_judge_not_run_no_longer_blocks_submit(career_tree):
    # FLIPPED from test_stale_audit_judge_not_run_is_not_submittable. A deterministic-gate-only PASS
    # (judge_ran False) with a clean gate is advisory now -> submittable.
    cfg, paths = career_tree
    rec = _valid_record(paths, audit={"verdict": "PASS", "judge_ran": False, "gate_blocks": 0,
                                      "findings": []})
    ok, reasons = verify_submittable(rec, cfg)
    assert ok is True, reasons


# ======================================================================================
# INVARIANT 5/6 — work-auth red flag, unfilled required, lingering edit_request
# ======================================================================================

def test_work_auth_red_flag_is_not_submittable(career_tree):
    cfg, paths = career_tree
    rec = _valid_record(paths, work_auth=[{"field": "sponsor", "answer": "Yes"}])
    ok, reasons = verify_submittable(rec, cfg)
    assert ok is False
    assert any("work-auth" in r.lower() for r in reasons)


def test_unfilled_required_is_not_submittable(career_tree):
    cfg, paths = career_tree
    rec = _valid_record(paths, unfilled_required=["Cover letter"])
    ok, reasons = verify_submittable(rec, cfg)
    assert ok is False


def test_lingering_edit_request_is_not_submittable(career_tree):
    """A custom_q still carrying an edit_request means an answer edit hasn't settled → BLOCK."""
    cfg, paths = career_tree
    rec = _valid_record(paths)
    rec["custom_qs"] = [
        {"q": "Why us?", "status": "drafted", "value": "x", "edit_request": "make it punchier"},
    ]
    ok, reasons = verify_submittable(rec, cfg)
    assert ok is False
    assert any("edit" in r.lower() for r in reasons)


def test_all_reasons_collected_not_just_first(career_tree):
    """verify_submittable returns EVERY failing reason, not just the first — so the dashboard can
    show the full picture. A record that fails both the PDF invariant AND a work-auth red flag
    surfaces both. (Updated 2026-06-22: the quality-audit invariant was demoted, so this pairs the
    PDF-integrity failure with a still-blocking work-auth red flag to exercise reason-collection.)"""
    cfg, paths = career_tree
    rec = _valid_record(paths, work_auth=[{"field": "sponsor", "answer": "Yes"}])
    rec["uploaded_docs"] = [{"doc": "resume", "path": str(paths["master"]),
                             "name": "APPLICANT_Resume_Master.pdf"}]
    ok, reasons = verify_submittable(rec, cfg)
    assert ok is False
    blob = " ".join(reasons).lower()
    assert "master" in blob          # PDF-integrity failure
    assert "work-auth" in blob       # work-auth red-flag failure (still a hard block)
    assert len(reasons) >= 2


# ======================================================================================
# _resolve_pdfs — returns the uploaded_docs path, never the master (the FINDING #1 unit test)
# ======================================================================================

def test_resolve_pdfs_uses_uploaded_docs_path(career_tree):
    """_resolve_pdfs must return the tailored uploaded_docs resume path, NOT a recomputed
    applications/<job_id>/ path or the master. Run against the OLD _resolve_pdfs this fails (it
    returned the master)."""
    cfg, paths = career_tree
    rec = _valid_record(paths)
    resume_pdf, cover_pdf = _resolve_pdfs(cfg, "JOB-001", record=rec)
    assert resume_pdf is not None
    assert Path(resume_pdf) == paths["tailored_resume"]
    assert cover_pdf is not None
    assert Path(cover_pdf) == paths["tailored_cover"]


def test_resolve_pdfs_returns_none_when_file_absent(career_tree):
    """When the uploaded_docs resume file does not exist on disk, _resolve_pdfs returns the
    no-fallback sentinel (None) — it must NEVER substitute the master."""
    cfg, paths = career_tree
    rec = _valid_record(paths)
    rec["uploaded_docs"] = [{"doc": "resume", "path": str(paths["appdir"] / "GONE.pdf"),
                             "name": "GONE.pdf"}]
    resume_pdf, cover_pdf = _resolve_pdfs(cfg, "JOB-001", record=rec)
    assert resume_pdf is None


def test_resolve_pdfs_never_returns_master(career_tree):
    """Even when uploaded_docs explicitly names the master, _resolve_pdfs must not hand it back as
    a submittable resume — it returns the sentinel so the gate blocks."""
    cfg, paths = career_tree
    rec = _valid_record(paths)
    rec["uploaded_docs"] = [{"doc": "resume", "path": str(paths["master"]),
                             "name": "APPLICANT_Resume_Master.pdf"}]
    resume_pdf, _ = _resolve_pdfs(cfg, "JOB-001", record=rec)
    assert resume_pdf is None


# ======================================================================================
# TAILORED-COVER RESOLUTION FALLBACK (2026-06-11)
# A cover built/edited AFTER the apply RUN is never written into uploaded_docs (the run only
# records what attached live). So a real tailored cover exists on disk but uploaded_docs lists
# resume only — _resolve_pdfs returned cover=None and the engine dropped the tailored cover at
# submit (silent quality-contract breach) / errored attaching it on the open path. The durable
# fix: when uploaded_docs has no cover entry but the tailored cover PDF sits beside the resolved
# resume (the same applications/<APP>/ dir), resolve THAT file. Never a master/generic.
# ======================================================================================

def test_resolve_pdfs_falls_back_to_sibling_tailored_cover(career_tree):
    """JOB-226 shape: uploaded_docs has resume ONLY, but APPLICANT_Cover_Letter.pdf sits in
    the same tailored dir. _resolve_pdfs must resolve the cover via the sibling-dir fallback."""
    cfg, paths = career_tree
    rec = _valid_record(paths)
    rec["uploaded_docs"] = [
        {"doc": "resume", "path": str(paths["tailored_resume"]),
         "name": "APPLICANT_Resume.pdf"},
    ]
    resume_pdf, cover_pdf = _resolve_pdfs(cfg, "JOB-226", record=rec)
    assert resume_pdf is not None and Path(resume_pdf) == paths["tailored_resume"]
    assert cover_pdf is not None, "tailored cover beside the resume must resolve via fallback"
    assert Path(cover_pdf) == paths["tailored_cover"]


def test_resolve_pdfs_cover_fallback_requires_file_on_disk(career_tree):
    """If uploaded_docs lacks a cover AND no tailored cover PDF exists anywhere, cover stays None —
    the fallback never invents a path or substitutes a generic."""
    cfg, paths = career_tree
    rec = _valid_record(paths)
    rec["uploaded_docs"] = [
        {"doc": "resume", "path": str(paths["tailored_resume"]),
         "name": "APPLICANT_Resume.pdf"},
    ]
    paths["tailored_cover"].unlink()  # no cover on disk
    resume_pdf, cover_pdf = _resolve_pdfs(cfg, "JOB-226", record=rec)
    assert resume_pdf is not None
    assert cover_pdf is None


def test_resolve_pdfs_resume_fallback_from_sibling_cover(career_tree):
    """Defensive symmetry: if uploaded_docs has the cover only but the tailored resume sits beside
    it, the resume resolves via the same sibling-dir fallback (never the master)."""
    cfg, paths = career_tree
    rec = _valid_record(paths)
    rec["uploaded_docs"] = [
        {"doc": "cover", "path": str(paths["tailored_cover"]),
         "name": "APPLICANT_Cover_Letter.pdf"},
    ]
    resume_pdf, cover_pdf = _resolve_pdfs(cfg, "JOB-226", record=rec)
    assert cover_pdf is not None and Path(cover_pdf) == paths["tailored_cover"]
    assert resume_pdf is not None and Path(resume_pdf) == paths["tailored_resume"]


# ======================================================================================
# SUBMIT-GATE COHERENCE — a built tailored cover MUST attach, never silently drop
# ======================================================================================

def _record_with_cover_dict(paths, **over):
    """A valid record whose package INCLUDES a tailored cover (a cover dict with paragraphs), but
    whose uploaded_docs lists resume ONLY — exactly the 16 live records' shape."""
    rec = _valid_record(paths)
    rec["uploaded_docs"] = [
        {"doc": "resume", "path": str(paths["tailored_resume"]),
         "name": "APPLICANT_Resume.pdf"},
    ]
    rec["cover"] = {"addressee": "Hiring Team", "salutation": "Dear Hiring Team",
                    "paragraphs": ["I am excited to apply.", "Here is why I fit."]}
    rec.update(over)
    return rec


def test_built_cover_resolvable_is_submittable(career_tree):
    """A tailored cover was built (cover dict) and its PDF resolves via the sibling fallback →
    the gate PASSES (the cover will attach). No false block."""
    cfg, paths = career_tree
    rec = _record_with_cover_dict(paths)
    ok, reasons = verify_submittable(rec, cfg)
    assert ok is True, reasons


def test_built_cover_unresolvable_blocks(career_tree):
    """A tailored cover was built (cover dict with paragraphs) but NO cover PDF exists anywhere →
    the gate must BLOCK with a clear 'built cover can't attach' reason, NOT silently submit
    resume-only and drop the user's tailored cover."""
    cfg, paths = career_tree
    rec = _record_with_cover_dict(paths)
    paths["tailored_cover"].unlink()  # built in data, but no PDF on disk
    ok, reasons = verify_submittable(rec, cfg)
    assert ok is False
    blob = " ".join(reasons).lower()
    assert "cover" in blob and ("resolve" in blob or "attach" in blob)


def test_no_cover_app_still_submittable(career_tree):
    """A role that legitimately wants NO cover: no cover dict, no cover PDF, uploaded_docs resume
    only. The gate must NOT invent a cover requirement — resume-only is submittable."""
    cfg, paths = career_tree
    rec = _valid_record(paths)
    rec["uploaded_docs"] = [
        {"doc": "resume", "path": str(paths["tailored_resume"]),
         "name": "APPLICANT_Resume.pdf"},
    ]
    rec.pop("cover", None)
    paths["tailored_cover"].unlink()  # genuinely no cover anywhere
    ok, reasons = verify_submittable(rec, cfg)
    assert ok is True, reasons


# ======================================================================================
# OPEN/FILL ATTACH PATH — a resolved cover must be a real file build_answers accepts
# ======================================================================================

def _profile_file(tmp_path):
    import json
    p = tmp_path / "profile.json"
    p.write_text(json.dumps({
        "first_name": "Sam", "last_name": "Rivera", "full_name": "Sam Rivera",
        "email": "sam.rivera@example.com", "phone": "555-555-0100", "city": "Austin",
        "state": "CA", "country": "United States", "linkedin": "https://linkedin.com/in/x",
        "portfolio_url": "", "how_did_you_hear": "Company website",
    }), encoding="utf-8")
    return p


def test_resolved_cover_is_accepted_by_build_answers(career_tree, tmp_path):
    """JOB-226 open/fill regression guard: the cover _resolve_pdfs hands forward (via the sibling
    fallback) must be a file that EXISTS, so build_answers never raises FileNotFoundError on the
    open path. A resolved-but-unused cover (the form may not ask for one) is fine, not an error."""
    cfg, paths = career_tree
    rec = _valid_record(paths)
    rec["uploaded_docs"] = [
        {"doc": "resume", "path": str(paths["tailored_resume"]),
         "name": "APPLICANT_Resume.pdf"},
    ]
    from apply_engine.source_data import build_answers  # local: autoflake strips top-level unused
    resume_pdf, cover_pdf = _resolve_pdfs(cfg, "JOB-226", record=rec)
    assert cover_pdf is not None and Path(cover_pdf).exists()
    # build_answers must accept the resolved pair without raising (cover exists on disk).
    ans = build_answers(profile_path=_profile_file(tmp_path),
                        job={"id": "JOB-226", "company": "Cresta", "title": "FDE"},
                        resume_pdf=resume_pdf, cover_pdf=cover_pdf)
    assert ans.cover_pdf == Path(cover_pdf)


def test_resolve_pdfs_never_hands_forward_a_missing_cover(career_tree):
    """The contract build_answers relies on: _resolve_pdfs returns a cover ONLY when it exists on
    disk, so the attach path can never receive a phantom cover path that crashes the run."""
    cfg, paths = career_tree
    rec = _valid_record(paths)
    rec["uploaded_docs"] = [
        {"doc": "resume", "path": str(paths["tailored_resume"]),
         "name": "APPLICANT_Resume.pdf"},
        {"doc": "cover", "path": str(paths["appdir"] / "GHOST_Cover_Letter.pdf"),
         "name": "GHOST_Cover_Letter.pdf"},
    ]
    paths["tailored_cover"].unlink()  # the sibling fallback also finds nothing
    _, cover_pdf = _resolve_pdfs(cfg, "JOB-226", record=rec)
    assert cover_pdf is None
