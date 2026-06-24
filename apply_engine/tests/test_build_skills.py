"""test_build_skills.py — the resume builder must never emit a bare 'Python' or 'MATLAB'
skills proficiency.

Background (claims_ledger.md ~line 87, feedback_no_coding_language_fluency):
The applicant does NOT claim unaided hand-coding fluency. On a resume skills line:
  - MATLAB must be DROPPED entirely (never a proficiency claim).
  - bare "Python" implies hand-coding fluency at rigorous-coding employers (Anthropic) and
    caused a calibration FAIL on JOB-237. Python may appear ONLY in the AI-orchestrated
    framing the master resume uses ("Python-based tooling, AI-orchestrated rather than
    hand-coded"), never as a standalone proficiency token.

These tests exercise build.build_resume_content's hardcoded per-app skills strings directly.
get_app_content is monkeypatched so the test does not depend on applications.json data.
"""

import re
import sys
from pathlib import Path

import pytest

# build.py lives in career/ (the apply_engine parent), not in apply_engine/. Put it on the path.
_CAREER_DIR = Path(__file__).resolve().parents[2]
if str(_CAREER_DIR) not in sys.path:
    sys.path.insert(0, str(_CAREER_DIR))

build = pytest.importorskip("build")  # skips cleanly if render/fitz deps are unavailable

# Every app_id that has a hardcoded skills branch in build_resume_content, plus one unknown id
# to exercise the `else` default branch.
_APP_IDS = [
    "APP-008", "APP-009", "APP-010", "APP-011", "APP-012", "APP-013", "APP-014",
    "APP-022", "APP-023", "APP-024", "APP-025", "APP-026", "APP-027",
    "APP-999-UNKNOWN-DEFAULT-BRANCH",
]

# A bare "Python" token = "Python" NOT immediately adjacent to an AI-orchestration qualifier.
# Acceptable: "Python (AI-orchestrated)", "AI-orchestrated Python-based tooling", etc.
_AI_QUALIFIER = r"(?:AI[\s\-]?orchestrat\w*|AI[\s\-]?native|AI[\s\-]?built|agent(?:ic)?[\s\-]?built)"
# A Python mention is OK if an AI qualifier sits within a short window on either side of it.
_PYTHON_OK = re.compile(
    rf"(?:{_AI_QUALIFIER}[^·|]{{0,40}}\bPython\b)|(?:\bPython\b[^·|]{{0,40}}{_AI_QUALIFIER})",
    re.IGNORECASE,
)
_PYTHON_ANY = re.compile(r"\bPython\b", re.IGNORECASE)
_MATLAB_ANY = re.compile(r"\bMATLAB\b", re.IGNORECASE)


def _skills_text(app_id, monkeypatch):
    """Render only the skills blocks for app_id, independent of applications.json."""
    monkeypatch.setattr(
        build, "get_app_content",
        lambda _id: {
            "app_id": _id, "job_id": None, "company": "", "role": "", "track": None,
            "is_competitor": False, "summary_style": "", "resume": {}, "cover": {},
            "cover_letter_text": "",
        },
    )
    return build.build_resume_content(app_id)["SKILLS_BLOCKS"]


@pytest.mark.parametrize("app_id", _APP_IDS)
def test_skills_has_no_bare_matlab(app_id, monkeypatch):
    text = _skills_text(app_id, monkeypatch)
    assert not _MATLAB_ANY.search(text), (
        f"{app_id} skills still lists MATLAB — must be dropped entirely. Got: {text!r}")


@pytest.mark.parametrize("app_id", _APP_IDS)
def test_skills_has_no_bare_python(app_id, monkeypatch):
    text = _skills_text(app_id, monkeypatch)
    # Strip every ACCEPTABLE (AI-orchestrated-qualified) Python mention; any Python left is bare.
    residual = _PYTHON_OK.sub("", text)
    assert not _PYTHON_ANY.search(residual), (
        f"{app_id} skills contain a bare 'Python' proficiency (not adjacent to an "
        f"AI-orchestration qualifier). Got: {text!r}")


@pytest.mark.parametrize("app_id", _APP_IDS)
def test_skills_preserve_core_engineering(app_id, monkeypatch):
    """Reframing must not strip the non-Python/MATLAB skills — FEA must survive."""
    text = _skills_text(app_id, monkeypatch)
    assert "FEA" in text, f"{app_id} lost its FEA skills content during reframing: {text!r}"
