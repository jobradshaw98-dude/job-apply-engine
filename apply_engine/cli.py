"""CLI entrypoint for apply_engine. Loads a job, ensures tailored PDFs, runs the
orchestrator, prints the audit summary, and records status in applications.json.
Submission is always manual — this never submits."""
import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from . import config
from .source_data import build_answers
from .orchestrator import apply_to_job
from .liveness import check_posting_liveness  # Bug #3 pre-flight (used in main())

# the statuses that represent a SUCCESSFUL stage (a clean record reached the review brink).
# needs_sam / needs_input / error / skipped are NOT successful stages and must NOT trigger
# an accuracy review (there is nothing review-ready to audit).
_STAGE_SUCCESS = {"ready_to_submit"}


def _load_jobs(jobs_path: Path) -> list:
    d = json.loads(Path(jobs_path).read_text(encoding="utf-8"))
    jobs = d if isinstance(d, list) else d.get("jobs", d)
    return list(jobs.values()) if isinstance(jobs, dict) else jobs


def find_job(jobs_path: Path, job_id: str) -> Optional[dict]:
    for j in _load_jobs(jobs_path):
        if j.get("id") == job_id:
            return j
    return None


def record_status(apps_path: Path, job_id: str, status: str, run_dir: str, note: str = "") -> None:
    apps_path = Path(apps_path)
    data = json.loads(apps_path.read_text(encoding="utf-8")) if apps_path.exists() else []
    if isinstance(data, dict):
        data = data.get("applications", [])
    row = next((r for r in data if r.get("job_id") == job_id), None)
    if row is None:
        row = {"job_id": job_id}
        data.append(row)
    row["status"] = status
    row["apply_run_dir"] = run_dir
    if note:
        row["apply_note"] = note
    apps_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def build_hooks(answer: bool, job: dict, recon: bool = False) -> tuple:
    """Wire the custom-question conversion path. When --answer is set, return the real
    Claude drafter + fabrication audit gate + grounding facts (resume + claims ledger + JD)
    so the engine can fill essays/dropdowns. Default-off returns (None, None, "") — the
    engine then escalates every custom question to the user (the safe default). Every drafted
    answer still requires the career-draft-auditor subagent + the user's eyes before submit.

    When `recon` is set (P3, real apply path only — never in unit tests), the web-enabled recon
    agent researches the company/role first and its brief is threaded into the drafter's grounding
    so answers are company-specific. Recon is best-effort: any failure leaves the brief empty and
    drafting proceeds on the corpus+JD floor.

    Degrades safely: if the LLM/gate can't be constructed (missing brief_config / api key /
    audit_gate import), it prints why and falls back to (None, None, "") so the run still
    proceeds with custom questions escalated — never a hard crash before the browser opens."""
    if not answer:
        return None, None, ""
    try:
        from .llm import make_single_call_agent, make_audit_fn, load_facts, run_recon
        # SINGLE-CALL engine (2026-06-17): ONE guarded agentic call drafts + self-critiques +
        # finalizes the whole answer set (measured equal-quality to the old multi-pass pipeline at
        # ~6x cheaper/faster). orchestrator.draft_single_call parses + gates it. Recon is LEAN
        # (search-only, tight brief) by default — it's the cost center, and the drafter only needs
        # company/values/hooks. The full networking recon stays a separate opt-in tool.
        brief = ""
        if recon and job.get("jd_text"):
            try:
                brief = run_recon(job, lean=True)
                print("  recon (lean): company/role brief generated.")
            except Exception as e:  # noqa: BLE001 — recon is best-effort; draft on the floor.
                print(f"  recon skipped ({type(e).__name__}: {e}); drafting on corpus+JD only.")
        return make_single_call_agent(), make_audit_fn(), load_facts(job, recon_brief=brief)
    except Exception as e:
        print(f"  --answer unavailable ({type(e).__name__}: {e}); "
              f"falling back to escalate-every-custom-question.")
        return None, None, ""


def _company_slug(company: str) -> str:
    """Mirror build.py's output-folder slug exactly: strip a trailing parenthetical, then
    swap spaces and slashes for hyphens. build.py uses
    company.split('(')[0].strip().replace(' ', '-').replace('/', '-')."""
    return str(company or "").split("(")[0].strip().replace(" ", "-").replace("/", "-")


def _tailored_app_dir(job: dict) -> Optional[Path]:
    """Resolve the build pipeline's per-application output folder for this job.

    build.py emits to applications/<APP-id>-<CompanySlug>/, keyed by the APPLICATION id
    (APP-028), not the JOB id (JOB-216). So we look up the applications.json record by
    job_id to recover the APP-id + company, then reconstruct the folder. Returns the
    Path if it exists on disk, else None. Read-only; never writes applications.json."""
    apps_path = config.APPLICATIONS_JSON
    if not apps_path.exists():
        return None
    try:
        data = json.loads(apps_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    rec = next((a for a in data
                if isinstance(a, dict) and a.get("id") and a.get("job_id") == job.get("id")), None)
    if not rec:
        return None
    folder = f"{rec['id']}-{_company_slug(rec.get('company', ''))}"
    cand = config.PKG_DIR.parent / "applications" / folder
    return cand if cand.is_dir() else None


def _first_existing(folder: Path, names: list) -> Optional[Path]:
    """Return the first name in `names` that exists inside `folder`, else None."""
    for n in names:
        p = folder / n
        if p.exists():
            return p
    return None


class NoTailoredPDF(RuntimeError):
    """Raised by ensure_pdfs when a selected job has no tailored resume PDF and the silent
    master-resume fallback is NOT explicitly opted into. main() converts this into a
    `needs_build` halt — a selected job must get a tailored package, never the generic master.
    See feedback_apply_quality_pipeline: no tailored package => halt, never generic."""


def ensure_pdfs(job: dict, *, allow_master: bool = False) -> tuple:
    """Resolve the tailored resume + cover PDFs the /career build pipeline produced for this job.
    Returns (resume_pdf, cover_pdf).

    Resolution order:
      1. The build pipeline's per-application output folder, applications/<APP-id>-<slug>/,
         located by matching the applications.json record on job_id. build.py emits
         APPLICANT_Resume.pdf / APPLICANT_Cover_Letter.pdf there; older runs may have
         emitted plain resume.pdf / cover.pdf, so both names are accepted.
      2. Legacy layout applications/<JOB-id>/resume.pdf (kept for any pre-existing per-job folders).

    If no tailored resume PDF resolves: by default raise NoTailoredPDF (the caller HALTs the run to
    `needs_build`). The QUALITY CONTRACT forbids silently attaching the generic master resume to a
    selected job. The old silent master-resume last-resort is removed; it now only fires behind the
    explicit `allow_master=True` debug opt-in, which the real live-stage path NEVER passes."""
    resume_pdf = None
    cover_pdf = None

    app_dir = _tailored_app_dir(job)
    if app_dir is not None:
        resume_pdf = _first_existing(app_dir, ["APPLICANT_Resume.pdf", "resume.pdf"])
        cover_pdf = _first_existing(app_dir, ["APPLICANT_Cover_Letter.pdf", "cover.pdf"])

    if resume_pdf is None:
        legacy = config.PKG_DIR.parent / "applications" / job.get("id", "")
        resume_pdf = _first_existing(legacy, ["APPLICANT_Resume.pdf", "resume.pdf"])
        if cover_pdf is None:
            cover_pdf = _first_existing(legacy, ["APPLICANT_Cover_Letter.pdf", "cover.pdf"])

    if resume_pdf is None:
        if not allow_master:
            raise NoTailoredPDF(
                f"no tailored resume PDF found for {job.get('id', '?')} "
                f"({job.get('company', '')}) — refusing to attach the generic master resume")
        # Explicit debug opt-in only: the live-stage path never reaches here.
        master = config.PKG_DIR.parent / "APPLICANT_Resume_Master.docx"
        master_pdf = master.with_suffix(".pdf")
        resume_pdf = master_pdf if master_pdf.exists() else master

    return resume_pdf, cover_pdf


def _utf8_stdout() -> None:
    """Force UTF-8 console output. Live ATS question labels carry non-cp1252 characters
    (e.g. Lever's heavy-asterisk required marker U+2731), which crash the default Windows
    cp1252 console with UnicodeEncodeError mid-print — aborting the run AFTER it staged but
    BEFORE it records status. errors='replace' guarantees a print can never crash the run."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _manifest_record(manifest_path: Path, job_id: str) -> Optional[dict]:
    """Read the freshly-written staged record for job_id from the manifest, or None.
    Best-effort: a missing/corrupt manifest or unknown id returns None (never raises)."""
    try:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    for entry in data:
        if isinstance(entry, dict) and entry.get("job_id") == job_id:
            return entry
    return None


def chain_accuracy_review(outcome, *, answered: bool) -> Optional[str]:
    """After a SUCCESSFUL --answer stage that drafted custom answers, auto-run the
    application-level accuracy review so the staged card arrives review-ready instead of
    locked behind a manual "Re-run accuracy review" click.

    This is a THIN orchestration call: all audit logic lives in refresh_audit.refresh (the
    same reusable run-function the dashboard's manual re-run uses). We do not re-implement the
    audit here — we just decide WHETHER to run it and call it in-process for this job_id.

    Guards (run the review only when it is meaningful):
      * answered           — the run was an --answer run (otherwise no answers were drafted).
      * outcome.status is a SUCCESSFUL stage (ready_to_submit). A needs_sam / needs_input /
        error / skipped stage never produced a review-ready record, so there is nothing to audit
        — we must not fabricate a verdict for it.
      * the freshly-written manifest record actually has FILLED custom answers (drafts_for_audit
        is non-empty). A stage with zero custom questions needs no accuracy review — running it
        would stamp a vacuous verdict, so we skip and leave the record audit-free.
      * no audit was already stamped on the record (don't double-audit). The stage path itself
        never stamps one today, but this keeps the chain idempotent if that ever changes.

    NON-RAISING: the accuracy review is supplementary review metadata. Any failure (Claude CLI
    down, an exception in refresh) must NEVER fail or crash the stage run — we catch everything,
    print a clear line, and leave the record un-audited (Submit simply stays locked, exactly as
    today). The stage's own exit code reflects the STAGE outcome, not the audit.

    Returns a short result tag ("PASS" / "BLOCKED" / "gate-only" / "skipped" / "error:<...>")
    for the caller to print/inspect; never None except when not applicable at all."""
    if not answered:
        return None
    if getattr(outcome, "status", "") not in _STAGE_SUCCESS:
        return None

    job_id = getattr(outcome, "job_id", "") or ""
    manifest_path = config.ARIA_DATA / "staged_applications.json"

    # Only audit when there ARE filled custom answers on the freshly-staged record. This mirrors
    # EXACTLY what refresh() would audit (both go through drafts_for_audit), so the guard and the
    # audit can never disagree: no filled custom answers -> nothing to review -> skip.
    try:
        from .draft_audit import load_job_drafts
        drafts = load_job_drafts(manifest_path, job_id)
    except Exception as e:  # noqa: BLE001 — guard read must never crash the stage
        print(f"  accuracy review: skipped (could not read staged answers: {type(e).__name__})")
        return "skipped"
    # NOTE: we do NOT skip a zero-custom-question stage anymore. finish.can_submit now fail-closes
    # on a record with NO `audit` stamp ("the deterministic gate never ran ⇒ refuse"), so every
    # staged record must carry a deterministic stamp — even a standard-fields-only app gets a clean
    # gate_blocks:0 stamp below (refresh on empty drafts stamps 0). This closes the SEV-HIGH hole
    # where a never-stamped ready_to_submit record read gate_blocks=0-by-default and auto-submitted.

    # don't double-audit: if something already stamped a verdict on this record, leave it.
    rec = _manifest_record(manifest_path, job_id)
    if isinstance(rec, dict) and isinstance(rec.get("audit"), dict) and rec["audit"].get("verdict"):
        print("  accuracy review: already stamped — skipping")
        return "skipped"

    # Run the real review in-process (refresh constructs the real deterministic gate + claude -p
    # judge when deps are omitted, and DEGRADES SAFELY if the Claude CLI is unavailable: judge_ran
    # goes False, the verdict fails closed to BLOCKED, and finish.can_submit keeps Submit locked).
    # NEVER uses the metered API. Wrapped so an audit failure can't fail the stage.
    #
    # include_quality=True: this is the ONE place a freshly staged app gets its single holistic
    # quality pass (incl. the grounded calibration gate). Every LATER refresh — the dashboard
    # accuracy-review button, every post-edit re-check — runs fabrication-only and leaves this
    # quality_audit untouched, so the quality judge never spins up a fresh batch of advisory fixes
    # on each edit (the treadmill fix, 2026-06-10).
    # DETERMINISTIC-ONLY at stage (2026-06-22): the LLM accuracy + quality judges
    # were demoted to advisory/on-demand, so stage-end stamps ONLY the deterministic gate verdict
    # (gate_blocks) — no claude -p, no quota burn, no holistic quality pass. finish.can_submit now
    # gates on gate_blocks alone, so this stamp is what locks/unlocks Submit. The LLM judges still
    # run on-demand via the dashboard "re-run accuracy review" button (advisory display only).
    try:
        from .refresh_audit import refresh
        audit = refresh(job_id, manifest_path=manifest_path, deterministic_only=True)
    except Exception as e:  # noqa: BLE001 — audit failure must never crash/fail the stage run
        print(f"  accuracy review: error ({type(e).__name__}: {e}) — record left un-audited, "
              f"Submit stays locked")
        return f"error:{type(e).__name__}"

    if isinstance(audit, dict) and audit.get("error"):
        print(f"  accuracy review: error ({audit['error']}) — record left un-audited")
        return f"error:{audit['error']}"

    n_gate = int((audit or {}).get("gate_blocks", 0) or 0)
    tag = "PASS" if n_gate == 0 else f"BLOCKED ({n_gate} deterministic gate finding(s))"
    print(f"  accuracy review (deterministic): {tag}")
    return tag


def _converge_after_stage(outcome, *, answered: bool) -> Optional[str]:
    """Phase 4d — drive the autonomous quality-convergence loop after a successful --answer stage.

    On a real --answer stage (answered=True) this runs converge.converge_quality(job_id), whose
    round 1 IS the single quality pass chain_accuracy_review used to run, and which then converges
    (or surfaces a human-only blocker / exhausted / error). It REPLACES the single chain pass on the
    real path. On a non-answer / dry stage (answered=False) it falls back to the cheap single-pass
    chain_accuracy_review (which itself no-ops on answered=False) so the cli surface is unchanged for
    dry runs and so the existing chain tests/behaviour still hold.

    NON-RAISING: any failure in the loop (or in the import) must NEVER crash/fail the stage — caught,
    a clear line printed, the record left as the loop last wrote it. The stage's exit code reflects
    the STAGE outcome, not the loop. Returns the loop's terminal tag (or the chain tag on the fallback
    path), or None when not applicable — purely for the caller to print/inspect."""
    if not answered:
        # dry / non-answer stage: keep the original single-pass behaviour (it no-ops here anyway).
        return chain_accuracy_review(outcome, answered=False)
    job_id = getattr(outcome, "status", "") in _STAGE_SUCCESS and (getattr(outcome, "job_id", "") or "")
    # Only a SUCCESSFUL stage produced a review-ready record to converge; a needs_sam/error stage
    # has nothing to converge (the loop's own guard also enforces this, but short-circuit cleanly).
    if not job_id:
        return None
    try:
        # Quality convergence (the LLM loop) is RETIRED from the stage path (2026-06-22): the
        # holistic quality judge is advisory/on-demand now, so stage-end runs only the cheap
        # deterministic accuracy stamp — no claude -p, no convergence treadmill. _reconcile keeps
        # status honest against can_submit (now deterministic-gate-only).
        tag = chain_accuracy_review(outcome, answered=True)
        print(f"  accuracy (deterministic): {tag}")
        # Bug #4 — STATUS-INTEGRITY reconciliation. converge can skip/error/pause/exhaust/block and
        # still leave the staged record at status=ready_to_submit WITHOUT a fresh PASS audit verdict
        # (the audit lives INSIDE converge). That makes the dashboard show green "ready" while
        # finish.can_submit (the fail-closed authority) would refuse the submit — a LYING status.
        # Reconcile every time converge returns, regardless of tag, using can_submit as the single
        # source of truth.
        _reconcile_ready_status(job_id, converge_tag=tag)
        return tag
    except Exception as e:  # noqa: BLE001 — the loop must never fail the stage
        print(f"  converge: error ({type(e).__name__}: {e}) — stage unaffected, record left as-is")
        # Even on a converge crash, reconcile so a pre-existing ready_to_submit that no longer
        # passes can_submit can't survive as a false-green (best-effort; never re-raises).
        try:
            _reconcile_ready_status(job_id, converge_tag=f"error:{type(e).__name__}")
        except Exception:  # noqa: BLE001
            pass
        return f"error:{type(e).__name__}"


def _reconcile_ready_status(job_id: str, *, converge_tag: str = "") -> Optional[str]:
    """Enforce the invariant: a staged record may carry status `ready_to_submit` ONLY if
    finish.can_submit(record) returns (True, ""). If the record is stamped ready_to_submit but
    can_submit refuses (e.g. converge skipped on a LockTimeout and wiped the audit verdict, or the
    judge never ran), DOWNGRADE it OFF ready_to_submit so the dashboard never shows a false "ready".

    finish.can_submit is the SINGLE SOURCE OF TRUTH — we do NOT re-derive the conditions here, and we
    NEVER alter can_submit's fail-closed behaviour. This is purely a status reconciliation.

    Downgrade target = `needs_sam` (not `needs_input`): `needs_input` connotes a specific required
    form FIELD still needs a value, whereas the failure here is "the mandatory accuracy review didn't
    run or didn't pass" — a human-review condition. Both are in finish._NOT_REVIEW_READY (the
    dashboard treats them identically as not-review-ready), but `needs_sam` reads truthfully.

    ONE-WAY DOWNGRADE: only acts on a record currently at ready_to_submit; a non-ready status is left
    untouched (recompute_status is the monotonic UPGRADE valve — this is its downgrade counterpart). A
    submitted record is never rewritten. Returns the record's status after reconciliation (or None on
    a missing/corrupt manifest). Merge-safe (re-reads under the filemutex) and non-raising — a
    reconciliation failure must never fail the stage."""
    import os

    from .finish import can_submit

    manifest = config.ARIA_DATA / "staged_applications.json"
    if not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, list):
        return None
    rec = next((r for r in data if isinstance(r, dict) and r.get("job_id") == job_id), None)
    if rec is None:
        return None

    current = rec.get("status") or ""
    if rec.get("submitted") or current != "ready_to_submit":
        return current  # only DOWNGRADE a (non-submitted) ready_to_submit; leave everything else

    ok, reason = can_submit(rec)
    if ok:
        return current  # a genuine PASS — ready_to_submit STANDS

    # The lie: stamped ready_to_submit but can_submit refuses. Downgrade, merge-safe, atomically.
    try:
        from .filemutex import locked
        with locked(manifest):
            try:
                fresh = json.loads(manifest.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                return current
            if not isinstance(fresh, list):
                return current
            for entry in fresh:
                if isinstance(entry, dict) and entry.get("job_id") == job_id:
                    # re-check against the FRESH record under the lock (its state may have changed)
                    if entry.get("submitted") or (entry.get("status") or "") != "ready_to_submit":
                        return entry.get("status") or ""
                    fresh_ok, fresh_reason = can_submit(entry)
                    if fresh_ok:
                        return "ready_to_submit"
                    entry["status"] = "needs_sam"
                    reason = fresh_reason or reason
                    break
            tmp = manifest.with_suffix(manifest.suffix + ".tmp")
            tmp.write_text(json.dumps(fresh, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, manifest)
    except Exception:  # noqa: BLE001 — never fail the stage on a reconciliation hiccup
        return current
    print(f"  status reconciled: ready_to_submit -> needs_sam "
          f"(converge={converge_tag or '?'}; can_submit refused: {reason})")
    return "needs_sam"


def _app_record_for(job_id: str) -> Optional[dict]:
    """Return the applications.json record matching this job_id, or None. Read-only."""
    apps_path = config.APPLICATIONS_JSON
    if not apps_path.exists():
        return None
    try:
        data = json.loads(apps_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict):
        data = data.get("applications", [])
    if not isinstance(data, list):
        return None
    return next((a for a in data
                 if isinstance(a, dict) and a.get("job_id") == job_id), None)


def _has_tailored_content(rec: Optional[dict]) -> bool:
    """True iff the APP record already carries a non-empty tailored resume AND cover dict.

    'Non-empty' means both keys are dicts with actual content — an empty {} resume or a record
    with only one of the two is treated as incomplete (regenerate). the user may have hand-edited
    the tailored content via the dashboard, so when it IS present we use it as-is and never
    regenerate over his edits."""
    if not isinstance(rec, dict):
        return False
    resume = rec.get("resume")
    cover = rec.get("cover")
    return bool(isinstance(resume, dict) and resume) and bool(isinstance(cover, dict) and cover)


def ensure_tailored_package(job: dict) -> str:
    """Guarantee a tailored resume+cover package + rendered PDFs exist for THIS job before staging.

    Fires only on the real live-stage path (see main). Behaviour:
      * APP record already has a non-empty tailored resume AND cover -> keep the stored content
        as-is (don't clobber a dashboard hand-edit), but ALWAYS re-render the PDFs from that
        content first. A dashboard edit can update applications.json without re-rendering, leaving
        on-disk PDFs that no longer match the stored content; re-rendering (~seconds) guarantees
        the attached PDFs always match the current tailored content. A render hang here is covered
        by _render's 180s timeout -> TailorError -> needs_build halt. Returns "existing".
      * absent/incomplete -> call tailor.generate_tailored_package(job), then reuse tailor's OWN
        atomic+mutex write helper (_write_app_record) and its build.py render (_render) so we never
        duplicate the applications.json write or the PDF rendering. Returns "generated".

    Raises tailor.TailorError / ValueError / LLMUnavailable straight up on any failure (thin JD,
    LLM down, validation-exhausted). main() catches these and HALTs the run to `needs_build` —
    we NEVER fall back to the generic master resume. This is the QUALITY CONTRACT enforcement
    point: a selected job gets a tailored package or it gets halted, surfaced as needs_build."""
    from . import tailor

    rec = _app_record_for(job.get("id", ""))
    if _has_tailored_content(rec):
        # rec may be an engine-written stub (no APP id) that had tailored content spliced in;
        # ensure_app_id backfills + persists a real id so _render never gets "?".
        app_id = tailor.ensure_app_id(job.get("id", ""))
        print(f"  tailored package already present on {app_id} — using stored content "
              f"(no regenerate); re-rendering PDFs to match...")
        # Re-render from the CURRENT stored content so the attached PDFs can never be stale
        # relative to a dashboard edit that updated applications.json but skipped the render.
        tailor._render(app_id)
        print(f"  re-rendered tailored PDFs for {app_id}")
        return "existing"

    print(f"  no tailored package for {job.get('id', '?')} — generating "
          f"(2 claude -p calls, ~250-350s)...")
    pkg = tailor.generate_tailored_package(job)          # raises on failure — never generic
    app_id = tailor._write_app_record(job.get("id", ""), pkg)  # atomic + mutex (reuse, no dup)
    print(f"  wrote tailored package to {app_id}; rendering PDFs...")
    tailor._render(app_id)                               # build.py render (reuse, no dup)
    print(f"  rendered tailored PDFs for {app_id}")
    return "generated"


def main(argv=None) -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(prog="apply_engine")
    ap.add_argument("--job", required=True, help="Job id, e.g. JOB-131")
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--live", dest="dry_run", action="store_false",
                    help="Allow real navigation/login (still never submits)")
    ap.add_argument("--headless", action="store_true", default=False)
    ap.add_argument("--answer", action="store_true", default=False,
                    help="Enable grounded LLM drafting + fabrication audit gate for custom "
                         "questions (default off: every custom question is escalated to the user). "
                         "Drafted answers still require the career-draft-auditor + the user before submit.")
    ap.add_argument("--rebuild", action="store_true", default=False,
                    help="Force a fresh tailored package: drop the stored resume+cover so they are "
                         "REGENERATED with the current drafting pipeline instead of reused. Use to "
                         "rebuild applications staged by an older/weaker pipeline. Live runs only.")
    ap.add_argument("--allow-master", action="store_true", default=False,
                    help="DEBUG ONLY. Permit the generic master resume as a last-resort doc when no "
                         "tailored PDF exists. Staging NEVER passes this — the default is to HALT "
                         "(needs_build) rather than attach a generic resume to a selected job.")
    ap.add_argument("--recon", dest="recon", action="store_true", default=True,
                    help="P3: run the web-enabled recon agent (company/role research + networking) "
                         "before drafting so answers are company-specific. On by default with --answer.")
    ap.add_argument("--no-recon", dest="recon", action="store_false",
                    help="Disable the recon research step (draft on corpus + JD only).")
    args = ap.parse_args(argv)

    # Friendly cold-start handling: a missing jobs.json used to crash with a raw
    # FileNotFoundError deep in the loader. Catch it at the call site and tell the user
    # exactly how to fix it. (Caught here rather than via an exists() pre-check so that a
    # mocked/injected find_job in tests is never short-circuited.)
    try:
        job = find_job(config.JOBS_JSON, args.job)
    except FileNotFoundError:
        sample = config.EXAMPLES_DIR / "jobs.sample.json"
        print(
            f"No jobs file found at {config.JOBS_JSON}.\n"
            f"  To get started:\n"
            f"    1. mkdir -p \"{config.ARIA_DATA}\"\n"
            f"    2. cp \"{sample}\" \"{config.JOBS_JSON}\"\n"
            f"       (or set the ARIA_CORE_DATA env var to a folder that contains jobs.json)\n"
            f"    3. edit it to add the job you want to apply to (id: {args.job}).",
        )
        return 2
    if not job:
        print(f"job {args.job} not found in {config.JOBS_JSON}")
        return 2

    # On a REAL live-stage run (the path that fills a form and attaches docs), guarantee a tailored
    # package exists BEFORE we resolve PDFs. If generation can't produce one (thin JD, LLM down,
    # validation-exhausted), HALT to `needs_build` and DO NOT proceed to attach a generic resume.
    # Gate on `not args.dry_run` so dry-run / audit-only / --open paths never trigger generation.
    # The thin-JD halt is correct: a job without a full JD must surface that Stage-0 sourcing needs
    # to enrich it first — we don't paper over it with the master resume.
    # Bug #3 — CHEAP URL-liveness pre-flight BEFORE the expensive tailor step. A --live run used to
    # spend ~5 min + 2 `claude -p` calls tailoring a package and only THEN open the page and HALT
    # "posting is closed". A <1s HTTP GET here catches an obviously-dead posting up front so the
    # tailor quota is never burned. FAIL-OPEN by contract (check_posting_liveness): only an
    # UNAMBIGUOUS closed signal halts; any network error / timeout / ambiguity proceeds to tailor as
    # normal, so a transient hiccup never blocks a real job. Gated on `not args.dry_run` so dry /
    # audit-only / --open paths never hit the network.
    if not args.dry_run:
        try:
            posting_url = job.get("url") or job.get("apply_url") or ""
            is_closed, why = check_posting_liveness(posting_url)
        except Exception:  # noqa: BLE001 — the pre-flight must itself fail open (never crash the run)
            is_closed, why = (False, "")
        if is_closed:
            reason = why or "posting closed — remove/re-source"
            print(f"  HALT (needs_sam): {reason} [liveness pre-flight, before tailoring]")
            record_status(config.APPLICATIONS_JSON, job_id=args.job, status="needs_sam",
                          run_dir="", note=reason)
            return 2

    if not args.dry_run:
        try:
            if getattr(args, "rebuild", False):
                # Force a fresh package, replacing the stored one ONLY on a clean regenerate.
                # generate-before-destroy: a failure here leaves the OLD package intact (halts to
                # needs_build below), never strips a submit-ready app to packageless.
                from . import tailor
                print(f"  --rebuild: regenerating a fresh tailored package for {args.job}...")
                tailor.rebuild_tailored_package(job)
            else:
                ensure_tailored_package(job)
        except Exception as e:  # noqa: BLE001 — includes TailorError / ValueError / LLMUnavailable
            reason = f"tailoring halt: {type(e).__name__}: {e}"
            print(f"  HALT (needs_build): {reason}")
            record_status(config.APPLICATIONS_JSON, job_id=args.job, status="needs_build",
                          run_dir="", note=reason)
            return 2

    try:
        resume_pdf, cover_pdf = ensure_pdfs(job, allow_master=args.allow_master)
    except NoTailoredPDF as e:
        # No tailored resume PDF resolved even after the tailoring step (e.g. a dashboard hand-edit
        # left content but the render is missing). Halt to needs_build rather than attach generic.
        reason = f"no tailored PDF: {e}"
        print(f"  HALT (needs_build): {reason}")
        record_status(config.APPLICATIONS_JSON, job_id=args.job, status="needs_build",
                      run_dir="", note=reason)
        return 2
    answers = build_answers(profile_path=config.PROFILE_JSON, job=job,
                            resume_pdf=resume_pdf, cover_pdf=cover_pdf)

    answer_fn, audit_fn, facts = build_hooks(args.answer, job, recon=getattr(args, "recon", False))
    outcome = apply_to_job(job=job, answers=answers, runs_root=config.RUNS_DIR,
                           profile_dir=config.PROFILE_DIR, headless=args.headless,
                           dry_run=args.dry_run,
                           answer_fn=answer_fn, audit_fn=audit_fn, facts=facts)

    print(f"\n=== {outcome.job_id}: {outcome.status} "
          f"(submitted={outcome.submitted}, verify_ok={outcome.verify_ok}) ===")
    print(f"run dir (screenshots): {outcome.run_dir}")
    if outcome.filled_fields:
        print(f"  filled ({len(outcome.filled_fields)}): {', '.join(outcome.filled_fields)}")
    if outcome.work_auth_answers:
        for w in outcome.work_auth_answers:
            print(f"  work-auth: {w['answer']}  <- {w['q']}")
    for g in outcome.generated:
        tag = g.get("status", "?").upper()
        flag = "  <-- REVIEW BEFORE SUBMIT" if g.get("status") == "drafted" else ""
        print(f"  [{tag}] {g.get('q', '')}{flag}")
    for c in outcome.corrections:
        mark = "applied" if c.get("applied") else "FLAGGED"
        print(f"  correction ({mark}): {c.get('label')}: {c.get('current')} -> {c.get('correct')}")
    if outcome.unfilled_required:
        print(f"  STILL NEEDS SAM ({len(outcome.unfilled_required)}): "
              f"{', '.join(outcome.unfilled_required)}")
    if outcome.halt_reason:
        print(f"  HALT: {outcome.halt_reason}")
    if outcome.error:
        print(f"  ERROR: {outcome.error}")

    note = outcome.halt_reason or outcome.error or "staged to submit brink"
    record_status(config.APPLICATIONS_JSON, job_id=outcome.job_id,
                  status=outcome.status, run_dir=outcome.run_dir, note=note)

    # Auto-run the application-level accuracy review at the end of a SUCCESSFUL --answer stage
    # that drafted custom answers, so the staged card arrives review-ready (Submit unlockable on
    # a clean PASS) instead of waiting on a manual "Re-run accuracy review" click. apply_to_job
    # has already written the staged manifest record by now, so a real record exists to audit.
    #
    # Phase 4d wiring: on a REAL --answer stage this is now the autonomous CONVERGENCE LOOP
    # (converge.converge_quality), whose ROUND 1 *is* the single quality pass chain_accuracy_review
    # used to run (include_quality=True), and which then audit→fixes→re-audits to convergence — so a
    # staged card = "ready to submit" or a human-only blocker, never a half-audited card. The loop is
    # bounded, fabrication-gated (converges by REMOVAL, never inventing), and surfaces blocked/
    # exhausted/error structurally. It is gated on a real stage (LLM fixes use claude -p, never the
    # metered API). On a DRY run (no navigation/staging) the loop is skipped and the cheap single-pass
    # chain_accuracy_review is kept (it no-ops on a dry run anyway via its answered guard), so a dry
    # run never spins fixes against a stale manifest record. Both paths are guarded + non-raising —
    # the stage's exit code below reflects the STAGE outcome, NOT the loop/audit.
    _converge_after_stage(outcome, answered=(args.answer and not args.dry_run))

    # Feature B / Phase 3 — engine-side Telegram notify for an OPEN human_blocker. Fires AFTER
    # record_status / the accuracy review so it runs for BOTH the single-stage route and the batch
    # drain (both go through `python -m apply_engine --live --answer`). Only a REAL stage can halt
    # with a blocker, so gate on `not args.dry_run` (a dry run never navigates/stages a form, so it
    # must never notify against a stale manifest record). Fully guarded + non-raising: a notify
    # failure must never crash the stage, exactly like chain_accuracy_review. The stamp
    # (notified.telegram=True) is written atomically under the manifest filemutex by mark_notified,
    # so the idempotency flag can't race a second sender.
    if not args.dry_run:
        _maybe_notify_blocker(outcome.job_id)
    return 0


def _maybe_notify_blocker(job_id: str) -> None:
    """If the freshly-staged manifest record for `job_id` carries an OPEN, not-yet-notified
    human_blocker, fire exactly ONE Telegram message and (on success) stamp notified.telegram=True
    atomically under the manifest filemutex. Idempotent + non-raising: a second stage of the same
    open blocker sends nothing; any failure (missing creds, network, corrupt manifest) is swallowed
    and the stage proceeds. A clean stage with no open blocker is a complete no-op (additive)."""
    manifest_path = config.ARIA_DATA / "staged_applications.json"
    try:
        rec = _manifest_record(manifest_path, job_id)
        if not isinstance(rec, dict):
            return
        from .notify import notify_blocker, mark_notified
        if notify_blocker(rec):
            mark_notified(manifest_path, job_id)
            print("  notify: Telegram halt alert sent")
    except Exception as e:  # noqa: BLE001 — notify is supplementary; never fail the stage
        print(f"  notify: skipped ({type(e).__name__})")
