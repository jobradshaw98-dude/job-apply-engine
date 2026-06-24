import json
from apply_engine.cli import record_status


def test_record_status_upserts(tmp_path):
    apps = tmp_path / "applications.json"
    apps.write_text(json.dumps([
        {"id": "APP-001", "job_id": "JOB-131", "status": "Drafted"}
    ]), encoding="utf-8")

    record_status(apps, job_id="JOB-131", status="ready_to_submit",
                  run_dir="/runs/JOB-131_x", note="staged")
    data = json.loads(apps.read_text(encoding="utf-8"))
    rec = [r for r in data if r["job_id"] == "JOB-131"][0]
    assert rec["status"] == "ready_to_submit"
    assert rec["apply_run_dir"] == "/runs/JOB-131_x"


def test_record_status_creates_new_row(tmp_path):
    apps = tmp_path / "applications.json"
    apps.write_text("[]", encoding="utf-8")
    record_status(apps, job_id="JOB-999", status="needs_sam",
                  run_dir="/runs/x", note="citizenship halt")
    data = json.loads(apps.read_text(encoding="utf-8"))
    assert data[0]["job_id"] == "JOB-999"
    assert data[0]["status"] == "needs_sam"
