# -*- coding: utf-8 -*-
"""Tests for apply_engine.regen_answer's edit-history + revert behavior.

Three things proven here:
  1. A successful regen appends an edit_history row {ts, instruction, before, after, status}.
  2. --revert restores the prior value, drops status back to 'drafted', and clears
     edit_request + review_findings.
  3. --revert refuses (exit 1) when the current value no longer matches the latest edit's
     `after` (a later change moved it) — it must NOT clobber the newer text.

No claude CLI runs: make_claude_llm / make_audit_fn / load_facts are monkeypatched. The
autouse conftest fixture redirects config.ARIA_DATA; we override it (and JOBS_JSON) to a tmp
manifest we seed ourselves.
"""
import json

from apply_engine import config
from apply_engine import regen_answer


def _seed_manifest(tmp_path):
    apps = [
        {
            "job_id": "JOB-900",
            "company": "TestCo",
            "role": "Engineer",
            "custom_qs": [
                {"q": "Why do you want to work here?", "kind": "short_text",
                 "status": "drafted", "value": "Existing drafted answer.", "reason": ""},
            ],
        },
        {"job_id": "JOB-OTHER", "company": "StubCo", "note": "must survive untouched"},
    ]
    mp = tmp_path / "staged_applications.json"
    mp.write_text(json.dumps(apps, indent=2), encoding="utf-8")
    (tmp_path / "jobs.json").write_text(json.dumps([{"id": "JOB-900"}]), encoding="utf-8")
    return mp


def _wire(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")
    monkeypatch.setattr(regen_answer, "load_facts", lambda job=None, **k: "FACTS")
    # Canned llm: rewrite for the question, [] for the self-audit JSON ask.
    def _factory(*a, **k):
        def _fn(prompt):
            if "Return ONLY a JSON array" in prompt:
                return "[]"
            return "Brand new answer text."
        return _fn
    monkeypatch.setattr(regen_answer, "make_claude_llm", _factory)
    monkeypatch.setattr(regen_answer, "make_audit_fn", lambda *a, **k: (lambda t: []))


def _read(mp):
    return json.loads(mp.read_text(encoding="utf-8"))


def _q(mp):
    app = next(a for a in _read(mp) if a.get("job_id") == "JOB-900")
    return app["custom_qs"][0]


# ── 1. successful edit appends edit_history ──────────────────────────────────

def test_edit_appends_history(tmp_path, monkeypatch):
    mp = _seed_manifest(tmp_path)
    _wire(tmp_path, monkeypatch)

    rc = regen_answer.main([
        "JOB-900", "--question", "Why do you want to work here?",
        "--instruction", "make it tighter",
    ])
    assert rc == 0

    q = _q(mp)
    assert q["value"] == "Brand new answer text."
    assert q["status"] == "drafted"
    # CONTRACT: a completed instruction-regen CLEARS edit_request (not "in flight" anymore).
    assert q["edit_request"] == ""
    hist = q["edit_history"]
    assert len(hist) == 1
    h = hist[0]
    assert h["instruction"] == "make it tighter"
    assert h["before"] == "Existing drafted answer."
    assert h["after"] == "Brand new answer text."
    # The HISTORY-row status is "edited" (what the revert gates key on), even though the
    # question's own status field is "drafted". A real edit MUST produce "edited" here.
    assert h["status"] == "edited"
    assert q["status"] == "drafted"
    assert "ts" in h and h["ts"]


# ── 2. revert restores + clears edit_request / review_findings ───────────────

def test_revert_restores_and_clears(tmp_path, monkeypatch):
    mp = _seed_manifest(tmp_path)
    _wire(tmp_path, monkeypatch)

    # Build the precondition by RUNNING the edit entrypoint (monkeypatched llm), NOT by
    # hand-seeding "status":"edited" — that hand-seed previously masked the literal-mismatch
    # bug. A real edit must produce a history row the revert gates accept.
    rc = regen_answer.main([
        "JOB-900", "--question", "Why do you want to work here?",
        "--instruction", "make it tighter",
    ])
    assert rc == 0
    q = _q(mp)
    assert q["value"] == "Brand new answer text."
    # CONTRACT: a COMPLETED regen clears edit_request (the self-audit IS the fresh review);
    # the instruction lives on in the edit_history row, which is what the dashboard renders.
    assert q["edit_request"] == ""
    assert q["edit_history"][-1]["status"] == "edited"  # real edit → revertible row
    assert q["edit_history"][-1]["instruction"] == "make it tighter"

    # Any LLM call during a revert is a bug.
    monkeypatch.setattr(regen_answer, "make_claude_llm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM in revert")))

    rc = regen_answer.main([
        "JOB-900", "--question", "Why do you want to work here?", "--revert",
    ])
    assert rc == 0

    q = _q(mp)
    assert q["value"] == "Existing drafted answer."   # restored
    assert q["status"] == "drafted"
    assert q["edit_request"] == ""
    assert q["review_findings"] == []
    # A reverted history row appended (edited + reverted == 2).
    assert len(q["edit_history"]) == 2
    r = q["edit_history"][-1]
    assert r["status"] == "reverted"
    assert r["instruction"] == "(revert)"
    assert r["before"] == "Brand new answer text."
    assert r["after"] == "Existing drafted answer."

    # Unrelated stub survived.
    other = next(a for a in _read(mp) if a.get("job_id") == "JOB-OTHER")
    assert other["note"] == "must survive untouched"


# ── 3. revert refused on mismatch ─────────────────────────────────────────────

def test_revert_refused_on_mismatch(tmp_path, monkeypatch, capsys):
    mp = _seed_manifest(tmp_path)
    _wire(tmp_path, monkeypatch)

    data = _read(mp)
    q = next(a for a in data if a.get("job_id") == "JOB-900")["custom_qs"][0]
    q["value"] = "A DIFFERENT later value."   # current != latest after
    q["status"] = "drafted"
    q["edit_history"] = [{
        "ts": "2026-06-03T10:00:00-07:00", "instruction": "make it tighter",
        "before": "Existing drafted answer.", "after": "Brand new answer text.",
        "status": "edited",
    }]
    mp.write_text(json.dumps(data, indent=2), encoding="utf-8")

    monkeypatch.setattr(regen_answer, "make_claude_llm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM in revert")))

    rc = regen_answer.main([
        "JOB-900", "--question", "Why do you want to work here?", "--revert",
    ])
    assert rc == 1
    assert "no longer matches" in capsys.readouterr().out
    q = _q(mp)
    assert q["value"] == "A DIFFERENT later value."   # untouched
    assert len(q["edit_history"]) == 1                # no revert row


def test_revert_and_instruction_mutually_exclusive(tmp_path, monkeypatch):
    mp = _seed_manifest(tmp_path)
    _wire(tmp_path, monkeypatch)
    rc = regen_answer.main([
        "JOB-900", "--question", "Why do you want to work here?",
        "--instruction", "x", "--revert",
    ])
    assert rc == 2


# ── FIX 1: a BLOCKED regen also clears edit_request ──────────────────────────

def test_blocked_edit_clears_edit_request(tmp_path, monkeypatch):
    """A regen whose audit BLOCKS the new draft TERMINATES the in-flight edit WITHOUT clobbering
    the original answer: q.value/q.status stay as they were (the original stands), edit_request
    is cleared (so Submit isn't locked forever), and a non-revertible 'blocked' history row is
    appended carrying the instruction, the kept original (before), and the refused draft (after).
    See test_regen_answer_blocked.py for the full contract."""
    mp = _seed_manifest(tmp_path)
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")
    monkeypatch.setattr(regen_answer, "load_facts", lambda job=None, **k: "FACTS")

    def _factory(*a, **k):
        def _fn(prompt):
            if "Return ONLY a JSON array" in prompt:
                return "[]"
            return "Answer with an unsupported claim."
        return _fn
    monkeypatch.setattr(regen_answer, "make_claude_llm", _factory)
    # audit returns a finding -> the new draft is refused
    monkeypatch.setattr(regen_answer, "make_audit_fn",
                        lambda *a, **k: (lambda t: ["fabricated metric"]))

    rc = regen_answer.main([
        "JOB-900", "--question", "Why do you want to work here?",
        "--instruction", "make it punchier",
    ])
    assert rc == 0
    q = _q(mp)
    # Original answer UNTOUCHED — the refused draft did NOT replace it (the defect this guards).
    assert q["status"] == "drafted"
    assert q["value"] == "Existing drafted answer."
    assert q["edit_request"] == ""               # in-flight edit terminated -> cleared
    h = q["edit_history"][-1]
    assert h["status"] == "blocked"              # non-revertible
    assert h["instruction"] == "make it punchier"
    assert h["before"] == "Existing drafted answer."          # kept original
    assert h["after"] == "Answer with an unsupported claim."  # refused draft
    assert "fabricated metric" in h["reason"]


# ── FIX 1: a needs_input (decline/empty) regen clears edit_request + logs instr ─

def test_needs_input_edit_clears_edit_request(tmp_path, monkeypatch):
    """When the model DECLINEs (no value), the edit still terminates: edit_request cleared,
    and the instruction preserved in a 'needs_input' history row (per contract — history is
    what the dashboard renders, even when there's no new value)."""
    mp = _seed_manifest(tmp_path)
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")
    monkeypatch.setattr(regen_answer, "load_facts", lambda job=None, **k: "FACTS")

    from apply_engine.answer_gen import DECLINE

    def _factory(*a, **k):
        def _fn(prompt):
            return DECLINE + " cannot answer within the facts"
        return _fn
    monkeypatch.setattr(regen_answer, "make_claude_llm", _factory)
    monkeypatch.setattr(regen_answer, "make_audit_fn", lambda *a, **k: (lambda t: []))

    rc = regen_answer.main([
        "JOB-900", "--question", "Why do you want to work here?",
        "--instruction", "make it punchier",
    ])
    assert rc == 0
    q = _q(mp)
    assert q["status"] == "needs_input"
    assert q["value"] == ""
    assert q["edit_request"] == ""
    h = q["edit_history"][-1]
    assert h["status"] == "needs_input"
    assert h["instruction"] == "make it punchier"
    assert h["after"] == ""
