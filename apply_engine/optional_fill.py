# -*- coding: utf-8 -*-
"""G5 — answer-every-field (optionals + EEO).

WHY THIS EXISTS (feedback_apply_answer_every_field, design doc §8.2 G5): the engine fills
required + known-mapped fields, but leaves OPTIONAL free-text and EEO/self-ID widgets blank. A
blank optional reads as a lazy autopilot submit; Sam wants every application to look like he
sat down and filled the whole thing. This pass runs AFTER the required-field fill and fills any
STILL-EMPTY optional/EEO field from a stored profile.

WHAT IT FILLS (only when the field is present + still empty):
  * EEO / voluntary self-ID — gender / race / hispanic / veteran / disability. These are Sam's
    REAL, confirmed values (gender=Male, race=White, hispanic=No, veteran="not a protected
    veteran"), disability=decline. Disclosed where the profile discloses; a decline-style option
    otherwise. Truthful — never asserts a value the profile doesn't hold.
  * Optional free-text patterns — name pronunciation, earliest start date, deadlines/timeline,
    additional-info (ONLY when there is no cover-letter field — otherwise it would be redundant),
    phone country.
  * Website — stays BLANK unless the profile holds a REAL url. Never fabricated.

HARD RULES (the whole point of the design):
  * ADDITIVE / BEST-EFFORT: every action is wrapped so an optional-fill failure can NEVER fail the
    stage. A form with no optional fields behaves exactly as today.
  * TRUTHFUL: EEO values come from the profile (Sam's real ones); website is never invented.
  * REQUIRED FIELDS ARE NOT TOUCHED: this pass only fills fields the form marks OPTIONAL (and EEO,
    which is voluntary). Required fields are owned by the normal fill/escalation path.
  * LIVE-DOM (feedback_apply_engine_live_dom_and_empty_guard): if a widget can't be driven, SKIP it
    and leave it for the normal escalation — never fabricate a fill or report a phantom success.
"""
import json
import re
from pathlib import Path
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Profile loading
# ---------------------------------------------------------------------------
def load_profile(profile_path) -> dict:
    """Load the applicant profile JSON. Returns {} on any read/parse error (best-effort — a
    missing/broken profile must never crash the stage; the pass then fills nothing)."""
    try:
        return json.loads(Path(profile_path).read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Category classification — map a field's label to an EEO/optional category.
# Word-boundary regexes; ORDER matters (veteran/disability before the generic gender/race).
# ---------------------------------------------------------------------------
_EEO_PATTERNS = [
    ("veteran",         re.compile(r"\bveteran|protected\s+veteran\b", re.I)),
    ("disability",      re.compile(r"\bdisabilit(?:y|ies)\b", re.I)),
    ("hispanic",        re.compile(r"\bhispanic|latin[oax]\b", re.I)),
    # sexual orientation + gender identity are voluntary self-ID too — defer + decline,
    # never let the screening path "answer" them (JOB-281 Together AI). Before generic gender.
    ("orientation",     re.compile(r"sexual\s+orientation|\blgbtq", re.I)),
    ("gender_identity", re.compile(r"\btransgender\b|gender\s+identity", re.I)),
    ("gender",          re.compile(r"\bgender\b", re.I)),
    ("race",            re.compile(r"\brace|ethnicity|ethnic\b", re.I)),
]

_OPTIONAL_TEXT_PATTERNS = [
    # name pronunciation — "how do you pronounce your name", "name pronunciation"
    ("name_pronunciation", re.compile(r"pronoun|pronunciation|how\s+do\s+(?:you|we)\s+say", re.I)),
    # earliest start / availability / notice period
    ("earliest_start",     re.compile(r"start\s*date|earliest\s+(?:start|avail)|when\s+can\s+you\s+start|availability|notice\s+period|how\s+soon", re.I)),
    # deadlines / competing offers / timeline considerations
    ("deadlines",          re.compile(r"deadline|competing\s+offer|timeline\s+consideration|time[-\s]?sensitive", re.I)),
    # additional information / anything else
    ("additional_info",    re.compile(r"additional\s+info|anything\s+else|anything\s+more|other\s+(?:info|comments)|is\s+there\s+anything|something\s+(?:we|else)\s+should\s+know", re.I)),
    # phone country (a country picker next to a phone field)
    ("phone_country",      re.compile(r"phone\s+country|country\s+code|phone.*country|dialing\s+code", re.I)),
]

# "select"/"decline"-style option text we fall back to when the profile declines a category.
_DECLINE_HINTS = (
    "decline", "do not want to answer", "don't want to answer", "prefer not",
    "rather not", "not want to disclose", "choose not", "i don't wish",
)


def classify_eeo(label: str) -> Optional[str]:
    """Return the EEO category for a label ('gender'|'race'|'hispanic'|'veteran'|'disability'),
    or None when the label isn't an EEO self-ID question."""
    text = label or ""
    for cat, rx in _EEO_PATTERNS:
        if rx.search(text):
            return cat
    return None


def classify_optional_text(label: str) -> Optional[str]:
    """Return the optional free-text category for a label, or None when it doesn't match a known
    optional-pattern (so a random optional free-text box is NOT force-filled with a canned note)."""
    text = label or ""
    for cat, rx in _OPTIONAL_TEXT_PATTERNS:
        if rx.search(text):
            return cat
    return None


# ---------------------------------------------------------------------------
# Profile -> value resolution
# ---------------------------------------------------------------------------
def _eeo_value(profile: dict, cat: str) -> str:
    sid = (profile or {}).get("self_id") or {}
    return str(sid.get(cat, "") or "").strip()


def _optional_text_value(profile: dict, cat: str) -> str:
    if cat == "phone_country":
        return str((profile or {}).get("phone_country", "") or "").strip()
    oa = (profile or {}).get("optional_answers") or {}
    return str(oa.get(cat, "") or "").strip()


def _pick_eeo_option(option_texts: List[str], desired: str) -> Optional[str]:
    """Choose the live option that best matches the desired EEO value.

    `desired` is the profile value ('Male', 'White', 'No', 'I am not a protected veteran',
    'I do not want to answer'). Strategy:
      1. exact (case-insensitive) match,
      2. the desired value is a substring of an option (or vice-versa) — handles long option
         phrasings ("White (Not Hispanic or Latino)" for 'White'),
      3. if the desired value is itself a decline phrase, fall back to ANY decline-style option.
    Returns the matched live option text, or None when nothing matches (then SKIP — never guess)."""
    want = (desired or "").strip().lower()
    if not want:
        return None
    opts = [o for o in option_texts if (o or "").strip()]
    # 1) exact
    for o in opts:
        if o.strip().lower() == want:
            return o
    # 2) substring either direction (longest-option-first so "White (Not Hispanic...)" wins over a
    #    bare "White" only when the bare one is absent — but exact already handled the bare case)
    for o in sorted(opts, key=lambda x: -len(x)):
        ol = o.strip().lower()
        if want in ol or ol in want:
            return o
    # 3) desired itself is a decline phrase -> match any decline option
    if any(h in want for h in _DECLINE_HINTS):
        for o in opts:
            ol = o.strip().lower()
            if any(h in ol for h in _DECLINE_HINTS):
                return o
    return None


# ---------------------------------------------------------------------------
# Live-DOM helpers — read options + emptiness without trusting any cached state.
# ---------------------------------------------------------------------------
def _field_is_empty(page, fs) -> bool:
    """True when the field has no value yet. Best-effort per widget kind; on any error we return
    False (treat as filled) so we NEVER overwrite a value we couldn't read."""
    try:
        if fs.widget_kind == "native_select":
            el = page.query_selector(fs.selector) if fs.selector else None
            if not el:
                return False
            return not (el.input_value() or "").strip()
        if fs.widget_kind in ("text", "textarea"):
            el = page.query_selector(fs.selector) if fs.selector else None
            if not el:
                return False
            return not (el.input_value() or "").strip()
        if fs.widget_kind == "react_select":
            ctrl = _react_control(page, fs)
            if ctrl is None:
                return False
            sv = ctrl.query_selector(".select__single-value")
            if sv and (sv.inner_text() or "").strip():
                return False
            dv = ctrl.get_attribute("data-value")
            return not (dv and dv.strip())
        if fs.widget_kind == "radio":
            nm = fs.key
            for r in page.query_selector_all(f'input[type="radio"][name="{nm}"]'):
                try:
                    if r.is_checked():
                        return False
                except Exception:
                    continue
            return True
    except Exception:
        return False
    return False


def _react_control(page, fs):
    """The .select__control element for a react_select FieldSpec (by its input id selector, else
    by matching the wrapper label). None when not locatable."""
    try:
        if fs.selector:
            inp = page.query_selector(fs.selector)
            if inp:
                h = inp.evaluate_handle("e => e.closest('.select__control')")
                el = h.as_element() if h else None
                if el:
                    return el
    except Exception:
        pass
    return None


def _native_select_options(page, fs) -> List[str]:
    try:
        el = page.query_selector(fs.selector) if fs.selector else None
        if not el:
            return []
        return el.evaluate(
            "e => Array.from(e.options).map(o => (o.textContent||'').trim())")
    except Exception:
        return []


def _react_options(page, fs) -> List[str]:
    """Open a react-select control and enumerate its visible options, then close it. Read-only."""
    ctrl = _react_control(page, fs)
    if ctrl is None:
        return []
    try:
        ctrl.scroll_into_view_if_needed()
        ctrl.click()
        page.wait_for_timeout(250)
    except Exception:
        return []
    out: List[str] = []
    try:
        for o in page.query_selector_all(".select__option"):
            t = (o.inner_text() or "").strip()
            if t:
                out.append(t)
    except Exception:
        pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return out


def _radio_options(page, fs) -> List[str]:
    out: List[str] = []
    try:
        for r in page.query_selector_all(f'input[type="radio"][name="{fs.key}"]'):
            v = (r.get_attribute("value") or "").strip()
            if v:
                out.append(v)
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Drivers — each returns True only if the value provably registered (live-dom rule). On any
# failure they return False and the caller records a SKIP (never a phantom fill).
# ---------------------------------------------------------------------------
def _drive_text(page, fs, value: str) -> bool:
    try:
        el = page.query_selector(fs.selector) if fs.selector else None
        if not el:
            return False
        el.fill(value)
        return (el.input_value() or "").strip() == value.strip()
    except Exception:
        return False


def _drive_native_select(page, fs, option_text: str) -> bool:
    try:
        page.select_option(fs.selector, label=option_text)
    except Exception:
        return False
    try:
        el = page.query_selector(fs.selector)
        return bool(el) and bool((el.input_value() or "").strip())
    except Exception:
        return False


def _drive_react_select(page, fs, option_text: str, adapter) -> bool:
    ctrl = _react_control(page, fs)
    if ctrl is None:
        return False
    if adapter is not None and hasattr(adapter, "_pick_react_select"):
        try:
            return bool(adapter._pick_react_select(page, ctrl, option_text))
        except Exception:
            return False
    return False


def _drive_radio(page, fs, option_text: str) -> bool:
    want = (option_text or "").strip().lower()
    try:
        for r in page.query_selector_all(f'input[type="radio"][name="{fs.key}"]'):
            if (r.get_attribute("value") or "").strip().lower() == want:
                try:
                    r.check()
                except Exception:
                    r.click()
                try:
                    return bool(r.is_checked())
                except Exception:
                    return False
    except Exception:
        return False
    return False


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------
def fill_optional_and_eeo(page, form_spec, profile: dict, adapter=None) -> Dict[str, object]:
    """Fill every STILL-EMPTY OPTIONAL or EEO field on the live form from `profile`. Runs AFTER
    the required-field fill, BEFORE the brink. ADDITIVE / BEST-EFFORT — never raises, never fails
    the stage, never touches a required field, never fabricates a website url, and only records a
    field as filled when the value provably registered.

    Returns a report: {"filled": {label: value}, "skipped": [label, ...]}. The caller logs it; an
    empty report (no optional/EEO fields, or none drivable) is the normal no-op for a plain form.
    """
    report: Dict[str, object] = {"filled": {}, "skipped": []}
    try:
        fields = list(getattr(form_spec, "fields", None) or []) if form_spec is not None else []
        has_cover = bool(getattr(form_spec, "has_cover_field", False))
    except Exception:
        # a malformed form model (e.g. a .fields that raises) must NEVER propagate — the caller's
        # stage proceeds exactly as if there were no optional fields.
        return report
    if not fields:
        return report

    for fs in fields:
        try:
            label = getattr(fs, "label", "") or ""
            kind = getattr(fs, "widget_kind", "")
            # NEVER touch required fields — the normal fill/escalation path owns them. (EEO is
            # voluntary, so a required marker on an EEO widget is honored as "fill it"; but a
            # required NON-EEO field is left alone.)
            eeo_cat = classify_eeo(label)
            if getattr(fs, "required", False) and eeo_cat is None:
                continue
            # only fill what is still empty
            if not _field_is_empty(page, fs):
                continue

            # ---- EEO / self-ID ----
            if eeo_cat is not None:
                desired = _eeo_value(profile, eeo_cat)
                if not desired:
                    continue
                if kind == "native_select":
                    chosen = _pick_eeo_option(_native_select_options(page, fs), desired)
                    if chosen and _drive_native_select(page, fs, chosen):
                        report["filled"][label] = chosen
                    else:
                        report["skipped"].append(label)
                elif kind == "react_select":
                    chosen = _pick_eeo_option(_react_options(page, fs), desired)
                    if chosen and _drive_react_select(page, fs, chosen, adapter):
                        report["filled"][label] = chosen
                    else:
                        report["skipped"].append(label)
                elif kind == "radio":
                    chosen = _pick_eeo_option(_radio_options(page, fs), desired)
                    if chosen and _drive_radio(page, fs, chosen):
                        report["filled"][label] = chosen
                    else:
                        report["skipped"].append(label)
                elif kind in ("text", "textarea"):
                    if _drive_text(page, fs, desired):
                        report["filled"][label] = desired
                    else:
                        report["skipped"].append(label)
                else:
                    report["skipped"].append(label)
                continue

            # ---- website: NEVER fabricate. Fill only with a real profile url. ----
            if _is_website_field(label, fs):
                url = str((profile or {}).get("website", "") or "").strip()
                if url and kind in ("text", "textarea"):
                    if _drive_text(page, fs, url):
                        report["filled"][label] = url
                # no url -> legitimately left blank (not even a skip — nothing to fill)
                continue

            # ---- optional free-text patterns ----
            cat = classify_optional_text(label)
            if cat is None:
                continue
            if cat == "additional_info" and has_cover:
                # a tailored cover already covers this — leave additional-info blank rather than
                # paste a redundant note.
                continue
            value = _optional_text_value(profile, cat)
            if not value:
                continue
            if cat == "phone_country":
                # phone country is usually a select/react-select picker, occasionally a text input.
                if kind == "native_select":
                    chosen = _pick_eeo_option(_native_select_options(page, fs), value) or value
                    if _drive_native_select(page, fs, chosen):
                        report["filled"][label] = chosen
                    else:
                        report["skipped"].append(label)
                elif kind == "react_select":
                    if _drive_react_select(page, fs, value, adapter):
                        report["filled"][label] = value
                    else:
                        report["skipped"].append(label)
                elif kind in ("text", "textarea"):
                    if _drive_text(page, fs, value):
                        report["filled"][label] = value
                    else:
                        report["skipped"].append(label)
                else:
                    report["skipped"].append(label)
                continue
            # plain optional free-text
            if kind in ("text", "textarea"):
                if _drive_text(page, fs, value):
                    report["filled"][label] = value
                else:
                    report["skipped"].append(label)
            else:
                report["skipped"].append(label)
        except Exception:
            # a single field's failure must never break the pass.
            try:
                report["skipped"].append(getattr(fs, "label", "") or "")
            except Exception:
                pass
            continue
    return report


_WEBSITE_RX = re.compile(r"\b(?:personal\s+)?(?:website|web\s*site|portfolio\s+url|homepage|personal\s+url)\b", re.I)


def _is_website_field(label: str, fs) -> bool:
    """A 'website' free-text field (so we can enforce the never-fabricate rule). Matches the label
    or a url-typed input named/ided 'website'. Excludes LinkedIn/GitHub (those are real mapped
    profile urls handled by the normal fill path)."""
    text = (label or "")
    if re.search(r"linkedin|github", text, re.I):
        return False
    if _WEBSITE_RX.search(text):
        return True
    key = (getattr(fs, "key", "") or "").lower()
    return key in ("website", "personal_website", "url") and getattr(fs, "widget_kind", "") in ("text", "textarea")
