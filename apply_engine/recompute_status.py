# -*- coding: utf-8 -*-
"""One-off CLI: recompute the stored `status` of staged-application records from their CURRENT
stored state and persist any change atomically.

WHY THIS EXISTS
Staged records carry the `status` the orchestrator set at STAGING time ("needs_input" when a
required field/question still needed Sam). Sam then answers everything from the dashboard
(via regen_answer --provide / --instruction) and the audit verdict refreshes to PASS — but until
those completion points were wired to recompute, nothing ever re-derived `status`, so
finish.can_submit kept refusing review-ready records with "status is 'needs_input'". The
completion points now recompute inline; this CLI applies the SAME pure recompute to the records
that were staged BEFORE the wiring existed (a backfill), and is a safe idempotent re-run any time.

    python -m apply_engine.recompute_status            # all records (skips submitted)
    python -m apply_engine.recompute_status JOB-131    # one record

Prints per-record `JOB-ID: <old> -> <new>` (and `(unchanged)` when nothing flips). Submitted
records are skipped by recompute_status itself (returns their status untouched), so they print
unchanged and are never rewritten. Uses apply_recompute for the atomic single-record write.
"""
import argparse
import json
import sys
from pathlib import Path

from . import config
from .staged_manifest import apply_recompute, recompute_status


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        prog="apply_engine.recompute_status",
        description="Recompute staged-application status from stored state (one-way valve "
                    "toward review-ready). Applies to all records, or one if a JOB-ID is given.")
    ap.add_argument("job_id", nargs="?", help="Optional single job id, e.g. JOB-131")
    args = ap.parse_args(argv)

    manifest = Path(config.ARIA_DATA) / "staged_applications.json"
    if not manifest.exists():
        print(f"no manifest at {manifest}")
        return 2
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as ex:  # noqa: BLE001
        print(f"could not read manifest: {ex}")
        return 2
    if not isinstance(data, list):
        print("manifest is not a list")
        return 2

    if args.job_id:
        records = [r for r in data if isinstance(r, dict) and r.get("job_id") == args.job_id]
        if not records:
            print(f"no staged record for {args.job_id}")
            return 2
    else:
        records = [r for r in data if isinstance(r, dict)]

    changed = 0
    for rec in records:
        jid = rec.get("job_id", "?")
        old = rec.get("status") or ""
        # Compute the would-be new status first (pure) so we can print honestly even when the
        # atomic write is a no-op; then persist via apply_recompute (which re-reads the manifest
        # and writes only on a real change — keeping the write atomic and single-record).
        new = recompute_status(rec)
        if new and new != old:
            apply_recompute(manifest, jid)
            changed += 1
            print(f"{jid}: {old} -> {new}")
        else:
            print(f"{jid}: {old} (unchanged)")

    print(f"\n{changed} record(s) updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
