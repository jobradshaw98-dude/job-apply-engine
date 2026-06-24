"""Captcha pre-check regression guard (PART 2 of the staged-apply scale-up hardening).

detect_captcha must recognize BLOCKING captchas (hCaptcha, a VISIBLE reCAPTCHA challenge)
so the orchestrator/finish divert to a manual submit, while IGNORING the invisible reCAPTCHA
that Greenhouse/Ashby drop on every form (a bare hidden g-recaptcha-response textarea) — if
that tripped the check, nearly every application would be falsely diverted to the user.

Real-Playwright against local HTML fixtures, matching test_react_select_modern.py style."""
from apply_engine.browser import launch_profile
from apply_engine.captcha import detect_captcha


def test_detects_hcaptcha(fixture_server, tmp_path):
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/captcha_hcaptcha_form.html")
        assert detect_captcha(page) == "hcaptcha"


def test_detects_visible_recaptcha(fixture_server, tmp_path):
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/captcha_recaptcha_visible_form.html")
        assert detect_captcha(page) == "recaptcha_visible"


def test_invisible_recaptcha_is_not_a_blocker(fixture_server, tmp_path):
    """The bare hidden g-recaptcha-response textarea + 0-size iframe (background scoring) must
    return None — it submits without a human, so it must NEVER divert to manual."""
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/captcha_recaptcha_invisible_form.html")
        assert detect_captcha(page) is None


def test_clean_form_has_no_captcha(fixture_server, tmp_path):
    """A normal form (no captcha at all) returns None — no false positive that would block a
    clean auto-submit."""
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/ashby_modern_form.html")
        assert detect_captcha(page) is None


def test_recaptcha_v3_blocks_submit_but_not_staging(fixture_server, tmp_path):
    """reCAPTCHA v3 (api.js?render=<sitekey>, score-based) flags an automated submit. It must be a
    blocker on the SUBMIT phase only — staging (fill to brink) is still fine. Root cause of the
    Baseten/Ashby JOB-297 'submission was flagged' bot-block (2026-06-09)."""
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/captcha_recaptcha_v3_form.html")
        # staging path (default) does NOT divert — we still stage v3 forms to the brink
        assert detect_captcha(page) is None
        # submit path DOES divert — an automated submit would be bot-flagged
        assert detect_captcha(page, submit_phase=True) == "recaptcha_v3"
