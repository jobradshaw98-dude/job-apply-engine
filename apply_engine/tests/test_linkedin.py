from apply_engine.browser import launch_profile
from apply_engine.linkedin import resolve_linkedin, EASY_APPLY


def test_resolve_follows_outbound_apply_link(fixture_server, tmp_path):
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(f"{fixture_server}/linkedin_redirect.html")
        target = resolve_linkedin(page)
        assert target == "https://job-boards.greenhouse.io/acme/jobs/999"


def test_resolve_returns_easy_apply_when_no_outbound(fixture_server, tmp_path):
    with launch_profile(headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.set_content("<button>Easy Apply</button>")
        assert resolve_linkedin(page) == EASY_APPLY
