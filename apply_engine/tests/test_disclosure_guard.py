# -*- coding: utf-8 -*-
"""Tests for the immigration/work-auth DISCLOSURE guard.

The guard makes it IMPOSSIBLE for a ledger-grounded regen to "converge" to a TRUTHFUL-but-policy-
violating essay that volunteers the user's visa/citizenship/sponsorship/GC status in free-text
application content. The structured work-auth DROPDOWN answers (sponsorship=No, authorized=Yes) are
fine — this guard is ONLY about volunteered status in FREE-TEXT (essays, additional-info,
cover, resume).

Three layers under test:
  1. detect_immigration_disclosure — the deterministic detector (precision + recall).
  2. Gate integration — a disclosure in an essay answer / cover paragraph surfaces a BLOCK finding
     from refresh_audit's gate path (so converge + verify_ready see it).
  3. verify_ready — a record whose essay/content carries a disclosure is NOT ready.
  4. Iterative removal — the disclosure finding threads into iterate_fix.feedback_clause as a
     REMOVAL instruction.

Deterministic + offline: no claude -p, no network. Injected gate_fn/llm are call-tracking.
"""

import pytest

from apply_engine.disclosure_guard import detect_immigration_disclosure
from apply_engine.refresh_audit import audit_answers, audit_content_text
from apply_engine import iterate_fix


# The exact JOB-237 violation that motivated the guard.
JOB237 = ("I am a Canadian citizen currently working in the United States on a TN visa, authorized "
          "to work for engineering roles that qualify under TN status. I raise this upfront because "
          "I value transparency with employers.")


# ---- 1. detect_immigration_disclosure: recall (must BLOCK) ------------------------------------

def test_job237_sentence_blocks():
    findings = detect_immigration_disclosure(JOB237)
    assert findings, "the JOB-237 TN-visa sentence must produce a disclosure finding"
    assert all(f["severity"] == "BLOCK" for f in findings)
    cats = {f["category"] for f in findings}
    assert "visa" in cats        # "on a TN visa" / "qualify under TN"
    assert "citizenship" in cats  # "I am a Canadian citizen"
    # the offending span is named so the iterate loop can remove it
    assert any("TN" in f["offending_text"] for f in findings)


@pytest.mark.parametrize("text,expect_cat", [
    ("I am on a TN visa.", "visa"),
    ("I am currently on an H-1B visa with my employer.", "visa"),
    ("I am authorized under my L-1 status.", "visa"),
    ("I am on OPT and looking for full-time roles.", "visa"),
    ("I am a Canadian citizen.", "citizenship"),
    ("I am a citizen of Canada.", "citizenship"),
    ("I am not a US citizen.", "citizenship"),
    ("I would require visa sponsorship.", "sponsorship"),
    ("I will need sponsorship to work in the US.", "sponsorship"),
    ("My green card is in progress.", "green_card"),
    ("I am a lawful permanent resident.", "green_card"),
    ("My status is adjustment of status pending.", "green_card"),
    ("I am pursuing a green card through marriage.", "marriage_gc"),
    ("My pathway is a marriage-based green card.", "marriage_gc"),
])
def test_each_disclosure_blocks(text, expect_cat):
    findings = detect_immigration_disclosure(text)
    assert findings, f"expected a disclosure finding for: {text!r}"
    assert all(f["severity"] == "BLOCK" for f in findings)
    assert expect_cat in {f["category"] for f in findings}


def test_finding_shape_matches_contract():
    f = detect_immigration_disclosure("I am on a TN visa.")[0]
    assert f["lens"] == "disclosure"
    assert f["severity"] == "BLOCK"
    assert "immigration/work-auth status" in f["issue"]
    assert "REMOVE" in f["fix"]
    assert f["offending_text"]  # the matched span


# ---- 1b. detect_immigration_disclosure: precision (must NOT fire) -----------------------------

@pytest.mark.parametrize("text", [
    "I led a citizen science project that engaged thousands of volunteers.",
    "I built an API that only authorized users of the API can call.",
    "I helped sponsor the event and recruit attendees.",
    "I designed a green dashboard with live status indicators.",
    "I believe in being a good corporate citizen.",
    # a clean, professional AI-native essay — no immigration content
    ("I architected an AI orchestration system at Meridian that shipped five production tools. "
     "I own the full loop from design to operation, and I am drawn to roles where I can build "
     "agentic systems end to end."),
    "",
    "   ",
])
def test_near_misses_and_clean_essay_no_finding(text):
    assert detect_immigration_disclosure(text) == []


# ---- 2. gate integration: audit_answers (essay) surfaces a BLOCK finding ----------------------

def _drafts(*answers):
    return [{"question": f"Q{i}", "answer": a, "kind": "essay"} for i, a in enumerate(answers)]


def test_disclosure_in_essay_answer_blocks_via_gate_path():
    # No fabrication (judge returns []), but the disclosure lens must still BLOCK.
    out = audit_answers(_drafts(JOB237),
                        gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER")
    assert out["verdict"] == "BLOCKED"
    assert out["block_findings"] >= 1
    disc = [f for f in out["findings"] if f.get("lens") == "disclosure"]
    assert disc, "a disclosure BLOCK finding must surface from audit_answers"
    assert all(f["severity"] == "BLOCK" for f in disc)
    assert all(f["doc"] == "essay_answer" for f in disc)
    # the finding carries the question so the converge loop can route the fix to the right answer
    assert all("question" in f for f in disc)


def test_clean_essay_answer_unaffected_by_disclosure_lens():
    clean = ("I architected an AI orchestration system at Meridian that shipped five production "
             "tools. I am drawn to building agentic systems end to end.")
    out = audit_answers(_drafts(clean),
                        gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER")
    assert out["verdict"] == "PASS"
    assert out["findings"] == []


# ---- 2b. gate integration: audit_content_text (cover paragraph) -------------------------------

def test_disclosure_in_cover_paragraph_blocks():
    out = audit_content_text(JOB237, "para.2",
                             gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER")
    disc = [f for f in out if f.get("lens") == "disclosure"]
    assert disc, "a disclosure BLOCK finding must surface for a cover paragraph"
    assert all(f["severity"] == "BLOCK" for f in disc)
    assert all(f["doc"] == "cover" for f in disc)


def test_disclosure_in_resume_bullet_blocks():
    out = audit_content_text("I am on a TN visa and authorized to work.", "current_bullets.0",
                             gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER")
    disc = [f for f in out if f.get("lens") == "disclosure"]
    assert disc
    assert all(f["doc"] == "resume" for f in disc)


def test_clean_content_unaffected():
    out = audit_content_text("cut analysis time with simulation-led design", "current_bullets.0",
                             gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER")
    assert out == []


# ---- 3. verify_ready: a record carrying a disclosure is NOT ready -----------------------------

def test_verify_ready_blocks_on_disclosure_finding():
    from apply_engine import finish

    # The disclosure surfaces as a BLOCK finding counted in block_findings — verify_ready's
    # _fab_calib_block_count makes the card NOT ready regardless of the other gates.
    audit = audit_answers(_drafts(JOB237),
                          gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER")
    assert audit["block_findings"] >= 1
    record = {
        "job_id": "JOB-237",
        "audit": audit,
        "status": "ready_to_submit",
    }
    ready, reason = finish.verify_ready(record, finish_config_stub())
    assert ready is False
    # the reason names an outstanding BLOCK finding (verify_submittable/can_submit/block-count chain)
    assert reason


def finish_config_stub():
    """A minimal stand-in for the `config` arg verify_ready takes. verify_ready only reaches the
    G-hooks (which pass-when-absent) after the block-count check, and our record fails the
    block-count check first, so the config is never dereferenced for paths we exercise."""
    from apply_engine import config
    return config


# ---- 4. iterative removal: the disclosure finding threads as a REMOVAL instruction ------------

def test_disclosure_finding_threads_into_feedback_clause_as_removal():
    finding = detect_immigration_disclosure("I am on a TN visa.")[0]
    clause = iterate_fix.feedback_clause(
        prev_attempt_text="I am on a TN visa and excited about the role.",
        blocks=[],
        findings=[finding],
        ledger_facts="Sam ships AI-native engineering at Meridian.",
    )
    assert clause, "a disclosure finding must produce a non-empty retry feedback clause"
    # the offending visa sentence is named so the rewrite knows exactly what to drop
    assert "TN visa" in clause
    # converge-by-REMOVAL wording
    assert "REMOVE or REWORD" in clause
    # the fix instruction (remove the immigration sentence) rides along via summarize_blocks
    assert "REMOVE" in clause
