"""WorkdayAdapter.login — sign in BEFORE the apply wizard (root-cause fix).

Production used to inherit the no-op FormAdapterBase.login and rely on a persisted
session; when it lapsed the wizard opened on the Create-Account/Sign-In gate and
My Information rendered empty. login() now signs in first (mirrors the proven
workday_walk.py `ensure_signed_in`). Browser-free: a fake page records goto/fill/clicks
and serves configurable query_selector results keyed by selector substring.
"""
from apply_engine.adapters.base import FormAdapterBase
from apply_engine.adapters.workday import WorkdayAdapter


class _El:
    def __init__(self):
        self.filled = None

    def fill(self, v):
        self.filled = v


class _FakePage:
    """Minimal Playwright-page stand-in.

    `present` maps a selector-substring -> element to return from query_selector
    (first matching key wins). `url` is the current URL. Records every goto target,
    every fill, and every _wd_click aid (via the click_filter selector).
    """
    def __init__(self, url, present=None):
        self.url = url
        self._present = present or {}
        self.gotos = []
        self.clicks = []
        self.email_el = _El()
        self.password_el = _El()

    def goto(self, url, **k):
        self.gotos.append(url)
        # let the test advance the URL as navigation happens
        self.url = url

    def wait_for_timeout(self, *a, **k):
        pass

    def wait_for_selector(self, *a, **k):
        pass

    def query_selector(self, sel):
        if "data-automation-id='email'" in sel:
            return self.email_el if self._present.get("email") else None
        if "data-automation-id='password'" in sel:
            return self.password_el if self._present.get("password") else None
        if "CandidateHomePage" in sel:
            return object() if self._present.get("signed_in") else None
        if "click_filter" in sel:
            # _wd_click looks up the overlay by aria-label; record the aid and report absent
            # so it falls through to the direct-element branch.
            return None
        for key, el in self._present.items():
            if key in sel:
                return el
        return None

    def query_selector_all(self, sel):
        return []


def _click_recorder(page):
    def _wd_click(self, p, aid):
        page.clicks.append(aid)
        return True
    return _wd_click


# ---------------------------------------------------------------------------
# _careers_base — PURE URL derivation (the regression was here): the base MUST
# include the careers-site path segment, or userHome/login 404 on path-nested
# tenants like Illumina. No tenant/lang strings are hardcoded.
# ---------------------------------------------------------------------------
def test_careers_base_job_url_with_locale():
    """job URL with a locale segment -> base keeps scheme+host+locale+site, drops /job/..."""
    assert WorkdayAdapter._careers_base(
        "https://illumina.wd1.myworkdayjobs.com/en-US/illumina-careers/job/Some-Role_R123"
    ) == "https://illumina.wd1.myworkdayjobs.com/en-US/illumina-careers"


def test_careers_base_careers_landing_no_locale():
    """careers landing with no locale segment -> base is host + the single site segment."""
    assert WorkdayAdapter._careers_base(
        "https://illumina.wd1.myworkdayjobs.com/illumina-careers"
    ) == "https://illumina.wd1.myworkdayjobs.com/illumina-careers"


def test_careers_base_generic_tenant_not_hardcoded():
    """Generic tenant/lang (not illumina/en-US): same rule, keep up to the site segment."""
    assert WorkdayAdapter._careers_base(
        "https://acme.wd5.myworkdayjobs.com/fr-FR/acme-careers/job/Engineer_R9"
    ) == "https://acme.wd5.myworkdayjobs.com/fr-FR/acme-careers"


def test_careers_base_drops_anything_below_job():
    """A deeper job path (apply/details after the slug) still collapses to the site base."""
    assert WorkdayAdapter._careers_base(
        "https://x.wd1.myworkdayjobs.com/en-US/x-careers/job/Role_R1/apply/applyManually"
    ) == "https://x.wd1.myworkdayjobs.com/en-US/x-careers"


def test_careers_base_no_host_returns_empty():
    """No host -> empty string so login() bails instead of navigating to a bad URL."""
    assert WorkdayAdapter._careers_base("not-a-url") == ""
    assert WorkdayAdapter._careers_base("") == ""


def test_login_short_circuits_when_already_signed_in(monkeypatch):
    """CandidateHomePage present at the SITE-PATH /userHome -> return without touching /login,
    and end back on the original job URL so _apply_entry runs on the posting."""
    JOB = "https://illumina.wd1.myworkdayjobs.com/en-US/illumina-careers/job/123"
    page = _FakePage(JOB, present={"signed_in": True})
    a = WorkdayAdapter()
    monkeypatch.setattr(WorkdayAdapter, "_wd_click", _click_recorder(page))
    # creds would exist, but we must NOT get as far as needing them
    monkeypatch.setattr(WorkdayAdapter, "_tenant_creds",
                        lambda self, p: {"email": "x@y.com", "password": "pw"})

    a.login(page)

    # userHome is under the careers-site path, NOT the bare host
    assert "https://illumina.wd1.myworkdayjobs.com/en-US/illumina-careers/userHome" in page.gotos
    assert "https://illumina.wd1.myworkdayjobs.com/userHome" not in page.gotos  # never the bare-host form
    assert "/login" not in "".join(page.gotos)
    assert page.clicks == []                       # no sign-in click attempted
    assert page.url == JOB                          # ended back on the job posting
    assert page.gotos[-1] == JOB


def test_login_signs_in_when_not_signed_in(monkeypatch):
    """No CandidateHomePage -> goto SITE-PATH /login, fill email+password, click
    signInSubmitButton, then return to the original job URL."""
    JOB = "https://illumina.wd1.myworkdayjobs.com/en-US/illumina-careers/job/123"
    page = _FakePage(JOB, present={"signed_in": False, "email": True, "password": True})
    a = WorkdayAdapter()
    monkeypatch.setattr(WorkdayAdapter, "_wd_click", _click_recorder(page))
    monkeypatch.setattr(WorkdayAdapter, "_tenant_creds",
                        lambda self, p: {"email": "a@b.com", "password": "secret"})

    a.login(page)

    assert "https://illumina.wd1.myworkdayjobs.com/en-US/illumina-careers/userHome" in page.gotos
    assert "https://illumina.wd1.myworkdayjobs.com/en-US/illumina-careers/login" in page.gotos
    # NOT the bare-host forms (the regression)
    assert "https://illumina.wd1.myworkdayjobs.com/userHome" not in page.gotos
    assert "https://illumina.wd1.myworkdayjobs.com/login" not in page.gotos
    assert page.email_el.filled == "a@b.com"
    assert page.password_el.filled == "secret"
    assert page.clicks == ["signInSubmitButton"]   # signed in via the overlay-aware click
    assert page.url == JOB                          # ended back on the job posting
    assert page.gotos[-1] == JOB


def test_login_does_not_create_account_when_creds_missing(monkeypatch):
    """No stored creds -> return WITHOUT navigating to /login, filling, or clicking. login()
    is sign-in only; the in-wizard _handle_account_gate remains the (account-creating)
    fallback. Still returns the browser to the original job URL."""
    JOB = "https://acme.wd5.myworkdayjobs.com/en-US/acme-careers/job/9"
    page = _FakePage(JOB, present={"signed_in": False, "email": True, "password": True})
    a = WorkdayAdapter()
    monkeypatch.setattr(WorkdayAdapter, "_wd_click", _click_recorder(page))
    monkeypatch.setattr(WorkdayAdapter, "_tenant_creds", lambda self, p: None)

    # also prove it never reaches account-creation machinery
    called = {"create": False}
    monkeypatch.setattr(WorkdayAdapter, "_create_account",
                        lambda self, p, email: called.__setitem__("create", True))

    a.login(page)

    # hit the SITE-PATH userHome, then returned to the job — never /login, never bare host
    assert "https://acme.wd5.myworkdayjobs.com/en-US/acme-careers/userHome" in page.gotos
    assert all("/login" not in g for g in page.gotos)
    assert page.email_el.filled is None
    assert page.password_el.filled is None
    assert page.clicks == []
    assert called["create"] is False
    assert page.url == JOB                          # ended back on the job posting


def test_login_is_overridden_not_the_base_noop():
    """Regression: if login() reverts to the inherited FormAdapterBase no-op, the
    sign-in-before-apply fix is silently gone. Assert the adapter defines its own login."""
    assert WorkdayAdapter.login is not FormAdapterBase.login
    assert "login" in WorkdayAdapter.__dict__   # defined on the adapter class itself
