import pytest
from apply_engine.browser import launch_profile
from apply_engine.adapters.ashby import AshbyAdapter
from apply_engine.source_data import Answers
from apply_engine.work_auth import classify_work_auth, WorkAuthDecision


@pytest.fixture
def answers(tmp_path):
    r = tmp_path / "r.pdf"; r.write_bytes(b"%PDF")
    return Answers(values={"full_name": "Sam Rivera",
                           "email": "sam.rivera@example.com", "phone": "555-555-0100"},
                   resume_pdf=r, cover_pdf=None)


def test_ashby_fill_readback_and_auth(fixture_server, answers, tmp_path):
    a = AshbyAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/ashby_form.html")
        intended = a.fill(page, answers)
        observed = a.read_back(page, list(intended.keys()))
        assert observed["full_name"] == "Sam Rivera"
        assert observed["email"] == "sam.rivera@example.com"
        assert observed["phone"] == "555-555-0100"   # matched via #phone (fallback list)
        qs = a.find_work_auth_questions(page)
        assert len(qs) == 1
        assert classify_work_auth(qs[0].label) == WorkAuthDecision.AUTHORIZED_YES
