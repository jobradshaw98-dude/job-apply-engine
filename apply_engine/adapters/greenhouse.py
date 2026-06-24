"""Greenhouse adapter — standard single-page form. Text fields keyed by id
(verified live: first_name/last_name/email/phone/resume). Work-auth questions are
React-Select widgets, handled by FormAdapterBase."""
from .base import FormAdapterBase


class GreenhouseAdapter(FormAdapterBase):
    name = "greenhouse"
    text_fields = {
        "first_name": "#first_name",
        "last_name": "#last_name",
        "email": "#email",
        "phone": "#phone",
    }
    resume_selector = "#resume"
