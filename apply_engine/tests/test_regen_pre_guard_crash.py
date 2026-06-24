# -*- coding: utf-8 -*-
"""TDD for BLOCK #4 invariant #5: a regen CLI that raises at ANY point (including the pre-guard
file I/O like load_facts) must still leave a TERMINAL state, not a permanent "Regenerating…".

Both regen_content and regen_answer run DETACHED with stderr->DEVNULL. They have a try/except
around the LLM call, but file I/O (load_facts, manifest read) BEFORE that guard could raise and
die leaving the server's pending marker / edit_request set forever (no TTL on that state).
load_facts failing has caused a live incident. A top-level try/except in BOTH CLIs must, on any
unhandled exception, write a terminal `failed` content_edits row (regen_content) / clear the
edit_request + append a failed edit_history row (regen_answer) under the file mutex, and exit
non-zero — so the UI shows "edit failed", not a permanent hang.

We simulate the crash by monkeypatching the PRE-GUARD call (load_facts) to raise.
"""
import json

import regen_content
from apply_engine import config, regen_answer


# ---------------- regen_content: pre-guard load_facts crash ----------------

def _seed_content(tmp_path):
    apps = [{
        "id": "APP-700", "job_id": "JOB-700", "company": "TestCo", "role": "Engineer",
        "resume": {"headline": "AI engineer", "summary": "shipped agents",
                   "current_bullets": ["cut analysis time with simulation"],
                   "skills": [{"label": "Sim", "content": "LS-DYNA"}]},
    }]
    (tmp_path / "applications.json").write_text(json.dumps(apps, indent=2), encoding="utf-8")
    (tmp_path / "jobs.json").write_text(json.dumps([{"id": "JOB-700"}]), encoding="utf-8")
    return tmp_path / "applications.json"


def test_content_pre_guard_crash_writes_terminal_failed(tmp_path, monkeypatch):
    ap = _seed_content(tmp_path)
    monkeypatch.setattr(regen_content, "ARIA_DATA", tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("load_facts disk read blew up")
    monkeypatch.setattr(regen_content, "load_facts", _boom)

    rc = regen_content.main([
        "APP-700", "--doc", "resume", "--element", "current_bullets.0",
        "--instruction", "tighten", "--no-rebuild"])
    assert rc != 0

    apps = json.loads(ap.read_text(encoding="utf-8"))
    app = next(a for a in apps if a["id"] == "APP-700")
    # A TERMINAL row was written for this element, not a left-behind pending/Regenerating state.
    rows = [e for e in (app.get("content_edits") or [])
            if e.get("doc") == "resume" and e.get("element") == "current_bullets.0"]
    assert rows, "a terminal content_edits row must be written on a pre-guard crash"
    last = rows[-1]
    assert last["status"] == "failed"
    assert "load_facts" in last.get("reason", "") or "RuntimeError" in last.get("reason", "")
    # The original text is untouched (the crash happened before any rewrite).
    assert app["resume"]["current_bullets"][0] == "cut analysis time with simulation"


def test_content_doclevel_pre_guard_crash_writes_terminal_failed(tmp_path, monkeypatch):
    ap = _seed_content(tmp_path)
    monkeypatch.setattr(regen_content, "ARIA_DATA", tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("load_facts blew up")
    monkeypatch.setattr(regen_content, "load_facts", _boom)

    rc = regen_content.main([
        "APP-700", "--doc", "resume", "--instruction", "lead with outcomes", "--no-rebuild"])
    assert rc != 0
    apps = json.loads(ap.read_text(encoding="utf-8"))
    app = next(a for a in apps if a["id"] == "APP-700")
    rows = [e for e in (app.get("content_edits") or [])
            if e.get("doc") == "resume" and e.get("element") == "resume.doc"]
    assert rows and rows[-1]["status"] == "failed"


# ---------------- regen_answer: pre-guard load_facts crash ----------------

def _seed_answer(tmp_path):
    apps = [{
        "job_id": "JOB-900", "company": "TestCo", "role": "Engineer",
        "custom_qs": [{"q": "Why us?", "kind": "essay", "status": "drafted",
                       "value": "Existing answer.", "reason": "", "edit_request": "in flight"}],
    }]
    mp = tmp_path / "staged_applications.json"
    mp.write_text(json.dumps(apps, indent=2), encoding="utf-8")
    (tmp_path / "jobs.json").write_text(json.dumps([{"id": "JOB-900"}]), encoding="utf-8")
    return mp


def test_answer_pre_guard_crash_clears_edit_request(tmp_path, monkeypatch):
    mp = _seed_answer(tmp_path)
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")

    def _boom(*a, **k):
        raise RuntimeError("load_facts blew up before the LLM guard")
    monkeypatch.setattr(regen_answer, "load_facts", _boom)

    rc = regen_answer.main([
        "JOB-900", "--question", "Why us?", "--instruction", "tighten"])
    assert rc != 0

    data = json.loads(mp.read_text(encoding="utf-8"))
    q = next(a for a in data if a["job_id"] == "JOB-900")["custom_qs"][0]
    # edit_request CLEARED so Submit can't lock forever; value untouched; a terminal history row.
    assert q["edit_request"] == ""
    assert q["value"] == "Existing answer."
    hist = q.get("edit_history") or []
    assert hist and hist[-1]["status"] == "failed"
