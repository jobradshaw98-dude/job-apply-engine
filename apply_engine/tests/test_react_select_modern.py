"""Modern Greenhouse (job-boards.greenhouse.io) renders EVERY dropdown — Country, State,
work-auth, and screening questions — as React-Select (.select__control), with ZERO native
<select>. This is the regression guard for the gap the first live Oura submit hit on
2026-06-07: screening Yes/No react-selects were never captured, Country/State were never
driven (el.fill is a no-op on react-select), and a blank required react-select slipped
through as a false ready_to_submit.

Runs against the faithful react-select double in fixtures/greenhouse_modern_form.html."""
from apply_engine.browser import launch_profile
from apply_engine.adapters.greenhouse import GreenhouseAdapter
from apply_engine.work_auth import classify_work_auth, WorkAuthDecision
from apply_engine.questions import extract_react_select_questions
from apply_engine.completeness import react_select_unfilled


def test_extract_only_custom_react_selects(fixture_server, tmp_path):
    """Captures the custom FEA screening question; EXCLUDES Country/State (standard-mapped)
    and both work-auth questions (the guard owns those)."""
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_modern_form.html")
        qs = extract_react_select_questions(page)
        labels = [q.label.lower() for q in qs]
        assert len(qs) == 1, labels
        assert "fea models" in qs[0].label.lower()
        assert qs[0].options == ["Yes", "No"]
        assert not any("country" in l or "state" in l for l in labels)
        assert not any("authorized" in l or "sponsorship" in l for l in labels)


def test_work_auth_still_detected_on_modern_form(fixture_server, tmp_path):
    """The work-auth react-selects remain visible to the guard (not swallowed by the new
    custom extractor) and classify correctly."""
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_modern_form.html")
        qs = a.find_work_auth_questions(page)
        assert len(qs) == 2
        decisions = {classify_work_auth(q.label) for q in qs}
        assert WorkAuthDecision.AUTHORIZED_YES in decisions
        assert WorkAuthDecision.SPONSORSHIP_NO in decisions


def test_select_react_by_label_drives_custom_question(fixture_server, tmp_path):
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_modern_form.html")
        assert a.select_react_by_label(page, "FEA models", "Yes") is True
        ctrl = page.query_selector('[data-rsname="fea"]')
        assert ctrl.get_attribute("data-value") == "Yes"


def test_country_then_state_cascade(fixture_server, tmp_path):
    """Country must be set before State (State shows 'No options' until Country=United
    States). The standard react-select driver handles the order + the full state name."""
    a = GreenhouseAdapter()
    answers = {"country": "United States", "state_full": "California", "state": "CA"}
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_modern_form.html")
        added = a._fill_standard_react_selects(page, answers)
        assert added.get("country") == "United States"
        assert added.get("state") == "California"
        assert page.query_selector('[data-rsname="country"]').get_attribute("data-value") == "United States"
        assert page.query_selector('[data-rsname="state"]').get_attribute("data-value") == "California"


def test_state_blocked_without_country(fixture_server, tmp_path):
    """Picking State directly with no Country yields nothing (cascade not populated) — the
    driver does not falsely report it set."""
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_modern_form.html")
        assert a.select_react_by_label(page, "state", "California") is False
        assert page.query_selector('[data-rsname="state"]').get_attribute("data-value") is None


def test_label_driver_refuses_work_auth_controls(fixture_server, tmp_path):
    """select_react_by_label must NEVER drive a work-auth/EEO control by substring — a
    'country' or 'state' location match could otherwise corrupt a work-auth answer
    ('authorized to work in this country'). Work-auth is owned by the guard only."""
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_modern_form.html")
        # try to drive the sponsorship work-auth control via its own label — must refuse
        assert a.select_react_by_label(page, "sponsorship", "Yes") is False
        assert page.query_selector('[data-rsname="sponsor"]').get_attribute("data-value") is None


def test_state_abbreviation_fallback_on_code_keyed_select(fixture_server, tmp_path):
    """SILENT-OURA-BLOCKER REGRESSION: when the State react-select lists 2-LETTER CODES
    ('CA','TX','NY') instead of full names, filling 'California' yields 'No options' and leaves
    State blank → required-field validation blocks submit. The [state_full, state] candidate
    fallback must recover by then trying 'CA'. Asserts State ends up set to the abbreviation."""
    a = GreenhouseAdapter()
    answers = {"country": "United States", "state_full": "California", "state": "CA"}
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_modern_form_codes.html")
        added = a._fill_standard_react_selects(page, answers)
        assert added.get("country") == "United States"
        # 'California' (full) missed → fell through to 'CA' (abbreviation), which registered.
        assert added.get("state") == "CA", added
        assert page.query_selector('[data-rsname="state"]').get_attribute("data-value") == "CA"


def test_full_name_into_code_select_reports_no_match(fixture_server, tmp_path):
    """The full name alone must NOT register on a code-keyed select (it's the 'No options'
    case). select_react_by_label('state','California') returns False and leaves State blank —
    this is the miss that the candidate-list fallback above turns into a recovery."""
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_modern_form_codes.html")
        # populate the cascade first so State has options at all (the codes)
        assert a.select_react_by_label(page, "country", "United States") is True
        assert a.select_react_by_label(page, "state", "California") is False
        assert page.query_selector('[data-rsname="state"]').get_attribute("data-value") is None
        # the abbreviation does register
        assert a.select_react_by_label(page, "state", "CA") is True
        assert page.query_selector('[data-rsname="state"]').get_attribute("data-value") == "CA"


def test_country_code_fallback(fixture_server, tmp_path):
    """Country defensive fallback: even if the profile passed an odd country value, the driver
    tries 'United States'/'US'/'USA'. Here the full name registers on the first try; the test
    pins that the candidate-list path still resolves Country (and cascades State)."""
    a = GreenhouseAdapter()
    answers = {"country": "USA", "state_full": "California", "state": "CA"}
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_modern_form_codes.html")
        added = a._fill_standard_react_selects(page, answers)
        # 'USA' misses (options are 'United States|Canada|Mexico') → falls through to
        # 'United States', which registers and cascades the State codes.
        assert added.get("country") == "United States", added
        assert added.get("state") == "CA", added


def test_react_select_unfilled_flags_then_clears(fixture_server, tmp_path):
    """Before filling, every required react-select is flagged; after answering all of them,
    react_select_unfilled is empty (no false ready_to_submit, no false block)."""
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_modern_form.html")
        before = react_select_unfilled(page)
        assert len(before) == 5  # country, state, auth, sponsor, fea
        # standard location via the cascade driver
        a._fill_standard_react_selects(page, {"country": "United States",
                                              "state_full": "California"})
        # work-auth via the guard path (select_react_by_label correctly REFUSES work-auth
        # controls now — that protection is the BLOCKER-2 fix)
        for q in a.find_work_auth_questions(page):
            d = classify_work_auth(q.label)
            if d == WorkAuthDecision.SPONSORSHIP_NO:
                assert a.answer_no(page, q) is True
            elif d in (WorkAuthDecision.AUTHORIZED_YES,
                       WorkAuthDecision.AUTHORIZED_NO_SPONSORSHIP):
                assert a.answer_yes(page, q) is True
        # custom screening question via label
        assert a.select_react_by_label(page, "FEA models", "Yes") is True
        after = react_select_unfilled(page)
        assert after == [], after
