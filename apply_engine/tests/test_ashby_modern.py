"""Ashby live-DOM hardening regression guard (PART 1 of the staged-apply scale-up).

Real DOM facts from the Ramp submit (jobs.ashbyhq.com 2026-06-08) folded into the adapter:
  * posting -> form: "Apply for this Job" click reveals the /application form;
  * Yes/No questions are BUTTON GROUPS — the selected button gets a class CONTAINING `_act`
    (no aria-pressed/checked); answers MUST verify by reading that `_act` state back;
  * a failed set returns a VERIFIED False (caller HALTs — never a phantom work-auth answer);
  * label-substring answering NEVER drives a work-auth or EEO control;
  * files attach via a visible "Upload File" button -> native chooser;
  * submit does NOT change the URL — an in-place "...has been received" success panel appears.

Real-Playwright against fixtures/ashby_modern_form.html + ashby_posting.html."""
from apply_engine.browser import launch_profile
from apply_engine.adapters.ashby import AshbyAdapter
from apply_engine.source_data import Answers
from apply_engine.work_auth import classify_work_auth, WorkAuthDecision


def _answers(tmp_path, with_cover=False):
    r = tmp_path / "r.pdf"; r.write_bytes(b"%PDF-1.4 resume")
    c = None
    if with_cover:
        c = tmp_path / "c.pdf"; c.write_bytes(b"%PDF-1.4 cover")
    return Answers(values={"full_name": "Sam Rivera",
                           "email": "sam.rivera@example.com", "phone": "555-555-0100"},
                   resume_pdf=r, cover_pdf=c)


# ---- posting -> form navigation -------------------------------------------------------

def test_go_to_form_clicks_apply_for_this_job(fixture_server, tmp_path):
    """The posting has no form until "Apply for this Job" is clicked. go_to_form must click it
    and reveal the resume field."""
    a = AshbyAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/ashby_posting.html")
        assert page.query_selector(a.resume_selector) is None   # no form yet
        a.go_to_form(page)
        assert page.query_selector(a.resume_selector) is not None  # form now rendered


# ---- button-group Y/N answering verified via the _act active class --------------------

def test_button_group_answer_sets_act_and_verifies_true(fixture_server, tmp_path):
    """Answering a work-auth button-group clicks the chosen button, which gains an `_act`-stem
    class; answer_* returns a VERIFIED True and the chosen button reads back active."""
    a = AshbyAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/ashby_modern_form.html")
        qs = a.find_work_auth_questions(page)
        # combined auth-without-sponsorship + sponsorship are both button-group work-auth Qs
        kinds = {q.kind for q in qs}
        assert kinds == {"button-yesno"}, kinds
        for q in qs:
            d = classify_work_auth(q.label)
            if d == WorkAuthDecision.SPONSORSHIP_NO:
                assert a.answer_no(page, q) is True
            elif d in (WorkAuthDecision.AUTHORIZED_YES,
                       WorkAuthDecision.AUTHORIZED_NO_SPONSORSHIP):
                assert a.answer_yes(page, q) is True
        # read back: each answered group has exactly one button with an _act class
        for group in ("auth", "sponsor"):
            acts = page.query_selector_all(f"._yesno[data-group='{group}'] button[class*='_act']")
            assert len(acts) == 1, (group, [b.get_attribute("class") for b in acts])


def test_button_group_failed_set_returns_false(tmp_path):
    """If the question's button block can't be relocated (label not present), the verified
    answer path returns False — the caller HALTs rather than record a phantom answer."""
    from apply_engine.adapters.base import WorkAuthQuestion
    a = AshbyAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto("data:text/html,<form><label>Authorized to work?*</label>"
                  "<div><button type='button'>Yes</button><button type='button'>No</button>"
                  "</div></form>")
        # a work-auth question whose label does NOT exist on the page -> cannot relocate -> False
        q = WorkAuthQuestion(label="THIS LABEL IS NOT ON THE PAGE", selector="",
                             kind="button-yesno")
        assert a.answer_yes(page, q) is False


def test_button_group_no_act_class_returns_false(tmp_path):
    """If clicking the button does NOT produce an `_act` class (the click silently didn't
    register), the verified read-back fails and answer_* returns False — never a phantom set."""
    from apply_engine.adapters.base import WorkAuthQuestion
    a = AshbyAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        # buttons that do NOTHING on click (no _act ever applied)
        page.goto("data:text/html,<form>"
                  "<label>Will you require sponsorship?*</label>"
                  "<div><button type='button'>Yes</button><button type='button'>No</button>"
                  "</div></form>")
        q = WorkAuthQuestion(label="Will you require sponsorship?*", selector="",
                             kind="button-yesno")
        assert a.answer_no(page, q) is False


# ---- label-substring answering NEVER touches work-auth or EEO controls ----------------

def test_label_substring_refuses_work_auth_control(fixture_server, tmp_path):
    """answer_button_group_by_label must REFUSE a work-auth control even if the substring
    matches — a 'sponsorship'/'authorized' label is owned by the guard, never driven here."""
    a = AshbyAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/ashby_modern_form.html")
        assert a.answer_button_group_by_label(page, "sponsorship", "Yes") is False
        assert a.answer_button_group_by_label(page, "authorized to work", "Yes") is False
        # neither work-auth group was touched
        for group in ("auth", "sponsor"):
            assert page.query_selector_all(
                f"._yesno[data-group='{group}'] button[class*='_act']") == []


def test_label_substring_refuses_eeo_control(fixture_server, tmp_path):
    """answer_button_group_by_label must REFUSE an EEO/demographic control by substring."""
    a = AshbyAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/ashby_modern_form.html")
        assert a.answer_button_group_by_label(page, "underrepresented gender", "Yes") is False
        assert page.query_selector_all(
            "._yesno[data-group='eeo'] button[class*='_act']") == []


def test_label_substring_drives_a_real_custom_group(fixture_server, tmp_path):
    """A genuine NON-work-auth/non-EEO custom group (RTO commitment) IS driven + verified."""
    a = AshbyAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/ashby_modern_form.html")
        assert a.answer_button_group_by_label(page, "willing to work from our office", "Yes") is True
        acts = page.query_selector_all("._yesno[data-group='rto'] button[class*='_act']")
        assert len(acts) == 1
        assert (acts[0].inner_text() or "").strip() == "Yes"


# ---- file uploads via the visible "Upload File" button --------------------------------

def test_resume_and_cover_upload_via_button(fixture_server, tmp_path):
    """Resume (required) attaches via the base React-safe Upload-File flow; cover (optional)
    attaches via the adapter's _attach_file. Both register the filename visibly."""
    a = AshbyAdapter()
    ans = _answers(tmp_path, with_cover=True)
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/ashby_modern_form.html")
        a.fill(page, ans)
        assert a.resume_attached_ok is True
        assert a.cover_attached_ok is True
        body = page.inner_text("body")
        assert "r.pdf" in body and "c.pdf" in body


def test_cover_optional_absent_does_not_fail(fixture_server, tmp_path):
    """With no cover PDF, fill must not flip cover_attached_ok to False (optional field)."""
    a = AshbyAdapter()
    ans = _answers(tmp_path, with_cover=False)
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/ashby_modern_form.html")
        a.fill(page, ans)
        assert a.resume_attached_ok is True
        assert a.cover_attached_ok is None   # untouched — no cover to attach


# ---- in-place success panel detection -------------------------------------------------

def test_submit_succeeded_detects_in_place_panel(fixture_server, tmp_path):
    """Before submit: submit_succeeded is False (submit button present, no success text).
    After clicking submit: the form is replaced in place by the success panel (no URL change),
    and submit_succeeded reads True."""
    a = AshbyAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/ashby_modern_form.html")
        assert a.submit_succeeded(page) is False
        url_before = page.url
        page.click("#submit_btn")
        page.wait_for_timeout(300)
        assert page.url == url_before          # Ashby does NOT navigate on submit
        assert a.submit_succeeded(page) is True


def test_submit_not_succeeded_while_form_present(fixture_server, tmp_path):
    """With the form still showing (submit button visible, no success text), submit_succeeded
    is False even after filling — only the in-place panel flips it True."""
    a = AshbyAdapter()
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/ashby_modern_form.html")
        a.fill(page, _answers(tmp_path))
        assert a.submit_succeeded(page) is False
