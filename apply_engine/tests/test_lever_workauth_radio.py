"""Lever (Palantir) work-auth is a RADIO Yes/No pair keyed by cards[<uuid>][fieldN], a 4th
widget kind the work-auth guard previously missed entirely (live 2026-06-02: both work-auth
questions fell into needs_input). Covers detection + classification + answering, and the
radio-group-aware completeness scan (a checked group is satisfied; an unanswered required
group is reported once, by its question label, not the raw field name)."""
from apply_engine.browser import launch_profile
from apply_engine.adapters.lever import LeverAdapter
from apply_engine.work_auth import classify_work_auth, WorkAuthDecision
from apply_engine.completeness import unfilled_required

FORM = "lever_workauth_radio_form.html"


def test_detects_radio_yesno_workauth_questions(fixture_server, tmp_path):
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/{FORM}")
        qs = LeverAdapter().find_work_auth_questions(page)
        labels = [q.label.lower() for q in qs]
        assert any("authorized to work" in l for l in labels), labels
        assert any("require sponsorship" in l for l in labels), labels
        # the non-work-auth security-clearance radio group is NOT picked up as work-auth
        assert not any("security clearance" in l for l in labels), labels
        for q in qs:
            assert q.kind == "radio-yesno"
            assert q.selector.startswith("cards[")  # the radio group's name=


def test_classification_picks_opposite_answers(fixture_server, tmp_path):
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/{FORM}")
        qs = LeverAdapter().find_work_auth_questions(page)
        auth = next(q for q in qs if "authorized to work" in q.label.lower())
        spon = next(q for q in qs if "require sponsorship" in q.label.lower())
        assert classify_work_auth(auth.label) == WorkAuthDecision.AUTHORIZED_YES
        assert classify_work_auth(spon.label) == WorkAuthDecision.SPONSORSHIP_NO


def test_answer_yes_and_no_check_the_right_radio(fixture_server, tmp_path):
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/{FORM}")
        adapter = LeverAdapter()
        qs = adapter.find_work_auth_questions(page)
        auth = next(q for q in qs if "authorized to work" in q.label.lower())
        spon = next(q for q in qs if "require sponsorship" in q.label.lower())

        adapter.answer_yes(page, auth)
        adapter.answer_no(page, spon)

        def checked_value(name):
            for r in page.query_selector_all(f'input[type="radio"][name="{name}"]'):
                if r.is_checked():
                    return r.get_attribute("value")
            return None

        assert checked_value(auth.selector) == "Yes"
        assert checked_value(spon.selector) == "No"


def test_completeness_radio_group_satisfied_when_answered(fixture_server, tmp_path):
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/{FORM}")
        adapter = LeverAdapter()
        # before answering: all three required radio groups are missing
        before = unfilled_required(page)
        assert len(before) == 3, before
        # answer the two work-auth groups
        for q in adapter.find_work_auth_questions(page):
            d = classify_work_auth(q.label)
            if d == WorkAuthDecision.SPONSORSHIP_NO:
                adapter.answer_no(page, q)
            elif d in (WorkAuthDecision.AUTHORIZED_YES,
                       WorkAuthDecision.AUTHORIZED_NO_SPONSORSHIP):
                adapter.answer_yes(page, q)
        after = unfilled_required(page)
        # the two answered groups drop out; only the untouched security-clearance group remains
        assert len(after) == 1, after
        assert "security clearance" in after[0].lower(), after
        # reported by its QUESTION label, never the raw cards[...] field name
        assert "cards[" not in after[0]
