"""runner.py — the engage reversibility spine + A/B/C orchestrator.

This is the heart of the engage agent: it sequences a set of lanes, isolates any
lane fault to bucket C (the run never aborts), records every change as a journal
entry, and on an opt-in live commit writes exactly one git commit covering ONLY
the files it changed — so a whole run is revertible with `git revert`.

Autonomy buckets (every touched item lands in exactly one):
  A  auto-commit   deterministic hygiene (schema repair, follow-up cadence)
  B  auto-stage    a sourced/verified contact or staged app at the BRINK (one click)
  C  needs-work    below-confidence / unverifiable -> a passive bucket, no push

Safety contract:
  * NEVER sends or submits. No path here calls an email send or an apply submit.
  * `--dry-run`  -> journal only: zero file writes, zero commit.
  * git commit is OPT-IN (config `commit: false` by default) and, when enabled,
    stages ONLY the specific state files this run changed — NEVER `git add -A`
    (which would sweep a cloner's untracked secrets into a commit).
  * B/C staging lanes are PLAN-ONLY in this public build: they SELECT targets and
    record the intended action, but fire no external engine (warm_path is stubbed;
    live application staging belongs to the apply flow, not here).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .. import config
from . import contact_hygiene, crm_util, warm_path

RUNS_SUBDIR = "engage_runs"

BUCKET_A = "A-auto-commit"
BUCKET_B = "B-auto-stage"
BUCKET_C = "C-needs-work"

CONTACT_DEAD_STATUSES = {"dead", "closed", "rejected"}
JOB_TERMINAL_STATUSES = {"applied", "closed", "rejected"}

# Config defaults. Every lane that could fire an external action is OFF by default;
# a malformed config file fails SAFE back to exactly these (see load_config).
_CONFIG_DEFAULTS = {
    "enable_hygiene": True,       # deterministic A-bucket hygiene (safe; on by default)
    "enable_staging": False,      # B-bucket app staging (plan-only in this build)
    "enable_sourcing": False,     # B-bucket warm-path sourcing (stubbed in this build)
    "enable_contact_linkedin": False,  # C-bucket LinkedIn sourcing (needs your own runner)
    "commit": False,              # opt-in: make one git commit per live run
    "min_fit": 7,                 # only act on jobs at/above this fit score
    "source_cap": 3,              # max companies sourced per run
    "stage_cap": 3,               # max apps staged per run
    "confidence_floor": 7,        # below this -> bucket C, never surfaced
}


def _runs_dir() -> Path:
    return config.ARIA_DATA / RUNS_SUBDIR


def config_file() -> Path:
    return config.ARIA_DATA / "engage_config.json"


def load_config() -> dict:
    """Load config, fail SAFE. A missing file uses defaults; a malformed file does
    NOT run any lane hot — it reverts entirely to the (mostly-off) defaults."""
    cfg = dict(_CONFIG_DEFAULTS)
    try:
        loaded = json.loads(config_file().read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            return dict(_CONFIG_DEFAULTS)
        cfg.update(loaded)
    except FileNotFoundError:
        pass
    except Exception:  # malformed -> fail safe, everything off
        return dict(_CONFIG_DEFAULTS)
    return cfg


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _run_id() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


class EngageRun:
    """Tracks every change as a journal entry; on an opt-in live commit, writes the
    journal and makes exactly one git commit (of ONLY the files it changed) so the
    whole run is revertible."""

    def __init__(self, dry_run: bool = False, commit: bool = False, repo: Path | None = None):
        self.dry_run = dry_run
        self.commit_enabled = commit and not dry_run
        # repo to commit into; defaults to the data dir's repo if the caller wants
        # commits. We only ever `git add` specific files under here.
        self.repo = Path(repo) if repo else config.ARIA_DATA
        self.run_id = _run_id()
        self.started = _now()
        self.entries: list[dict] = []

    def record(self, *, file: str, target_id, field: str, before, after,
               bucket: str, reason: str, confidence=None) -> None:
        self.entries.append({
            "file": file, "id": target_id, "field": field,
            "before": before, "after": after, "bucket": bucket,
            "reason": reason, "confidence": confidence,
        })

    # ---- summary -------------------------------------------------------- #
    def counts(self) -> dict:
        c = {BUCKET_A: 0, BUCKET_B: 0, BUCKET_C: 0}
        for e in self.entries:
            c[e["bucket"]] = c.get(e["bucket"], 0) + 1
        return c

    def changed_files(self) -> list[str]:
        """The distinct real state files this run actually wrote to (bucket A only —
        B/C are plan/flag records that mutate nothing). Used to scope the git add."""
        files = []
        for e in self.entries:
            f = e.get("file")
            if (e.get("bucket") == BUCKET_A and isinstance(f, str)
                    and f.endswith(".json") and not f.startswith("(")):
                if f not in files:
                    files.append(f)
        return files

    # ---- persistence ---------------------------------------------------- #
    def write_journal(self) -> Path:
        runs = _runs_dir()
        runs.mkdir(parents=True, exist_ok=True)
        path = runs / f"{self.run_id}{'.dryrun' if self.dry_run else ''}.json"
        payload = {
            "run_id": self.run_id, "started": self.started, "finished": _now(),
            "dry_run": self.dry_run, "commit_enabled": self.commit_enabled,
            "counts": self.counts(), "entries": self.entries,
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def commit(self, journal_path: Path | None = None) -> str | None:
        """Stage + commit ONLY the specific files this run changed (plus the run's
        journal). Returns the short sha, or None if disabled / dry-run / nothing
        changed / not a git repo. NEVER runs `git add -A`."""
        if self.dry_run or not self.commit_enabled:
            return None
        # only commit if we're inside a git work tree
        try:
            inside = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"], cwd=self.repo,
                capture_output=True, text=True).stdout.strip()
        except Exception:
            return None
        if inside != "true":
            return None

        # build the explicit, scoped file list — relative to the data dir
        targets: list[Path] = []
        for rel in self.changed_files():
            p = config.ARIA_DATA / rel
            if p.exists():
                targets.append(p)
        if journal_path and Path(journal_path).exists():
            targets.append(Path(journal_path))
        if not targets:
            return None

        # stage each file explicitly (never `git add -A`)
        for p in targets:
            subprocess.run(["git", "add", "--", str(p)], cwd=self.repo, check=True)
        # anything actually staged?
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"], cwd=self.repo,
            capture_output=True, text=True).stdout.strip()
        if not staged:
            return None
        c = self.counts()
        msg = (f"[engage] run {self.run_id}: "
               f"{c[BUCKET_A]} cleaned, {c[BUCKET_B]} staged, {c[BUCKET_C]} needs-work")
        subprocess.run(["git", "commit", "-m", msg], cwd=self.repo, check=True,
                       capture_output=True, text=True)
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=self.repo,
                             capture_output=True, text=True).stdout.strip()
        return sha


# ------------------------------------------------------------------------- #
# Bucket A — deterministic hygiene
# ------------------------------------------------------------------------- #

def hygiene_contacts(run: EngageRun, cfg: dict) -> None:
    """Schema-repair contacts via the canonical writer: fill missing ids, coerce
    warmth, migrate .draft->.body, ensure the outreach.verify gate exists."""
    path = crm_util._contacts_file()
    rows = crm_util.load_contacts(path, normalize=False)
    before = json.dumps(rows, ensure_ascii=False, sort_keys=True)

    for c in rows:
        if isinstance(c, dict) and not c.get("id"):
            new_id = crm_util.next_prefixed_id(rows, "CON")
            run.record(file="contacts.json", target_id=new_id, field="id",
                       before=None, after=new_id, bucket=BUCKET_A,
                       reason="contact had no id (would not render/click)")
            c["id"] = new_id
    for c in rows:
        if isinstance(c, dict):
            crm_util.normalize_contact(c)

    after = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    if before != after:
        run.record(file="contacts.json", target_id="*", field="schema",
                   before="pre-normalize", after="canonical", bucket=BUCKET_A,
                   reason="normalized contact schema (warmth/outreach/verify)")
        if not run.dry_run:
            crm_util.save_contacts(rows, path)


def hygiene_applications(run: EngageRun, cfg: dict) -> None:
    """FLAG-ONLY (no writes). applications.json can co-mingle two shapes: real
    packages (have an APP id) and apply-flow stubs (keyed by job_id, deliberately
    id-less). Assigning ids to stubs would make them masquerade as real packages, so
    engage never auto-mutates applications — it surfaces integrity issues to bucket C."""
    rows = crm_util.load_applications(crm_util._applications_file(), normalize=False)
    real_jids = {a.get("job_id") for a in rows
                 if isinstance(a, dict) and a.get("id") and a.get("job_id")}
    for a in rows:
        if not isinstance(a, dict):
            continue
        jid = a.get("job_id")
        if not a.get("id") and jid in real_jids:
            run.record(file="applications.json", target_id=jid, field="duplicate",
                       before="stub + real app share job_id", after=None,
                       bucket=BUCKET_C,
                       reason="id-less stub duplicates an id-bearing app — review/merge by hand")


def hygiene_outreach(run: EngageRun, cfg: dict) -> None:
    """Deterministic outreach-freshness self-heal (no LLM):
      - set a missing next_follow_up from cadence so cold contacts resurface (bucket A)
      - clamp a future-dated last_contact (data error) (bucket A)
      - flag overdue follow-ups + phantom-"active" rows for review (bucket C)"""
    path = crm_util._contacts_file()
    rows = crm_util.load_contacts(path, normalize=False)
    today = datetime.now()

    changes = contact_hygiene.repair_followups(rows, today)
    for ch in changes:
        run.record(file="contacts.json", target_id=ch["id"], field=ch["field"],
                   before=ch["before"], after=ch["after"], bucket=BUCKET_A,
                   reason=("set follow-up from cadence so the contact resurfaces"
                           if ch["field"] == "next_follow_up"
                           else "clamped future-dated last_contact (data error)"))
    if changes and not run.dry_run:
        crm_util.save_contacts(rows, path)

    for issue in contact_hygiene.find_outreach_issues(rows, today):
        if issue["issue"] in ("overdue_followup", "phantom_engaged"):
            run.record(file="contacts.json", target_id=issue["id"], field=issue["issue"],
                       before=None, after=issue["detail"], bucket=BUCKET_C,
                       reason="outreach note stale — needs a follow-up or a status change")


# ------------------------------------------------------------------------- #
# Bucket B/C — selection (pure, tested) + plan-only staging lanes
# ------------------------------------------------------------------------- #

def _fit(job: dict) -> int:
    for k in ("fit_score", "score"):
        v = job.get(k)
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return 0


def live_companies(contacts: list[dict]) -> set[str]:
    """Casefolded set of companies that already have a non-dead contact."""
    out = set()
    for c in contacts:
        if not isinstance(c, dict):
            continue
        status = (c.get("status") or "").strip().lower()
        if status in CONTACT_DEAD_STATUSES:
            continue
        comp = (c.get("company") or "").strip()
        if comp:
            out.add(comp.casefold())
    return out


def select_companies_needing_contacts(jobs, contacts, *, min_fit, cap):
    """High-fit-job companies that have NO live contact, ranked by best fit, capped.
    Returns the original-cased company name (best-fit row wins the casing)."""
    have = live_companies(contacts)
    best: dict[str, tuple[int, str]] = {}
    for j in jobs:
        if not isinstance(j, dict):
            continue
        comp = (j.get("company") or "").strip()
        if not comp or comp.casefold() in have:
            continue
        f = _fit(j)
        if f < min_fit:
            continue
        key = comp.casefold()
        if key not in best or f > best[key][0]:
            best[key] = (f, comp)
    ranked = sorted(best.values(), key=lambda t: -t[0])
    return [name for _f, name in ranked[:cap]]


def _enrichment_ok(job: dict) -> bool:
    """Reuse the apply flow's JD-quality gate if available; fail CLOSED if it can't
    be imported (never silently re-open a junk floodgate)."""
    try:
        from .. import job_enrichment  # optional; not part of this port
        ok, _reason = job_enrichment.enrichment_status(job)
        return bool(ok)
    except Exception:
        # No enrichment module in this build -> use a minimal in-line quality bar:
        # a real, non-LinkedIn apply URL and a non-trivial description.
        url = (job.get("url") or job.get("apply_url") or "").strip().lower()
        if not url or "linkedin.com" in url:
            return False
        return len((job.get("description") or "")) >= 200


def _stage_eligible(job: dict, staged_ids: set[str]) -> bool:
    if not isinstance(job, dict):
        return False
    jid = job.get("id")
    if not jid or jid in staged_ids:
        return False
    url = (job.get("url") or job.get("apply_url") or "").strip()
    if not url or "linkedin.com" in url.lower():
        return False
    if (job.get("status") or "").strip().lower() in JOB_TERMINAL_STATUSES:
        return False
    return _enrichment_ok(job)


def select_jobs_to_stage(jobs, staged_ids, *, min_fit, cap):
    """Eligible, unstaged, high-fit jobs ranked by fit, capped. Returns job ids."""
    elig = [j for j in jobs if _fit(j) >= min_fit and _stage_eligible(j, staged_ids)]
    elig.sort(key=lambda j: -_fit(j))
    return [j["id"] for j in elig[:cap]]


def _load_json_list(name: str) -> list:
    try:
        raw = json.loads((config.ARIA_DATA / name).read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


def _staged_job_ids() -> set[str]:
    return {r.get("job_id") for r in _load_json_list("staged_applications.json")
            if isinstance(r, dict) and r.get("job_id")}


def lane_source_contacts(run: EngageRun, cfg: dict) -> None:
    """SELECT top-fit companies with no live contact and record the intended action.

    In this public build the warm_path engine is a stub, so this lane is PLAN-ONLY:
    it records a bucket-B 'planned' entry per target (or routes to C if the stub is
    invoked and reports not-configured). It fires no external engine and sends nothing."""
    jobs = _load_json_list("jobs.json")
    try:
        contacts = crm_util.load_contacts(normalize=False)
    except Exception as e:
        run.record(file="(lane_source)", target_id=None, field="error", before=None,
                   after=str(e), bucket=BUCKET_C, reason="could not load contacts")
        return

    targets = select_companies_needing_contacts(
        jobs, contacts, min_fit=cfg["min_fit"], cap=cfg["source_cap"])

    for company in targets:
        if not cfg.get("enable_sourcing") or run.dry_run:
            run.record(file="contacts.json", target_id=company, field="source-contact",
                       before=None, after="planned", bucket=BUCKET_B,
                       reason=("would source+verify a contact (sourcing disabled — set "
                               "enable_sourcing=true)" if not cfg.get("enable_sourcing")
                               else "dry-run: would source a contact"))
            continue
        # enabled + live: consult the (stubbed) warm_path hook; not-configured -> C
        result = warm_path.find_path(company, config=cfg)
        ok = bool(result.get("ok"))
        run.record(file="contacts.json", target_id=company, field="source-contact",
                   before=None, after=("verified" if ok else result.get("status", "no-contact")),
                   bucket=(BUCKET_B if ok else BUCKET_C),
                   reason=result.get("detail", "warm-path returned no verified contact"))


def lane_stage_applications(run: EngageRun, cfg: dict) -> None:
    """SELECT top-fit eligible jobs and record the intended staging action.

    PLAN-ONLY in this build: live stage-to-brink belongs to the apply flow, not the
    engage overlay. This lane never opens a browser and never submits — it records a
    bucket-B 'planned' entry per eligible job."""
    jobs = _load_json_list("jobs.json")
    targets = select_jobs_to_stage(
        jobs, _staged_job_ids(), min_fit=cfg["min_fit"], cap=cfg["stage_cap"])
    for jid in targets:
        run.record(file="staged_applications.json", target_id=jid, field="stage-app",
                   before=None, after="planned", bucket=BUCKET_B,
                   reason=("would stage to brink (staging disabled — set enable_staging=true)"
                           if not cfg.get("enable_staging")
                           else "would stage to brink via the apply flow (run the apply CLI)"))


def lane_source_linkedin(run: EngageRun, cfg: dict) -> None:
    """SELECT actionable contacts with no LinkedIn URL and record the intended action.

    The LLM sourcing call (`contact_hygiene.source_linkedin`) needs a `runner` you
    supply; this build ships none, so the lane is PLAN-ONLY (bucket C 'planned')
    unless enable_contact_linkedin=true AND a runner is wired in by an embedder."""
    path = crm_util._contacts_file()
    try:
        rows = crm_util.load_contacts(path, normalize=False)
    except Exception as e:
        run.record(file="(lane_source_linkedin)", target_id=None, field="error",
                   before=None, after=str(e), bucket=BUCKET_C, reason="could not load contacts")
        return
    missing = contact_hygiene.contacts_missing_linkedin(rows)[: cfg.get("source_cap", 3)]
    for c in missing:
        run.record(file="contacts.json", target_id=c.get("id"), field="source-linkedin",
                   before=None, after="planned", bucket=BUCKET_C,
                   reason=("would web-search this contact's LinkedIn (lane needs your own "
                           "runner + enable_contact_linkedin=true)"))


PHASE_A = [hygiene_contacts, hygiene_applications, hygiene_outreach]
PHASE_BC = [lane_source_contacts, lane_source_linkedin, lane_stage_applications]


def run_engage(*, dry_run: bool = False, commit: bool = False,
               repo: Path | None = None, cfg: dict | None = None) -> EngageRun:
    """Orchestrate the lanes. A broken lane is isolated to bucket C and the run
    continues — it never aborts. Returns the completed EngageRun."""
    cfg = cfg if cfg is not None else load_config()
    run = EngageRun(dry_run=dry_run, commit=commit, repo=repo)

    phase_a = PHASE_A if cfg.get("enable_hygiene", True) else []
    for step in phase_a:
        try:
            step(run, cfg)
        except Exception as e:  # a broken step must not abort the whole run
            run.record(file="(orchestrator)", target_id=step.__name__, field="error",
                       before=None, after=str(e), bucket=BUCKET_C,
                       reason=f"step {step.__name__} raised")
    for lane in PHASE_BC:
        try:
            lane(run, cfg)
        except Exception as e:
            run.record(file="(orchestrator)", target_id=lane.__name__, field="error",
                       before=None, after=str(e), bucket=BUCKET_C,
                       reason=f"lane {lane.__name__} raised")

    journal = run.write_journal()
    run.last_journal = journal
    run.last_sha = run.commit(journal_path=journal)
    return run


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="career-engine engage",
                                 description="Autonomous career-ops orchestrator (A/B/C buckets).")
    ap.add_argument("--dry-run", action="store_true",
                    help="journal only — no file writes, no commit")
    ap.add_argument("--commit", action="store_true",
                    help="on a live run, make one git commit of ONLY the files changed "
                         "(overrides config commit=false; ignored under --dry-run)")
    args = ap.parse_args(argv)

    cfg = load_config()
    commit = args.commit or bool(cfg.get("commit"))
    run = run_engage(dry_run=args.dry_run, commit=commit, cfg=cfg)
    c = run.counts()

    mode = "DRY-RUN" if args.dry_run else "LIVE"
    print(f"[engage {mode}] run {run.run_id}")
    print(f"  A cleaned: {c[BUCKET_A]} | B staged: {c[BUCKET_B]} | C needs-work: {c[BUCKET_C]}")
    print(f"  journal: {getattr(run, 'last_journal', None)}")
    print(f"  commit:  {getattr(run, 'last_sha', None) or '(none — disabled / dry-run / no changes)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
