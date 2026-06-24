# -*- coding: utf-8 -*-
"""reconcile_form — diff a live FormSpec against the staged record (Phase 0, design doc §8.4 / G1).

WHAT THIS IS: the diff half of the live-form model. Given a `FormSpec` (the live form, read by
`adapter.enumerate_fields`) and a `staged_record` (the flat manifest entry the orchestrator wrote:
`custom_qs`, `needs_sam`, `work_auth`, `filled_fields`, `uploaded_docs`), classify every staged
answer and every live field into one of:

  * matched               — a staged answer maps cleanly to a real live field (shapes agree).
  * mismatched            — a staged value exists but the live field wants a DIFFERENT shape
                            (e.g. a 253-char narrative staged for a field whose label is
                            "Current employer" / a short text). Carries needs_human_or_llm.
  * missing_live_field    — staged has an answer but NO live field for it (e.g. cover content with
                            no cover upload field). This is STRUCTURAL, not a failure (G7).
  * unfilled_required_live — a live REQUIRED field with no staged answer.

THE BUILD-vs-CHECK SPLIT (design doc §8.3, M3): the AMBIGUOUS mapping decision — "does this prose
map to employer+title?" — is REASONING, not a deterministic scrape. Phase 0 does NOT guess-map it.
A mismatch is returned with `needs_human_or_llm` + the evidence (the staged value, the live field
label/kind/constraints); a LATER phase wires the `claude -p` mapper that decides the ambiguous case.
This honors the live-dom rule (feedback_apply_engine_live_dom_and_empty_guard): never assert a
mapping the engine can't verify.

ADDITIVE / PURE (Phase 0 hard rule): this takes a FormSpec + a staged dict and RETURNS data. It
reads no page, writes no manifest, auto-corrects nothing. No LLM call, no network — fully offline-
testable.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .form_spec import FormSpec, FieldSpec


# Heuristic: how long a staged answer can be before it's "narrative" (prose), which a SHORT live
# field (a short_text / native_select / a label like 'employer'/'title'/'name') cannot hold. This
# is the 253-char-narrative-into-"employer" case the 2026-06-11 live runs exposed. Deterministic
# threshold only — the BORDERLINE decision is what gets escalated to the future claude -p mapper.
_NARRATIVE_CHARS = 120
# Labels whose live field is structurally SHORT (a few words), so a narrative staged value is a
# clear shape mismatch. Matched as whole-word-ish substrings of the normalized label.
_SHORT_FIELD_HINTS = (
    "employer", "company", "current title", "job title", "title",
    "city", "state", "country", "zip", "postal", "phone",
    "first name", "last name", "full name", "legal name", "name",
    "start date", "date", "salary", "years",
)
# Widget kinds that are inherently SHORT/constrained (a prose answer can't go here cleanly).
_SHORT_KINDS = ("native_select", "react_select", "radio", "checkbox", "text", "combobox", "file")


def _norm(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())


def _norm_words(s: str) -> str:
    """Lowercased, single-spaced — for substring label matching that respects word boundaries
    better than the alnum-collapse (so 'title' doesn't match inside 'entitlement')."""
    return " ".join((s or "").lower().split())


@dataclass
class FieldOutcome:
    """One row of the reconciliation. `classification` is the verdict; the rest is evidence so a
    human or the future claude -p mapper can act without re-reading the page."""
    classification: str          # matched | mismatched | missing_live_field | unfilled_required_live
    staged_label: str = ""       # the staged question/field label (the answer side), '' if none
    live_label: str = ""         # the matched/contested live field label, '' if none
    live_key: str = ""           # the live field key (for a future remap), '' if none
    widget_kind: str = ""        # the live field's widget kind, '' if none
    staged_value: str = ""       # the staged answer text (truncated evidence), '' if none
    needs_human_or_llm: bool = False   # True for mismatched: don't guess-map; defer the decision
    structural: bool = False     # True for missing_live_field: NOT a failure (G7)
    constraints: Dict[str, object] = field(default_factory=dict)  # live field's stated limits
    reason: str = ""             # one-line human explanation of the verdict


@dataclass
class ReconcileResult:
    """The whole diff. Lists are independent; a consumer reads whichever it cares about. `clean`
    is a convenience: no mismatches and no unfilled-required (structural missing fields are fine)."""
    matched: List[FieldOutcome] = field(default_factory=list)
    mismatched: List[FieldOutcome] = field(default_factory=list)
    missing_live_field: List[FieldOutcome] = field(default_factory=list)
    unfilled_required_live: List[FieldOutcome] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.mismatched and not self.unfilled_required_live

    def all_outcomes(self) -> List[FieldOutcome]:
        return (self.matched + self.mismatched
                + self.missing_live_field + self.unfilled_required_live)

    def to_record(self) -> Dict[str, object]:
        """COMPACT serializable summary for the staged manifest record (Phase 4b capture). Stores
        the `clean` bool (what `finish._g1_reconcile_ok` gates on) + the counts + the actionable
        lists with evidence (mismatched / unfilled_required_live). `escalations` is the union of the
        two not-clean lists rendered as short reasons — the gate reads `len(escalations)` for the
        human count, and a future claude -p mapper / the dashboard read the structured rows.
        Matched rows are summarized by count only (they're the non-actionable majority)."""
        def _row(o: "FieldOutcome") -> Dict[str, object]:
            return {
                "classification": o.classification,
                "staged_label": o.staged_label,
                "live_label": o.live_label,
                "live_key": o.live_key,
                "widget_kind": o.widget_kind,
                "staged_value": o.staged_value,
                "needs_human_or_llm": bool(o.needs_human_or_llm),
                "structural": bool(o.structural),
                "constraints": dict(o.constraints or {}),
                "reason": o.reason,
            }

        mismatched = [_row(o) for o in self.mismatched]
        unfilled = [_row(o) for o in self.unfilled_required_live]
        return {
            "clean": bool(self.clean),
            "n_matched": len(self.matched),
            "n_mismatched": len(self.mismatched),
            "n_missing_live_field": len(self.missing_live_field),
            "n_unfilled_required_live": len(self.unfilled_required_live),
            "mismatches": mismatched,            # the G1 hook's fallback count key
            "unfilled_required_live": unfilled,
            "missing_live_field": [_row(o) for o in self.missing_live_field],  # structural (G7) — informational
            # `escalations`: the union of the actionable not-clean rows, as short reasons. The G1
            # gate reads len(escalations) for its human-facing count.
            "escalations": [o.reason for o in (self.mismatched + self.unfilled_required_live)],
        }


# ---------------------------------------------------------------------------
# Staged-answer extraction — read the manifest record into a uniform list of (label, value, kind).
# ---------------------------------------------------------------------------

@dataclass
class _StagedAnswer:
    label: str
    value: str
    kind: str        # the staged kind (essay/short_text/select/react_select/checkbox_group/...)
    is_doc: bool = False   # True for a staged document (resume/cover) vs a form question


def _staged_answers(record: dict) -> List[_StagedAnswer]:
    """Flatten the staged record into the answers we can reconcile against live fields.

    Sources on the record (from staged_manifest.build_record):
      * custom_qs (generated): [{"q": label, "kind": ..., "value": ..., "status": ...}, ...]
      * work_auth: [{"field": ..., "q": label, "answer": ...}, ...]  (the answered guard set)
      * uploaded_docs: [{"kind": "resume"|"cover", ...} | "resume" | "cover", ...]  (docs staged)
    filled_fields are standard mapped fields (name/email/...) — they always have a live field by
    construction, so they're not a reconcile risk; we don't enumerate them as answers here."""
    out: List[_StagedAnswer] = []
    for q in (record.get("custom_qs") or []):
        if not isinstance(q, dict):
            continue
        label = (q.get("q") or q.get("label") or "").strip()
        if not label:
            continue
        val = q.get("value")
        if val is None and q.get("values"):
            val = ", ".join(str(v) for v in q.get("values"))
        out.append(_StagedAnswer(label=label, value="" if val is None else str(val),
                                 kind=str(q.get("kind") or "")))
    for w in (record.get("work_auth") or []):
        if not isinstance(w, dict):
            continue
        label = (w.get("q") or "").strip()
        if not label:
            continue
        out.append(_StagedAnswer(label=label, value=str(w.get("answer") or ""),
                                 kind="work_auth"))
    for d in (record.get("uploaded_docs") or []):
        kind = ""
        if isinstance(d, dict):
            kind = str(d.get("kind") or d.get("type") or "").lower()
        elif isinstance(d, str):
            kind = d.lower()
        if "resume" in kind or "cv" in kind:
            out.append(_StagedAnswer(label="resume", value="resume", kind="resume", is_doc=True))
        elif "cover" in kind:
            out.append(_StagedAnswer(label="cover", value="cover", kind="cover", is_doc=True))
    return out


def _label_is_short_field(label: str) -> bool:
    nl = _norm_words(label)
    return any(h in nl for h in _SHORT_FIELD_HINTS)


def _find_live_field(spec: FormSpec, staged: _StagedAnswer) -> Optional[FieldSpec]:
    """Find the live field a staged answer corresponds to. Exact normalized-label match first,
    then a containment match (one label inside the other) — the SAME bidirectional containment
    drop_answered uses, so reconciliation agrees with the completeness logic. Returns the field or
    None (None => missing_live_field for a question, or a structural doc absence)."""
    sl = _norm(staged.label)
    if not sl:
        return None
    # exact normalized-label match
    for f in spec.fields:
        if _norm(f.label) == sl:
            return f
    # containment (label_for can return slightly different text live vs. staged)
    for f in spec.fields:
        fl = _norm(f.label)
        if fl and (sl in fl or fl in sl):
            return f
    return None


def _doc_field_present(spec: FormSpec, doc_kind: str) -> bool:
    if doc_kind == "resume":
        return spec.has_resume_field
    if doc_kind == "cover":
        return spec.has_cover_field
    return any(f.widget_kind == "file" for f in spec.fields)


def _is_shape_mismatch(staged: _StagedAnswer, live: FieldSpec) -> bool:
    """A staged value is a SHAPE mismatch for a live field when a narrative (prose) answer is
    staged for a field that is structurally short — a short label ('employer'/'title') or a
    constrained widget (select/radio/short text). This is the 253-char-narrative-into-'employer'
    case. The BORDERLINE call (is THIS prose really an employer+title split?) is deferred — we only
    flag the clear shape conflict and hand it to needs_human_or_llm; we never auto-remap."""
    val = staged.value or ""
    long_answer = len(val) >= _NARRATIVE_CHARS or "\n" in val.strip()
    if not long_answer:
        return False
    # a long answer is fine in a textarea/essay live field (that's where prose belongs)
    if live.widget_kind == "textarea":
        return False
    short_target = (live.widget_kind in _SHORT_KINDS) or _label_is_short_field(live.label)
    return bool(short_target)


def _truncate(s: str, n: int = 160) -> str:
    s = s or ""
    return s if len(s) <= n else (s[: n - 1] + "…")


def reconcile_form(form_spec: FormSpec, staged_record: dict) -> ReconcileResult:
    """Diff the live form (`form_spec`) against the staged answers (`staged_record`) and classify
    each. PURE: no page, no manifest write, no auto-correct, no LLM. See module docstring + the
    four classifications. The ambiguous mapping decision is NEVER guessed here — a shape mismatch
    is returned `needs_human_or_llm=True` with the evidence for a later claude -p mapper phase."""
    result = ReconcileResult()
    staged_record = staged_record or {}

    matched_live_keys = set()

    # ---- staged-answer side: matched / mismatched / missing_live_field ----
    for ans in _staged_answers(staged_record):
        if ans.is_doc:
            present = _doc_field_present(form_spec, ans.kind)
            if present:
                # find the actual file field for evidence
                live = next((f for f in form_spec.fields
                             if f.widget_kind == "file" and f.doc_kind == ans.kind), None)
                result.matched.append(FieldOutcome(
                    classification="matched", staged_label=ans.label,
                    live_label=live.label if live else ans.kind,
                    live_key=live.key if live else "", widget_kind="file",
                    staged_value=ans.kind,
                    reason=f"{ans.kind} document has a live upload field"))
                if live:
                    matched_live_keys.add(live.key)
            else:
                # no upload field for this doc — STRUCTURAL, not a failure (G7). A no-cover-field
                # form is handled downstream by an 'Additional Information' note, never reported
                # as a miss.
                result.missing_live_field.append(FieldOutcome(
                    classification="missing_live_field", staged_label=ans.label,
                    staged_value=ans.kind, structural=True,
                    reason=f"no live upload field for the {ans.kind} document (structural — "
                           f"use an Additional Information note, not a failure)"))
            continue

        live = _find_live_field(form_spec, ans)
        if live is None:
            # a staged answer with no live field for it. Structural (the form simply doesn't ask
            # this) — NOT a failure; a later phase decides whether the content belongs elsewhere.
            result.missing_live_field.append(FieldOutcome(
                classification="missing_live_field", staged_label=ans.label,
                staged_value=_truncate(ans.value), structural=True,
                reason="no live form field matches this staged answer"))
            continue

        matched_live_keys.add(live.key)
        if _is_shape_mismatch(ans, live):
            result.mismatched.append(FieldOutcome(
                classification="mismatched", staged_label=ans.label,
                live_label=live.label, live_key=live.key, widget_kind=live.widget_kind,
                staged_value=_truncate(ans.value), needs_human_or_llm=True,
                constraints=dict(live.constraints or {}),
                reason=("staged value looks like prose/narrative but the live field is short "
                        f"({live.widget_kind}; label '{live.label}') — mapping is ambiguous, "
                        "deferred to a human/LLM mapper (not guessed)")))
        else:
            result.matched.append(FieldOutcome(
                classification="matched", staged_label=ans.label,
                live_label=live.label, live_key=live.key, widget_kind=live.widget_kind,
                staged_value=_truncate(ans.value),
                constraints=dict(live.constraints or {}),
                reason="staged answer maps to a live field of compatible shape"))

    # ---- live side: unfilled_required_live (a required field nothing staged covers) ----
    # Build the set of staged labels (normalized) so we can tell which required live fields have
    # SOME staged answer pointing at them. We also treat any live field we already matched as
    # covered. File fields whose doc was staged are covered by the doc-match above.
    staged_label_norms = {_norm(a.label) for a in _staged_answers(staged_record)}
    # standard mapped fields (name/email/phone/...) are filled by the engine but not enumerated as
    # 'answers'; treat a live field whose key/label matches a filled_fields entry as covered so we
    # don't flag email/name as unfilled.
    # Expand each filled key into its normalized whole AND its underscore/hyphen-split tokens, so
    # the engine's SEMANTIC keys cover the live LABELS: `full_name` -> {fullname, full, name} so the
    # live "Name" / "_systemfield_name" field reads as covered. An exact-only compare missed these
    # (Ashby _systemfield_* labels differ from the engine's keys), false-flagging filled required
    # fields as unfilled -> a spurious human_blocker + Telegram page (JOB-296, 2026-06-18).
    filled = set()
    for f in (staged_record.get("filled_fields") or []):
        if _norm(str(f)):
            filled.add(_norm(str(f)))
        for tok in str(f).replace("-", "_").split("_"):
            if _norm(tok):
                filled.add(_norm(tok))
    # doc kinds uploaded (resume / cover) cover a required FILE field of that kind
    staged_doc_types = set()
    for d in (staged_record.get("uploaded_docs") or []):
        t = d.get("doc") if isinstance(d, dict) else d
        if _norm(str(t or "")):
            staged_doc_types.add(_norm(str(t)))

    def _covered_by_filled(fnorm: str, keynorm: str) -> bool:
        # substring match guarded at len>=4 so a real semantic key (name/email/linkedin/resume)
        # maps to its live label without a 2-3 char token matching by accident.
        for s in filled:
            if not s:
                continue
            if fnorm and (fnorm == s
                          or (len(s) >= 4 and s in fnorm)
                          or (len(fnorm) >= 4 and fnorm in s)):
                return True
            if keynorm and keynorm == s:
                return True
        return False

    for f in form_spec.required_fields():
        if f.key in matched_live_keys:
            continue
        fnorm = _norm(f.label)
        keynorm = _norm(f.key)
        # covered by a staged answer label, or by a standard filled field (by key or label)?
        if fnorm and any(fnorm == s or fnorm in s or s in fnorm
                         for s in staged_label_norms if s):
            continue
        if _covered_by_filled(fnorm, keynorm):
            continue
        # a required FILE field is covered by an uploaded doc of that kind (resume/cover) even when
        # the live file-field label/key (e.g. _systemfield_resume) didn't match the doc-match above.
        if f.widget_kind == "file" and any(
                dt and (dt in fnorm or dt in keynorm) for dt in staged_doc_types):
            continue
        result.unfilled_required_live.append(FieldOutcome(
            classification="unfilled_required_live", live_label=f.label, live_key=f.key,
            widget_kind=f.widget_kind, constraints=dict(f.constraints or {}),
            reason="live required field has no staged answer"))

    return result
