# -*- coding: utf-8 -*-
"""Deterministic-gate coverage for the 2026-06-21 stale-rule cleanup.

Three rules consolidated into audit_gate.py so every build path (build.py, tailor.py, and the
static stream templates that bypass tailor's own guards) is caught:
  1. MATLAB never appears on a skills line (promoted from tailor.py's _MATLAB reject).
  2. New honesty-stray forbidden phrases: clinical-facing, Claude SDK, investment scale,
     production-level.
  3. impact-as-count is RESUME-only — covers/essays (prose) may use a people/time count.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import audit_gate  # noqa: E402


def _audit(html: str, stem: str):
    p = Path(__file__).parent / f"{stem}.html"
    p.write_text(html, encoding="utf-8")
    try:
        return audit_gate.audit_file(str(p))
    finally:
        try:
            p.unlink()
        except OSError:
            pass


def _rules(res, sev=None):
    return [v["rule"] for v in res["violations"] if sev is None or v["severity"] == sev]


# ---- MATLAB on a skills line --------------------------------------------------

def test_matlab_on_skills_line_blocks():
    html = ('<html><body><div class="section-header">Skills</div>'
            '<div class="skill-row">Software &amp; Languages: Python · Git · MATLAB</div></body></html>')
    res = _audit(html, "_matlab_tmp")
    assert "matlab_skills" in _rules(res, "block")


def test_skills_line_without_matlab_clean():
    html = ('<html><body><div class="section-header">Skills</div>'
            '<div class="skill-row">Software &amp; Languages: Python · Git · Bash</div></body></html>')
    res = _audit(html, "_matlab_clean_tmp")
    assert "matlab_skills" not in _rules(res)


# ---- new honesty-stray forbidden phrases -------------------------------------

@pytest.mark.parametrize("phrase", [
    "clinical-facing",
    "Claude SDK",
    "investment scale",
    "production-level",
])
def test_new_forbidden_phrases_flagged(phrase):
    html = f"<html><body><p>I bring {phrase} experience to the team.</p></body></html>"
    res = _audit(html, "_phrase_cover_tmp")
    hits = [v for v in res["violations"]
            if v["rule"] == "forbidden_phrase" and phrase.lower() in v["found"].lower()]
    assert hits, f"expected forbidden_phrase hit for {phrase!r}"


# ---- cover vs resume coherence on the count rule -----------------------------

_COUNT = "replaced a 10-person, two-hour cross-team review with an automated pipeline"


def test_count_blocks_on_resume_but_not_cover():
    resume = ('<html><body><div class="summary">x</div>'
              '<div class="section-header">Experience</div>'
              f"<ul><li>{_COUNT}</li></ul></body></html>")
    cover = f"<html><body><p>At Meridian I {_COUNT}.</p></body></html>"
    r = _audit(resume, "_count_resume_tmp")
    c = _audit(cover, "_count_cover_tmp")
    assert "impact_as_count" in _rules(r, "block"), "resume must still block the count"
    assert "impact_as_count" not in _rules(c), "cover must allow the count"
