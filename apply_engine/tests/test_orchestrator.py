from pathlib import Path
import pytest
from apply_engine.orchestrator import apply_to_job, JobOutcome
from apply_engine.source_data import Answers


@pytest.fixture
def answers(tmp_path):
    resume = tmp_path / "resume.pdf"; resume.write_bytes(b"%PDF-1.4")
    return Answers(values={
        "first_name": "Sam", "last_name": "Rivera",
        "email": "sam.rivera@example.com", "phone": "555-555-0100",
    }, resume_pdf=resume, cover_pdf=None)


def test_apply_stages_to_brink_and_answers_sponsorship_no(fixture_server, answers, tmp_path):
    job = {"id": "JOB-TEST", "company": "Acme", "title": "FEA Engineer",
           "url": f"{fixture_server}/greenhouse_form.html"}
    outcome = apply_to_job(
        job=job, answers=answers, runs_root=tmp_path / "runs",
        profile_dir=tmp_path / "prof", headless=True, dry_run=True,
        ats_override="greenhouse",
    )
    assert isinstance(outcome, JobOutcome)
    assert outcome.status == "ready_to_submit"
    assert outcome.submitted is False
    # sponsorship auto-answered No, captured in the audit summary
    assert any(s["field"] == "sponsor" and s["answer"] == "No" for s in outcome.work_auth_answers)
    # verification passed
    assert outcome.verify_ok is True
    # audit log + a screenshot exist
    assert (Path(outcome.run_dir) / "audit.jsonl").exists()
    assert list(Path(outcome.run_dir).glob("step_*_*.png"))


def test_apply_halts_on_citizenship_question(fixture_server, answers, tmp_path, monkeypatch):
    # Patch the adapter to surface a citizenship question -> must HALT, not guess
    import apply_engine.adapters.greenhouse as gh
    from apply_engine.adapters.base import WorkAuthQuestion
    monkeypatch.setattr(gh.GreenhouseAdapter, "find_work_auth_questions",
                        lambda self, page: [WorkAuthQuestion(
                            label="What is your country of citizenship?",
                            selector="#q_sponsor", kind="select")])
    job = {"id": "JOB-CIT", "company": "Acme", "title": "Eng",
           "url": f"{fixture_server}/greenhouse_form.html"}
    outcome = apply_to_job(job=job, answers=answers, runs_root=tmp_path / "runs",
                           profile_dir=tmp_path / "prof", headless=True, dry_run=True,
                           ats_override="greenhouse")
    assert outcome.status == "needs_sam"
    assert "citizenship" in outcome.halt_reason.lower()


def test_apply_halts_on_geography_mismatch_work_auth(fixture_server, answers, tmp_path, monkeypatch):
    """G4: a work-AUTHORIZATION question (which for a US role would auto-answer Yes) must instead
    HALT to needs_sam when the role is based abroad (Cresta "Australia (Remote)") — the applicant's
    authorization does not extend to Australia, so auto-Yes there is a truthfulness violation. The
    blocker is an ANSWERABLE work_auth human_blocker, never an escalate/auto-answer."""
    import apply_engine.adapters.greenhouse as gh
    from apply_engine.adapters.base import WorkAuthQuestion
    monkeypatch.setattr(gh.GreenhouseAdapter, "find_work_auth_questions",
                        lambda self, page: [WorkAuthQuestion(
                            label="Are you authorized to work in this country?",
                            selector="#q_auth", kind="select")])
    # answer_yes must NEVER be called for a foreign role — fail loudly if the resolver leaked an
    # auto-Yes past the geography gate.
    def _boom(self, page, q):  # noqa: ANN001
        raise AssertionError("answer_yes called on a foreign-role work-auth question (auto-Yes leak)")
    monkeypatch.setattr(gh.GreenhouseAdapter, "answer_yes", _boom)

    job = {"id": "JOB-AUS", "company": "Cresta", "title": "Applied AI Engineer",
           "location": "Australia (Remote)",
           "url": f"{fixture_server}/greenhouse_form.html"}
    outcome = apply_to_job(job=job, answers=answers, runs_root=tmp_path / "runs",
                           profile_dir=tmp_path / "prof", headless=True, dry_run=True,
                           ats_override="greenhouse")
    assert outcome.status == "needs_sam"
    assert "outside the us" in outcome.halt_reason.lower() or "australia" in outcome.halt_reason.lower()
    # a structured answerable work_auth blocker was stamped (Phase-1 classify_halt path)
    blk = getattr(outcome, "human_blocker", None)
    assert isinstance(blk, dict)
    assert blk.get("category") == "work_auth"
    assert blk.get("tier") == "answerable"


def test_apply_us_role_still_auto_answers_sponsorship_no(fixture_server, answers, tmp_path):
    """Regression: the common US path is unchanged by G4 — a US-located role still auto-answers the
    sponsorship screen No and does NOT halt on work-auth. Guards against the geography gate
    over-firing on domestic roles.

    Asserts the work-auth-specific behaviour (sponsor=No recorded, no work-auth halt), NOT a full
    ready_to_submit — the shared greenhouse fixture has a known native-select limitation that lands
    the run on needs_input downstream (the allowed baseline trio), independent of G4."""
    job = {"id": "JOB-US", "company": "Acme", "title": "FEA Engineer",
           "location": "Carlsbad, CA",
           "url": f"{fixture_server}/greenhouse_form.html"}
    outcome = apply_to_job(job=job, answers=answers, runs_root=tmp_path / "runs",
                           profile_dir=tmp_path / "prof", headless=True, dry_run=True,
                           ats_override="greenhouse")
    # the US role auto-answered sponsorship No (the geography gate did NOT fire)
    assert any(s["field"] == "sponsor" and s["answer"] == "No" for s in outcome.work_auth_answers)
    # and it did NOT halt for a work-auth reason
    assert "work-auth" not in (outcome.halt_reason or "").lower()
    assert "outside the us" not in (outcome.halt_reason or "").lower()
