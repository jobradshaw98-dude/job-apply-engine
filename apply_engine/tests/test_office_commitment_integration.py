"""Real-Playwright integration: screen-clearing commitment questions get answered Yes + VERIFIED
on BOTH widget styles (button-group + react-select), while an EEO control is never driven.

Policy 2026-06-09: relocation is now AUTO_YES (Sam is open to relocation), joining the same
screen-clearing bucket as in-office/RTO — so the office guard answers office AND relocation Qs.
Only EEO/demographic stays untouched. Runs against fixtures/office_commitment_form.html, which
carries 3 office Qs (2 button-group + 1 react-select), 2 relocation Qs (button + react-select),
and 1 EEO control."""
from apply_engine.browser import launch_profile
from apply_engine.adapters.greenhouse import GreenhouseAdapter
from apply_engine.office_commitment import (classify_office_commitment,
                                            OfficeCommitmentDecision)


def _act_count(page, group):
    return len(page.query_selector_all(f"._yesno[data-group='{group}'] button[class*='_act']"))


def test_finds_office_and_relocation_questions(fixture_server, tmp_path):
    """Detection captures the screen-clearing bucket: 3 office Qs + 2 relocation Qs (now AUTO_YES
    per the 2026-06-09 policy), and EXCLUDES EEO — which shares the Yes/No shape."""
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/office_commitment_form.html")
        qs = a.find_office_commitment_questions(page)
        labels = [q.label.lower() for q in qs]
        # office (3) + relocation (2) = 5; EEO never included
        assert len(qs) == 5, labels
        assert any("four days per week" in l for l in labels)
        assert any("hybrid" in l for l in labels)
        assert any("in-person" in l and "25%" in l for l in labels)
        assert any("relocat" in l for l in labels)        # relocation IS now in-bucket
        assert not any("gender" in l for l in labels)     # EEO never


def test_office_and_relocation_answered_yes_and_verified(fixture_server, tmp_path):
    """answer_yes on each detected office/relocation question returns a VERIFIED True; button-
    groups read back a single Yes `_act`, react-selects read back data-value Yes."""
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/office_commitment_form.html")
        qs = a.find_office_commitment_questions(page)
        assert len(qs) == 5
        for q in qs:
            assert classify_office_commitment(q.label) == OfficeCommitmentDecision.AUTO_YES
            assert a.answer_yes(page, q) is True, q.label

        # button-groups: exactly one _act (Yes) each — office AND relocation now answered
        for group in ("office_days", "hybrid", "relocation"):
            assert _act_count(page, group) == 1, group
            acts = page.query_selector_all(
                f"._yesno[data-group='{group}'] button[class*='_act']")
            assert (acts[0].inner_text() or "").strip() == "Yes"
        # react-select office + relocation questions both read back Yes
        assert page.query_selector('[data-rsname="inperson"]').get_attribute("data-value") == "Yes"
        assert page.query_selector('[data-rsname="reloc_rs"]').get_attribute("data-value") == "Yes"


def test_eeo_left_untouched(fixture_server, tmp_path):
    """EEO/demographic controls must NEVER be auto-driven by the screen-clearing guard."""
    a = GreenhouseAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/office_commitment_form.html")
        for q in a.find_office_commitment_questions(page):
            a.answer_yes(page, q)
        assert _act_count(page, "eeo") == 0
