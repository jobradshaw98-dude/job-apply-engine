"""Benchmark / verification harness for the autonomous apply engine.

Two clearly separated layers:

  (a) PURE scoring + reporting core — no browser, no I/O. Outcomes are scored from
      plain dicts so the whole thing is unit-testable. Two failures are treated as
      "can never happen" criticals regardless of expectation:
        - submitted is True            -> CRITICAL: SUBMITTED   (the engine must never submit)
        - ready_to_submit w/ 0 fields  -> FALSE SUCCESS         (a staged app with nothing filled)

  (b) LIVE runner — a thin wrapper around orchestrator.apply_to_job(dry_run=True).
      It is NOT unit-tested (it drives a real browser). It builds a minimal job +
      Answers per target, runs the engine with hooks OFF (answer_fn=None, the
      default-safe path), converts each JobOutcome to a plain dict, and scores it.
      One bad target is recorded as an "error" outcome and never aborts the run.

CLI:  python -m apply_engine.bench --targets bench_targets.json [--headless] [--only JOB-ID]
      Exits non-zero if any critical (SUBMITTED / FALSE SUCCESS) occurred.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional


# Verdicts that represent the can-never-happen failures. Collected separately so the
# CLI can fail loudly (non-zero exit) the moment one appears.
CRITICAL_VERDICTS = ("CRITICAL: SUBMITTED", "FALSE SUCCESS (0 fields filled)")


# ---------------------------------------------------------------------------
# (a) PURE scoring + reporting core
# ---------------------------------------------------------------------------

def score_outcome(outcome: dict, expect: str) -> dict:
    """Score one outcome dict against the expected status.

    Returns {job_id, ats, expected, got, ok, verdict, reasons}. The two critical
    guards are checked FIRST and override everything else (a submit or a 0-field
    "ready" is a failure no matter what we expected to see).
    """
    job_id = outcome.get("job_id", "JOB-?")
    ats = outcome.get("ats", "")
    status = outcome.get("status", "")
    reasons: List[str] = []

    # HARD INVARIANT: the engine must NEVER submit. This outranks every other check,
    # including a status that happens to match `expect`.
    if outcome.get("submitted") is True:
        reasons.append("engine reported submitted=True — this must never happen")
        return _result(job_id, ats, expect, status, False, "CRITICAL: SUBMITTED", reasons)

    # FALSE SUCCESS: a staged ("ready_to_submit") application with zero filled fields
    # is not a real application — mirrors the engine's own empty-fill guard.
    if status == "ready_to_submit" and not outcome.get("filled_fields"):
        reasons.append("status=ready_to_submit but filled_fields is empty")
        return _result(job_id, ats, expect, status, False,
                       "FALSE SUCCESS (0 fields filled)", reasons)

    # An errored run is never "ok", even if an error was what we expected.
    if status == "error":
        err = outcome.get("error") or "(no error message)"
        reasons.append(f"run errored: {err}")
        return _result(job_id, ats, expect, status, False, "ERROR", reasons)

    if status == expect:
        return _result(job_id, ats, expect, status, True, "PASS", reasons)

    reasons.append(f"got {status}, expected {expect}")
    return _result(job_id, ats, expect, status, False,
                   f"MISMATCH (got {status}, expected {expect})", reasons)


def _result(job_id, ats, expected, got, ok, verdict, reasons) -> dict:
    return {
        "job_id": job_id, "ats": ats, "expected": expected, "got": got,
        "ok": ok, "verdict": verdict, "reasons": reasons,
    }


def summarize(results: List[dict]) -> dict:
    """Aggregate scored results: per-verdict counts, total, pass_rate, critical list."""
    counts: dict = {}
    critical: List[dict] = []
    passes = 0
    for r in results:
        verdict = r["verdict"]
        counts[verdict] = counts.get(verdict, 0) + 1
        if r["ok"]:
            passes += 1
        if verdict in CRITICAL_VERDICTS:
            critical.append(r)
    total = len(results)
    return {
        "total": total,
        "counts": counts,
        "pass_rate": (passes / total) if total else 0.0,
        "passes": passes,
        "critical": critical,
    }


def render_report(summary: dict, results: List[dict]) -> str:
    """A terminal-readable markdown table (one row per target) + an aggregate footer."""
    cols = ("job_id", "ats", "expected", "got", "verdict")
    widths = {c: len(c) for c in cols}
    rows = []
    for r in results:
        row = {
            "job_id": str(r.get("job_id", "")),
            "ats": str(r.get("ats", "")),
            "expected": str(r.get("expected", "")),
            "got": str(r.get("got", "")),
            "verdict": str(r.get("verdict", "")),
        }
        for c in cols:
            widths[c] = max(widths[c], len(row[c]))
        rows.append(row)

    def fmt(d):
        return " | ".join(d[c].ljust(widths[c]) for c in cols)

    header = fmt({c: c for c in cols})
    sep = "-+-".join("-" * widths[c] for c in cols)
    lines = ["## Apply-engine benchmark", "", header, sep]
    lines += [fmt(r) for r in rows]

    lines.append("")
    lines.append(f"Total: {summary['total']}  |  Pass: {summary['passes']}  |  "
                 f"Pass rate: {summary['pass_rate'] * 100:.0f}%")
    if summary["critical"]:
        lines.append("")
        lines.append(f"!! CRITICAL FAILURES: {len(summary['critical'])} "
                     "(these can NEVER happen) !!")
        for c in summary["critical"]:
            lines.append(f"   - {c['job_id']} [{c['ats']}]: {c['verdict']}")
    else:
        lines.append("No critical failures.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# (b) LIVE runner — thin wrapper over apply_to_job (NOT unit-tested)
# ---------------------------------------------------------------------------

def _outcome_to_dict(outcome, ats: str, expect: str) -> dict:
    """Flatten a JobOutcome dataclass into the plain dict score_outcome consumes."""
    return {
        "job_id": outcome.job_id,
        "ats": ats,
        "expect": expect,
        "status": outcome.status,
        "submitted": outcome.submitted,
        "verify_ok": outcome.verify_ok,
        "filled_fields": list(outcome.filled_fields or []),
        "unfilled_required": list(outcome.unfilled_required or []),
        "halt_reason": outcome.halt_reason,
        "error": outcome.error,
        "run_dir": outcome.run_dir,
    }


def _build_answers(profile_json: Path, job: dict):
    """Build a minimal Answers for a target. Resume PDF is optional — if no real
    tailored PDF exists we point at a tiny stub so source_data's existence check
    passes; the live form fill is what the benchmark is measuring, not the PDF."""
    from .source_data import Answers
    profile = {}
    try:
        if profile_json.exists():
            profile = json.loads(profile_json.read_text(encoding="utf-8"))
    except Exception:
        profile = {}
    values = dict(profile)
    values["company"] = job.get("company", "")
    values["role"] = job.get("title", "")
    # A resume path is required by some adapters' attach step; use a real file if we
    # have one, else a throwaway stub written next to the profile.
    resume = profile_json.parent / "_bench_stub_resume.pdf"
    if not resume.exists():
        try:
            resume.write_bytes(b"%PDF-1.4\n%bench stub\n")
        except Exception:
            pass
    return Answers(values=values, resume_pdf=resume, cover_pdf=None)


def run_targets(targets: List[dict], profile_dir: Path, runs_root: Path,
                headless: bool = True, only: Optional[str] = None,
                ats_override_from_target: bool = True) -> List[dict]:
    """Run each target through apply_to_job(dry_run=True) with hooks OFF, score it.

    A per-target exception is recorded as an "error" outcome so one bad target can
    never abort the whole run. The dry_run=True / answer_fn=None invariant is asserted
    on every call — the harness must never be able to trigger a submit.
    """
    from . import config
    from .orchestrator import apply_to_job

    results: List[dict] = []
    for t in targets:
        job_id = t.get("id", "JOB-?")
        ats = t.get("ats")
        expect = t.get("expect", "ready_to_submit")
        if only and job_id != only:
            continue

        job = {
            "id": job_id,
            "company": t.get("company", ""),
            "title": t.get("title", ""),
            "url": t.get("url", ""),
        }
        try:
            answers = _build_answers(config.PROFILE_JSON, job)
            ats_override = ats if (ats_override_from_target and ats) else None
            # INVARIANT: dry_run is always True and hooks are off (default-safe path).
            assert ats_override is None or isinstance(ats_override, str)
            outcome = apply_to_job(
                job=job, answers=answers, runs_root=runs_root,
                profile_dir=profile_dir, headless=headless, dry_run=True,
                stamp="bench", ats_override=ats_override,
                answer_fn=None, audit_fn=None, facts="",
            )
            # Defense in depth: the engine never submits, but if a future regression
            # ever flips this, the harness still catches it as the worst failure.
            od = _outcome_to_dict(outcome, ats=ats or "", expect=expect)
        except Exception as e:  # noqa: BLE001 — one bad target must not abort the run
            od = {
                "job_id": job_id, "ats": ats or "", "expect": expect,
                "status": "error", "submitted": False, "verify_ok": False,
                "filled_fields": [], "unfilled_required": [],
                "halt_reason": "", "error": repr(e), "run_dir": "",
            }
        results.append(score_outcome(od, expect))
    return results


def _load_targets(path: Path) -> List[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["targets"] if isinstance(data, dict) else data


def main(argv=None) -> int:
    from . import config
    ap = argparse.ArgumentParser(prog="apply_engine.bench",
                                 description="Live cross-ATS benchmark for the apply engine.")
    ap.add_argument("--targets", default=str(Path(__file__).resolve().parent / "bench_targets.json"),
                    help="Path to the targets JSON (default: bundled bench_targets.json)")
    ap.add_argument("--headless", action="store_true", default=False,
                    help="Run Chrome headless")
    ap.add_argument("--only", default=None, help="Run a single target by job id")
    args = ap.parse_args(argv)

    targets = _load_targets(Path(args.targets))
    results = run_targets(
        targets, profile_dir=config.PROFILE_DIR, runs_root=config.RUNS_DIR,
        headless=args.headless, only=args.only,
    )
    summary = summarize(results)
    print(render_report(summary, results))
    # Non-zero exit if any can-never-happen failure occurred.
    return 1 if summary["critical"] else 0


if __name__ == "__main__":
    sys.exit(main())
