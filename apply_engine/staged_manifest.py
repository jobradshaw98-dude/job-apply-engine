"""Staged-application manifest: turn a JobOutcome into a flat review record and
merge it into a JSON list keyed by job_id.

`build_record` is PURE — it takes an already-computed `staged_at` string (the
orchestrator stamps the time) so this module never reads the clock and stays
deterministic/testable. `write_record` is the only side-effecting function.
"""
from pathlib import Path
from typing import Optional

# The status the orchestrator assigns a clean stage, and the one finish.can_submit treats as
# review-ready. recompute_status only ever flips a blocked record TO this value (never away).
REVIEW_READY = "ready_to_submit"
# Statuses recompute_status is allowed to OVERWRITE. These are the "blocked at staging" states.
# A status outside this set (ready_to_submit / submitted / anything more review-ready) is left
# untouched — the one-way valve. Mirrors finish._NOT_REVIEW_READY plus "submitted" guard below.
_RECOMPUTABLE = {"needs_input", "needs_sam", "error"}

# The quality verdicts that CLEAR the second gate. Mirrors finish.can_submit's allow-list exactly
# so status and gate never disagree: only an explicit PASS/FLAG (after normalize) is review-ready.
_QUALITY_OK = {"PASS", "FLAG"}


def _quality_blocks(record: dict) -> bool:
    """True if the quality judge has NOT cleared this record — i.e. it is NOT review-ready.

    Mirrors finish.can_submit's quality gate (2026-06-08) so the dashboard status and the submit
    gate always agree: a record is blocked if quality_audit is missing/not-a-dict, judge_ran is
    explicitly False, or the (normalized) verdict is not in the {PASS, FLAG} allow-list. FAIL, a
    missing verdict, None, and any unknown value all block."""
    quality = record.get("quality_audit")
    if not isinstance(quality, dict):
        return True
    if quality.get("judge_ran") is False:
        return True
    return str(quality.get("verdict")).strip().upper() not in _QUALITY_OK


def recompute_status(record: dict) -> str:
    """Pure: return the status this record SHOULD carry given its STORED state, after Sam has
    answered questions / provided inputs from the dashboard. Returns REVIEW_READY only when every
    stored blocker the orchestrator considered at staging is resolved; otherwise returns the
    record's CURRENT status unchanged.

    Blockers, mirrored from orchestrator's staging logic honestly:
      * any custom_q with status "needs_input" (a question that could not be drafted and still
        needs Sam). Declined questions are NOT blockers — the engine intentionally stages them
        for Sam and they never gated a clean form.
      * any unresolved required question still listed in needs_sam (the orchestrator's
        `unfilled_required` list; legacy records used the `unfilled_required` key). --provide prunes
        these as Sam answers them, so an empty list means nothing required is outstanding.
      * an audit verdict of "BLOCKED" (a fabrication-class finding, or the fail-closed degraded
        stamp) or an audit with judge_ran explicitly False (a legacy degraded stamp) — never
        review-ready while set.
      * the quality judge not clearing the record (quality_audit missing, judge_ran False, or its
        normalized verdict not in {PASS, FLAG}) — mirrors finish.can_submit's second gate so the
        dashboard never shows green "ready" while the submit gate refuses.

    ONE-WAY VALVE: this is monotonic toward review-ready. It only recomputes a record whose status
    is one of the "blocked at staging" states (needs_input / needs_sam / error); it returns any
    already-review-ready status (ready_to_submit) or a submitted record's status UNCHANGED, so it
    can never DOWNGRADE a status the engine set more review-ready than this stored view computes.

    HONESTY: this can only see STORED state — it does NOT re-open the live form. That is acceptable
    because finish.replay re-fills and finish.can_submit re-verifies the live form (read-back +
    a live can_submit re-check) before any submit click. The live form is the durable gate; this
    function only unlocks the dashboard's review step so Sam can reach that gate. A record is
    never made review-ready here without the live re-verification still standing between it and a
    submit."""
    if not isinstance(record, dict):
        return ""
    current = record.get("status") or ""
    # a submitted record is terminal — never recompute it.
    if record.get("submitted"):
        return current

    # Upgrade a "blocked at staging" record once its stored blockers are resolved. The upgrade path
    # requires FULL clearance (mirrors finish.can_submit, incl. a present+passing quality_audit), so
    # the absence of a stamp keeps a record blocked rather than promoting it.
    if current in _RECOMPUTABLE:
        return REVIEW_READY if _stage_blocker(record) is None else current
    # TWO-WAY VALVE (2026-06-16): a record already marked review-ready must be DOWNGRADED when a
    # POSITIVE blocker is present — e.g. an audit re-stamped BLOCKED outside the staging converge
    # loop (dashboard re-review, post-edit refresh). The old one-way valve left the green
    # "ready_to_submit" standing while finish.can_submit refused the submit, so the dashboard lied
    # (JOB-293 Future sat in exactly this gap). Crucially the downgrade keys off a POSITIVE block
    # signal, NOT the mere ABSENCE of a stamp: a legacy ready record with no quality_audit (staged
    # before that judge existed) must NOT be yanked back — only a concrete recorded blocker downgrades.
    if current == REVIEW_READY and _positive_block(record):
        return "needs_sam"
    return current


def _stage_blocker(record: dict) -> Optional[str]:
    """Return a short reason string for the FIRST unresolved staging blocker on this record, or
    None if the stored state clears every gate finish.can_submit checks. Pure (reads stored state
    only). Used by recompute_status in both directions (unlock when None, downgrade when set)."""
    # blocker 1: a custom_q that still needs Sam (failed draft). declined != blocker.
    for q in (record.get("custom_qs") or []):
        if isinstance(q, dict) and q.get("status") == "needs_input":
            return "custom_q needs_input"

    # blocker 2: unresolved required questions (orchestrator's unfilled_required; pruned by --provide)
    outstanding = record.get("needs_sam")
    if not outstanding:
        outstanding = record.get("unfilled_required")
    if outstanding:
        return "unresolved required field"

    # blocker 3: a live DETERMINISTIC gate block on the current answers (forbidden phrase /
    # fabrication pattern). The LLM accuracy + quality judges were demoted to advisory/on-demand
    # (2026-06-22) and no longer gate status — mirror finish.can_submit so status and the submit
    # gate agree. A stale/absent LLM verdict no longer keeps a record out of review-ready.
    # FAIL-CLOSED on a missing stamp (mirror finish.can_submit, 2026-06-22 reviewer fix): no `audit`
    # = the deterministic gate never ran ⇒ not review-ready (don't promote an unchecked record).
    audit = record.get("audit")
    if not isinstance(audit, dict):
        return "deterministic gate not run"
    if int(audit.get("gate_blocks", 0) or 0) > 0:
        return "deterministic gate block"

    return None


def _positive_block(record: dict) -> Optional[str]:
    """Return a reason for a POSITIVE, recorded blocker on this record, or None. Unlike
    _stage_blocker (which also blocks on the ABSENCE of a clearing stamp, for the upgrade path),
    this fires ONLY on concrete recorded signals — used to DOWNGRADE an already-review-ready record
    without yanking back legacy records that simply predate the quality judge. A missing/degraded
    quality_audit is deliberately NOT a positive block here."""
    for q in (record.get("custom_qs") or []):
        if isinstance(q, dict) and q.get("status") == "needs_input":
            return "custom_q needs_input"
    outstanding = record.get("needs_sam")
    if not outstanding:
        outstanding = record.get("unfilled_required")
    if outstanding:
        return "unresolved required field"
    # A deterministic gate block — OR a MISSING stamp (gate never ran) — downgrades a ready record
    # so the dashboard can't show green while finish.can_submit refuses (2026-06-22 reviewer fix).
    # LLM accuracy + quality judges are advisory/on-demand now and never downgrade.
    audit = record.get("audit")
    if not isinstance(audit, dict):
        return "deterministic gate not run"
    if int(audit.get("gate_blocks", 0) or 0) > 0:
        return "deterministic gate block"
    return None


def apply_recompute(manifest_path, job_id: str) -> Optional[str]:
    """Recompute ONE record's status from its stored state and persist it atomically if it changed.
    Returns the NEW status (whether or not it changed), or None if the record is absent/submitted/
    the manifest is missing or corrupt. Safe to call at any answer-completion point — a no-op when
    nothing flips. Skips submitted records by design (recompute_status returns their status as-is,
    so no write happens)."""
    import json
    import os

    mp = Path(manifest_path)
    if not mp.exists():
        return None
    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, list):
        return None

    rec = None
    for entry in data:
        if isinstance(entry, dict) and entry.get("job_id") == job_id:
            rec = entry
            break
    if rec is None:
        return None

    new_status = recompute_status(rec)
    if new_status and new_status != (rec.get("status") or ""):
        # Merge-safe: re-read FRESH under the mutex and flip only THIS record's status, so a
        # concurrent answer edit (which whole-file-rewrites the same manifest) isn't clobbered.
        from .filemutex import locked
        with locked(mp):
            try:
                fresh = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                return new_status
            if not isinstance(fresh, list):
                return new_status
            for entry in fresh:
                if isinstance(entry, dict) and entry.get("job_id") == job_id:
                    # recompute against the FRESH record (its blockers may have changed)
                    ns = recompute_status(entry)
                    if ns and ns != (entry.get("status") or ""):
                        entry["status"] = ns
                    new_status = ns or new_status
                    break
            tmp = mp.with_suffix(mp.suffix + ".tmp")
            tmp.write_text(json.dumps(fresh, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, mp)
    return new_status


# screenshots whose label suggests the form is most fully filled — later in the
# list = better preview. We pick the highest-priority label that exists, falling
# back to the last screenshot on disk if none of these labels match.
_PREVIEW_PRIORITY = [
    "opened", "filled", "answered", "audited", "multi_step_end",
    "review_brink", "halt", "verify_fail", "easy_apply",
]


def _best_preview_png(run_dir: str) -> str:
    """Basename of the most useful screenshot in run_dir, or '' if none/absent."""
    if not run_dir:
        return ""
    rd = Path(run_dir)
    if not rd.is_dir():
        return ""
    shots = sorted(p.name for p in rd.glob("step_*_*.png"))
    if not shots:
        return ""

    def _rank(name: str) -> tuple:
        # higher priority label first, then later step number as a tiebreak
        for i, label in enumerate(_PREVIEW_PRIORITY):
            if name.endswith(f"_{label}.png"):
                return (i, name)
        return (-1, name)  # unknown label: low priority, ordered by name

    return max(shots, key=_rank)


def build_record(outcome, job: dict, staged_at: str) -> dict:
    """Map a JobOutcome + job dict into a flat review record. Pure: no clock, no IO
    beyond a read-only screenshot listing inside the run_dir."""
    job = job or {}
    needs_sam = list(getattr(outcome, "unfilled_required", []) or [])
    halt = getattr(outcome, "halt_reason", "") or ""
    # surface a needs-Sam reason that isn't already a field (e.g. work-auth halt)
    if halt and getattr(outcome, "status", "") in ("needs_sam", "error") and not needs_sam:
        needs_sam = [halt]

    return {
        "job_id": getattr(outcome, "job_id", "") or job.get("id", ""),
        "company": job.get("company", "") or "",
        "role": job.get("title", "") or job.get("role", "") or "",
        "ats": job.get("ats", "") or "",
        "url": job.get("url", "") or job.get("apply_url", "") or "",
        "status": getattr(outcome, "status", "") or "",
        "submitted": bool(getattr(outcome, "submitted", False)),
        "run_dir": getattr(outcome, "run_dir", "") or "",
        "preview_png": _best_preview_png(getattr(outcome, "run_dir", "") or ""),
        "filled_fields": list(getattr(outcome, "filled_fields", []) or []),
        "work_auth": list(getattr(outcome, "work_auth_answers", []) or []),
        "corrections": list(getattr(outcome, "corrections", []) or []),
        "uploaded_docs": list(getattr(outcome, "uploaded_docs", []) or []),
        "custom_qs": list(getattr(outcome, "generated", []) or []),
        "needs_sam": needs_sam,
        "halt_reason": halt,
        # Structured halt record (Feature B, Phase 1) — the machine-readable "what kind of answer
        # unblocks this" surface. Additive + backward-compat: None when the outcome had no blocker
        # (old records simply lack the key; recompute_status/can_submit ignore it). halt_reason stays
        # as the human sentence; this carries the tier/category/answer_target on top.
        "human_blocker": getattr(outcome, "human_blocker", None),
        # Phase 4b — live-form model captured at the brink. All ADDITIVE + backward-compat: None when
        # capture didn't run (best-effort) or on an old record. The G1/G2 readiness gates
        # (finish._g1_reconcile_ok / _g2_compliance_ok) read `reconcile`/`compliance` and PASS-WHEN-
        # ABSENT, so a record without these keys behaves exactly as before. `form_spec` is the
        # compact live-form summary; `reconcile` carries the clean bool + the mismatched/unfilled
        # lists; `compliance` carries the ok bool + the length violations.
        "form_spec": getattr(outcome, "form_spec", None),
        "reconcile": getattr(outcome, "reconcile", None),
        "compliance": getattr(outcome, "compliance", None),
        "outcome": getattr(outcome, "outcome", "") or "",   # precise no-form category (dashboard filter)
        "error": getattr(outcome, "error", "") or "",
        "staged_at": staged_at,
    }


def write_record(record: dict, manifest_path) -> None:
    """Merge `record` into the JSON list at manifest_path, keyed by job_id (replace
    an existing entry for that job_id, else append). Atomic-ish: write to a temp
    file then replace. A corrupt/missing manifest is treated as an empty list."""
    import json
    import os

    from .filemutex import locked

    mp = Path(manifest_path)
    mp.parent.mkdir(parents=True, exist_ok=True)

    # Under the file mutex so the read-modify-write is atomic against a concurrent answer edit
    # (which whole-file-rewrites the same manifest). Without the lock, this re-read-then-write has
    # a TOCTOU window where a concurrent regen's write between our read and our write is lost.
    with locked(mp):
        data = []
        if mp.exists():
            try:
                loaded = json.loads(mp.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    data = loaded
            except Exception:
                data = []  # corrupt manifest -> start clean rather than crash an apply run

        jid = record.get("job_id")
        replaced = False
        for i, entry in enumerate(data):
            if isinstance(entry, dict) and entry.get("job_id") == jid:
                data[i] = record
                replaced = True
                break
        if not replaced:
            data.append(record)

        tmp = mp.with_suffix(mp.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, mp)


def attach_audit(manifest_path, job_id, audit: dict) -> None:
    """Merge `audit` onto the manifest record whose job_id matches, under an
    "audit" key. Atomic-ish write like write_record. Defensive by design: a
    missing/corrupt manifest or an unknown job_id is a silent no-op — this is
    called after an apply run and must never raise or clobber the manifest.

    Why so forgiving: the auditor verdict is supplementary review metadata. If
    the manifest can't be parsed or the job was never staged, there's nothing to
    attach to, and crashing here would lose the (already-written) staged record.
    """
    import json
    import os

    from .filemutex import locked

    mp = Path(manifest_path)
    if not mp.exists():
        return  # nothing staged yet -> nothing to attach to

    # Under the file mutex: attach_audit whole-file-rewrites staged_applications.json, so a
    # concurrent answer edit landing between our read and our write would otherwise be clobbered
    # (the audit refresh races the very answer edit that triggered it). Re-read FRESH inside.
    with locked(mp):
        try:
            loaded = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            return  # corrupt manifest -> no-op rather than overwrite with partial data
        if not isinstance(loaded, list):
            return

        matched = False
        for entry in loaded:
            if isinstance(entry, dict) and entry.get("job_id") == job_id:
                entry["audit"] = audit
                matched = True
                break
        if not matched:
            return  # unknown job_id -> no-op, don't append a bare audit-only record

        tmp = mp.with_suffix(mp.suffix + ".tmp")
        tmp.write_text(json.dumps(loaded, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, mp)


def attach_quality_audit(manifest_path, job_id, quality_audit: dict) -> None:
    """Merge `quality_audit` onto the manifest record whose job_id matches, under a
    "quality_audit" key. The holistic quality judge's verdict is the SECOND gate (alongside
    the fabrication "audit" key). Same atomic-ish, fully-forgiving contract as attach_audit:
    a missing/corrupt manifest or an unknown job_id is a silent no-op — this runs after a stage
    or a dashboard re-run and must never raise or clobber the manifest. Re-reads FRESH under the
    file mutex so a concurrent answer edit landing between read and write isn't clobbered."""
    import json
    import os

    from .filemutex import locked

    mp = Path(manifest_path)
    if not mp.exists():
        return  # nothing staged yet -> nothing to attach to

    with locked(mp):
        try:
            loaded = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            return  # corrupt manifest -> no-op rather than overwrite with partial data
        if not isinstance(loaded, list):
            return

        matched = False
        for entry in loaded:
            if isinstance(entry, dict) and entry.get("job_id") == job_id:
                entry["quality_audit"] = quality_audit
                matched = True
                break
        if not matched:
            return  # unknown job_id -> no-op

        tmp = mp.with_suffix(mp.suffix + ".tmp")
        tmp.write_text(json.dumps(loaded, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, mp)
