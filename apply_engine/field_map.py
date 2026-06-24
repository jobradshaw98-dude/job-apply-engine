"""Deterministic field -> answer-key mapping by label/name/placeholder text.
Order matters: more specific patterns first (first/last name before bare 'name')."""
import re
from typing import Optional

# (answer_key, regex) — first match wins; ordered most-specific to least.
_RULES = [
    ("first_name", r"first\s*name|given\s*name|\bfname\b"),
    ("last_name", r"last\s*name|surname|family\s*name|\blname\b"),
    ("email", r"e-?mail"),
    ("phone", r"phone|mobile|tel\b|telephone"),
    ("linkedin", r"linkedin"),
    ("portfolio_url", r"portfolio|website|personal\s*site"),
    ("city", r"\bcity\b|town"),
    ("state", r"\bstate\b|province|region"),
    ("country", r"\bcountry\b"),
    ("full_name", r"\bfull\s*name\b|\bname\b"),  # least specific name rule LAST
]


def map_field(label: str, name: str = "", placeholder: str = "",
              llm_hook=None) -> Optional[str]:
    hay = " ".join([(label or ""), (name or ""), (placeholder or "")]).lower()
    for key, pat in _RULES:
        if re.search(pat, hay):
            return key
    # Unmapped: optionally escalate to an injectable LLM hook (returns key or None).
    if llm_hook is not None:
        return llm_hook(label, name, placeholder)
    return None
