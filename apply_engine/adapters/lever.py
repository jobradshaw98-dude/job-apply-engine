"""Lever adapter — verified against live jobs.lever.co/.../apply (2026-05-31).

The application form lives at <posting-url>/apply. Standard fields are targeted by
`name` attribute (Lever inputs have no id): name/email/phone, plus urls[LinkedIn] and
the resume file input #resume-upload-input. Custom per-job questions (university,
veteran/disability, and any work-auth) are native <select>s in cards[...]; work-auth
ones are answered by FormAdapterBase, the rest are left for the user at review."""
from .base import FormAdapterBase


class LeverAdapter(FormAdapterBase):
    name = "lever"
    text_fields = {
        "full_name": "input[name='name']",
        "email": "input[name='email']",
        "phone": "input[name='phone']",
        "linkedin": 'input[name="urls[LinkedIn]"]',
    }
    resume_selector = "#resume-upload-input"

    def go_to_form(self, page) -> None:
        # Already on the form?
        if page.query_selector(self.resume_selector):
            return
        # Try the generic Apply control first (Lever's Apply button links to /apply).
        super().go_to_form(page)
        if page.query_selector(self.resume_selector):
            return
        # Fallback: navigate straight to the canonical <posting>/apply URL.
        cur = page.url.split("?")[0].rstrip("/")
        if not cur.endswith("/apply"):
            try:
                page.goto(cur + "/apply", wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
            except Exception:
                pass
