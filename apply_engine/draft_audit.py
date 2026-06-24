"""Extract the LLM-GENERATED custom answers from a run so the career-draft-auditor (the
judgment gate) can trace them to the claims ledger before they reach the user.

The deterministic audit_gate already ran in-engine on each answer; this surfaces the same
answers for the stricter human-judgment fabrication/overstatement audit. Only answers that
were actually FILLED by the model (status drafted/answered, non-empty) are returned —
declined/blocked/fill_error filled nothing, so there's nothing to audit."""
import json
from pathlib import Path
from typing import List, Optional

# statuses where the model produced text that landed on the form
_FILLED = ("drafted", "answered")


def _answer_text(rec: dict) -> str:
    """The text that was filled, from a generated record. Free-text/select use `value`;
    checkbox-groups use `values` (list) joined for the auditor to read."""
    v = rec.get("value")
    if isinstance(v, str) and v.strip():
        return v.strip()
    vals = rec.get("values")
    if isinstance(vals, list) and vals:
        return ", ".join(str(x) for x in vals)
    return ""


def drafts_for_audit(generated: Optional[List[dict]]) -> List[dict]:
    """PURE. Map a run's `generated`/`custom_qs` records to the LLM-filled answers needing
    the judgment gate: [{question, answer, kind}]. Excludes declined/blocked/fill_error and
    empty values (nothing was filled)."""
    out: List[dict] = []
    for rec in generated or []:
        if rec.get("status") not in _FILLED:
            continue
        answer = _answer_text(rec)
        if not answer:
            continue
        out.append({
            "question": rec.get("q", "") or "",
            "answer": answer,
            "kind": rec.get("kind", "") or "",
        })
    return out


def load_job_drafts(manifest_path, job_id: str) -> List[dict]:
    """Read the staged manifest and return drafts_for_audit for one job_id. Best-effort:
    a missing/corrupt manifest or unknown job_id returns [] (never raises — this gates a
    review step, it must not crash one)."""
    try:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    for entry in data:
        if isinstance(entry, dict) and entry.get("job_id") == job_id:
            return drafts_for_audit(entry.get("custom_qs"))
    return []


def _main(argv=None) -> int:
    """`python -m apply_engine.draft_audit JOB-ID` → prints the LLM-drafted answers for that
    job (from the staged manifest) as JSON, for the /apply flow to hand to career-draft-auditor.
    Prints `[]` (and exits 0) when there are no drafted answers to audit."""
    import sys
    from . import config
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not argv:
        argv = sys.argv[1:]
    if not argv:
        print("usage: python -m apply_engine.draft_audit JOB-ID")
        return 2
    drafts = load_job_drafts(config.ARIA_DATA / "staged_applications.json", argv[0])
    print(json.dumps(drafts, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
