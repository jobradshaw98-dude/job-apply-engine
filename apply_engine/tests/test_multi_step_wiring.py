"""The multi-step (Workday) path must thread the grounded-answer hooks
(answer_fn / audit_fn / facts) down to the adapter so custom-question escalations can
be resolved by the gated LLM picker. No browser — a fake adapter records what it got."""
from pathlib import Path

from apply_engine.orchestrator import _stage_multi_step, JobOutcome
from apply_engine.run_context import RunContext


class _FakePage:
    def screenshot(self, path=None, full_page=False):
        Path(path).write_bytes(b"x")


class _RecordingAdapter:
    name = "fake-multistep"
    multi_step = True

    def __init__(self):
        self.got = {}

    def stage_application(self, page, answers, profile, job,
                          answer_fn=None, audit_fn=None, facts=""):
        self.got = {"answer_fn": answer_fn, "audit_fn": audit_fn, "facts": facts}
        return {"reached": "review", "submitted": False,
                "work_auth_verified": "Will you require sponsorship? No",
                "escalations": [], "filled_steps": ["s1"], "error": None}


def _ctx(tmp_path):
    return RunContext(job_id="JOB-X", runs_root=tmp_path / "runs", stamp="t")


def test_stage_multi_step_forwards_answer_hooks(tmp_path):
    adapter = _RecordingAdapter()
    ctx = _ctx(tmp_path)
    out = JobOutcome(job_id="JOB-X", status="error", run_dir=str(ctx.run_dir))
    af = lambda p: "ans"
    gf = lambda t: []
    res = _stage_multi_step(adapter, _FakePage(), object(), {"id": "JOB-X"}, ctx, out,
                            answer_fn=af, audit_fn=gf, facts="FACTS-BLOB")
    assert adapter.got["answer_fn"] is af
    assert adapter.got["audit_fn"] is gf
    assert adapter.got["facts"] == "FACTS-BLOB"
    # a clean review with no escalations + verified no-red-flag work-auth still stages
    assert res.status == "ready_to_submit"


def test_stage_multi_step_works_without_hooks(tmp_path):
    # backward-compatible: no hooks passed -> adapter still called, escalate-everything default
    adapter = _RecordingAdapter()
    ctx = _ctx(tmp_path)
    out = JobOutcome(job_id="JOB-X", status="error", run_dir=str(ctx.run_dir))
    res = _stage_multi_step(adapter, _FakePage(), object(), {"id": "JOB-X"}, ctx, out)
    assert adapter.got["answer_fn"] is None
    assert adapter.got["facts"] == ""
    assert res.status == "ready_to_submit"
