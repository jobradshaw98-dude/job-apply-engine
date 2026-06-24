"""Validate work-auth handling against a React-Select form (mirrors live Greenhouse,
which uses React-Select widgets, NOT native <select>). This is the regression guard
for the gap the live Oura demo surfaced on 2026-05-31."""
from apply_engine.browser import launch_profile
from apply_engine.adapters.greenhouse import GreenhouseAdapter
from apply_engine.work_auth import classify_work_auth, WorkAuthDecision


def test_detects_both_react_select_work_auth_questions(fixture_server, tmp_path):
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_react_form.html")
        qs = a.find_work_auth_questions(page)
        labels = " || ".join(q.label.lower() for q in qs)
        assert len(qs) == 2
        assert all(q.kind == "react-select" for q in qs)
        assert "authorized to work" in labels
        assert "sponsorship" in labels


def test_answers_react_select_yes_and_no(fixture_server, tmp_path):
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_react_form.html")
        for q in a.find_work_auth_questions(page):
            d = classify_work_auth(q.label)
            if d == WorkAuthDecision.AUTHORIZED_YES:
                a.answer_yes(page, q)
            elif d == WorkAuthDecision.SPONSORSHIP_NO:
                a.answer_no(page, q)
        # read the React-Select controls back: authorized=Yes, sponsor=No
        auth = page.query_selector("[data-rs='auth']").get_attribute("data-value")
        spon = page.query_selector("[data-rs='sponsor']").get_attribute("data-value")
        assert auth == "Yes"
        assert spon == "No"
