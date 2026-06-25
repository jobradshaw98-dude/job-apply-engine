"""qualify CLI — STAGE 2: two actions.

  career-engine qualify run
      Drain the holding list (ARIA_DATA/holding.json): for each discovered stub,
      resolve a direct posting URL, fetch the full JD, gate on enrichment, score
      against your fit rubric, and PROMOTE passers into jobs.json with a fresh
      JOB-NNN id. Failures are held (retry next run) or pruned (dead links only —
      never dropped for low fit). --dry-run computes outcomes but writes nothing.

  career-engine qualify resolve --company X --title Y
      Resolve a single company+title to its direct apply URL on a supported ATS
      (Greenhouse / Lever / Ashby). Prints the URL on a confident match, or a clear
      "no confident match" and exits non-zero (fail closed — never guesses a URL).
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from . import qualify as Q
from . import resolve_url as R


def _cmd_run(args) -> int:
    Q.run_qualify(cap=args.cap, dry_run=args.dry_run)
    return 0


def _cmd_resolve(args) -> int:
    match = R.resolve({"company": args.company, "title": args.title})
    if not match:
        print(f"no confident match for {args.company!r} / {args.title!r} "
              f"(fail closed — no URL guessed)")
        return 1
    if args.json:
        print(json.dumps(match, indent=2))
    else:
        print(match["url"])
        print(f"  ats={match['ats']} score={match['score']} title={match['title']!r}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="career-engine qualify",
        description="Qualify discovered stubs: resolve URL -> fetch JD -> gate -> "
                    "score -> promote into jobs.json.")
    sub = ap.add_subparsers(dest="action", required=True)

    p_run = sub.add_parser("run", help="drain the holding list: enrich + score + promote passers")
    p_run.add_argument("--cap", type=int, default=25, help="max stubs to process this run")
    p_run.add_argument("--dry-run", action="store_true", help="compute outcomes but write nothing")
    p_run.set_defaults(func=_cmd_run)

    p_res = sub.add_parser("resolve", help="resolve one company+title to a direct apply URL")
    p_res.add_argument("--company", required=True, help="company name (e.g. \"Acme AI\")")
    p_res.add_argument("--title", required=True, help="job title to match (e.g. \"Forward Deployed Engineer\")")
    p_res.add_argument("--json", action="store_true", help="emit the full match dict as JSON")
    p_res.set_defaults(func=_cmd_resolve)
    return ap


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
