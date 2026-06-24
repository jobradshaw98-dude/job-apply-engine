# -*- coding: utf-8 -*-
"""TDD for BLOCK #2 wiring: regen_content must, after a successful resume/cover edit lands +
rebuilds, re-run the fabrication + calibration gates and re-stamp the STAGED verdict (mirroring
how regen_answer self-heals the answer-scope verdict). A fabrication or calibration violation
INTRODUCED by an edit must block submit; a clean edit must return to submittable.

regen_content lives at the career ROOT and reads ARIA_DATA from its own module global. We seed a
throwaway applications.json + staged_applications.json + jobs.json and patch regen_content.ARIA_DATA.
The LLM (rewrite), the gate, and the audit/calibration LLMs are all injected via call-tracking
fakes (never raising stubs).
"""
import json

import regen_content


def _seed(tmp_path, *, staged_audit_verdict="PASS"):
    apps = [{
        "id": "APP-700", "job_id": "JOB-700", "company": "TestCo", "role": "Engineer",
        "resume": {"headline": "AI engineer", "summary": "shipped agents",
                   "current_bullets": ["cut analysis time with simulation"],
                   "skills": [{"label": "Sim", "content": "LS-DYNA"}]},
        "cover": {"salutation": "Dear team,", "paragraphs": ["p1", "p2", "p3", "p4"]},
    }]
    staged = [{
        "job_id": "JOB-700", "custom_qs": [],
        "audit": {"verdict": staged_audit_verdict, "judge_ran": True, "gate_blocks": 0,
                  "findings": [], "block_findings": 0, "flag_findings": 0,
                  "refreshed_at": "2026-06-10T09:00:00-07:00"},
        "quality_audit": {"verdict": "PASS",
                          "dimensions": {n: {"score": 5, "note": "", "fix": ""}
                                         for n in ("jd_coverage", "fit", "specificity", "voice")},
                          "calibration": [], "judge_ran": True, "summary": "ok",
                          "refreshed_at": "2026-06-10T09:00:00-07:00"},
        "status": "ready_to_submit",
    }]
    (tmp_path / "applications.json").write_text(json.dumps(apps, indent=2), encoding="utf-8")
    (tmp_path / "staged_applications.json").write_text(json.dumps(staged, indent=2), encoding="utf-8")
    (tmp_path / "jobs.json").write_text(
        json.dumps([{"id": "JOB-700", "jd_text": "Build applied-AI agents."}]), encoding="utf-8")
    return tmp_path / "staged_applications.json"


def _patch(monkeypatch, tmp_path, *, rewrite, gate, fab, cal):
    """Patch regen_content's ARIA_DATA + LLM factories. rewrite/fab return text; gate returns
    a block list; cal returns the calibration-only JSON."""
    monkeypatch.setattr(regen_content, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(regen_content, "load_facts", lambda job=None, **k: "FACTS")
    monkeypatch.setattr(regen_content, "make_claude_llm", lambda *a, **k: rewrite)
    monkeypatch.setattr(regen_content, "make_audit_fn", lambda *a, **k: gate)
    # The self-heal calls into refresh_audit; patch its dep constructors so no real claude runs.
    from apply_engine import refresh_audit, config
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "APPLICATIONS_JSON", tmp_path / "applications.json")
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")
    monkeypatch.setattr(refresh_audit, "_ledger_text", lambda: "LEDGER")

    import apply_engine.llm as _llm
    monkeypatch.setattr(_llm, "make_audit_fn", lambda *a, **k: gate)
    # refresh_after_content_edit builds its fabrication llm via make_claude_llm() AND the calibration
    # recheck builds its own via quality_judge -> make_claude_llm("sonnet"). In production both are
    # claude -p on the same model, so a model-arg split won't distinguish them. Instead route by the
    # PROMPT: the calibration prompt asks for a {"calibration": [...]} object; the fabrication prompt
    # asks for a [...] array. A single dispatcher fake records to the right tracker accordingly.
    def _dispatch(prompt):
        if "CALIBRATION" in prompt or "positioning rules" in prompt.lower():
            return cal(prompt)
        return fab(prompt)
    monkeypatch.setattr(_llm, "make_claude_llm", lambda *a, **k: _dispatch)
    # No PDF rebuild in tests.


class _Fake:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        return self.payload


def test_edit_introducing_fabrication_blocks_staged_verdict(tmp_path, monkeypatch):
    sp = _seed(tmp_path)
    # The rewrite returns a bullet with an invented metric; the fab judge flags it BLOCK.
    rewrite = _Fake("Boosted revenue 300% with simulation")
    gate = lambda t: []
    fab = _Fake(json.dumps([{"offending_text": "Boosted revenue 300%", "issue": "unsupported",
                             "fix": "remove", "severity": "BLOCK"}]))
    cal = _Fake(json.dumps({"calibration": []}))
    _patch(monkeypatch, tmp_path, rewrite=rewrite, gate=gate, fab=fab, cal=cal)

    rc = regen_content.main([
        "APP-700", "--doc", "resume", "--element", "current_bullets.0",
        "--instruction", "make it punchier", "--no-rebuild"])
    assert rc == 0  # the edit itself landed (the gate didn't block the deterministic check)
    assert fab.calls, "the fabrication self-heal must call the ledger lens on the new text"

    rec = next(r for r in json.loads(sp.read_text(encoding="utf-8")) if r["job_id"] == "JOB-700")
    assert rec["audit"]["verdict"] == "BLOCKED"
    assert rec["audit"]["block_findings"] >= 1


def test_edit_introducing_calibration_violation_fails_quality(tmp_path, monkeypatch):
    sp = _seed(tmp_path)
    rewrite = _Fake("Proficient in Python and expert MATLAB programmer")
    gate = lambda t: []
    fab = _Fake("[]")
    cal = _Fake(json.dumps({"calibration": [
        {"type": "coding_fluency", "where": "resume", "evidence": "Proficient in Python",
         "fix": "frame as AI-orchestrated"}]}))
    _patch(monkeypatch, tmp_path, rewrite=rewrite, gate=gate, fab=fab, cal=cal)

    rc = regen_content.main([
        "APP-700", "--doc", "resume", "--element", "current_bullets.0",
        "--instruction", "list my coding skills", "--no-rebuild"])
    assert rc == 0
    rec = next(r for r in json.loads(sp.read_text(encoding="utf-8")) if r["job_id"] == "JOB-700")
    assert rec["quality_audit"]["verdict"] == "FAIL"
    assert rec["quality_audit"]["calibration"][0]["type"] == "coding_fluency"
    # Polish dims frozen.
    assert all(rec["quality_audit"]["dimensions"][n]["score"] == 5
               for n in ("jd_coverage", "fit", "specificity", "voice"))


def _outdated(rec, app_rec):
    """Mirror aria_server._content_edit_outdates_audit: latest landed (edited/reverted) content_edit
    ts vs min(audit, quality_audit).refreshed_at. True == the dashboard would read STALE."""
    terminal = {"edited", "reverted"}
    latest = None
    for e in (app_rec.get("content_edits") or []):
        if not isinstance(e, dict):
            continue
        if (e.get("status", "") or "").lower() not in terminal:
            continue
        ts = e.get("ts")
        if ts and (latest is None or ts > latest):
            latest = ts
    if latest is None:
        return False
    fab = (rec.get("audit") or {}).get("refreshed_at")
    qual = (rec.get("quality_audit") or {}).get("refreshed_at")
    if not fab or not qual:
        return True
    return latest > min(fab, qual)


def _future_clock(monkeypatch):
    """Monotonic fake clock on regen_content._local_iso that returns timestamps in the FUTURE
    (after real now), advancing one minute per call. This reproduces the production timing GAP:
    the content_edit rows get FUTURE ts while the self-heal's audit stamp (refresh_audit._local_iso,
    real now) is EARLIER — so an unfixed self-heal reads stale. The floor fix lifts the audit stamp
    to >= the edit ts, clearing the false staleness."""
    from datetime import datetime as _dt, timedelta as _td
    base = _dt(2099, 1, 1, 12, 0, 0).astimezone()
    state = {"n": 0}

    def _clock():
        ts = (base + _td(minutes=state["n"])).isoformat(timespec="seconds")
        state["n"] += 1
        return ts

    monkeypatch.setattr(regen_content, "_local_iso", _clock)


def test_clean_edit_self_heal_does_not_read_stale(tmp_path, monkeypatch):
    # BUG B end-to-end (JOB-242): after a clean per-element edit, the landed content_edit terminal
    # row must NOT post-date the re-stamped audit. Before the fix the audit was stamped mid-run and
    # the edit row's later ts made the dashboard read the edit as stale forever.
    sp = _seed(tmp_path)
    rewrite = _Fake("Cut analysis time with simulation and optimization")
    gate = lambda t: []
    fab = _Fake("[]")
    cal = _Fake(json.dumps({"calibration": []}))
    _patch(monkeypatch, tmp_path, rewrite=rewrite, gate=gate, fab=fab, cal=cal)
    _future_clock(monkeypatch)   # edit rows land in the future -> exposes the false-staleness bug

    rc = regen_content.main([
        "APP-700", "--doc", "resume", "--element", "current_bullets.0",
        "--instruction", "tighten", "--no-rebuild"])
    assert rc == 0
    rec = next(r for r in json.loads(sp.read_text(encoding="utf-8")) if r["job_id"] == "JOB-700")
    app_rec = next(a for a in json.loads((tmp_path / "applications.json").read_text(encoding="utf-8"))
                   if a["id"] == "APP-700")
    assert _outdated(rec, app_rec) is False, (
        "a clean self-healed edit must NOT read as stale "
        f"(edit rows={[ (e.get('ts'), e.get('status')) for e in app_rec.get('content_edits', []) ]}, "
        f"audit={rec['audit'].get('refreshed_at')}, quality={rec['quality_audit'].get('refreshed_at')})")


def test_doc_level_clean_edit_self_heal_does_not_read_stale(tmp_path, monkeypatch):
    # Same invariant for the DOC-LEVEL path, which writes a terminal f"{doc}.doc" row AFTER the
    # self-heal. That terminal row's ts must be covered by the audit floor too.
    sp = _seed(tmp_path)
    # the doc-level LLM returns a JSON change-set; the gate passes, fab/cal clean.
    rewrite = _Fake(json.dumps([{"element": "current_bullets.0",
                                 "text": "Cut analysis time with simulation and optimization"}]))
    gate = lambda t: []
    fab = _Fake("[]")
    cal = _Fake(json.dumps({"calibration": []}))
    _patch(monkeypatch, tmp_path, rewrite=rewrite, gate=gate, fab=fab, cal=cal)
    _future_clock(monkeypatch)

    rc = regen_content.main([
        "APP-700", "--doc", "resume",
        "--instruction", "tighten the whole resume", "--no-rebuild"])
    assert rc == 0
    rec = next(r for r in json.loads(sp.read_text(encoding="utf-8")) if r["job_id"] == "JOB-700")
    app_rec = next(a for a in json.loads((tmp_path / "applications.json").read_text(encoding="utf-8"))
                   if a["id"] == "APP-700")
    # the terminal "resume.doc" row exists and is the newest
    assert any((e.get("element") == "resume.doc" and (e.get("status") or "").lower() == "edited")
               for e in app_rec.get("content_edits", []))
    assert _outdated(rec, app_rec) is False, (
        "a clean doc-level self-healed edit (incl. the terminal .doc row) must NOT read stale")


def test_edit_without_self_heal_still_reads_stale(tmp_path, monkeypatch):
    # NEGATIVE invariant: an edit that landed with NO subsequent re-audit (the self-heal never ran,
    # e.g. it crashed) MUST still read stale so the wedge-recovery path stays live. Here we land a
    # content_edit row directly and leave the audit stamp at its OLD pre-edit time.
    sp = _seed(tmp_path)
    apps = json.loads((tmp_path / "applications.json").read_text(encoding="utf-8"))
    apps[0].setdefault("content_edits", []).append(
        {"ts": "2099-01-01T12:00:00-07:00", "doc": "resume", "element": "current_bullets.0",
         "status": "edited", "instruction": "manual", "before": "x", "after": "y"})
    (tmp_path / "applications.json").write_text(json.dumps(apps, indent=2), encoding="utf-8")
    rec = next(r for r in json.loads(sp.read_text(encoding="utf-8")) if r["job_id"] == "JOB-700")
    assert _outdated(rec, apps[0]) is True, "an un-healed landed edit must still read stale"


def test_clean_edit_returns_to_submittable(tmp_path, monkeypatch):
    sp = _seed(tmp_path)
    rewrite = _Fake("Cut analysis time with simulation and optimization")
    gate = lambda t: []
    fab = _Fake("[]")
    cal = _Fake(json.dumps({"calibration": []}))
    _patch(monkeypatch, tmp_path, rewrite=rewrite, gate=gate, fab=fab, cal=cal)

    rc = regen_content.main([
        "APP-700", "--doc", "resume", "--element", "current_bullets.0",
        "--instruction", "tighten", "--no-rebuild"])
    assert rc == 0
    rec = next(r for r in json.loads(sp.read_text(encoding="utf-8")) if r["job_id"] == "JOB-700")
    assert rec["audit"]["verdict"] == "PASS"
    assert rec["quality_audit"]["verdict"] == "PASS"
