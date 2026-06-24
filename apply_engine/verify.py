"""Compare intended field values against what is actually present in the form.
Email compared case-insensitively; URL fields tolerate benign site normalization
(scheme, leading www., trailing slash, case) but still catch a wrong path; all values
whitespace-trimmed. Anything else must match exactly. Returns a structured result the
orchestrator logs and gates on."""
from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class VerifyResult:
    ok: bool
    mismatches: List[Tuple[str, str, str]] = field(default_factory=list)  # (key, intended, observed)


_URL_KEYS = ("linkedin", "github", "portfolio", "website", "url")
_PHONE_KEYS = ("phone", "tel", "mobile", "cell")


def _is_url_field(key: str, *vals: str) -> bool:
    """A field is URL-like if its key names a known URL field, or any value is a full
    http(s) URL. Keeps the loosened comparison OFF for ordinary text fields."""
    if any(u in key.lower() for u in _URL_KEYS):
        return True
    return any((v or "").strip().lower().startswith(("http://", "https://")) for v in vals)


def _is_phone_field(key: str) -> bool:
    return any(p in key.lower() for p in _PHONE_KEYS)


def _phone_match(want: str, got: str) -> bool:
    """True if two phone strings are the same number ignoring formatting. A form re-renders the
    same number with different separators (Reducto: '+1 555-555-0100' vs '+1 555-555-0100') — a
    pure string compare false-flagged that as a verification mismatch and aborted the finish.
    Compare digits only; tolerate a dropped/added country-code prefix via a 10-digit suffix match
    (a US local number is the last 10 digits), but a genuinely different number still differs."""
    dw = "".join(c for c in (want or "") if c.isdigit())
    dg = "".join(c for c in (got or "") if c.isdigit())
    if not dw or not dg:
        return dw == dg
    if dw == dg:
        return True
    return len(dw) >= 10 and len(dg) >= 10 and (dw.endswith(dg) or dg.endswith(dw))


def _canon_url(val: str) -> str:
    """Canonicalize a URL for tolerant comparison: drop scheme, leading www., trailing
    slash, and case. Lever (live) rewrites https://www.linkedin.com/in/x ->
    http://linkedin.com/in/x — the same profile, so these must compare equal. A different
    path (a wrong profile) still differs and is correctly flagged."""
    v = (val or "").strip().lower()
    for s in ("https://", "http://"):
        if v.startswith(s):
            v = v[len(s):]
            break
    if v.startswith("www."):
        v = v[4:]
    return v.rstrip("/")


def _norm(key: str, val: str) -> str:
    v = (val or "").strip()
    if key == "email":
        v = v.lower()
    return v


def verify_fields(intended: dict, observed: dict) -> VerifyResult:
    mismatches = []
    for key, want in intended.items():
        got = observed.get(key)
        if got is None:
            mismatches.append((key, str(want), ""))
            continue
        if _is_url_field(key, str(want), str(got)):
            match = _canon_url(str(got)) == _canon_url(str(want))
        elif _is_phone_field(key):
            match = _phone_match(str(want), str(got))
        else:
            match = _norm(key, str(got)) == _norm(key, str(want))
        if not match:
            mismatches.append((key, str(want), str(got)))
    return VerifyResult(ok=(not mismatches), mismatches=mismatches)
