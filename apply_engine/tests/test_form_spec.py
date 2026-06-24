# -*- coding: utf-8 -*-
"""Phase 0 — live-form MODEL: enumerate_fields + constraint scrape + reconcile_form.

These run against the faithful fixture forms served by the session `fixture_server` (real
Playwright DOM, not a string mock), exactly like test_react_select_modern.py. Phase 0 is
read-only: nothing here fills a form or writes a manifest.

Fixtures used:
  * phase0_form_spec_form.html  — purpose-built: a "200-400 words" essay, a maxlength=500 essay,
    an unconstrained essay, a SHORT "Current employer" field, a required "Current title" with no
    staged answer, a required <select>, AND a resume+cover upload pair (WITH-cover form, G7).
  * greenhouse_form.html        — resume but NO cover field (WITHOUT-cover form, G7).
  * ashby_modern_form.html      — resume + cover upload fields (independent WITH-cover check).
"""
from apply_engine.browser import launch_profile
from apply_engine.adapters.greenhouse import GreenhouseAdapter
from apply_engine.adapters.ashby import AshbyAdapter
from apply_engine.form_spec import scrape_constraints
from apply_engine.reconcile import reconcile_form


# ---------------------------------------------------------------------------
# Constraint scrape — pure, offline (no page needed).
# ---------------------------------------------------------------------------

def test_scrape_word_range():
    c = scrape_constraints(helper_text="Please answer in 200-400 words.")
    assert c == {"words": [200, 400]}


def test_scrape_word_range_endash_and_to():
    assert scrape_constraints(helper_text="200–400 words")["words"] == [200, 400]
    assert scrape_constraints(helper_text="answer in 150 to 300 words")["words"] == [150, 300]


def test_scrape_maxlength_attr_yields_chars_max():
    assert scrape_constraints(maxlength=500) == {"chars_max": 500}
    assert scrape_constraints(maxlength="500") == {"chars_max": 500}


def test_scrape_char_max_from_copy():
    assert scrape_constraints(helper_text="max 500 characters")["chars_max"] == 500
    assert scrape_constraints(placeholder="up to 280 chars")["chars_max"] == 280


def test_scrape_word_and_char_maxima():
    assert scrape_constraints(helper_text="maximum 400 words")["words_max"] == 400
    assert scrape_constraints(helper_text="at least 150 words")["words_min"] == 150


def test_scrape_neither_yields_empty():
    assert scrape_constraints(helper_text="Tell us about yourself.") == {}
    assert scrape_constraints() == {}


def test_maxlength_attr_wins_over_copy_for_chars():
    # the attribute is authoritative; copy doesn't overwrite it
    c = scrape_constraints(helper_text="max 999 characters", maxlength=500)
    assert c["chars_max"] == 500


# ---------------------------------------------------------------------------
# enumerate_fields — against the live fixture DOM.
# ---------------------------------------------------------------------------

def _spec(fixture_server, tmp_path, fixture):
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/{fixture}")
        return a.enumerate_fields(page)


def test_enumerate_fields_covers_every_field(fixture_server, tmp_path):
    spec = _spec(fixture_server, tmp_path, "phase0_form_spec_form.html")
    by_key = {f.key: f for f in spec.fields}
    # every authored field is present
    for k in ("first_name", "email", "employer", "title", "why", "short_essay",
              "open_essay", "exp", "resume", "cover"):
        assert k in by_key, (k, list(by_key))
    # widget kinds correct
    assert by_key["employer"].widget_kind == "text"
    assert by_key["why"].widget_kind == "textarea"
    assert by_key["exp"].widget_kind == "native_select"
    assert by_key["resume"].widget_kind == "file"
    assert by_key["cover"].widget_kind == "file"


def test_enumerate_fields_required_flags(fixture_server, tmp_path):
    spec = _spec(fixture_server, tmp_path, "phase0_form_spec_form.html")
    by_key = {f.key: f for f in spec.fields}
    assert by_key["first_name"].required is True
    assert by_key["employer"].required is True
    assert by_key["title"].required is True
    assert by_key["why"].required is True
    assert by_key["resume"].required is True
    # the unconstrained "Anything else?" essay and the cover upload are optional
    assert by_key["open_essay"].required is False
    assert by_key["cover"].required is False


def test_enumerate_fields_constraints_captured(fixture_server, tmp_path):
    spec = _spec(fixture_server, tmp_path, "phase0_form_spec_form.html")
    by_key = {f.key: f for f in spec.fields}
    assert by_key["why"].constraints == {"words": [200, 400]}      # helper "200-400 words"
    assert by_key["short_essay"].constraints == {"chars_max": 500}  # maxlength attr
    assert by_key["open_essay"].constraints == {}                   # neither


def test_enumerate_fields_doc_kinds_and_g7_with_cover(fixture_server, tmp_path):
    spec = _spec(fixture_server, tmp_path, "phase0_form_spec_form.html")
    by_key = {f.key: f for f in spec.fields}
    assert by_key["resume"].doc_kind == "resume"
    assert by_key["cover"].doc_kind == "cover"
    assert spec.has_resume_field is True
    assert spec.has_cover_field is True   # G7: this form HAS a cover field


def test_enumerate_fields_g7_without_cover(fixture_server, tmp_path):
    """greenhouse_form.html has a resume input but NO cover field — has_cover_field is False."""
    spec = _spec(fixture_server, tmp_path, "greenhouse_form.html")
    assert spec.has_resume_field is True
    assert spec.has_cover_field is False
    # the native sponsorship <select> is enumerated as a native_select (work-auth field still a field)
    kinds = {f.widget_kind for f in spec.fields}
    assert "native_select" in kinds
    assert "file" in kinds


def test_enumerate_fields_ashby_detects_cover(fixture_server, tmp_path):
    """Independent G7 WITH-cover check on the Ashby modern fixture (resume + cover file inputs)."""
    a = AshbyAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/ashby_modern_form.html")
        spec = a.enumerate_fields(page)
    assert spec.has_resume_field is True
    assert spec.has_cover_field is True


# ---------------------------------------------------------------------------
# reconcile_form — pure diff of a built spec vs a staged record.
# ---------------------------------------------------------------------------

def test_reconcile_clean_match(fixture_server, tmp_path):
    """A staged essay answer for the 'Why do you want to work here?' field maps cleanly."""
    spec = _spec(fixture_server, tmp_path, "phase0_form_spec_form.html")
    record = {
        "custom_qs": [
            {"q": "Why do you want to work here?", "kind": "essay",
             "value": "I am excited about your mission and my FEA background fits the role well. "
                      * 10, "status": "answered"},
        ],
        "filled_fields": ["first_name", "email", "employer", "title"],
        "uploaded_docs": [{"kind": "resume"}, {"kind": "cover"}],
        "custom_qs_extra": None,
    }
    # also stage the required select + employer so they don't show as unfilled
    record["custom_qs"].append(
        {"q": "Years of simulation experience?", "kind": "select", "value": "3-5 years",
         "status": "answered"})
    record["custom_qs"].append(
        {"q": "Describe a project", "kind": "essay", "value": "Built a smooth-baseline FEA model.",
         "status": "answered"})
    res = reconcile_form(spec, record)
    why = [m for m in res.matched if "why" in m.staged_label.lower()]
    assert why and why[0].classification == "matched"
    assert why[0].needs_human_or_llm is False


def test_reconcile_narrative_into_short_employer_is_mismatch(fixture_server, tmp_path):
    """A 253-char narrative staged for the SHORT 'Current employer' field -> mismatched +
    needs_human_or_llm (NOT guess-mapped). This is the §8.1 live-run defect."""
    spec = _spec(fixture_server, tmp_path, "phase0_form_spec_form.html")
    narrative = ("At Meridian Devices I led R&D product development across multiple programs, "
                 "owning FEA validation, prototyping, and cross-functional execution from "
                 "concept through to manufacturing handoff over several seasons of work.")
    assert len(narrative) > 120
    record = {"custom_qs": [
        {"q": "Current employer", "kind": "short_text", "value": narrative, "status": "answered"},
    ]}
    res = reconcile_form(spec, record)
    mis = [m for m in res.mismatched if "employer" in m.live_label.lower()]
    assert mis, [m.classification for m in res.all_outcomes()]
    assert mis[0].classification == "mismatched"
    assert mis[0].needs_human_or_llm is True
    assert mis[0].staged_value  # evidence carried for the future mapper


def test_reconcile_cover_with_no_cover_field_is_structural(fixture_server, tmp_path):
    """Cover content staged but the live form has NO cover upload field -> missing_live_field,
    flagged STRUCTURAL (G7), never a failure."""
    spec = _spec(fixture_server, tmp_path, "greenhouse_form.html")  # resume only, no cover
    record = {"uploaded_docs": [{"kind": "resume"}, {"kind": "cover"}]}
    res = reconcile_form(spec, record)
    cover = [m for m in res.missing_live_field if m.staged_value == "cover"]
    assert cover, [(m.classification, m.staged_value) for m in res.all_outcomes()]
    assert cover[0].structural is True
    # the resume, which DOES have a field, is matched not missing
    assert any(m.staged_value == "resume" for m in res.matched)


def test_reconcile_unfilled_required_live(fixture_server, tmp_path):
    """A required live field ('Current title') with no staged answer -> unfilled_required_live."""
    spec = _spec(fixture_server, tmp_path, "phase0_form_spec_form.html")
    record = {"custom_qs": [], "filled_fields": ["first_name", "email"]}
    res = reconcile_form(spec, record)
    labels = {m.live_label.lower() for m in res.unfilled_required_live}
    assert any("current title" in l for l in labels), labels
    # 'why' essay (required, unstaged) is also surfaced as unfilled-required
    assert any("why do you want" in l for l in labels), labels


def test_reconcile_missing_live_field_for_orphan_answer(fixture_server, tmp_path):
    """A staged answer with no matching live field -> missing_live_field (structural)."""
    spec = _spec(fixture_server, tmp_path, "greenhouse_form.html")
    record = {"custom_qs": [
        {"q": "What is your favorite programming paradigm?", "kind": "essay",
         "value": "Declarative.", "status": "answered"}]}
    res = reconcile_form(spec, record)
    orphan = [m for m in res.missing_live_field
              if "paradigm" in m.staged_label.lower()]
    assert orphan and orphan[0].structural is True


def test_reconcile_clean_property(fixture_server, tmp_path):
    """`clean` is True only when there are no mismatches and no unfilled-required (structural
    missing fields don't count)."""
    spec = _spec(fixture_server, tmp_path, "greenhouse_form.html")
    # a record that covers the required fields and only has a structural orphan
    record = {
        "filled_fields": ["first_name", "last_name", "email"],
        "uploaded_docs": [{"kind": "resume"}, {"kind": "cover"}],  # cover is structural-missing
    }
    res = reconcile_form(spec, record)
    # the only non-matched outcome is the structural cover -> clean stays True
    assert res.mismatched == []
    assert res.unfilled_required_live == []
    assert res.clean is True
