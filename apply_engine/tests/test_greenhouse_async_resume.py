"""Regression: modern job-boards.greenhouse.io renders the attached resume filename
ASYNCHRONOUSLY (~1.5s after change). The old _attach_resume used a single fixed 1200ms wait
that fired too early and reported a real upload as "did not attach" (live miss on Anthropic
JOB-210, 2026-06-08). The polling _wait_resume_filename_visible must catch the late render.
"""
from pathlib import Path

from apply_engine.browser import launch_profile
from apply_engine.adapters.greenhouse import GreenhouseAdapter


def _make_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "SAM_RIVERA_Resume.pdf"
    p.write_bytes(b"%PDF-1.4\n%fake\n")
    return p


def test_async_filename_render_is_caught_by_polling(fixture_server, tmp_path):
    """Direct-set path: filename renders at ~1500ms; the poll (3500ms budget) must return True."""
    a = GreenhouseAdapter()
    pdf = _make_pdf(tmp_path)
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_modern_async_resume.html")
        ok = a._attach_resume(page, str(pdf))
        assert ok is True
        assert "SAM_RIVERA_Resume" in page.inner_text("body")


def test_old_fixed_wait_would_have_missed_it(fixture_server, tmp_path):
    """Proves the bug mode: at 1200ms (the old fixed wait) the filename is NOT yet visible,
    so a single-check-at-1200ms would have returned False. The poll keeps going and succeeds."""
    a = GreenhouseAdapter()
    pdf = _make_pdf(tmp_path)
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/greenhouse_modern_async_resume.html")
        page.query_selector(a.resume_selector).set_input_files(str(pdf))
        page.wait_for_timeout(1200)
        # the old code checked exactly here and returned False:
        assert a._resume_filename_visible(page, pdf.name) is False
        # the new poll, given the remaining budget, catches the later render:
        assert a._wait_resume_filename_visible(page, pdf.name, timeout_ms=3000) is True
