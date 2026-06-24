"""Extraction of CUSTOM <select> dropdowns and "check all that apply" checkbox-groups
on single-page ATSs. Browser-backed (Playwright on the fixture server) so the DOM-walk
classification (required + UNRELATED work-auth + not standard-mapped) is exercised for real.

Default-off behavior is preserved: these extractors are only CALLED from the orchestrator
when answer_fn is passed; here we call them directly to verify the classification logic."""
from apply_engine.browser import launch_profile
from apply_engine.questions import (
    extract_select_questions, extract_checkbox_groups, extract_questions,
    SelectQuestion, CheckboxGroup,
)


def test_extract_questions_handles_idless_lever_essay(fixture_server, tmp_path):
    # id-less Lever "cards" textarea: selector falls back to name=, label recovered from
    # the .application-label (label_for would otherwise return name/"Type your response").
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/lever_custom_form.html")
        qs = extract_questions(page)
        match = [q for q in qs if "why do you want" in q.label.lower()]
        assert match, f"id-less essay not extracted; got {[q.label for q in qs]}"
        q = match[0]
        assert q.kind == "essay"
        assert q.selector.startswith('[name="cards[deadbeef')


def _page(fixture_server, tmp_path, name="custom_widgets_form.html"):
    return fixture_server, tmp_path, name


def test_extract_select_finds_custom_required_dropdown(fixture_server, tmp_path):
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/custom_widgets_form.html")
        qs = extract_select_questions(page)
        labels = [q.label.lower() for q in qs]
        # the custom experience select is extracted...
        assert any("simulation experience" in l for l in labels)
        # ...with its option texts read (blank placeholder dropped)
        exp = next(q for q in qs if "simulation experience" in q.label.lower())
        assert isinstance(exp, SelectQuestion)
        assert "5+ years" in exp.options
        assert "" not in exp.options  # the empty "Select" placeholder is not an option


def test_extract_select_skips_standard_mapped_and_workauth(fixture_server, tmp_path):
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/custom_widgets_form.html")
        labels = [q.label.lower() for q in extract_select_questions(page)]
        # Country is standard-mapped -> skipped
        assert not any("country" in l for l in labels)
        # work-auth select -> skipped (work-auth guard owns it)
        assert not any("authorized to work" in l for l in labels)


def test_extract_select_skips_non_required(fixture_server, tmp_path):
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/custom_widgets_form.html")
        labels = [q.label.lower() for q in extract_select_questions(page)]
        assert not any("favorite color" in l for l in labels)


def test_extract_checkbox_group_is_one_group_with_all_options(fixture_server, tmp_path):
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/custom_widgets_form.html")
        groups = extract_checkbox_groups(page)
        assert len(groups) == 1
        g = groups[0]
        assert isinstance(g, CheckboxGroup)
        assert "language skill" in g.label.lower()
        assert g.options == ["English (ENG)", "French (FRA)",
                             "Mandarin (MAN)", "Spanish (SPA)"]
        # one selector per checkbox, in option order
        assert len(g.selectors) == 4


def test_extractors_find_nothing_on_a_plain_form(fixture_server, tmp_path):
    # required_form.html has only text/textarea/file -> no selects, no checkbox-groups.
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/required_form.html")
        assert extract_select_questions(page) == []
        assert extract_checkbox_groups(page) == []


# --- LIVE Lever DOM hardening (id-less name-keyed widgets + EEO skip) ---------------
# lever_custom_form.html mirrors the live Palantir/Lever structure captured 2026-06-01:
# custom "cards[<uuid>][field0]" selects/checkbox-groups have NO id, demographic selects
# are keyed by name="eeo[...]", and the checkbox-group has no <fieldset>.

def test_lever_idless_custom_select_extracted_by_name(fixture_server, tmp_path):
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/lever_custom_form.html")
        qs = extract_select_questions(page)
        labels = [q.label.lower() for q in qs]
        # the id-less "How did you hear" card select is extracted...
        assert any("how did you hear" in l for l in labels)
        q = next(q for q in qs if "how did you hear" in q.label.lower())
        # ...by a name-based selector (no id present)
        assert q.selector.startswith('select[name="cards[')
        assert q.selector.endswith('"]')
        # ...with real options, the "Select..." placeholder dropped
        assert q.options == ["LinkedIn", "Referral", "Company website"]
        # the university card select drops the "Click Here (...)" placeholder too
        uni = next(q for q in qs if "university" in q.label.lower())
        assert "State University" in uni.options
        assert all("Click Here" not in o for o in uni.options)


def test_lever_eeo_selects_are_skipped(fixture_server, tmp_path):
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/lever_custom_form.html")
        labels = [q.label.lower() for q in extract_select_questions(page)]
        # protected-class self-ID selects (eeo[veteran]/eeo[disability]/eeo[gender]) -> skipped
        assert not any("veteran" in l for l in labels)
        assert not any("disability" in l for l in labels)
        assert not any("gender" in l for l in labels)


def test_lever_workauth_select_still_skipped(fixture_server, tmp_path):
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/lever_custom_form.html")
        labels = [q.label.lower() for q in extract_select_questions(page)]
        assert not any("authorized to work" in l for l in labels)


def test_lever_namegrouped_checkbox_group_extracted(fixture_server, tmp_path):
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/lever_custom_form.html")
        groups = extract_checkbox_groups(page)
        langs = [g for g in groups if "language skill" in g.label.lower()]
        assert len(langs) == 1
        g = langs[0]
        assert isinstance(g, CheckboxGroup)
        # option text = the box's value (Lever's value IS the display text), in DOM order
        assert g.options == ["English (ENG)", "French (FRA)",
                             "Mandarin (MAN)", "Spanish (SPA)"]
        assert len(g.selectors) == 4
        # name-based selector with the value double-quoted (spaces/parens are valid inside)
        assert g.selectors[0].startswith('input[type="checkbox"][name="cards[')
        assert g.selectors[0].endswith('[value="English (ENG)"]')


def test_lever_workauth_checkbox_group_skipped(fixture_server, tmp_path):
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/lever_custom_form.html")
        labels = [g.label.lower() for g in extract_checkbox_groups(page)]
        # the "Which work authorizations do you hold?" group must be left for Sam
        assert not any("work authorization" in l for l in labels)
