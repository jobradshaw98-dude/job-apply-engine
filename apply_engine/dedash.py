# -*- coding: utf-8 -*-
r"""One-off cleanup: reduce em-dashes in already-staged AI-drafted answers.

WHY THIS EXISTS
Earlier drafts were written before the em-dash hard rule landed, so staged answers are full of
em-dashes — a clear AI tell. This pass walks the staged manifest and, for every AI-drafted answer
carrying more than one em-dash, runs the SAME minimal-edit path the dashboard uses
(apply_engine.regen_answer with --instruction) to reduce them to at most one, changing nothing
else about the content.

SCOPE (deliberately tight)
  * Skip submitted records entirely (read-only).
  * Only custom_qs with status == "drafted". Never "answered" short-facts, never declined/blocked
    empties, never needs_input.
  * NEVER touch an answer Sam wrote himself (answered_by == "sam") — his words are final.
  * Only answers whose current value has MORE THAN ONE em-dash. One or zero is already fine.

HOW IT EDITS
Sequentially (the manifest is a single shared file, so concurrent regens would race the
read-modify-write). Each answer is handed to regen_answer.main([...]) with a fixed instruction; the
CURRENT-ANSWER minimal-edit contract in regen_answer keeps every other sentence verbatim. The
deterministic em-dash backstop in apply_engine.llm.make_audit_fn (> 2 em-dashes blocks) is the
safety net if a rewrite somehow keeps too many.

This module does NOT run automatically. Invoke it explicitly:

    cd ~/projects/career
    apply_engine\.venv\Scripts\python.exe -m apply_engine.dedash            # all staged apps
    apply_engine\.venv\Scripts\python.exe -m apply_engine.dedash JOB-131    # one app

It prints a per-answer before/after em-dash count and a final summary.
"""
import argparse
import json
import sys
from pathlib import Path

from . import config
from . import regen_answer

# The minimal-edit instruction handed to regen_answer for every over-dashed answer. Phrased to
# touch ONLY punctuation, never content — paired with regen_answer's CURRENT-ANSWER contract.
DEDASH_INSTRUCTION = (
    "Reduce em-dashes: keep at most one, replacing the rest with periods, commas, or "
    "restructured sentences. Change nothing else about content.")


def _emdashes(s):
    """Count em-dash characters in a string (None-safe)."""
    return (s or "").count("—")


def _load_manifest(manifest_path):
    """Read the staged manifest as a list of dict records; [] on missing/corrupt/non-list."""
    try:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except Exception:
        return []
    return [a for a in data] if isinstance(data, list) else []


def _candidates(app):
    """Yield (question, dash_count) for every custom_q on `app` that should be de-dashed:
    status == 'drafted', NOT answered_by == 'sam', and value carries > 1 em-dash. We read the
    question text fresh from the manifest each pass, never caching the dict, because regen_answer
    rewrites the file underneath us between answers."""
    out = []
    for q in (app.get("custom_qs") or []):
        if not isinstance(q, dict):
            continue
        if (q.get("status", "") or "").lower() != "drafted":
            continue
        if (q.get("answered_by", "") or "").strip().lower() == "sam":
            continue
        n = _emdashes(q.get("value", ""))
        if n > 1:
            out.append((q.get("q", "") or "", n))
    return out


def _current_value(manifest_path, job_id, question):
    """Re-read the manifest and return the current value for (job_id, question), or '' if gone.
    Used to report the AFTER count once regen_answer has rewritten the file."""
    qk = regen_answer._qkey(question)
    for app in _load_manifest(manifest_path):
        if isinstance(app, dict) and app.get("job_id") == job_id:
            for q in (app.get("custom_qs") or []):
                if isinstance(q, dict) and regen_answer._qkey(q.get("q", "")) == qk:
                    return q.get("value", "") or ""
    return ""


def dedash(job_id=None, manifest_path=None):
    """Run the de-dash pass. If job_id is given, only that app; else every staged app. Returns a
    summary dict {scanned, edited, skipped_submitted, failures}. Prints per-answer before/after.

    Sequential by construction: each regen_answer.main call reads and rewrites the same manifest,
    so they MUST NOT overlap. We re-scan the manifest fresh before each app to pick up edits."""
    manifest_path = Path(manifest_path) if manifest_path else (
        config.ARIA_DATA / "staged_applications.json")
    apps = _load_manifest(manifest_path)
    if job_id:
        apps = [a for a in apps if isinstance(a, dict) and a.get("job_id") == job_id]

    scanned = edited = skipped_submitted = failures = 0
    for app in apps:
        if not isinstance(app, dict):
            continue
        jid = app.get("job_id")
        if app.get("submitted"):
            skipped_submitted += 1
            print(f"[{jid}] submitted — skipped")
            continue
        targets = _candidates(app)
        if not targets:
            print(f"[{jid}] no over-dashed drafted answers")
            continue
        for question, before in targets:
            scanned += 1
            label = (question or "")[:60]
            rc = regen_answer.main([jid, "--question", question,
                                    "--instruction", DEDASH_INSTRUCTION])
            after = _emdashes(_current_value(manifest_path, jid, question))
            if rc == 0:
                edited += 1
                print(f"[{jid}] {label!r}: em-dashes {before} -> {after}")
            else:
                failures += 1
                print(f"[{jid}] {label!r}: edit FAILED (rc={rc}); em-dashes still {after}")

    print(f"\n=== dedash: scanned={scanned} edited={edited} "
          f"skipped_submitted={skipped_submitted} failures={failures} ===")
    return {"scanned": scanned, "edited": edited,
            "skipped_submitted": skipped_submitted, "failures": failures}


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        prog="apply_engine.dedash",
        description="Reduce em-dashes in already-staged AI-drafted answers (minimal edit).")
    ap.add_argument("job_id", nargs="?", help="Limit to one app, e.g. JOB-131. Omit for all.")
    args = ap.parse_args(argv)
    dedash(args.job_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
