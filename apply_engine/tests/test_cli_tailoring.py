# -*- coding: utf-8 -*-
"""The live-stage path of cli.main must guarantee a TAILORED package before staging, and must
NEVER fall back to the generic master resume.

These tests monkeypatch tailor.generate_tailored_package / _write_app_record / _render and the
orchestrator so NO real claude -p call and NO real browser run ever happens. They prove:

  1. APP missing tailored content -> main (live-stage) calls the generator, writes+renders, and
     proceeds to stage.
  2. The generator RAISING -> status becomes needs_build and NO master resume is attached
     (ensure_pdfs / apply_to_job are never reached).
  3. APP already has tailored resume+cover -> generator is NOT called (uses the existing edit).
"""
import json
import types

from apply_engine import cli, config, tailor


JOB = {
    "id": "JOB-500",
    "company": "Acme Robotics",
    "role": "Staff FEA Engineer",
    "url": "https://boards.greenhouse.io/acme/jobs/123",
    "jd_text": "X" * 600,
}


def _setup(tmp_path, monkeypatch, apps_records):
    """Point config at temp jobs/applications.json and stub everything downstream of the tailoring
    trigger so main() can run headless with no browser and no LLM. Returns the apps_json path."""
    jobs_json = tmp_path / "jobs.json"
    jobs_json.write_text(json.dumps([JOB]), encoding="utf-8")
    apps_json = tmp_path / "applications.json"
    apps_json.write_text(json.dumps(apps_records), encoding="utf-8")

    monkeypatch.setattr(config, "JOBS_JSON", jobs_json)
    monkeypatch.setattr(config, "APPLICATIONS_JSON", apps_json)

    # Stub the form-fill machinery so main() never touches a browser or real PDFs. build_answers
    # and apply_to_job are imported into cli's namespace at module load, so patch them there.
    monkeypatch.setattr(cli, "build_answers", lambda **kw: object())
    monkeypatch.setattr(cli, "build_hooks", lambda answer, job, recon=False: (None, None, ""))

    outcome = types.SimpleNamespace(
        job_id=JOB["id"], status="ready_to_submit", submitted=False, verify_ok=True,
        run_dir=str(tmp_path / "run"), filled_fields=[], work_auth_answers=[], generated=[],
        corrections=[], unfilled_required=[], halt_reason="", error="",
    )
    monkeypatch.setattr(cli, "apply_to_job", lambda **kw: outcome)
    # ensure_pdfs would otherwise raise NoTailoredPDF (no real PDFs on disk); the tailoring-trigger
    # tests care about generation, not PDF resolution, so stub it to a benign pair.
    monkeypatch.setattr(cli, "ensure_pdfs",
                        lambda job, allow_master=False: (tmp_path / "r.pdf", tmp_path / "c.pdf"))
    return apps_json


def test_live_stage_generates_when_app_missing_tailored(tmp_path, monkeypatch):
    """APP record exists but has no resume/cover -> generator IS called, result written+rendered,
    and the run proceeds to stage (returns 0)."""
    apps_json = _setup(tmp_path, monkeypatch,
                       [{"id": "APP-050", "job_id": "JOB-500", "company": "Acme Robotics"}])

    calls = {"gen": 0, "write": 0, "render": 0}

    def fake_gen(job, **kw):
        calls["gen"] += 1
        return {"resume": {"current_bullets": ["b"]}, "cover": {"paragraphs": ["p"]}}

    def fake_write(job_id, pkg):
        calls["write"] += 1
        return "APP-050"

    def fake_render(app_id):
        calls["render"] += 1

    monkeypatch.setattr(tailor, "generate_tailored_package", fake_gen)
    monkeypatch.setattr(tailor, "_write_app_record", fake_write)
    monkeypatch.setattr(tailor, "_render", fake_render)

    rc = cli.main(["--job", "JOB-500", "--live"])
    assert rc == 0
    assert calls == {"gen": 1, "write": 1, "render": 1}

    # status was recorded from the (successful) stage outcome, not needs_build
    rec = next(r for r in json.loads(apps_json.read_text(encoding="utf-8"))
               if r["job_id"] == "JOB-500")
    assert rec["status"] == "ready_to_submit"


def test_live_stage_halts_needs_build_when_generation_raises(tmp_path, monkeypatch):
    """Generator raises (thin JD / LLM down / validation-exhausted) -> status becomes needs_build,
    the run returns non-zero, and NO master resume is attached (ensure_pdfs/apply_to_job never run)."""
    apps_json = _setup(tmp_path, monkeypatch,
                       [{"id": "APP-051", "job_id": "JOB-500", "company": "Acme Robotics"}])

    def boom(job, **kw):
        raise tailor.TailorError("validation exhausted after repairs")

    monkeypatch.setattr(tailor, "generate_tailored_package", boom)

    # If we somehow reach PDF resolution or staging, fail loudly — the halt must short-circuit.
    def must_not_run(*a, **k):
        raise AssertionError("reached form-fill path after a tailoring halt")

    monkeypatch.setattr(cli, "ensure_pdfs", must_not_run)
    monkeypatch.setattr(cli, "apply_to_job", must_not_run)

    rc = cli.main(["--job", "JOB-500", "--live"])
    assert rc == 2

    rec = next(r for r in json.loads(apps_json.read_text(encoding="utf-8"))
               if r["job_id"] == "JOB-500")
    assert rec["status"] == "needs_build"
    # No master resume / tailored content was attached.
    assert "resume" not in rec
    assert "tailoring halt" in rec.get("apply_note", "")


def test_live_stage_existing_tailored_rerenders_but_never_regenerates(tmp_path, monkeypatch):
    """APP already carries a non-empty tailored resume AND cover (e.g. a dashboard hand-edit) ->
    the generator/write helper are NOT called (the stored content is kept verbatim), but the PDFs
    ARE re-rendered from that stored content so a content edit that skipped the render can't leave
    a STALE PDF attached. The run stages successfully."""
    apps_json = _setup(tmp_path, monkeypatch, [{
        "id": "APP-052", "job_id": "JOB-500", "company": "Acme Robotics",
        "resume": {"current_bullets": ["hand-edited bullet"]},
        "cover": {"paragraphs": ["hand-edited paragraph"]},
    }])

    def must_not_generate(*a, **k):
        raise AssertionError("generator/write was called despite existing tailored content")

    render_calls = {"n": 0, "app_id": None}

    def fake_render(app_id):
        render_calls["n"] += 1
        render_calls["app_id"] = app_id

    monkeypatch.setattr(tailor, "generate_tailored_package", must_not_generate)
    monkeypatch.setattr(tailor, "_write_app_record", must_not_generate)
    monkeypatch.setattr(tailor, "_render", fake_render)

    rc = cli.main(["--job", "JOB-500", "--live"])
    assert rc == 0

    # The PDFs were re-rendered (from the stored content) for the existing APP id...
    assert render_calls["n"] == 1, "existing-content path must re-render exactly once"
    assert render_calls["app_id"] == "APP-052"

    rec = next(r for r in json.loads(apps_json.read_text(encoding="utf-8"))
               if r["job_id"] == "JOB-500")
    # ...and the hand-edited content is untouched (never regenerated) and the stage succeeded.
    assert rec["resume"]["current_bullets"] == ["hand-edited bullet"]
    assert rec["status"] == "ready_to_submit"


def test_dry_run_never_triggers_tailoring(tmp_path, monkeypatch):
    """The tailoring trigger fires only on the real live-stage path. A dry run (default) must NOT
    call the generator even when the APP has no tailored content."""
    _setup(tmp_path, monkeypatch,
           [{"id": "APP-053", "job_id": "JOB-500", "company": "Acme Robotics"}])

    def must_not_generate(*a, **k):
        raise AssertionError("generator was called on a dry run")

    monkeypatch.setattr(tailor, "generate_tailored_package", must_not_generate)

    rc = cli.main(["--job", "JOB-500"])  # no --live -> dry_run True
    assert rc == 0
