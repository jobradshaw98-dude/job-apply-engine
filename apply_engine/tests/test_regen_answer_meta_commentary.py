# -*- coding: utf-8 -*-
"""Bug #6 (live JOB-246, 2026-06-13): the answer regenerator sometimes stores META-COMMENTARY
ABOUT the answer instead of the answer itself, corrupting the stored value.

When an --instruction edit is effectively a NO-OP (the thing to change isn't present), the model
returns commentary like "'Over the past year' doesn't appear in the current answer. The ARIA
sentence reads: > '...'. No edit needed." — and the single-pass landing path stored that
commentary as status=drafted, REPLACING the real answer. A corrupted answer then flowed
downstream.

FIX under test: a deterministic meta-commentary GUARD in regen_answer's landing path detects
commentary-about-the-answer (vs an answer) via tight heuristics, re-prompts ONCE with a stronger
instruction, and — if the re-prompt STILL trips the guard — KEEPS THE PRIOR answer (never
overwrites with commentary) and flags it visibly (status=needs_input + a regen_produced_commentary
note) instead of silently corrupting.

No claude CLI runs: make_claude_llm / make_audit_fn / load_facts are monkeypatched; the ledger read
is redirected at config.PKG_DIR.
"""
import json

from apply_engine import config
from apply_engine import regen_answer


REAL_ORIGINAL = (
    "I built ARIA, a personal multi-agent system on Claude Code that runs my finance, career, and "
    "lead-gen workflows. It taught me to design agent guardrails and ship autonomously."
)


def _seed(tmp_path, value=REAL_ORIGINAL):
    apps = [
        {
            "job_id": "JOB-246", "company": "TestCo", "role": "Engineer",
            "custom_qs": [
                {"q": "Why are you interested in this role?", "kind": "essay",
                 "status": "drafted", "value": value, "reason": "",
                 "review_findings": [], "edit_request": "drop the phrase 'over the past year'"},
            ],
        },
        {"job_id": "JOB-OTHER", "note": "must survive untouched"},
    ]
    mp = tmp_path / "staged_applications.json"
    mp.write_text(json.dumps(apps, indent=2), encoding="utf-8")
    (tmp_path / "jobs.json").write_text(json.dumps([{"id": "JOB-246"}]), encoding="utf-8")
    return mp


def _q(mp):
    data = json.loads(mp.read_text(encoding="utf-8"))
    app = next(a for a in data if a.get("job_id") == "JOB-246")
    return app["custom_qs"][0], data


def _wire_pkgdir(tmp_path, monkeypatch):
    pkg = tmp_path / "career" / "apply_engine"
    pkg.mkdir(parents=True)
    (pkg.parent / "claims_ledger.md").write_text(
        "Sam built ARIA, a personal multi-agent system on Claude Code.", encoding="utf-8")
    monkeypatch.setattr(config, "PKG_DIR", pkg)


def _wire(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")
    _wire_pkgdir(tmp_path, monkeypatch)
    monkeypatch.setattr(regen_answer, "load_facts", lambda job=None, **k: "FACTS")
    # The gate never blocks in these tests — we are isolating the meta-commentary guard, which sits
    # BEFORE the fabrication gate. (A real meta-commentary string is gate-clean: it fabricates
    # nothing, it just isn't an answer.)
    monkeypatch.setattr(regen_answer, "make_audit_fn", lambda *a, **k: (lambda t: []))


# A representative meta-commentary blob like the live JOB-246 corruption.
META = (
    "'Over the past year' doesn't appear in the current answer. The ARIA sentence reads:\n"
    "> I built ARIA, a personal multi-agent system on Claude Code.\n"
    "No edit needed."
)

# A clean, real answer (what a good rewrite produces).
CLEAN = (
    "I built ARIA, a personal multi-agent system on Claude Code that runs my finance and career "
    "workflows. It taught me to design agent guardrails and ship autonomously."
)


def test_meta_commentary_blocked_prior_answer_preserved(tmp_path, monkeypatch):
    """(a) The model returns meta-commentary on BOTH the first attempt and the re-prompt → the guard
    fires, the prior answer is PRESERVED (never overwritten with commentary), and the record is
    flagged so it's visible, not silent. The commentary text is NEVER stored as the value."""
    _wire(tmp_path, monkeypatch)
    mp = _seed(tmp_path)

    calls = {"n": 0, "prompts": []}

    def _factory(*a, **k):
        def _fn(prompt):
            if "Return ONLY a JSON array" in prompt:   # ledger self-audit
                return "[]"
            calls["n"] += 1
            calls["prompts"].append(prompt)
            return META   # always commentary, even after the stronger re-prompt
        return _fn
    monkeypatch.setattr(regen_answer, "make_claude_llm", _factory)

    rc = regen_answer.main([
        "JOB-246", "--question", "Why are you interested in this role?",
        "--instruction", "drop the phrase 'over the past year'"])
    assert rc == 0  # terminal-but-clean: a flagged outcome is a normal terminal state

    q, data = _q(mp)
    # PRIOR answer preserved — the commentary must NOT have landed.
    assert q["value"] == REAL_ORIGINAL
    assert META not in q["value"]
    assert "No edit needed" not in q["value"]
    # Flagged for the user, not silently "drafted".
    assert q["status"] == "needs_input"
    assert "commentary" in (q.get("reason", "") or "").lower()
    # Re-prompted exactly once (two generation attempts), and the re-prompt carried a stronger
    # "output ONLY the answer" instruction.
    assert calls["n"] == 2
    assert any("commentary" in p.lower() for p in calls["prompts"][1:])
    # Sibling job untouched.
    assert any(a.get("job_id") == "JOB-OTHER" for a in data)
    # History row records the commentary outcome (visible), NOT as a clean 'edited' landing.
    h = q["edit_history"][-1]
    assert h["status"] != "edited"


def test_clean_answer_lands_normally(tmp_path, monkeypatch):
    """(b) The model returns a real answer → it lands normally (status=drafted), guard does not fire."""
    _wire(tmp_path, monkeypatch)
    mp = _seed(tmp_path)

    calls = {"n": 0}

    def _factory(*a, **k):
        def _fn(prompt):
            if "Return ONLY a JSON array" in prompt:
                return "[]"
            calls["n"] += 1
            return CLEAN
        return _fn
    monkeypatch.setattr(regen_answer, "make_claude_llm", _factory)

    rc = regen_answer.main([
        "JOB-246", "--question", "Why are you interested in this role?",
        "--instruction", "drop the phrase 'over the past year'"])
    assert rc == 0
    q, _ = _q(mp)
    assert q["value"] == CLEAN
    assert q["status"] == "drafted"
    assert calls["n"] == 1   # no re-prompt needed
    assert q["edit_history"][-1]["status"] == "edited"


def test_meta_then_real_on_reprompt_lands(tmp_path, monkeypatch):
    """(c) The model returns meta-commentary first, then a real answer on the re-prompt → the real
    answer lands (status=drafted), prior answer is replaced by the GOOD rewrite."""
    _wire(tmp_path, monkeypatch)
    mp = _seed(tmp_path)

    gen = {"n": 0}

    def _factory(*a, **k):
        def _fn(prompt):
            if "Return ONLY a JSON array" in prompt:
                return "[]"
            gen["n"] += 1
            return META if gen["n"] == 1 else CLEAN
        return _fn
    monkeypatch.setattr(regen_answer, "make_claude_llm", _factory)

    rc = regen_answer.main([
        "JOB-246", "--question", "Why are you interested in this role?",
        "--instruction", "drop the phrase 'over the past year'"])
    assert rc == 0
    q, _ = _q(mp)
    assert q["value"] == CLEAN
    assert q["status"] == "drafted"
    assert gen["n"] == 2   # one re-prompt
    assert q["edit_history"][-1]["status"] == "edited"


def test_real_answer_with_change_word_or_quote_not_flagged(tmp_path, monkeypatch):
    """(d) FALSE-POSITIVE guard: a legitimate answer that merely CONTAINS the word 'change' or a
    short quotation must NOT be flagged as meta-commentary. The heuristics are scoped to
    opening/structural signals + explicit no-op phrases, not stray words."""
    _wire(tmp_path, monkeypatch)
    mp = _seed(tmp_path)

    legit = (
        'I am drawn to this role because I want to drive change in how applied-AI teams ship. At '
        'Meridian I led a project the team described as "a step change in turnaround," cutting '
        'analysis time with simulation. I would bring that same bias to action here.'
    )

    gen = {"n": 0}

    def _factory(*a, **k):
        def _fn(prompt):
            if "Return ONLY a JSON array" in prompt:
                return "[]"
            gen["n"] += 1
            return legit
        return _fn
    monkeypatch.setattr(regen_answer, "make_claude_llm", _factory)

    rc = regen_answer.main([
        "JOB-246", "--question", "Why are you interested in this role?",
        "--instruction", "mention change leadership"])
    assert rc == 0
    q, _ = _q(mp)
    assert q["value"] == legit       # landed, not flagged
    assert q["status"] == "drafted"
    assert gen["n"] == 1             # no re-prompt — guard never fired
    assert q["edit_history"][-1]["status"] == "edited"
