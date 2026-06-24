"""Fixture-backed tests for finish.replay against the greenhouse_custom_form fixture.

These exercise the deterministic re-fill: standard fields, the sponsorship work-auth
guard, and custom answers re-filled FROM THE STORED RECORD (select + checkbox-group).
They do NOT submit (submit=False) — the real submit click can only be verified on a live
ATS (a fixture has no server to confirm submission against). The submit-control finder is
tested directly against the fixture's button."""
import pytest

from apply_engine.browser import launch_profile
from apply_engine.adapters.greenhouse import GreenhouseAdapter
from apply_engine.ats_detect import AtsKind
from apply_engine.finish import replay, _find_submit_control
from apply_engine.source_data import Answers


@pytest.fixture
def answers(tmp_path):
    r = tmp_path / "r.pdf"
    r.write_bytes(b"%PDF-1.4 fake")
    return Answers(
        values={"first_name": "Sam", "last_name": "Rivera",
                "email": "sam.rivera@example.com", "phone": "555-555-0100"},
        resume_pdf=r, cover_pdf=None)


def _record(**over):
    rec = {
        "job_id": "JOB-CUSTOM",
        "status": "ready_to_submit",
        "submitted": False,
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "work_auth": [{"field": "sponsor", "q": "sponsorship?", "answer": "No"}],
        "custom_qs": [
            {"q": "Years of simulation experience?", "kind": "select",
             "status": "answered", "value": "5+ years"},
            {"q": "Language Skill(s) (check all that apply)", "kind": "checkbox_group",
             "status": "answered", "values": ["English (ENG)", "Mandarin (MAN)"]},
        ],
        "unfilled_required": [],
        "needs_sam": [],
    }
    rec.update(over)
    return rec


def test_replay_refills_standard_workauth_and_custom(fixture_server, answers, tmp_path):
    adapter = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_custom_form.html")
        res = replay(_record(), page, answers, adapter, submit=False)

        assert res["ok"] is True
        assert res["submitted"] is False
        assert res["opened"] is True

        # standard fields re-filled + verified by read-back
        assert page.query_selector("#first_name").input_value() == "Sam"
        assert page.query_selector("#email").input_value() == "sam.rivera@example.com"

        # work-auth sponsorship answered "No" deterministically
        assert page.query_selector("#q_sponsor").input_value() == "no"

        # custom select re-filled from the STORED value
        assert page.query_selector("#q_exp").input_value() == "c"  # "5+ years"

        # custom checkbox-group: stored subset checked, others left unchecked
        assert page.query_selector("#lang_eng").is_checked() is True
        assert page.query_selector("#lang_man").is_checked() is True
        assert page.query_selector("#lang_fra").is_checked() is False

        # the custom questions were matched (not orphaned)
        assert not res["unmatched_custom"]
        assert any(r.startswith("custom:") for r in res["refilled"])


def test_replay_reports_unmatched_custom(fixture_server, answers, tmp_path):
    # stored record's custom labels don't match the live form -> reported, never guessed
    rec = _record(custom_qs=[{"q": "Totally unrelated question", "kind": "select",
                              "status": "answered", "value": "x"}])
    adapter = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_custom_form.html")
        res = replay(rec, page, answers, adapter, submit=False)
        assert res["ok"] is True
        # the live custom select + checkbox-group had no stored match
        labels = {u["q"] for u in res["unmatched_custom"]}
        assert "Years of simulation experience?" in labels


def test_replay_does_not_click_submit_when_submit_false(fixture_server, answers, tmp_path):
    # submit=False must leave us on the same form page (no navigation / no submit)
    adapter = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_custom_form.html")
        url_before = page.url
        res = replay(_record(), page, answers, adapter, submit=False)
        assert res["submitted"] is False
        assert page.url == url_before  # still on the form, never submitted


def test_find_submit_control_locates_greenhouse_button(fixture_server, tmp_path):
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_custom_form.html")
        el = _find_submit_control(page, AtsKind.GREENHOUSE)
        assert el is not None
        assert (el.get_attribute("id") or "") == "submit_app"


def test_replay_aborts_on_verification_mismatch(fixture_server, tmp_path, monkeypatch):
    # force a read-back mismatch: adapter reports a different value than it filled -> ABORT,
    # and crucially submitted stays False (never submit on drift).
    r = tmp_path / "r.pdf"; r.write_bytes(b"%PDF")
    answers = Answers(values={"first_name": "Sam", "last_name": "Rivera",
                              "email": "sam.rivera@example.com", "phone": "555-555-0100"},
                      resume_pdf=r, cover_pdf=None)
    adapter = GreenhouseAdapter()

    def _bad_readback(page, keys):
        return {k: "WRONG" for k in keys}
    monkeypatch.setattr(adapter, "read_back", _bad_readback)

    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_custom_form.html")
        res = replay(_record(), page, answers, adapter, submit=True)
        assert res["ok"] is False
        assert res["submitted"] is False
        assert "mismatch" in res["reason"].lower()
