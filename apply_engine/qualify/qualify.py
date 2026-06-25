"""qualify.py — the QUALIFY pass (the missing middle of the pipeline).

Takes raw discovered stubs from the holding list and turns each into a real,
actionable job — or prunes it. Per stub:
  1. resolve a direct ATS posting URL (fail closed on ambiguity),
  2. fetch the FULL job description,
  3. gate on enrichment (JD >= 1200 real chars + specific posting URL),
  4. PASS -> score against your fit rubric, allocate a canonical JOB-NNN, PROMOTE
     into jobs.json, remove from holding,
  5. FAIL -> bump attempts; dead-after-3-tries (no recoverable JD AND no live URL)
     -> prune; else leave in holding for next run.

NEVER drops a job for LOW FIT — only truly dead ones are pruned. Every heavy
external (resolve_url / fetch_jd / enrich_ok / score) is an injectable seam
defaulting to the real impl, so this whole pass is unit-testable with no network,
browser, or LLM call.

Persistence is a plain atomic JSON write (write-temp + os.replace) — no git spine,
no external journal — so promotions land durably without coupling to private infra.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .. import config
from . import enrichment as _enrichment
from . import jd_fetch as _jd_fetch
from . import resolve_url as _resolve_url

MAX_ATTEMPTS = 3
MIN_JD_CHARS = _enrichment.MIN_JD_CHARS
_JOB_ID_RE = re.compile(r"^JOB-(\d+)$")


# ---- default (real) externals — injected so tests stay offline -------------- #

def _default_resolve_url(job: dict):
    """Resolve a direct ATS posting URL for a stub, or None (fail closed)."""
    try:
        match = _resolve_url.resolve(job)
        return match.get("url") if isinstance(match, dict) else None
    except Exception:
        return None


def _default_fetch_jd(url: str):
    """Fetch the full JD via the ATS posting APIs (Greenhouse/Lever/Ashby). None on
    failure or a too-thin body (Workday/generic have no structured fetcher -> stay
    thin and held)."""
    if not url:
        return None
    try:
        jd = _jd_fetch.fetch_jd(url)
        if jd and len(jd) >= MIN_JD_CHARS:
            return jd
    except Exception:
        pass
    return None


def _enrichment_ok(job: dict) -> bool:
    """The deterministic stageability gate (full JD + specific posting URL)."""
    try:
        ok, _ = _enrichment.enrichment_status(job)
        return bool(ok)
    except Exception:
        return False


# ---- default fit scorer: rubric + `claude -p`, plan-quota only -------------- #
# This is the ONLY external that reaches an LLM. It is injectable (the `score=`
# param), so tests pass a deterministic stub and NEVER shell out. The default runs
# on the local Claude Code CLI (`claude -p`) against a fit rubric you supply — same
# no-API-fallback contract as the rest of the engine: if `claude` is absent it
# raises rather than silently billing a metered API.

_FIT_SYSTEM = (
    "You score how well ONE job posting fits a candidate, using ONLY the rubric you "
    "are given. Read the rubric, then the posting (title, company, full job "
    "description). Return STRICT JSON ONLY — no prose, no code fences: "
    '{"fit_score": <integer 1-10>, "reason": "<one sentence, <=200 chars, citing the '
    'rubric band>"}. Score conservatively and never invent facts about the candidate '
    "or the role beyond what the rubric and posting state.")


def _load_rubric() -> str:
    """Read the fit rubric text (config.FIT_RUBRIC). Raises if missing — scoring
    without a rubric would be meaningless, so fail loud rather than guess."""
    path = Path(config.FIT_RUBRIC)
    return path.read_text(encoding="utf-8")


def _default_score(job: dict) -> dict:
    """Score a job against the fit rubric via `claude -p`. Returns
    {"fit_score": int|None, "reason": str}. Plan-quota only; NO metered-API fallback.

    Injectable — tests pass a stub `score=` and this never runs."""
    import shutil
    import subprocess

    cli = shutil.which("claude")
    if not cli:
        raise RuntimeError(
            "Claude Code CLI ('claude') not found on PATH. Fit scoring runs on the "
            "plan via `claude -p`; refusing to fall back to a metered API. "
            "Inject a `score=` callable for offline/test use.")
    rubric = _load_rubric()
    prompt = (
        f"# FIT RUBRIC (the ONLY basis for scoring)\n{rubric}\n\n"
        f"# JOB POSTING\nTitle: {job.get('title') or ''}\n"
        f"Company: {job.get('company') or ''}\n"
        f"Location: {job.get('location') or ''}\n\n"
        f"## Job description\n{str(job.get('jd_text') or '')}\n\n"
        "Score this posting against the rubric and return the JSON.")
    try:
        r = subprocess.run(
            [cli, "-p", "--output-format", "text", "--model", "sonnet",
             "--strict-mcp-config", "--append-system-prompt", _FIT_SYSTEM],
            input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=420,
        )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"claude -p fit-score call failed ({type(e).__name__}: {e}); "
                           "not falling back to the API.") from e
    out = (r.stdout or "").strip()
    return _parse_score(out)


def _parse_score(text: str) -> dict:
    """Parse a scorer reply into {"fit_score": int|None, "reason": str}. Tolerates a
    code-fenced or chatty reply by extracting the first JSON object; never raises."""
    if not text:
        return {"fit_score": None, "reason": ""}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    raw = m.group(0) if m else text
    try:
        obj = json.loads(raw)
    except Exception:
        return {"fit_score": None, "reason": text[:200]}
    score = obj.get("fit_score")
    try:
        score = int(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    return {"fit_score": score, "reason": str(obj.get("reason") or "")[:200]}


# ---- holding + jobs persistence (plain atomic JSON, no git spine) ----------- #

def _load_json_list(path: Path) -> list:
    """Read a JSON list from disk; missing/unreadable -> []. Non-list -> []."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _atomic_write_json(path: Path, data) -> None:
    """Write JSON to `path` atomically: serialize to a temp file in the same dir,
    then os.replace (atomic on the same filesystem) so a crash mid-write can never
    leave a half-written jobs/holding file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _next_job_id(jobs: list[dict]) -> str:
    """Allocate the next canonical JOB-NNN id (max existing + 1, zero-padded)."""
    mx = 0
    for j in jobs:
        if isinstance(j, dict):
            m = _JOB_ID_RE.match(str(j.get("id", "")))
            if m:
                mx = max(mx, int(m.group(1)))
    return f"JOB-{mx + 1:03d}"


# ---- core: one stub, then drain the holding list ---------------------------- #

def qualify_one(h: dict, jobs: list[dict], *, resolve_url=_default_resolve_url,
                fetch_jd=_default_fetch_jd, score=_default_score, enrich_ok=_enrichment_ok):
    """Resolve + enrich + score ONE holding stub.

    Returns (outcome, promoted_record | None) where outcome ∈
    {'promoted', 'prune', 'hold'}. Mutates `h` in place (attempts / url / jd_text).

      promoted -> `record` is a ready-to-append jobs.json dict with a fresh JOB-NNN.
      hold     -> couldn't enrich yet (thin JD / unresolved URL); retry next run.
      prune    -> dead: >= MAX_ATTEMPTS tries AND no live URL -> drop it. We NEVER
                  prune for low fit — only genuinely dead links.
    """
    # 1. resolve a direct posting URL if we have none OR only a LinkedIn link
    #    (un-driveable). A confident match overwrites; the original is preserved.
    cur = (h.get("url") or "").strip()
    if not cur or "linkedin.com" in cur.lower():
        u = resolve_url(h)
        if u and "linkedin.com" not in u.lower():
            if cur:
                h["source_url"] = cur
            h["url"] = u
    # 2. fetch the full JD if the one we have is thin and we have a URL to fetch from
    if len((h.get("jd_text") or "")) < MIN_JD_CHARS and (h.get("url") or "").strip():
        jd = fetch_jd(h["url"])
        if jd and len(jd) >= len(h.get("jd_text") or ""):
            h["jd_text"] = jd
    # 3. enrichment gate (deterministic stageability)
    if not enrich_ok(h):
        h["attempts"] = int(h.get("attempts", 0)) + 1
        dead = h["attempts"] >= MAX_ATTEMPTS and not (h.get("url") or "").strip()
        return ("prune" if dead else "hold"), None
    # 4. PASS -> score against the rubric + build a promotable jobs.json record
    r = score(h)
    rec = {
        "id": _next_job_id(jobs),
        "title": h.get("title") or "",
        "company": h.get("company") or "",
        "status": "Spotted",
        "fit_score": r.get("fit_score"),
        "fit_reason": r.get("reason", ""),
        "track": h.get("track"),
        "url": h.get("url") or "",
        "apply_url": h.get("url") or "",
        "jd_text": h.get("jd_text") or "",
        "location": h.get("location", ""),
        "source": h.get("source", "discover"),
        "date_added": h.get("date_added"),
    }
    return "promoted", rec


def run_qualify(cap: int = 25, dry_run: bool = False, *,
                holding_path: Path | None = None, jobs_path: Path | None = None,
                **inject) -> dict:
    """Drain up to `cap` stubs from the holding list: resolve + enrich + score +
    PROMOTE passers into jobs.json, hold or prune failures.

    Returns a summary dict {promoted, held, pruned, errors, promoted_ids}. With
    dry_run=True nothing is written to disk. `**inject` forwards the injectable
    seams (resolve_url / fetch_jd / score / enrich_ok) to qualify_one — tests pass
    stubs so the run is fully offline.
    """
    # Resolve paths LIVE from config (not at import time) so tests that monkeypatch
    # config.ARIA_DATA — and any ARIA_CORE_DATA / FIT_RUBRIC override — are honored.
    holding_path = holding_path or config.HOLDING_JSON
    jobs_path = jobs_path or config.JOBS_JSON

    holding = _load_json_list(holding_path)
    jobs = _load_json_list(jobs_path)
    promoted_ids: list = []
    pruned = held = errors = 0
    kept: list = []

    for h in holding[:cap]:
        if not isinstance(h, dict):
            errors += 1
            continue
        try:
            outcome, rec = qualify_one(h, jobs, **inject)
        except Exception:  # one bad stub can't abort the whole run
            errors += 1
            kept.append(h)  # leave it in holding for a human to look at
            continue
        if outcome == "promoted":
            jobs.append(rec)
            promoted_ids.append(rec["id"])
        elif outcome == "prune":
            pruned += 1  # dropped: not re-added to holding
        else:  # hold
            held += 1
            kept.append(h)

    # holding now = the kept ones (promoted + pruned both LEAVE holding) plus
    # everything past the cap we never touched this run.
    kept_all = kept + [h for h in holding[cap:]]

    if not dry_run:
        _atomic_write_json(jobs_path, jobs)
        _atomic_write_json(holding_path, kept_all)

    summary = {
        "promoted": len(promoted_ids),
        "held": held,
        "pruned": pruned,
        "errors": errors,
        "promoted_ids": promoted_ids,
    }
    mode = "DRY-RUN" if dry_run else "LIVE"
    print(f"[qualify {mode}] promoted {summary['promoted']} | held {held} | "
          f"pruned {pruned} | errors {errors}")
    return summary


if __name__ == "__main__":  # convenience: `python -m apply_engine.qualify.qualify`
    from .cli import main
    sys.exit(main())
