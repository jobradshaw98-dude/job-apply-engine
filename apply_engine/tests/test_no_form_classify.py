"""_classify_no_form: precise reason for a zero-fields halt (replaces the vague
'no fillable fields' message that conflated closed / homepage / unsupported / miss)."""
from apply_engine.orchestrator import _classify_no_form
from apply_engine.ats_detect import AtsKind


class FakePage:
    def __init__(self, body="", n_links=0):
        self._body = body
        self._n = n_links

    def inner_text(self, sel):
        return self._body

    def eval_on_selector_all(self, sel, js):
        return self._n


def _label(page, url, kind):
    return _classify_no_form(page, url, kind)[0]


def test_closed_posting_by_text():
    p = FakePage(body="This job is no longer open. Create a job alert.")
    assert _label(p, "https://job-boards.greenhouse.io/anthropic/jobs/5107121008", AtsKind.GREENHOUSE) == "closed"


def test_closed_posting_by_listing_redirect():
    p = FakePage(body="376 jobs", n_links=40)  # redirected to the full board
    assert _label(p, "https://job-boards.greenhouse.io/anthropic/jobs/123", AtsKind.GREENHOUSE) == "closed"


def test_homepage_no_link():
    p = FakePage(body="Open roles at Together AI")
    assert _label(p, "https://www.together.ai/careers", AtsKind.UNKNOWN) == "homepage_no_link"


def test_unsupported_ats_real_job_page():
    p = FakePage(body="Apply for this role at Terumo")
    assert _label(p, "https://www.terumoneuro.com/jobs/12872BR", AtsKind.UNKNOWN) == "unsupported_ats"
    assert _label(p, "https://jobs.jobvite.com/zodiac/job/o22yzfwV", AtsKind.UNKNOWN) == "unsupported_ats"


def test_supported_ats_miss_is_flagged():
    p = FakePage(body="Senior Engineer\nApply", n_links=2)  # not closed, supported ATS, no form
    assert _label(p, "https://job-boards.greenhouse.io/x/jobs/999", AtsKind.GREENHOUSE) == "form_not_found"
