"""Regression guards for the live-demo findings (2026-05-31): a posting page with no
form must NOT report ready_to_submit (empty-fill guard), and an adapter must navigate
through an 'Apply' control to reach the real form (go_to_form)."""
import pytest
from apply_engine.browser import launch_profile
from apply_engine.adapters.lever import LeverAdapter
from apply_engine.orchestrator import apply_to_job
from apply_engine.source_data import Answers


@pytest.fixture
def answers(tmp_path):
    r = tmp_path / "r.pdf"; r.write_bytes(b"%PDF")
    return Answers(values={"full_name": "Sam Rivera", "first_name": "Sam",
                           "last_name": "Rivera",
                           "email": "sam.rivera@example.com", "phone": "555-555-0100"},
                   resume_pdf=r, cover_pdf=None)


def test_empty_posting_is_not_ready_to_submit(fixture_server, answers, tmp_path):
    job = {"id": "JOB-EMPTY", "company": "Acme", "title": "Eng",
           "url": f"{fixture_server}/empty_form.html"}
    out = apply_to_job(job=job, answers=answers, runs_root=tmp_path / "runs",
                       profile_dir=tmp_path / "p", headless=True, dry_run=True)
    assert out.status == "needs_sam"
    # zero-fields halt now carries a PRECISE outcome label (closed / homepage_no_link /
    # unsupported_ats / form_not_found) instead of the old vague "no fillable fields" string.
    assert out.outcome in ("homepage_no_link", "unsupported_ats", "closed", "form_not_found")
    assert out.halt_reason  # a human reason is always set


def test_go_to_form_clicks_apply_then_fills(fixture_server, answers, tmp_path):
    a = LeverAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/lever_landing.html")
        # form not present on landing page
        assert page.query_selector("#name") is None
        a.go_to_form(page)
        # after clicking "Apply for this job" we land on the form
        intended = a.fill(page, answers)
        assert intended["full_name"] == "Sam Rivera"


def test_newsletter_page_is_not_an_application(fixture_server, answers, tmp_path):
    """A careers page whose only field is a newsletter email box must NOT pass as a staged
    application (the CommerceIQ/Getinge bug: filled `email` only -> ready_to_submit). It must
    halt as needs_sam with outcome 'not_an_application'."""
    job = {"id": "JOB-NL", "company": "Acme", "title": "Eng",
           "url": f"{fixture_server}/newsletter_page.html"}
    out = apply_to_job(job=job, answers=answers, runs_root=tmp_path / "runs",
                       profile_dir=tmp_path / "p", headless=True, dry_run=True)
    assert out.status == "needs_sam"
    assert out.outcome == "not_an_application"
    assert out.status != "ready_to_submit"
    # and it must NOT claim a resume was attached (no resume field on the page)
    assert not any(d.get("doc") == "resume" for d in (out.uploaded_docs or []))
