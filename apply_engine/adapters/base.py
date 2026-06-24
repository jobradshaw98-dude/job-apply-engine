"""Adapter contract + shared single-page form mechanics.

Each ATS adapter sets `text_fields` (answer key -> CSS selector) and a resume
selector; the base handles fill, work-auth detection (native <select> AND modern
React-Select widgets), answering, and field read-back. Adapters NEVER click submit.
"""
from dataclasses import dataclass
from typing import Protocol, Dict, List


@dataclass
class WorkAuthQuestion:
    """A work-auth/sponsorship question found on the form + how to answer it.

    kind == 'select'        -> native <select>; `selector` targets it.
    kind == 'react-select'  -> JS combobox; `control_index` is its position among
                               all .select__control nodes on the page.
    kind == 'button-yesno'  -> Yes/No <button> pair (Ashby); re-located by `label` text.
    kind == 'radio-yesno'   -> Yes/No <input type=radio> group (Lever cards[...]);
                               `selector` is the radio group's name= attribute.
    """
    label: str
    selector: str
    kind: str                 # 'select' | 'react-select' | 'button-yesno' | 'radio-yesno'
    control_index: int = -1


class Adapter(Protocol):
    name: str

    def login(self, page, profile_signed_in: bool) -> None: ...
    def fill(self, page, answers) -> Dict[str, str]: ...
    def find_work_auth_questions(self, page) -> List[WorkAuthQuestion]: ...
    def answer_no(self, page, q: WorkAuthQuestion) -> None: ...
    def answer_yes(self, page, q: WorkAuthQuestion) -> None: ...
    def read_back(self, page, keys: List[str]) -> Dict[str, str]: ...
    def go_to_review(self, page) -> None: ...


class NeedsHumanLogin(Exception):
    """Raised when authentication needs Sam (new account / verification / CAPTCHA)."""


class FormAdapterBase:
    """Shared single-page form mechanics. Never clicks submit."""
    name = "base"
    text_fields: Dict[str, str] = {}
    resume_selector: str = ""
    resume_attached_ok = None   # set during fill: True/False if a resume field was present
    work_auth_keywords = ("sponsor", "authorized to work", "authorization for employment",
                          "work authorization", "visa",
                          "citizen", "nationality", "eligible to work",
                          "immigration status")

    # ---- auth / navigation / fill ----
    def login(self, page, profile_signed_in: bool) -> None:
        return

    def fill_remaining(self, page, answers) -> Dict[str, str]:
        """After the core fields, fill any OTHER empty text-like input we can map to a
        known answer key (city/state/country/linkedin/github/portfolio/...). Best-effort;
        leaves anything unmappable for the completeness scan to surface."""
        from ..field_map import map_field
        added: Dict[str, str] = {}
        sel = ("input[type='text'], input[type='email'], input[type='tel'], "
               "input[type='url'], input:not([type])")
        for el in page.query_selector_all(sel):
            try:
                if (el.input_value() or "").strip():
                    continue  # already filled
            except Exception:
                continue
            # react-select internals (combobox input + Greenhouse's hidden 'requiredInput'
            # proxy in .select__container) are NOT free-text — el.fill types a filter string
            # that never selects an option. Match any [class*="select__"] ancestor. They are
            # driven separately by _fill_standard_react_selects (Country/State) / the picker.
            try:
                if el.evaluate("e => !!e.closest('[class*=\"select__\"]')"):
                    continue
            except Exception:
                pass
            label = ""
            eid = el.get_attribute("id")
            if eid:
                lab = page.query_selector(f"label[for='{eid}']")
                if lab:
                    label = (lab.inner_text() or "").strip()
            if not label:
                label = (el.get_attribute("aria-label")
                         or el.get_attribute("placeholder") or "")
            key = map_field(label, el.get_attribute("name") or "",
                            el.get_attribute("placeholder") or "")
            if not key:
                continue
            val = answers.get(key)
            if not val:
                continue
            try:
                el.fill(str(val))
                added[key] = str(val)
            except Exception:
                pass
        added.update(self._fill_standard_react_selects(page, answers))
        return added

    def _fill_standard_react_selects(self, page, answers) -> Dict[str, str]:
        """Drive standard LOCATION react-selects (modern Greenhouse renders Country + State as
        React-select, not native <select>). el.fill() is a no-op on react-select, so without
        this they stay blank and block submit. Country MUST be chosen before State — State
        shows 'No options' until Country = United States (live-verified on Greenhouse). Returns
        the keys actually set; safe no-op on forms with no react-selects.

        Options are keyed inconsistently across boards: some State selects list full names
        ('California'), others list 2-LETTER CODES ('CA'); Country can be 'United States' or
        'US'. A single guess silently leaves the field blank → required-field validation blocks
        submit (this was the silent blocker on the first Oura auto-submit: 'California' typed
        into a code-keyed select → 'No options' → State blank). So we try a candidate LIST in
        order and stop at the first that registers — full name first, then abbreviation."""
        added: Dict[str, str] = {}
        try:
            if not page.query_selector(".select__control"):
                return added
        except Exception:
            return added
        country = answers.get("country")
        if country and self._react_needs_value(page, "country"):
            # Some forms key Country by code too — try the full name then common codes.
            cand = [str(country)]
            for c in ("United States", "US", "USA"):
                if c not in cand:
                    cand.append(c)
            chosen = self._select_react_first_match(page, "country", cand)
            if chosen:
                added["country"] = chosen
                try:
                    page.wait_for_timeout(400)  # let the State options cascade populate
                except Exception:
                    pass
        # State select may list full names OR 2-letter codes — try full name then abbreviation.
        state_full = answers.get("state_full")
        state_abbr = answers.get("state")
        cand = [str(v) for v in (state_full, state_abbr) if v]
        if cand and self._react_needs_value(page, "state"):
            chosen = self._select_react_first_match(page, "state", cand)
            if chosen:
                added["state"] = chosen
        # Location / City react-select (modern Greenhouse "Location (City)"). Unlike Country/
        # State this is an ASYNC autocomplete: options load from a geo service after you type, so
        # the option-wait must tolerate a short network delay (longer timeout, below). Options
        # come back QUALIFIED ("Austin, TX, USA" / "Austin, California, United States"), so
        # a bare city may miss the typeahead — try the bare term first (fast path) then the two
        # qualified forms. _react_control_for_label already EXCLUDES work-auth/EEO controls, so a
        # 'location'/'city' label match can never hijack "authorized to work in this country".
        city = answers.get("city")
        if city:
            state_disp = answers.get("state") or answers.get("state_full")
            state_full_disp = answers.get("state_full")
            cand = [str(city)]
            for extra in (f"{city}, {state_disp}" if state_disp else None,
                          f"{city}, {state_full_disp}" if state_full_disp else None):
                if extra and extra not in cand:
                    cand.append(extra)
            # Try the explicit "Location (City)" label first, then a bare "city" label. Either
            # only fires if a matching react-select exists and is still unfilled.
            for label_substr in ("location", "city"):
                if not self._react_needs_value(page, label_substr):
                    continue
                chosen = self._select_react_first_match(
                    page, label_substr, cand, option_wait_ms=6000)
                if chosen:
                    added["city"] = str(city)  # record the canonical profile value
                    break
        return added

    def _select_react_first_match(self, page, label_substr: str, candidates,
                                  option_wait_ms: int = 3000) -> str:
        """Try each candidate value against the react-select for `label_substr`, in order, and
        return the FIRST one that provably registers (select_react_by_label reads back the
        chosen chip and returns True only on a verified match). Returns '' if none registered.

        This is the abbreviation-fallback: e.g. State candidates ['California','CA'] — a
        full-name miss ('No options' on a code-keyed select) cleanly falls through to 'CA'.
        Each attempt re-opens the same control; _pick_react_select closes the menu (Escape) on
        a miss, so the next candidate starts from a clean closed state.

        `option_wait_ms` is how long to wait for options to render after typing — bumped above
        the 3s default for ASYNC autocompletes (Location/City fetches options from a geo
        service), left at the default for the synchronous Country/State selects."""
        for value in candidates:
            value = str(value).strip()
            if not value:
                continue
            if self.select_react_by_label(page, label_substr, value,
                                          option_wait_ms=option_wait_ms):
                return value
        return ""

    def _react_needs_value(self, page, label_substr: str) -> bool:
        """True if a react-select for label_substr exists and has no chosen value yet."""
        ctrl = self._react_control_for_label(page, label_substr)
        if ctrl is None:
            return False
        try:
            if ctrl.query_selector(".select__single-value, .select__multi-value"):
                return False  # already chosen
        except Exception:
            pass
        return True

    def go_to_form(self, page) -> None:
        """If the application fields aren't on the current page (many ATSs show the
        posting first), click an 'Apply' control to reveal/navigate to the form.
        No-op when the form is already present (e.g. Greenhouse embeds it)."""
        first = next(iter(self.text_fields.values()), None)
        if first and page.query_selector(first):
            return  # form already present
        for el in page.query_selector_all("a, button"):
            txt = (el.inner_text() or "").strip().lower()
            if txt.startswith("apply"):
                try:
                    el.click()
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                return

    def fill(self, page, answers) -> Dict[str, str]:
        intended: Dict[str, str] = {}
        for key, sel in self.text_fields.items():
            val = answers.get(key)
            if not val:
                continue
            # Fill via a LOCATOR (.first), NOT a cached ElementHandle. A modern React form
            # (job-boards.greenhouse.io / Anthropic) re-renders asynchronously — a handle grabbed
            # with query_selector then .fill()'d can detach in between ("Element is not attached to
            # the DOM", the JOB-210 submit crash, 2026-06-09). A locator re-RESOLVES the element at
            # action time and auto-waits, so a re-render can't stale it. `.first` keeps the original
            # first-match semantics (so comma/fallback selectors don't trip Playwright strict mode).
            if not page.query_selector(sel):
                continue  # field absent on this form — skip (verify_fields flags a real miss)
            try:
                page.locator(sel).first.fill(str(val))
                intended[key] = str(val)
            except Exception:  # noqa: BLE001 — a transient fill failure leaves the field for verify
                continue
        if self.resume_selector and getattr(answers, "resume_pdf", None):
            self.resume_attached_ok = self._attach_resume(page, str(answers.resume_pdf))
        return intended

    # ---- resume attachment (React-safe) ----
    def _attach_resume(self, page, pdf_path) -> bool:
        """Attach the resume and return True only if the filename is VISIBLY registered.

        Two mechanisms, each gated on the filename actually RENDERING on the page (the positive
        signal that the ATS/React registered the upload — input.files alone is not enough):
          1. Direct set_input_files on the hidden <input type=file>. On MODERN
             job-boards.greenhouse.io this registers and the filename renders within ~0.6-2s
             (verified live on Anthropic 2026-06-08). We try it FIRST because it never opens a
             native file-chooser dialog — the chooser path can leave an OS dialog pending and
             wedge the run, which is exactly what made a real upload report 'did not attach'.
          2. Visible 'Attach' button -> native file chooser (React-safe path). Needed for the
             classic React forms (Ōura 2026-06-07) where set_input_files sets input.files but
             React's form state never registers it, so the filename never renders. Only reached
             when path 1 didn't register, so it never fires on forms where the direct set works.
        Success is judged by the filename appearing on the page, POLLED (not a single fixed wait
        fired too early — that early check was the 'did not attach' bug on modern greenhouse)."""
        from pathlib import Path
        fname = Path(pdf_path).name
        # 1) direct set on the hidden input — registers on modern greenhouse; gated on the
        #    filename rendering, so on a classic-React form that ignores it we fall through to (2).
        try:
            rel = page.query_selector(self.resume_selector)
            if rel:
                rel.set_input_files(pdf_path)
                if self._wait_resume_filename_visible(page, fname, timeout_ms=3500):
                    return True
                # Filename did not render. On a PLAIN <input type=file> (classic Greenhouse and
                # most ATSs) the file IS registered — input.files is the ground truth and the page
                # simply never echoes the name. The OLD body-text-only check false-blocked those as
                # 'did not attach' (the production needs_input pileup). Trust input.files there.
                # React dropzones that ignore set_input_files DO leave input.files set too, so this
                # would false-positive them — but those always expose a visible Attach/Upload
                # control, so we gate the trust on its ABSENCE and let path (2) handle React forms.
                if self._resume_input_has_file(page) and self._find_resume_attach_button(page) is None:
                    return True
        except Exception:
            pass
        # 2) visible Attach button -> native file chooser (classic React forms)
        try:
            btn = self._find_resume_attach_button(page)
            if btn is not None:
                with page.expect_file_chooser(timeout=6000) as fc:
                    btn.click()
                fc.value.set_files(pdf_path)
                if self._wait_resume_filename_visible(page, fname, timeout_ms=6000):
                    return True
        except Exception:
            pass
        return False

    def _wait_resume_filename_visible(self, page, fname: str, timeout_ms: int = 6000,
                                      interval_ms: int = 300) -> bool:
        """Poll _resume_filename_visible up to timeout_ms. Modern ATS forms render the attached
        filename asynchronously (~0.6-2s on job-boards.greenhouse.io), so the old single
        fixed-wait check fired too early and reported a real upload as 'did not attach'."""
        import time
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            if self._resume_filename_visible(page, fname):
                return True
            if time.monotonic() >= deadline:
                return False
            try:
                page.wait_for_timeout(interval_ms)
            except Exception:
                time.sleep(interval_ms / 1000.0)

    def _find_resume_attach_button(self, page):
        """The visible 'Attach' button inside the resume field's container (the hidden
        #resume input is present at fill time; walk up to the container that holds it and
        find a button whose text matches Attach/Upload). Returns an element handle or None."""
        inp = page.query_selector(self.resume_selector)
        if not inp:
            return None
        try:
            handle = inp.evaluate_handle(
                "e => { let c = e.closest('div');"
                " for (let i=0;i<6 && c;i++){"
                "  const b=[...c.querySelectorAll('button,label')]"
                "   .find(x => /attach|upload/i.test(x.textContent||''));"
                "  if (b) return b; c=c.parentElement; }"
                " return null; }")
            return handle.as_element() if handle else None
        except Exception:
            return None

    def _resume_filename_visible(self, page, fname: str) -> bool:
        """True if the attached filename (or its stem) shows up in the page body — the
        positive signal that the ATS actually registered the upload. Case-insensitive:
        ATS confirmations vary the case of the rendered filename."""
        from pathlib import Path
        try:
            body = (page.inner_text("body") or "").lower()
        except Exception:
            return False
        fn = fname.lower()
        return fn in body or Path(fn).stem in body

    def _resume_input_has_file(self, page) -> bool:
        """DOM ground truth: a file is actually set on the resume <input type=file>
        (input.files.length > 0). This is what survives whether or not the page echoes
        the filename, and is the reliable signal on plain (non-React) file inputs."""
        try:
            n = page.eval_on_selector(self.resume_selector, "e => (e.files && e.files.length) || 0")
            return bool(n and int(n) > 0)
        except Exception:
            return False

    # ---- work-auth detection (native <select> + React-Select) ----
    def _matches(self, text: str) -> bool:
        low = (text or "").lower()
        return bool(text) and any(k in low for k in self.work_auth_keywords)

    def find_work_auth_questions(self, page) -> List[WorkAuthQuestion]:
        return self._find_yesno_questions(page, self._matches)

    def find_office_commitment_questions(self, page) -> List[WorkAuthQuestion]:
        """Find office / in-person / hybrid / RTO commitment questions across the SAME widget
        kinds as work-auth (react-select / native select / Yes-No button group / radio group),
        filtered by the office-commitment classifier instead of the work-auth keyword match.

        Reuses the exact widget-enumeration + the verified `answer_yes` driver — no new
        clicking primitive. The orchestrator drives a Yes on each and HALTs on a failed set,
        with the SAME verified-set discipline as the work-auth guard (never a phantom Yes).
        EEO / relocation / work-auth labels are excluded inside the classifier itself, so they
        can never be returned here even though they share the Yes/No widget shape."""
        from ..office_commitment import (classify_office_commitment,
                                         OfficeCommitmentDecision)

        def _is_office(text: str) -> bool:
            return (classify_office_commitment(text)
                    == OfficeCommitmentDecision.AUTO_YES)

        return self._find_yesno_questions(page, _is_office)

    def find_screening_yesno_questions(self, page) -> List[WorkAuthQuestion]:
        """Find CUSTOM Yes/No screening qualifiers rendered as a BUTTON-GROUP or RADIO pair — the
        widget kinds the orchestrator's custom-select extractors (extract_select_questions /
        extract_react_select_questions) do NOT cover. (Native <select> and react-select Yes/No
        screens are already routed through resolve_with_screening in the custom-select paths, so
        they are deliberately excluded here to avoid double-handling.)

        These are the "3+ years experience?", "designed LLM apps?", "proficient in Python?" screens
        the engine used to punt wholesale to Sam. The orchestrator runs each through the
        conservative `resolve_with_screening` classifier (capabilities-grounded; EEO/sensitive/
        negation excluded INSIDE the classifier) and drives the answer via the SAME verified
        `answer_yes`/`answer_no` path. Work-auth and office-commitment questions are excluded (their
        own guards own them); EEO/relocation share this shape but the classifier escalates them, so
        they are never auto-answered even though returned here."""
        from ..work_auth import classify_work_auth, WorkAuthDecision
        from ..office_commitment import (classify_office_commitment,
                                         OfficeCommitmentDecision)

        def _custom_yesno(text: str) -> bool:
            if not text:
                return False
            if classify_work_auth(text) != WorkAuthDecision.UNRELATED:
                return False            # work-auth guard owns it
            if classify_office_commitment(text) == OfficeCommitmentDecision.AUTO_YES:
                return False            # office-commitment guard owns it
            return True

        return [q for q in self._find_yesno_questions(page, _custom_yesno)
                if q.kind in ("button-yesno", "radio-yesno")]

    def _find_yesno_questions(self, page, predicate) -> List[WorkAuthQuestion]:
        """Enumerate Yes/No-shaped questions whose label satisfies `predicate`, across every
        widget kind the engine can drive: react-select, native <select>, Ashby button-group,
        and Lever radio-group. Shared by the work-auth and office-commitment guards so both use
        the identical, verified answer paths (`answer_yes`/`answer_no`) — only the label filter
        differs. `predicate(label) -> bool`."""
        found: List[WorkAuthQuestion] = []

        # React-Select widgets (modern Greenhouse/Lever/Ashby). Index = DOM order.
        controls = page.query_selector_all(".select__control")
        for idx, ctrl in enumerate(controls):
            wrapper = ctrl.evaluate_handle(
                "e => e.closest('.field-wrapper, [class*=field], form > div')")
            el = wrapper.as_element() if wrapper else None
            label_el = el.query_selector("label") if el else None
            text = (label_el.inner_text().strip() if label_el else "")
            if predicate(text):
                found.append(WorkAuthQuestion(label=text, selector="",
                                              kind="react-select", control_index=idx))

        # Native <select> (fixtures / legacy forms).
        for lbl in page.query_selector_all("label[for]"):
            text = (lbl.inner_text() or "").strip()
            if not predicate(text):
                continue
            target = lbl.get_attribute("for")
            if not target:
                continue
            # Ashby/modern forms use UUID/leading-digit ids ("1235eadb-..."), which are INVALID
            # as `#id` CSS selectors (an id selector can't start with a digit) and throw a
            # SyntaxError that would abort the whole sweep. Use an [id="..."] attribute selector,
            # valid for any id value, and stay defensive so one odd control can't kill enumeration.
            sel = f'[id="{target}"]'
            try:
                el = page.query_selector(sel)
                if el and el.evaluate("e => e.tagName.toLowerCase()") == "select":
                    found.append(WorkAuthQuestion(label=text, selector=sel, kind="select"))
            except Exception:
                continue

        # Yes/No <button> pair (Ashby and similar). Answered by clicking the button by text.
        for lbl in page.query_selector_all("label"):
            text = (lbl.inner_text() or "").strip()
            if not predicate(text):
                continue
            block = lbl.evaluate_handle("e => e.closest('div, fieldset, li')")
            el = block.as_element() if block else None
            if not el:
                continue
            btn_txts = {(b.inner_text() or "").strip().lower()
                        for b in el.query_selector_all("button")}
            if {"yes", "no"} <= btn_txts:
                found.append(WorkAuthQuestion(label=text, selector="", kind="button-yesno"))

        # Radio Yes/No groups (live Lever cards[...]). Group radios by name; a group whose
        # options include both Yes and No, and whose recovered question text satisfies the
        # predicate, is answered by checking the right radio. The question text is NOT a
        # <label for=> here — it lives in the card's .application-label (recovered below).
        radio_groups: Dict[str, list] = {}
        for r in page.query_selector_all("input[type='radio']"):
            nm = r.get_attribute("name")
            if nm:
                radio_groups.setdefault(nm, []).append(r)
        for nm, radios in radio_groups.items():
            vals = {(r.get_attribute("value") or "").strip().lower() for r in radios}
            if not ({"yes", "no"} <= vals):
                continue
            label = self._radio_group_label(page, radios[0])
            if predicate(label):
                found.append(WorkAuthQuestion(label=label, selector=nm, kind="radio-yesno"))
        return found

    def _radio_group_label(self, page, radio) -> str:
        """Recover the question text for a radio group. Lever puts it in the card's
        `.application-label`, not a `<label for=>`, so reuse the questions module's
        name-group label recovery. Returns '' (never the sentinel 'field') so an
        unlabelled group is never mistaken for a work-auth question."""
        try:
            from ..questions import _name_group_label
            lab = _name_group_label(page, radio)
            return "" if lab == "field" else lab
        except Exception:
            return ""

    # ---- answering ----
    # Every answer path returns a VERIFIED bool: True only when the value provably
    # registered on the widget. A void/None return let the orchestrator record a work-auth
    # answer that never actually got set, which `drop_answered` would then scrub from the
    # missing set — a blank required work-auth field passing as ready_to_submit (the one
    # unrecoverable wrong-answer field). Callers MUST gate on the return.
    def answer_no(self, page, q: WorkAuthQuestion) -> bool:
        return self._answer(page, q, "No", native_value="no")

    def answer_yes(self, page, q: WorkAuthQuestion) -> bool:
        return self._answer(page, q, "Yes", native_value="yes")

    def _answer(self, page, q: WorkAuthQuestion, choice_text: str, native_value: str) -> bool:
        if q.kind == "select":
            try:
                page.select_option(q.selector, native_value)
            except Exception:
                return False
            try:
                el = page.query_selector(q.selector)
                return bool(el) and (el.input_value() or "").strip().lower() == native_value
            except Exception:
                return False
        if q.kind == "button-yesno":
            # re-locate the question's block by label text, click the Yes/No button.
            for lbl in page.query_selector_all("label"):
                if (lbl.inner_text() or "").strip() == q.label:
                    block = lbl.evaluate_handle("e => e.closest('div, fieldset, li')")
                    el = block.as_element() if block else None
                    if not el:
                        return False
                    for b in el.query_selector_all("button"):
                        if (b.inner_text() or "").strip().lower() == choice_text.lower():
                            try:
                                b.click()
                                return True
                            except Exception:
                                return False
                    return False
            return False
        if q.kind == "radio-yesno":
            # check the radio in the named group whose value matches Yes/No. The group name
            # holds brackets (cards[uuid][fieldN]) — valid inside a double-quoted attr selector.
            for r in page.query_selector_all(f'input[type="radio"][name="{q.selector}"]'):
                if (r.get_attribute("value") or "").strip().lower() == choice_text.lower():
                    try:
                        r.check()
                    except Exception:
                        try:
                            r.click()
                        except Exception:
                            return False
                    try:
                        return bool(r.is_checked())
                    except Exception:
                        return False
            return False
        # react-select: drive + VERIFY via the shared picker (reads back the chosen chip).
        controls = page.query_selector_all(".select__control")
        if q.control_index < 0 or q.control_index >= len(controls):
            return False
        return self._pick_react_select(page, controls[q.control_index], choice_text)

    # ---- React-select driving (modern Greenhouse/Ashby/Lever) ----
    # Modern boards (job-boards.greenhouse.io) render State AND every Yes/No screening
    # question as React-select widgets (.select__control), NOT native <select>. el.fill /
    # native select_option do nothing on them, so those fields stayed blank and blocked
    # submit. These helpers open the widget, type to filter (searchable selects like State),
    # and click the matching .select__option.
    def _pick_react_select(self, page, control_el, value: str,
                           option_wait_ms: int = 3000) -> bool:
        """Open one .select__control, choose the option matching `value`, and return True ONLY
        if the selection provably registered (the control's chosen-value chip reads back as
        `value`). Never trusts the click — a click that didn't fire React's onClick, or a fuzzy
        mis-match, would otherwise be reported as answered. Typing into the input filters
        searchable selects (State); match is exact, then startswith, then (for values longer
        than 3 chars only) contains — a bare 'no'/'yes' must never contains-match inside another
        option ('Minnesota', 'not requiring sponsorship').

        `option_wait_ms` is the max wait for `.select__option` to appear after typing. The 3s
        default is right for synchronous selects (Country/State); ASYNC autocompletes (Location/
        City fetch options from a geo service) need longer — and a "No options" notice seen
        before the fetch returns is a TRANSIENT loading state, not a real miss. So for the async
        path we re-check the no-options notice only AFTER waiting for options, never fast-miss on
        the first transient empty menu."""
        val = (value or "").strip()
        if not val:
            return False
        try:
            control_el.scroll_into_view_if_needed()
            control_el.click()
            page.wait_for_timeout(300)
        except Exception:
            return False
        inp = control_el.query_selector("input")
        if inp:
            try:
                # Clear any residual filter text first. The multi-candidate loop re-opens the
                # SAME control after a miss; a prior candidate's typed text ('California', 'USA')
                # lingers in the input and would concatenate with the next candidate ('CA' ->
                # 'CaliforniaCA'), guaranteeing 'No options' on every retry. Select-all+delete
                # leaves the control empty regardless of how much was typed before.
                inp.click()
                inp.press("Control+A")
                inp.press("Delete")
                inp.type(val, delay=20)
                page.wait_for_timeout(400)
            except Exception:
                pass
        # Distinguish "menu open but NO options" from "options present, none matched". The
        # former is the abbreviation-fallback signal: a code-keyed select typed with a full
        # name (or vice-versa) renders a `.select__menu-notice` ('No options') and zero
        # `.select__option`. Treat that as a deliberate miss — close cleanly and return False
        # fast so _select_react_first_match advances to the next candidate, instead of waiting
        # out the wait_for_selector below every time.
        #
        # BUT for an async autocomplete (option_wait_ms bumped above the 3s default), a "No
        # options" notice at this point may just mean the geo fetch hasn't returned yet — fast-
        # missing would discard a candidate that would have matched. So we only fast-miss when
        # using the synchronous default; for the async path we fall through and wait for options.
        if option_wait_ms <= 3000 and self._react_menu_has_no_options(page):
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return False
        try:
            page.wait_for_selector(".select__option", timeout=option_wait_ms)
        except Exception:
            pass
        # Async path: after the wait, if the menu is still showing only the empty notice, the
        # fetch genuinely returned nothing for this candidate — close cleanly so the candidate
        # loop advances to the next (qualified) form.
        if option_wait_ms > 3000 and self._react_menu_has_no_options(page):
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return False
        opts = page.query_selector_all(".select__option")
        low = val.lower()
        matchers = [lambda t: t == low, lambda t: t.startswith(low)]
        if len(low) > 3:
            matchers.append(lambda t: low in t)
        clicked = False
        for match in matchers:
            for o in opts:
                try:
                    if match((o.inner_text() or "").strip().lower()):
                        o.click()
                        clicked = True
                        break
                except Exception:
                    continue
            if clicked:
                break
        if not clicked:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return False
        # VERIFY: read back the chosen value chip and confirm it IS the intended value.
        try:
            page.wait_for_timeout(150)
        except Exception:
            pass
        sel = " ".join(self._react_selected_text(control_el).lower().split())
        return bool(sel) and (sel == low or sel.startswith(low))

    def _react_menu_has_no_options(self, page) -> bool:
        """True when an open react-select menu is showing the empty state: a `.select__menu-notice`
        ('No options') is present and there are zero `.select__option` nodes. This is the explicit
        signal that the typed candidate doesn't exist in this select (e.g. a full name typed into a
        code-keyed State select), so the multi-candidate loop should fall through to the next value.
        Returns False on any error (then the normal option-scan path runs)."""
        try:
            notice = page.query_selector(".select__menu-notice")
            opts = page.query_selector_all(".select__option")
            return bool(notice) and len(opts) == 0
        except Exception:
            return False

    def _react_selected_text(self, control_el) -> str:
        """The value a react-select currently shows as chosen: the .select__single-value chip
        text, else a data-value attribute (test doubles / some variants). '' if none."""
        try:
            sv = control_el.query_selector(".select__single-value")
            if sv:
                t = (sv.inner_text() or "").strip()
                if t:
                    return t
        except Exception:
            pass
        try:
            dv = control_el.get_attribute("data-value")
            if dv and dv.strip():
                return dv.strip()
        except Exception:
            pass
        return ""

    def _react_control_for_label(self, page, label_substr: str):
        """Find the .select__control belonging to the field whose label contains
        label_substr (case-insensitive). Returns an element handle or None.

        EXCLUDES work-auth and EEO/demographic controls: this helper drives location
        (Country/State) and custom questions by label substring, and 'country'/'state' would
        otherwise match a work-auth label like 'authorized to work in this country' (the real
        Country control may sit later in the DOM) — corrupting a work-auth answer. Work-auth is
        owned by find_work_auth_questions; EEO is left for Sam."""
        sub = (label_substr or "").strip().lower()
        if not sub:
            return None
        from ..work_auth import classify_work_auth, WorkAuthDecision
        try:
            from ..questions import _is_eeo
        except Exception:
            def _is_eeo(_n, _l):
                return False
        for lbl in page.query_selector_all("label"):
            try:
                text = (lbl.inner_text() or "").strip()
                if sub not in text.lower():
                    continue
                if classify_work_auth(text) != WorkAuthDecision.UNRELATED:
                    continue
                if _is_eeo("", text):
                    continue
                h = lbl.evaluate_handle(
                    "e => { let c = e.closest('div');"
                    " for (let i=0;i<6 && c;i++){"
                    "  const s = c.querySelector('.select__control');"
                    "  if (s) return s; c = c.parentElement; } return null; }")
                el = h.as_element() if h else None
                if el:
                    return el
            except Exception:
                continue
        return None

    def select_react_by_label(self, page, label_substr: str, value: str,
                              option_wait_ms: int = 3000) -> bool:
        """Public: answer a React-select field/question identified by its label.

        `option_wait_ms` is forwarded to _pick_react_select for async-autocomplete fields
        (Location/City) that load options from a network call after typing."""
        ctrl = self._react_control_for_label(page, label_substr)
        if ctrl is None:
            return False
        return self._pick_react_select(page, ctrl, value, option_wait_ms=option_wait_ms)

    # ---- read-back / staging ----
    def read_back(self, page, keys: List[str]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for key in keys:
            sel = self.text_fields.get(key)
            if not sel:
                continue
            el = page.query_selector(sel)
            if el:
                out[key] = el.input_value()
        return out

    def go_to_review(self, page) -> None:
        return  # single-page form: filled form IS the review. Never submit.

    # ---- Phase 0: live-form MODEL (read-only; never fills, never submits) ----
    def enumerate_fields(self, page):
        """Read EVERY field on the live form into a FormSpec (Phase 0 — design doc §8.4 / G1+G7).

        This is the "read the whole form" capability the engine lacks today: today field discovery
        is purpose-built per signal (completeness.unfilled_required = unfilled-required only; the
        adapter finders cover only work-auth/screening classes). enumerate_fields enumerates ALL
        fields with, per field: key/label, required, widget_kind, doc_kind (for uploads), and
        stated length constraints (form_spec.scrape_constraints).

        ADDITIVE / READ-ONLY (Phase 0 hard rule): this NEVER fills, clicks, or mutates the page or
        the manifest. It only reads. It REUSES the existing primitives as building blocks — it does
        NOT reinvent label/required/widget detection:
          * completeness.label_for / _is_required         (text/select/checkbox/radio)
          * completeness._react_wrapper_label / _react_select_required  (react-select)
          * questions._name_group_label / _name_group_required         (Lever id-less cards)
          * form_spec.field_helper_text + scrape_constraints           (length limits)

        Overridable per adapter when an ATS needs custom enumeration; the base covers the common
        single-page shapes (Greenhouse/Lever/Ashby fixtures + live).
        """
        from ..form_spec import FormSpec, FieldSpec, scrape_constraints, field_helper_text
        from ..completeness import (_is_required, _react_wrapper_label,
                                    _react_select_required)

        spec = FormSpec(ats=self.name)
        seen_keys = set()
        radio_names_done = set()

        def _add(fs: FieldSpec):
            if fs.key in seen_keys:
                return
            seen_keys.add(fs.key)
            spec.fields.append(fs)

        def _key_for(el, label: str) -> str:
            eid = el.get_attribute("id")
            if eid:
                return eid
            nm = el.get_attribute("name")
            if nm:
                return nm
            from ..completeness import _norm
            return _norm(label) or "field"

        # ---- native inputs / textareas / selects, in DOM order ----
        for el in page.query_selector_all("input, textarea, select"):
            try:
                t = (el.get_attribute("type") or "").lower()
                if t in ("hidden", "submit", "button", "reset"):
                    continue
                # react-select internals (combobox input + Greenhouse's hidden requiredInput proxy
                # in .select__container) are NOT standalone fields — the react-select pass owns them.
                try:
                    if el.evaluate("e => !!e.closest('[class*=\"select__\"]')"):
                        continue
                except Exception:
                    pass
                tag = el.evaluate("e => e.tagName.toLowerCase()")
                # File inputs are routinely HIDDEN behind a visible "Upload File"/"Attach" button
                # (Ashby, modern Greenhouse) — the hidden <input type=file> IS the load-bearing
                # field for G7 doc detection, so never skip a file input on visibility. Everything
                # else must be visible to count as a real form field.
                if t != "file" and not self._safe_visible(el):
                    continue

                if tag == "select":
                    kind = "native_select"
                elif tag == "textarea":
                    kind = "textarea"
                elif t == "file":
                    kind = "file"
                elif t == "checkbox":
                    kind = "checkbox"
                elif t == "radio":
                    kind = "radio"
                else:
                    kind = "text"

                if kind == "radio":
                    # one FieldSpec per radio GROUP (keyed by name), recovered question label.
                    nm = el.get_attribute("name") or ""
                    if not nm or nm in radio_names_done:
                        continue
                    radio_names_done.add(nm)
                    label = self._enum_radio_label(page, el)
                    req = self._enum_radio_required(page, el)
                    _add(FieldSpec(key=nm or _key_for(el, label), label=label,
                                   required=req, widget_kind="radio",
                                   selector=f'input[type="radio"][name="{nm}"]' if nm else ""))
                    continue

                label = self._enum_label(page, el)
                required = bool(_is_required(page, el))
                doc_kind = ""
                constraints = {}
                if kind == "file":
                    doc_kind = self._enum_doc_kind(el, label)
                if kind in ("text", "textarea"):
                    helper = ""
                    placeholder = el.get_attribute("placeholder") or ""
                    try:
                        helper = field_helper_text(page, el)
                    except Exception:
                        helper = ""
                    constraints = scrape_constraints(
                        helper_text=helper, placeholder=placeholder,
                        maxlength=el.get_attribute("maxlength"))
                sel = self._enum_selector(el)
                _add(FieldSpec(key=_key_for(el, label), label=label, required=required,
                               widget_kind=kind, doc_kind=doc_kind,
                               constraints=constraints, selector=sel))
            except Exception:
                continue

        # ---- react-select controls (modern Greenhouse/Ashby/Lever) ----
        try:
            controls = page.query_selector_all(".select__control")
        except Exception:
            controls = []
        for ctrl in controls:
            try:
                if not ctrl.is_visible():
                    continue
                label = _react_wrapper_label(ctrl) or "field"
                required = bool(_react_select_required(ctrl))
                qid = ""
                inp = ctrl.query_selector("input")
                if inp:
                    qid = inp.get_attribute("id") or ""
                from ..completeness import _norm
                key = qid or (_norm(label) or "reactselect")
                _add(FieldSpec(key=key, label=label, required=required,
                               widget_kind="react_select",
                               selector=f'[id="{qid}"]' if qid else ""))
            except Exception:
                continue

        # ---- non-react role=combobox (rare typeahead not backed by .select__control) ----
        try:
            for cb in page.query_selector_all('[role="combobox"]'):
                try:
                    if cb.evaluate("e => !!e.closest('[class*=\"select__\"]')"):
                        continue  # already covered by the react-select pass
                    if not cb.is_visible():
                        continue
                    label = self._enum_label(page, cb)
                    key = self._enum_selector(cb) or label
                    _add(FieldSpec(key=key, label=label,
                                   required=bool(_is_required(page, cb)),
                                   widget_kind="combobox",
                                   selector=self._enum_selector(cb)))
                except Exception:
                    continue
        except Exception:
            pass

        spec.has_resume_field = any(f.widget_kind == "file" and f.doc_kind == "resume"
                                    for f in spec.fields)
        spec.has_cover_field = any(f.widget_kind == "file" and f.doc_kind == "cover"
                                   for f in spec.fields)
        return spec

    # ---- enumeration helpers (read-only; reuse completeness/questions primitives) ----
    def _enum_label(self, page, el) -> str:
        """Label for a field, reusing completeness.label_for, then recovering id-less Lever card
        labels via questions._name_group_label (the same recovery the extractors use)."""
        from ..completeness import label_for
        label = label_for(page, el)
        name = el.get_attribute("name") or ""
        if not label or label in ("field", name) or label.lower() == "type your response":
            try:
                from ..questions import _name_group_label
                recovered = _name_group_label(page, el)
                if recovered and recovered != "field":
                    label = recovered
            except Exception:
                pass
        return label or "field"

    def _enum_radio_label(self, page, radio) -> str:
        """Question label for a radio group — Lever puts it in the card's .application-label, not a
        <label for=>. Reuse the questions module recovery; '' -> 'field' fallback."""
        try:
            from ..questions import _name_group_label
            lab = _name_group_label(page, radio)
            if lab and lab != "field":
                return lab
        except Exception:
            pass
        from ..completeness import label_for
        return label_for(page, radio) or "field"

    def _enum_radio_required(self, page, radio) -> bool:
        from ..completeness import _is_required
        if _is_required(page, radio):
            return True
        try:
            from ..questions import _name_group_required
            return bool(_name_group_required(page, radio))
        except Exception:
            return False

    def _enum_doc_kind(self, el, label: str) -> str:
        """Classify an upload field as resume / cover / other. The adapter's own resume_selector is
        the strongest signal; otherwise classify by the label/id/name text."""
        # 1) the adapter's known resume input.
        try:
            if self.resume_selector:
                rid = (self.resume_selector or "").lstrip("#")
                eid = el.get_attribute("id") or ""
                if eid and (eid == rid or self.resume_selector == f"#{eid}"):
                    return "resume"
        except Exception:
            pass
        blob = " ".join(x for x in (label or "",
                                    el.get_attribute("id") or "",
                                    el.get_attribute("name") or "",
                                    el.get_attribute("aria-label") or "") if x).lower()
        if any(k in blob for k in ("resume", "cv", "résumé", "curriculum")):
            return "resume"
        if "cover" in blob or "coverletter" in blob.replace(" ", ""):
            return "cover"
        return "other"

    def _enum_selector(self, el) -> str:
        """Best-effort CSS selector to a field: #id (attribute form for digit/UUID ids), else
        [name="..."], else ''. Informational in Phase 0."""
        eid = el.get_attribute("id")
        if eid:
            return f'[id="{eid}"]'
        nm = el.get_attribute("name")
        if nm:
            return f'[name="{nm}"]'
        return ""

    def _safe_visible(self, el) -> bool:
        try:
            return el.is_visible()
        except Exception:
            return False
