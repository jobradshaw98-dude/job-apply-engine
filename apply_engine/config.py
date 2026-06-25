"""Shared paths and constants for apply_engine."""
import os
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
REPO_ROOT = PKG_DIR.parent
EXAMPLES_DIR = REPO_ROOT / "examples"   # bundled sample jobs.json / profile / voice — copy these to start

# Where your real data lives (jobs.json, applications.json, master_resume.md, ...).
# Override with the ARIA_CORE_DATA env var; otherwise defaults to a local, git-ignored ./data
# folder at the repo root. (It deliberately does NOT point anywhere outside the repo.)
ARIA_DATA = Path(os.environ.get("ARIA_CORE_DATA", str(REPO_ROOT / "data")))
JOBS_JSON = ARIA_DATA / "jobs.json"
APPLICATIONS_JSON = ARIA_DATA / "applications.json"
STAGED_MANIFEST = ARIA_DATA / "staged_applications.json"  # the apply-queue staged-record manifest
HOLDING_JSON = ARIA_DATA / "holding.json"  # raw discovered stubs awaiting qualify (source -> qualify hand-off)

# Fit-scoring rubric the qualify pass scores each posting against. Defaults to the
# bundled generic example; point FIT_RUBRIC at your own rubric (describe the candidate
# you're scoring for) via the env var, or drop one in your ARIA_CORE_DATA folder.
FIT_RUBRIC = Path(os.environ.get("FIT_RUBRIC", str(EXAMPLES_DIR / "fit_rubric.example.md")))

RUNS_DIR = PKG_DIR / "runs"              # per-job run artifacts (git-ignored)
PROFILE_DIR = PKG_DIR / "profile"        # dedicated bot Chrome user-data-dir (git-ignored)
PROFILE_EXAMPLE = PKG_DIR / "applicant_profile.example.json"
PROFILE_JSON = PKG_DIR / "applicant_profile.json"  # real PII (git-ignored)
