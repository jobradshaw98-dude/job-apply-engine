# -*- coding: utf-8 -*-
"""regen_answer's G2 length re-check (--min-words / --max-words), the engine-own length-fix path.

When a length target is passed, an attempt that PASSES the fabrication/disclosure gate is ALSO
checked against the stated word range; an out-of-range answer is re-prompted (with a length feedback
clause) within the --max-attempts budget. The hard floor stays first: a lengthened answer that
introduces a fabrication is blocked by the gate BEFORE length is ever considered.

No claude CLI runs: make_claude_llm / make_audit_fn / load_facts are monkeypatched; the ledger read
is redirected at config.PKG_DIR.
"""
import json

from apply_engine import config
from apply_engine import regen_answer


def _seed(tmp_path, value="too short"):
    apps = [{
        "job_id": "JOB-237", "company": "Anthropic", "role": "Applied AI Engineer",
        "custom_qs": [{"q": "Why Anthropic?", "kind": "essay", "status": "drafted",
                       "value": value, "reason": "", "review_findings": [],
                       "edit_request": "lengthen"}],
    }]
    mp = tmp_path / "staged_applications.json"
    mp.write_text(json.dumps(apps, indent=2), encoding="utf-8")
    (tmp_path / "jobs.json").write_text(json.dumps([{"id": "JOB-237"}]), encoding="utf-8")
    return mp


def _q(mp):
    data = json.loads(mp.read_text(encoding="utf-8"))
    app = next(a for a in data if a.get("job_id") == "JOB-237")
    return app["custom_qs"][0]


def _wire(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")
    pkg = tmp_path / "career" / "apply_engine"
    pkg.mkdir(parents=True)
    (pkg.parent / "claims_ledger.md").write_text(
        "Sam ships AI-native engineering systems (Meridian DevBot, ARIA).", encoding="utf-8")
    monkeypatch.setattr(config, "PKG_DIR", pkg)
    monkeypatch.setattr(regen_answer, "load_facts", lambda job=None, **k: "FACTS")


def _clean_gate(monkeypatch):
    """Fabrication/disclosure gate that passes EVERYTHING (so only length can gate)."""
    monkeypatch.setattr(regen_answer, "make_audit_fn", lambda *a, **k: (lambda t: []))


def test_under_length_retries_then_lands_in_range(tmp_path, monkeypatch):
    """attempt-1 is gate-clean but too short (10 words < 200); attempt-2 reaches the band -> lands.
    The attempt-2 prompt carries the length feedback clause naming the count + the target."""
    _wire(tmp_path, monkeypatch)
    _clean_gate(monkeypatch)
    mp = _seed(tmp_path)
    prompts = []
    gen = {"n": 0}

    short = "word " * 10           # 10 words — under 200
    inrange = "word " * 250        # 250 words — in 200-400

    def _factory(*a, **k):
        def _fn(prompt):
            if "Return ONLY a JSON array" in prompt:
                return "[]"
            prompts.append(prompt)
            gen["n"] += 1
            return short if gen["n"] == 1 else inrange
        return _fn
    monkeypatch.setattr(regen_answer, "make_claude_llm", _factory)

    rc = regen_answer.main([
        "JOB-237", "--question", "Why Anthropic?", "--instruction", "lengthen",
        "--max-attempts", "3", "--min-words", "200", "--max-words", "400",
    ])
    assert rc == 0
    q = _q(mp)
    # regen strips the landed value; compare against the stripped in-range draft
    assert q["value"] == inrange.strip() and q["status"] == "drafted"
    assert gen["n"] == 2  # converged at the in-range attempt
    assert "residual" not in q
    # the retry prompt named the wrong length + the band
    assert any("WRONG LENGTH" in p for p in prompts)
    assert any("at least 200" in p for p in prompts)


def test_never_reaches_range_stamps_length_unmet(tmp_path, monkeypatch):
    """A gate-clean answer that STAYS too short after K attempts -> residual length_unmet (NOT
    human_only / unsupportable). The best gate-clean draft still lands (the answer improves)."""
    _wire(tmp_path, monkeypatch)
    _clean_gate(monkeypatch)
    mp = _seed(tmp_path)
    gen = {"n": 0}

    def _factory(*a, **k):
        def _fn(prompt):
            if "Return ONLY a JSON array" in prompt:
                return "[]"
            if '"class"' in prompt:
                raise AssertionError("length residual must not call the residual classifier")
            gen["n"] += 1
            return "word " * 50   # 50 words — always under 200
        return _fn
    monkeypatch.setattr(regen_answer, "make_claude_llm", _factory)

    rc = regen_answer.main([
        "JOB-237", "--question", "Why Anthropic?", "--instruction", "lengthen",
        "--max-attempts", "3", "--min-words", "200", "--max-words", "400",
    ])
    assert rc == 0
    q = _q(mp)
    assert gen["n"] == 3                      # used the whole budget
    assert q["residual"]["class"] == "length_unmet"
    assert q["residual"]["attempts"] == 3
    # the gate-clean (if short) draft landed so the answer still improves
    assert q["value"] == ("word " * 50).strip()
    assert q["status"] == "drafted"


def test_fabrication_while_lengthening_is_blocked_before_length(tmp_path, monkeypatch):
    """The hard floor: a lengthen attempt that introduces a fabrication is rejected by the gate and
    re-prompted; length is only checked on a gate-CLEAN draft. attempt-1 fabricates (blocked),
    attempt-2 is clean AND in range -> lands. Proves length never overrides the truth gate."""
    _wire(tmp_path, monkeypatch)
    mp = _seed(tmp_path)
    gen = {"n": 0}

    fab_long = "led a team of 50 " * 60      # long but fabricated ("team of 50")
    clean_inrange = "word " * 250            # clean + in range

    def _factory(*a, **k):
        def _fn(prompt):
            if "Return ONLY a JSON array" in prompt:
                return "[]"
            gen["n"] += 1
            return fab_long if gen["n"] == 1 else clean_inrange
        return _fn
    monkeypatch.setattr(regen_answer, "make_claude_llm", _factory)
    # gate BLOCKS any "team of 50" fabrication, passes otherwise.
    monkeypatch.setattr(regen_answer, "make_audit_fn",
                        lambda *a, **k: (lambda t: (["fabricated team size"]
                                                    if "team of 50" in t.lower() else [])))

    rc = regen_answer.main([
        "JOB-237", "--question", "Why Anthropic?", "--instruction", "lengthen",
        "--max-attempts", "3", "--min-words", "200", "--max-words", "400",
    ])
    assert rc == 0
    q = _q(mp)
    # the fabricated long draft was NEVER written; the clean in-range one landed.
    assert q["value"] == clean_inrange.strip()
    assert "team of 50" not in q["value"]
    assert "residual" not in q


def test_no_length_args_means_no_length_check(tmp_path, monkeypatch):
    """Backward-compat: with NO --min/--max-words, a gate-clean short answer lands as before — the
    length re-check is inert unless a target is passed."""
    _wire(tmp_path, monkeypatch)
    _clean_gate(monkeypatch)
    mp = _seed(tmp_path)
    gen = {"n": 0}

    def _factory(*a, **k):
        def _fn(prompt):
            if "Return ONLY a JSON array" in prompt:
                return "[]"
            gen["n"] += 1
            return "word " * 10   # short, but no length target is set
        return _fn
    monkeypatch.setattr(regen_answer, "make_claude_llm", _factory)

    rc = regen_answer.main([
        "JOB-237", "--question", "Why Anthropic?", "--instruction", "lengthen",
        "--max-attempts", "3",
    ])
    assert rc == 0
    q = _q(mp)
    assert gen["n"] == 1                 # landed on attempt 1, no length retry
    assert q["value"] == ("word " * 10).strip()
    assert "residual" not in q
