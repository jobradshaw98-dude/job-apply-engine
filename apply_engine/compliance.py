# -*- coding: utf-8 -*-
"""G2 form-constraint compliance — deterministic CHECK of staged answers vs the live form_spec.

WHAT THIS IS (design doc §8.2 / G2, the CHECK half of the BUILD-vs-CHECK split §8.3): given a
captured `FormSpec` (built by `adapter.enumerate_fields`, which already scraped each field's stated
length limits via `form_spec.scrape_constraints`) and the staged answers (the manifest record's
`custom_qs`/`generated`), validate every staged answer against the limit its live field STATES:

  * `words: [lo, hi]`  — a stated word RANGE -> FAIL when the answer is under `lo` or over `hi`.
  * `words_min: n`     — a stated minimum     -> FAIL when the answer is under `n`.
  * `words_max: n`     — a stated maximum     -> FAIL when the answer is over `n`.
  * `chars_max: n`     — a stated char cap (a `maxlength` attr or "max 500 characters")
                         -> FAIL when the answer exceeds `n` characters.

This is the "essay 150 vs stated 200-400" / "cover too long" catch from the 2026-06-11 live runs.
Under/over-length is BLOCK-class (it drives the convergence loop).

BUILD vs CHECK (§8.3, M3): the BUILD half — reading ambiguous word ranges out of helper copy — is a
`claude -p` reasoning job and lives upstream in `form_spec.scrape_constraints` /
`form_spec._llm_constraint_read` (a Phase-0 no-op hook). THIS module is the CHECK half: it is
purely deterministic, offline, and testable — it only compares an answer's measured length against an
already-captured numeric constraint. No LLM, no network, no page, no manifest write.

PASS-WHEN-ABSENT lives in the GATE (`finish._g2_compliance_ok`), not here. Here, a field with no
stated constraint simply contributes no violation (the safe default — never a fabricated limit, per
form_spec._llm_constraint_read's contract). An empty form_spec yields ok=True with zero violations.
"""
from dataclasses import dataclass, field
from typing import List, Optional

from .form_spec import FormSpec, FieldSpec


def _norm(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())


def count_words(text: str) -> int:
    """Whitespace-split word count. Deterministic; matches the way a word RANGE is stated to a
    human ("200-400 words" = 200-400 whitespace tokens). Empty/blank -> 0."""
    return len((text or "").split())


# Which violation kinds are too SHORT (need lengthening) vs too LONG (need tightening). A length
# fix is engine-fixable: regen to the stated range by ADDING supported detail (under) or CUTTING
# redundancy (over) — never by inventing. char-cap overflow is an "over" (tighten) case.
_UNDER_KINDS = ("words_under", "words_min")
_OVER_KINDS = ("words_over", "words_max", "chars_over")


@dataclass
class Violation:
    """One compliance breach, with enough evidence for the dashboard + the convergence loop to act
    (which field, which limit, the measured value). `kind` is the machine reason."""
    field_key: str
    label: str
    kind: str            # words_under | words_over | words_min | words_max | chars_over
    limit: object        # the stated limit ([lo,hi] / int)
    measured: int        # the answer's measured words/chars
    detail: str          # one-line human explanation

    def __str__(self) -> str:  # rendered into the gate's reason + the stored `violations` list
        return self.detail

    @property
    def direction(self) -> str:
        """'under' (too short → lengthen) or 'over' (too long → tighten). Drives which instruction
        the convergence loop builds for the regen."""
        return "under" if self.kind in _UNDER_KINDS else "over"

    def bounds(self) -> tuple:
        """(min_words, max_words) the answer must land within, derived from `limit`. A full word
        RANGE [lo, hi] pins both; a standalone min/max pins one side and leaves the other None; a
        char cap pins neither word bound (None, None — the regen tightens by chars, not words).
        Always returns a 2-tuple of int-or-None."""
        lim = self.limit
        if self.kind in ("words_under", "words_over") and isinstance(lim, (list, tuple)) and len(lim) == 2:
            try:
                return int(lim[0]), int(lim[1])
            except (TypeError, ValueError):
                return None, None
        if self.kind == "words_min":
            try:
                return int(lim), None
            except (TypeError, ValueError):
                return None, None
        if self.kind == "words_max":
            try:
                return None, int(lim)
            except (TypeError, ValueError):
                return None, None
        return None, None  # chars_over (no word bound) or unknown

    def to_finding(self) -> dict:
        """The structured, ROUTABLE finding the convergence loop consumes (kind='length'). Carries
        the question/field, the [min,max] word range, the current word count, and the direction so
        `converge.apply_own_fix` can build a lengthen/tighten instruction and `regen_answer` can
        re-check the range as part of its iterate-to-clean gate. `doc='essay_answer'` so it routes
        exactly like a fabrication ANSWER finding (to regen_answer, keyed by `question`)."""
        lo, hi = self.bounds()
        return {
            "kind": "length",
            "doc": "essay_answer",
            "question": self.label,
            "field": self.field_key,
            "vkind": self.kind,
            "range": [lo, hi],
            "current_words": int(self.measured),
            "direction": self.direction,
            "issue": self.detail,
        }


@dataclass
class ComplianceResult:
    """The whole G2 check. `ok` is True only when there are zero violations. `to_record()` is the
    compact serializable summary stored on the manifest record under `compliance` — the exact shape
    `finish._g2_compliance_ok` reads (`{"ok": bool, "violations": [str, ...]}`)."""
    ok: bool = True
    violations: List[Violation] = field(default_factory=list)

    def to_record(self) -> dict:
        return {
            "ok": bool(self.ok),
            "violations": [str(v) for v in self.violations],
            # keep the structured rows too (additive; the gate only needs ok + violations strings)
            "detail": [
                {"field_key": v.field_key, "label": v.label, "kind": v.kind,
                 "limit": v.limit, "measured": v.measured}
                for v in self.violations
            ],
        }

    def to_findings(self) -> List[dict]:
        """The routable length findings (one per violation) the convergence loop collects alongside
        fabrication/calibration BLOCKs. Each is engine-fixable by regen (kind='length'), NOT human-
        only — a length problem is reachable by adding supported detail / cutting redundancy."""
        return [v.to_finding() for v in self.violations]


def _staged_text_answers(record: dict):
    """Yield (label, value) for every staged FREE-TEXT answer that could violate a length limit.

    Sources mirror reconcile._staged_answers but we only care about answers that carry prose whose
    length matters: `custom_qs`/`generated` essay/short_text entries with a string value. Select /
    checkbox / react-select / work-auth answers are option picks, not length-bearing prose, so they
    can't breach a word/char limit and are skipped (a stray length limit on a select is meaningless).
    """
    for q in (record.get("custom_qs") or record.get("generated") or []):
        if not isinstance(q, dict):
            continue
        label = (q.get("q") or q.get("label") or "").strip()
        if not label:
            continue
        val = q.get("value")
        if not isinstance(val, str):
            continue
        kind = str(q.get("kind") or "").lower()
        # option-pick widgets don't carry length-bearing prose
        if kind in ("select", "react_select", "checkbox_group", "screening-yesno",
                    "screening", "work_auth"):
            continue
        yield label, val


def _find_field(form_spec: FormSpec, label: str) -> Optional[FieldSpec]:
    """Find the live field a staged answer's label corresponds to — exact normalized-label match
    first, then bidirectional containment (the SAME matching reconcile._find_live_field uses, so G1
    and G2 agree on which live field an answer points at). Returns the field or None."""
    sl = _norm(label)
    if not sl:
        return None
    for f in form_spec.fields:
        if _norm(f.label) == sl:
            return f
    for f in form_spec.fields:
        fl = _norm(f.label)
        if fl and (sl in fl or fl in sl):
            return f
    return None


def _check_one(field_spec: FieldSpec, value: str) -> Optional[Violation]:
    """Compare one staged `value` against one live field's stated constraints. Returns the FIRST
    violation found (most-specific first: a full word RANGE pins both bounds, so it's checked before
    the standalone min/max), or None when compliant / unconstrained. Deterministic."""
    c = field_spec.constraints or {}
    if not c:
        return None
    words = count_words(value)
    chars = len(value or "")

    # 1) word RANGE [lo, hi] — pins both bounds.
    rng = c.get("words")
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        try:
            lo, hi = int(rng[0]), int(rng[1])
        except (TypeError, ValueError):
            lo = hi = None
        if lo is not None:
            if words < lo:
                return Violation(
                    field_spec.key, field_spec.label, "words_under", [lo, hi], words,
                    f"'{field_spec.label}': answer is {words} words, under the stated minimum "
                    f"of {lo} (range {lo}-{hi} words)")
            if hi is not None and words > hi:
                return Violation(
                    field_spec.key, field_spec.label, "words_over", [lo, hi], words,
                    f"'{field_spec.label}': answer is {words} words, over the stated maximum "
                    f"of {hi} (range {lo}-{hi} words)")
    else:
        # 2) standalone word minimum / maximum (only when no full range pinned both).
        wmin = c.get("words_min")
        if isinstance(wmin, int) and words < wmin:
            return Violation(
                field_spec.key, field_spec.label, "words_min", wmin, words,
                f"'{field_spec.label}': answer is {words} words, under the stated minimum of {wmin}")
        wmax = c.get("words_max")
        if isinstance(wmax, int) and words > wmax:
            return Violation(
                field_spec.key, field_spec.label, "words_max", wmax, words,
                f"'{field_spec.label}': answer is {words} words, over the stated maximum of {wmax}")

    # 3) character cap (a maxlength attr or "max N characters"). Independent of the word checks.
    cmax = c.get("chars_max")
    if isinstance(cmax, int) and chars > cmax:
        return Violation(
            field_spec.key, field_spec.label, "chars_over", cmax, chars,
            f"'{field_spec.label}': answer is {chars} characters, over the stated maximum "
            f"of {cmax}")
    return None


def form_spec_from_summary(summary: dict) -> FormSpec:
    """Reconstruct a FormSpec from the COMPACT `form_spec` summary stored on a manifest record
    (FormSpec.to_summary()'s inverse, lossy: drops `selector`, which compliance doesn't need). Lets
    the G2 gate recompute compliance straight from a captured form_spec when no `compliance` block
    was stored. Defensive: a malformed summary yields an empty FormSpec (no constraints -> ok)."""
    spec = FormSpec()
    if not isinstance(summary, dict):
        return spec
    spec.ats = str(summary.get("ats") or "")
    spec.has_resume_field = bool(summary.get("has_resume_field"))
    spec.has_cover_field = bool(summary.get("has_cover_field"))
    for f in (summary.get("fields") or []):
        if not isinstance(f, dict):
            continue
        spec.fields.append(FieldSpec(
            key=str(f.get("key") or ""),
            label=str(f.get("label") or ""),
            required=bool(f.get("required")),
            widget_kind=str(f.get("widget_kind") or ""),
            doc_kind=str(f.get("doc_kind") or ""),
            constraints=dict(f.get("constraints") or {}),
        ))
    return spec


def check_record_compliance(record: dict) -> Optional[ComplianceResult]:
    """G2 from a manifest RECORD: rebuild the FormSpec from the record's stored `form_spec` summary
    and check the staged answers against it. Returns a ComplianceResult, or None when the record has
    no captured `form_spec` (the gate then pass-when-absent). Deterministic + pure (no page/LLM).

    This is what lets `finish._g2_compliance_ok` recompute compliance live from a captured form_spec
    even if no separate `compliance` block was stored — a single source of truth, no duplicated
    length logic."""
    if not isinstance(record, dict):
        return None
    summary = record.get("form_spec")
    if not isinstance(summary, dict) or not summary.get("fields"):
        return None
    spec = form_spec_from_summary(summary)
    return check_form_constraints(spec, record)


def check_form_constraints(form_spec: FormSpec, record: dict) -> ComplianceResult:
    """G2 — validate every staged free-text answer against its live field's stated length limit.

    DETERMINISTIC + PURE: takes a captured FormSpec + the staged record, returns a ComplianceResult.
    No LLM, no page, no manifest write. A field with no stated constraint contributes no violation
    (safe default). An answer with no matching live field is skipped here (that's G1's job —
    reconcile classifies a missing live field; G2 only judges length against a field that exists).

    Returns ok=True with zero violations when every constrained answer is within its limit (this is
    the converge target). The gate (`finish._g2_compliance_ok`) applies pass-when-absent on top."""
    result = ComplianceResult()
    if not isinstance(form_spec, FormSpec) or not isinstance(record, dict):
        return result  # nothing to check -> ok

    for label, value in _staged_text_answers(record):
        fld = _find_field(form_spec, label)
        if fld is None:
            continue  # no live field -> G1's concern, not a length violation
        v = _check_one(fld, value)
        if v is not None:
            result.violations.append(v)

    result.ok = not result.violations
    return result
