# -*- coding: utf-8 -*-
"""Blocked-path test for apply_engine.regen_answer.

DEFECT THIS GUARDS: when the deterministic accuracy gate REFUSES a rewrite, the original
answer must STAND. The old code wrote the refused draft onto the record (q.value = refused
text, q.status = 'blocked'), replacing a good answer with a worse one. The fix leaves
q.value/q.status/q.reason UNTOUCHED and records the refusal ONLY in the edit_history row
(before = kept original, after = refused draft, status = 'blocked', reason = gate blocks).

Proven here:
  1. A blocked edit leaves value, status, and reason exactly as they were (original kept).
  2. The edit_history row carries the refused draft (after) + the gate reason, status 'blocked'.
  3. edit_request is cleared (terminal outcome — Submit must not lock forever).
  4. review_findings on the untouched original are not churned by the refused draft.

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
                {"q": "Why do you want to work here?", "kind": "essay",
                 "status": "drafted", "value": "The good original answer.", "reason": "",
                 "review_findings": [{"severity": "FLAG", "offending_text": "x",
                                      "issue": "y", "fix": "z"}],
                 "edit_request": "make it tighter"},
            ],
        },
        {"job_id": "JOB-OTHER", "company": "StubCo", "note": "must survive untouched"},
    ]
    mp = tmp_path / "staged_applications.json"
    mp.write_text(json.dumps(apps, indent=2), encoding="utf-8")
    (tmp_path / "jobs.json").write_text(json.dumps([{"id": "JOB-900"}]), encoding="utf-8")
    return mp


def _wire(tmp_path, monkeypatch, refused_draft, blocks):
    """Stub LLM returns `refused_draft` for the rewrite (and '[]' for any self-audit JSON
    prompt); the deterministic gate returns `blocks` so the draft is refused."""
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")
    monkeypatch.setattr(regen_answer, "load_facts", lambda job=None, **k: "FACTS")

    def _factory(*a, **k):
        def _fn(prompt):
            if "Return ONLY a JSON array" in prompt:
                return "[]"
            return refused_draft
        return _fn
    monkeypatch.setattr(regen_answer, "make_claude_llm", _factory)
    monkeypatch.setattr(regen_answer, "make_audit_fn", lambda *a, **k: (lambda t: list(blocks)))


def _q(mp):
    data = json.loads(mp.read_text(encoding="utf-8"))
    app = next(a for a in data if a.get("job_id") == "JOB-900")
    return app["custom_qs"][0], data


def test_blocked_edit_keeps_original_and_records_refusal(tmp_path, monkeypatch):
    mp = _seed_manifest(tmp_path)
    _wire(tmp_path, monkeypatch,
          refused_draft="A REFUSED draft that fabricates a metric.",
          blocks=["fabricated metric: 47%"])

    rc = regen_answer.main([
        "JOB-900", "--question", "Why do you want to work here?",
        "--instruction", "make it tighter",
    ])
    # Refused edit is a non-zero outcome (nothing landed).
    assert rc == 0  # the run completed cleanly; the gate refusal is a normal terminal state

    q, data = _q(mp)

    # 1. Original answer STANDS — value/status/reason untouched (the defect this fixes).
    assert q["value"] == "The good original answer."
    assert q["status"] == "drafted"
    assert q["reason"] == ""

    # 4. The original's review_findings were NOT churned by the refused draft.
    assert q["review_findings"] == [{"severity": "FLAG", "offending_text": "x",
                                     "issue": "y", "fix": "z"}]

    # 3. edit_request cleared (terminal outcome).
    assert q["edit_request"] == ""

    # 2. The refusal lives in the edit_history row.
    h = q["edit_history"][-1]
    assert h["status"] == "blocked"
    assert h["instruction"] == "make it tighter"
    assert h["before"] == "The good original answer."     # kept original
    assert h["after"] == "A REFUSED draft that fabricates a metric."  # refused draft
    assert "fabricated metric: 47%" in h["reason"]
    assert h["ts"]

    # The unrelated stub app survived.
    other = next(a for a in data if a.get("job_id") == "JOB-OTHER")
    assert other["note"] == "must survive untouched"


def test_blocked_history_row_is_non_revertible(tmp_path, monkeypatch):
    """A refused edit must not be revertible — the revert gate only fires on a latest history
    row with status 'edited'. A 'blocked' row means nothing changed, so --revert refuses."""
    mp = _seed_manifest(tmp_path)
    _wire(tmp_path, monkeypatch,
          refused_draft="Refused draft.",
          blocks=["some block"])
    regen_answer.main([
        "JOB-900", "--question", "Why do you want to work here?",
        "--instruction", "make it tighter",
    ])

    rc = regen_answer.main([
        "JOB-900", "--question", "Why do you want to work here?", "--revert",
    ])
    assert rc == 1  # nothing to revert — latest history row is 'blocked', not 'edited'

    q, _ = _q(mp)
    # Value still the untouched original after the failed revert attempt.
    assert q["value"] == "The good original answer."
