# -*- coding: utf-8 -*-
"""FIX 1 — stale-finding pruning after a dashboard 'Apply this fix'.

When regen_answer completes an --instruction edit successfully (status drafted), it must prune
from app['audit']['findings'] any finding for THIS question whose offending_text is gone from the
new answer, and flip the stored verdict BLOCKED->PASS once findings are empty and gate_blocks==0.
This makes the dashboard reflect reality after each fix without a full re-audit.

Proven here:
  1. A finding is pruned when its offending text no longer appears in the new answer.
  2. A finding is KEPT when its offending text still appears in the new answer.
  3. Verdict flips to PASS only when findings become empty AND gate_blocks == 0.
  4. Verdict stays BLOCKED when findings are empty but gate_blocks > 0.
  5. A finding for a DIFFERENT question is never touched.

No claude CLI runs: make_claude_llm / make_audit_fn / load_facts are monkeypatched to a stub
LLM that returns a fixed new answer (and [] for the JSON self-audit prompt) and a no-block gate.
"""
import json

from apply_engine import config
from apply_engine import regen_answer


def _seed(tmp_path, audit, value="Old answer with the BAD PHRASE inside.",
          question="Why do you want to work here?"):
    apps = [
        {
            "job_id": "JOB-900",
            "company": "TestCo",
            "role": "Engineer",
            "audit": audit,
            "custom_qs": [
                {"q": question, "kind": "essay", "status": "drafted",
                 "value": value, "reason": ""},
                {"q": "Tell us about a project.", "kind": "essay", "status": "drafted",
                 "value": "An unrelated drafted answer.", "reason": ""},
            ],
        },
        {"job_id": "JOB-OTHER", "company": "StubCo", "note": "must survive untouched"},
    ]
    mp = tmp_path / "staged_applications.json"
    mp.write_text(json.dumps(apps, indent=2), encoding="utf-8")
    (tmp_path / "jobs.json").write_text(json.dumps([{"id": "JOB-900"}]), encoding="utf-8")
    return mp


def _wire(tmp_path, monkeypatch, new_answer):
    """Wire config + a stub LLM that returns `new_answer` for the rewrite and '[]' for the
    judgment self-audit (the prompt asking for a JSON array). No-block deterministic gate."""
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")
    monkeypatch.setattr(regen_answer, "load_facts", lambda job=None, **k: "FACTS")

    def _factory(*a, **k):
        def _fn(prompt):
            if "Return ONLY a JSON array" in prompt:
                return "[]"
            return new_answer
        return _fn
    monkeypatch.setattr(regen_answer, "make_claude_llm", _factory)
    monkeypatch.setattr(regen_answer, "make_audit_fn", lambda *a, **k: (lambda t: []))


def _read(mp):
    return json.loads(mp.read_text(encoding="utf-8"))


def _app(mp):
    return next(a for a in _read(mp) if a.get("job_id") == "JOB-900")


# ── 1 + 3. offending text gone -> finding pruned -> verdict flips to PASS ──────

def test_finding_pruned_when_offending_text_gone_and_verdict_flips(tmp_path, monkeypatch):
    audit = {
        "verdict": "BLOCKED",
        "gate_blocks": 0,
        "findings": [
            {"doc": "essay_answer", "question": "Why do you want to work here?",
             "severity": "BLOCK", "offending_text": "the BAD PHRASE",
             "issue": "unsupported", "fix": "remove it"},
        ],
        "summary": "1 unresolved finding",
    }
    mp = _seed(tmp_path, audit)
    # New answer no longer contains "the BAD PHRASE".
    _wire(tmp_path, monkeypatch, "A clean answer with no offending wording at all.")

    rc = regen_answer.main([
        "JOB-900", "--question", "Why do you want to work here?",
        "--instruction", "Apply this accuracy correction and change nothing else.",
    ])
    assert rc == 0
    a = _app(mp)
    assert a["audit"]["findings"] == []
    assert a["audit"]["verdict"] == "PASS"
    assert "addressed via dashboard fixes" in a["audit"]["summary"]


# ── 2. offending text still present -> finding KEPT, verdict stays BLOCKED ─────

def test_finding_kept_when_offending_text_still_present(tmp_path, monkeypatch):
    audit = {
        "verdict": "BLOCKED",
        "gate_blocks": 0,
        "findings": [
            {"doc": "essay_answer", "question": "Why do you want to work here?",
             "severity": "BLOCK", "offending_text": "the BAD PHRASE",
             "issue": "unsupported", "fix": "remove it"},
        ],
        "summary": "1 unresolved finding",
    }
    mp = _seed(tmp_path, audit)
    # The rewrite STILL contains "the BAD PHRASE" (fix didn't actually land).
    _wire(tmp_path, monkeypatch, "A revised answer that still has the BAD PHRASE in it.")

    rc = regen_answer.main([
        "JOB-900", "--question", "Why do you want to work here?",
        "--instruction", "Apply this accuracy correction and change nothing else.",
    ])
    assert rc == 0
    a = _app(mp)
    assert len(a["audit"]["findings"]) == 1
    assert a["audit"]["verdict"] == "BLOCKED"


# ── 4. findings empty but gate_blocks > 0 -> verdict stays BLOCKED ─────────────

def test_verdict_stays_blocked_when_gate_blocks_outstanding(tmp_path, monkeypatch):
    audit = {
        "verdict": "BLOCKED",
        "gate_blocks": 1,   # a deterministic gate block is still outstanding
        "findings": [
            {"doc": "essay_answer", "question": "Why do you want to work here?",
             "severity": "BLOCK", "offending_text": "the BAD PHRASE",
             "issue": "unsupported", "fix": "remove it"},
        ],
        "summary": "blocked",
    }
    mp = _seed(tmp_path, audit)
    _wire(tmp_path, monkeypatch, "A clean answer with no offending wording at all.")

    rc = regen_answer.main([
        "JOB-900", "--question", "Why do you want to work here?",
        "--instruction", "Fix it.",
    ])
    assert rc == 0
    a = _app(mp)
    # The judgment finding pruned, but gate_blocks>0 keeps the verdict BLOCKED.
    assert a["audit"]["findings"] == []
    assert a["audit"]["verdict"] == "BLOCKED"


# ── 6. two-severity alignment: a remaining FLAG finding does NOT block PASS ────

def test_verdict_flips_to_pass_when_only_flag_findings_remain(tmp_path, monkeypatch):
    # After pruning the BLOCK finding for this question, a FLAG finding (for a different
    # question) remains. Under the two-severity policy the verdict must still flip to PASS:
    # PASS recompute keys on "no BLOCK-severity findings remain", not "findings empty".
    audit = {
        "verdict": "BLOCKED",
        "gate_blocks": 0,
        "findings": [
            {"doc": "essay_answer", "question": "Why do you want to work here?",
             "severity": "BLOCK", "offending_text": "the BAD PHRASE",
             "issue": "fabrication", "fix": "remove it"},
            {"doc": "essay_answer", "question": "Tell us about a project.",
             "severity": "FLAG", "offending_text": "a touch enthusiastic",
             "issue": "tone", "fix": "tighten"},
        ],
        "summary": "1 block, 1 flag",
    }
    mp = _seed(tmp_path, audit)
    _wire(tmp_path, monkeypatch, "A clean answer with no offending wording at all.")

    rc = regen_answer.main([
        "JOB-900", "--question", "Why do you want to work here?",
        "--instruction", "Fix it.",
    ])
    assert rc == 0
    a = _app(mp)
    remaining = a["audit"]["findings"]
    # The BLOCK finding is pruned; the FLAG finding rides along.
    assert len(remaining) == 1
    assert remaining[0]["severity"] == "FLAG"
    # No BLOCK-severity finding remains and gate_blocks == 0 -> PASS.
    assert a["audit"]["verdict"] == "PASS"
    assert "style flag" in a["audit"]["summary"].lower()


# ── 5. a finding for a DIFFERENT question is left alone ────────────────────────

def test_other_questions_finding_untouched(tmp_path, monkeypatch):
    audit = {
        "verdict": "BLOCKED",
        "gate_blocks": 0,
        "findings": [
            {"doc": "essay_answer", "question": "Why do you want to work here?",
             "severity": "BLOCK", "offending_text": "the BAD PHRASE",
             "issue": "unsupported", "fix": "remove it"},
            {"doc": "essay_answer", "question": "Tell us about a project.",
             "severity": "BLOCK", "offending_text": "some other claim",
             "issue": "unsupported", "fix": "remove it"},
        ],
        "summary": "2 unresolved findings",
    }
    mp = _seed(tmp_path, audit)
    _wire(tmp_path, monkeypatch, "A clean answer with no offending wording at all.")

    rc = regen_answer.main([
        "JOB-900", "--question", "Why do you want to work here?",
        "--instruction", "Fix it.",
    ])
    assert rc == 0
    a = _app(mp)
    # The edited question's finding is gone; the other question's finding remains.
    remaining = a["audit"]["findings"]
    assert len(remaining) == 1
    assert remaining[0]["question"] == "Tell us about a project."
    # Findings not empty -> verdict stays BLOCKED.
    assert a["audit"]["verdict"] == "BLOCKED"
