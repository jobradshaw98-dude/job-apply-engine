"""Per-job conductor: detect -> (LinkedIn resolve) -> auth -> fill -> work-auth guard
-> verify -> stage-to-brink. NEVER submits. Returns a structured JobOutcome.

Screenshots are full-page so the user reviews the whole filled form, not just the top."""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .ats_detect import detect_ats, AtsKind
from .work_auth import classify_work_auth, WorkAuthDecision
from .work_auth import verify_sponsorship_answer, WorkAuthVerify
from .verify import verify_fields
from .completeness import unfilled_required, drop_answered
from .run_context import RunContext
from .browser import launch_profile
from .adapters.greenhouse import GreenhouseAdapter
from .adapters.lever import LeverAdapter
from .adapters.ashby import AshbyAdapter
from .adapters.workday import WorkdayAdapter
from .adapters.generic import GenericFiller
from .linkedin import resolve_linkedin, EASY_APPLY

_ADAPTERS = {
    AtsKind.GREENHOUSE: GreenhouseAdapter,
    AtsKind.LEVER: LeverAdapter,
    AtsKind.ASHBY: AshbyAdapter,
    AtsKind.WORKDAY: WorkdayAdapter,
}

# Unambiguous "this posting is gone" phrases that appear in the rendered page body of a closed/
# removed posting (Greenhouse/Ashby/Lever/generic careers pages). Shared with the cheap pre-tailor
# liveness pre-flight (liveness.py) so the mid-run closed-detection and the up-front pre-flight use
# ONE source of truth. Keep these UNAMBIGUOUS — a phrase that could appear on a live page (e.g. a
# generic "search" footer) would break the fail-open contract of the pre-flight.
CLOSED_BODY_SIGNALS = (
    "no longer open", "no longer available", "position has been filled",
    "this job is no longer", "posting is closed", "this posting is closed",
    "job not found", "page not found", "the page you requested was not found",
)


@dataclass
class JobOutcome:
    job_id: str
    status: str           # ready_to_submit | needs_input | needs_sam | skipped | error
    submitted: bool = False
    verify_ok: bool = False
    run_dir: str = ""
    halt_reason: str = ""
    work_auth_answers: List[dict] = field(default_factory=list)
    filled_fields: List[str] = field(default_factory=list)
    unfilled_required: List[str] = field(default_factory=list)
    generated: List[dict] = field(default_factory=list)
    corrections: List[dict] = field(default_factory=list)  # ATS-injected fields fixed/flagged
    uploaded_docs: List[dict] = field(default_factory=list)  # files actually attached this run
    optional_filled: dict = field(default_factory=dict)  # G5: optional/EEO fields filled {label: value}
    error: str = ""
    outcome: str = ""     # precise machine label for a no-form halt: closed | unsupported_ats
                          # | homepage_no_link | form_not_found (for dashboard filtering)
    human_blocker: Optional[dict] = None  # §1b structured halt record (Feature B, Phase 1).
                          # Additive: defaults None so old readers/records are unaffected. Set at
                          # each halt site via halt_classifier.classify_halt; carried onto the flat
                          # record by staged_manifest.build_record. Behavior-neutral in Phase 1.
    form_spec: Optional[dict] = None    # Phase 4b: COMPACT FormSpec.to_summary() of the LIVE form,
                          # captured at the brink by _capture_form_model. None when capture didn't
                          # run / threw (best-effort). Feeds the G2 compliance gate.
    reconcile: Optional[dict] = None    # Phase 4b: ReconcileResult.to_record() — the live-vs-staged
                          # diff (clean bool + mismatched/unfilled lists). None when capture didn't
                          # run / threw. Feeds the G1 reconciliation gate (finish._g1_reconcile_ok).
    compliance: Optional[dict] = None   # Phase 4b: ComplianceResult.to_record() — staged answers vs
                          # the form's stated length limits (ok bool + violations). None when capture
                          # didn't run / threw. Feeds the G2 compliance gate (finish._g2_compliance_ok).


def _record_uploaded_docs(out: "JobOutcome", adapter, answers) -> None:
    """Record exactly which document files were attached to the form this run, so the
    dashboard's Documents tab can show the TRUTH (the engine uploads the master resume as
    a fallback when no tailored package exists; that upload was previously invisible).

    Only the resume is wired for upload today (via adapter.resume_selector). Cover/portfolio
    are recorded the same way if/when an adapter ever attaches them — keyed off the path
    actually passed to set_input_files, never a path we merely intended."""
    from pathlib import Path
    docs: List[dict] = []
    # Record resume ONLY when it VERIFIABLY attached (filename visible on the form). The old
    # `is not False` recorded a resume even when resume_attached_ok was None (no resume field on
    # the page), so a careers homepage with no upload falsely claimed "resume attached".
    if getattr(adapter, "resume_selector", "") and adapter.resume_attached_ok is True:
        rp = getattr(answers, "resume_pdf", None)
        if rp:
            docs.append({"doc": "resume", "path": str(rp), "name": Path(rp).name})
    cp = getattr(answers, "cover_pdf", None)
    if cp and getattr(adapter, "cover_attached_ok", None):
        docs.append({"doc": "cover", "path": str(cp), "name": Path(cp).name})
    out.uploaded_docs = docs


def _adapter_for(kind: AtsKind):
    cls = _ADAPTERS.get(kind)
    return cls() if cls else GenericFiller()


def _qkey(s):
    """Normalize a question label to a stable key. MUST match regen_answer._qkey exactly
    (alnum-only, lowercased, first 70 chars) so a user-provided answer keyed by the
    dashboard/`--provide` is found here against the live-form label."""
    return "".join(c for c in (s or "").lower() if c.isalnum())[:70]


def _load_provided_answers(job_id):
    """Read-only, best-effort: load the prior staged record for `job_id` and return a
    map {qkey(label) -> provided_value} for every custom_q the user answered himself
    (answered_by == "sam") with a non-empty value.

    This is what lets a re-stage CONSUME an answer the user supplied via
    `python -m apply_engine.regen_answer <job> --question ... --provide ...` instead of
    re-extracting the question and re-declining it (the bug). `--provide` writes onto the
    custom_q: value (or values for multi), status="answered", answered_by="sam". We key
    by the SAME _qkey on the stored question text `q` so the live-form label matches.

    Missing file / missing record / malformed manifest -> empty map (NEVER crash): with no
    prior provided answer the stage must behave EXACTLY as before."""
    provided = {}
    try:
        from . import config
        manifest = config.ARIA_DATA / "staged_applications.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return provided
        rec = next((a for a in data
                    if isinstance(a, dict) and a.get("job_id") == job_id), None)
        if not isinstance(rec, dict):
            return provided
        for q in (rec.get("custom_qs") or []):
            if not isinstance(q, dict):
                continue
            if (q.get("answered_by") or "").strip().lower() != "sam":
                continue
            label = q.get("q", "")
            # value is the canonical single field --provide always sets (it also sets a
            # comma-joined `value` for multi-kind), so reading `value` covers every kind.
            val = q.get("value", "")
            if isinstance(val, list):
                val = ", ".join(str(x) for x in val)
            val = (val or "").strip()
            if not label or not val:
                continue
            provided[_qkey(label)] = val
    except Exception:  # noqa: BLE001 — best-effort; a bad manifest must not break a stage
        return provided
    return provided


def _classify_no_form(page, url: str, kind: "AtsKind") -> tuple:
    """When zero fields were fillable, say WHY precisely (the old single 'no fillable fields'
    message conflated four very different situations). Returns (outcome_label, human_reason):

      closed           : the posting is closed / redirected to a job-board listing (e.g. a
                         Greenhouse job id that 404s now lands on the company's full board).
      homepage_no_link : the URL is a careers HOMEPAGE, not a specific posting — bad sourcing,
                         the career agent needs to capture the direct apply link.
      unsupported_ats  : a real job page on an ATS the engine does not drive (Workday tenant w/o
                         account, iCIMS, Jobvite, bespoke portal) — apply manually.
      form_not_found   : a SUPPORTED ATS (Greenhouse/Ashby/Lever) rendered no form and isn't
                         obviously closed — a possible engine miss worth a look.
    """
    import re
    u = (url or "").split("?")[0].rstrip("/").lower()
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        body = ""
    if any(s in body for s in CLOSED_BODY_SIGNALS):
        return ("closed", "posting is closed or no longer available — remove or re-source")
    # Redirected to a board/listing: a single posting page shouldn't carry dozens of job links.
    try:
        n_job_links = page.eval_on_selector_all(
            "a[href*='/jobs/'], a[href*='/job/'], a[href*='/careers/']", "els => els.length")
    except Exception:
        n_job_links = 0
    if (n_job_links or 0) > 20:
        return ("closed", "the posting redirected to a job-board listing — likely closed/moved")
    if kind == AtsKind.UNKNOWN:
        is_homepage = bool(re.search(r"/careers$|/jobs$|/careers/?$|/job-openings?$", u)) \
            or u.count("/") <= 3  # bare domain or domain/careers, no posting path
        if is_homepage:
            return ("homepage_no_link",
                    "URL is a careers homepage, not a job posting — needs the direct apply link")
        return ("unsupported_ats",
                "real job page on an ATS the engine can't auto-drive (Workday/iCIMS/Jobvite/"
                "custom) — apply manually")
    # Supported ATS but nothing rendered and no closed signal — flag it as a possible miss.
    return ("form_not_found",
            f"no form found on a supported ATS ({getattr(kind, 'value', kind)}) — may be closed "
            "or an engine miss; worth checking")


def _shot(page, ctx, label):
    page.screenshot(path=str(ctx.next_screenshot_path(label)), full_page=True)


def _load_profile() -> dict:
    """Load the applicant profile dict for multi-step adapters. Adapters take everything
    from this dict (no hardcoded PII). Returns {} if the file is absent."""
    import json
    from .config import PROFILE_JSON
    try:
        if PROFILE_JSON.exists():
            return json.loads(PROFILE_JSON.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _stage_multi_step(adapter, page, answers, job, ctx, out: "JobOutcome",
                      answer_fn=None, audit_fn=None, facts: str = "") -> "JobOutcome":
    """Run a multi-step adapter's full wizard walk and map its result onto JobOutcome.

    ready_to_submit ONLY if it reached review AND there are no blocking escalations AND
    the staged work-auth answer verified as a no-red-flag ("No" to sponsorship). Otherwise
    needs_input / needs_sam with the escalations surfaced in halt_reason. Never submits.

    answer_fn/audit_fn/facts (opt-in) let the adapter resolve custom-question escalations
    via the gated grounded-choice picker; without them the adapter escalates every custom
    question (the safe default).
    """
    profile = _load_profile()
    res = adapter.stage_application(page, answers, profile, job,
                                    answer_fn=answer_fn, audit_fn=audit_fn, facts=facts)
    out.submitted = False
    out.filled_fields = list(res.get("filled_steps", []))
    escalations = res.get("escalations", []) or []
    wa = res.get("work_auth_verified")
    out.work_auth_answers = [{"field": "work_auth", "q": "review-page verification",
                              "answer": wa}] if wa else []
    ctx.log("multi_step", f"reached={res.get('reached')}",
            filled_steps=res.get("filled_steps"), escalations=escalations,
            work_auth_verified=wa, error=res.get("error"))
    _shot(page, ctx, "multi_step_end")

    if res.get("error") and res.get("reached") != "review":
        out.status = "error"
        out.error = str(res["error"])
        out.halt_reason = f"workday walk stopped: {res['error']}"
        return out

    if res.get("reached") != "review":
        out.status = "needs_sam"
        out.halt_reason = (f"did not reach review (stopped at {res.get('reached')!r})")
        return out

    # reached review. Verify the staged work-auth answer is the no-red-flag one using a
    # word-boundary predicate (NOT a substring scan — "no" in "now" was a real false-pass).
    wa_verdict = verify_sponsorship_answer(wa or "")
    if escalations:
        out.status = "needs_input"
        labels = [e.get("q", "")[:60] for e in escalations]
        out.unfilled_required = labels
        out.halt_reason = "questions need the user: " + "; ".join(labels[:8])
        return out
    if wa_verdict == WorkAuthVerify.FAIL:
        out.status = "needs_sam"
        out.halt_reason = ("staged work-auth answer reads as an AFFIRMATIVE sponsorship "
                           f"request (red flag) — needs the user (saw: {wa!r})")
        return out
    if wa_verdict != WorkAuthVerify.PASS:
        out.status = "needs_sam"
        out.halt_reason = ("could not verify the staged work-auth answer on the review "
                           f"page (saw: {wa!r})")
        return out

    out.status = "ready_to_submit"
    out.verify_ok = True
    ctx.log("stage", "staged to review brink (multi-step) — NOT submitted")
    return out


def _staged_record_in_progress(out: "JobOutcome") -> dict:
    """Build the MINIMAL staged-record view reconcile_form + check_form_constraints read, FROM the
    in-flight JobOutcome (the answers/docs already filled this run). Mirrors the keys
    staged_manifest.build_record writes (`custom_qs`=out.generated, `work_auth`=out.work_auth_answers,
    `uploaded_docs`, `filled_fields`) so the capture diffs against the SAME staged data the dashboard
    record will carry. Pure."""
    return {
        "custom_qs": list(getattr(out, "generated", []) or []),
        "work_auth": list(getattr(out, "work_auth_answers", []) or []),
        "uploaded_docs": list(getattr(out, "uploaded_docs", []) or []),
        "filled_fields": list(getattr(out, "filled_fields", []) or []),
    }


def _capture_form_model(out: "JobOutcome", page, adapter, ctx) -> None:
    """Phase 4b — capture the LIVE-form model at the brink (after fields are filled, BEFORE staging)
    and run the G1 reconciliation + G2 compliance checks, storing COMPACT summaries on `out`.

    BEST-EFFORT + NON-BREAKING (HARD constraint): the WHOLE capture is wrapped in try/except. If
    enumerate_fields / reconcile_form / check_form_constraints throws (a live widget the model can't
    read, a malformed page), we LOG and return — the stage proceeds EXACTLY as before with no
    form_spec/reconcile/compliance on the record. The pass-when-absent G-hooks (finish._g1/_g2) then
    keep verify_ready safe. This NEVER changes fill/submit behavior and NEVER fails a stage.

    Deterministic + offline: enumerate_fields READS the DOM (no LLM); reconcile_form /
    check_form_constraints are pure. The ambiguous prose->fields MAPPING stays DEFERRED — reconcile
    returns mismatched+needs_human_or_llm WITHOUT guessing a remap (live-dom rule); 4b only FLAGS
    not-clean, never auto-remaps."""
    try:
        from .reconcile import reconcile_form
        from .compliance import check_form_constraints

        spec = adapter.enumerate_fields(page)
        staged = _staged_record_in_progress(out)

        out.form_spec = spec.to_summary()

        rec = reconcile_form(spec, staged)
        out.reconcile = rec.to_record()

        comp = check_form_constraints(spec, staged)
        out.compliance = comp.to_record()

        ctx.log("capture", "live-form model captured",
                n_fields=out.form_spec.get("n_fields", 0),
                reconcile_clean=out.reconcile.get("clean"),
                compliance_ok=out.compliance.get("ok"))
    except Exception as e:  # noqa: BLE001 — capture is best-effort; NEVER break the stage.
        out.form_spec = None
        out.reconcile = None
        out.compliance = None
        try:
            ctx.log("capture", "live-form capture skipped (best-effort)", error=repr(e)[:200])
        except Exception:
            pass


def _stage_manifest(out: "JobOutcome", job: dict) -> None:
    """Write/update the staged-application manifest for this run. Guarded so a
    manifest failure NEVER breaks an apply run. The orchestrator stamps the time
    here (the builder stays pure)."""
    try:
        from datetime import datetime
        from . import config
        from .staged_manifest import build_record, write_record
        staged_at = datetime.now().isoformat(timespec="seconds")
        rec = build_record(out, job, staged_at)
        write_record(rec, config.ARIA_DATA / "staged_applications.json")
    except Exception:  # noqa: BLE001 — manifest is best-effort, never load-bearing
        pass


def apply_to_job(job: dict, answers, runs_root: Path, profile_dir: Path,
                 headless: bool = False, dry_run: bool = True,
                 stamp: str = "run", ats_override: Optional[str] = None,
                 answer_fn=None, audit_fn=None, facts: str = "") -> JobOutcome:
    """Public entrypoint: run the apply flow, then record the result to the staged
    manifest on every terminal path. Behavior of the run itself is unchanged."""
    out = _apply_to_job(job, answers, runs_root, profile_dir, headless=headless,
                        dry_run=dry_run, stamp=stamp, ats_override=ats_override,
                        answer_fn=answer_fn, audit_fn=audit_fn, facts=facts)
    _stage_manifest(out, job)
    return out


def _apply_to_job(job: dict, answers, runs_root: Path, profile_dir: Path,
                  headless: bool = False, dry_run: bool = True,
                  stamp: str = "run", ats_override: Optional[str] = None,
                  answer_fn=None, audit_fn=None, facts: str = "") -> JobOutcome:
    from datetime import datetime

    from .halt_classifier import classify_halt
    from .halt_classifier import is_raw_field_key

    job_id = job.get("id", "JOB-?")
    ctx = RunContext(job_id=job_id, runs_root=runs_root, stamp=stamp)
    out = JobOutcome(job_id=job_id, status="error", run_dir=str(ctx.run_dir))

    # user-provided answers from the PRIOR staged record (--provide / dashboard). On a re-stage,
    # each custom-question handler below consumes these to FILL the live widget instead of
    # re-running the classifier and re-declining the same question (the needs_input->halt loop bug).
    # Read-only + best-effort: no prior record / first stage -> {} -> behavior identical to before.
    provided_answers = _load_provided_answers(job_id)
    if provided_answers:
        ctx.log("provided", "loaded user-provided answers from prior stage",
                n=len(provided_answers))

    # One real timestamp per run — every halt blocker's deterministic id derives from job_id + this
    # (NOT a fresh random), so a re-staged card's blocker is keyed by its own halt time and a test
    # can pin it. Phase 1 is additive: classify_halt only populates out.human_blocker, never changes
    # control flow. Centralized in _halt() so every site produces a structured blocker identically.
    halt_ts = datetime.now().astimezone().isoformat(timespec="seconds")

    def _halt(category, *, question="", options=None, free_text_ok=False,
              answer_qkey_source="", finding=None, code_source="", code_snippet=""):
        """Stamp out.human_blocker for the current halt site (additive; out.status/halt_reason are
        already set by the caller). page_state.fields_filled reads the running fill count."""
        try:
            out.human_blocker = classify_halt(
                out, _page_ref[0], ctx, category=category, halt_ts=halt_ts,
                ats=getattr(kind, "value", str(kind)), reached=_reached[0],
                fields_filled=len(getattr(out, "filled_fields", []) or []),
                question=question, options=options, free_text_ok=free_text_ok,
                answer_qkey_source=answer_qkey_source, finding=finding,
                code_source=code_source, code_snippet=code_snippet)
        except Exception:  # noqa: BLE001 — blocker is best-effort metadata, never load-bearing
            pass

    _page_ref = [None]   # filled once the browser page exists (page_state.url)
    _reached = ["start"]  # coarse phase label for page_state.reached

    url = job.get("url") or job.get("apply_url") or ""
    kind = AtsKind(ats_override) if ats_override else detect_ats(url)
    ctx.log("detect", f"ATS={kind.value}", url=url)

    # G4: the role's location drives the work-auth geography gate. A role based outside the US (where
    # the applicant's work authorization does not apply) must NEVER be auto-answered "Yes, authorized" — it
    # halts into a work_auth human_blocker instead (resolve_work_auth below). Read from the job
    # record's location/JD fields; an absent/sparse value defaults the resolver to the common US path.
    role_location = (job.get("location") or job.get("role_location")
                     or job.get("job_location") or "")

    try:
        with launch_profile(headless=headless, profile_dir=profile_dir) as (c, page):
            _page_ref[0] = page   # page_state.url for any halt blocker below
            page.goto(url)
            ctx.log("nav", f"opened {url}")

            # ---- LinkedIn pre-step: resolve to real ATS or stage Easy-Apply ----
            if kind == AtsKind.LINKEDIN:
                target = resolve_linkedin(page)
                if target == EASY_APPLY:
                    out.status = "needs_sam"
                    out.halt_reason = "LinkedIn Easy-Apply only — complete manually"
                    ctx.log("halt", out.halt_reason)
                    _shot(page, ctx, "easy_apply")
                    return out
                ctx.log("linkedin", f"following outbound apply link -> {target}")
                page.goto(target)
                kind = detect_ats(target)

            adapter = _adapter_for(kind)
            ctx.log("adapter", f"using {adapter.name}")
            _shot(page, ctx, "opened")
            adapter.login(page, profile_signed_in=True)

            # ---- multi-step adapters (e.g. Workday) drive their own full wizard walk ----
            # The single-page flow below is for forms that live on one page; multi-step
            # ATSs page through several screens and stage to the Review brink themselves.
            if getattr(adapter, "multi_step", False):
                return _stage_multi_step(adapter, page, answers, job, ctx, out,
                                         answer_fn=answer_fn, audit_fn=audit_fn, facts=facts)

            adapter.go_to_form(page)   # reveal/navigate to the form if posting shown first
            _reached[0] = "form"

            # core fields (known selectors -> readback-verifiable)
            intended = adapter.fill(page, answers)
            # EVERY other mappable field (city/linkedin/github/portfolio/...) — best-effort,
            # confirmed by the completeness scan rather than readback (selectors not tracked).
            extra = adapter.fill_remaining(page, answers)
            for k, v in intended.items():
                ctx.log("fill", f"filled {k}", field=k, value=v)
            if extra:
                ctx.log("fill_remaining", "mapped additional fields", fields=list(extra.keys()))
            if getattr(adapter, "unmapped", None):
                ctx.log("unmapped", "fields generic filler could not map",
                        fields=adapter.unmapped)
            out.filled_fields = list(intended.keys()) + [k for k in extra if k not in intended]
            _record_uploaded_docs(out, adapter, answers)
            _shot(page, ctx, "filled")

            # ---- empty-fill guard: zero fields filled is NOT a staged application ----
            if not intended and not extra:
                out.status = "needs_sam"
                label, reason = _classify_no_form(page, url, kind)
                out.outcome = label
                out.halt_reason = reason
                ctx.log("halt", reason, outcome=label)
                # escalate/zero_fields: bad sourcing or unsupported ATS — not an answerable question.
                _halt("zero_fields", code_source="orchestrator.py:327",
                      code_snippet="if not intended and not extra: out.status='needs_sam'")
                return out

            # ---- not-an-application guard: a lone email box on a non-ATS page is a NEWSLETTER /
            # contact widget, NOT a job application. The bug: a careers homepage (UNKNOWN ATS) with
            # a "subscribe" email field filled `email` only, and that passed as ready_to_submit —
            # the engine would have signed the user up for a newsletter. A real application fills more
            # than a bare email (name + resume at minimum). On a KNOWN ATS the form IS an application
            # so this never triggers; only the generic/custom-page path can hit a newsletter box.
            meaningful = set(out.filled_fields) - {"email"}
            resume_ok = getattr(adapter, "resume_attached_ok", None) is True
            if kind == AtsKind.UNKNOWN and not meaningful and not resume_ok:
                out.status = "needs_sam"
                out.outcome = "not_an_application"
                out.halt_reason = ("only an email field filled on a non-ATS page — this looks like "
                                   "a newsletter/contact form on a careers page, not a job "
                                   "application. Needs the direct apply link.")
                ctx.log("halt", out.halt_reason, outcome="not_an_application")
                # escalate/zero_fields: a newsletter box, not a form the user can answer into.
                _halt("zero_fields", code_source="orchestrator.py:343",
                      code_snippet="not_an_application: only an email field filled on a non-ATS page")
                return out

            # ---- work-auth guard ----
            # The answer call returns a VERIFIED bool — record the answer ONLY when it actually
            # registered. A blank work-auth field that silently failed to set must HALT to
            # the user, never be recorded as answered (which drop_answered would scrub from the
            # missing set, passing a blank required work-auth field as ready_to_submit).
            from .work_auth_policy import resolve_work_auth
            from .work_auth_policy import WorkAuthResolution
            for q in adapter.find_work_auth_questions(page):
                # G4: the answer comes from POLICY + GEOGRAPHY (resolve_work_auth), NOT from a
                # possibly-wrong staged value (a real bug: staged sponsorship="Yes"). The resolver
                # also catches a FOREIGN role (e.g. "Australia (Remote)") and returns NEEDS_HUMAN —
                # the applicant is authorized in the US, so auto-answering "Yes" for a country they
                # aren't authorized in is a truthfulness violation. It halts into a work_auth
                # human_blocker instead of auto-Yes.
                resolution = resolve_work_auth(q.label, role_location)
                ctx.log("work_auth", q.label, decision=resolution.value, widget=q.kind,
                        role_location=role_location)
                if resolution == WorkAuthResolution.SPONSORSHIP_NO:
                    set_ok = adapter.answer_no(page, q)
                    field, ans = "sponsor", "No"
                elif resolution == WorkAuthResolution.AUTHORIZED_YES:
                    set_ok = adapter.answer_yes(page, q)
                    field, ans = "authorized", "Yes"
                elif resolution == WorkAuthResolution.AUTHORIZED_NO_SPONSORSHIP:
                    # combined "authorized WITHOUT requiring sponsorship?" — the affirmative
                    # IS the no-red-flag answer (authorized=yes / sponsorship=no), so click Yes.
                    set_ok = adapter.answer_yes(page, q)
                    field, ans = "authorized_no_sponsorship", "Yes"
                else:  # NEEDS_HUMAN: citizenship/visa/ambiguous OR a geography mismatch — never guess
                    geo_mismatch = (classify_work_auth(q.label) != WorkAuthDecision.HALT)
                    if geo_mismatch:
                        out.halt_reason = (
                            f"work-auth question needs the user — this role is based outside the US "
                            f"({role_location!r}), where your US (TN) authorization doesn't apply; "
                            f"not auto-answering: {q.label}")
                    else:
                        out.halt_reason = f"work-auth question needs the user: {q.label}"
                    out.status = "needs_sam"
                    ctx.log("halt", out.halt_reason)
                    _shot(page, ctx, "halt")
                    # answerable/work_auth: an ambiguous citizenship/visa question OR a foreign-role
                    # geography mismatch — only the user can answer; constrained options, never guessed.
                    _halt("work_auth", question=q.label, options=["Yes", "No"], free_text_ok=True,
                          answer_qkey_source=q.label, code_source="orchestrator.py:445",
                          code_snippet="resolve_work_auth -> NEEDS_HUMAN (citizenship/visa or geography mismatch)")
                    return out
                if not set_ok:
                    out.status = "needs_sam"
                    out.halt_reason = (f"could not set work-auth answer ({field}={ans}) on the "
                                       f"form — needs the user: {q.label}")
                    ctx.log("halt", out.halt_reason, widget=q.kind)
                    _shot(page, ctx, "halt")
                    # escalate/unknown_widget: a FAILED WIDGET SET is never answerable — a value
                    # the user types can't fix a DOM the engine couldn't drive (live-dom rule).
                    _halt("unknown_widget", code_source="orchestrator.py:384",
                          code_snippet="if not set_ok: out.status='needs_sam' (work-auth set failed)")
                    return out
                out.work_auth_answers.append({"field": field, "q": q.label, "answer": ans})

            # ---- office-commitment guard ----
            # In-office / hybrid / RTO / on-site / days-per-week commitment questions are
            # screen-out gates that policy says are always answered YES (feedback_office_
            # commitment_answer). Drive them deterministically BEFORE the custom-question/LLM
            # path so they never escalate to the user. SAME verified-set discipline as work-auth:
            # answer_yes returns a VERIFIED bool, and a failed set HALTs to needs_sam rather
            # than recording a phantom Yes (a wrong auto-Yes here is a serious error). The
            # classifier already excludes relocation/work-auth/EEO/travel, so only true office-
            # commitment questions reach this loop.
            for q in adapter.find_office_commitment_questions(page):
                ctx.log("office_commitment", q.label, decision="AUTO_YES", widget=q.kind)
                if not adapter.answer_yes(page, q):
                    out.status = "needs_sam"
                    out.halt_reason = ("could not set office-commitment answer (Yes) on the "
                                       f"form — needs the user: {q.label}")
                    ctx.log("halt", out.halt_reason, widget=q.kind)
                    _shot(page, ctx, "halt")
                    # escalate/unknown_widget: a failed widget set (the policy answer IS known —
                    # Yes — but the DOM wouldn't take it), so route to a watched run, not the user.
                    _halt("unknown_widget", code_source="orchestrator.py:404",
                          code_snippet="if not adapter.answer_yes(...): (office-commitment set failed)")
                    return out
                out.work_auth_answers.append({"field": "office_commitment",
                                              "q": q.label, "answer": "Yes"})

            # ---- verification (readback) ----
            observed = adapter.read_back(page, list(intended.keys()))
            vr = verify_fields(intended, observed)
            out.verify_ok = vr.ok
            ctx.log("verify", "ok" if vr.ok else "mismatch", mismatches=vr.mismatches)
            if not vr.ok:
                out.status = "needs_sam"
                out.halt_reason = f"verification mismatch: {vr.mismatches}"
                _shot(page, ctx, "verify_fail")
                # escalate/unknown_widget: a field set but did not read back — a DOM the engine
                # can't reliably drive; perception (watched run), not a value the user types.
                _halt("unknown_widget", code_source="orchestrator.py:432",
                      code_snippet="if not vr.ok: out.status='needs_sam' (readback mismatch)")
                return out

            # ---- draft answers to CUSTOM questions (opt-in; grounded + gated) ----
            if answer_fn is not None:
                from .questions import (extract_questions, extract_select_questions,
                                        extract_checkbox_groups)
                from .answer_gen import draft_single_call as _gen
                from .choice_gen import resolve_multi_choice
                from .screening import resolve_with_screening, load_capabilities
                if audit_fn is None:
                    from .llm import make_audit_fn
                    audit_fn = make_audit_fn()
                _audit = audit_fn
                # Capability facts ground the conservative Yes/No screening classifier (truthful
                # qualifiers answered instead of all escalated). Loaded once for both select paths.
                _caps = load_capabilities()
                any_answered = False

                # ── user-provided-answer consume (the re-stage fix) ──────────────────────
                # Before a handler runs the classifier/drafter on a question, it checks whether
                # the user already provided an answer for it (loaded into provided_answers from the
                # prior staged record). If so, it DRIVES that value into the live widget with the
                # SAME driver the path uses, records it as the user's own (answered_by="sam", no
                # fabrication gate), and skips the classifier. HONORS the live-dom rule: if the
                # value will not register in the widget, we DO NOT report a phantom "answered" —
                # the caller records fill_error/escalates exactly as it would for any failed set.
                def _provided_for(label):
                    """The value the user provided for `label`, or None. Pops it so a duplicate
                    live label can't double-consume one provided answer."""
                    if not provided_answers:
                        return None
                    return provided_answers.pop(_qkey(label), None)

                # (a0) custom Yes/No screening qualifiers (button-group / radio / select / react-
                # select) — e.g. "3+ years experience?", "designed LLM apps?", "proficient in
                # Python?". resolve_with_screening grounds them in capabilities (EEO/sensitive/
                # negation escalate INSIDE the classifier); an answered YES/NO is driven via the
                # adapter's VERIFIED answer_yes/answer_no (Ashby reads back the _act class). SAME
                # discipline as work-auth: a failed set is NEVER recorded as answered (which would
                # let drop_answered scrub a blank required screen as ready_to_submit) — it stays in
                # the missing set for the user. ESCALATE → left for the user (not recorded answered).
                for q in adapter.find_screening_yesno_questions(page):
                    _pv = _provided_for(q.label)
                    if _pv is not None:
                        # the user answered this screen himself — drive his Yes/No via the
                        # VERIFIED answer_yes/answer_no, no classifier. Honors the live-dom
                        # rule: a failed set is fill_error, never a phantom answered.
                        rec = {"q": q.label, "kind": "screening-yesno",
                               "status": "answered", "answered_by": "sam", "reason": ""}
                        set_ok = (adapter.answer_yes(page, q)
                                  if _pv.strip().lower() == "yes"
                                  else adapter.answer_no(page, q))
                        if set_ok:
                            rec["value"] = _pv
                            out.work_auth_answers.append(
                                {"field": "screening", "q": q.label, "answer": _pv})
                        else:
                            rec["status"] = "fill_error"
                            rec["reason"] = "provided screening answer did not register on the widget"
                            _halt("unknown_widget", code_source="orchestrator.py:provide-screening",
                                  code_snippet="provided screening answer did not register on the widget")
                        out.generated.append(rec)
                        ctx.log("answer", q.label, status=rec["status"],
                                qkind="screening-yesno", source="sam")
                        any_answered = True
                        continue
                    ch = resolve_with_screening(q.label, ["Yes", "No"], facts, _caps,
                                                answer_fn, _audit)
                    rec = {"q": q.label, "kind": "screening-yesno",
                           "status": ch.status, "reason": ch.reason}
                    if ch.status == "answered" and ch.value:
                        set_ok = (adapter.answer_yes(page, q)
                                  if ch.value.strip().lower() == "yes"
                                  else adapter.answer_no(page, q))
                        if set_ok:
                            rec["value"] = ch.value
                            # record so drop_answered removes it from the missing set
                            out.work_auth_answers.append(
                                {"field": "screening", "q": q.label, "answer": ch.value})
                        else:  # never report a screen answered when the widget didn't register
                            rec["status"] = "fill_error"
                            rec["reason"] = "screening answer did not register on the widget"
                            # escalate/unknown_widget: a failed widget set, never answerable.
                            _halt("unknown_widget", code_source="orchestrator.py:480",
                                  code_snippet="screening answer did not register on the widget")
                    elif rec["status"] not in ("answered", "declined"):
                        # ESCALATED (classifier left it for the user) — answerable/screening_yesno:
                        # a Yes/No qualifier the user can answer via the dashboard (maps to custom_q).
                        _halt("screening_yesno", question=q.label, options=["Yes", "No"],
                              free_text_ok=False, answer_qkey_source=q.label,
                              code_source="orchestrator.py:466",
                              code_snippet="screening qualifier escalated (resolve_with_screening)")
                    out.generated.append(rec)
                    ctx.log("answer", q.label, status=rec["status"], qkind="screening-yesno")
                    any_answered = True

                # (a) free-text essays / short answers
                _essay_qs = extract_questions(page)
                _drafted_essays = []
                for a in _essay_qs:
                    _pv = _provided_for(a.label)
                    if _pv is None:
                        _drafted_essays.append(a)
                        continue
                    # the user provided this answer — type HIS text into the textarea, no drafter,
                    # no fabrication gate. Same fill driver + fill_error discipline as below.
                    rec = {"q": a.label, "kind": a.kind, "status": "answered",
                           "answered_by": "sam", "reason": ""}
                    try:
                        page.fill(a.selector, _pv)
                        rec["value"] = _pv
                    except Exception as e:  # noqa: BLE001 — provided text couldn't be typed
                        rec["status"] = "fill_error"; rec["reason"] = repr(e)[:120]
                        _halt("unknown_widget", code_source="orchestrator.py:provide-essay",
                              code_snippet="provided essay answer could not be typed into the widget")
                    out.generated.append(rec)
                    ctx.log("answer", a.label, status=rec["status"], qkind=a.kind, source="sam")
                    any_answered = True
                drafts = _gen(_drafted_essays, facts, answer_fn, _audit)
                for a in drafts:
                    rec = {"q": a.label, "kind": a.kind, "status": a.status, "reason": a.reason}
                    if a.status in ("answered", "drafted") and a.value:
                        try:
                            page.fill(a.selector, a.value)
                            rec["value"] = a.value
                        except Exception as e:  # noqa: BLE001
                            rec["status"] = "fill_error"; rec["reason"] = repr(e)[:120]
                    out.generated.append(rec)
                    ctx.log("answer", a.label, status=rec["status"], qkind=a.kind)
                    any_answered = True

                # (b) custom <select> dropdowns — pick ONE grounded option or escalate
                for sq in extract_select_questions(page):
                    _pv = _provided_for(sq.label)
                    if _pv is not None:
                        # the user picked this option — select it by visible label, no classifier.
                        # select_option(label=...) raises if no option matches -> fill_error
                        # (the verified set: a value that isn't an option never reports answered).
                        rec = {"q": sq.label, "kind": "select",
                               "status": "answered", "answered_by": "sam", "reason": ""}
                        try:
                            page.select_option(sq.selector, label=_pv)
                            rec["value"] = _pv
                        except Exception as e:  # noqa: BLE001 — provided value isn't a valid option
                            rec["status"] = "fill_error"; rec["reason"] = repr(e)[:120]
                            _halt("unknown_widget", code_source="orchestrator.py:provide-select",
                                  code_snippet="provided <select> value did not match an option")
                        out.generated.append(rec)
                        ctx.log("answer", sq.label, status=rec["status"], qkind="select", source="sam")
                        any_answered = True
                        continue
                    # Huge dropdowns (e.g. Lever's ~thousands-of-universities list) would
                    # blow up the prompt and cost — don't send them to the model; escalate.
                    if len(sq.options) > 60:
                        out.generated.append({"q": sq.label, "kind": "select",
                                              "status": "declined",
                                              "reason": f"too many options ({len(sq.options)}) — left for the user"})
                        ctx.log("answer", sq.label, status="declined", qkind="select")
                        any_answered = True
                        continue
                    # EEO/self-ID rendered as a <select> -> defer to the G5 optional_fill phase
                    # (it owns the applicant's real voluntary values); don't let the screening path try
                    # to "answer" it and fill_error (JOB-281 Together AI race/orientation selects).
                    from .optional_fill import classify_eeo
                    if classify_eeo(sq.label) is not None:
                        out.generated.append({"q": sq.label, "kind": "select", "status": "skipped",
                                              "reason": "EEO/self-ID — deferred to optional_fill"})
                        ctx.log("answer", sq.label, status="skipped", qkind="select")
                        continue
                    # in-office / RTO / relocation rendered as a <select> -> auto-Yes (same policy
                    # as the radio office guard). The radio-based find_office_commitment_questions
                    # misses <select> widgets, so this question reached the answer path and was
                    # wrongly declined -> false HALT (JOB-281 Together AI "4 days/week in office").
                    from .office_commitment import classify_office_commitment, OfficeCommitmentDecision
                    if classify_office_commitment(sq.label) == OfficeCommitmentDecision.AUTO_YES:
                        _yes = next((o for o in sq.options
                                     if o.strip().lower() in ("yes", "y", "yes.")), None)
                        if _yes is not None:
                            rec = {"q": sq.label, "kind": "select", "status": "answered",
                                   "answered_by": "office_commitment",
                                   "reason": "in-office/RTO/relocation auto-Yes"}
                            try:
                                page.select_option(sq.selector, label=_yes); rec["value"] = _yes
                            except Exception as e:  # noqa: BLE001 — never leave a half-set widget
                                rec["status"] = "fill_error"; rec["reason"] = repr(e)[:120]
                            out.generated.append(rec)
                            ctx.log("answer", sq.label, status=rec["status"], qkind="select")
                            any_answered = True
                            continue
                    ch = resolve_with_screening(sq.label, sq.options, facts, _caps,
                                                answer_fn, _audit)
                    rec = {"q": sq.label, "kind": "select",
                           "status": ch.status, "reason": ch.reason}
                    if ch.status == "answered" and ch.value:
                        try:
                            page.select_option(sq.selector, label=ch.value)
                            rec["value"] = ch.value
                        except Exception as e:  # noqa: BLE001 — never leave a half-set widget
                            rec["status"] = "fill_error"; rec["reason"] = repr(e)[:120]
                    out.generated.append(rec)
                    ctx.log("answer", sq.label, status=rec["status"], qkind="select")
                    any_answered = True

                # (c) checkbox-groups ("check all that apply") — check the grounded subset
                for cg in extract_checkbox_groups(page):
                    _pv = _provided_for(cg.label)
                    if _pv is not None:
                        # the user's comma-separated subset — check each named box, no classifier.
                        # An option the user named that isn't on the live group can't be checked:
                        # treat that as fill_error (live-dom rule), never a phantom answered.
                        want = [p.strip() for p in _pv.split(",") if p.strip()] or [_pv.strip()]
                        rec = {"q": cg.label, "kind": "checkbox_group",
                               "status": "answered", "answered_by": "sam", "reason": ""}
                        checked = []
                        try:
                            for val in want:
                                if val not in cg.options:
                                    raise ValueError(f"provided option {val!r} not on this group")
                                page.check(cg.selectors[cg.options.index(val)])
                                checked.append(val)
                            rec["values"] = checked
                        except Exception as e:  # noqa: BLE001 — provided subset couldn't be checked
                            rec["status"] = "fill_error"; rec["reason"] = repr(e)[:120]
                            _halt("unknown_widget", code_source="orchestrator.py:provide-checkbox",
                                  code_snippet="provided checkbox-group subset could not be checked")
                        out.generated.append(rec)
                        ctx.log("answer", cg.label, status=rec["status"],
                                qkind="checkbox_group", source="sam")
                        any_answered = True
                        continue
                    # EEO/self-ID "mark all that apply" (race / ethnicity / orientation / gender)
                    # -> defer to the G5 optional_fill phase, don't try to "answer" + fill_error it
                    # (JOB-281 Together AI racial/ethnic + sexual-orientation checkbox groups).
                    from .optional_fill import classify_eeo as _classify_eeo_cg
                    if _classify_eeo_cg(cg.label) is not None:
                        out.generated.append({"q": cg.label, "kind": "checkbox_group",
                                              "status": "skipped",
                                              "reason": "EEO/self-ID — deferred to optional_fill"})
                        ctx.log("answer", cg.label, status="skipped", qkind="checkbox_group")
                        continue
                    mc = resolve_multi_choice(cg.label, cg.options, facts, answer_fn, _audit)
                    rec = {"q": cg.label, "kind": "checkbox_group",
                           "status": mc.status, "reason": mc.reason}
                    if mc.status == "answered" and mc.values:
                        checked = []
                        try:
                            for val in mc.values:
                                sel = cg.selectors[cg.options.index(val)]
                                page.check(sel)
                                checked.append(val)
                            rec["values"] = checked
                        except Exception as e:  # noqa: BLE001
                            rec["status"] = "fill_error"; rec["reason"] = repr(e)[:120]
                    out.generated.append(rec)
                    ctx.log("answer", cg.label, status=rec["status"], qkind="checkbox_group")
                    any_answered = True

                # (d) custom React-select questions (modern Greenhouse: Yes/No screening +
                # searchable dropdowns are .select__control, not native <select>). Same
                # grounded+gated picker as native selects; the chosen option is clicked via
                # the react-select driver. Work-auth/EEO/standard fields are excluded already.
                from .questions import extract_react_select_questions
                for rq in extract_react_select_questions(page):
                    _pv = _provided_for(rq.label)
                    if _pv is not None:
                        # the user picked this react-select option — click it via the VERIFIED
                        # select_react_by_label (returns True only if the chip reads back). A
                        # value that won't register is fill_error, never a phantom answered
                        # (live-dom rule). This is the Anthropic "AI Policy" / "interviewed
                        # before?" case the re-stage loop got stuck on.
                        rec = {"q": rq.label, "kind": "react_select",
                               "status": "answered", "answered_by": "sam", "reason": ""}
                        if adapter.select_react_by_label(page, rq.label, _pv):
                            rec["value"] = _pv
                        else:
                            rec["status"] = "fill_error"
                            rec["reason"] = "provided react-select option did not register"
                            _halt("unknown_widget", code_source="orchestrator.py:provide-react",
                                  code_snippet="provided react-select option did not register")
                        out.generated.append(rec)
                        ctx.log("answer", rq.label, status=rec["status"],
                                qkind="react_select", source="sam")
                        any_answered = True
                        continue
                    if len(rq.options) > 60:
                        out.generated.append({"q": rq.label, "kind": "react_select",
                                              "status": "declined",
                                              "reason": f"too many options ({len(rq.options)}) — left for the user"})
                        ctx.log("answer", rq.label, status="declined", qkind="react_select")
                        any_answered = True
                        continue
                    # Modern Greenhouse renders office/EEO/screening as react-selects, NOT native
                    # <select> — so the same guards the native-select path uses must run here too
                    # (JOB-281 Together AI: office + race/orientation/transgender are react-selects).
                    from .optional_fill import classify_eeo as _eeo_rs
                    if _eeo_rs(rq.label) is not None:
                        out.generated.append({"q": rq.label, "kind": "react_select", "status": "skipped",
                                              "reason": "EEO/self-ID — deferred to optional_fill"})
                        ctx.log("answer", rq.label, status="skipped", qkind="react_select")
                        continue
                    from .office_commitment import (classify_office_commitment as _oc_rs,
                                                    OfficeCommitmentDecision as _OCD_rs)
                    if _oc_rs(rq.label) == _OCD_rs.AUTO_YES:
                        _yes = next((o for o in rq.options
                                     if o.strip().lower() in ("yes", "y", "yes.")), None)
                        if _yes is not None:
                            rec = {"q": rq.label, "kind": "react_select", "status": "answered",
                                   "answered_by": "office_commitment",
                                   "reason": "in-office/RTO/relocation auto-Yes"}
                            if adapter.select_react_by_label(page, rq.label, _yes):
                                rec["value"] = _yes
                            else:  # never leave a half-set widget reported as answered
                                rec["status"] = "fill_error"
                                rec["reason"] = "react-select Yes did not register"
                            out.generated.append(rec)
                            ctx.log("answer", rq.label, status=rec["status"], qkind="react_select")
                            any_answered = True
                            continue
                    ch = resolve_with_screening(rq.label, rq.options, facts, _caps,
                                                answer_fn, _audit)
                    rec = {"q": rq.label, "kind": "react_select",
                           "status": ch.status, "reason": ch.reason}
                    if ch.status == "answered" and ch.value:
                        # NOTE: unlike the standard State/Country path (which tries a [full, abbrev]
                        # candidate list via _select_react_first_match), the custom path passes a
                        # SINGLE value. That is safe here because resolve_choice is constrained to
                        # rq.options — the options enumerated from THIS live control — so ch.value is
                        # already keyed the way the select expects (code vs. full word). If a future
                        # board is found where the rendered option text differs from the submittable
                        # value, give this callsite a candidate-list fallback like the location path.
                        if adapter.select_react_by_label(page, rq.label, ch.value):
                            rec["value"] = ch.value
                        else:  # never leave a half-set widget reported as answered
                            rec["status"] = "fill_error"
                            rec["reason"] = "react-select option click did not register"
                            # escalate/unknown_widget: a react-select the engine couldn't drive —
                            # a value the user types can't fix the DOM (live-dom rule), so escalate.
                            _halt("unknown_widget", code_source="orchestrator.py:580",
                                  code_snippet="react-select option click did not register")
                    out.generated.append(rec)
                    ctx.log("answer", rq.label, status=rec["status"], qkind="react_select")
                    any_answered = True

                if any_answered:
                    _shot(page, ctx, "answered")

            # ---- G5: answer EVERY field — optionals + EEO --------------------------
            # AFTER the required + custom fill, fill any STILL-EMPTY optional/EEO field from the
            # stored applicant profile (gender/race/hispanic/veteran/disability + start date /
            # pronunciation / deadlines / additional-info / phone-country; website stays blank
            # unless a real url is stored). A blank optional reads as a lazy autopilot submit
            # (feedback_apply_answer_every_field). BEST-EFFORT / NON-BREAKING: enumerate +
            # fill is wrapped so an optional-fill failure can NEVER fail the stage — a form
            # with no optional fields behaves exactly as before.
            try:
                from .optional_fill import fill_optional_and_eeo
                _opt_spec = adapter.enumerate_fields(page)
                _opt_report = fill_optional_and_eeo(
                    page, _opt_spec, _load_profile(), adapter=adapter)
                if _opt_report.get("filled"):
                    out.optional_filled = dict(_opt_report["filled"])
                    ctx.log("optional_fill", "filled optional/EEO fields",
                            fields=list(_opt_report["filled"].keys()))
                    _shot(page, ctx, "optional_filled")
                if _opt_report.get("skipped"):
                    ctx.log("optional_fill", "skipped undrivable optional/EEO fields",
                            fields=_opt_report["skipped"])
            except Exception as _e:  # noqa: BLE001 — G5 is additive; never break the stage.
                ctx.log("optional_fill", f"skipped (best-effort): {_e!r}"[:160])

            # ---- audit ATS-injected fields + correct wrong ones --------------------
            # ATSs parse the uploaded resume and silently auto-fill identity/employment
            # fields the engine never set (live: Lever put "Current company: BUILDS" from
            # a resume heading). verify_fields only checks the engine's own fields, so this
            # is the only place those get caught. Deterministic (no LLM) -> always runs.
            # The parse is ASYNC (the value lands a few seconds after the resume upload), so
            # settle briefly before reading or a freshly-injected wrong value is missed.
            page.wait_for_timeout(3000)
            from .form_audit import audit_form, apply_corrections
            corrections = audit_form(page, _load_profile())
            if corrections:
                applied = apply_corrections(page, corrections)
                applied_sel = {a["selector"] for a in applied}
                for c in corrections:
                    was_applied = c.action == "overwrite" and c.selector in applied_sel
                    ctx.log("audit", c.label, action=c.action, current=c.current,
                            correct=c.correct, applied=was_applied)
                    out.corrections.append({"label": c.label, "action": c.action,
                                            "current": c.current, "correct": c.correct,
                                            "applied": was_applied})
                if applied:
                    _shot(page, ctx, "audited")

            # ---- completeness: no blank required field may hide behind "ready" ----
            # (drop work-auth questions already answered via JS widgets the scanner can't read)
            # yesno_button_groups catches Ashby-style custom Y/N widgets that have no native
            # input — without it a required Y/N (e.g. "willing to relocate?") the engine
            # didn't answer would slip through as a false ready_to_submit.
            from .completeness import yesno_button_groups, react_select_unfilled
            missing = drop_answered(
                unfilled_required(page) + yesno_button_groups(page)
                + react_select_unfilled(page),
                [w["q"] for w in out.work_auth_answers])
            # Only a form that ACTUALLY HAS a resume input can fail to attach one. The adapter's
            # resume_selector is a constant, so on a form with no file input `resume_attached_ok`
            # is False simply because there was nothing to attach to — flagging that as "did not
            # attach" was a false blocker that forced clean no-resume forms to needs_input. Gate on
            # the input being present on the live page.
            if (adapter.resume_selector and adapter.resume_attached_ok is False
                    and page.query_selector(adapter.resume_selector) is not None):
                missing.insert(0, "Resume (did not attach)")
            adapter.go_to_review(page)
            _reached[0] = "review"
            _shot(page, ctx, "review_brink")

            # ---- captcha pre-check: a captcha-gated form can never be auto-submitted ----
            # The engine never solves captchas. If one is present, divert to the user for a
            # MANUAL submit (he solves it + clicks submit) — never a hard failure, and never a
            # false ready_to_submit. INVISIBLE reCAPTCHA (background scoring) returns None and is
            # correctly ignored here so it doesn't divert every Greenhouse/Ashby application.
            from .captcha import detect_captcha
            cap = detect_captcha(page)
            if cap:
                out.status = "needs_sam"
                out.halt_reason = (f"captcha-gated ({cap}) — manual submit required: the user "
                                   "solves the captcha and clicks submit")
                ctx.log("captcha", out.halt_reason, captcha_kind=cap)
                # escalate/captcha: only a human-in-browser can solve it — never an answer box.
                _halt("captcha", code_source="orchestrator.py:641",
                      code_snippet="if cap: out.status='needs_sam' (captcha-gated)")
                return out

            if missing:
                out.status = "needs_input"
                out.unfilled_required = missing
                out.halt_reason = ("required fields still need the user: "
                                   + "; ".join(missing[:12]))
                ctx.log("needs_input", "required fields unfilled", fields=missing)
                # First missing item is the surfaced question. A real labeled question (has a space
                # or '?') is answerable/missing_value; a bare field key / UUID token has no human
                # question to answer -> escalate/unknown_widget (mirrors aria_server._is_raw_field_key
                # discipline). Resume-attach failure is a perception job -> escalate/file_upload.
                first = missing[0]
                if first == "Resume (did not attach)":
                    _halt("file_upload", code_source="orchestrator.py:629",
                          code_snippet="missing.insert(0, 'Resume (did not attach)')")
                elif is_raw_field_key(first):
                    _halt("unknown_widget", code_source="orchestrator.py:648",
                          code_snippet="unfilled required is a raw field key (no human question)")
                else:
                    _halt("missing_value", question=first, free_text_ok=True,
                          answer_qkey_source=first, code_source="orchestrator.py:648",
                          code_snippet="if missing: out.status='needs_input' (required field unfilled)")
                return out

            # ---- Phase 4b: capture the LIVE-form model + G1/G2 checks at the brink ----
            # AFTER fields are filled and BEFORE we declare the stage. Best-effort: wrapped in
            # try/except inside _capture_form_model so it can NEVER change the stage outcome — a
            # throw leaves no form_spec/reconcile/compliance and the pass-when-absent G-hooks keep
            # verify_ready safe. The page is on the review brink (go_to_review already ran).
            _capture_form_model(out, page, adapter, ctx)

            ctx.log("stage", "staged to submit brink — NOT submitted", dry_run=dry_run)
            out.status = "ready_to_submit"
            out.submitted = False
            return out
    except Exception as e:  # noqa: BLE001 — surface any failure, never half-submit
        out.status = "error"; out.error = repr(e)
        ctx.log("error", repr(e))
        return out
