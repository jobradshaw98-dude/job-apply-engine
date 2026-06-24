# -*- coding: utf-8 -*-
"""Data fix: register EVERY tailored doc (resume AND cover) that exists on disk into the
uploaded_docs of every NON-submitted staged record.

WHY (root cause): the apply RUN records only the files it attached live
(orchestrator._record_uploaded_docs). A cover (or resume) built or edited AFTER that run — by the
/career build pipeline or a dashboard content edit — is never added to uploaded_docs. So a real
tailored cover sits on disk in applications/<APP-ID>-<slug>/ while uploaded_docs lists resume only.
finish._resolve_pdfs then returns cover=None and the engine silently submits resume-only, dropping
Sam's tailored cover (a quality-contract breach), and the open/fill path can error on it.

This backfill makes the on-disk truth explicit so the durable resolver and the submit gate both see
the tailored cover. It reuses finish._resolve_doc_pdf (registered path, then the sibling-dir tailored
fallback) as the SINGLE source of resolution — it does not fork a second path scheme. Master-resume
entries are never carried forward (the quality contract forbids the generic master at attach).

Idempotent: a record whose uploaded_docs already lists the same tailored resume+cover is left
unchanged. Atomic write under the engine's cross-process file mutex.
"""
import json
import os
from pathlib import Path
from typing import List, Tuple

from . import config
from .finish import _is_master_resume


def _doc_entry(doc: str, path: str) -> dict:
    return {"doc": doc, "path": str(path), "name": Path(path).name}


def _sibling_resolver(record: dict, doc: str):
    """Default (PURE, no JSON read) tailored resolver: the sibling-dir fallback only. Drives the
    unit tests. main() composes this with the canonical ensure_pdfs resolver for the live records
    whose tailored dir can only be found via applications.json (master-only / empty uploaded_docs)."""
    from .finish import _tailored_sibling_pdf  # local: autoflake strips top-level unused imports
    return _tailored_sibling_pdf(record, doc)


def plan_backfill(record: dict, resolver=_sibling_resolver) -> Tuple[bool, List[dict]]:
    """PURE planning core. Given a staged record, return (changed, new_uploaded_docs).

    ADD-ONLY / preserve-existing semantics — the backfill only REGISTERS tailored docs that exist on
    disk; it never deletes an entry it cannot improve. For a NON-submitted record, for each doc
    ("resume", "cover"):
      * find the tailored PDF on disk via `resolver(record, doc)` (default: the sibling-dir fallback,
        the SAME canonical scheme the engine attaches with; main() augments it with ensure_pdfs);
      * if no entry is currently registered for that doc, ADD the tailored one;
      * if an entry IS registered but it is the GENERIC MASTER resume, REPLACE it with the tailored
        one (the quality contract forbids the master at attach); a tailored entry already present is
        left untouched.
    An existing entry that is NOT a master and has no better tailored replacement is PRESERVED as-is —
    a legacy master entry with no tailored file anywhere stays (the submit gate blocks it loudly; the
    backfill must not silently strip it). `changed` is True only when the list actually differs (a
    re-run is a no-op). A submitted record is never touched.

    Filesystem-read only; never writes."""
    if not isinstance(record, dict) or record.get("submitted"):
        return False, list(record.get("uploaded_docs") or [])

    # index existing entries by doc, preserving any non-doc / unknown entries untouched.
    current = [d for d in (record.get("uploaded_docs") or []) if isinstance(d, dict)]
    by_doc = {}
    for d in current:
        key = (d.get("doc") or "").strip().lower()
        if key in ("resume", "cover") and key not in by_doc:
            by_doc[key] = d

    out = list(current)  # start from current, mutate in place below
    changed = False
    for doc in ("resume", "cover"):
        existing = by_doc.get(doc)
        existing_path = (existing.get("path") or "").strip() if existing else ""
        tailored = resolver(record, doc)
        # also accept an already-registered tailored path (don't require the sibling lookup when the
        # current entry is already a good tailored file that exists).
        cur_is_tailored = bool(existing_path) and Path(existing_path).exists() and not (
            doc == "resume" and _is_master_resume(existing_path))
        if existing is None:
            # no entry yet: add the tailored PDF if one exists on disk.
            if tailored and Path(tailored).exists():
                out.append(_doc_entry(doc, tailored))
                changed = True
        elif doc == "resume" and _is_master_resume(existing_path):
            # master registered: replace ONLY if a tailored sibling exists; otherwise PRESERVE
            # (don't strip to nothing — let the submit gate block the master loudly).
            if tailored and Path(tailored).exists():
                out[out.index(existing)] = _doc_entry(doc, tailored)
                changed = True
        elif not cur_is_tailored and tailored and Path(tailored).exists():
            # a non-master entry that no longer exists on disk, but a tailored sibling does: upgrade.
            out[out.index(existing)] = _doc_entry(doc, tailored)
            changed = True
        # else: existing tailored entry is fine — leave it untouched.

    return changed, out


def _before_after(record: dict, new_docs: List[dict]) -> str:
    def fmt(lst):
        return ", ".join(f"{(d.get('doc') or '?')}" for d in lst) or "(none)"
    return f"{record.get('job_id')}: [{fmt(record.get('uploaded_docs') or [])}] -> [{fmt(new_docs)}]"


def _live_resolver(record: dict, doc: str):
    """Live tailored resolver: the sibling-dir fallback first, then the canonical ensure_pdfs path
    (applications.json -> APP-id -> applications/<APP-ID>-<slug>/). The ensure_pdfs leg is what
    finds the tailored dir for records whose only uploaded_docs entry is the MASTER (JOB-212) or
    whose uploaded_docs is empty (JOB-214) — the sibling lookup has no tailored anchor for those.
    Reuses cli.ensure_pdfs (the single canonical resolver), never a forked path scheme."""
    sib = _sibling_resolver(record, doc)
    if sib:
        return sib
    try:
        from .cli import ensure_pdfs
        job = {"id": record.get("job_id", ""), "company": record.get("company", "")}
        resume_pdf, cover_pdf = ensure_pdfs(job)  # raises NoTailoredPDF when no tailored resume
    except Exception:
        return None
    pick = resume_pdf if doc == "resume" else cover_pdf
    return str(pick) if pick else None


def main() -> int:
    mp = config.ARIA_DATA / "staged_applications.json"
    # Re-read FRESH (a parallel history may have just modified this manifest).
    data = json.loads(mp.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        print("manifest is not a list - aborting, no write")
        return 1

    changed_ids = []
    for rec in data:
        if not isinstance(rec, dict):
            continue
        changed, new_docs = plan_backfill(rec, resolver=_live_resolver)
        if changed:
            print("  CHANGED " + _before_after(rec, new_docs))
            rec["uploaded_docs"] = new_docs
            changed_ids.append(rec.get("job_id"))

    if not changed_ids:
        print("nothing changed - no write")
        return 0

    # Take the same cross-process file mutex the dashboard edit CLIs use: a backfill run that
    # overlapped a live resume/cover/answer edit would otherwise whole-file-clobber it (this script
    # re-reads `data` fresh just above, but the mutex serializes the actual write so a concurrent
    # merge-write doesn't land between our read and our replace). Import beside its use so autoflake
    # can't strip it as unused.
    from .filemutex import locked
    with locked(mp):
        tmp = mp.with_suffix(mp.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, mp)
    print(f"backfilled uploaded_docs on {len(changed_ids)} record(s): {', '.join(changed_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
