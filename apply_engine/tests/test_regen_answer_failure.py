# -*- coding: utf-8 -*-
"""Failure-path test for apply_engine.regen_answer.

regen_answer runs DETACHED (stdout/stderr at DEVNULL) off the dashboard's request-edit
endpoint. If the claude-CLI factory/generation raises (e.g. llm.LLMUnavailable) BEFORE the
manifest is written, an unhandled raise would die leaving no trace and the dashboard would
show the old answer forever. This proves the raise is caught: the target answer is marked
needs_input with its existing value untouched, the manifest is persisted, and exit code is 1.

The autouse conftest fixture redirects config.ARIA_DATA to a throwaway dir; here we override
it (and config.JOBS_JSON) to a tmp manifest we seed ourselves so nothing touches live data.
"""
import json

from apply_engine import config
from apply_engine import regen_answer


def _seed_manifest(tmp_path):
    """Write a staged_applications.json with ONE app carrying ONE drafted custom question."""
    apps = [
        {
            "job_id": "JOB-900",
            "company": "TestCo",
            "role": "Engineer",
            "custom_qs": [
                {"q": "Why do you want to work here?", "kind": "essay",
                 "status": "drafted", "value": "Existing drafted answer.", "reason": ""},
            ],
        },
        {"job_id": "JOB-OTHER", "company": "StubCo", "note": "must survive untouched"},
    ]
    mp = tmp_path / "staged_applications.json"
    mp.write_text(json.dumps(apps, indent=2), encoding="utf-8")
    (tmp_path / "jobs.json").write_text(json.dumps([{"id": "JOB-900"}]), encoding="utf-8")
    return mp


def test_llm_raises_records_needs_input(tmp_path, monkeypatch):
    mp = _seed_manifest(tmp_path)
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")

    class LLMUnavailable(RuntimeError):
        pass

    def _raising_factory(*a, **k):
        raise LLMUnavailable("claude CLI exited non-zero")

    monkeypatch.setattr(regen_answer, "make_claude_llm", _raising_factory)
    monkeypatch.setattr(regen_answer, "load_facts", lambda job=None, **k: "FACTS")

    rc = regen_answer.main([
        "JOB-900", "--question", "Why do you want to work here?",
        "--instruction", "make it tighter",
    ])
    assert rc == 1

    data = json.loads(mp.read_text(encoding="utf-8"))
    app = next(a for a in data if a.get("job_id") == "JOB-900")
    q = app["custom_qs"][0]
    # Existing value untouched.
    assert q["value"] == "Existing drafted answer."
    assert q["status"] == "needs_input"
    assert "LLMUnavailable" in q["reason"]
    assert q["reason"].startswith("regeneration failed:")
    # CONTRACT: a terminated (failed) regen is no longer "in flight" — edit_request is CLEARED
    # so Submit can't lock forever. The instruction is preserved in the edit_history row instead.
    assert q["edit_request"] == ""
    h = q["edit_history"][-1]
    assert h["status"] == "failed"
    assert h["instruction"] == "make it tighter"
    assert h["before"] == "Existing drafted answer."
    assert h["after"] == "Existing drafted answer."   # value untouched on failure
    assert h["ts"]

    # The unrelated stub app survived.
    other = next(a for a in data if a.get("job_id") == "JOB-OTHER")
    assert other["note"] == "must survive untouched"
