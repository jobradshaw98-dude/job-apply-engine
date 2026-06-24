# -*- coding: utf-8 -*-
"""Workday adapter.

Two layers live here on purpose:

1. LEGACY single-page layer (kept for the existing fixture test): `text_fields` +
   `resume_selector`, with `fill` / `read_back` / `find_work_auth_questions` inherited
   from FormAdapterBase. This exercises `tests/fixtures/workday_form.html`. Do not remove
   the `text_fields` map below — the fixture's selectors depend on it.

2. MULTI-STEP layer (the real Workday wizard): `multi_step = True` + `stage_application`,
   ported faithfully from the proven prototype (`aria/tmp/workday_walk.py`) which drove a
   live Illumina application end-to-end to the Review brink and STOPPED. It uses the
   reusable widget helpers in `apply_engine.wd_widgets`. It NEVER submits, refuses any
   control whose text contains "submit", and verifies the staged work-auth answer by
   reading the rendered Review page.

All real data comes from `answers` / `profile` / `job` — no hardcoded paths or creds.
"""
from datetime import date

from .base import FormAdapterBase
from .. import wd_widgets as W
from ..work_auth import classify_work_auth, WorkAuthDecision
from ..choice_gen import make_resolver


def _match_tenant_creds(data: dict, host: str):
    """Pick the credentials whose tenant-host key matches `host` (each Workday employer is a
    separate tenant = separate account). Substring-both-ways so an apply-flow subdomain
    variant still matches. Returns the creds dict or None. PURE (no I/O)."""
    host = (host or "").lower()
    if not host:
        return None
    for key, val in (data or {}).items():
        k = (key or "").lower()
        if k and (k == host or k in host or host in k):
            return val
    return None


def _generate_password(length: int = 16) -> str:
    """A strong password meeting Workday's rules (≥8, upper+lower+digit+special). Built with
    `secrets`; guarantees one of each class then shuffles. PURE-ish (uses secrets, no I/O)."""
    import secrets
    import string
    specials = "!@#$%^&*-_=+"
    pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, specials]
    chars = [secrets.choice(p) for p in pools]  # one guaranteed from each class
    allchars = "".join(pools)
    chars += [secrets.choice(allchars) for _ in range(max(length, 12) - len(chars))]
    # shuffle without Random() — secrets-driven Fisher-Yates
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


def _merge_creds(existing: dict, host: str, email: str, password: str) -> dict:
    """Return `existing` with the tenant `host` set to {email, password}. PURE (no I/O).
    No host -> returned unchanged so we never write a bad key."""
    if not host:
        return existing
    out = dict(existing or {})
    out[host] = {"email": email, "password": password}
    return out


class WorkdayAdapter(FormAdapterBase):
    name = "workday"
    multi_step = True

    # set per-run in stage_application; (question, options) -> Choice, or None to escalate
    _choose = None
    _gate_note = ""   # set by _create_account when a tenant needs email verification

    # --- legacy single-page map (fixture-backed; keep exactly) ---
    text_fields = {
        "first_name": "[data-automation-id='legalNameSection_firstName']",
        "last_name": "[data-automation-id='legalNameSection_lastName']",
        "email": "[data-automation-id='email']",
        "phone": "[data-automation-id='phone-number']",
    }
    resume_selector = "[data-automation-id='file-upload-input-ref']"

    # ------------------------------------------------------------------
    # Sign-in BEFORE the apply wizard (root-cause fix)
    # ------------------------------------------------------------------
    @staticmethod
    def _careers_base(url: str) -> str:
        """Derive the Workday CAREERS-SITE base — INCLUDING the careers-site path segment —
        from a job/careers URL. PURE (no I/O).

        Workday auth (userHome / login) lives UNDER the careers-site path, not at the bare
        host. Real shapes:
          - job URL:        .../en-US/illumina-careers/job/<slug>  -> .../en-US/illumina-careers
          - careers landing: .../illumina-careers                   -> .../illumina-careers
        Rule: keep scheme+host + the path up to AND INCLUDING the careers-site segment,
        dropping `/job/...` and anything deeper. The careers-site segment is the LAST path
        segment before `/job/` (or the last segment overall if there's no `/job/`). An
        optional leading locale segment (e.g. `en-US`) is preserved as-is — we don't
        special-case any tenant or language.

        Earlier this method didn't exist: login() built the base as scheme://host only via
        urlunparse, so it navigated to `<host>/userHome` and `<host>/login`, both of which
        404 on path-nested tenants (Illumina) and stranded the browser on a dead page before
        the apply wizard ran. That regression is what this fixes.
        """
        from urllib.parse import urlparse
        parts = urlparse(url or "")
        scheme = parts.scheme or "https"
        host = parts.netloc
        if not host:
            return ""
        segs = [s for s in (parts.path or "").split("/") if s]
        # drop `/job/<...>` and anything after it -> the careers-site path is what precedes it
        if "job" in segs:
            segs = segs[:segs.index("job")]
        base_path = "/".join(segs)
        return f"{scheme}://{host}" + (f"/{base_path}" if base_path else "")

    def login(self, page, profile_signed_in: bool = True) -> None:
        """Sign in to THIS Workday tenant before the apply wizard opens.

        Why this exists: the base `FormAdapterBase.login` is a no-op, so production
        used to enter the apply flow relying on a persisted browser session. When that
        session lapsed, the wizard opened on the 7-step Create-Account/Sign-In gate and
        "My Information" rendered empty downstream. Signing in here first means apply-entry
        always runs from the clean signed-in 6-step flow (proven in the workday_walk.py
        prototype's `ensure_signed_in`).

        Mechanics (mirrors the prototype): capture the ORIGINAL job URL first, derive the
        careers-site base via `_careers_base` (which KEEPS the site-path segment — bare host
        404s on path-nested tenants like Illumina), goto `<base>/userHome`; if a
        CandidateHomePage is present we're already in. Otherwise goto `<base>/login`, fill the
        email/password fields, and click Sign In via the overlay-aware `_wd_click`. LOGIN
        ONLY — never creates an account here.

        On EVERY exit path (signed in, already signed in, creds missing, sign-in failed, any
        exception) the browser is navigated BACK to the original job URL before returning, so
        `_apply_entry` runs on the job posting (it clicks `adventureButton`), not on userHome.
        If creds are missing or sign-in fails this still returns without raising; the in-wizard
        `_handle_account_gate` remains the fallback.
        """
        try:
            original = page.url
        except Exception:
            return
        base = self._careers_base(original)
        if not base:
            return  # no usable host -> nothing to do (already on whatever page we're on)

        def _return_to_job():
            # Always land back on the job posting so _apply_entry runs on the right page.
            try:
                if page.url != original:
                    page.goto(original, wait_until="domcontentloaded")
                    page.wait_for_timeout(1500)
            except Exception:
                pass

        try:
            page.goto(base + "/userHome", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
        except Exception:
            _return_to_job()
            return
        if page.query_selector("[data-automation-id='CandidateHomePage']"):
            _return_to_job()        # already signed in — back to the job posting
            return

        creds = self._tenant_creds(page)
        if not (creds and creds.get("email") and creds.get("password")):
            _return_to_job()        # no creds -> in-wizard gate is the fallback
            return

        try:
            page.goto(base + "/login", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
        except Exception:
            _return_to_job()
            return
        em = page.query_selector("[data-automation-id='email']")
        pw = page.query_selector("[data-automation-id='password']")
        if not (em and pw):
            _return_to_job()        # not the sign-in form -> defer to the in-wizard gate
            return
        try:
            em.fill(creds["email"])
            pw.fill(creds["password"])
        except Exception:
            _return_to_job()
            return
        # buttons sit under a click_filter overlay that eats direct clicks -> _wd_click
        self._wd_click(page, "signInSubmitButton")
        page.wait_for_timeout(5000)
        # success/failure is non-fatal: the wizard's _handle_account_gate is the fallback.
        _return_to_job()            # back to the job posting for _apply_entry

    # ------------------------------------------------------------------
    # Multi-step live wizard
    # ------------------------------------------------------------------
    def stage_application(self, page, answers, profile: dict, job: dict,
                          answer_fn=None, audit_fn=None, facts: str = "") -> dict:
        """Walk the live Workday apply wizard to the Review brink and STOP.

        Returns a dict:
          {reached, submitted (always False), work_auth_verified, escalations,
           filled_steps, error}

        answer_fn/audit_fn/facts (opt-in) bind a gated grounded-choice picker used on
        custom application-question dropdowns: a supported option is auto-selected, an
        unsupported/judgment-call question DECLINEs and is escalated to the user. Without
        them every custom question is escalated (the safe default).
        """
        self._choose = make_resolver(facts, answer_fn, audit_fn)
        result = {
            "reached": "", "submitted": False, "work_auth_verified": None,
            "escalations": [], "filled_steps": [], "error": None,
        }
        try:
            # Sign in FIRST so apply-entry always runs signed-in (clean 6-step flow).
            # Non-fatal if it can't: _handle_account_gate below is the in-wizard fallback.
            self.login(page, profile_signed_in=True)
            self._apply_entry(page, result)
            # walk up to 10 steps; stop at Review or when a step won't advance
            for _ in range(10):
                W.settle(page)
                step = W.active_step(page)
                if "review" in step:
                    result["reached"] = "review"
                    result["work_auth_verified"] = self._verify_work_auth(page)
                    # confirm a Submit control exists (we are truly at the brink) but
                    # NEVER click it.
                    self._assert_not_submitting(page)
                    return result
                # in-wizard account/sign-in gate (session didn't carry into the apply flow)
                if ("create account" in step or "sign in" in step
                        or page.query_selector("[data-automation-id='createAccountSubmitButton']")):
                    if not self._handle_account_gate(page, profile, job):
                        result["reached"] = step or "account-gate"
                        result["error"] = self._gate_note or (
                            "stuck at Workday account/sign-in gate "
                            "(creds missing or sign-in failed) — needs the user")
                        result["escalations"].append(
                            {"field": "account", "q": "Workday account gate",
                             "reason": result["error"]})
                        return result
                    continue  # signed in -> re-loop onto My Information
                self._fill_step(page, answers, profile, job, step, result)
                if not W.advance(page):
                    result["reached"] = step or "unknown"
                    result["error"] = f"stuck on step {step!r} (did not advance)"
                    return result
            result["reached"] = W.active_step(page) or "unknown"
            result["error"] = "exhausted step budget without reaching review"
            return result
        except Exception as e:  # noqa: BLE001 — surface, never half-submit
            result["error"] = repr(e)[:200]
            if not result["reached"]:
                result["reached"] = W.active_step(page) or "error"
            return result

    # ---- apply entry ----
    def _apply_entry(self, page, result) -> None:
        """Click Apply (adventureButton); resume an in-progress draft via 'Continue
        Application', else 'Apply Manually'. Then wait for the first form to render."""
        try:
            page.wait_for_selector("[data-automation-id='adventureButton']", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(1200)
        self._wd_click(page, "adventureButton")
        page.wait_for_timeout(3000)

        # PREFER resuming a real in-progress draft FOR THIS REQ (skips the account gate +
        # avoids a duplicate application). Only DRAFT-SPECIFIC controls qualify — a bare
        # "Continue" can be the account-create gate's own button, which (when signed in)
        # would wrongly fork into account creation, so it is excluded. The draft controls
        # ("Continue Application" / "Manage Application" / "Resume Application") only appear
        # in the Apply popup when a draft exists for this req. Fresh req -> Apply Manually.
        resumed = False
        draft_seen = False  # a draft control was PRESENT (even if its click failed)
        for name in ("Continue Application", "Manage Application", "Resume Application"):
            try:
                loc = page.get_by_role("button", name=name, exact=False).first
                if not loc.count():
                    loc = page.get_by_text(name, exact=True).first
                if loc.count():
                    draft_seen = True
                    loc.click(timeout=4000)
                    page.wait_for_timeout(4500)
                    resumed = True
                    break
            except Exception:
                continue
        # Only start a FRESH application when no draft control was even present. If a draft
        # existed but we couldn't resume it (click threw), do NOT fall through to Apply
        # Manually — that would create a duplicate application over the existing draft.
        if not resumed and not draft_seen and page.query_selector("[data-automation-id='applyManually']"):
            self._wd_click(page, "applyManually")
            page.wait_for_timeout(4500)

    # ---- in-wizard account / sign-in gate ----
    def _tenant_creds(self, page):
        """Load this Workday tenant's stored creds (applicant_credentials.json, keyed by
        tenant host). Returns the creds dict or None."""
        import json
        from urllib.parse import urlparse
        from .. import config
        try:
            data = json.loads(
                (config.PKG_DIR / "applicant_credentials.json").read_text(encoding="utf-8"))
        except Exception:
            return None
        return _match_tenant_creds(data, urlparse(page.url).hostname or "")

    def _handle_account_gate(self, page, profile: dict = None, job: dict = None) -> bool:
        """The apply wizard opens NOT signed in on a 'Create Account / Sign In' gate. Stored
        creds -> sign in; else CREATE an account autonomously. If the tenant then requires
        EMAIL VERIFICATION (ResMed), read the verify link from the user's inbox, click it, and
        re-enter. Returns True if the gate cleared, False to escalate."""
        if not page.query_selector("[data-automation-id='createAccountSubmitButton'], "
                                   "[data-automation-id='signInSubmitButton']"):
            return True  # not on the gate
        creds = self._tenant_creds(page)
        if creds and creds.get("email") and creds.get("password"):
            if self._sign_in(page, creds):
                return True
        else:
            if self._create_account(page, (profile or {}).get("email", "")):
                return True
            creds = self._tenant_creds(page)  # _create_account stored the new creds
        # both paths may land on a 'verify your account' page -> verify via the inbox
        if self._verify_email_required(page):
            return self._verify_via_email(page, job, creds)
        return False

    def _verify_via_email(self, page, job, creds) -> bool:
        """Tenant requires email verification: poll the inbox for the Workday verify link,
        click it, re-enter the apply flow, and sign in with the (now-verified) creds."""
        from ..email_verify import fetch_verify_link
        from urllib.parse import urlparse
        host = urlparse(page.url).hostname or ""
        link = None
        for _ in range(8):                      # wait up to ~80s for the email to arrive
            link = fetch_verify_link(host)
            if link:
                break
            page.wait_for_timeout(10000)
        if not link:
            self._gate_note = ("account created but email verification needed and no verify "
                               "email was found in the inbox — needs the user")
            return False
        page.goto(link, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        url = (job or {}).get("url") or ""
        if url:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            self._apply_entry(page, {"escalations": [], "filled_steps": []})
        W.settle(page)
        if page.query_selector("[data-automation-id='signInSubmitButton'], "
                               "[data-automation-id='createAccountSubmitButton']"):
            c = creds or self._tenant_creds(page)
            if c and c.get("email") and c.get("password"):
                self._sign_in(page, c)
        return self._advanced_off_gate(page)

    def _advanced_off_gate(self, page) -> bool:
        """Truly advanced = we're on a REAL wizard step (non-empty progress-bar step that
        isn't the gate). A bare Sign In / 'verify your account' page has NO progress bar
        (active_step == '') — that is NOT advancing, so don't false-pass on it."""
        W.settle(page)
        step = W.active_step(page)
        if not step:
            return False
        return ("create account" not in step) and ("sign in" not in step)

    @staticmethod
    def _verify_email_required(page) -> bool:
        """ResMed-style tenants redirect to Sign In with 'please verify your account' after
        Create Account — the account exists but is unusable until the emailed link is clicked."""
        try:
            body = (page.inner_text("body") or "").lower()
        except Exception:
            return False
        return "verify your account" in body or "an email has been sent" in body

    def _sign_in(self, page, creds) -> bool:
        """Switch to the Sign In form, enter creds, submit. Workday buttons sit under a
        click_filter overlay that eats direct clicks -> always use _wd_click."""
        if page.query_selector("[data-automation-id='signInLink']"):
            self._wd_click(page, "signInLink")
            page.wait_for_timeout(1800)
        em = page.query_selector("[data-automation-id='email']")
        pw = page.query_selector("[data-automation-id='password']")
        if not (em and pw):
            return False
        em.fill(creds["email"])
        pw.fill(creds["password"])
        if not self._wd_click(page, "signInSubmitButton"):
            return False
        page.wait_for_timeout(5000)
        return self._advanced_off_gate(page)

    def _create_account(self, page, email: str) -> bool:
        """Create a Workday account for this tenant: ensure the Create Account form, fill
        email + a generated password (twice), submit. NEVER fill the beecatcher honeypot. On
        success (the wizard advances off the gate) store the creds for re-use. Returns False
        (escalate) if no email, the account already exists, or email-verification blocks
        advance — the run then halts safely, never submitting."""
        if not email:
            return False
        # ensure the Create Account variant (it's the default; switch from Sign In if needed)
        if (page.query_selector("[data-automation-id='createAccountLink']")
                and not page.query_selector("[data-automation-id='verifyPassword']")):
            self._wd_click(page, "createAccountLink")
            page.wait_for_timeout(1800)
        em = page.query_selector("[data-automation-id='email']")
        pw = page.query_selector("[data-automation-id='password']")
        vp = page.query_selector("[data-automation-id='verifyPassword']")
        if not (em and pw and vp):
            return False
        password = _generate_password()
        em.fill(email)            # targeted ids only — beecatcher honeypot is NEVER touched
        pw.fill(password)
        vp.fill(password)
        # some tenants (e.g. ResMed) require a consent checkbox ("I agree to be contacted")
        # before Create Account will proceed. Check VISIBLE checkboxes only — the beecatcher
        # honeypot is hidden, so a visibility filter excludes it.
        for cb in page.query_selector_all("input[type='checkbox']"):
            try:
                if cb.is_visible() and not cb.is_checked():
                    cb.check(timeout=2500)
            except Exception:
                pass
        if not self._wd_click(page, "createAccountSubmitButton"):
            return False
        page.wait_for_timeout(5000)
        W.settle(page)
        if self._advanced_off_gate(page):
            self._store_creds(page, email, password)
            return True
        # account may have been created but this tenant requires email verification first
        # (ResMed): the account now exists, so store the creds for a later (verified) sign-in,
        # and escalate with a clear reason. We never submit, but we also can't proceed.
        if self._verify_email_required(page):
            self._store_creds(page, email, password)
            self._gate_note = ("account created but tenant requires EMAIL VERIFICATION "
                               "before continuing — click the Workday verify link in the user's "
                               "inbox (no Gmail-verify in the engine yet), then re-run")
        return False

    def _store_creds(self, page, email: str, password: str) -> None:
        """Persist newly-created tenant creds to applicant_credentials.json (gitignored)."""
        import json
        from urllib.parse import urlparse
        from .. import config
        path = config.PKG_DIR / "applicant_credentials.json"
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        host = urlparse(page.url).hostname or ""
        merged = _merge_creds(existing, host, email, password)
        try:
            path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _wd_click(self, page, aid: str) -> bool:
        """Click a Workday control by data-automation-id, preferring the click_filter
        overlay (it intercepts pointer events on the underlying button)."""
        ov = page.query_selector(f"[data-automation-id='click_filter'][aria-label='{aid}']")
        if ov and W.is_visible(ov):
            try:
                ov.click(timeout=4000)
                return True
            except Exception:
                pass
        el = page.query_selector(f"[data-automation-id='{aid}']")
        if el and W.is_visible(el):
            try:
                el.click(timeout=4000)
                return True
            except Exception:
                box = el.bounding_box()
                if box:
                    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    return True
        return False

    # ---- per-step dispatch ----
    def _fill_step(self, page, answers, profile, job, step, result) -> None:
        did = False
        if page.query_selector("button[id^='primaryQuestionnaire--']"):
            self._application_questions(page, result)
            did = True
        if page.query_selector(
                "[id^='personalInfoUS--'], [id^='selfIdentifiedDisabilityData--'], "
                "[id='termsAndConditions--acceptTermsAndAgreements']"):
            self._disclosures(page, profile)
            did = True
        if self._my_information(page, profile):
            did = True
        if self._upload_resume(page, answers):
            did = True
        if did:
            label = step or f"step{len(result['filled_steps']) + 1}"
            if label not in result["filled_steps"]:
                result["filled_steps"].append(label)

    # ---- My Information ----
    def _my_information(self, page, profile: dict) -> bool:
        digits = "".join(c for c in str(profile.get("phone", "")) if c.isdigit())[-10:]
        text_by_id = {
            "name--legalName--firstName": profile.get("first_name", ""),
            "name--legalName--lastName": profile.get("last_name", ""),
            "address--addressLine1": profile.get("address_line1", ""),
            "address--city": profile.get("city", ""),
            "address--postalCode": profile.get("postal_code", ""),
            "phoneNumber--phoneNumber": digits,
        }
        touched = False
        for fid, val in text_by_id.items():
            if not val:
                continue
            el = page.query_selector(W.esc_id(fid))
            if el and W.is_visible(el):
                try:
                    if not (el.input_value() or "").strip():
                        el.fill(str(val))
                        touched = True
                except Exception:
                    pass

        # button single-selects (State, Phone Device Type)
        state = profile.get("state_full") or profile.get("state", "")
        for bid, opt in (("address--countryRegion", state),
                         ("phoneNumber--phoneType", "Mobile")):
            if opt and page.query_selector(W.esc_id(bid)):
                if W.button_select(page, bid, opt):
                    touched = True

        # "How did you hear" cascade — Illumina-specific values, so best-effort only:
        # try the profile-configured picks if present, else a generic single pick. NOT a
        # hard requirement (no escalation if it doesn't take).
        picks = profile.get("how_did_you_hear_picks")
        if not picks:
            hdyh = profile.get("how_did_you_hear")
            picks = [hdyh] if hdyh else None
        if picks and page.query_selector(W.esc_id("source--source")):
            try:
                W.multiselect(page, "source--source", "", picks)
                touched = True
            except Exception:
                pass

        # Country Phone Code (long virtualized multiselect; default is US)
        if page.query_selector(W.esc_id("phoneNumber--countryPhoneCode")):
            try:
                if W.multiselect(page, "phoneNumber--countryPhoneCode",
                                 "United States of America",
                                 ["United States of America (+1)"]):
                    touched = True
            except Exception:
                pass
        return touched

    # ---- My Experience (resume upload) ----
    def _upload_resume(self, page, answers) -> bool:
        fu = page.query_selector(
            "[data-automation-id='file-upload-input-ref'], input[type='file']")
        resume = getattr(answers, "resume_pdf", None)
        if fu and resume and str(resume):
            try:
                # don't re-upload if a file is already attached this step
                already = fu.evaluate("e => !!(e.files && e.files.length > 0)")
            except Exception:
                already = False
            if not already:
                try:
                    fu.set_input_files(str(resume))
                    page.wait_for_timeout(800)
                    return True
                except Exception:
                    return False
        return False

    # ---- Application Questions ----
    def _application_questions(self, page, result) -> None:
        """Answer primaryQuestionnaire--* button dropdowns. Work-auth via the classifier
        (answer to clear the screen, no red flags); 'worked here' -> No; anything else is
        an ESCALATION (recorded so the caller/LLM can answer) with a best-effort pick."""
        btns = page.query_selector_all("button[id^='primaryQuestionnaire--']")
        for b in btns:
            fid = b.get_attribute("id")
            qtext = self._question_text(b)
            low = qtext.lower()
            decision = classify_work_auth(qtext)

            if decision == WorkAuthDecision.SPONSORSHIP_NO:
                W.button_select(page, fid, "No")
            elif decision == WorkAuthDecision.AUTHORIZED_YES:
                W.button_select(page, fid, "Yes")
            elif decision == WorkAuthDecision.AUTHORIZED_NO_SPONSORSHIP:
                # combined "authorized without sponsorship?" -> affirmative no-red-flag.
                # Try the explicit affirmative phrasings first, then plain Yes. NO trailing
                # "No": if none of these match we ESCALATE rather than click a negative
                # option (which here means "I require sponsorship" — a red flag).
                picked = self._pick_first(page, fid,
                                          ["authorized", "do not", "without", "Yes"])
                if not picked:
                    result["escalations"].append({
                        "field": fid, "q": qtext,
                        "reason": ("combined authorized-without-sponsorship question: no "
                                   "affirmative no-red-flag option matched — needs the user"),
                    })
            elif decision == WorkAuthDecision.HALT:
                # citizenship / free-text visa — never guess. Record + best-effort skip.
                result["escalations"].append(
                    {"field": fid, "q": qtext, "reason": "work-auth HALT (needs the user)"})
            elif any(k in low for k in
                     ("currently working", "ever worked", "previously worked", "worked at")):
                W.button_select(page, fid, "No")
            else:
                # custom (non-work-auth, non-"worked-here") question.
                self._resolve_custom_question(page, fid, qtext, result)

    def _resolve_custom_question(self, page, fid, qtext, result) -> None:
        """Answer a custom dropdown question with a GROUNDED, gated pick — or escalate.

        With no resolver configured (no LLM hooks), escalate (leave the field BLANK):
        Workday blocks advance on a required blank, the honest signal that the run can't
        reach review, so it can never be reported ready_to_submit on a guess.

        With a resolver, read the dropdown's offered options and let the gated picker
        choose a supported one. Only an `answered` choice that actually selects is treated
        as resolved; DECLINE (no factual basis — e.g. a familiarity self-assessment), a
        gate BLOCK, or a failed select all escalate to the user, never a guess.
        """
        if self._choose is None:
            result["escalations"].append({
                "field": fid, "q": qtext,
                "reason": "custom question — needs career-agent answer (left blank)",
            })
            return
        options = W.read_options(page, fid)
        choice = self._choose(qtext, options)
        if choice.status == "answered" and choice.value and W.button_select(page, fid, choice.value):
            return  # grounded option selected — resolved, not escalated
        result["escalations"].append({
            "field": fid, "q": qtext,
            "reason": f"custom question ({choice.status}): "
                      f"{choice.reason or 'unresolved'} — left for the user",
        })

    def _pick_first(self, page, button_id, prefs) -> bool:
        for p in prefs:
            if W.button_select(page, button_id, p):
                return True
        return False

    @staticmethod
    def _question_text(button_el) -> str:
        cont = button_el.evaluate_handle("e=>e.closest('[data-automation-id^=\"formField-\"]')")
        ce = cont.as_element() if cont else None
        if not ce:
            return ""
        txt = (ce.inner_text() or "")
        return txt.replace("Select One Required", "").replace("Select One", "").strip()

    # ---- Voluntary Disclosures / Self Identify ----
    def _disclosures(self, page, profile: dict) -> None:
        eeo = [
            ("personalInfoUS--gender", ["wish to answer", "Decline", "not to"]),
            ("personalInfoUS--veteranStatus", ["not a protected", "not a veteran",
                                               "wish to answer", "Decline"]),
            ("personalInfoUS--ethnicity", ["wish to answer", "Decline", "not to"]),
            ("personalInfoUS--hispanicOrLatino", ["wish to answer", "Decline", "No"]),
            ("selfIdentifiedDisabilityData--disabilityForm", ["English"]),  # language
        ]
        for bid, prefs in eeo:
            if page.query_selector(W.esc_id(bid)):
                self._pick_first(page, bid, prefs)

        # Disability self-id is a CHECKBOX GROUP — check "I do not want to answer",
        # uncheck any other box in the group. NEVER touch this group elsewhere.
        for cb in page.query_selector_all("input[type='checkbox'][id$='-disabilityStatus']"):
            try:
                cid = cb.get_attribute("id")
                lab = ""
                if cid:
                    l = page.query_selector(f"label[for='{cid}']")
                    lab = (l.inner_text() if l else "") or ""
                want = "do not want to answer" in lab.lower() or "not to answer" in lab.lower()
                if want and not cb.is_checked():
                    cb.check(timeout=2500)
                elif not want and cb.is_checked():
                    cb.uncheck(timeout=2500)
            except Exception:
                pass

        # Sign the self-id form: Name (e-signature) + today's date
        full_name = profile.get("full_name") or (
            f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip())
        nm = page.query_selector(W.esc_id("selfIdentifiedDisabilityData--name"))
        if nm and W.is_visible(nm) and full_name:
            try:
                nm.click()
                nm.fill("")
                nm.fill(full_name)
                if not (nm.input_value() or "").strip():
                    nm.press_sequentially(full_name, delay=40)
            except Exception:
                pass
        today = date.today()
        for aid, val in (("dateSectionMonth-input", f"{today.month:02d}"),
                         ("dateSectionDay-input", f"{today.day:02d}"),
                         ("dateSectionYear-input", str(today.year))):
            el = page.query_selector(f"[id$='{aid}']")
            if el and W.is_visible(el) and not (el.input_value() or "").strip():
                try:
                    el.fill(val)
                except Exception:
                    pass

        # terms/consent checkboxes ONLY (NEVER the disabilityStatus group)
        for cb in page.query_selector_all("input[type='checkbox']"):
            try:
                cbid = (cb.get_attribute("id") or "")
                if cbid.endswith("-disabilityStatus"):
                    continue
                req = cb.get_attribute("aria-required") or cb.get_attribute("required")
                if (req or "terms" in cbid.lower() or "accept" in cbid.lower()) \
                        and not cb.is_checked():
                    cb.check(timeout=2500)
            except Exception:
                pass

    # ---- Review verification ----
    def _verify_work_auth(self, page):
        """Read the rendered Review page around the sponsorship / work-authorization text
        and return that snippet. The caller runs `verify_sponsorship_answer` (word-boundary
        predicate) on it — this method only EXTRACTS the answer text, it does not judge it.

        Returns the question+answer snippet, or None if no such text is present (which the
        caller treats as ambiguous -> needs_sam, the fail-safe). The window is kept tight
        (the question line plus its immediate answer) so unrelated downstream copy can't leak
        a stray token into the predicate."""
        try:
            body = page.inner_text("body") or ""
        except Exception:
            return None
        low = body.lower()
        for kw in ("sponsorship", "work authorization", "authorized to work"):
            i = low.find(kw)
            if i >= 0:
                return body[i:i + 200].replace("\n", " | ").strip()
        return None

    @staticmethod
    def _assert_not_submitting(page) -> None:
        """Sanity check: a Submit control should exist at Review (we're at the brink) —
        but this method exists to make the no-submit contract explicit. We never click it."""
        # intentionally a no-op beyond detection; advance() already refuses 'submit'.
        _ = page.query_selector("[data-automation-id='pageFooterSubmitButton'], "
                                "button:has-text('Submit')")
