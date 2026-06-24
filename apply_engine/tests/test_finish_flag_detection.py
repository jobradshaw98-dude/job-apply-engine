"""The submit-confirm step must distinguish a BOT-FLAGGED submission (Ashby's "we couldn't
submit … submit again" banner — the click did NOT submit) from a generic could-not-confirm, so
the caller reports the explicit flagged_bot_detection outcome instead of a vague detection gap.
(feedback_ashby_flags_automated_submit; JOB-297 Baseten 2026-06-09.) No browser — a fake page."""
from apply_engine.finish import _confirm_submitted


class FakePage:
    def __init__(self, body, url="https://jobs.ashbyhq.com/x/app"):
        self._body = body
        self.url = url

    def wait_for_timeout(self, _ms):
        pass

    def inner_text(self, _sel):
        return self._body


def test_flag_banner_is_detected_as_flagged_not_confirmed():
    page = FakePage("Application\nWe couldn't submit your application. Your submission was "
                    "flagged. Please submit again.")
    confirmed, evidence, flagged = _confirm_submitted(page, page.url, timeout_ms=2000)
    assert confirmed is False
    assert flagged is True
    assert "flagged" in evidence.lower()


def test_confirmation_text_is_a_clean_confirm_not_flagged():
    page = FakePage("Thank you for applying! Your application has been submitted.",
                    url="https://boards.greenhouse.io/x/app")
    confirmed, evidence, flagged = _confirm_submitted(page, "https://boards.greenhouse.io/x/form",
                                                      timeout_ms=2000)
    assert confirmed is True
    assert flagged is False


def test_plain_unchanged_page_is_unconfirmed_but_not_flagged():
    page = FakePage("Still on the application form. First name. Last name.")
    confirmed, evidence, flagged = _confirm_submitted(page, page.url, timeout_ms=2000)
    assert confirmed is False
    assert flagged is False          # generic "couldn't confirm", NOT a bot-flag
