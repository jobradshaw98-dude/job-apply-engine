"""Ashby adapter — hardened against the LIVE jobs.ashbyhq.com DOM (Ramp submit 2026-06-08).

What the live form actually does (folded in below):
  * The POSTING (<job_id>) is NOT the form — it has zero inputs. Clicking "Apply for this
    Job" navigates to <job_id>/application, where the form renders (a slow client-rendered
    SPA). `go_to_form` clicks that button if present, then ensures the URL ends /application.
  * System fields are stable by id: #_systemfield_name (single Legal Name), #_systemfield_email,
    #_systemfield_resume (file, REQUIRED), #cover_letter (file, OPTIONAL). Phone varies per
    job (#phone or any tel input).
  * Yes/No questions are BUTTON GROUPS, not <select>: a hidden checkbox + a Yes/No button
    pair. The SELECTED button gets a class CONTAINING "_act" (active). There is NO
    aria-pressed / aria-checked — selection is read back ONLY from the _act class.
  * File fields have a visible "Upload File" button → native chooser → set_files.
  * Submitting does NOT change the URL — the form is replaced IN PLACE by a success panel
    ("Application — Success — ... Your application has been received"). Detected in finish.py
    via `submit_succeeded` (submit button gone AND success text present).

SAFETY: every answer/widget-action method returns a VERIFIED bool — it drives the widget,
reads back the _act state, and returns True ONLY if the chosen button is active (and the
other is NOT). A False return means the orchestrator/finish MUST HALT to the user; a phantom
answer (the one unrecoverable wrong-answer field, work-auth) is never recorded. Label-
substring matching for custom button-groups NEVER touches a work-auth or EEO control —
those are owned by the work-auth guard / left for the user.
"""
import re

from .base import FormAdapterBase, WorkAuthQuestion
from ..work_auth import classify_work_auth, WorkAuthDecision


class AshbyAdapter(FormAdapterBase):
    name = "ashby"
    text_fields = {
        "full_name": "#_systemfield_name",          # single "Legal Name" field
        "email": "#_systemfield_email",
        "phone": "#phone, input[type='tel']",        # first match wins (system or custom)
    }
    resume_selector = "#_systemfield_resume"
    cover_selector = "#cover_letter"
    cover_attached_ok = None     # set during fill: True/False if cover field present + attached

    # ---- navigation: posting -> /application form ----
    def go_to_form(self, page) -> None:
        """Reveal the application form. On a live Ashby posting the form does not exist until
        the "Apply for this Job" button is clicked (which routes <id> -> <id>/application). We
        click it if present, then ensure the URL ends /application as a belt-and-suspenders
        fallback. No-op once the resume field is on the page (form already rendered)."""
        if page.query_selector(self.resume_selector):
            return
        # 1) Click "Apply for this Job" if the posting shows it (the form renders after).
        self._click_apply_for_this_job(page)
        if page.query_selector(self.resume_selector):
            return
        # 2) Fallback: navigate straight to the /application route.
        try:
            cur = page.url.split("?")[0].rstrip("/")
        except Exception:
            cur = ""
        if cur and not cur.endswith("/application"):
            try:
                page.goto(cur + "/application", wait_until="networkidle")
                page.wait_for_timeout(4000)
            except Exception:
                pass
            # the button may live on the /application route too (some postings)
            if not page.query_selector(self.resume_selector):
                self._click_apply_for_this_job(page)

    def _click_apply_for_this_job(self, page) -> None:
        """Click the "Apply for this Job" CTA if present. Matches by visible text so it
        survives Ashby's class churn. Best-effort; waits for the SPA to render the form."""
        try:
            els = page.query_selector_all("a, button")
        except Exception:
            return
        for el in els:
            try:
                txt = " ".join((el.inner_text() or "").split()).lower()
            except Exception:
                continue
            if "apply for this job" in txt or txt == "apply":
                try:
                    el.click()
                    page.wait_for_timeout(2500)
                except Exception:
                    pass
                return

    # ---- fill: core text fields + resume (required) + cover (optional) ----
    def fill(self, page, answers) -> dict:
        intended = super().fill(page, answers)   # text fields + resume via base
        # cover letter is OPTIONAL on Ashby — attach only if we have one and the field exists.
        cover = getattr(answers, "cover_pdf", None)
        if cover and self.cover_selector and page.query_selector(self.cover_selector):
            self.cover_attached_ok = self._attach_file(page, self.cover_selector, str(cover))
        return intended

    def _attach_file(self, page, selector: str, pdf_path) -> bool:
        """Attach a file via Ashby's visible "Upload File" button -> native chooser -> set_files,
        falling back to a direct set on the hidden input. Returns True only if the filename is
        VISIBLY registered (the positive signal Ashby actually took the upload). Mirrors the
        base resume-attach flow but targets an arbitrary file field (resume reuses the base)."""
        from pathlib import Path
        fname = Path(pdf_path).name
        # 1) visible "Upload File" button next to this field -> native chooser (React-safe)
        try:
            btn = self._find_upload_button(page, selector)
            if btn is not None:
                with page.expect_file_chooser(timeout=6000) as fc:
                    btn.click()
                fc.value.set_files(pdf_path)
                page.wait_for_timeout(1500)
                if self._resume_filename_visible(page, fname):
                    return True
        except Exception:
            pass
        # 2) fallback: direct set on the hidden input
        try:
            el = page.query_selector(selector)
            if el:
                el.set_input_files(pdf_path)
                page.wait_for_timeout(1000)
                return self._resume_filename_visible(page, fname)
        except Exception:
            pass
        return False

    def _find_upload_button(self, page, file_selector: str):
        """The visible "Upload File" button inside the file field's container. Walk up from the
        hidden <input type=file> and find a button/label whose text matches Upload/Attach.
        Returns an element handle or None."""
        inp = page.query_selector(file_selector)
        if not inp:
            return None
        try:
            handle = inp.evaluate_handle(
                "e => { let c = e.closest('div');"
                " for (let i=0;i<6 && c;i++){"
                "  const b=[...c.querySelectorAll('button,label')]"
                "   .find(x => /upload file|upload|attach/i.test(x.textContent||''));"
                "  if (b) return b; c=c.parentElement; }"
                " return null; }")
            return handle.as_element() if handle else None
        except Exception:
            return None

    # Resume attach: reuse the base's React-safe flow but recognize Ashby's "Upload File"
    # button text too. The base's _find_resume_attach_button already matches /attach|upload/i,
    # which covers "Upload File", so no override is needed.

    # ---- work-auth detection: Ashby renders Yes/No as a button GROUP (no native input) ----
    def find_work_auth_questions(self, page):
        """Find work-auth questions. Reuses the base detection (native select / react-select /
        radio / generic button-yesno) AND adds Ashby's _act button-group widget, which the base
        button-yesno path also recognizes (Yes/No buttons in a block). The base already returns
        kind='button-yesno' for those, and `_answer` below drives + verifies them via _act."""
        return super().find_work_auth_questions(page)

    # ---- answering: Ashby button groups verify via the _act class ----
    def _answer(self, page, q: WorkAuthQuestion, choice_text: str, native_value: str) -> bool:
        """Override the base button-yesno path to VERIFY via Ashby's `_act` active class (the
        base only verified select/radio; its button path returned True on click without read-
        back). Other widget kinds fall through to the base implementation unchanged.

        Verified contract: click the target Yes/No button, then read back — return True ONLY if
        the chosen button now carries an `_act` class AND the other button does NOT. A False
        means the click didn't register; the caller HALTs to the user rather than record a phantom
        work-auth answer."""
        if q.kind == "button-yesno":
            block = self._relocate_button_block(page, q.label)
            if block is None:
                return False
            return self._click_yesno_and_verify(page, block, choice_text)
        return super()._answer(page, q, choice_text, native_value)

    def _relocate_button_block(self, page, label: str):
        """Re-find the block element that holds the Yes/No button pair for `label`. Ashby labels
        are not always <label for=>, so match any <label> whose text equals the stored question
        label, then walk to its enclosing block. Returns an element handle or None."""
        for lbl in page.query_selector_all("label"):
            try:
                if (lbl.inner_text() or "").strip() != (label or "").strip():
                    continue
            except Exception:
                continue
            block = lbl.evaluate_handle("e => e.closest('div, fieldset, li')")
            el = block.as_element() if block else None
            if el is not None:
                return el
        return None

    def _click_yesno_and_verify(self, page, block, choice_text: str) -> bool:
        """Click the Yes/No button matching choice_text inside `block`, then VERIFY via the
        `_act` active class. Returns True ONLY if the chosen button reads back active and the
        other does NOT. Drives by visible button text; never trusts the click."""
        want = (choice_text or "").strip().lower()
        target = other = None
        for b in block.query_selector_all("button"):
            try:
                t = (b.inner_text() or "").strip().lower()
            except Exception:
                continue
            if t == want:
                target = b
            elif t in ("yes", "no"):
                other = b
        if target is None:
            return False
        try:
            target.click()
        except Exception:
            return False
        # Let the SPA's async class-swap settle before read-back: Ashby toggles `_act` on the
        # chosen button AND removes it from the sibling asynchronously, so on a re-answer
        # (Yes->No) an immediate read can briefly see BOTH active and falsely report ambiguous.
        try:
            page.wait_for_timeout(150)
        except Exception:
            pass
        # read back the _act active state from the chosen + the other button.
        if not self._button_is_active(target):
            return False
        if other is not None and self._button_is_active(other):
            # both active => ambiguous; never report a verified single choice.
            return False
        return True

    @staticmethod
    def _button_is_active(btn) -> bool:
        """True if the button's class list contains a token CONTAINING `_act` (Ashby marks the
        selected button with an `_act…`-style active class — e.g. `_active_xxx` / `_act_hash`).
        Substring match on the class string is correct here: the live class is hashed
        (`_active_h7s2`), so we look for the `_act` stem, not an exact class name."""
        try:
            cls = (btn.get_attribute("class") or "").lower()
        except Exception:
            return False
        return "_act" in cls

    # ---- custom (non-work-auth) Yes/No button groups, answered by label substring ----
    def answer_button_group_by_label(self, page, label_substr: str, choice_text: str) -> bool:
        """Answer a custom Yes/No BUTTON-GROUP whose label contains `label_substr`, returning a
        VERIFIED bool (the chosen button reads back `_act`). Mirrors select_react_by_label for
        react-selects, but for Ashby's button widget.

        HARD SAFETY: never touches a control whose label classifies as work-auth or EEO — those
        are owned by the work-auth guard / left for the user, and a label-substring match
        ('authorized', 'gender', etc.) must never drive them here. Returns False if no matching
        non-work-auth/non-EEO block is found, or the click didn't verify."""
        sub = (label_substr or "").strip().lower()
        if not sub:
            return False
        try:
            from ..questions import _is_eeo
        except Exception:
            def _is_eeo(_n, _l):
                return False
        for lbl in page.query_selector_all("label"):
            try:
                text = (lbl.inner_text() or "").strip()
            except Exception:
                continue
            if sub not in text.lower():
                continue
            # SKIP any work-auth or EEO control — never answer those by substring.
            if classify_work_auth(text) != WorkAuthDecision.UNRELATED:
                continue
            if _is_eeo("", text):
                continue
            block = lbl.evaluate_handle("e => e.closest('div, fieldset, li')")
            el = block.as_element() if block else None
            if el is None:
                continue
            btn_txts = set()
            for b in el.query_selector_all("button"):
                try:
                    btn_txts.add((b.inner_text() or "").strip().lower())
                except Exception:
                    pass
            if {"yes", "no"} <= btn_txts:
                return self._click_yesno_and_verify(page, el, choice_text)
        return False

    # ---- submit confirmation: in-place success panel, NO url change ----
    _SUCCESS_RE = re.compile(
        r"your application has been received|application has been received|"
        r"thank you for your interest|application\s+success",
        re.IGNORECASE,
    )

    def submit_succeeded(self, page) -> bool:
        """True if the Ashby in-place success panel is showing. Ashby does NOT navigate on
        submit — the form is replaced by a "Success / ... Your application has been received"
        panel. Confirm by BOTH signals: the Submit button is gone AND the success text is
        present. Read-only; never raises."""
        # (a) submit control disappeared (the form is gone)
        submit_present = False
        try:
            for b in page.query_selector_all("button, input[type='submit']"):
                try:
                    t = (b.inner_text() or b.get_attribute("value") or "").strip().lower()
                except Exception:
                    continue
                if "submit application" in t or t == "submit":
                    if b.is_visible():
                        submit_present = True
                        break
        except Exception:
            pass
        if submit_present:
            return False
        # (b) success text present
        try:
            body = page.inner_text("body") or ""
        except Exception:
            return False
        return bool(self._SUCCESS_RE.search(body))
