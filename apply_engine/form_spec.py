# -*- coding: utf-8 -*-
"""Live-form MODEL (Phase 0, its own milestone — the dependency root of the convergence loop).

WHY THIS EXISTS (design doc §8.4): the engine has NO full-form scrape today. Field discovery is
purpose-built per signal (`completeness.unfilled_required` returns only unfilled-REQUIRED labels;
the adapter finders cover only work-auth / screening classes), and ZERO word/char-constraint
capture exists anywhere. The adapters are WRITE-oriented (find-this-question-and-fill-it), never
read-the-whole-form. Until a faithful form model exists, the downstream convergence loop would
optimize against staged FICTION — so this is built first and gates everything else.

WHAT THIS MODULE OWNS (deterministic-first, fully offline-testable):
  * `FieldSpec` / `FormSpec` — the data shapes (one row per live field).
  * `scrape_constraints(...)` — DETERMINISTIC regex-first capture of stated length limits
    (`maxlength`, "200-400 words", "max 500 characters") from helper text / placeholder.
  * `_llm_constraint_read(...)` — a CLEARLY-MARKED no-op hook where a future `claude -p` pass can
    read ambiguous helper copy. It returns None in Phase 0 (no LLM call, so this is testable
    offline). DO NOT call an LLM here yet.

WHAT THIS MODULE DOES NOT DO (Phase 0 hard constraint — ADDITIVE / read-only):
  * It does NOT change fill or submit behavior, does NOT write the manifest, does NOT auto-correct.
  * `enumerate_fields` (on the adapter) and `reconcile_form` (in `reconcile.py`) are pure-ish:
    they take a live page / a built spec and return data. Nothing is mutated on disk.

LIVE-DOM RULE (feedback_apply_engine_live_dom_and_empty_guard): the model is built by READING the
real DOM via the same primitives the adapters drive (`completeness.label_for` / `_is_required` /
react-select detection). It never asserts a field/constraint it can't see on the live page.
"""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# Widget kinds the model distinguishes. These mirror the kinds the adapters already drive, so a
# downstream consumer (reconcile / a future filler) can route by the same vocabulary.
#   text          - <input type=text|email|tel|url> / <input> with no type
#   textarea      - free-text essay box
#   native_select - <select>
#   react_select  - .select__control JS combobox (modern Greenhouse/Ashby/Lever)
#   radio         - <input type=radio> group (keyed by name)
#   checkbox      - <input type=checkbox> (lone or part of a check-all group)
#   file          - <input type=file> upload
#   combobox      - role=combobox that is NOT a react-select (rare; typeahead)
WIDGET_KINDS = (
    "text", "textarea", "native_select", "react_select",
    "radio", "checkbox", "file", "combobox",
)

# doc_kind for upload (file) fields — what the form wants attached there.
DOC_KINDS = ("resume", "cover", "other")


@dataclass
class FieldSpec:
    """One live form field, as READ from the DOM (never invented).

    key:        a stable handle for the field (id, else name, else the normalized label). Used to
                map a staged answer back to this field during reconciliation.
    label:      the human-visible question/label (via completeness.label_for + Lever card recovery).
    required:   whether the live form marks it required (via completeness._is_required + markers).
    widget_kind: one of WIDGET_KINDS.
    doc_kind:   for file fields only — 'resume' | 'cover' | 'other'; '' for non-file fields.
    constraints: stated length limits (see scrape_constraints) — {} when none stated.
    selector:   a best-effort CSS selector to the field ('' when not addressable, e.g. a react
                control with no id). Informational in Phase 0 (nothing drives off it yet).
    """
    key: str
    label: str
    required: bool
    widget_kind: str
    doc_kind: str = ""
    constraints: Dict[str, object] = field(default_factory=dict)
    selector: str = ""


@dataclass
class FormSpec:
    """The whole live form, as a flat list of FieldSpec rows + a small summary.

    fields:        every enumerated field, in DOM order.
    has_resume_field / has_cover_field: convenience flags (G7 — attach what the form has fields
                   for; a missing cover field is STRUCTURAL, not a failure).
    ats:           the adapter name that built this spec (informational).
    """
    fields: List[FieldSpec] = field(default_factory=list)
    has_resume_field: bool = False
    has_cover_field: bool = False
    ats: str = ""

    def required_fields(self) -> List[FieldSpec]:
        return [f for f in self.fields if f.required]

    def by_key(self, key: str) -> Optional[FieldSpec]:
        for f in self.fields:
            if f.key == key:
                return f
        return None

    def to_summary(self) -> Dict[str, object]:
        """A COMPACT, JSON-serializable summary of the live form for the staged manifest record
        (Phase 4b capture). Stores enough for the dashboard / a later mapper to reason about the
        form WITHOUT re-opening the page: each field's key/label/required/widget_kind/doc_kind +
        any captured length constraints, plus the G7 doc-field flags + the ATS name. Additive — a
        reader that doesn't know `form_spec` ignores it."""
        return {
            "ats": self.ats,
            "has_resume_field": bool(self.has_resume_field),
            "has_cover_field": bool(self.has_cover_field),
            "n_fields": len(self.fields),
            "fields": [
                {
                    "key": f.key,
                    "label": f.label,
                    "required": bool(f.required),
                    "widget_kind": f.widget_kind,
                    "doc_kind": f.doc_kind,
                    "constraints": dict(f.constraints or {}),
                }
                for f in self.fields
            ],
        }


# ---------------------------------------------------------------------------
# Constraint scrape — DETERMINISTIC regex-first. Stated length limits live in helper text /
# placeholder / a `maxlength` attribute, NOT in labels. Phase 0 captures ONLY the common explicit
# patterns + maxlength; the ambiguous-helper-copy read is deferred to a future claude -p pass via
# the marked `_llm_constraint_read` hook (no-op here).
# ---------------------------------------------------------------------------

# "200-400 words", "200 - 400 words", "200 to 400 words" (en-dash, em-dash, hyphen, or 'to').
_WORD_RANGE = re.compile(
    r"\b(\d{1,5})\s*(?:[-–—]|to)\s*(\d{1,5})\s*words?\b", re.IGNORECASE)
# "max 500 characters", "maximum 500 chars", "up to 500 characters", "500 character max".
_MAX_WORDS = re.compile(
    r"(?:max(?:imum)?|up to|no more than|at most)\s*(\d{1,6})\s*words?\b", re.IGNORECASE)
_MAX_CHARS = re.compile(
    r"(?:max(?:imum)?|up to|no more than|at most)\s*(\d{1,7})\s*(?:characters?|chars?)\b",
    re.IGNORECASE)
_MIN_WORDS = re.compile(
    r"(?:min(?:imum)?|at least|no fewer than)\s*(\d{1,5})\s*words?\b", re.IGNORECASE)
# trailing "(500 characters)" / "500 char limit" forms
_CHAR_LIMIT_TAIL = re.compile(
    r"\b(\d{1,7})\s*(?:characters?|chars?)\s*(?:max(?:imum)?|limit)?\b", re.IGNORECASE)


def _llm_constraint_read(helper_text: str):  # noqa: ARG001
    """HOOK (Phase 0 NO-OP): a future `claude -p` pass reads AMBIGUOUS helper copy that the
    deterministic regex can't parse (e.g. "keep it brief — a paragraph or two", "roughly a page").

    Returns None in Phase 0 so the whole module is deterministic + offline-testable. DO NOT call an
    LLM here yet (feedback_background_work_on_plan_not_api: when this is wired, it MUST go through
    `claude -p`, never the metered API). The reconcile/compliance phases consume `constraints`; an
    empty constraint set is the safe default (no length gate) — never a fabricated limit.
    """
    return None


def scrape_constraints(helper_text: str = "", placeholder: str = "",
                       maxlength=None) -> Dict[str, object]:
    """Capture STATED length limits for one field from its DOM-visible copy. DETERMINISTIC only.

    Returns a dict with any of:
      {"words": [lo, hi]}    a stated word RANGE ("200-400 words")
      {"words_max": n}       a stated word maximum ("max 400 words")
      {"words_min": n}       a stated word minimum ("at least 150 words")
      {"chars_max": n}       a stated character maximum (a `maxlength` attr OR "max 500 characters")
    Empty {} when nothing is stated (the safe default — no length gate downstream).

    Sources, in priority order:
      1. `maxlength` attribute (the hardest, most reliable signal) -> chars_max.
      2. helper text + placeholder, regex-scanned for word ranges / word & char maxima / minima.
    A future ambiguous-copy read is left to `_llm_constraint_read` (no-op in Phase 0)."""
    out: Dict[str, object] = {}

    # 1) maxlength attribute — deterministic, authoritative for chars_max.
    if maxlength is not None:
        try:
            ml = int(str(maxlength).strip())
            if ml > 0:
                out["chars_max"] = ml
        except (TypeError, ValueError):
            pass

    blob = " ".join(t for t in (helper_text or "", placeholder or "") if t).strip()
    if blob:
        m = _WORD_RANGE.search(blob)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo <= hi:
                out["words"] = [lo, hi]
        # only record a standalone max/min when there is no full range (the range already pins both)
        if "words" not in out:
            mx = _MAX_WORDS.search(blob)
            if mx:
                out["words_max"] = int(mx.group(1))
            mn = _MIN_WORDS.search(blob)
            if mn:
                out["words_min"] = int(mn.group(1))
        # char max from copy only if no maxlength attr already set it (the attr is more reliable)
        if "chars_max" not in out:
            cm = _MAX_CHARS.search(blob)
            if cm:
                out["chars_max"] = int(cm.group(1))
            else:
                ct = _CHAR_LIMIT_TAIL.search(blob)
                if ct:
                    out["chars_max"] = int(ct.group(1))

    # Future hook (no-op in Phase 0): let a claude -p pass add anything the regex missed, but never
    # let it OVERRIDE a deterministically-captured value.
    extra = _llm_constraint_read(blob)
    if isinstance(extra, dict):
        for k, v in extra.items():
            out.setdefault(k, v)

    return out


# ---------------------------------------------------------------------------
# Helper-text discovery — find the descriptive copy near a field (helper/description/hint).
# Best-effort DOM walk; '' when none. Kept here (not in completeness) because it is constraint-
# oriented, not completeness-oriented.
# ---------------------------------------------------------------------------

# class fragments that conventionally hold helper/description copy on the supported ATSs
_HELPER_CLASS_HINTS = ("help", "hint", "description", "subtext", "sublabel",
                       "field-description", "char-count", "word-count", "caption")


def field_helper_text(page, el) -> str:
    """Best-effort helper/description text associated with a field, for constraint scraping.

    Looks at: aria-describedby targets, then sibling/nearby elements whose class hints at helper
    copy within the field's container. Returns the joined visible text, or '' if none. Never
    raises — helper text is supplemental context, not load-bearing."""
    texts: List[str] = []
    # 1) aria-describedby -> the element(s) that describe this field.
    try:
        described = el.get_attribute("aria-describedby") or ""
        for did in described.split():
            d = page.query_selector(f'[id="{did}"]')
            if d:
                t = (d.inner_text() or "").strip()
                if t:
                    texts.append(t)
    except Exception:
        pass
    # 2) helper-class elements: inside the field's container AND immediately-following siblings of
    #    the field itself (many ATSs render the helper as a sibling right after the input, with no
    #    wrapping container — e.g. <textarea>...</textarea><div class="field-description">).
    try:
        hint_sel = ",".join(f'[class*="{h}"]' for h in _HELPER_CLASS_HINTS)
        found = el.evaluate(
            "(e, sel) => {"
            " const out = [];"
            " const c = e.closest('div, fieldset, li, .field-wrapper, .application-question, "
            "  .field, p');"
            " if (c) { c.querySelectorAll(sel).forEach(n => {"
            "   const t=(n.innerText||'').trim(); if (t) out.push(t); }); }"
            " let n = e.nextElementSibling;"  # walk forward across a few siblings
            " for (let i=0; i<3 && n; i++){"
            "   if (n.matches && n.matches(sel)) { const t=(n.innerText||'').trim();"
            "     if (t) out.push(t); }"
            "   n = n.nextElementSibling; }"
            " return out.join(' '); }",
            hint_sel)
        if found:
            texts.append(found.strip())
    except Exception:
        pass
    return " ".join(dict.fromkeys(t for t in texts if t)).strip()
