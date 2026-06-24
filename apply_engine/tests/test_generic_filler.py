import pytest
from apply_engine.browser import launch_profile
from apply_engine.adapters.generic import GenericFiller
from apply_engine.source_data import Answers


@pytest.fixture
def answers(tmp_path):
    r = tmp_path / "r.pdf"; r.write_bytes(b"%PDF")
    return Answers(values={"first_name": "Sam", "last_name": "Rivera",
                           "email": "sam.rivera@example.com", "phone": "555-555-0100"},
                   resume_pdf=r, cover_pdf=None)


def test_generic_fills_mapped_and_reports_unmapped(fixture_server, answers, tmp_path):
    g = GenericFiller()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/generic_form.html")
        intended = g.fill(page, answers)
        assert intended["first_name"] == "Sam"
        assert intended["email"] == "sam.rivera@example.com"
        observed = g.read_back(page, list(intended.keys()))
        assert observed["last_name"] == "Rivera"
        # the "Years of experience" field could not be mapped -> recorded
        assert any("experience" in u.lower() for u in g.unmapped)
