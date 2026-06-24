import json
from apply_engine.run_context import RunContext


def test_run_context_creates_dir_and_logs(tmp_path):
    ctx = RunContext(job_id="JOB-999", runs_root=tmp_path)
    assert ctx.run_dir.exists()
    assert ctx.run_dir.name.startswith("JOB-999")

    ctx.log("step", "filled first name", field="first_name", value="Sam")
    ctx.log("verify", "match ok")

    lines = (ctx.run_dir / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["kind"] == "step"
    assert rec["field"] == "first_name"
    assert rec["seq"] == 1


def test_run_context_screenshot_path_increments(tmp_path):
    ctx = RunContext(job_id="JOB-999", runs_root=tmp_path)
    p1 = ctx.next_screenshot_path("login")
    p2 = ctx.next_screenshot_path("form")
    assert p1.name == "step_01_login.png"
    assert p2.name == "step_02_form.png"
