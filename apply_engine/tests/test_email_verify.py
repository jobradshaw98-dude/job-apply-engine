"""Pure extraction of a Workday account-verification link from an email body. No IMAP."""
from apply_engine.email_verify import extract_verify_link


RESMED = "resmed.wd3.myworkdayjobs.com"


def test_extracts_verify_link_for_tenant():
    body = (
        "Welcome to ResMed Careers. Please verify your account:\n"
        "https://resmed.wd3.myworkdayjobs.com/en-US/ResMed_External_Careers/"
        "register/verifyEmail/abc123token\n"
        "If you did not request this, ignore."
    )
    link = extract_verify_link(body, RESMED)
    assert link == ("https://resmed.wd3.myworkdayjobs.com/en-US/ResMed_External_Careers/"
                    "register/verifyEmail/abc123token")


def test_prefers_verify_link_over_other_urls():
    body = (
        "Unsubscribe: https://resmed.wd3.myworkdayjobs.com/unsubscribe\n"
        "Careers home: https://resmed.wd3.myworkdayjobs.com/careers\n"
        "Verify: https://resmed.wd3.myworkdayjobs.com/register/verifyEmail/tok"
    )
    assert extract_verify_link(body, RESMED).endswith("/register/verifyEmail/tok")


def test_strips_trailing_punctuation():
    body = "Verify here: https://resmed.wd3.myworkdayjobs.com/register/verifyEmail/tok."
    assert extract_verify_link(body, RESMED).endswith("/verifyEmail/tok")


def test_html_href_body():
    body = '<a href="https://x.wd5.myworkdayjobs.com/register/verifyEmail/zzz">Verify</a>'
    assert extract_verify_link(body, "x.wd5.myworkdayjobs.com").endswith("/verifyEmail/zzz")


def test_no_matching_link_returns_none():
    assert extract_verify_link("no links here", RESMED) is None
    assert extract_verify_link("https://example.com/foo", RESMED) is None
    assert extract_verify_link("", RESMED) is None
