"""Audit a staged application form for values the ATS auto-populated WRONG.

Why this exists: ATSs parse the uploaded resume and silently auto-fill fields the
engine never set — and sometimes get them wrong. Live example (Lever, 2026-06-01):
"Current company" came back as "BUILDS" because the parser grabbed the resume's
"SELECTED BUILDS" section heading; "Current location" came back "Austin, TX".
verify.py only checks the handful of fields the engine itself set, so it is blind
to these. This module catches them.

The core (`audit_fields`) is pure: it takes an `observed` dict (form LABEL,
lowercased -> current value) and a `known` dict (canonical key -> Sam's correct
value) and returns a conservative list of Corrections. It NEVER touches free-text /
essay / custom questions, and it only proposes an `overwrite` when it is confident
the label maps to a known identity/employment/contact field AND the values differ.
When a field looks identity-bearing but we have no known value, it `flag`s it for
Sam instead of guessing. Nothing here submits anything.
"""
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Correction:
    label: str        # the form field label (as observed, lowercased)
    current: str      # what the ATS put in the field
    correct: str      # Sam's correct value ("" when we have none -> flag)
    action: str       # "overwrite" (confident fix) | "flag" (leave for Sam)
    selector: str = ""  # stable selector captured at read time ("" = ambiguous/uncorrectable)


# Ordered label keyword -> known key. Order matters: more specific / employment
# keywords are checked before generic location keywords so "current company" can
# never be mistaken for a location field. Each entry: (keywords, known_key).
# A label matches an entry if it CONTAINS any of the entry's keywords.
_LABEL_RULES = [
    # NOTE: keep these SPECIFIC. Bare tokens ("employer", "location", "city") wrongly match
    # custom short-text questions ("describe your ideal employer", "which city would you
    # relocate from?") and the always-on audit would clobber a drafted answer. Only compound,
    # unambiguous identity/employment labels belong here.
    (("current company", "current employer", "company name", "current organization", "current organisation"), "current_company"),
    (("current location", "current city", "your location", "city/state", "city / state", "home city"), "current_location"),
    (("linkedin",), "linkedin"),
    (("email address", "e-mail address", "email"), "email"),
    (("phone number", "mobile number", "cell phone", "phone", "mobile"), "phone"),
    (("full name", "your name", "legal name", "candidate name"), "full_name"),
]

# Keys whose values are URLs — compared with benign-normalization tolerance.
_URL_KEYS = {"linkedin", "github", "portfolio", "website"}


def _canon_url(val: str) -> str:
    """Canonicalize a URL so benign site rewrites (scheme, leading www., trailing
    slash, case) compare equal but a genuinely different path still differs.
    Mirrors verify.py._canon_url so link audits behave the same way the verifier does."""
    v = (val or "").strip().lower()
    for s in ("https://", "http://"):
        if v.startswith(s):
            v = v[len(s):]
            break
    if v.startswith("www."):
        v = v[4:]
    return v.rstrip("/")


def _canon_text(val: str) -> str:
    """Case- and whitespace-insensitive canonical form for ordinary text values:
    lowercase and collapse all runs of whitespace to a single space."""
    return " ".join((val or "").lower().split())


def _known_key_for(label: str) -> Optional[str]:
    """Map a form label to a canonical known key, or None if the label is not a
    recognized identity/employment/contact field. Conservative by design: an
    unrecognized label (essays, custom questions, salary, etc.) returns None and is
    left completely untouched."""
    low = (label or "").lower()
    for keywords, key in _LABEL_RULES:
        if any(kw in low for kw in keywords):
            return key
    return None


def _values_match(key: str, current: str, correct: str) -> bool:
    if key in _URL_KEYS:
        return _canon_url(current) == _canon_url(correct)
    return _canon_text(current) == _canon_text(correct)


def audit_fields(observed: dict, known: dict) -> List[Correction]:
    """Compare what the form currently shows against what we know to be correct.

    For each observed (label -> current value):
      * If the label does not map to a known identity/employment/contact field, skip
        it entirely (never touch essays/custom questions).
      * If the current value is blank, skip it — filling blanks is the filler's job,
        not the auditor's.
      * If we have a non-empty known value and it differs from the current value,
        propose an `overwrite`.
      * If the label maps to a known field but we have no value for it, `flag` it so
        Sam can decide — never guess.
    """
    corrections: List[Correction] = []
    for raw_label, raw_current in observed.items():
        label = (raw_label or "").lower()
        current = (raw_current or "").strip()
        if not current:
            continue
        key = _known_key_for(label)
        if key is None:
            continue
        correct = str(known.get(key) or "").strip()
        if not correct:
            corrections.append(Correction(label=label, current=current, correct="", action="flag"))
            continue
        if not _values_match(key, current, correct):
            corrections.append(Correction(label=label, current=current, correct=correct, action="overwrite"))
    return corrections


# Standard ATS field NAMES whose label_for() returns the raw name (e.g. Lever uses
# name="org" for Current company, name="location" for Current location) instead of the
# visible label. Map them to the canonical label the audit rules understand. Matched on
# the EXACT name only (never a substring) so it can't catch a custom question.
_NAME_TO_LABEL = {
    "org": "current company",
    "location": "current location",
    "name": "full name",
    "email": "email",
    "phone": "phone",
}


def _scan_text_inputs(page):
    """One live scan of visible, FILLED text <input>s. Yields (label_lower, value,
    selector) where selector is a stable attribute selector — [id="..."] preferred, else
    [name="..."], else "" (uncorrectable). When a field's `name` is a known standard-field
    name (e.g. Lever's "org"/"location"), the canonical label is used so the audit rules
    match — label_for returns the raw name for those. NOT unit-tested (live-only)."""
    from apply_engine.completeness import label_for

    rows = []
    for el in page.query_selector_all("input"):
        t = (el.get_attribute("type") or "text").lower()
        if t not in ("text", "email", "tel", "url", ""):
            continue
        try:
            if not el.is_visible():
                continue
            val = (el.input_value() or "").strip()
        except Exception:
            continue
        if not val:
            continue
        eid = el.get_attribute("id")
        name = el.get_attribute("name")
        label = _NAME_TO_LABEL.get((name or "").strip().lower()) or label_for(page, el).lower()
        sel = f'[id="{eid}"]' if eid else (f'[name="{name}"]' if name else "")
        rows.append((label, val, sel))
    return rows


def read_text_fields(page) -> dict:
    """Live: {label (lowercased) -> current value} for visible, filled text inputs."""
    return {lab: val for lab, val, _ in _scan_text_inputs(page)}


def _known_from_profile(profile: dict) -> dict:
    """Build the `known` dict audit_fields expects from an applicant profile.
    current_location is composed from city/state when not given explicitly."""
    loc = (profile.get("current_location") or "").strip()
    if not loc:
        city = (profile.get("city") or "").strip()
        state = (profile.get("state") or "").strip()
        loc = f"{city}, {state}".strip().strip(",").strip() if (city or state) else ""
    return {
        "current_company": (profile.get("current_company") or "").strip(),
        "current_location": loc,
        "full_name": (profile.get("full_name") or "").strip(),
        "email": (profile.get("email") or "").strip(),
        "phone": (profile.get("phone") or "").strip(),
        "linkedin": (profile.get("linkedin") or "").strip(),
    }


def index_rows(rows):
    """PURE: fold scanned (label, value, selector) rows into (observed, selmap). `observed`
    maps label->value (last wins). `selmap` maps label->selector, BUT any label seen on more
    than one field is marked ambiguous (selector "") so apply_corrections refuses to overwrite
    it blindly — the wrong-field-overwrite guard. Unit-tested separately from the live scan."""
    observed: dict = {}
    selmap: dict = {}
    seen: dict = {}
    for lab, val, sel in rows:
        observed[lab] = val
        selmap[lab] = sel
        seen[lab] = seen.get(lab, 0) + 1
    for lab, n in seen.items():
        if n > 1:
            selmap[lab] = ""
    return observed, selmap


def audit_form(page, profile: dict) -> List[Correction]:
    """Read the live form's text fields, audit against the profile, and attach a STABLE
    selector to each correction so overwrites target the exact field that was read (not a
    re-matched label). A label shared by >1 field is marked ambiguous (selector "") so we
    never blindly overwrite the wrong one."""
    observed, selmap = index_rows(_scan_text_inputs(page))
    corrections = audit_fields(observed, _known_from_profile(profile))
    for c in corrections:
        c.selector = selmap.get(c.label, "")
    return corrections


def apply_corrections(page, corrections: List[Correction]) -> List[dict]:
    """Live: overwrite each `overwrite` correction by its CAPTURED selector (never by
    re-matching the label — a duplicate label could hit the wrong field). Skips `flag`s and
    corrections with no/ambiguous selector. Returns ONLY the fields actually written — a
    fill that throws is NOT reported as applied. NOT unit-tested (live-only)."""
    applied: List[dict] = []
    for c in corrections:
        if c.action != "overwrite" or not c.correct or not c.selector:
            continue
        try:
            page.fill(c.selector, c.correct)
            applied.append({"label": c.label, "value": c.correct, "selector": c.selector})
        except Exception:
            pass
    return applied
