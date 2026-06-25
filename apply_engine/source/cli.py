"""source CLI — STAGE 1: scan ATS feeds for new postings.

  career-engine source scan
      Scan every company in your watchlist (ARIA_DATA/ats_watchlist.json) for new,
      keyword-matched postings, dedupe against jobs.json, print a markdown table,
      and write a JSON review queue to ARIA_DATA. Never writes jobs.json — review
      the queue and hand stubs to `qualify run`.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from . import feeds


def _cmd_scan(args) -> int:
    result = feeds.run_scan(write_queue=not args.json)
    cands, errs = result["candidates"], result["errors"]
    if args.json:
        print(json.dumps({"candidates": cands, "errors": errs}, indent=2))
        return 0
    print(f"# ATS scan — {len(cands)} new candidates  ({len(errs)} fetch errors)\n")
    print(feeds.render_table(cands))
    if errs:
        print("\n## Fetch errors / unknown slugs\n")
        for e in errs:
            print(f"  - {e.get('company','?')} ({e.get('ats','?')}/{e.get('slug','?')}): {e.get('reason')}")
    if result.get("queue_path"):
        print(f"\nReview queue written to: {result['queue_path']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="career-engine source",
        description="Source new postings from public ATS feeds (scan-only; never writes jobs.json).")
    sub = ap.add_subparsers(dest="action", required=True)

    p_scan = sub.add_parser("scan", help="scan the watchlist for new keyword-matched postings")
    p_scan.add_argument("--json", action="store_true", help="emit JSON to stdout (skips queue file)")
    p_scan.set_defaults(func=_cmd_scan)
    return ap


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
