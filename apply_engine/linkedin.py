"""Resolve a LinkedIn posting to its real apply destination WITHOUT automating
linkedin.com beyond reading the page already loaded by the caller.

- Outbound 'Apply on company website' link -> return its href (a real ATS URL).
- Otherwise assume in-platform Easy Apply -> return EASY_APPLY sentinel so the
  orchestrator stages it for the user to complete manually.
"""
EASY_APPLY = "__EASY_APPLY__"

_APPLY_SELECTORS = [
    "a.apply-button[href]",
    "a[href*='greenhouse.io']",
    "a[href*='ashbyhq.com']",
    "a[href*='myworkdayjobs.com']",
    "a[href*='lever.co']",
    "a[data-tracking-control-name*='apply'][href^='http']",
]


def resolve_linkedin(page) -> str:
    for sel in _APPLY_SELECTORS:
        el = page.query_selector(sel)
        if el:
            href = el.get_attribute("href")
            if href and "linkedin.com" not in href:
                return href
    return EASY_APPLY
