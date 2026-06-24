"""Tests for the finish_job orchestration entry that the dashboard backend calls, plus
the manifest helpers. The fail-fast submit pre-check and the missing-record path do NOT
open a browser, so they are fully testable here. The full live navigate->replay->submit
path needs a real ATS and is noted in the summary as live-only."""
import json

from apply_engine.finish import finish_job, _load_record, _mark_submitted, _job_from_record


def _manifest(tmp_path, records):
    mp = tmp_path / "staged_applications.json"
    mp.write_text(json.dumps(records, indent=2), encoding="utf-8")
    return mp


def _blocked_record():
    # Blocked by the DETERMINISTIC gate (gate_blocks > 0) — the one content gate that still hard-
    # blocks submit (2026-06-22 demotion). The advisory LLM verdict no longer fail-fasts a submit.
    return {"job_id": "JOB-9", "status": "ready_to_submit", "submitted": False,
            "url": "https://boards.greenhouse.io/acme/jobs/9",
            "audit": {"verdict": "BLOCKED", "gate_blocks": 1},
            "work_auth": [{"field": "sponsor", "answer": "No"}]}


def test_finish_job_missing_record_no_browser(tmp_path):
    mp = _manifest(tmp_path, [])
    res = finish_job("JOB-NONE", submit=False, headless=True,
                     runs_root=tmp_path / "runs", profile_dir=tmp_path / "p",
                     manifest_path=mp)
    assert res["ok"] is False
    assert "no staged record" in res["reason"]


def test_finish_job_submit_failfast_before_browser(tmp_path):
    # a deterministic-gate-blocked record with submit=True must be refused BEFORE any browser
    # launches (can_submit fail-fast). The deterministic gate is the hard backstop post-demotion.
    mp = _manifest(tmp_path, [_blocked_record()])
    res = finish_job("JOB-9", submit=True, headless=True,
                     runs_root=tmp_path / "runs", profile_dir=tmp_path / "p",
                     manifest_path=mp)
    assert res["ok"] is False
    assert res["submitted"] is False
    assert "deterministic gate" in res["reason"].lower()


def test_finish_job_never_throws(tmp_path):
    # a corrupt manifest must not raise out of finish_job
    mp = tmp_path / "staged_applications.json"
    mp.write_text("{ not json", encoding="utf-8")
    res = finish_job("JOB-1", submit=True, headless=True,
                     runs_root=tmp_path / "runs", profile_dir=tmp_path / "p",
                     manifest_path=mp)
    assert res["ok"] is False  # no record loaded -> refused, not crashed


def test_load_record_round_trip(tmp_path):
    rec = {"job_id": "JOB-1", "status": "ready_to_submit"}
    mp = _manifest(tmp_path, [rec, {"job_id": "JOB-2"}])
    got = _load_record(mp, "JOB-1")
    assert got["status"] == "ready_to_submit"
    assert _load_record(mp, "JOB-404") is None


def test_load_record_missing_file(tmp_path):
    assert _load_record(tmp_path / "nope.json", "JOB-1") is None


def test_job_from_record_maps_fields():
    rec = {"job_id": "JOB-1", "company": "Acme", "role": "FEA Eng",
           "url": "https://x", "ats": "greenhouse"}
    job = _job_from_record(rec)
    assert job["id"] == "JOB-1"
    assert job["title"] == "FEA Eng"      # role -> title (what build_answers expects)
    assert job["company"] == "Acme"
    assert job["url"] == "https://x"


def test_mark_submitted_stamps_record(tmp_path):
    mp = _manifest(tmp_path, [{"job_id": "JOB-1", "status": "ready_to_submit",
                               "submitted": False}])
    _mark_submitted(mp, "JOB-1", "2026-06-02T10:00:00")
    data = json.loads(mp.read_text())
    assert data[0]["submitted"] is True
    assert data[0]["status"] == "submitted"
    assert data[0]["submitted_at"] == "2026-06-02T10:00:00"


def test_mark_submitted_unknown_id_noop(tmp_path):
    mp = _manifest(tmp_path, [{"job_id": "JOB-1", "submitted": False}])
    before = mp.read_text()
    _mark_submitted(mp, "JOB-X", "t")
    assert mp.read_text() == before


def test_mark_submitted_missing_file_noop(tmp_path):
    _mark_submitted(tmp_path / "nope.json", "JOB-1", "t")  # must not raise
