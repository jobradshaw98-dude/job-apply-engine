"""CLI for the resume/cover builder: `apply-engine build ...`."""

import argparse
import sys
from datetime import date
from pathlib import Path

from .. import config
from .. import cli as apply_cli
from . import generate


def _job_from_args(args) -> dict:
    """Resolve the target job from --job (jobs.json lookup) or --jd-file (raw JD text)."""
    if args.jd_file:
        jd = Path(args.jd_file).read_text(encoding="utf-8")
        return {"id": args.job or "JD", "company": args.company or "", "title": args.title or "",
                "jd_text": jd}
    # --job path: look it up in jobs.json with the same friendly handling as the apply CLI
    try:
        job = apply_cli.find_job(config.JOBS_JSON, args.job)
    except FileNotFoundError:
        sample = config.EXAMPLES_DIR / "jobs.sample.json"
        print(f"No jobs file found at {config.JOBS_JSON}.\n"
              f"  cp \"{sample}\" \"{config.JOBS_JSON}\"  (or set ARIA_CORE_DATA), then add job "
              f"{args.job}.", file=sys.stderr)
        return None
    if not job:
        print(f"job {args.job} not found in {config.JOBS_JSON}", file=sys.stderr)
        return None
    return job


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="apply-engine build",
        description="Draft + render a tailored resume and/or cover letter from your profile.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--job", help="Job id to look up in jobs.json (e.g. JOB-001)")
    src.add_argument("--jd-file", help="Path to a text file containing the job description")
    ap.add_argument("--company", default="", help="Company name (with --jd-file)")
    ap.add_argument("--title", default="", help="Role title (with --jd-file)")
    ap.add_argument("--type", choices=["resume", "cover", "both"], default="both",
                    help="Which document(s) to generate (default: both)")
    ap.add_argument("--out", default="", help="Output directory (default: ./out/<job-id>)")
    ap.add_argument("--profile", default="", help="Path to your profile.json (default: builder/profile.json)")
    ap.add_argument("--model", default="sonnet", help="Claude model for `claude -p` (default: sonnet)")
    args = ap.parse_args(argv)

    job = _job_from_args(args)
    if job is None:
        return 2

    kinds = ("resume", "cover") if args.type == "both" else (args.type,)
    out_dir = Path(args.out) if args.out else (Path.cwd() / "out" / (job.get("id") or "job"))

    try:
        results = generate.generate(
            job, out_dir, kinds=kinds, profile_path=(args.profile or None),
            model=args.model, date_str=date.today().strftime("%B %d, %Y"))
    except generate.llm.LLMUnavailable as e:
        print(f"Generation unavailable: {e}", file=sys.stderr)
        return 3

    ok = True
    for kind, res in results.items():
        status = "OK" if res.get("checks", {}).get("page_count_is_1") else "CHECK"
        print(f"  [{status}] {kind}: {res.get('pdf_path')} "
              f"({res.get('page_count')} page(s), {res.get('autofit_adjustments', 0)} fit-pass)")
        ok = ok and res.get("all_pass", False)
    print(f"Done. Output in {out_dir}")
    return 0 if ok else 0  # render warnings don't fail the command; the files are still produced
