"""engage CLI.

  career-engine engage run [--dry-run] [--commit]
      Run the autonomous career-ops orchestrator: deterministic CRM hygiene
      (bucket A), plan staged contacts/applications to the brink (bucket B), and
      surface unverifiable items (bucket C). Writes a per-run journal. With
      --commit (and a live, non-dry-run), makes exactly ONE git commit of only the
      files it changed. --dry-run journals the plan and changes nothing.
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from . import runner


def _cmd_run(args) -> int:
    return runner.main(
        ([] if not args.dry_run else ["--dry-run"]) + (["--commit"] if args.commit else [])
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="career-engine engage",
        description="Autonomous career-ops orchestrator (A/B/C autonomy buckets, reversibility spine).")
    sub = ap.add_subparsers(dest="action", required=True)
    p_run = sub.add_parser("run", help="run the orchestrator over the CRM + job pipeline")
    p_run.add_argument("--dry-run", action="store_true",
                       help="journal only — no file writes, no commit")
    p_run.add_argument("--commit", action="store_true",
                       help="on a live run, make one git commit of ONLY the files changed")
    p_run.set_defaults(func=_cmd_run)
    return ap


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
