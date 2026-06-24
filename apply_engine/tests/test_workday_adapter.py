import pytest
from apply_engine.browser import launch_profile
from apply_engine.adapters.workday import (
    WorkdayAdapter, _match_tenant_creds, _generate_password, _merge_creds)


def test_generate_password_meets_workday_rules():
    for _ in range(20):
        p = _generate_password()
        assert len(p) >= 12
        assert any(c.isupper() for c in p)
        assert any(c.islower() for c in p)
        assert any(c.isdigit() for c in p)
        assert any(not c.isalnum() for c in p)   # special char


def test_merge_creds_adds_tenant_without_clobbering():
    existing = {"illumina.wd1.myworkdayjobs.com": {"email": "a@b.com", "password": "x"}}
    out = _merge_creds(existing, "orthofix.wd1.myworkdayjobs.com", "a@b.com", "newpw")
    assert out["orthofix.wd1.myworkdayjobs.com"] == {"email": "a@b.com", "password": "newpw"}
    assert out["illumina.wd1.myworkdayjobs.com"]["password"] == "x"   # untouched
    assert _merge_creds(existing, "", "a@b.com", "p") == existing      # no host -> no change
from apply_engine.source_data import Answers
from apply_engine.work_auth import classify_work_auth, WorkAuthDecision


def test_match_tenant_creds_by_host():
    data = {"illumina.wd1.myworkdayjobs.com": {"email": "a@b.com", "password": "x"}}
    assert _match_tenant_creds(data, "illumina.wd1.myworkdayjobs.com")["email"] == "a@b.com"
    # apply flow can run on a subdomain variant -> substring match both ways
    assert _match_tenant_creds(data, "illumina.wd1.myworkdayjobs.com/en-US") is not None
    assert _match_tenant_creds(data, "acme.wd5.myworkdayjobs.com") is None
    assert _match_tenant_creds({}, "illumina.wd1.myworkdayjobs.com") is None
    assert _match_tenant_creds(data, "") is None


@pytest.fixture
def answers(tmp_path):
    r = tmp_path / "r.pdf"; r.write_bytes(b"%PDF")
    return Answers(values={"first_name": "Sam", "last_name": "Rivera",
                           "email": "sam.rivera@example.com", "phone": "555-555-0100"},
                   resume_pdf=r, cover_pdf=None)


def test_workday_fill_readback_and_sponsor(fixture_server, answers, tmp_path):
    a = WorkdayAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/workday_form.html")
        intended = a.fill(page, answers)
        observed = a.read_back(page, list(intended.keys()))
        assert observed["first_name"] == "Sam"
        assert observed["last_name"] == "Rivera"
        qs = a.find_work_auth_questions(page)
        assert len(qs) == 1
        assert classify_work_auth(qs[0].label) == WorkAuthDecision.SPONSORSHIP_NO
