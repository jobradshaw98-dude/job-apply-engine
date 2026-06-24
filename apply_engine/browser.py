"""Owns the Playwright persistent Chrome context pointed at the dedicated,
Google-signed bot profile. Non-headless so Google Password Manager autofills logins."""
from contextlib import contextmanager
from pathlib import Path
from playwright.sync_api import sync_playwright

from . import config


@contextmanager
def launch_profile(headless: bool = False, profile_dir: Path = None):
    profile_dir = Path(profile_dir or config.PROFILE_DIR)
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            channel="chrome",          # real Chrome, so Google profile + autofill work
            args=["--no-first-run", "--no-default-browser-check"],
            accept_downloads=True,
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            yield ctx, page
        finally:
            ctx.close()
