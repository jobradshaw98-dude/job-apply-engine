"""Validate the Yes/No <button> work-auth widget (live Ashby uses this, not a select).
Regression guard for the Ashby work-auth gap found 2026-05-31."""
from apply_engine.browser import launch_profile
from apply_engine.adapters.ashby import AshbyAdapter
from apply_engine.work_auth import classify_work_auth, WorkAuthDecision


def test_detects_button_yesno_work_auth(fixture_server, tmp_path):
    a = AshbyAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/ashby_yesno_form.html")
        qs = a.find_work_auth_questions(page)
        assert len(qs) == 1
        assert qs[0].kind == "button-yesno"
        assert classify_work_auth(qs[0].label) == WorkAuthDecision.SPONSORSHIP_NO


def test_answers_button_yesno_no(fixture_server, tmp_path):
    a = AshbyAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/ashby_yesno_form.html")
        q = a.find_work_auth_questions(page)[0]
        a.answer_no(page, q)   # sponsorship -> No
        selected = page.query_selector("button[data-selected='1']")
        assert selected is not None
        assert selected.inner_text().strip() == "No"
