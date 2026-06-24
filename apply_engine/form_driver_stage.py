# -*- coding: utf-8 -*-
"""Canonical writer for a FORM-DRIVER result into the apply-queue staged manifest.

WHY THIS EXISTS (JOB-163 Edwards, 2026-06-20): the form-driver agent drives a live form to the
submit brink and REPORTS prose back to its parent; the parent then hand-wrote the staged record and
serialized those prose lines as plain STRINGS into the manifest's dict-list fields
(`custom_qs` / `work_auth` / `uploaded_docs`). The dashboard renderers iterate those fields as
list-of-dict (every call site does `item.get(...)`), so a string raised
`AttributeError: 'str' object has no attribute 'get'` and 500'd the whole Apply Queue.

The fix is to NEVER hand-roll that record. Build it here, where the shapes are validated and a
wrong type fails LOUD at write time (a clear ValueError the caller sees immediately) instead of
silently at render time (a 500 nobody notices until the page is opened). The schemas mirror exactly
what the deterministic engine writes (verified against a clean engine-staged record):

  custom_qs[i]     = {"q": str, "kind": "essay"|"text"|..., "status": "drafted"|"answered",
                      "reason": str, "value": str, ["answered_by": "sam"]}
  work_auth[i]     = {"q": str, "field": str, "answer": str}
  uploaded_docs[i] = {"doc": "resume"|"cover"|..., "path": str, "name": str}
  filled_fields    = list[str]   # plain strings ARE valid here — its renderer expects strings

Persisting goes through staged_manifest.write_record (the file-mutex'd, merge-safe writer) so a
concurrent answer-edit can't be clobbered.
"""
from __future__ import annotations

from typing import List, Optional

from . import config
from .staged_manifest import write_record

# Required keys per dict-list field — the renderers .get() these, so a missing key renders blank
# (fine) but a non-dict item crashes (not fine). We enforce dict-ness and required keys.
_CUSTOM_Q_KEYS = ("q", "kind", "status", "reason", "value")
_WORK_AUTH_KEYS = ("q", "field", "answer")
_UPLOADED_DOC_KEYS = ("doc", "path", "name")


def _coerce_dict_list(field_name: str, items, required_keys) -> List[dict]:
    """Validate that every item is a dict and fill any missing required key with "". Raises
    ValueError (LOUD, at write time) on a non-dict item — the exact failure that used to slip
    through as a string and 500 the dashboard later."""
    out: List[dict] = []
    for i, it in enumerate(items or []):
        if not isinstance(it, dict):
            raise ValueError(
                f"{field_name}[{i}] must be a dict (got {type(it).__name__}: {it!r}). "
                f"Form-driver results must use the {field_name} schema {required_keys}, "
                f"never a freeform string — see form_driver_stage.py."
            )
        rec = {k: it.get(k, "") for k in required_keys}
        # Preserve any extra keys the caller added (e.g. answered_by, values) without dropping them.
        for k, v in it.items():
            if k not in rec:
                rec[k] = v
        out.append(rec)
    return out


def build_record(
    *,
    job_id: str,
    company: str,
    role: str,
    url: str,
    ats: str,
    staged_at: str,
    status: str = "ready_to_submit",
    reached: str = "review-brink",
    filled_fields: Optional[List[str]] = None,
    custom_qs: Optional[List[dict]] = None,
    work_auth: Optional[List[dict]] = None,
    uploaded_docs: Optional[List[dict]] = None,
) -> dict:
    """Build a schema-valid staged record from a form-driver result. `staged_at` is passed in
    (callers stamp the real timestamp) so this stays pure/testable. Raises ValueError on any
    malformed dict-list item."""
    return {
        "job_id": job_id,
        "company": company or "",
        "role": role or "",
        "url": url or "",
        "ats": ats or "",
        "staged_at": staged_at,
        "status": status,
        "reached": reached,
        "submitted": False,
        # filled_fields renders as plain strings — keep as-is (just stringify defensively).
        "filled_fields": [str(f) for f in (filled_fields or [])],
        "custom_qs": _coerce_dict_list("custom_qs", custom_qs, _CUSTOM_Q_KEYS),
        "work_auth": _coerce_dict_list("work_auth", work_auth, _WORK_AUTH_KEYS),
        "uploaded_docs": _coerce_dict_list("uploaded_docs", uploaded_docs, _UPLOADED_DOC_KEYS),
    }


def stage_form_driver_result(*, manifest_path=None, **kwargs) -> dict:
    """Build the record (validating shapes) and merge it into the manifest via the mutex'd writer.
    Returns the written record. `manifest_path` defaults to the shared staged manifest."""
    record = build_record(**kwargs)
    write_record(record, manifest_path or config.STAGED_MANIFEST)
    return record


def run_accuracy_review(job_id: str, status: str = "ready_to_submit"):
    """Run the SAME application-level accuracy review the engine's --answer stage runs, so a
    form-driver-staged package gets the IDENTICAL honesty check before it can be submitted.

    WHY (2026-06-20): the deterministic engine path auto-runs cli.chain_accuracy_review after a
    successful stage (→ refresh_audit.refresh, include_quality=True), which stamps record["audit"]
    + quality_audit; finish.can_submit keeps Submit locked until that verdict is PASS. Form-driver
    brink-drives skipped this entirely (JOB-163 Edwards / JOB-160 Stryker staged with no audit), so
    a hand-driven Workday app reached "ready" without the review every engine-driven app gets. This
    closes that gap by calling the EXACT same reusable hook — no re-implementation, same guards
    (skips a zero-custom-question stage, never double-audits, fails closed if the Claude CLI is down).

    NON-RAISING by contract (mirrors chain_accuracy_review): a review failure leaves the record
    un-audited and Submit simply stays locked — it never crashes the staging step. Returns the
    short result tag ("PASS" / "BLOCKED..." / "skipped" / "error:...") or None if not applicable.
    """
    try:
        from .cli import chain_accuracy_review  # lazy: pulls the engine; only needed at review time
    except Exception as e:  # noqa: BLE001
        return f"error:import:{type(e).__name__}"
    # chain_accuracy_review reads only outcome.status (must be a successful stage) and outcome.job_id.
    outcome = type("_OutcomeShim", (), {"status": status, "job_id": job_id})()
    try:
        return chain_accuracy_review(outcome, answered=True)
    except Exception as e:  # noqa: BLE001 — review must never fail the stage
        return f"error:{type(e).__name__}"


def main(argv=None) -> int:
    """CLI for the form-driver agent (Bash-only): stage a result from a JSON file.

        python -m apply_engine.form_driver_stage --json result.json [--staged-at <iso>]

    The JSON is the kwargs for build_record (job_id, company, role, url, ats, custom_qs, ...).
    `staged_at` may be in the JSON or passed via --staged-at; if neither, the current time is used.
    On a shape error this prints the ValueError and exits non-zero — the agent must fix the shape,
    never fall back to hand-writing the manifest.
    """
    import argparse
    import datetime
    import json
    import sys

    ap = argparse.ArgumentParser(description="Stage a form-driver result into the apply queue.")
    ap.add_argument("--json", required=True, help="path to a JSON file of build_record kwargs")
    ap.add_argument("--staged-at", default=None, help="ISO timestamp (else now)")
    ap.add_argument("--no-review", action="store_true",
                    help="skip the auto accuracy review (NOT recommended — Submit stays locked "
                         "without a PASS verdict anyway)")
    args = ap.parse_args(argv)

    payload = json.loads(open(args.json, encoding="utf-8").read())
    if not payload.get("staged_at"):
        payload["staged_at"] = args.staged_at or datetime.datetime.now().isoformat(timespec="seconds")
    try:
        rec = stage_form_driver_result(**payload)
    except ValueError as e:
        print(f"STAGE FAILED (shape error, NOT written): {e}", file=sys.stderr)
        return 2
    print(f"staged {rec['job_id']} -> {rec['status']} "
          f"(custom_qs={len(rec['custom_qs'])}, work_auth={len(rec['work_auth'])}, "
          f"uploaded_docs={len(rec['uploaded_docs'])})")

    # Auto-run the SAME accuracy review the engine stage path runs, so a form-driver package gets
    # the identical honesty check before it can be submitted (closes the JOB-163/JOB-160 gap).
    if not args.no_review:
        tag = run_accuracy_review(rec["job_id"], rec["status"])
        print(f"accuracy review: {tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
