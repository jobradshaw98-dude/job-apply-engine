# -*- coding: utf-8 -*-
"""Deterministic gate for the ledger 'impact-as-people/time-count' rendering rule.

The claims_ledger (Meridian block, locked 2026-05-30) requires the prototype-analysis agent's
impact to be rendered as a PERCENTAGE ('~90% reduction'), NEVER as a count of engineers/people
or an hour/meeting duration. The LLM judge (refresh_audit) already BLOCKs this, but the
convergence rewrite loop in regen_answer keys off the DETERMINISTIC gate (career/audit_gate.py),
which could not see the pattern — so such answers landed BLOCKED-for-review instead of being
auto-rewritten to the percentage form.

This adds a SCOPED deterministic check. The discipline is FALSE-POSITIVE-FIRST: it must catch the
JOB-307 renderings (and close cousins) while blocking ZERO legitimate uses. The must-NOT-block
corpus below is the contract that keeps the regex tight.

Two levels are proven:
  1. audit_file() directly (the rule lives in audit_gate.py).
  2. make_audit_fn() wrapper — proves the convergence loop (regen_answer's audit(raw)) will see
     the new BLOCK so converge can re-attempt the rewrite.
"""
import sys
from pathlib import Path

import pytest

# Make career-root modules (audit_gate) importable when pytest launches from career root or the
# apply_engine package dir.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import audit_gate  # noqa: E402
from apply_engine.llm import make_audit_fn  # noqa: E402


# ---------------------------------------------------------------------------
# Corpora. The MUST-NOT-BLOCK set is the false-positive contract — it is as
# important as the MUST-BLOCK set and is verified to produce ZERO blocks.
# ---------------------------------------------------------------------------

# Forbidden renderings: a headcount OR a time/meeting duration used AS the impact / what the
# agent replaced. Includes the live JOB-307 text and its close cousins.
MUST_BLOCK = [
    # JOB-307 live text + cousins (headcount + review/meeting)
    "My prototype-analysis agent replaced a recurring review of 10+ engineers and a two-hour "
    "cross-team meeting.",
    "Built a root-cause agent that replaced a 10+ person, two-hour cross-team review.",
    "It replaced a review of more than ten engineers and a two-hour cross-team review.",
    "an agent that eliminated a meeting of ten engineers and a two-hour review",
    "replaced a 10+ person cross-team review",
    "automated away a 12-person design review",
    "a 10-engineer review that I automated",
    # time/meeting duration AS the impact (no headcount needed)
    "My agent replaced a two-hour cross-team review.",
    "It eliminated a two-hour cross-team meeting every prototype cycle.",
    "replaced a recurring 90-minute design review",
]

# Legitimate uses — descriptive headcount, personal effort, non-review durations, and any
# percentage-framed impact. EVERY one of these MUST produce zero blocks from the new rule.
MUST_NOT_BLOCK = [
    # the correct rendering of the very claim above
    "My prototype-analysis agent drove roughly a ~90% reduction in cross-team review effort.",
    "reduced review time by 90%",
    "~90% reduction in review effort",
    "drove a ~90% reduction in cross-team review effort with a prototype-analysis agent",
    # descriptive headcount, NOT impact-framed
    "10 people on the team",
    "the team of 12 engineers shipped the launch on time",
    "I led a review with several engineers across teams",
    # durations that are NOT a review/meeting being replaced
    "a two-hour onboarding session",
    "I spent two hours debugging the pipeline",
    "a 30-minute standup keeps us aligned",
    "the build takes about two hours end to end",
    # normal grounded bullet, no counts at all
    "I orchestrate AI agents to build and ship automation rather than hand-coding it myself.",
    # cross-clause false positives: a replace-verb in one comma/conjunction clause + an
    # UNRELATED review-of-N-engineers or duration-review in the next clause. The verb and the
    # count are NOT in the same clause, so this is descriptive, not a ledger violation.
    "I automated the reporting pipeline, and led a review of 8 engineers on a separate launch.",
    "I removed legacy code that a review of ten engineers had flagged.",
    "I saved budget on tooling and also sat in a two-hour review weekly.",
]


def _gate_blocks(text: str):
    """Run the answer-path gate wrapper and return only the impact-count block notes."""
    notes = make_audit_fn()(text)
    return [n for n in notes if n and "percentage" in n.lower()]


def _impact_blocks_for(html: str, stem: str):
    """Run audit_file on the given HTML (written under `stem`) → impact_as_count blocks only."""
    p = Path(__file__).parent / f"{stem}.html"
    p.write_text(html, encoding="utf-8")
    try:
        res = audit_gate.audit_file(str(p))
    finally:
        try:
            p.unlink()
        except OSError:
            pass
    return [v for v in res["violations"]
            if v["rule"] == "impact_as_count" and v["severity"] == "block"]


def _resume_blocks(text: str):
    """Wrap text as a RESUME bullet (section-structured → is_letter False)."""
    html = ('<html><body><div class="summary">x</div>'
            '<div class="section-header">Experience</div>'
            f"<ul><li>{text}</li></ul></body></html>")
    return _impact_blocks_for(html, "_impact_resume_tmp")


def _cover_blocks(text: str):
    """Wrap text as a COVER LETTER (bare prose + 'cover' in the stem → is_letter True)."""
    return _impact_blocks_for(f"<html><body><p>{text}</p></body></html>", "_impact_cover_tmp")


# ---- MUST-BLOCK (RESUME path — percentage-only stays enforced) ------------

@pytest.mark.parametrize("text", MUST_BLOCK)
def test_forbidden_rendering_blocks_on_resume(text):
    blocks = _resume_blocks(text)
    assert blocks, f"expected an impact_as_count BLOCK on a resume for {text!r}"
    # fix hint must steer toward the percentage form
    assert "percentage" in (blocks[0]["note"] or "").lower()
    assert "%" in (blocks[0]["note"] or "")


# ---- COVER path — counts ALLOWED in prose (2026-06-21) -------------

@pytest.mark.parametrize("text", MUST_BLOCK)
def test_count_allowed_in_cover_letter(text):
    # Cover letters are prose, like essay answers: a concrete "10-person, two-hour review" is
    # stronger than a vague "~90%". So the count must NOT block on the cover path; only the resume
    # bullet stays percentage-only (test_forbidden_rendering_blocks_on_resume, above).
    assert _cover_blocks(text) == [], f"cover path should ALLOW people-count framing: {text!r}"


@pytest.mark.parametrize("text", MUST_BLOCK)
def test_people_count_allowed_in_answer_wrapper(text):
    # POLICY (2026-06-17): people/time-count impact framing is ALLOWED in free-text ESSAY answers
    # (a concrete '10-person review' is vivid and credible). make_audit_fn is the ANSWER gate, so it
    # must NOT block these. The ledger's percentage-only rule stays for resume/cover bullets — which
    # audit on the audit_file path directly (test_forbidden_rendering_blocks_in_audit_file, above).
    assert _gate_blocks(text) == [], f"answer gate should ALLOW people-count framing now: {text!r}"


# ---- MUST-NOT-BLOCK (false-positive contract) -----------------------------

@pytest.mark.parametrize("text", MUST_NOT_BLOCK)
def test_legitimate_use_not_blocked_on_resume(text):
    assert _resume_blocks(text) == [], f"FALSE POSITIVE: impact_as_count blocked {text!r}"


@pytest.mark.parametrize("text", MUST_NOT_BLOCK)
def test_legitimate_use_not_blocked_in_wrapper(text):
    assert _gate_blocks(text) == [], f"FALSE POSITIVE via wrapper: {text!r}"
