"""qualify — STAGE 2 of the pipeline: turn raw discovered stubs into real jobs.

Per stub from the holding list:
  1. resolve_url  — resolve a direct ATS posting URL (fail closed on ambiguity).
  2. jd_fetch     — fetch the FULL job description from the posting API.
  3. enrichment   — deterministic stageability gate (JD >= 1200 real chars + a
                    specific posting URL, not a careers homepage).
  4. qualify      — PASS -> score against your rubric, allocate a JOB-NNN id, and
                    PROMOTE into jobs.json. FAIL -> hold (retry next run) or prune
                    after 3 dead tries. NEVER drops a job for low fit — only truly
                    dead links (no recoverable JD AND no live URL) are pruned.

Every heavy external (resolve_url / fetch_jd / enrich_ok / score) is an injectable
seam defaulting to the real impl, so the qualify pass is hermetically testable
without network, browser, or an LLM call.
"""
