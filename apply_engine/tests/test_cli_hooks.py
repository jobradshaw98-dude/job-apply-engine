"""The --answer flag must wire the real Claude drafter + audit gate + grounding facts
into the run; default-off must yield (None, None, "") so the engine escalates every
custom question (the safe default). This is the production switch that makes the
full-form-fill / custom-question conversion path reachable from the CLI."""
import io
import json
import sys

import pytest

from apply_engine import config
from apply_engine.cli import build_hooks, _utf8_stdout, ensure_pdfs, NoTailoredPDF


def _write_apps(path, records):
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")


def test_ensure_pdfs_resolves_tailored_app_folder(tmp_path, monkeypatch):
    """ensure_pdfs must follow job_id -> applications.json record -> APP-id+company-slug folder,
    and return the build pipeline's SAM_RIVERA_*.pdf there. Regression: it previously looked
    in applications/<JOB-id>/resume.pdf (wrong id, wrong filename) and always fell back to the
    master, so tailored packages were never attached."""
    career = tmp_path / "career"
    apps_dir = career / "applications"  # PKG_DIR.parent (== career) / "applications"
    folder = apps_dir / "APP-028-Ramp"
    folder.mkdir(parents=True)
    (folder / "SAM_RIVERA_Resume.pdf").write_bytes(b"%PDF-1.4 resume")
    (folder / "SAM_RIVERA_Cover_Letter.pdf").write_bytes(b"%PDF-1.4 cover")

    apps_json = tmp_path / "applications.json"
    _write_apps(apps_json, [{"id": "APP-028", "job_id": "JOB-216", "company": "Ramp"}])

    monkeypatch.setattr(config, "PKG_DIR", career / "apply_engine")
    monkeypatch.setattr(config, "APPLICATIONS_JSON", apps_json)

    resume_pdf, cover_pdf = ensure_pdfs({"id": "JOB-216"})
    assert resume_pdf == folder / "SAM_RIVERA_Resume.pdf"
    assert cover_pdf == folder / "SAM_RIVERA_Cover_Letter.pdf"


def test_ensure_pdfs_accepts_plain_filenames(tmp_path, monkeypatch):
    """Older build runs emitted plain resume.pdf / cover.pdf; both naming schemes must resolve."""
    career = tmp_path / "career"
    folder = career / "applications" / "APP-029-Palantir"
    folder.mkdir(parents=True)
    (folder / "resume.pdf").write_bytes(b"%PDF-1.4 resume")
    (folder / "cover.pdf").write_bytes(b"%PDF-1.4 cover")

    apps_json = tmp_path / "applications.json"
    _write_apps(apps_json, [{"id": "APP-029", "job_id": "JOB-212", "company": "Palantir"}])

    monkeypatch.setattr(config, "PKG_DIR", career / "apply_engine")
    monkeypatch.setattr(config, "APPLICATIONS_JSON", apps_json)

    resume_pdf, cover_pdf = ensure_pdfs({"id": "JOB-212"})
    assert resume_pdf == folder / "resume.pdf"
    assert cover_pdf == folder / "cover.pdf"


def test_ensure_pdfs_raises_instead_of_master_fallback(tmp_path, monkeypatch):
    """The silent master-resume last-resort is KILLED. When no tailored resume PDF resolves for a
    selected job, ensure_pdfs must RAISE NoTailoredPDF (main turns it into a needs_build halt) and
    must NOT return the generic master resume — even though the master PDF exists on disk."""
    career = tmp_path / "career"
    (career / "apply_engine").mkdir(parents=True)
    master_pdf = career / "Sam_Rivera_Resume_Master.pdf"
    master_pdf.write_bytes(b"%PDF-1.4 master")

    apps_json = tmp_path / "applications.json"
    _write_apps(apps_json, [{"id": "APP-001", "job_id": "JOB-999", "company": "Other"}])

    monkeypatch.setattr(config, "PKG_DIR", career / "apply_engine")
    monkeypatch.setattr(config, "APPLICATIONS_JSON", apps_json)

    with pytest.raises(NoTailoredPDF):
        ensure_pdfs({"id": "JOB-216"})


def test_ensure_pdfs_allow_master_opt_in_still_works(tmp_path, monkeypatch):
    """The master resume is reachable ONLY behind the explicit allow_master debug opt-in, which the
    live-stage path never passes. With it set, the old last-resort behaviour is preserved."""
    career = tmp_path / "career"
    (career / "apply_engine").mkdir(parents=True)
    master_pdf = career / "Sam_Rivera_Resume_Master.pdf"
    master_pdf.write_bytes(b"%PDF-1.4 master")

    apps_json = tmp_path / "applications.json"
    _write_apps(apps_json, [{"id": "APP-001", "job_id": "JOB-999", "company": "Other"}])

    monkeypatch.setattr(config, "PKG_DIR", career / "apply_engine")
    monkeypatch.setattr(config, "APPLICATIONS_JSON", apps_json)

    resume_pdf, cover_pdf = ensure_pdfs({"id": "JOB-216"}, allow_master=True)
    assert resume_pdf == master_pdf
    assert cover_pdf is None


def test_utf8_stdout_survives_non_cp1252_char(monkeypatch):
    """Regression: live Lever question labels carry U+2731 (heavy asterisk required marker),
    which crashed the review print on Windows' default cp1252 console (UnicodeEncodeError) —
    aborting the run AFTER it staged but BEFORE recording status. _utf8_stdout must make that
    print safe. Reproduce the failure with a real cp1252 stream, then prove the fix."""
    buf = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", buf)
    _utf8_stdout()                       # reconfigures stdout to utf-8 / errors='replace'
    print("[DRAFTED] Why do you want to work here? ✱")  # must NOT raise
    buf.flush()


def test_build_hooks_off_by_default():
    answer_fn, audit_fn, facts = build_hooks(False, {"id": "JOB-1"})
    assert answer_fn is None
    assert audit_fn is None
    assert facts == ""


def test_build_hooks_on_wires_llm(monkeypatch):
    import apply_engine.llm as llm
    monkeypatch.setattr(llm, "make_claude_llm", lambda *a, **k: (lambda p: "drafted"))
    monkeypatch.setattr(llm, "make_audit_fn", lambda: (lambda t: []))
    monkeypatch.setattr(llm, "load_facts", lambda job, **k: "FACTS:" + job["id"])

    answer_fn, audit_fn, facts = build_hooks(True, {"id": "JOB-1"})
    assert callable(answer_fn)
    assert callable(audit_fn)
    assert facts == "FACTS:JOB-1"


def test_build_hooks_degrades_when_cli_missing(tmp_path, capsys, monkeypatch):
    """--answer must NOT crash the run when the generator can't be constructed. Generation now
    runs on the plan via the `claude` CLI (no Anthropic-API fallback, brief_config untouched), so
    the real degrade trigger is the `claude` CLI being absent from PATH — make_claude_llm raises
    LLMUnavailable, and build_hooks must catch it and fall back to (None, None, "") with a notice
    so every custom question safely escalates to Sam instead of hard-crashing the run."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _name: None)  # simulate `claude` not on PATH

    answer_fn, audit_fn, facts = build_hooks(True, {"id": "JOB-1"})
    assert answer_fn is None
    assert audit_fn is None
    assert facts == ""
    assert "unavailable" in capsys.readouterr().out
