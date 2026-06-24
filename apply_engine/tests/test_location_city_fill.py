"""Location / City auto-fill regression guard.

Modern Greenhouse renders "Location (City)" as an ASYNC autocomplete react-select (options
fetched from a geo service after you type), so el.fill is a no-op and fill_remaining's react-
select skip left it blank → completeness flagged it required-unfilled → needs_input. The fix
drives it from answers["city"] via the standard react-select path, with an async-tolerant
option-wait and a [city, "city, state", "city, state_full"] candidate fallback. The plain-text
shape (a normal <input> mapped to "city") is already handled by fill_remaining's text loop —
guarded here so it can't regress.

SAFETY: a work-auth react-select whose label contains "country" must NEVER be hijacked by the
location/city driver (classify_work_auth owns it).

Runs against fixtures/greenhouse_location_async_form.html (react-select, async options) and
fixtures/greenhouse_location_text_form.html (plain text input)."""
from apply_engine.browser import launch_profile
from apply_engine.adapters.greenhouse import GreenhouseAdapter
from apply_engine.completeness import react_select_unfilled, unfilled_required

# Mirrors the real profile values the engine fills from.
ANSWERS = {"city": "Austin", "state": "CA", "state_full": "California",
           "country": "United States"}


def test_async_location_react_select_filled_and_verified(fixture_server, tmp_path):
    """The async Location (City) react-select gets driven to Austin and READS BACK verified
    (a chosen .select__single-value chip), proving it registered — not a void action."""
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_location_async_form.html")
        added = a._fill_standard_react_selects(page, ANSWERS)
        assert added.get("city") == "Austin", added
        # Verified read-back: the location control shows a chosen value chip that starts with
        # the city (the bare "Austin" typeahead matches "Austin, TX, USA").
        chip = page.query_selector('[data-rsname="location"]').get_attribute("data-value")
        assert chip and chip.lower().startswith("austin"), chip


def test_async_location_clears_required_flag(fixture_server, tmp_path):
    """Before filling, the location react-select is flagged required-unfilled; after the driver
    runs it is no longer flagged — so it stops forcing needs_input."""
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_location_async_form.html")
        before = [l.lower() for l in react_select_unfilled(page)]
        assert any("location" in l or "city" in l for l in before), before
        a._fill_standard_react_selects(page, ANSWERS)
        after = [l.lower() for l in react_select_unfilled(page)]
        assert not any("location" in l or "city" in l for l in after), after


def test_location_driver_does_not_hijack_work_auth_country(fixture_server, tmp_path):
    """The work-auth react-select labelled '...authorized to work in this country?' must be
    untouched by the location/city driver — classify_work_auth excludes it. After the driver
    runs, the auth control still has NO value (it is owned by the work-auth guard)."""
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_location_async_form.html")
        a._fill_standard_react_selects(page, ANSWERS)
        auth_val = page.query_selector('[data-rsname="auth"]').get_attribute("data-value")
        assert auth_val is None, auth_val
        # And driving it directly by a 'country' label substring must still be refused.
        assert a.select_react_by_label(page, "country", "Yes") is False
        assert page.query_selector('[data-rsname="auth"]').get_attribute("data-value") is None


def test_plain_text_location_input_filled(fixture_server, tmp_path):
    """The plain-text 'Location (City)' input maps to 'city' and gets filled to Austin by
    fill_remaining's text loop (the simple, non-react shape)."""
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_location_text_form.html")
        added = a.fill_remaining(page, ANSWERS)
        assert added.get("city") == "Austin", added
        assert page.query_selector("#loc_city").input_value() == "Austin"
        # and it no longer reads as a required-unfilled field
        assert not any("city" in m.lower() or "location" in m.lower()
                       for m in unfilled_required(page))


def test_location_no_options_for_any_candidate_escalates(fixture_server, tmp_path):
    """If the geo service returns nothing for every candidate (no real match), the driver must
    NOT force a wrong value — it leaves the field blank so completeness escalates it to the user.
    Simulated by asking for a city that isn't in the fixture's GEO list."""
    a = GreenhouseAdapter()
    answers = dict(ANSWERS, city="Zzhbphakeville")
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_location_async_form.html")
        added = a._fill_standard_react_selects(page, answers)
        assert "city" not in added, added
        assert page.query_selector('[data-rsname="location"]').get_attribute("data-value") is None
        # still flagged required-unfilled -> needs_input (never a forced wrong value)
        after = [l.lower() for l in react_select_unfilled(page)]
        assert any("location" in l or "city" in l for l in after), after
