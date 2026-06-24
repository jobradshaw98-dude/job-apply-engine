# -*- coding: utf-8 -*-
"""FIX 2 cleanup tool — apply_engine.dedash.

dedash walks the staged manifest and, for every AI-drafted answer with > 1 em-dash, runs the
regen_answer minimal-edit path to reduce them. Proven here against a tmp manifest with a MOCKED
LLM (no claude CLI, no API):
  1. An over-dashed drafted answer is edited; its em-dash count drops; status stays drafted.
  2. A user-provided answer (answered_by=sam) is NEVER touched, even if over-dashed.
  3. A one-em-dash answer is left alone (not a candidate).
  4. A submitted record is skipped entirely.
  5. The summary counts reflect what happened.

The mock LLM simulates the fix by collapsing every em-dash in the CURRENT ANSWER to a comma
(so the AFTER count is 0). It returns '[]' for the judgment self-audit prompt. The deterministic
gate is wired to no-block so the rewrite lands as 'drafted'.
"""
import json

from apply_engine import config
from apply_engine import dedash
from apply_engine import regen_answer


def _seed(tmp_path):
    apps = [
        {
            "job_id": "JOB-900",
            "company": "TestCo",
            "custom_qs": [
                # over-dashed AI draft -> should be edited
                {"q": "Why us?", "kind": "essay", "status": "drafted",
                 "value": "I build — I ship — I learn — daily.", "reason": ""},
                # the user's own over-dashed answer -> must NEVER be touched
                {"q": "Constraint?", "kind": "short_text", "status": "drafted",
                 "answered_by": "sam",
                 "value": "No — none — at all.", "reason": ""},
                # only one em-dash -> not a candidate
                {"q": "About a project?", "kind": "essay", "status": "drafted",
                 "value": "I built one thing — and it worked.", "reason": ""},
            ],
        },
        {
            "job_id": "JOB-SUB",
            "company": "SubCo",
            "submitted": True,
            "custom_qs": [
                {"q": "Why us?", "kind": "essay", "status": "drafted",
                 "value": "Over — dashed — but — submitted.", "reason": ""},
            ],
        },
        {"job_id": "JOB-OTHER", "company": "StubCo", "note": "untouched"},
    ]
    mp = tmp_path / "staged_applications.json"
    mp.write_text(json.dumps(apps, indent=2), encoding="utf-8")
    (tmp_path / "jobs.json").write_text(
        json.dumps([{"id": "JOB-900"}, {"id": "JOB-SUB"}]), encoding="utf-8")
    return mp


def _wire(tmp_path, monkeypatch):
    """Point config at the tmp manifest and mock regen_answer's LLM machinery so the 'fix'
    deterministically removes em-dashes from the CURRENT ANSWER."""
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")
    monkeypatch.setattr(regen_answer, "load_facts", lambda job=None, **k: "FACTS")

    def _factory(*a, **k):
        def _fn(prompt):
            if "Return ONLY a JSON array" in prompt:
                return "[]"
            # The rewrite prompt embeds the CURRENT ANSWER block; recover it and de-dash it.
            marker = "CURRENT ANSWER (edit THIS text"
            if marker in prompt:
                after = prompt.split(marker, 1)[1]
                # the answer text sits between the first newline after the marker and the
                # EDIT INSTRUCTION block; collapse all em-dashes regardless of exact bounds.
                body = after.split("EDIT INSTRUCTION", 1)[0]
                return body.replace("—", ",").strip(": \n")
            return "A de-dashed answer."
        return _fn
    monkeypatch.setattr(regen_answer, "make_claude_llm", _factory)
    monkeypatch.setattr(regen_answer, "make_audit_fn", lambda *a, **k: (lambda t: []))


def _read(mp):
    return json.loads(mp.read_text(encoding="utf-8"))


def _q(mp, job_id, qtext):
    qk = regen_answer._qkey(qtext)
    app = next(a for a in _read(mp) if a.get("job_id") == job_id)
    return next(q for q in app["custom_qs"] if regen_answer._qkey(q.get("q", "")) == qk)


def test_dedash_edits_overdashed_skips_sam_and_submitted(tmp_path, monkeypatch, capsys):
    mp = _seed(tmp_path)
    _wire(tmp_path, monkeypatch)

    summary = dedash.dedash(manifest_path=mp)

    # 1. the over-dashed AI draft was edited; em-dashes are gone; still drafted.
    why = _q(mp, "JOB-900", "Why us?")
    assert why["value"].count("—") == 0
    assert why["status"] == "drafted"

    # 2. The user's answer is untouched (still has its em-dashes, still answered_by sam).
    mine = _q(mp, "JOB-900", "Constraint?")
    assert mine["value"] == "No — none — at all."
    assert mine["answered_by"] == "sam"

    # 3. the single-em-dash answer was not a candidate -> unchanged.
    proj = _q(mp, "JOB-900", "About a project?")
    assert proj["value"] == "I built one thing — and it worked."

    # 4. the submitted record was skipped -> its over-dashed answer is unchanged.
    sub = _q(mp, "JOB-SUB", "Why us?")
    assert sub["value"] == "Over — dashed — but — submitted."

    # 5. summary counts: one answer scanned+edited, one submitted app skipped, no failures.
    assert summary["scanned"] == 1
    assert summary["edited"] == 1
    assert summary["skipped_submitted"] == 1
    assert summary["failures"] == 0

    # before/after line was printed for the edited answer.
    out = capsys.readouterr().out
    assert "em-dashes 3 -> 0" in out


def test_dedash_single_app_scope(tmp_path, monkeypatch):
    """Passing a job_id limits the pass to that app only."""
    mp = _seed(tmp_path)
    _wire(tmp_path, monkeypatch)
    summary = dedash.dedash(job_id="JOB-900", manifest_path=mp)
    assert summary["scanned"] == 1
    assert summary["edited"] == 1
    assert summary["skipped_submitted"] == 0
