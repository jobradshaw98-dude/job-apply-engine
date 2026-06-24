# -*- coding: utf-8 -*-
"""Tests for apply_engine.tailor — the JD-driven tailoring generator.

All tests use a FAKE llm (a lambda returning canned JSON). Real `claude -p` is NEVER called.
"""
import json

import pytest

from apply_engine import config, tailor


# A long-enough JD to clear the MIN_JD_CHARS gate.
GOOD_JD = (
    "We are hiring a Staff FEA Engineer to own structural simulation for a wearable device. "
    "You will run non-linear static and dynamic analysis, drive design through finite element "
    "modeling, correlate simulation predictions against physical test data, and characterize "
    "materials and failure criteria under cyclic loading. You will partner with mechanical "
    "designers and test engineers to retire risk before tooling. Five plus years of FEA and "
    "test-to-simulation correlation experience required. " * 3
)


def _valid_pkg():
    """A canned package that should pass shape + all hard-rule guards."""
    return {
        "resume": {
            "current_bullets": [
                "Lead simulation analyst on the flagship woods family, owning FEA from concept "
                "to launch with LS-DYNA and HyperWorks and test-to-simulation correlation.",
                "Built a Python test-analysis agent that pulls data on a schedule and assembles "
                "the stakeholder report, cutting hands-on analysis effort by roughly 60%.",
            ],
            "stateuni_bullets": [
                "Designed prosthetic foot systems and polymer implant components to improve "
                "durability for mechanical applications.",
            ],
            "masc_bullets": [
                "Built an automated design-optimization framework in ANSYS and OptiSLang AMOP "
                "that searched texture geometry and cut contact stress up to 88%.",
            ],
            "skills": [
                {"label": "Engineering & Analysis",
                 "content": "FEA, LS-DYNA, HyperWorks, non-linear analysis, optimization"},
            ],
            "include_mobilityco": True,  # the generator MUST force this back to False
            "headline": "Staff FEA Engineer",
            "summary": "Simulation-led product engineer with ~5 years across consumer products.",
        },
        "cover": {
            "addressee": "Hiring Manager<br>Acme &mdash; Hardware Engineering<br>San Diego, CA",
            "salutation": "Dear Hiring Manager,",
            "paragraphs": ["P1 hook.", "P2 evidence.", "P3 differentiator.", "P4 close."],
        },
    }


def _is_cover_prompt(prompt: str) -> bool:
    """generate_tailored_package now makes TWO calls. The cover call's prompt is the one that says
    'COVER LETTER ONLY'; the resume call's says 'RESUME ONLY'. A fake llm uses this to return the
    right slice for whichever call it is handling."""
    return "COVER LETTER ONLY" in prompt


def _llm_returning(pkg_or_str):
    """Build a fake llm matching the two-call protocol.

    - If given a dict package, it returns the package's "resume" slice on the resume call and the
      "cover" slice on the cover call (detected via the prompt marker).
    - If given a raw string, it returns that string verbatim for BOTH calls — used by the
      code-fence / malformed-JSON tests where the same canned payload should drive every call. For
      a fenced/plain package string we still split by call so each slice parses to the right shape.
    """
    if isinstance(pkg_or_str, str):
        raw = pkg_or_str
        # Try to recover a package dict from the (possibly fenced) string so we can serve the
        # correct slice per call; if it isn't a package, just echo the raw string both times.
        parsed = None
        try:
            parsed = tailor._extract_json(raw)
        except Exception:
            parsed = None

        def _fn_str(prompt):
            if isinstance(parsed, dict) and "resume" in parsed and "cover" in parsed:
                slice_ = parsed["cover"] if _is_cover_prompt(prompt) else parsed["resume"]
                # Preserve a leading code fence if the original string was fenced, so the
                # strip-fence path is still exercised.
                body = json.dumps(slice_)
                if raw.lstrip().startswith("```"):
                    return "```json\n" + body + "\n```"
                return body
            return raw
        return _fn_str

    pkg = pkg_or_str

    def _fn(prompt):
        return json.dumps(pkg["cover"] if _is_cover_prompt(prompt) else pkg["resume"])
    return _fn


# ── happy path ──────────────────────────────────────────────────────────────

def test_happy_path_returns_exact_keys():
    job = {"id": "JOB-1", "company": "Acme", "role": "Staff FEA Engineer", "track": 2,
           "jd_text": GOOD_JD}
    out = tailor.generate_tailored_package(job, llm=_llm_returning(_valid_pkg()))

    assert set(out.keys()) == {"resume", "cover"}
    r = out["resume"]
    for key in ("current_bullets", "stateuni_bullets", "masc_bullets", "skills", "include_mobilityco"):
        assert key in r, f"missing resume key {key}"
    assert isinstance(r["current_bullets"], list) and r["current_bullets"]
    assert all(isinstance(s, dict) and "label" in s and "content" in s for s in r["skills"])
    # include_mobilityco must be forced False even though the LLM returned True.
    assert r["include_mobilityco"] is False

    c = out["cover"]
    for key in ("addressee", "salutation", "paragraphs"):
        assert key in c, f"missing cover key {key}"
    assert len(c["paragraphs"]) == 4


def test_happy_path_strips_code_fence():
    job = {"id": "JOB-1", "company": "Acme", "role": "FEA", "track": 2, "jd_text": GOOD_JD}
    fenced = "```json\n" + json.dumps(_valid_pkg()) + "\n```"
    out = tailor.generate_tailored_package(job, llm=_llm_returning(fenced))
    assert out["resume"]["include_mobilityco"] is False


# ── thin JD ─────────────────────────────────────────────────────────────────

def test_thin_jd_raises_value_error():
    job = {"id": "JOB-1", "company": "Acme", "role": "FEA", "jd_text": "too short"}
    with pytest.raises(ValueError, match="insufficient JD"):
        tailor.generate_tailored_package(job, llm=_llm_returning(_valid_pkg()))


def test_missing_jd_raises_value_error():
    job = {"id": "JOB-1", "company": "Acme", "role": "FEA"}
    with pytest.raises(ValueError, match="insufficient JD"):
        tailor.generate_tailored_package(job, llm=_llm_returning(_valid_pkg()))


# ── hard-rule post-validators ───────────────────────────────────────────────

def _job():
    return {"id": "JOB-1", "company": "Acme", "role": "FEA", "track": 2, "jd_text": GOOD_JD}


def test_reject_ansys_in_meridian_context():
    pkg = _valid_pkg()
    pkg["resume"]["current_bullets"][0] = (
        "Ran ANSYS Mechanical simulations on the Meridian woods family from concept to launch.")
    with pytest.raises(tailor.TailorError, match="ANSYS"):
        tailor.generate_tailored_package(_job(), llm=_llm_returning(pkg))


def test_ansys_allowed_in_masc_bullet():
    # ANSYS in a MASc bullet (no Meridian context) is legitimate and must NOT be rejected.
    pkg = _valid_pkg()
    pkg["resume"]["masc_bullets"][0] = (
        "Built an automated optimization framework in ANSYS and OptiSLang for the MASc thesis.")
    out = tailor.generate_tailored_package(_job(), llm=_llm_returning(pkg))
    assert out["resume"]["masc_bullets"]


def test_ansys_allowed_in_skills_entry():
    # ANSYS in the skills tool-inventory list is LEGITIMATE and required — skills is a capability
    # list, not an employer-attributed claim. Mirrors the gold APP-023 resume, whose skills line
    # reads "FEA — ANSYS Mechanical, LS-DYNA, ANSYS MBD, ANSYS OptiSlang, OptiStruct, HyperWorks".
    pkg = _valid_pkg()
    pkg["resume"]["skills"] = [
        {"label": "FEA",
         "content": "ANSYS Mechanical, LS-DYNA, ANSYS MBD, ANSYS OptiSlang, OptiStruct, "
                    "HyperWorks, HyperMesh"},
    ]
    out = tailor.generate_tailored_package(_job(), llm=_llm_returning(pkg))
    assert any("ANSYS" in s["content"] for s in out["resume"]["skills"])


def test_reject_bare_matlab_in_skills():
    # MATLAB must be dropped entirely from skills proficiencies (claims_ledger.md ~87). A skills
    # row that lists a bare "MATLAB" token is a hard reject, mirroring the ANSYS/MobilityCo guards.
    pkg = _valid_pkg()
    pkg["resume"]["skills"] = [
        {"label": "Engineering & Analysis",
         "content": "FEA, LS-DYNA, HyperWorks, structural optimization, MATLAB, Python (AI-orchestrated)"},
    ]
    with pytest.raises(tailor.TailorError, match="MATLAB"):
        tailor.generate_tailored_package(_job(), llm=_llm_returning(pkg))


def test_reject_bare_python_in_skills():
    # bare "Python" as a skills proficiency implies hand-coding fluency the applicant does not claim
    # (caused the JOB-237 calibration FAIL). It is a hard reject unless framed as AI-orchestrated.
    pkg = _valid_pkg()
    pkg["resume"]["skills"] = [
        {"label": "Software & Automation",
         "content": "Claude Code, Python, Flask, Git, Bash"},
    ]
    with pytest.raises(tailor.TailorError, match="(?i)python"):
        tailor.generate_tailored_package(_job(), llm=_llm_returning(pkg))


def test_ai_orchestrated_python_allowed_in_skills():
    # Python framed as AI-orchestrated tooling is the master-resume framing and must PASS.
    pkg = _valid_pkg()
    pkg["resume"]["skills"] = [
        {"label": "AI-Native Development",
         "content": "Claude Code · Codex · agentic automation · Git · Flask · Playwright · "
                    "Python-based tooling, AI-orchestrated rather than hand-coded"},
    ]
    out = tailor.generate_tailored_package(_job(), llm=_llm_returning(pkg))
    assert any("Python" in s["content"] for s in out["resume"]["skills"])


def test_python_in_meridian_bullet_not_flagged_as_skills_proficiency():
    # The bare-Python skills guard is scoped to SKILLS rows only. "Built a Python test-analysis
    # agent" in a Meridian bullet is an AI-built-tool claim, not a proficiency, and must PASS even
    # without an explicit AI qualifier token next to the word "Python".
    pkg = _valid_pkg()
    pkg["resume"]["current_bullets"][0] = (
        "Built a Python test-analysis agent that pulls data on a schedule and assembles the "
        "stakeholder report, cutting hands-on analysis effort by roughly 60%.")
    out = tailor.generate_tailored_package(_job(), llm=_llm_returning(pkg))
    assert out["resume"]["current_bullets"]


def test_reject_ansys_in_summary():
    # ANSYS in the summary has no Meridian token but still reads as Meridian use (employer is
    # implicitly Meridian on the resume) — must be rejected.
    pkg = _valid_pkg()
    pkg["resume"]["summary"] = (
        "Simulation-led engineer fluent in ANSYS Mechanical and non-linear contact analysis.")
    with pytest.raises(tailor.TailorError, match="ANSYS"):
        tailor.generate_tailored_package(_job(), llm=_llm_returning(pkg))


def test_reject_ansys_in_cover_paragraph():
    # ANSYS in a cover paragraph with no State University/MASc context reads as Meridian use — reject.
    pkg = _valid_pkg()
    pkg["cover"]["paragraphs"][1] = (
        "At Meridian I ran ANSYS simulations to retire structural risk before tooling.")
    with pytest.raises(tailor.TailorError, match="ANSYS"):
        tailor.generate_tailored_package(_job(), llm=_llm_returning(pkg))


def test_reject_mobilityco():
    pkg = _valid_pkg()
    pkg["cover"]["paragraphs"][1] = "I also founded MobilityCo, a homecare hospital bed startup."
    with pytest.raises(tailor.TailorError, match="MobilityCo"):
        tailor.generate_tailored_package(_job(), llm=_llm_returning(pkg))


def test_reject_signal_intel():
    pkg = _valid_pkg()
    pkg["resume"]["skills"].append(
        {"label": "Ventures", "content": "Signal Intel B2B lead-gen pipeline (ariasignals.com)"})
    with pytest.raises(tailor.TailorError, match="Signal Intel"):
        tailor.generate_tailored_package(_job(), llm=_llm_returning(pkg))


def test_reject_portfolio_figure():
    pkg = _valid_pkg()
    pkg["resume"]["current_bullets"][0] = (
        "Drove a 6% YoY revenue increase on a $198M woods portfolio.")
    with pytest.raises(tailor.TailorError, match="198M"):
        tailor.generate_tailored_package(_job(), llm=_llm_returning(pkg))


def test_reject_adopted_frameworks():
    pkg = _valid_pkg()
    pkg["resume"]["current_bullets"].append(
        "Built agentic frameworks now adopted across R&D teams.")
    with pytest.raises(tailor.TailorError, match="adopted|rolling out"):
        tailor.generate_tailored_package(_job(), llm=_llm_returning(pkg))


def test_reject_empty_current_bullets():
    pkg = _valid_pkg()
    pkg["resume"]["current_bullets"] = []
    with pytest.raises(tailor.TailorError, match="current_bullets"):
        tailor.generate_tailored_package(_job(), llm=_llm_returning(pkg))


def test_reject_empty_masc_bullets():
    # A full package requires the MASc block; build.py silently skips an empty one, so the
    # validator must fail loud.
    pkg = _valid_pkg()
    pkg["resume"]["masc_bullets"] = []
    with pytest.raises(tailor.TailorError, match="masc_bullets"):
        tailor.generate_tailored_package(_job(), llm=_llm_returning(pkg))


def test_reject_empty_stateuni_bullets():
    pkg = _valid_pkg()
    pkg["resume"]["stateuni_bullets"] = []
    with pytest.raises(tailor.TailorError, match="stateuni_bullets"):
        tailor.generate_tailored_package(_job(), llm=_llm_returning(pkg))


def test_reject_empty_cover_paragraphs():
    pkg = _valid_pkg()
    pkg["cover"]["paragraphs"] = []
    with pytest.raises(tailor.TailorError, match="paragraph"):
        tailor.generate_tailored_package(_job(), llm=_llm_returning(pkg))


# ── malformed JSON: one retry then raise ────────────────────────────────────

def test_malformed_json_retries_once_then_raises():
    calls = {"n": 0}

    def _bad_llm(_prompt):
        calls["n"] += 1
        return "this is not json at all {oops"

    with pytest.raises(tailor.TailorError, match="valid JSON"):
        tailor.generate_tailored_package(_job(), llm=_bad_llm)
    assert calls["n"] == 2, "expected exactly one retry (2 total calls)"


def test_malformed_then_valid_on_retry_succeeds():
    # The RESUME call returns garbage first (forcing its one retry to fire) then valid JSON; the
    # COVER call returns valid JSON first try. Each call serves only its own slice.
    pkg = _valid_pkg()
    resume_json = json.dumps(pkg["resume"])
    cover_json = json.dumps(pkg["cover"])
    calls = {"n": 0}

    def _llm(prompt):
        calls["n"] += 1
        if _is_cover_prompt(prompt):
            return cover_json
        # resume call: garbage on its first invocation, valid JSON on the retry
        return "garbage not json" if calls["n"] == 1 else resume_json

    out = tailor.generate_tailored_package(_job(), llm=_llm)
    # 3 calls total: resume (garbage) + resume retry (valid) + cover (valid).
    assert calls["n"] == 3
    assert out["resume"]["include_mobilityco"] is False


# ── validation-repair loop ──────────────────────────────────────────────────

def test_repair_loop_self_corrects_recoverable_violation():
    # The RESUME call first returns a slice that trips a hard-rule guard ("adopted ... frameworks"),
    # then a CLEAN resume on the repair call. The cover call is clean first try. The package should
    # SUCCEED (the repair loop self-corrected) and return the clean resume.
    clean = _valid_pkg()
    dirty_resume = json.loads(json.dumps(clean["resume"]))
    dirty_resume["current_bullets"].append(
        "Built agentic frameworks now adopted across every R&D team.")
    resume_clean_json = json.dumps(clean["resume"])
    resume_dirty_json = json.dumps(dirty_resume)
    cover_json = json.dumps(clean["cover"])

    calls = {"resume": 0, "cover": 0}

    def _llm(prompt):
        if _is_cover_prompt(prompt):
            calls["cover"] += 1
            return cover_json
        calls["resume"] += 1
        # First resume attempt is dirty; the repair attempt is clean.
        return resume_dirty_json if calls["resume"] == 1 else resume_clean_json

    out = tailor.generate_tailored_package(_job(), llm=_llm)

    # resume: original (dirty) + 1 repair (clean) = 2 calls; cover: 1 clean call.
    assert calls["resume"] == 2, "resume should be original + exactly one repair"
    assert calls["cover"] == 1, "cover should not be re-run when only the resume violated"
    # The returned resume is the clean one (no 'adopted' bullet).
    assert not any("adopted" in b for b in out["resume"]["current_bullets"])
    assert out["resume"]["include_mobilityco"] is False


def test_repair_loop_exhausted_raises_after_max_repairs():
    # The RESUME call returns a violating slice on EVERY attempt. After original + 2 repairs the
    # generator must HALT with TailorError (no partial, no master fallback) — and must have tried
    # exactly 3 times (original + 2 repairs), not stopped after one.
    clean = _valid_pkg()
    dirty_resume = json.loads(json.dumps(clean["resume"]))
    dirty_resume["current_bullets"].append(
        "Built agentic frameworks now adopted across every R&D team.")
    dirty_resume_json = json.dumps(dirty_resume)
    cover_json = json.dumps(clean["cover"])

    calls = {"resume": 0, "cover": 0}

    def _llm(prompt):
        if _is_cover_prompt(prompt):
            calls["cover"] += 1
            return cover_json
        calls["resume"] += 1
        return dirty_resume_json

    with pytest.raises(tailor.TailorError, match="adopted|rolling out"):
        tailor.generate_tailored_package(_job(), llm=_llm)

    assert calls["resume"] == 3, "resume should be tried original + 2 repairs = 3 times"
    # The cover call never runs because the resume loop raised first.
    assert calls["cover"] == 0, "cover must not run once the resume loop exhausts and halts"


def test_no_violation_path_makes_no_extra_calls():
    # Clean on first try for BOTH sections: exactly one resume call + one cover call, no repairs.
    calls = {"resume": 0, "cover": 0}
    pkg = _valid_pkg()

    def _llm(prompt):
        if _is_cover_prompt(prompt):
            calls["cover"] += 1
            return json.dumps(pkg["cover"])
        calls["resume"] += 1
        return json.dumps(pkg["resume"])

    out = tailor.generate_tailored_package(_job(), llm=_llm)
    assert calls == {"resume": 1, "cover": 1}, "clean-on-first-try must make no repair calls"
    assert out["resume"]["include_mobilityco"] is False


# ── APP-record creation path (CLI write helper) ─────────────────────────────

def test_app_record_creation(tmp_path, monkeypatch):
    jobs_path = tmp_path / "jobs.json"
    apps_path = tmp_path / "applications.json"
    jobs_path.write_text(json.dumps([
        {"id": "JOB-1", "company": "Acme", "title": "Staff FEA Engineer", "track": 2},
    ]), encoding="utf-8")
    apps_path.write_text(json.dumps([
        {"id": "APP-001", "job_id": "JOB-OTHER", "company": "Other"},
    ]), encoding="utf-8")

    monkeypatch.setattr(config, "JOBS_JSON", jobs_path)
    monkeypatch.setattr(config, "APPLICATIONS_JSON", apps_path)

    pkg = {"resume": _valid_pkg()["resume"], "cover": _valid_pkg()["cover"]}
    app_id = tailor._write_app_record("JOB-1", pkg)

    apps = json.loads(apps_path.read_text(encoding="utf-8"))
    assert app_id == "APP-002", "should mint the next sequential APP id"
    new = next(a for a in apps if a["id"] == app_id)
    assert new["job_id"] == "JOB-1"
    assert new["company"] == "Acme"
    assert new["role"] == "Staff FEA Engineer"   # from job["title"]
    assert new["resume"]["current_bullets"]
    assert new["cover"]["paragraphs"]


# ── render subprocess timeout / failure ─────────────────────────────────────

def test_render_raises_tailorerror_on_timeout(monkeypatch):
    # A hung build.py (the known Edge --print-to-pdf no-op-with-browser-open class) must NOT wedge
    # the stage run forever: subprocess.run carries a timeout, and a TimeoutExpired surfaces as a
    # TailorError (which main()/ensure_tailored_package convert into a needs_build halt). Assert the
    # call raises rather than hangs, and that a real timeout= was passed to subprocess.run.
    import subprocess

    captured = {}

    def fake_run(cmd, **kw):
        captured.update(kw)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kw.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(tailor.TailorError, match="timed out"):
        tailor._render("APP-999")

    assert captured.get("timeout"), "_render must pass a timeout= to subprocess.run"


def test_render_raises_tailorerror_on_nonzero_exit(monkeypatch):
    # A non-zero build.py exit (e.g. a real render error) must also raise TailorError, not return
    # silently — otherwise the run would proceed to attach whatever stale PDFs happen to be on disk.
    import subprocess
    import types as _types

    def fake_run(cmd, **kw):
        return _types.SimpleNamespace(returncode=1, stdout="", stderr="build blew up")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(tailor.TailorError, match="render failed"):
        tailor._render("APP-999")


def test_app_record_match_existing_updates_in_place(tmp_path, monkeypatch):
    jobs_path = tmp_path / "jobs.json"
    apps_path = tmp_path / "applications.json"
    jobs_path.write_text(json.dumps([
        {"id": "JOB-1", "company": "Acme", "title": "FEA", "track": 2},
    ]), encoding="utf-8")
    apps_path.write_text(json.dumps([
        {"id": "APP-007", "job_id": "JOB-1", "company": "Acme", "status": "drafting"},
    ]), encoding="utf-8")

    monkeypatch.setattr(config, "JOBS_JSON", jobs_path)
    monkeypatch.setattr(config, "APPLICATIONS_JSON", apps_path)

    pkg = {"resume": _valid_pkg()["resume"], "cover": _valid_pkg()["cover"]}
    app_id = tailor._write_app_record("JOB-1", pkg)

    apps = json.loads(apps_path.read_text(encoding="utf-8"))
    assert app_id == "APP-007", "should reuse the existing record matched by job_id"
    assert len(apps) == 1, "must not create a duplicate"
    # The write helper persists the package it is handed verbatim (hard-rule forcing happens
    # upstream in generate_tailored_package, not here). Confirm the content landed.
    assert apps[0]["resume"]["current_bullets"]
    assert apps[0]["cover"]["paragraphs"]


# --- regression: engine-stub records without an APP id (the batch KeyError: 'id') -----------
def test_write_app_record_backfills_id_on_idless_stub(tmp_path, monkeypatch):
    """An engine-written stub record (job_id/status only, NO 'id') matched by job_id must get a
    real APP id backfilled before resume/cover are written — else `return app['id']` KeyErrors
    and render runs `--job ?`. (Live batch bug 2026-06-08: JOB-227/242/233/248.)"""
    import json as _json
    from apply_engine import tailor, config
    apps = tmp_path / "applications.json"
    jobs = tmp_path / "jobs.json"
    # an id-less engine stub + one real APP record (so _next_app_id has a baseline)
    apps.write_text(_json.dumps([
        {"id": "APP-005", "job_id": "JOB-999", "resume": {}, "cover": {}},
        {"job_id": "JOB-233", "status": "needs_input", "apply_run_dir": "x"},  # NO id
    ]), encoding="utf-8")
    jobs.write_text(_json.dumps([{"id": "JOB-233", "company": "DevRev", "title": "FDE", "track": 5}]), encoding="utf-8")
    monkeypatch.setattr(config, "APPLICATIONS_JSON", apps)
    monkeypatch.setattr(config, "JOBS_JSON", jobs)
    pkg = {"resume": {"current_bullets": ["x"]}, "cover": {"paragraphs": ["y"]}}
    app_id = tailor._write_app_record("JOB-233", pkg)
    assert app_id and app_id.startswith("APP-")        # not "?" / not KeyError
    rec = next(a for a in _json.loads(apps.read_text(encoding="utf-8")) if a.get("job_id") == "JOB-233")
    assert rec["id"] == app_id and rec["resume"] == pkg["resume"]
    # ensure_app_id is idempotent on an already-id'd record
    assert tailor.ensure_app_id("JOB-233") == app_id


# ── rebuild_tailored_package (the --rebuild escape hatch) ───────────────────

def _stub_apps(tmp_path, monkeypatch, old_resume_marker):
    """A staged record carrying an OLD package, wired into config."""
    apps_path = tmp_path / "applications.json"
    old = _valid_pkg()
    old["resume"]["current_bullets"] = [old_resume_marker]
    apps_path.write_text(json.dumps([
        {"id": "APP-009", "job_id": "JOB-9", "company": "Acme",
         "resume": old["resume"], "cover": old["cover"], "status": "ready_to_submit"},
    ]), encoding="utf-8")
    monkeypatch.setattr(config, "APPLICATIONS_JSON", apps_path)
    return apps_path


def test_rebuild_replaces_package_on_successful_generate(tmp_path, monkeypatch):
    # A clean regenerate OVERWRITES the old resume+cover with the fresh ones and re-renders.
    apps_path = _stub_apps(tmp_path, monkeypatch, "OLD-bullet")
    fresh = _valid_pkg()
    fresh["resume"]["current_bullets"] = ["NEW-bullet"]
    monkeypatch.setattr(tailor, "generate_tailored_package", lambda job: fresh)
    rendered = {}
    monkeypatch.setattr(tailor, "_render", lambda app_id: rendered.setdefault("id", app_id))

    app_id = tailor.rebuild_tailored_package({"id": "JOB-9", "company": "Acme"})

    rec = next(a for a in json.loads(apps_path.read_text(encoding="utf-8"))
               if a["job_id"] == "JOB-9")
    assert rec["resume"]["current_bullets"] == ["NEW-bullet"], "fresh package must replace old"
    assert rendered["id"] == app_id, "must re-render the new PDFs"
    assert rec["status"] == "ready_to_submit", "must not touch unrelated fields"


def test_rebuild_keeps_old_package_when_generate_fails(tmp_path, monkeypatch):
    # THE footgun guard: if generation raises (LLM down / thin JD / validation-exhausted), the OLD
    # package must survive intact — never strip a submit-ready app to packageless.
    apps_path = _stub_apps(tmp_path, monkeypatch, "OLD-bullet")

    def boom(job):
        raise tailor.TailorError("LLM down")
    monkeypatch.setattr(tailor, "generate_tailored_package", boom)
    monkeypatch.setattr(tailor, "_render", lambda app_id: (_ for _ in ()).throw(
        AssertionError("must not render on failed generate")))

    with pytest.raises(tailor.TailorError):
        tailor.rebuild_tailored_package({"id": "JOB-9", "company": "Acme"})

    rec = next(a for a in json.loads(apps_path.read_text(encoding="utf-8"))
               if a["job_id"] == "JOB-9")
    assert rec["resume"]["current_bullets"] == ["OLD-bullet"], "old package must remain intact"
    assert "cover" in rec, "old cover must remain intact"
