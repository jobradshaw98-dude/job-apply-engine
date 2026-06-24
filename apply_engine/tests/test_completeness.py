"""Completeness guard: 'ready_to_submit' must never hide a blank required field.
Also resume-attachment detection. Implements the user's rule (2026-05-31): ensure all
fields are filled, or report exactly which required ones still need him."""
import pytest
from apply_engine.browser import launch_profile
from apply_engine.completeness import resume_attached, unfilled_required
from apply_engine.orchestrator import apply_to_job
from apply_engine.source_data import Answers


def test_drop_answered_filters_answered_workauth():
    from apply_engine.completeness import drop_answered
    missing = ["Country", "Are you legally authorized to work in the United States of America?"]
    answered = ["Are you legally authorized to work in the United States of America? *"]
    assert drop_answered(missing, answered) == ["Country"]


@pytest.fixture
def answers(tmp_path):
    r = tmp_path / "r.pdf"; r.write_bytes(b"%PDF")
    return Answers(values={"first_name": "Sam", "last_name": "Rivera",
                           "full_name": "Sam Rivera",
                           "email": "sam.rivera@example.com", "phone": "555-555-0100"},
                   resume_pdf=r, cover_pdf=None)


def test_resume_attached_detects_before_and_after(fixture_server, tmp_path):
    f = tmp_path / "cv.pdf"; f.write_bytes(b"%PDF")
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/required_form.html")
        assert resume_attached(page, "#r") is False
        page.set_input_files("#r", str(f))
        assert resume_attached(page, "#r") is True


def test_yesno_button_groups_flags_required_only(fixture_server, tmp_path):
    from apply_engine.completeness import yesno_button_groups
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/yesno_buttons_form.html")
        groups = yesno_button_groups(page)
        joined = " | ".join(g.lower() for g in groups)
        assert "willing to relocate" in joined          # required (*) -> flagged
        assert "authorized to work" in joined           # required (*) -> flagged (guard drops it later)
        assert "newsletter" not in joined               # optional (no *) -> NOT flagged


def test_orchestrator_needs_input_on_unanswered_yesno(fixture_server, answers, tmp_path):
    # name+email fill. Work-auth Y/N is answered by the work-auth guard; the "willing to relocate"
    # Y/N is auto-answered "Yes" by the office-commitment policy (feedback_office_commitment_answer,
    # 2026-06-09 — relocation/RTO is always Yes). The genuinely-unanswerable required screening Y/N
    # ("active U.S. security clearance?") is NOT work-auth, NOT a commitment, and ungrounded without
    # an LLM, so it must still force needs_input — a custom Y/N widget the engine cannot answer can
    # never slip through as a false ready_to_submit. The optional newsletter Y/N must never block.
    job = {"id": "JOB-YN", "company": "Acme", "title": "Eng",
           "url": f"{fixture_server}/yesno_buttons_form.html"}
    out = apply_to_job(job=job, answers=answers, runs_root=tmp_path / "runs",
                       profile_dir=tmp_path / "p", headless=True, dry_run=True,
                       ats_override="greenhouse")
    assert out.status == "needs_input"
    # the unanswerable screening Y/N blocks
    assert any("security clearance" in m.lower() for m in out.unfilled_required)
    # relocation is policy-answered "Yes", so it must NOT appear as unfilled
    assert not any("relocate" in m.lower() for m in out.unfilled_required)
    # the optional Y/N never blocks
    assert not any("newsletter" in m.lower() for m in out.unfilled_required)


def test_unfilled_required_lists_empty_required(fixture_server, tmp_path):
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/required_form.html")
        missing = unfilled_required(page)
        # all four are required and empty initially
        joined = " | ".join(m.lower() for m in missing)
        assert "first name" in joined
        assert "why do you want this role?" in joined


def test_orchestrator_needs_input_when_required_unfilled(fixture_server, answers, tmp_path):
    # generic filler maps First Name + Email, but the required essay + resume(req) remain
    job = {"id": "JOB-REQ", "company": "Acme", "title": "Eng",
           "url": f"{fixture_server}/required_form.html"}
    out = apply_to_job(job=job, answers=answers, runs_root=tmp_path / "runs",
                       profile_dir=tmp_path / "p", headless=True, dry_run=True)
    assert out.status == "needs_input"
    assert any("why do you want" in m.lower() for m in out.unfilled_required)
    # the mapped fields DID get filled
    assert "first_name" in out.filled_fields
    assert "email" in out.filled_fields
