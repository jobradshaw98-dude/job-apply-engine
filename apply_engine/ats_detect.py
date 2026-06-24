"""Classify an apply URL into a known ATS. Pure function, no I/O."""
import re
from enum import Enum


class AtsKind(str, Enum):
    GREENHOUSE = "greenhouse"
    ASHBY = "ashby"
    WORKDAY = "workday"
    LEVER = "lever"
    LINKEDIN = "linkedin"
    UNKNOWN = "unknown"


_PATTERNS = [
    (AtsKind.GREENHOUSE, r"greenhouse\.io"),
    (AtsKind.ASHBY, r"ashbyhq\.com"),
    (AtsKind.WORKDAY, r"myworkdayjobs\.com|\.workday\."),
    (AtsKind.LEVER, r"lever\.co"),
    (AtsKind.LINKEDIN, r"linkedin\.com"),
]


def detect_ats(url: str) -> AtsKind:
    low = (url or "").lower()
    for kind, pat in _PATTERNS:
        if re.search(pat, low):
            return kind
    return AtsKind.UNKNOWN
