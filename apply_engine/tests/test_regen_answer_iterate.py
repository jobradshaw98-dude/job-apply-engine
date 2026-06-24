# -*- coding: utf-8 -*-
"""Iterate-to-clean tests for apply_engine.regen_answer (--max-attempts N).

These prove the upgrade that turns the one-shot regen into a rewrite→re-gate→rewrite loop:
  1. Iterates WITH FEEDBACK — an injected llm whose attempt-1 output fails the gate and attempt-2
     passes converges, and the attempt-2 PROMPT carries the attempt-1 finding's feedback.
  2. Bounded — an llm that NEVER grounds returns a classified residual after K attempts, writes no
     passing answer, and never loops past K.
  3. Classification — a user-only-fact finding -> human_only; a premise-unsupportable -> unsupportable.
  4. Backward-compat — max_attempts=1 is exactly one regen (today's behaviour); a user --instruction
     edit that fabricates is still BLOCKED (original kept), NOT auto-iterated.

No claude CLI runs: make_claude_llm / make_audit_fn / load_facts are monkeypatched; the ledger read
is redirected at config.PKG_DIR so iterate_fix's ledger-facts read is deterministic.
"""
import json

from apply_engine import config
from apply_engine import regen_answer


def _seed(tmp_path, value="The good original answer."):
    apps = [
        {
            "job_id": "JOB-900", "company": "TestCo", "role": "Engineer",
            "custom_qs": [
                {"q": "Tell us about your wear analysis.", "kind": "essay",
                 "status": "drafted", "value": value, "reason": "",
                 "review_findings": [], "edit_request": "reframe to optimization"},
            ],
        },
        {"job_id": "JOB-OTHER", "note": "must survive untouched"},
    ]
    mp = tmp_path / "staged_applications.json"
    mp.write_text(json.dumps(apps, indent=2), encoding="utf-8")
    (tmp_path / "jobs.json").write_text(json.dumps([{"id": "JOB-900"}]), encoding="utf-8")
    return mp


def _q(mp):
    data = json.loads(mp.read_text(encoding="utf-8"))
    app = next(a for a in data if a.get("job_id") == "JOB-900")
    return app["custom_qs"][0], data


def _wire_pkgdir(tmp_path, monkeypatch):
    """Redirect config.PKG_DIR to a tmp pkg so PKG_DIR.parent/claims_ledger.md is ours."""
    pkg = tmp_path / "career" / "apply_engine"
    pkg.mkdir(parents=True)
    (pkg.parent / "claims_ledger.md").write_text(
        "Sam optimized a surface-texture design using FEA contact-stress modelling.",
        encoding="utf-8")
    monkeypatch.setattr(config, "PKG_DIR", pkg)


def test_iterates_with_feedback_attempt2_passes(tmp_path, monkeypatch):
    """attempt-1 output trips the gate; attempt-2 passes -> converges with max_attempts=3, and the
    attempt-2 PROMPT contains the attempt-1 finding's feedback (the clause was threaded in)."""
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")
    _wire_pkgdir(tmp_path, monkeypatch)
    mp = _seed(tmp_path)
    monkeypatch.setattr(regen_answer, "load_facts", lambda job=None, **k: "FACTS")

    prompts = []
    gen = {"n": 0}

    def _factory(*a, **k):
        def _fn(prompt):
            # self-audit calls (JSON-array honesty trace) always return [] so they don't add findings.
            if "Return ONLY a JSON array" in prompt:
                return "[]"
            # classification call (only on exhaustion) — never reached here.
            if '"class"' in prompt:
                return '{"class":"unsupportable","why":"x"}'
            prompts.append(prompt)
            gen["n"] += 1
            # attempt 1: a draft that fabricates "wear"; attempt 2: a clean reframe.
            return ("Reduced part wear by 30 percent." if gen["n"] == 1
                    else "Reframed the work as a contact-stress optimization study.")
        return _fn
    monkeypatch.setattr(regen_answer, "make_claude_llm", _factory)

    # Gate BLOCKS any text containing "wear" (the JOB-237-style fabrication), passes otherwise.
    def _audit_factory(*a, **k):
        return lambda t: (["fabricated wear claim"] if "wear" in t.lower() else [])
    monkeypatch.setattr(regen_answer, "make_audit_fn", _audit_factory)

    rc = regen_answer.main([
        "JOB-900", "--question", "Tell us about your wear analysis.",
        "--instruction", "reframe to optimization", "--max-attempts", "3",
    ])
    assert rc == 0
    q, _ = _q(mp)
    # The clean attempt-2 text LANDED.
    assert q["value"] == "Reframed the work as a contact-stress optimization study."
    assert q["status"] == "drafted"
    # Two generation attempts only (converged at 2).
    assert gen["n"] == 2
    # The attempt-2 prompt carries the feedback clause naming the attempt-1 rejection.
    assert any("PREVIOUS ATTEMPT WAS REJECTED" in p for p in prompts)
    assert any("fabricated wear claim" in p for p in prompts)
    # No residual on a clean converge.
    assert "residual" not in q


def test_bounded_never_grounds_returns_residual(tmp_path, monkeypatch):
    """An llm that ALWAYS emits a gate-tripping draft -> after K attempts a classified residual,
    no passing answer written (original kept), and exactly K generation attempts (no loop past K)."""
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")
    _wire_pkgdir(tmp_path, monkeypatch)
    mp = _seed(tmp_path, value="ORIGINAL kept answer.")
    monkeypatch.setattr(regen_answer, "load_facts", lambda job=None, **k: "FACTS")

    gen = {"n": 0}

    def _factory(*a, **k):
        def _fn(prompt):
            if "Return ONLY a JSON array" in prompt:
                return "[]"
            if '"class"' in prompt:
                return '{"class":"unsupportable","why":"premise not grounded"}'
            gen["n"] += 1
            return "Still claims wear reduction."   # always trips the gate
        return _fn
    monkeypatch.setattr(regen_answer, "make_claude_llm", _factory)
    monkeypatch.setattr(regen_answer, "make_audit_fn",
                        lambda *a, **k: (lambda t: ["fabricated wear claim"]))

    rc = regen_answer.main([
        "JOB-900", "--question", "Tell us about your wear analysis.",
        "--instruction", "reframe", "--max-attempts", "3",
    ])
    assert rc == 0  # terminal-but-clean (a blocked outcome is a normal terminal state)
    q, _ = _q(mp)
    # Original answer kept (no passing draft written).
    assert q["value"] == "ORIGINAL kept answer."
    # Exactly K generation attempts — never past the cap.
    assert gen["n"] == 3
    # Classified residual stamped on the answer + the latest history row.
    assert q["residual"]["class"] == "unsupportable"
    assert q["residual"]["attempts"] == 3
    h = q["edit_history"][-1]
    assert h["status"] == "blocked"
    assert h["residual"]["class"] == "unsupportable"


def test_classification_human_only(tmp_path, monkeypatch):
    """A residual the classifier calls human_only is stamped human_only (asks the user)."""
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")
    _wire_pkgdir(tmp_path, monkeypatch)
    mp = _seed(tmp_path)
    monkeypatch.setattr(regen_answer, "load_facts", lambda job=None, **k: "FACTS")

    def _factory(*a, **k):
        def _fn(prompt):
            if '"class"' in prompt:           # classification call
                return '{"class":"human_only","why":"only Sam can confirm this experience"}'
            if "Return ONLY a JSON array" in prompt:   # self-audit -> one structured finding
                return ('[{"offending_text":"led a team of 12","issue":"team size not in ledger",'
                        '"fix":"confirm with the user"}]')
            return "I led a team of 12 on the project."   # always blocked
        return _fn
    monkeypatch.setattr(regen_answer, "make_claude_llm", _factory)
    monkeypatch.setattr(regen_answer, "make_audit_fn",
                        lambda *a, **k: (lambda t: ["unsupported team-size claim"]))

    rc = regen_answer.main([
        "JOB-900", "--question", "Tell us about your wear analysis.",
        "--instruction", "add leadership", "--max-attempts", "3",
    ])
    assert rc == 0
    q, _ = _q(mp)
    assert q["residual"]["class"] == "human_only"


def test_backward_compat_max_attempts_1_single_pass(tmp_path, monkeypatch):
    """max_attempts=1 -> exactly ONE generation, a fabricating edit is BLOCKED (original kept), and
    NO residual is stamped (today's single-pass behaviour, untouched). This is the user-edit path."""
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")
    _wire_pkgdir(tmp_path, monkeypatch)
    mp = _seed(tmp_path, value="ORIGINAL kept answer.")
    monkeypatch.setattr(regen_answer, "load_facts", lambda job=None, **k: "FACTS")

    gen = {"n": 0}

    def _factory(*a, **k):
        def _fn(prompt):
            if "Return ONLY a JSON array" in prompt:
                return "[]"
            assert '"class"' not in prompt, "no classification call on single-pass"
            gen["n"] += 1
            return "Claims wear reduction."   # trips the gate
        return _fn
    monkeypatch.setattr(regen_answer, "make_claude_llm", _factory)
    monkeypatch.setattr(regen_answer, "make_audit_fn",
                        lambda *a, **k: (lambda t: ["fabricated wear claim"]))

    # Default max_attempts is 1 (omit the flag entirely — the dashboard/user path).
    rc = regen_answer.main([
        "JOB-900", "--question", "Tell us about your wear analysis.",
        "--instruction", "reframe",
    ])
    assert rc == 0
    q, _ = _q(mp)
    assert gen["n"] == 1                       # single pass, no retry
    assert q["value"] == "ORIGINAL kept answer."   # blocked -> original kept
    assert "residual" not in q                  # no residual on single-pass
    h = q["edit_history"][-1]
    assert h["status"] == "blocked"
    assert "residual" not in h
