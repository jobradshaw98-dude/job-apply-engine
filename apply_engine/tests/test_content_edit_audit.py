# -*- coding: utf-8 -*-
"""TDD for BLOCK #2: a resume/cover EDIT must re-run the fabrication + calibration gates and
re-stamp the STAGED verdict, so an invented metric or a mis-targeting pitch INTRODUCED by an
edit blocks submit instead of riding along on the stale staging PASS.

Content edits live in applications.json (keyed by app_id); the submit-gating verdict lives in
staged_applications.json (keyed by job_id). regen_content rebuilds the PDF but historically never
touched the staged verdict, so an edit was invisible to can_submit. This drives the new engine
entrypoints that close that gap, mirroring how regen_answer self-heals the answer-scope verdict.

FAKE llms are CALL-TRACKING (return canned JSON, never raise — a raising stub gets swallowed by a
fail-closed except and passes vacuously, the trap that bit us before).
"""
import json

from apply_engine.refresh_audit import audit_content_text, refresh_after_content_edit


# ---- audit_content_text: PURE ledger trace over resume/cover text, BLOCK-capable ----

def test_clean_content_no_findings():
    out = audit_content_text("cut analysis time with simulation", "current_bullets",
                             gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER")
    assert out == []


def test_block_severity_content_finding():
    judged = json.dumps([{"offending_text": "boosted revenue 300%", "issue": "no ledger support",
                          "fix": "remove the number", "severity": "BLOCK"}])
    out = audit_content_text("boosted revenue 300%", "current_bullets",
                             gate_fn=lambda t: [], llm=lambda p: judged, ledger="LEDGER")
    assert len(out) == 1
    assert out[0]["severity"] == "BLOCK"
    assert out[0]["doc"] == "resume"
    assert out[0]["lens"] == "fabrication"
    assert out[0]["offending_text"] == "boosted revenue 300%"


def test_gate_block_on_content_is_block_finding():
    out = audit_content_text("uses ANSYS at Meridian", "current_bullets",
                             gate_fn=lambda t: ["forbidden tool attribution"],
                             llm=lambda p: "[]", ledger="LEDGER")
    assert any(f["severity"] == "BLOCK" and f["lens"] == "gate" for f in out)


# ---- refresh_after_content_edit: re-stamps the STAGED audit + quality_audit ----

def _seed(tmp_path, *, custom_qs=None, quality_audit=None):
    staged = [{
        "job_id": "JOB-700",
        "custom_qs": custom_qs if custom_qs is not None else [],
        "audit": {"verdict": "PASS", "judge_ran": True, "gate_blocks": 0,
                  "findings": [], "block_findings": 0, "flag_findings": 0,
                  "refreshed_at": "2026-06-10T09:00:00-07:00"},
        "quality_audit": quality_audit if quality_audit is not None else {
            "verdict": "PASS",
            "dimensions": {n: {"score": 5, "note": "", "fix": ""}
                           for n in ("jd_coverage", "fit", "specificity", "voice")},
            "calibration": [], "judge_ran": True, "summary": "ok",
            "refreshed_at": "2026-06-10T09:00:00-07:00"},
        "status": "ready_to_submit",
    }]
    apps = [{"id": "APP-700", "job_id": "JOB-700",
             "resume": {"headline": "AI engineer", "summary": "shipped agents",
                        "current_bullets": ["cut analysis time with simulation"],
                        "skills": [{"label": "Sim", "content": "LS-DYNA"}]},
             "cover": {"salutation": "Dear team,", "paragraphs": ["p1", "p2", "p3", "p4"]}}]
    sp = tmp_path / "staged_applications.json"
    ap = tmp_path / "applications.json"
    jp = tmp_path / "jobs.json"
    sp.write_text(json.dumps(staged, indent=2), encoding="utf-8")
    ap.write_text(json.dumps(apps, indent=2), encoding="utf-8")
    jp.write_text(json.dumps([{"id": "JOB-700", "jd_text": "Build applied-AI agents."}]),
                  encoding="utf-8")
    return sp, ap, jp


def _patch_paths(monkeypatch, tmp_path, sp, ap, jp):
    from apply_engine import config
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "APPLICATIONS_JSON", ap)
    monkeypatch.setattr(config, "JOBS_JSON", jp)


def test_content_edit_with_fabrication_blocks_submit(tmp_path, monkeypatch):
    sp, ap, jp = _seed(tmp_path)
    _patch_paths(monkeypatch, tmp_path, sp, ap, jp)
    # The just-edited bullet text carries an invented metric the ledger doesn't support.
    fab = json.dumps([{"offending_text": "boosted revenue 300%", "issue": "unsupported",
                       "fix": "remove", "severity": "BLOCK"}])
    fab_calls = []
    def _fab_llm(p):
        fab_calls.append(p)
        return fab
    cal_calls = []
    def _cal_llm(p):
        cal_calls.append(p)
        return json.dumps({"calibration": []})

    refresh_after_content_edit(
        "JOB-700", "resume", "current_bullets.0", "boosted revenue 300%",
        manifest_path=sp, gate_fn=lambda t: [], llm=_fab_llm, ledger="LEDGER",
        quality_llm=_cal_llm)

    assert fab_calls, "the fabrication lens must actually be called on the edited text"
    rec = next(r for r in json.loads(sp.read_text(encoding="utf-8")) if r["job_id"] == "JOB-700")
    assert rec["audit"]["verdict"] == "BLOCKED"
    assert rec["audit"]["block_findings"] >= 1
    # The fabrication finding names the edited content.
    assert any("boosted revenue 300%" in (f.get("offending_text", "") or "")
               for f in rec["audit"]["findings"])


def test_content_edit_with_calibration_violation_fails_quality(tmp_path, monkeypatch):
    sp, ap, jp = _seed(tmp_path)
    _patch_paths(monkeypatch, tmp_path, sp, ap, jp)
    cal = json.dumps({"calibration": [
        {"type": "coding_fluency", "where": "resume", "evidence": "proficient in Python",
         "fix": "frame as AI-orchestrated"}]})

    refresh_after_content_edit(
        "JOB-700", "resume", "current_bullets.0", "proficient in Python",
        manifest_path=sp, gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER",
        quality_llm=lambda p: cal)

    rec = next(r for r in json.loads(sp.read_text(encoding="utf-8")) if r["job_id"] == "JOB-700")
    qa = rec["quality_audit"]
    assert qa["verdict"] == "FAIL"
    assert qa["calibration"] and qa["calibration"][0]["type"] == "coding_fluency"
    # POLISH DIMS FROZEN: the four dimension scores from staging are byte-identical.
    assert all(qa["dimensions"][n]["score"] == 5
               for n in ("jd_coverage", "fit", "specificity", "voice"))


def test_clean_content_edit_returns_to_submittable(tmp_path, monkeypatch):
    sp, ap, jp = _seed(tmp_path)
    _patch_paths(monkeypatch, tmp_path, sp, ap, jp)
    refresh_after_content_edit(
        "JOB-700", "resume", "current_bullets.0", "cut analysis time with simulation",
        manifest_path=sp, gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER",
        quality_llm=lambda p: json.dumps({"calibration": []}))
    rec = next(r for r in json.loads(sp.read_text(encoding="utf-8")) if r["job_id"] == "JOB-700")
    assert rec["audit"]["verdict"] == "PASS"
    assert rec["quality_audit"]["verdict"] == "PASS"   # frozen all-5 dims, no violation


def test_degraded_judge_during_edit_fails_closed(tmp_path, monkeypatch):
    # LLM lens unavailable during a content edit (an empty ledger disables the judge, so the
    # canned llm is never called and nothing raises): the re-stamped fabrication verdict must be
    # BLOCKED, not a PASS that only judge_ran marks degraded, and the summary must say the
    # review was unavailable.
    sp, ap, jp = _seed(tmp_path)
    _patch_paths(monkeypatch, tmp_path, sp, ap, jp)
    refresh_after_content_edit(
        "JOB-700", "resume", "current_bullets.0", "cut analysis time with simulation",
        manifest_path=sp, gate_fn=lambda t: [], llm=lambda p: "[]", ledger="",
        quality_llm=lambda p: json.dumps({"calibration": []}))
    rec = next(r for r in json.loads(sp.read_text(encoding="utf-8")) if r["job_id"] == "JOB-700")
    assert rec["audit"]["judge_ran"] is False
    assert rec["audit"]["verdict"] == "BLOCKED"
    assert "unavailable" in rec["audit"]["summary"].lower()


def test_self_heal_stamps_audit_floor_not_below_edit_ts(tmp_path, monkeypatch):
    # BUG B (JOB-242 false staleness): the staged audit refreshed_at is stamped MID-run, but the
    # content_edit row it heals is finalized with a LATER ts. The dashboard's staleness gate then
    # reads the edit as "newer than the audit" -> permanently false-stale. Fix: the self-heal must
    # stamp BOTH audit.refreshed_at and quality_audit.refreshed_at to a time >= the edit ts it heals
    # (audit_floor_ts). Here we pass a floor in the FUTURE relative to "now": both stamps must land
    # at-or-after the floor, so latest_edit (== floor) is NOT > the audit stamps.
    sp, ap, jp = _seed(tmp_path)
    _patch_paths(monkeypatch, tmp_path, sp, ap, jp)
    # a floor well after the natural _local_iso() now, mirroring the terminal-row ts written later
    floor = "2099-01-01T12:00:00-07:00"
    refresh_after_content_edit(
        "JOB-700", "resume", "current_bullets.0", "cut analysis time with simulation",
        manifest_path=sp, gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER",
        quality_llm=lambda p: json.dumps({"calibration": []}), audit_floor_ts=floor)
    rec = next(r for r in json.loads(sp.read_text(encoding="utf-8")) if r["job_id"] == "JOB-700")
    assert rec["audit"]["refreshed_at"] >= floor, "fabrication stamp must be >= the edit floor"
    assert rec["quality_audit"]["refreshed_at"] >= floor, "quality stamp must be >= the edit floor"


def test_self_heal_floor_does_not_pull_stamps_backwards(tmp_path, monkeypatch):
    # A floor in the PAST (the normal case: the edit row was written before the heal) must NOT pull
    # the stamps back to that past floor — the stamps stay at "now" (max(now, floor)). So a heal that
    # ran genuinely refreshes the verdict; the floor only ever RAISES the stamp, never lowers it.
    sp, ap, jp = _seed(tmp_path)
    _patch_paths(monkeypatch, tmp_path, sp, ap, jp)
    past = "2000-01-01T00:00:00-07:00"
    refresh_after_content_edit(
        "JOB-700", "resume", "current_bullets.0", "cut analysis time with simulation",
        manifest_path=sp, gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER",
        quality_llm=lambda p: json.dumps({"calibration": []}), audit_floor_ts=past)
    rec = next(r for r in json.loads(sp.read_text(encoding="utf-8")) if r["job_id"] == "JOB-700")
    # both stamps are recent (this decade), not dragged back to the year 2000 floor
    assert rec["audit"]["refreshed_at"] > "2020-01-01"
    assert rec["quality_audit"]["refreshed_at"] > "2020-01-01"


def test_clean_edit_does_not_regenerate_polish_advisories(tmp_path, monkeypatch):
    # The four polish dimension NOTES/FIXES from staging must survive a clean edit untouched (no
    # treadmill). Seed a prior quality_audit with a distinctive fix on `fit`.
    prior_q = {
        "verdict": "FLAG",
        "dimensions": {
            "jd_coverage": {"score": 5, "note": "", "fix": ""},
            "fit": {"score": 3, "note": "sharpen", "fix": "NAME-THE-TEAM-MARKER"},
            "specificity": {"score": 4, "note": "", "fix": ""},
            "voice": {"score": 4, "note": "", "fix": ""}},
        "calibration": [], "judge_ran": True, "summary": "FLAG",
        "refreshed_at": "2026-06-10T09:00:00-07:00"}
    sp, ap, jp = _seed(tmp_path, quality_audit=prior_q)
    _patch_paths(monkeypatch, tmp_path, sp, ap, jp)
    refresh_after_content_edit(
        "JOB-700", "resume", "current_bullets.0", "cut analysis time with simulation",
        manifest_path=sp, gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER",
        quality_llm=lambda p: json.dumps({"calibration": []}))
    rec = next(r for r in json.loads(sp.read_text(encoding="utf-8")) if r["job_id"] == "JOB-700")
    qa = rec["quality_audit"]
    assert qa["dimensions"]["fit"]["fix"] == "NAME-THE-TEAM-MARKER"  # advisory NOT regenerated
    assert qa["verdict"] == "FLAG"                                   # follows the frozen 3
