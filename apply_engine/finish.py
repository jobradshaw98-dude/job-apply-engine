# -*- coding: utf-8 -*-
"""Finish a STAGED application: re-open the form, deterministically re-fill the EXACT
stored answers, then either leave the browser on the review screen for Sam or click
the ATS submit control.

This is the ONLY module in the engine permitted to click a submit control, and only on
the `replay(submit=True)` path, behind two gates:

  1. `can_submit(record)` — a PURE safety gate run BEFORE the browser opens (fail fast)
     and AGAIN live just before the click. It refuses an already-submitted record, a
     BLOCKED fabrication audit, a not-review-ready status, any still-unfilled required
     field, and any work-auth answer that is not a no-red-flag answer.
  2. a live read-back VERIFY of the standard fields — a mismatch ABORTS without clicking.

Determinism is the core safety property: custom answers are re-filled FROM THE STORED
RECORD, never from a fresh LLM call. There is no `answer_fn` anywhere in this module.
"""
from pathlib import Path
from typing import Optional, Tuple

from .ats_detect import detect_ats, AtsKind
from .work_auth import (classify_work_auth, WorkAuthDecision,
                        verify_sponsorship_answer, WorkAuthVerify)
from .verify import verify_fields


# --------------------------------------------------------------------------------------
# 1. can_submit — PURE safety gate
# --------------------------------------------------------------------------------------

# (work_auth `field` value) -> the ONE answer that is a no-red-flag answer for it. These
# mirror the orchestrator's work-auth guard: sponsorship->No, authorized->Yes, combined
# "authorized without sponsorship"->Yes. Anything else (or a different answer) is refused.
_ALLOWED_WORK_AUTH = {
    "sponsor": "no",
    "sponsorship": "no",
    "authorized": "yes",
    "authorized_no_sponsorship": "yes",
}

_NOT_REVIEW_READY = {"needs_input", "needs_sam", "error"}


# Fields stored in the `work_auth` list that are NOT sponsorship/authorization questions and
# must be EXCLUDED from the red-flag verifier. `office_commitment` (RTO/in-person) is correctly
# answered "Yes" per feedback_office_commitment_answer — a different question class verified at
# fill time; running its bare "Yes" through verify_sponsorship_answer would falsely read it as a
# sponsorship red flag and block submit. Non-dict / unknown-field entries are deliberately KEPT
# so _work_auth_answer_ok still refuses malformed data (defensive).
_NON_WORK_AUTH_FIELDS = {"office_commitment"}


def _work_auth_entries(record: dict) -> list:
    """The stored work-auth answers, EXCLUDING non-sponsorship classes (office_commitment). The
    staged manifest writes them under `work_auth` (from JobOutcome.work_auth_answers) mixed with
    office-commitment answers; carve those out so a correct office "Yes" is never mistaken for a
    visa red flag. Everything else (genuine work-auth, review snippets, malformed non-dicts) is
    kept and judged by _work_auth_answer_ok."""
    raw = list(record.get("work_auth") or record.get("work_auth_answers") or [])
    return [e for e in raw
            if not (isinstance(e, dict) and (e.get("field") or "").strip().lower() in _NON_WORK_AUTH_FIELDS)]


def _work_auth_answer_ok(entry: dict) -> bool:
    """True if a single work-auth answer is a no-red-flag answer.

    Two shapes exist:
      * single-page guard: {field: sponsor|authorized|authorized_no_sponsorship, answer: Yes|No}
        — checked against the locked field->answer table.
      * multi-step (Workday) review-page verification: {field: "work_auth", answer: <snippet>}
        — the answer is a free-text review-page snippet, validated with the word-boundary
        `verify_sponsorship_answer` predicate (the same one the orchestrator gates on).
    """
    field = (entry.get("field") or "").strip().lower()
    answer = (entry.get("answer") or "").strip()
    if not answer:
        return False
    if field in _ALLOWED_WORK_AUTH:
        return answer.lower() == _ALLOWED_WORK_AUTH[field]
    # review-page snippet (field == "work_auth" or anything else): require a verified PASS.
    return verify_sponsorship_answer(answer) == WorkAuthVerify.PASS


def _had_unanswered_work_auth(record: dict) -> bool:
    """Heuristic: did this record have a work-auth question that was NOT answered? The
    only signals available on a flat record are the human-facing reason fields, which the
    orchestrator fills with the literal question text on a work-auth HALT. We refuse if any
    of those reasons classifies as a real work-auth question — a work-auth question slipped
    through to Sam means it was never given a no-red-flag answer."""
    reasons = list(record.get("needs_sam") or [])
    halt = record.get("halt_reason") or ""
    if halt:
        reasons.append(halt)
    for r in reasons:
        if classify_work_auth(str(r)) != WorkAuthDecision.UNRELATED:
            return True
    return False


def can_submit(record: dict) -> Tuple[bool, str]:
    """PURE safety gate. Return (True, "") only if every condition for a safe submit holds;
    otherwise (False, <human-readable reason>). No I/O, no clock, no browser."""
    if not isinstance(record, dict):
        return False, "no staged record to submit"

    if record.get("submitted"):
        return False, "already submitted"

    # AUTOMATED CONTENT GATE = the DETERMINISTIC gate ONLY (2026-06-22, Sam's call). The LLM
    # accuracy-review + holistic quality judge were demoted from required submit-blockers to
    # advisory/on-demand: the hardened engine already produces clean, self-critiqued content, the
    # cheap deterministic gate is the hard backstop (and re-runs on every per-element edit), and
    # Sam's own review is the quality gate. So a stale or absent LLM verdict no longer blocks —
    # only a live DETERMINISTIC gate block (forbidden phrase / fabrication pattern on the current
    # answers) does. This removes the quota burn + the stale-verdict batch-restage treadmill while
    # keeping the part that actually catches junk. (LLM judges still run on-demand via the
    # dashboard's "re-run accuracy review" button; their verdicts are advisory display only.)
    audit = record.get("audit") if isinstance(record.get("audit"), dict) else None
    # FAIL-CLOSED on a missing stamp (2026-06-22 reviewer fix): an absent `audit` means the
    # deterministic gate NEVER RAN on this record — not that it passed. Reading gate_blocks as
    # 0-by-default here would auto-submit a never-checked package (zero-question stage, or a stage
    # where refresh raised/errored). The deterministic gate IS the gate now, so it must have run.
    if audit is None:
        return False, ("the accuracy gate hasn't run on this application yet — re-stage it before "
                       "submitting (a missing stamp means the gate never ran, not that it passed)")
    n_gate = int(audit.get("gate_blocks", 0) or 0)
    if n_gate > 0:
        return False, (f"deterministic gate blocked {n_gate} fabrication-class finding"
                       + ("s" if n_gate != 1 else "")
                       + " on the current answers — fix via the edit loop before submitting")

    status = record.get("status") or ""
    if status in _NOT_REVIEW_READY:
        return False, f"status is {status!r}, not review-ready"

    unfilled = list(record.get("unfilled_required") or record.get("needs_sam") or [])
    if unfilled:
        return False, f"{len(unfilled)} required field(s) still need Sam: " \
                      + "; ".join(str(u) for u in unfilled[:8])

    # work-auth: every stored answer must be a no-red-flag answer, and no work-auth
    # question may have been left unanswered (slipped through to Sam).
    entries = _work_auth_entries(record)
    for e in entries:
        if not isinstance(e, dict) or not _work_auth_answer_ok(e):
            saw = e.get("answer") if isinstance(e, dict) else e
            return False, f"work-auth answer is not a no-red-flag answer (saw: {saw!r})"
    if _had_unanswered_work_auth(record):
        return False, "a work-auth question was left unanswered — needs Sam, never auto-submit"

    return True, ""


def _lingering_edit_request(record: dict) -> bool:
    """True if any stored custom_q still carries a non-empty `edit_request` — an AI rewrite that
    is in flight / awaiting re-review (regen_answer sets it when a dashboard regen launches and
    clears it on every terminal outcome). Submitting on top of an unsettled edit would ship a
    half-applied answer, so the invariant gate blocks it. A Sam-provided answer deliberately
    leaves edit_request empty, so it never trips this."""
    for q in (record.get("custom_qs") or record.get("generated") or []):
        if isinstance(q, dict) and (q.get("edit_request") or "").strip():
            return True
    return False


def verify_submittable(record: dict, config) -> Tuple[bool, list]:
    """THE DETERMINISTIC PRE-SUBMIT INVARIANT GATE (2026-06-10).

    One place that asserts EVERY invariant a safe submit requires and returns ALL failing reasons
    (named, human-readable) — so a master-resume attach, a missing quality audit, or any other
    bug-class is caught LOUDLY at submit time and by the contract test, instead of being discovered
    months later. Returns (True, []) only when every invariant holds.

    Invariants asserted (each contributes its own reason on failure; none short-circuits the rest):
      1. A TAILORED resume PDF resolves from the staged record AND exists on disk AND is NOT the
         master file. (This is the invariant that would have caught FINDING #1 — the master-attach.)
      2. If a cover is part of the package (an uploaded_docs cover entry), the cover PDF exists.
      3. The fabrication audit verdict == PASS and judge_ran is not False.
      4. The quality audit verdict in {PASS, FLAG} and judge_ran is not False (a missing/None
         quality_audit fails here with a clear reason — the FINDING #3 wedge).
      5. Work-auth answers carry no red flag.
      6. No unfilled required fields, no work-auth question left for Sam, and no custom_q still
         carrying an in-flight edit_request.

    Invariants 3-6 reuse the EXACT logic in can_submit (the single source of truth for those
    checks) — this ADDS the PDF-integrity invariant (1-2) and the edit_request check (6) that
    can_submit lacked, and collects every reason rather than returning only the first. `config`
    supplies PKG_DIR for the PDF resolution; PURE otherwise (no browser, no LLM, no clock)."""
    reasons: list = []

    if not isinstance(record, dict):
        return False, ["no staged record to submit"]

    # ---- INVARIANT 1: a tailored (non-master) resume PDF must resolve + exist on disk ----
    # Resolution mirrors _resolve_pdfs (registered uploaded_docs path, then the sibling-dir tailored
    # fallback) so the gate asserts the EXACT file that would attach — not a stricter view that would
    # false-block a resume only resolvable via the fallback.
    rp = _resolve_doc_pdf(record, "resume")
    if not rp:
        reasons.append("PDF integrity: no resume in the staged package (uploaded_docs has no "
                       "resume entry and no tailored resume on disk); cannot prove a tailored "
                       "resume would attach")
    elif _is_master_resume(rp):
        reasons.append("PDF integrity: the staged resume is the GENERIC MASTER resume "
                       f"({Path(rp).name}), not a tailored one; refusing to submit the master")
    elif not Path(rp).exists():
        reasons.append("PDF integrity: the staged tailored resume PDF does not exist on disk "
                       f"({rp}); refusing to submit without it")

    # ---- INVARIANT 2: a BUILT tailored cover must resolve + exist so it actually attaches ----
    # Two cases, kept distinct so a no-cover role is never wedged:
    #   * cover was tailored (cover dict with paragraphs, or a tailored cover PDF resolves) → the
    #     cover PDF MUST resolve + exist. A built-but-unresolvable cover BLOCKS (would otherwise be
    #     silently dropped, submitting resume-only and breaching the quality contract).
    #   * no cover was tailored (role wants none) → nothing required here.
    cp = _resolve_doc_pdf(record, "cover")
    if _cover_was_tailored(record):
        if not cp:
            reasons.append("PDF integrity: a tailored cover letter was built for this application "
                           "but no cover PDF could be resolved to attach; refusing to submit "
                           "resume-only and drop the tailored cover (rebuild the cover PDF)")
        elif not Path(cp).exists():
            reasons.append("PDF integrity: a tailored cover letter was built but its PDF does not "
                           f"exist on disk ({cp}); refusing to submit without attaching it")
    elif cp and not Path(cp).exists():
        # not tailored, but a stale uploaded_docs cover entry points at a missing file → still flag.
        reasons.append(f"PDF integrity: the staged cover PDF does not exist on disk ({cp})")

    # ---- INVARIANTS 3-6 (audits, work-auth, unfilled, status): reuse can_submit verbatim ----
    ok, why = can_submit(record)
    if not ok and why:
        reasons.append(why)

    # ---- INVARIANT 6 (extra): no in-flight answer edit awaiting re-review ----
    if _lingering_edit_request(record):
        reasons.append("an answer edit is still in flight (a custom question carries an "
                       "edit_request); wait for it to settle and re-review before submitting")

    return (len(reasons) == 0), reasons


# --------------------------------------------------------------------------------------
# 1b. verify_ready — THE SINGLE READINESS AUTHORITY (M4, Phase 4a)
# --------------------------------------------------------------------------------------
#
# verify_ready is a STRICT SUPERSET of verify_submittable. It is the one place that decides a
# staged package is fully ready (the convergence loop's "converged" stop condition gates on it,
# §3/§8.4 of AUTONOMOUS_CONVERGENCE_AND_COMM_CHANNEL.md). It holds True ONLY when ALL of:
#
#     verify_submittable PASS  AND  can_submit (True, "")  AND  zero fabrication/calibration
#     BLOCK findings  AND  G1 reconciliation clean  AND  G2 form-constraint compliance PASS
#     AND  G3 cover-length PASS
#
# verify_submittable already folds in can_submit and the fabrication/quality verdicts. verify_ready
# ADDS the three Phase-0/4b field-learning gates (G1/G2/G3) ON TOP, plus an explicit zero-BLOCK
# check on the fabrication+calibration finding counts so a record can never read "ready" while a
# BLOCK-class finding is still outstanding.
#
# PHASE 4a — THE G-HOOKS ARE PASS-WHEN-ABSENT STUBS. G1/G2/G3 data (the live-form reconciliation
# result, the form-constraint compliance check, the cover auto-fit adjustment count) is populated
# by Phase 4b. Until then, each G-hook returns PASS WHEN ITS DATA IS ABSENT — so today verify_ready
# behaves as a strict superset of verify_submittable that NEVER regresses an existing ready card,
# yet is already wired so 4b only fills in the hook bodies (the call sites don't change). The hooks
# are named + separately testable on purpose.


def _g1_reconcile_ok(record: dict) -> Tuple[bool, str]:
    """G1 — live-form reconciliation clean (design doc §8.2, BLOCK class for mis-mapped fields).

    Phase 4b STORES a reconciliation result (the `reconcile_form` diff of the live form vs the
    staged answers — `ReconcileResult.to_record()`) on the record under `reconcile`. This hook
    fails CLOSED when that diff is NOT clean:
      * any `mismatched` entry  — a staged answer that doesn't fit the live field (e.g. a 253-char
        narrative into a short 'employer' field). The ambiguous remap is DEFERRED (reconcile flagged
        needs_human_or_llm); G1 only reports it as not-clean, never auto-remaps (live-dom rule).
      * any `unfilled_required_live` entry — a live REQUIRED field with no staged answer.
    A `missing_live_field` that is STRUCTURAL (e.g. cover content with no cover upload field, G7)
    does NOT fail — those are excluded from the lists below and from `clean` by construction.

    PASS-WHEN-ABSENT (Phase 4a contract, preserved): no `reconcile` block (None / not a dict / a
    garbled `clean`) -> PASS. So a record captured before 4b — or one whose best-effort capture was
    skipped — passes verify_ready exactly as before; no existing ready card regresses."""
    rec = record.get("reconcile") if isinstance(record, dict) else None
    if not isinstance(rec, dict):
        return True, ""  # pass-when-absent: no live-form reconciliation captured yet (Phase 4b)

    # Read the actionable lists directly (the ReconcileResult.to_record() shape). A mismatched OR an
    # unfilled_required_live entry FAILS; structural missing_live_field is intentionally NOT here.
    mismatched = rec.get("mismatches") or rec.get("mismatched") or []
    unfilled = rec.get("unfilled_required_live") or []
    if mismatched or unfilled:
        nm, nu = len(mismatched), len(unfilled)
        parts = []
        if nm:
            parts.append(f"{nm} mis-mapped field" + ("s" if nm != 1 else ""))
        if nu:
            parts.append(f"{nu} required live field" + ("s" if nu != 1 else "") + " with no answer")
        return False, "G1 live-form reconciliation is not clean: " + ", ".join(parts)

    # No actionable lists present -> fall back to the `clean` bool (the authoritative summary).
    if rec.get("clean") is False:
        n = len(rec.get("escalations") or [])
        detail = f" ({n} unresolved field mapping issue" + ("s" if n != 1 else "") + ")" if n else ""
        return False, f"G1 live-form reconciliation is not clean{detail}"
    # `clean` True, or neither True/False with no lists (partial/garbled) -> PASS (pass-when-absent).
    return True, ""


def _g2_compliance_ok(record: dict) -> Tuple[bool, str]:
    """G2 — form-constraint compliance (design doc §8.2: per-question word ranges, char caps; an
    answer under a stated minimum / over a stated maximum is BLOCK-class and drives the loop).
    This is the essay-too-short / cover-too-long catch from the 2026-06-11 live runs.

    Phase 4b STORES the compliance check on the record under `compliance` (the deterministic
    `compliance.check_form_constraints` output against the captured live `form_spec` — an `ok` bool
    + a `violations` list). This hook fails CLOSED on any violation, NAMING the field + the breach.

    Two read paths (single source of truth — the length logic lives only in `compliance.py`):
      1. a stored `compliance` block (the capture computed it) — read `ok` + `violations`;
      2. no `compliance` block but a captured `form_spec` is present — RECOMPUTE deterministically
         via `compliance.check_record_compliance(record)` so a record carrying only the form model
         is still gated (no duplicated length code).

    PASS-WHEN-ABSENT (Phase 4a contract, preserved): neither a `compliance` block nor a usable
    `form_spec` -> PASS. So a pre-4b / capture-skipped record passes verify_ready unchanged."""
    if not isinstance(record, dict):
        return True, ""

    # LIVE-FIRST (feedback_no_derived_fields_in_state): recompute compliance from the captured
    # form_spec + the CURRENT answers and gate on THAT — never on the stored `compliance` field,
    # which goes STALE the moment a length/content fix rewrites an answer (the 2026-06-12 bug:
    # the loop lengthened "Why Anthropic?" into range but the stored compliance still read the old
    # under-length count, so verify_ready never acknowledged the fix -> false "exhausted"). The
    # stored block is kept only for display, and used here ONLY as a fallback when there is no
    # form_spec to recompute from (legacy / pre-4b records).
    from .compliance import check_record_compliance
    res = check_record_compliance(record)
    if res is not None:
        comp = res.to_record()                 # live wins, always fresh
    else:
        comp = record.get("compliance")         # no form_spec -> fall back to stored block
        if not isinstance(comp, dict):
            return True, ""                      # pass-when-absent (Phase 4a contract)

    if comp.get("ok") is True:
        return True, ""
    if comp.get("ok") is False:
        viol = comp.get("violations") or []
        detail = ": " + "; ".join(str(v) for v in viol[:4]) if viol else ""
        return False, f"G2 form-constraint compliance failed{detail}"
    return True, ""  # `ok` neither True nor False -> treat as absent (pass-when-absent)


def _g3_cover_ok(record: dict) -> Tuple[bool, str]:
    """G3 — cover length / auto-fit (design doc §8.2: the renderer's auto-fit adjustment count;
    >0 adjustments means the cover was silently shrunk to fake one page → BLOCK, re-draft shorter).

    Phase 4b will surface the renderer's auto-fit adjustment count on the record (build.py already
    logs `[auto-fit] … N adjustment(s)`; 4b returns N as a value). When present, this hook fails
    CLOSED if the count is > 0 (the shrunk PDF must never ship).

    PASS-WHEN-ABSENT (Phase 4a): no cover auto-fit data yet → PASS. Reads
    `record['cover']['autofit_adjustments']` (an int); 4b populates it. A role with no cover is
    trivially compliant (absent → PASS)."""
    cover = record.get("cover") if isinstance(record, dict) else None
    if not isinstance(cover, dict) or "autofit_adjustments" not in cover:
        return True, ""  # pass-when-absent: no auto-fit adjustment count captured yet (Phase 4b)
    try:
        n = int(cover.get("autofit_adjustments") or 0)
    except (TypeError, ValueError):
        return True, ""  # garbled -> treat as absent (pass-when-absent)
    if n > 0:
        return False, (f"G3 cover length: the cover was auto-fit-shrunk ({n} adjustment"
                       + ("s" if n != 1 else "") + ") to fake one page; re-draft shorter")
    return True, ""


def _fab_calib_block_count(record: dict) -> int:
    """Count outstanding fabrication + calibration BLOCK-class findings on the record's fabrication
    audit. (The quality 4-dim polish critic's FLAGs are advisory and NEVER counted — they don't
    block can_submit and never converge; see feedback_apply_quality_once_and_calibration.) Reads
    the same `audit` counters can_submit reads: gate_blocks + block_findings, plus any explicit
    calibration_blocks the calibration recheck stamps. A missing/garbled audit yields 0 here — the
    audit-existence + verdict gate is enforced by can_submit/verify_submittable, so this counter is
    purely the 'are there BLOCK findings' superset check, not a re-implementation of those gates."""
    audit = record.get("audit") if isinstance(record, dict) else None
    if not isinstance(audit, dict):
        return 0
    try:
        gate = int(audit.get("gate_blocks", 0) or 0)
        block = int(audit.get("block_findings", 0) or 0)
        calib = int(audit.get("calibration_blocks", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return gate + block + calib


def verify_ready(record: dict, config) -> Tuple[bool, str]:
    """THE SINGLE READINESS AUTHORITY (M4). A STRICT SUPERSET of verify_submittable.

    Returns (True, "") only when ALL hold:
      * verify_submittable(record, config) PASSES (which itself folds in can_submit + the
        fabrication/quality verdicts + PDF integrity + no in-flight edit), AND
      * can_submit(record) == (True, "")  (asserted explicitly so the superset is self-evident and
        a future verify_submittable refactor can't silently drop it), AND
      * zero fabrication/calibration BLOCK findings, AND
      * G1 reconciliation clean (pass-when-absent until Phase 4b), AND
      * G2 form-constraint compliance PASS (pass-when-absent until Phase 4b), AND
      * G3 cover length / auto-fit PASS (pass-when-absent until Phase 4b).

    On failure returns (False, <first failing reason>) — checks run in the order above and the
    first failing reason is returned (verify_submittable's reasons are joined so none is hidden).

    ADDITIVE + BACKWARD-SAFE: this is a NEW function; it does NOT change verify_submittable or
    can_submit. Because the three G-hooks pass-when-absent, a record that already passes
    verify_submittable + can_submit with no G-data today passes verify_ready unchanged — no existing
    ready card regresses. The wiring is in place so Phase 4b only fills the G-hook bodies. PURE
    (verify_submittable/can_submit are pure; the G-hooks read stored fields only)."""
    if not isinstance(record, dict):
        return False, "no staged record"

    ok, reasons = verify_submittable(record, config)
    if not ok:
        return False, "; ".join(str(r) for r in reasons) or "verify_submittable failed"

    cs_ok, cs_reason = can_submit(record)
    if not cs_ok:
        return False, cs_reason or "can_submit refused"

    nblocks = _fab_calib_block_count(record)
    if nblocks > 0:
        return False, (f"{nblocks} fabrication/calibration BLOCK finding"
                       + ("s" if nblocks != 1 else "") + " still outstanding")

    for hook in (_g1_reconcile_ok, _g2_compliance_ok, _g3_cover_ok):
        g_ok, g_reason = hook(record)
        if not g_ok:
            return False, g_reason

    return True, ""


# --------------------------------------------------------------------------------------
# 2. label matching for deterministic custom-answer replay (PURE)
# --------------------------------------------------------------------------------------

def _norm_label(s: str) -> str:
    """Normalize a question label for matching: lowercase, collapse whitespace, drop a
    trailing required-marker asterisk and surrounding punctuation. PURE."""
    t = (s or "").replace("*", " ").lower()
    return " ".join(t.split())


def match_custom_entry(live_label: str, stored_qs: list) -> Optional[dict]:
    """Find the stored custom-question record whose label matches `live_label`.

    Deterministic + PURE. Only entries that were actually answered (status answered/drafted)
    AND carry a value/values are eligible — declined/blocked/error entries are never re-filled.
    Matching is exact-on-normalized first, then a containment fallback (a live label may carry
    extra helper text around the stored question). Returns the entry or None."""
    target = _norm_label(live_label)
    if not target:
        return None
    eligible = [q for q in (stored_qs or [])
                if isinstance(q, dict)
                and q.get("status") in ("answered", "drafted")
                and (q.get("value") is not None or q.get("values"))]
    # exact normalized match wins
    for q in eligible:
        if _norm_label(q.get("q", "")) == target:
            return q
    # containment fallback (either direction), longest stored label first to avoid a short
    # generic label swallowing the wrong question
    for q in sorted(eligible, key=lambda e: -len(_norm_label(e.get("q", "")))):
        ql = _norm_label(q.get("q", ""))
        if ql and (ql in target or target in ql):
            return q
    return None


# --------------------------------------------------------------------------------------
# submit controls (researched from the live adapters + fixtures) — NEVER referenced
# anywhere outside replay(submit=True)
# --------------------------------------------------------------------------------------

# ordered CSS-selector candidates per ATS; first visible+enabled match is clicked.
_SUBMIT_SELECTORS = {
    AtsKind.GREENHOUSE: ["#submit_app", "input#submit_app",
                         "button[type='submit']#submit_app"],
    AtsKind.LEVER:      ["#btn-submit", "button#btn-submit"],
    AtsKind.ASHBY:      ["button[type='submit']"],
    AtsKind.WORKDAY:    ["[data-automation-id='pageFooterSubmitButton']"],
}
# text-based fallback (button/input whose visible text matches) when no selector hits.
_SUBMIT_TEXTS = ("submit application", "submit")


def _find_submit_control(page, kind: AtsKind):
    """Return a Playwright element handle for the ATS submit control, or None. Tries the
    per-ATS selector candidates first, then a visible button/input whose text is a submit
    label. Read-only — does NOT click."""
    for sel in _SUBMIT_SELECTORS.get(kind, []):
        try:
            el = page.query_selector(sel)
        except Exception:
            el = None
        if el is not None:
            try:
                if el.is_visible():
                    return el
            except Exception:
                return el
    # text fallback
    try:
        candidates = page.query_selector_all("button, input[type='submit']")
    except Exception:
        candidates = []
    for el in candidates:
        try:
            if not el.is_visible():
                continue
            txt = (el.inner_text() or el.get_attribute("value") or "").strip().lower()
        except Exception:
            continue
        if txt in _SUBMIT_TEXTS:
            return el
    return None


# Selectors that surface submit-time validation/error text across Greenhouse/ATS forms.
# Pure observability — scraped AFTER a submit click to tell us WHICH field blocked. Never
# influences whether a submit is claimed.
_ERROR_SELECTORS = (
    "[aria-invalid='true']",
    ".field-error",
    "[class*='error']",
    "[role='alert']",
    ".error-message",
)


def _scrape_form_errors(page, cap: int = 12) -> list:
    """Collect visible submit-time validation/error text from the live DOM, deduped and
    capped to `cap` short strings. Defensive: a mock page without these methods (or any DOM
    error) yields []. NEVER raises — capture failure must never fail the run."""
    out = []
    seen = set()
    for sel in _ERROR_SELECTORS:
        try:
            els = page.query_selector_all(sel)
        except Exception:  # noqa: BLE001 — mock page / detached DOM
            continue
        for el in els or []:
            try:
                if not el.is_visible():
                    continue
                txt = (el.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            # collapse whitespace, keep it short and human-readable
            txt = " ".join(txt.split())
            if not txt or len(txt) > 160:
                continue
            key = txt.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(txt)
            if len(out) >= cap:
                return out
    return out


def _capture_submit_result(page, record: dict) -> Tuple[Optional[str], list]:
    """Screenshot the live page to `<run_dir>/submit_result.png` and scrape visible error
    text — both pure observability, taken while the page is still alive after a submit click.

    The screenshot lands in the record's `run_dir` (the same folder `preview_png` is served
    from, so the dashboard's existing /apply-queue/shot route finds it with no new wiring).
    Returns (submit_shot_filename_or_None, form_errors). Fully defensive: a mock page, a
    missing/unwritable run_dir, or any I/O error degrades to (None, []) — capture failure
    must NEVER fail the run or change whether a submit is claimed."""
    from pathlib import Path

    errors = _scrape_form_errors(page)

    shot = None
    run_dir = (record.get("run_dir") or "").strip() if isinstance(record, dict) else ""
    if run_dir:
        try:
            rd = Path(run_dir)
            rd.mkdir(parents=True, exist_ok=True)
            target = rd / "submit_result.png"
            page.screenshot(path=str(target), full_page=True)
            if target.is_file():
                shot = "submit_result.png"
        except Exception:  # noqa: BLE001 — mock page w/o .screenshot, bad path, I/O error
            shot = None
    return shot, errors


_CONFIRM_PHRASES = (
    "application submitted", "thank you for applying", "thanks for applying",
    "your application has been submitted", "successfully submitted",
    "we have received your application", "application received",
    "thank you for your application", "you have applied", "applied successfully",
    "submission received", "we've received your application",
    # NOTE: Ashby's in-place success ("...your application has been received") is confirmed by
    # the AshbyAdapter.submit_succeeded hook (checked first in _confirm_submitted), NOT a shared
    # phrase — keeping it out of this generic list avoids a false-confirm on non-Ashby boards
    # whose page may pre-render that wording before the engine's own submit.
)

# BOT-FLAG / submission-rejected banners. Ashby (Baseten/Ramp/LangChain) flags the engine's
# automated submit click and renders a red "we couldn't submit … please submit again" banner —
# the click did NOT submit. Detecting this turns the vague "could not confirm" into an explicit
# "the ATS flagged the auto-submit; submit manually/watched" outcome (feedback_ashby_flags_
# automated_submit). Phrases are matched against the lowercased page body.
_FLAG_PHRASES = (
    "submission was flagged", "application was flagged", "flagged for review",
    "couldn't submit your application", "could not submit your application",
    "couldn't submit this application", "unable to submit your application",
    "submission could not be processed", "please submit your application again",
    "please submit again", "submission failed",
)


def _confirm_submitted(page, url_before: str, timeout_ms: int = 15000,
                       adapter=None) -> Tuple[bool, str, bool]:
    """Positively confirm the application was submitted, POLLING for up to timeout_ms.

    The old single 3s check missed Greenhouse/Ashby thank-you pages that navigate or render
    a confirmation a few seconds after the click (AJAX/redirect), producing false 'could not
    confirm' results. Now we poll every second for: a URL change away from the form, a known
    confirmation phrase, or the submit control disappearing from the page (the form is gone =
    it was accepted). Returns (confirmed, evidence).

    Ashby is the key exception the URL/phrase checks miss: it does NOT navigate on submit — the
    form is replaced IN PLACE by a "Success / Your application has been received" panel. When the
    adapter exposes an adapter-specific `submit_succeeded(page)` (Ashby does), we trust it as a
    positive confirmation signal in addition to the generic checks below.

    Returns (confirmed, evidence, flagged). `flagged=True` means a bot-flag / submission-rejected
    banner was seen (the click did NOT submit) — the caller turns that into an explicit
    flagged_bot_detection outcome rather than a vague "could not confirm"."""
    waited = 0
    step = 1000
    submit_succeeded = getattr(adapter, "submit_succeeded", None)
    while waited <= timeout_ms:
        try:
            page.wait_for_timeout(step)
        except Exception:
            pass
        waited += step
        # adapter-specific positive confirmation (Ashby in-place success panel: submit button
        # gone AND success text present). Checked first because Ashby never changes the URL.
        if callable(submit_succeeded):
            try:
                if submit_succeeded(page):
                    return True, "adapter confirmed in-place success panel", False
            except Exception:
                pass
        try:
            url_after = page.url
        except Exception:
            url_after = url_before
        if url_after and url_after != url_before:
            return True, f"url changed -> {url_after}", False
        try:
            body = (page.inner_text("body") or "").lower()
        except Exception:
            body = ""
        for phrase in _CONFIRM_PHRASES:
            if phrase in body:
                return True, f"confirmation text: {phrase!r}", False
        # bot-flag / submission-rejected banner -> definitively NOT submitted; bail early.
        for phrase in _FLAG_PHRASES:
            if phrase in body:
                return False, f"the ATS flagged the automated submit ({phrase!r})", True
    return (False,
            (f"no URL change or confirmation text detected after {timeout_ms // 1000}s "
             "(if the browser visibly showed a confirmation, this is a detection gap — "
             "tell me the exact confirmation wording and I'll add it)"),
            False)


# --------------------------------------------------------------------------------------
# 3. replay — deterministic re-fill, optional submit
# --------------------------------------------------------------------------------------

def replay(record: dict, page, answers, adapter, *, submit: bool) -> dict:
    """Re-fill the stored answers onto the already-navigated form `page`, then either leave
    the page on review (submit=False) or click the ATS submit control (submit=True).

    Deterministic: standard fields + work-auth come from `answers`/the classifier exactly
    as in staging; custom answers come FROM `record`, never an LLM. Returns a result dict:
      {ok, submitted, opened?, reason?, refilled[], unmatched_custom[], confirmation?}

    Submit path safety: re-runs `can_submit(record)` live, requires the standard-field
    read-back to verify, and only then clicks submit — the ONE permitted submit click in
    the engine. If submission cannot be positively confirmed, returns submitted=False with
    the reason (never claims an unverified submit)."""
    from .questions import (extract_questions, extract_select_questions,
                            extract_checkbox_groups)

    result = {"ok": True, "submitted": False, "refilled": [], "unmatched_custom": []}

    # --- standard fields (deterministic, same as staging) ---
    intended = adapter.fill(page, answers)            # core fields + resume re-attach
    extra = adapter.fill_remaining(page, answers)     # best-effort extra mapped fields
    result["refilled"] = list(intended.keys()) + [k for k in extra if k not in intended]

    # --- work-auth (deterministic via the classifier, same as staging) ---
    for q in adapter.find_work_auth_questions(page):
        decision = classify_work_auth(q.label)
        if decision == WorkAuthDecision.SPONSORSHIP_NO:
            set_ok = adapter.answer_no(page, q)
        elif decision in (WorkAuthDecision.AUTHORIZED_YES,
                          WorkAuthDecision.AUTHORIZED_NO_SPONSORSHIP):
            set_ok = adapter.answer_yes(page, q)
        else:
            # a HALT-class work-auth question reappearing means this is not safe to finish.
            return {"ok": False, "submitted": False,
                    "reason": f"work-auth question needs Sam: {q.label}",
                    "refilled": result["refilled"], "unmatched_custom": []}
        # the answer call returns a VERIFIED bool — abort the finish if it did not register,
        # never proceed toward submit with a silently-blank work-auth field.
        if not set_ok:
            return {"ok": False, "submitted": False,
                    "reason": f"could not set work-auth answer on the form: {q.label}",
                    "refilled": result["refilled"], "unmatched_custom": []}

    # --- custom answers re-filled FROM THE STORED RECORD (never the LLM) ---
    from .questions import extract_react_select_questions
    stored_qs = list(record.get("custom_qs") or record.get("generated") or [])
    if stored_qs:
        _replay_custom(page, stored_qs, result, adapter,
                       extract_questions, extract_select_questions, extract_checkbox_groups,
                       extract_react_select_questions)

    # --- VERIFY standard fields by read-back; a mismatch ABORTS (never submit on drift) ---
    observed = adapter.read_back(page, list(intended.keys()))
    vr = verify_fields(intended, observed)
    if not vr.ok:
        return {"ok": False, "submitted": False,
                "reason": f"read-back verification mismatch: {vr.mismatches}",
                "refilled": result["refilled"],
                "unmatched_custom": result["unmatched_custom"]}

    adapter.go_to_review(page)

    if not submit:
        result["opened"] = True
        return result  # leave the page on review for Sam — do NOT close

    # ---- SUBMIT branch: the ONE place a submit click is allowed ----
    ok, reason = can_submit(record)           # live re-check just before the click
    if not ok:
        return {"ok": False, "submitted": False, "reason": f"submit re-check refused: {reason}",
                "refilled": result["refilled"], "unmatched_custom": result["unmatched_custom"]}
    # captcha pre-check: abort the auto-submit if a captcha gates the form — the engine never
    # solves one. submit_phase=True also catches reCAPTCHA v3 (score-based): automation scores low
    # and the backend bot-flags the submit, so it must go to a real human browser. INVISIBLE v2
    # reCAPTCHA returns None and does NOT block.
    from .captcha import detect_captcha
    cap = detect_captcha(page, submit_phase=True)
    if cap:
        if cap == "recaptcha_v3":
            reason = ("reCAPTCHA v3 (invisible bot-scoring) gates this form — an automated submit "
                      "scores low and gets flagged as a bot. There is no challenge to solve; submit "
                      "this one from your OWN browser (a real session passes). The form is staged "
                      "and ready.")
            outcome = "needs_watched_submit"
        else:
            reason = (f"captcha-gated ({cap}) — manual submit required: solve the captcha and click "
                      "submit yourself")
            outcome = "captcha_gated"
        return {"ok": False, "submitted": False, "outcome": outcome, "reason": reason,
                "refilled": result["refilled"], "unmatched_custom": result["unmatched_custom"]}
    # Never click submit when a required resume didn't actually attach — that just bounces
    # off the ATS's required-field validation (the silent "clicked but couldn't confirm"
    # failure mode). Fail loud with the real reason instead.
    if getattr(adapter, "resume_attached_ok", None) is False:
        return {"ok": False, "submitted": False,
                "reason": "resume failed to attach — refusing to submit without it "
                          "(ATS would reject on the required-resume field)",
                "refilled": result["refilled"], "unmatched_custom": result["unmatched_custom"]}

    kind = detect_ats(record.get("url", "") or record.get("apply_url", ""))

    # Ashby (Baseten/Ramp/LangChain) BOT-FLAGS the engine's automated submit click — the click is
    # rejected with a "submission was flagged, submit again" banner and nothing is submitted
    # (feedback_ashby_flags_automated_submit; proven on JOB-297 Baseten 2026-06-09). Refuse the CLI
    # auto-submit by default so we don't burn a flagged attempt — Ashby submits go manually or
    # watched via the browser MCP (a real human session isn't flagged). Override to force a try:
    # ARIA_ALLOW_ASHBY_SUBMIT=1.
    import os as _os
    if "ashby" in str(kind).lower() and _os.environ.get("ARIA_ALLOW_ASHBY_SUBMIT") != "1":
        return {"ok": False, "submitted": False, "outcome": "needs_watched_submit",
                "reason": ("Ashby blocks automated submission (it bot-flags the engine's submit "
                           "click). The form is staged and ready — submit this one manually or "
                           "watched via the browser. (Set ARIA_ALLOW_ASHBY_SUBMIT=1 to force a "
                           "CLI submit attempt anyway.)"),
                "refilled": result["refilled"], "unmatched_custom": result["unmatched_custom"]}

    btn = _find_submit_control(page, kind)
    if btn is None:
        return {"ok": False, "submitted": False,
                "reason": "could not locate the ATS submit control",
                "refilled": result["refilled"], "unmatched_custom": result["unmatched_custom"]}

    try:
        url_before = page.url
    except Exception:
        url_before = ""
    try:
        btn.click()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "submitted": False, "reason": f"submit click failed: {e!r}",
                "refilled": result["refilled"], "unmatched_custom": result["unmatched_custom"]}

    confirmed, evidence, flagged = _confirm_submitted(page, url_before, adapter=adapter)

    # OBSERVABILITY (both outcomes): while the page is still alive, screenshot the result and
    # scrape any submit-time validation/error text. This is pure instrumentation around the
    # existing flow — it never changes whether a submit is claimed (capture runs AFTER the
    # confirm decision and its failure is swallowed). The dashboard renders these so a remote
    # submit is self-diagnosing: confirmation page (worked, detection gap) OR the blocked field.
    submit_shot, form_errors = _capture_submit_result(page, record)
    result["submit_shot"] = submit_shot
    result["form_errors"] = form_errors

    if not confirmed:
        # clicked but cannot positively confirm — NEVER claim a submit we can't verify.
        out = {"ok": True, "submitted": False,
               "refilled": result["refilled"], "unmatched_custom": result["unmatched_custom"],
               "submit_shot": submit_shot, "form_errors": form_errors}
        if flagged:
            # the ATS actively REJECTED the automated submit (Ashby bot-flag). Distinct, explicit
            # outcome so the dashboard says "submit manually/watched", not a vague detection gap.
            out["outcome"] = "flagged_bot_detection"
            out["reason"] = (f"{evidence} — the application was NOT submitted. This ATS blocks "
                             "automated submission; submit it manually or watched via the browser.")
        else:
            reason = f"clicked submit but could not confirm submission: {evidence}"
            if form_errors:
                # surface the scraped field errors so the log/Telegram show WHICH field blocked.
                reason += " — form flagged: " + "; ".join(form_errors)
            out["reason"] = reason
        return out
    result["submitted"] = True
    result["confirmation"] = evidence
    return result


def _replay_custom(page, stored_qs, result, adapter, extract_questions,
                   extract_select_questions, extract_checkbox_groups,
                   extract_react_select_questions) -> None:
    """Re-fill the live custom questions from the stored record. For each live custom
    widget, find the matching stored entry by label and write the STORED value. Records
    what matched (refilled) vs. couldn't be re-matched (unmatched_custom). Never raises."""
    # (a) free-text essays / short answers
    for q in extract_questions(page):
        entry = match_custom_entry(q.label, stored_qs)
        if entry is None:
            result["unmatched_custom"].append({"q": q.label, "kind": "text"})
            continue
        try:
            page.fill(q.selector, str(entry.get("value", "")))
            result["refilled"].append(f"custom:{q.label}")
        except Exception as e:  # noqa: BLE001
            result["unmatched_custom"].append({"q": q.label, "kind": "text",
                                               "reason": repr(e)[:120]})

    # (b) custom <select> dropdowns
    for sq in extract_select_questions(page):
        entry = match_custom_entry(sq.label, stored_qs)
        if entry is None or not entry.get("value"):
            result["unmatched_custom"].append({"q": sq.label, "kind": "select"})
            continue
        try:
            page.select_option(sq.selector, label=str(entry["value"]))
            result["refilled"].append(f"custom:{sq.label}")
        except Exception as e:  # noqa: BLE001
            result["unmatched_custom"].append({"q": sq.label, "kind": "select",
                                               "reason": repr(e)[:120]})

    # (c) checkbox-groups ("check all that apply")
    for cg in extract_checkbox_groups(page):
        entry = match_custom_entry(cg.label, stored_qs)
        values = (entry or {}).get("values") or []
        if entry is None or not values:
            result["unmatched_custom"].append({"q": cg.label, "kind": "checkbox_group"})
            continue
        checked = []
        for val in values:
            if val in cg.options:
                try:
                    page.check(cg.selectors[cg.options.index(val)])
                    checked.append(val)
                except Exception:  # noqa: BLE001
                    pass
        if checked:
            result["refilled"].append(f"custom:{cg.label}")
        else:
            result["unmatched_custom"].append({"q": cg.label, "kind": "checkbox_group",
                                               "reason": "no stored value matched a live option"})

    # (d) custom React-select questions (modern Greenhouse Yes/No + searchable). The stored
    # value is re-selected via the react-select driver (click the matching .select__option),
    # never an LLM. An unmatched/empty entry is surfaced, never silently skipped.
    for rq in extract_react_select_questions(page):
        entry = match_custom_entry(rq.label, stored_qs)
        if entry is None or not entry.get("value"):
            result["unmatched_custom"].append({"q": rq.label, "kind": "react_select"})
            continue
        try:
            if adapter.select_react_by_label(page, rq.label, str(entry["value"])):
                result["refilled"].append(f"custom:{rq.label}")
            else:
                result["unmatched_custom"].append(
                    {"q": rq.label, "kind": "react_select",
                     "reason": "react-select option click did not register"})
        except Exception as e:  # noqa: BLE001
            result["unmatched_custom"].append({"q": rq.label, "kind": "react_select",
                                               "reason": repr(e)[:120]})


# --------------------------------------------------------------------------------------
# 4. finish_job — orchestration entry the dashboard backend calls
# --------------------------------------------------------------------------------------

def finish_job(job_id: str, *, submit: bool, headless: bool, runs_root,
               profile_dir, manifest_path) -> dict:
    """Load the staged record for `job_id`, re-open the form, and replay it. When
    submit=True, pre-checks `can_submit` BEFORE opening a browser (fail fast). On a
    confirmed submit, stamps the manifest record submitted/status. Wrapped so it never
    throws to the caller — always returns a result dict."""
    try:
        from datetime import datetime
        from . import config
        from .source_data import build_answers
        from .browser import launch_profile
        from .orchestrator import _adapter_for  # reuse adapter selection

        record = _load_record(manifest_path, job_id)
        if record is None:
            return {"ok": False, "submitted": False,
                    "reason": f"no staged record for {job_id} in manifest"}

        # fail fast: never open a browser for a submit that can't pass the gate.
        if submit:
            ok, reason = can_submit(record)
            if not ok:
                return {"ok": False, "submitted": False, "reason": reason}

        job = _job_from_record(record)
        # Resolve the TAILORED package from the staged record's uploaded_docs (never the master).
        resume_pdf, cover_pdf = _resolve_pdfs(config, job_id, record=record)
        # No-fallback sentinel: if a tailored resume can't be resolved, refuse BEFORE opening a
        # browser. Submitting here would attach nothing/the master and bounce off the ATS — fail
        # loud with the real reason instead. (Mirror of the can_submit PDF-integrity invariant for
        # the --open path too, which build_answers would otherwise carry an empty resume_pdf into.)
        if submit and resume_pdf is None:
            return {"ok": False, "submitted": False,
                    "reason": ("no tailored resume PDF resolved from the staged package; refusing "
                               "to submit (will not attach the generic master resume)")}
        answers = build_answers(profile_path=config.PROFILE_JSON, job=job,
                                resume_pdf=resume_pdf, cover_pdf=cover_pdf)

        url = record.get("url") or record.get("apply_url") or job.get("url", "")
        kind = detect_ats(url)
        adapter = _adapter_for(kind)

        with launch_profile(headless=headless, profile_dir=profile_dir) as (ctx, page):
            page.goto(url)
            # multi-step adapters re-drive their own wizard; the dashboard finish path
            # currently supports the single-page adapters' deterministic replay.
            if getattr(adapter, "multi_step", False):
                return {"ok": False, "submitted": False,
                        "reason": f"{adapter.name} is multi-step — finish via the staging "
                                  "wizard, not the single-page replay path"}
            adapter.login(page, profile_signed_in=True)
            adapter.go_to_form(page)
            res = replay(record, page, answers, adapter, submit=submit)

            # The launch_profile context manager closes the browser the moment this block
            # exits (and a detached process exiting kills Chrome too) — that was the "filled
            # then suddenly closed" bug, which also stopped Sam SEEING a submit's result.
            # Hold the browser open here for BOTH modes: --open (review the fill) and --submit
            # (see whether the confirmation page actually appeared, especially when the engine
            # reports submitted=False). Interactive run waits for Enter; a detached run (no
            # stdin → EOFError) falls back to a window so there's still time to look.
            if res.get("opened") or submit:
                import sys as _sys
                import time as _time
                if submit:
                    _hint = ("SUBMITTED" if res.get("submitted")
                             else "could NOT confirm submission — look at the page: if it shows "
                                  "a confirmation/thank-you it DID submit (detection gap)")
                    _prompt = f"\n[finish] Submit result: {_hint}. Review the browser, then Enter to close..."
                    _idle = 120
                else:
                    _prompt = "\n[finish] Review the filled form in the browser, then Enter to close..."
                    _idle = 900
                try:
                    if _sys.stdin and _sys.stdin.isatty():
                        input(_prompt)
                    else:
                        print(f"[finish] Browser left open (~{_idle // 60} min for this "
                              "non-interactive run).", flush=True)
                        _time.sleep(_idle)
                except (EOFError, OSError):
                    _time.sleep(_idle)

        if submit and res.get("submitted"):
            _mark_submitted(manifest_path, job_id,
                            datetime.now().isoformat(timespec="seconds"))
        return res
    except Exception as e:  # noqa: BLE001 — never throw to the dashboard backend
        return {"ok": False, "submitted": False, "reason": f"finish_job error: {e!r}"}


def _load_record(manifest_path, job_id: str) -> Optional[dict]:
    """Read the staged-application record for job_id from the manifest list, or None."""
    import json
    from pathlib import Path
    mp = Path(manifest_path)
    if not mp.exists():
        return None
    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    for entry in data:
        if isinstance(entry, dict) and entry.get("job_id") == job_id:
            return entry
    return None


def _job_from_record(record: dict) -> dict:
    """Reconstruct the minimal job dict build_answers/adapters need from a flat record."""
    return {
        "id": record.get("job_id", ""),
        "company": record.get("company", ""),
        "title": record.get("role", ""),
        "url": record.get("url", "") or record.get("apply_url", ""),
        "ats": record.get("ats", ""),
    }


def _is_master_resume(path) -> bool:
    """True if `path` is the generic MASTER resume (by filename). The master must NEVER be the
    file attached to a selected job — the QUALITY CONTRACT requires the tailored package. The
    check is on the filename stem so it catches the .docx or .pdf form regardless of directory
    (the live legacy records JOB-216/JOB-212 carry the master path in uploaded_docs)."""
    from pathlib import Path
    if not path:
        return False
    return Path(str(path)).name.lower().startswith("sam_rivera_resume_master")


def _uploaded_doc_path(record: dict, doc: str):
    """Return the staged absolute path for `doc` ("resume"|"cover") from record['uploaded_docs'],
    or None. THIS is the authoritative source: the staging step computes the correct tailored path
    (applications/<APP-ID>-<Company>/...) and stores it on the record. We do NOT recompute a path
    here — the old recompute (applications/<job_id>/resume.pdf) pointed at a dir that never exists,
    which silently fell back to the master at submit time (the bug this fix exists for)."""
    if not isinstance(record, dict):
        return None
    for d in (record.get("uploaded_docs") or []):
        if isinstance(d, dict) and (d.get("doc") or "").strip().lower() == doc:
            p = (d.get("path") or "").strip()
            if p:
                return p
    return None


# Canonical tailored filenames the /career build pipeline emits into applications/<APP-ID>-<slug>/.
# Mirrors cli.ensure_pdfs' accepted names (newer SAM_RIVERA_* form + older plain form). Kept
# here as the single tailored-name list the resolution fallback reads — do NOT fork a second scheme.
_TAILORED_NAMES = {
    "resume": ("SAM_RIVERA_Resume.pdf", "resume.pdf"),
    "cover":  ("SAM_RIVERA_Cover_Letter.pdf", "cover.pdf"),
}


def _tailored_sibling_pdf(record: dict, doc: str):
    """Resolution FALLBACK (2026-06-11): find a tailored `doc` PDF that exists on disk in the same
    applications/<APP-ID>-<slug>/ dir as an ALREADY-registered uploaded_docs sibling, when `doc`
    itself has no uploaded_docs entry. Returns the absolute path string or None.

    WHY this exists: a cover (or resume) built/edited AFTER the original apply RUN never gets written
    into uploaded_docs — the run only records what attached live (orchestrator._record_uploaded_docs).
    So a real tailored cover sits on disk in the app's tailored dir while uploaded_docs lists resume
    only; _uploaded_doc_path returns None and the engine drops the tailored cover at submit. This
    derives the missing doc from the sibling's directory using the SAME canonical filename scheme the
    build pipeline / ensure_pdfs use. It NEVER returns a master/generic — only a file physically
    present in the tailored dir. PURE (filesystem read only)."""
    from pathlib import Path
    if not isinstance(record, dict):
        return None
    other = "cover" if doc == "resume" else "resume"
    sib = _uploaded_doc_path(record, other)
    if not sib:
        return None
    app_dir = Path(sib).parent
    for name in _TAILORED_NAMES.get(doc, ()):  # canonical names, newest first
        cand = app_dir / name
        if cand.exists():
            return str(cand)
    return None


def _resolve_doc_pdf(record: dict, doc: str):
    """Resolve the tailored PDF path for `doc` ("resume"|"cover"): the registered uploaded_docs path
    first, then the sibling-dir tailored fallback. Returns a path string or None. Does NOT validate
    existence or master-ness — the callers (_resolve_pdfs / verify_submittable) apply those rules so
    each can keep its own policy (the resume must additionally be non-master)."""
    return _uploaded_doc_path(record, doc) or _tailored_sibling_pdf(record, doc)


def _cover_was_tailored(record: dict) -> bool:
    """True if a tailored cover letter was actually BUILT for this app — either a cover dict carrying
    paragraphs is present on the record, or a tailored cover PDF resolves (registered or sibling).
    Used by the submit gate to require that a built cover actually attaches (never silently dropped),
    WITHOUT inventing a cover requirement for roles that legitimately want none."""
    cov = record.get("cover") if isinstance(record, dict) else None
    if isinstance(cov, dict) and cov.get("paragraphs"):
        return True
    return _resolve_doc_pdf(record, "cover") is not None


def _resolve_pdfs(config, job_id: str, record: Optional[dict] = None):
    """Resolve the tailored resume + cover PDFs to attach for this job FROM THE STAGED RECORD's
    `uploaded_docs` (the authoritative paths staging computed), returning (resume_pdf, cover_pdf).

    HARD CONTRACT (2026-06-10): never substitute the generic master resume at submit. The resume
    is returned ONLY if a tailored uploaded_docs entry resolves, the file exists on disk, AND it is
    not the master file; otherwise the resume is returned as None (the no-fallback SENTINEL) so the
    submit gate treats it as a hard block. The old behaviour recomputed `applications/<job_id>/
    resume.pdf` (a path that never exists) and fell back to the master PDF — silently shipping the
    generic resume on every "submittable" job. That fallback is removed.

    `record` is the staged manifest record. It is keyword-optional for backward-compat with any
    caller that didn't pass it, but a None record (or one with no uploaded_docs resume) yields the
    sentinel None — there is no master last-resort anywhere on this path."""
    from pathlib import Path

    resume_pdf = None
    # Registered path first, then the sibling-dir tailored fallback (a resume rebuilt after the
    # apply run may be missing from uploaded_docs). Tailored ONLY: must resolve, exist on disk, and
    # NOT be the master. Any miss → sentinel (no master last-resort anywhere on this path).
    rp = _resolve_doc_pdf(record or {}, "resume")
    if rp and not _is_master_resume(rp) and Path(rp).exists():
        resume_pdf = Path(rp)

    cover_pdf = None
    # A cover built/edited AFTER the apply run is not in uploaded_docs; resolve it from the sibling
    # tailored dir so the tailored cover is attached instead of silently dropped at submit.
    cp = _resolve_doc_pdf(record or {}, "cover")
    if cp and Path(cp).exists():
        cover_pdf = Path(cp)

    return resume_pdf, cover_pdf


def _mark_submitted(manifest_path, job_id: str, stamp: str) -> None:
    """Stamp the manifest record submitted=True / status='submitted' after a confirmed
    submit. Atomic-ish write; silent no-op on a missing/corrupt manifest or unknown id."""
    import json
    import os
    from pathlib import Path

    from .filemutex import locked

    mp = Path(manifest_path)
    if not mp.exists():
        return
    # Under the file mutex: _mark_submitted whole-file-rewrites staged_applications.json, so a
    # concurrent answer edit on this (or a sibling) record would otherwise be clobbered. Re-read
    # FRESH inside the lock and stamp only this record.
    with locked(mp):
        try:
            data = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, list):
            return
        changed = False
        for entry in data:
            if isinstance(entry, dict) and entry.get("job_id") == job_id:
                entry["submitted"] = True
                entry["status"] = "submitted"
                entry["submitted_at"] = stamp
                changed = True
                break
        if not changed:
            return
        tmp = mp.with_suffix(mp.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, mp)
