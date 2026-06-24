# -*- coding: utf-8 -*-
"""Tiny CLI wrapper around finish.finish_job, for the ARIA dashboard backend to shell out to.

The dashboard's /apply-queue/<job_id>/submit and /open endpoints launch this DETACHED (the
browser work is long), so this stays minimal and import-safe:

    python -m apply_engine.finish_cli JOB-131 --submit   # re-fill + click submit (the ONE submit path)
    python -m apply_engine.finish_cli JOB-131 --open      # re-fill, leave the browser on review

Config paths mirror cli.py: runs under config.RUNS_DIR, the bot profile under config.PROFILE_DIR,
and the staged manifest at config.ARIA_DATA / "staged_applications.json". The submit gate is NOT
re-implemented here — finish_job pre-checks can_submit before opening a browser (fail fast) and
replay() re-checks it live just before the click. This wrapper only wires paths + argv."""
import argparse
import json
import sys

from . import config
from .finish import finish_job

STAGED_MANIFEST = config.ARIA_DATA / "staged_applications.json"


def _persist_finish(manifest_path, job_id, mode, result):
    """Stamp the staged record with the LAST finish outcome so the dashboard can show it on
    refresh — the whole point of the fix: a submit that doesn't positively confirm used to
    revert to 'ready' with no explanation. Writes a compact `last_finish` block; never raises
    and never touches `submitted` (finish_job owns that on a confirmed submit)."""
    import os
    from datetime import datetime
    from pathlib import Path
    mp = Path(manifest_path)
    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return
    except Exception:
        return
    for entry in data:
        if isinstance(entry, dict) and entry.get("job_id") == job_id:
            entry["last_finish"] = {
                "mode": mode,
                "ok": bool(result.get("ok")),
                "submitted": bool(result.get("submitted")),
                "reason": result.get("reason", "") or "",
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                # observability from replay's submit branch: the result screenshot filename
                # (served from run_dir via /apply-queue/shot) + the scraped form-error strings.
                # Carried so the dashboard "Last submit attempt" panel is self-diagnosing.
                "submit_shot": result.get("submit_shot") or "",
                "form_errors": list(result.get("form_errors") or []),
            }
            try:
                tmp = mp.with_suffix(mp.suffix + ".tmp")
                tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, mp)
            except Exception:
                pass
            return


def _utf8_stdout() -> None:
    """Force UTF-8 console output — live ATS labels carry non-cp1252 chars that otherwise
    crash the default Windows console mid-print. Mirrors cli._utf8_stdout."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv=None) -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(prog="apply_engine.finish_cli")
    ap.add_argument("job_id", help="Staged job id, e.g. JOB-131")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--submit", action="store_true",
                      help="Re-fill and click the ATS submit control (gated by can_submit).")
    mode.add_argument("--open", action="store_true",
                      help="Re-fill and leave the browser open on the review screen (no submit).")
    ap.add_argument("--headless", action="store_true", default=False,
                    help="Run headless (default: visible browser, since this is a human-review path).")
    args = ap.parse_args(argv)

    result = finish_job(
        args.job_id,
        submit=bool(args.submit),
        headless=bool(args.headless),
        runs_root=config.RUNS_DIR,
        profile_dir=config.PROFILE_DIR,
        manifest_path=STAGED_MANIFEST,
    )
    # Persist the outcome to the staged record so the dashboard surfaces it on refresh
    # (a non-confirmed submit otherwise reverts to "ready" with no explanation).
    _persist_finish(STAGED_MANIFEST, args.job_id, "submit" if args.submit else "open", result)
    # Machine-readable line for the dashboard log; finish_job never throws.
    print("FINISH_RESULT " + json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
