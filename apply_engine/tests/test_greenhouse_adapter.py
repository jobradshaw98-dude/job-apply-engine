import pytest
from apply_engine.browser import launch_profile
from apply_engine.adapters.greenhouse import GreenhouseAdapter
from apply_engine.source_data import Answers
from apply_engine.work_auth import classify_work_auth, WorkAuthDecision


@pytest.fixture
def answers(tmp_path):
    resume = tmp_path / "resume.pdf"; resume.write_bytes(b"%PDF-1.4 test")
    return Answers(values={
        "first_name": "Sam", "last_name": "Rivera",
        "email": "sam.rivera@example.com", "phone": "555-555-0100",
    }, resume_pdf=resume, cover_pdf=None)


def test_greenhouse_fill_and_readback(fixture_server, answers, tmp_path):
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "prof") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_form.html")
        intended = a.fill(page, answers)
        observed = a.read_back(page, list(intended.keys()))
        assert observed["first_name"] == "Sam"
        assert observed["email"] == "sam.rivera@example.com"
        assert observed["last_name"] == "Rivera"


def test_greenhouse_finds_sponsorship_question(fixture_server, answers, tmp_path):
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "prof") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_form.html")
        qs = a.find_work_auth_questions(page)
        assert len(qs) == 1
        assert classify_work_auth(qs[0].label) == WorkAuthDecision.SPONSORSHIP_NO
