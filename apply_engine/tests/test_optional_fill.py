# -*- coding: utf-8 -*-
"""G5 — answer-every-field (optionals + EEO).

Runs `fill_optional_and_eeo` against the faithful double in fixtures/optional_eeo_form.html
(native-select EEO + optional free-text + a blank-website field + a required field), proving:
  * EEO is filled with Sam's disclosed values (gender=Male/race=White/hispanic=No/
    veteran=not-protected) + disability=decline.
  * Optional free-text gets the canned profile answers (start date / pronunciation / deadlines).
  * Website stays BLANK (no url in the profile -> never fabricated).
  * The REQUIRED non-EEO field is NOT touched by the optional pass.
  * A non-drivable widget is SKIPPED, not fabricated.
  * An exception inside the pass leaves the stage intact (never raises).

Deterministic + offline: drives a local Playwright page against the fixture; no network, no LLM.
"""
from apply_engine.browser import launch_profile
from apply_engine.adapters.greenhouse import GreenhouseAdapter
from apply_engine.optional_fill import fill_optional_and_eeo
from apply_engine.optional_fill import classify_eeo
from apply_engine.optional_fill import classify_optional_text
from apply_engine.optional_fill import _pick_eeo_option


# Sam's confirmed real EEO values + the optional-answer patterns (a self-contained profile so
# the test never depends on the live applicant_profile.json).
PROFILE = {
    "website": "",
    "phone_country": "United States",
    "self_id": {
        "gender": "Male",
        "race": "White",
        "hispanic": "No",
        "veteran": "I am not a protected veteran",
        "disability": "I do not want to answer",
    },
    "optional_answers": {
        "name_pronunciation": "Sam Rivera — JOR-din BRAD-shaw",
        "earliest_start": "Flexible — about two to three weeks from an offer.",
        "deadlines": "None at this time.",
        "additional_info": "Hands-on engineering depth + an ARIA demo offer.",
    },
}


def _spec_for(page):
    return GreenhouseAdapter().enumerate_fields(page)


def test_fills_eeo_disclosed_values_and_declines_disability(fixture_server, tmp_path):
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/optional_eeo_form.html")
        report = fill_optional_and_eeo(page, a.enumerate_fields(page), PROFILE, adapter=a)

        assert page.query_selector('[id="eeo_gender"]').input_value() == "Male"
        assert page.query_selector('[id="eeo_race"]').input_value() == "White"
        assert page.query_selector('[id="eeo_hispanic"]').input_value() == "No"
        assert page.query_selector('[id="eeo_veteran"]').input_value() == \
            "I am not a protected veteran"
        # disability = decline (substring-matched to the live "I do not want to answer" option)
        assert page.query_selector('[id="eeo_disability"]').input_value() == \
            "I do not want to answer"
        for lab in ("Gender", "Race / Ethnicity"):
            assert lab in report["filled"]


def test_fills_optional_free_text(fixture_server, tmp_path):
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/optional_eeo_form.html")
        fill_optional_and_eeo(page, a.enumerate_fields(page), PROFILE, adapter=a)

        assert "JOR-din" in page.query_selector('[id="pronounce"]').input_value()
        assert "two to three weeks" in page.query_selector('[id="start_date"]').input_value()
        assert page.query_selector('[id="deadlines"]').input_value() == "None at this time."


def test_website_stays_blank_when_no_url(fixture_server, tmp_path):
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/optional_eeo_form.html")
        report = fill_optional_and_eeo(page, a.enumerate_fields(page), PROFILE, adapter=a)
        assert page.query_selector('[id="website"]').input_value() == ""
        assert "Personal website" not in report["filled"]


def test_required_field_not_touched_by_optional_pass(fixture_server, tmp_path):
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/optional_eeo_form.html")
        # simulate the required-fill path having set first name
        page.fill('[id="first_name"]', "Sam")
        fill_optional_and_eeo(page, a.enumerate_fields(page), PROFILE, adapter=a)
        # untouched: still exactly what the required path set, never overwritten/blanked.
        assert page.query_selector('[id="first_name"]').input_value() == "Sam"


def test_already_filled_optional_is_not_overwritten(fixture_server, tmp_path):
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/optional_eeo_form.html")
        page.fill('[id="start_date"]', "Immediately")
        fill_optional_and_eeo(page, a.enumerate_fields(page), PROFILE, adapter=a)
        assert page.query_selector('[id="start_date"]').input_value() == "Immediately"


def test_additional_info_skipped_when_cover_field_present(fixture_server, tmp_path):
    """When the form has a cover-letter field, additional-info is left blank (a tailored cover
    already covers it — don't paste a redundant note)."""
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/optional_eeo_form.html")
        spec = a.enumerate_fields(page)
        spec.has_cover_field = True   # simulate a cover-letter upload field on the form
        fill_optional_and_eeo(page, spec, PROFILE, adapter=a)
        assert page.query_selector('[id="additional"]').input_value() == ""


def test_non_drivable_widget_skipped_not_fabricated(fixture_server, tmp_path):
    """A field whose desired value has NO matching live option is recorded as SKIPPED, never
    reported as filled and never coerced onto a wrong option — the live-dom rule: never fabricate
    a fill. Here the profile's gender value isn't among the form's gender options."""
    a = GreenhouseAdapter()
    profile = {**PROFILE, "self_id": {**PROFILE["self_id"], "gender": "Genderqueer"}}
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/optional_eeo_form.html")
        spec = a.enumerate_fields(page)
        report = fill_optional_and_eeo(page, spec, profile, adapter=a)
        # the value couldn't be matched to any live option -> SKIPPED, never fabricated.
        assert "Gender" in report["skipped"]
        assert "Gender" not in report["filled"]
        # the real gender select stays blank (we never coerced a wrong option onto it).
        assert page.query_selector('[id="eeo_gender"]').input_value() == ""


def test_exception_in_pass_leaves_stage_intact(fixture_server, tmp_path):
    """An exception raised mid-pass (here: an enumerate that hands back a bad spec) must NEVER
    propagate — the caller's stage proceeds. fill_optional_and_eeo swallows per-field errors and
    a top-level bad input returns an empty report rather than raising."""
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/optional_eeo_form.html")

        class _Boom:
            """A spec whose .fields raises when iterated — simulates a malformed form model."""
            @property
            def fields(self):
                raise RuntimeError("boom")
            has_cover_field = False

        # must not raise
        report = fill_optional_and_eeo(page, _Boom(), PROFILE, adapter=a)
        assert report == {"filled": {}, "skipped": []}


def test_none_spec_is_safe_noop():
    assert fill_optional_and_eeo(None, None, PROFILE, adapter=None) == \
        {"filled": {}, "skipped": []}


# ---- pure-function classifier unit tests (no browser) ----
def test_classify_eeo_categories():
    assert classify_eeo("Gender") == "gender"
    assert classify_eeo("Race / Ethnicity") == "race"
    assert classify_eeo("Are you Hispanic or Latino?") == "hispanic"
    assert classify_eeo("Veteran status") == "veteran"
    assert classify_eeo("Disability status") == "disability"
    assert classify_eeo("First name") is None
    # JOB-281 Together AI: sexual orientation + gender identity are voluntary self-ID too —
    # must classify (so the answer path defers them) rather than returning None and erroring.
    assert classify_eeo("How would you describe your sexual orientation?") == "orientation"
    assert classify_eeo("Do you identify as transgender?") == "gender_identity"


def test_classify_optional_text_categories():
    assert classify_optional_text("How do you pronounce your name?") == "name_pronunciation"
    assert classify_optional_text("What is your earliest start date?") == "earliest_start"
    assert classify_optional_text("Any competing offers or deadlines?") == "deadlines"
    assert classify_optional_text("Is there anything else you'd like us to know?") == \
        "additional_info"
    assert classify_optional_text("Favorite color") is None


def test_pick_eeo_option_matches_long_phrasings():
    # 'White' should match a long option phrasing
    opts = ["White (Not Hispanic or Latino)", "Asian", "Decline to self-identify"]
    assert _pick_eeo_option(opts, "White") == "White (Not Hispanic or Latino)"
    # a decline desired value falls back to any decline option
    assert _pick_eeo_option(opts, "I do not want to answer") == "Decline to self-identify"
    # no match -> None (never guess)
    assert _pick_eeo_option(["Asian", "Black or African American"], "White") is None
