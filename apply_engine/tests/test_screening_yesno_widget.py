"""Wiring test: custom Yes/No screening BUTTON-GROUPS (Ashby) are discovered, work-auth/EEO are
excluded from discovery (work-auth) or escalated by the classifier (EEO), and an answered screen
is driven via the adapter's VERIFIED `_act` read-back. Regression guard for the LangChain (Ashby)
gap found 2026-06-09 — the screening classifier existed but was never wired to button-groups."""
from apply_engine.browser import launch_profile
from apply_engine.adapters.ashby import AshbyAdapter
from apply_engine.screening import resolve_with_screening, load_capabilities


def test_find_screening_yesno_excludes_workauth_returns_custom(fixture_server, tmp_path):
    a = AshbyAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/ashby_screening_form.html")
        qs = a.find_screening_yesno_questions(page)
        labels = [q.label for q in qs]
        # the work-auth sponsorship question is owned by the work-auth guard -> NOT here
        assert not any("sponsorship" in l.lower() for l in labels)
        # the custom screening qualifier IS returned, as a button-yesno
        assert any("3+ years" in l for l in labels)
        # the EEO question shares the Yes/No shape so it is RETURNED by discovery...
        assert any("hispanic" in l.lower() for l in labels)
        assert all(q.kind == "button-yesno" for q in qs)


def test_eeo_button_group_is_escalated_by_the_classifier(fixture_server, tmp_path):
    # ...but the classifier ESCALATES it, so the orchestrator never auto-answers it.
    a = AshbyAdapter()
    caps = load_capabilities()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/ashby_screening_form.html")
        eeo = next(q for q in a.find_screening_yesno_questions(page)
                   if "hispanic" in q.label.lower())
        # a tracker llm that would wrongly say YES if reached; EEO must short-circuit before it
        calls = []
        ch = resolve_with_screening(eeo.label, ["Yes", "No"], facts="", capabilities=caps,
                                    llm_fn=lambda p: calls.append(1) or "YES", audit_fn=lambda t: [])
        assert ch.status == "declined"
        assert not calls


def test_answered_screen_drives_button_and_verifies_via_act(fixture_server, tmp_path):
    a = AshbyAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/ashby_screening_form.html")
        q = next(x for x in a.find_screening_yesno_questions(page) if "3+ years" in x.label)
        ok = a.answer_yes(page, q)          # verified driver (reads back _act)
        assert ok is True
        # the chosen Yes button now carries the _act active class
        block = page.query_selector("xpath=//label[contains(.,'3+ years')]/following-sibling::div")
        yes_btn = next(b for b in block.query_selector_all("button")
                       if (b.inner_text() or "").strip() == "Yes")
        assert "_act" in (yes_btn.get_attribute("class") or "")
