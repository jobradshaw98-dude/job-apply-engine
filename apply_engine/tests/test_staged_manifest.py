import json

from apply_engine.staged_manifest import attach_audit, build_record, write_record
from apply_engine.orchestrator import JobOutcome


def _job():
    return {"id": "JOB-1", "company": "Acme", "title": "FEA Engineer",
            "url": "https://boards.greenhouse.io/acme/jobs/1", "ats": "greenhouse"}


def test_build_record_ready_to_submit_maps_core_fields():
    out = JobOutcome(
        job_id="JOB-1", status="ready_to_submit", submitted=False, verify_ok=True,
        run_dir="/runs/JOB-1_run",
        filled_fields=["first_name", "last_name", "email"],
        work_auth_answers=[{"field": "sponsor", "q": "Require sponsorship?", "answer": "No"}],
    )
    rec = build_record(out, _job(), staged_at="2026-06-02T09:00:00")
    assert rec["job_id"] == "JOB-1"
    assert rec["company"] == "Acme"
    assert rec["role"] == "FEA Engineer"
    assert rec["ats"] == "greenhouse"
    assert rec["url"] == "https://boards.greenhouse.io/acme/jobs/1"
    assert rec["status"] == "ready_to_submit"
    assert rec["submitted"] is False
    assert rec["run_dir"] == "/runs/JOB-1_run"
    assert rec["filled_fields"] == ["first_name", "last_name", "email"]
    assert rec["work_auth"] == [{"field": "sponsor", "q": "Require sponsorship?", "answer": "No"}]
    assert rec["needs_sam"] == []
    assert rec["staged_at"] == "2026-06-02T09:00:00"


def test_build_record_picks_best_preview_png(tmp_path):
    rd = tmp_path / "JOB-1_run"
    rd.mkdir()
    for name in ["step_01_opened.png", "step_02_filled.png", "step_05_review_brink.png", "audit.jsonl"]:
        (rd / name).write_bytes(b"x")
    out = JobOutcome(job_id="JOB-1", status="ready_to_submit", run_dir=str(rd))
    rec = build_record(out, _job(), staged_at="t")
    # last interesting step screenshot wins (review_brink > filled > opened)
    assert rec["preview_png"] == "step_05_review_brink.png"


def test_build_record_no_screenshots_gives_empty_preview(tmp_path):
    rd = tmp_path / "JOB-1_run"
    rd.mkdir()
    out = JobOutcome(job_id="JOB-1", status="error", run_dir=str(rd))
    rec = build_record(out, _job(), staged_at="t")
    assert rec["preview_png"] == ""


def test_build_record_missing_run_dir_gives_empty_preview():
    out = JobOutcome(job_id="JOB-1", status="error", run_dir="/does/not/exist")
    rec = build_record(out, _job(), staged_at="t")
    assert rec["preview_png"] == ""


def test_build_record_needs_sam_assembled_from_unfilled_and_halt():
    out = JobOutcome(
        job_id="JOB-1", status="needs_input",
        unfilled_required=["Cover letter", "Relocation?"],
        halt_reason="required fields still need Sam: Cover letter; Relocation?",
    )
    rec = build_record(out, _job(), staged_at="t")
    assert "Cover letter" in rec["needs_sam"]
    assert "Relocation?" in rec["needs_sam"]
    assert rec["halt_reason"].startswith("required fields")


def test_build_record_submitted_always_reflected():
    out = JobOutcome(job_id="JOB-1", status="ready_to_submit", submitted=True)
    rec = build_record(out, _job(), staged_at="t")
    assert rec["submitted"] is True


def test_build_record_custom_qs_from_generated():
    out = JobOutcome(
        job_id="JOB-1", status="ready_to_submit",
        generated=[{"q": "Why us?", "kind": "text", "status": "drafted", "value": "Because..."},
                   {"q": "Pick one", "kind": "select", "status": "answered", "value": "B"}],
    )
    rec = build_record(out, _job(), staged_at="t")
    assert len(rec["custom_qs"]) == 2
    assert rec["custom_qs"][0]["q"] == "Why us?"


def test_build_record_corrections_passed_through():
    out = JobOutcome(
        job_id="JOB-1", status="ready_to_submit",
        corrections=[{"label": "Current company", "action": "overwrite",
                      "current": "BUILDS", "correct": "Meridian Devices", "applied": True}],
    )
    rec = build_record(out, _job(), staged_at="t")
    assert rec["corrections"][0]["current"] == "BUILDS"
    assert rec["corrections"][0]["correct"] == "Meridian Devices"


def test_build_record_uploaded_docs_land_on_record():
    out = JobOutcome(
        job_id="JOB-1", status="needs_input",
        uploaded_docs=[{"doc": "resume",
                        "path": "/career/APPLICANT_Resume_Master.pdf",
                        "name": "APPLICANT_Resume_Master.pdf"}],
    )
    rec = build_record(out, _job(), staged_at="t")
    assert rec["uploaded_docs"] == [
        {"doc": "resume", "path": "/career/APPLICANT_Resume_Master.pdf",
         "name": "APPLICANT_Resume_Master.pdf"}]


def test_build_record_uploaded_docs_default_empty():
    out = JobOutcome(job_id="JOB-1", status="ready_to_submit")
    rec = build_record(out, _job(), staged_at="t")
    assert rec["uploaded_docs"] == []


def test_build_record_empty_outcome_safe():
    out = JobOutcome(job_id="JOB-X", status="error", error="boom")
    rec = build_record(out, {"id": "JOB-X"}, staged_at="t")
    assert rec["company"] == ""
    assert rec["role"] == ""
    assert rec["ats"] == ""
    assert rec["url"] == ""
    assert rec["filled_fields"] == []
    assert rec["needs_sam"] == []


def test_build_record_ats_falls_back_to_url_detection():
    # job dict has no explicit ats; record still carries whatever job provides ("" here)
    job = {"id": "JOB-1", "company": "Acme", "title": "Eng",
           "url": "https://jobs.lever.co/acme/1"}
    out = JobOutcome(job_id="JOB-1", status="ready_to_submit")
    rec = build_record(out, job, staged_at="t")
    assert rec["url"] == "https://jobs.lever.co/acme/1"


def test_write_record_appends_then_replaces(tmp_path):
    mp = tmp_path / "staged_applications.json"
    out1 = JobOutcome(job_id="JOB-1", status="needs_input")
    write_record(build_record(out1, _job(), staged_at="t1"), mp)
    data = json.loads(mp.read_text())
    assert len(data) == 1 and data[0]["status"] == "needs_input"

    # different job appends
    out2 = JobOutcome(job_id="JOB-2", status="ready_to_submit")
    write_record(build_record(out2, {"id": "JOB-2", "company": "B", "title": "R", "url": "u"}, staged_at="t2"), mp)
    data = json.loads(mp.read_text())
    assert len(data) == 2

    # same job_id replaces in place
    out1b = JobOutcome(job_id="JOB-1", status="ready_to_submit")
    write_record(build_record(out1b, _job(), staged_at="t3"), mp)
    data = json.loads(mp.read_text())
    assert len(data) == 2
    j1 = [d for d in data if d["job_id"] == "JOB-1"][0]
    assert j1["status"] == "ready_to_submit"
    assert j1["staged_at"] == "t3"


def test_write_record_handles_corrupt_file(tmp_path):
    mp = tmp_path / "staged_applications.json"
    mp.write_text("{ not json")
    out = JobOutcome(job_id="JOB-1", status="ready_to_submit")
    write_record(build_record(out, _job(), staged_at="t"), mp)
    data = json.loads(mp.read_text())
    assert isinstance(data, list) and len(data) == 1


def _audit():
    return {"app_id": "JOB-1", "verdict": "BLOCKED", "gate_blocks": 0,
            "findings": [{"doc": "essay_answer", "lens": "fabrication",
                          "severity": "BLOCK", "offending_text": "made it up",
                          "issue": "fabrication", "fix": "remove it",
                          "auto_fixable": False}],
            "summary": "blocked for fabrication"}


def _stage_two(tmp_path):
    """Write a manifest with JOB-1 and JOB-2 staged; return its path."""
    mp = tmp_path / "staged_applications.json"
    write_record(build_record(JobOutcome(job_id="JOB-1", status="needs_input"),
                              _job(), staged_at="t1"), mp)
    write_record(build_record(JobOutcome(job_id="JOB-2", status="ready_to_submit"),
                              {"id": "JOB-2", "company": "B", "title": "R", "url": "u"},
                              staged_at="t2"), mp)
    return mp


def test_attach_audit_attaches_by_job_id(tmp_path):
    mp = _stage_two(tmp_path)
    attach_audit(mp, "JOB-1", _audit())
    data = json.loads(mp.read_text())
    j1 = [d for d in data if d["job_id"] == "JOB-1"][0]
    assert j1["audit"]["verdict"] == "BLOCKED"
    assert j1["audit"]["findings"][0]["severity"] == "BLOCK"


def test_attach_audit_leaves_other_records_untouched(tmp_path):
    mp = _stage_two(tmp_path)
    attach_audit(mp, "JOB-1", _audit())
    data = json.loads(mp.read_text())
    j2 = [d for d in data if d["job_id"] == "JOB-2"][0]
    assert "audit" not in j2
    assert j2["status"] == "ready_to_submit"
    assert len(data) == 2  # no record added


def test_attach_audit_missing_file_is_noop(tmp_path):
    mp = tmp_path / "staged_applications.json"
    attach_audit(mp, "JOB-1", _audit())  # must not raise
    assert not mp.exists()


def test_attach_audit_corrupt_file_is_noop(tmp_path):
    mp = tmp_path / "staged_applications.json"
    mp.write_text("{ not json")
    attach_audit(mp, "JOB-1", _audit())  # must not raise
    assert mp.read_text() == "{ not json"  # untouched


def test_attach_audit_unknown_job_id_is_noop(tmp_path):
    mp = _stage_two(tmp_path)
    before = mp.read_text()
    attach_audit(mp, "JOB-999", _audit())
    assert mp.read_text() == before  # nothing changed, no bare record appended
