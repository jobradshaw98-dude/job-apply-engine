# -*- coding: utf-8 -*-
"""Tests for apply_engine.regen_answer's --provide mode (the user's own answer).

What's proven here:
  1. --provide writes value + status=answered + answered_by=sam, clears reason/findings,
     leaves edit_request empty (so it never trips the submit's edited-answer block), and
     appends a 'provided' edit_history row.
  2. --provide prunes the matching item out of the record's top-level needs_sam list.
  3. A multi-value (checkbox_group) question stores `values` (comma-split) AND a joined value.
  4. NO LLM is ever constructed/called on the provide path.
  5. A needs_sam-ONLY question (no matching custom_q) still prunes the callout item.
  6. Unrelated stub records survive untouched.
  7. --provide is mutually exclusive with --instruction / --revert, and rejects empty text.
  8. Draft-on-empty: --instruction on a question whose value is "" still produces an answer
     (the regen path must not assume a prior value existed).

No claude CLI runs: make_claude_llm / make_audit_fn / load_facts are monkeypatched. The
autouse conftest fixture redirects config.ARIA_DATA; we override it (and JOBS_JSON) to a tmp
manifest we seed ourselves.
"""
import json

from apply_engine import config
from apply_engine import finish
from apply_engine import regen_answer


def test_created_custom_q_is_replay_matchable(tmp_path, monkeypatch):
    """A custom_q created by --provide on a needs_sam-only item must be picked up by
    finish.match_custom_entry (the deterministic replay matcher) when the live form shows the
    same label. Proves the answer is no longer silently discarded — it will be re-typed."""
    mp = _seed_manifest(tmp_path)
    _wire_no_llm(tmp_path, monkeypatch)
    regen_answer.main(["JOB-900", "--question", "Country", "--provide", "Canada"])
    stored_qs = _app(mp)["custom_qs"]
    # The live widget label carries a required asterisk + extra whitespace; match must survive
    # normalization (drop '*', collapse spaces). This is exactly _norm_label's job.
    entry = finish.match_custom_entry("Country *", stored_qs)
    assert entry is not None
    assert entry["value"] == "Canada"


def _seed_manifest(tmp_path):
    apps = [
        {
            "job_id": "JOB-900",
            "company": "TestCo",
            "role": "Engineer",
            "status": "needs_input",
            "needs_sam": [
                "Are you able to commit to being in the office 3x per week?",
                "Country",
            ],
            "custom_qs": [
                {"q": "Are you able to commit to being in the office 3x per week?",
                 "kind": "essay", "status": "declined", "value": None,
                 "reason": "personal commitment / logistics — left for Sam"},
                {"q": "Which stacks have you used?", "kind": "checkbox_group",
                 "status": "declined", "value": None, "reason": "left for Sam"},
                {"q": "Why do you want to work here?", "kind": "short_text",
                 "status": "declined", "value": "", "reason": "not supported by facts"},
            ],
        },
        {"job_id": "JOB-OTHER", "company": "StubCo", "note": "must survive untouched"},
    ]
    mp = tmp_path / "staged_applications.json"
    mp.write_text(json.dumps(apps, indent=2), encoding="utf-8")
    (tmp_path / "jobs.json").write_text(json.dumps([{"id": "JOB-900"}]), encoding="utf-8")
    return mp


def _wire_no_llm(tmp_path, monkeypatch):
    """Wire config paths and make any LLM construction an immediate failure — the provide
    path must never touch the model."""
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")
    monkeypatch.setattr(regen_answer, "make_claude_llm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM on provide")))
    monkeypatch.setattr(regen_answer, "make_audit_fn",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no audit on provide")))
    monkeypatch.setattr(regen_answer, "load_facts",
                        lambda job=None, **k: (_ for _ in ()).throw(AssertionError("no facts on provide")))


def _read(mp):
    return json.loads(mp.read_text(encoding="utf-8"))


def _app(mp):
    return next(a for a in _read(mp) if a.get("job_id") == "JOB-900")


def _q(mp, text):
    qk = regen_answer._qkey(text)
    return next(q for q in _app(mp)["custom_qs"] if regen_answer._qkey(q.get("q", "")) == qk)


# ── 1 + 2. provide writes the answer + prunes needs_sam ────────────────────

def test_provide_writes_answer_and_prunes_needs(tmp_path, monkeypatch):
    mp = _seed_manifest(tmp_path)
    _wire_no_llm(tmp_path, monkeypatch)

    rc = regen_answer.main([
        "JOB-900",
        "--question", "Are you able to commit to being in the office 3x per week?",
        "--provide", "Yes",
    ])
    assert rc == 0

    q = _q(mp, "Are you able to commit to being in the office 3x per week?")
    assert q["value"] == "Yes"
    assert q["status"] == "answered"
    assert q["answered_by"] == "sam"
    assert q["reason"] == ""
    assert q["review_findings"] == []
    # edit_request MUST stay empty so the submit gate's edited-answer block never fires.
    assert q.get("edit_request", "") == ""
    hist = q["edit_history"]
    assert hist[-1]["status"] == "provided"
    assert hist[-1]["instruction"] == "(provided by the user)"
    assert hist[-1]["before"] == ""        # was None -> ""
    assert hist[-1]["after"] == "Yes"
    assert hist[-1]["ts"]

    # the matching needs_sam item is gone; the unrelated "Country" item stays.
    needs = _app(mp)["needs_sam"]
    assert "Country" in needs
    assert all("office 3x" not in n for n in needs)


# ── 3. multi-value (checkbox_group) stores values + joined value ──────────────

def test_provide_multi_value(tmp_path, monkeypatch):
    mp = _seed_manifest(tmp_path)
    _wire_no_llm(tmp_path, monkeypatch)

    rc = regen_answer.main([
        "JOB-900", "--question", "Which stacks have you used?",
        "--provide", "Python, LS-DYNA, ANSYS",
    ])
    assert rc == 0
    q = _q(mp, "Which stacks have you used?")
    assert q["values"] == ["Python", "LS-DYNA", "ANSYS"]
    assert q["value"] == "Python, LS-DYNA, ANSYS"
    assert q["status"] == "answered"
    assert q["answered_by"] == "sam"


# ── 5. needs_sam-ONLY question (no custom_q) CREATES a custom_q + prunes ────

def test_provide_needs_sam_only_creates_custom_q(tmp_path, monkeypatch):
    """Audit J1: a question that exists ONLY in needs_sam must have the user's answer STORED
    as a new custom_q (so finish.replay re-types it), not silently discarded. The created entry
    carries the exact shape finish.match_custom_entry/_replay_custom can pick up."""
    mp = _seed_manifest(tmp_path)
    _wire_no_llm(tmp_path, monkeypatch)

    # "Country" is in needs_sam but is NOT one of the custom_qs.
    rc = regen_answer.main(["JOB-900", "--question", "Country", "--provide", "Canada"])
    assert rc == 0
    needs = _app(mp)["needs_sam"]
    assert "Country" not in needs

    # A custom_q was CREATED for it, in the replay-eligible shape.
    created = _q(mp, "Country")
    assert created["q"] == "Country"              # full needs_sam item text -> matches live label
    assert created["value"] == "Canada"
    assert created["status"] == "answered"        # eligible for replay
    assert created["answered_by"] == "sam"
    assert created["reason"] == ""
    assert created["review_findings"] == []
    assert created["edit_request"] == ""          # never blocks submit
    assert created["kind"] == "short_text"        # "Canada" is not yes/no
    h = created["edit_history"][-1]
    assert h["status"] == "provided"
    assert h["instruction"] == "(provided by the user)"
    assert h["before"] == ""
    assert h["after"] == "Canada"
    assert h["ts"]


def test_provide_needs_sam_only_yesno_kind(tmp_path, monkeypatch):
    """A Yes/No provided value (Ramp JOB-216 office-days case) is stored with kind=yesno."""
    mp = _seed_manifest(tmp_path)
    _wire_no_llm(tmp_path, monkeypatch)
    # Append a needs_sam-only office question, mirroring the reported Ramp case.
    data = _read(mp)
    app = next(a for a in data if a.get("job_id") == "JOB-900")
    app["needs_sam"].append(
        "Are you willing to work from our NYC/SF office 2-3 days per week?")
    mp.write_text(json.dumps(data, indent=2), encoding="utf-8")

    rc = regen_answer.main([
        "JOB-900",
        "--question", "Are you willing to work from our NYC/SF office 2-3 days per week?",
        "--provide", "yes",
    ])
    assert rc == 0
    created = _q(mp, "Are you willing to work from our NYC/SF office 2-3 days per week?")
    assert created["kind"] == "yesno"
    assert created["value"] == "yes"
    assert created["status"] == "answered"
    assert all("NYC/SF office" not in n for n in _app(mp)["needs_sam"])


# ── 6. unrelated stub survives ────────────────────────────────────────────────

def test_provide_preserves_other_records(tmp_path, monkeypatch):
    mp = _seed_manifest(tmp_path)
    _wire_no_llm(tmp_path, monkeypatch)
    regen_answer.main(["JOB-900", "--question", "Country", "--provide", "Canada"])
    other = next(a for a in _read(mp) if a.get("job_id") == "JOB-OTHER")
    assert other["note"] == "must survive untouched"


# ── 7. argument validation ────────────────────────────────────────────────────

def test_provide_mutually_exclusive(tmp_path, monkeypatch):
    mp = _seed_manifest(tmp_path)
    _wire_no_llm(tmp_path, monkeypatch)
    rc = regen_answer.main([
        "JOB-900", "--question", "Country", "--provide", "Canada", "--revert",
    ])
    assert rc == 2


def test_provide_empty_rejected(tmp_path, monkeypatch):
    mp = _seed_manifest(tmp_path)
    _wire_no_llm(tmp_path, monkeypatch)
    rc = regen_answer.main(["JOB-900", "--question", "Country", "--provide", "   "])
    assert rc == 2


def test_provide_unknown_question(tmp_path, monkeypatch):
    mp = _seed_manifest(tmp_path)
    _wire_no_llm(tmp_path, monkeypatch)
    # matches neither a custom_q nor a needs_sam item -> not found
    rc = regen_answer.main(["JOB-900", "--question", "totally unrelated thing", "--provide", "x"])
    assert rc == 2


# ── 8. draft-on-empty: --instruction works when the prior value is "" ─────────

def test_draft_on_empty_value(tmp_path, monkeypatch):
    """The dashboard's 'Have Claude draft this' calls request-edit on an UNANSWERED question
    (value == ""). Prove the regen path produces an answer and doesn't assume a prior value
    (edit_history before == "")."""
    mp = _seed_manifest(tmp_path)
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")
    monkeypatch.setattr(regen_answer, "load_facts", lambda job=None, **k: "FACTS")

    def _factory(*a, **k):
        def _fn(prompt):
            if "Return ONLY a JSON array" in prompt:
                return "[]"
            return "A freshly drafted answer."
        return _fn
    monkeypatch.setattr(regen_answer, "make_claude_llm", _factory)
    monkeypatch.setattr(regen_answer, "make_audit_fn", lambda *a, **k: (lambda t: []))

    rc = regen_answer.main([
        "JOB-900", "--question", "Why do you want to work here?",
        "--instruction", "Draft an answer to this question from scratch.",
    ])
    assert rc == 0
    q = _q(mp, "Why do you want to work here?")
    assert q["value"] == "A freshly drafted answer."
    assert q["status"] in ("drafted", "answered")
    # history row records before == "" (there was no prior value)
    assert q["edit_history"][-1]["before"] == ""
    assert q["edit_history"][-1]["after"] == "A freshly drafted answer."
