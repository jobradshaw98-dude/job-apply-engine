# -*- coding: utf-8 -*-
"""Phase 5 — tests for the TTL watchdog + stale-blocker cleanup sweep, and the m5 kill switch.

Pins the brief's required cases, all DETERMINISTIC + OFFLINE (timestamps are constructed and `now`
is injected — NO real wall-clock drift, NO network, NO claude -p):

  WATCHDOG
    * a `running` record with an OLD started_at -> aged to `error` + a generic blocker.
    * a FRESH `running` (recent started_at) -> untouched.
    * running the sweep twice -> idempotent (no second change).
    * a `running` with a missing/garbled started_at -> fail-safe, NOT aged (never barge a live loop).

  STALE-BLOCKER CLEANUP
    * a blocker whose answer_target question is now answered/absent -> dropped.
    * an OPEN blocker whose need is still unanswered -> kept.
    * a blocker carrying answered_at -> dropped.

  KILL SWITCH (m5)
    * registry pauses APPLY_CONVERGE_LOOP -> converge_quality returns "paused" + runs ZERO
      audit/fix (recording stubs assert zero calls).
    * registry absent -> loop runs normally (fail-safe).
    * registry malformed -> loop runs normally (fail-safe).
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from apply_engine import converge
from apply_engine import converge_watchdog as wd
from apply_engine import paused_registry as pr
from apply_engine import config as _cfg


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------

def _write_manifest(data_dir: Path, records: list) -> Path:
    mp = data_dir / "staged_applications.json"
    mp.write_text(json.dumps(records, indent=2), encoding="utf-8")
    return mp


def _read_record(mp: Path, job_id: str = None) -> dict:
    data = json.loads(mp.read_text(encoding="utf-8"))
    if job_id is None:
        return data[0]
    for r in data:
        if r.get("job_id") == job_id:
            return r
    raise KeyError(job_id)


def _iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(_cfg, "ARIA_DATA", d)
    return d


# ======================================================================================
# WATCHDOG — TTL age-out
# ======================================================================================

def test_old_running_is_aged_to_error_with_blocker(data_dir):
    now = datetime(2026, 6, 12, 12, 0, 0)
    old = now - timedelta(seconds=wd.STALE_CONVERGE_TTL + 600)  # comfortably past TTL
    mp = _write_manifest(data_dir, [{
        "job_id": "JOB-001", "company": "Acme", "role": "AI Eng",
        "convergence": {"state": "running", "rounds": 1, "started_at": _iso(old)},
    }])

    summary = wd.sweep_stale_convergence(manifest_path=mp, now=now)
    assert summary["aged"] == 1
    assert summary["changed"] is True

    rec = _read_record(mp)
    assert rec["convergence"]["state"] == "error"
    assert rec["convergence"].get("finished_at")
    blk = rec["human_blocker"]
    assert blk["category"] == "render_fail"
    assert blk["tier"] == "escalate"
    assert "interrupted" in blk["question"].lower()
    assert blk["answered_at"] is None


def test_fresh_running_is_untouched(data_dir):
    now = datetime(2026, 6, 12, 12, 0, 0)
    fresh = now - timedelta(seconds=30)  # well within TTL
    mp = _write_manifest(data_dir, [{
        "job_id": "JOB-001",
        "convergence": {"state": "running", "rounds": 1, "started_at": _iso(fresh)},
    }])

    summary = wd.sweep_stale_convergence(manifest_path=mp, now=now)
    assert summary["aged"] == 0
    assert summary["changed"] is False

    rec = _read_record(mp)
    assert rec["convergence"]["state"] == "running"  # untouched
    assert "human_blocker" not in rec


def test_sweep_is_idempotent(data_dir):
    now = datetime(2026, 6, 12, 12, 0, 0)
    old = now - timedelta(seconds=wd.STALE_CONVERGE_TTL + 600)
    mp = _write_manifest(data_dir, [{
        "job_id": "JOB-001",
        "convergence": {"state": "running", "rounds": 1, "started_at": _iso(old)},
    }])

    first = wd.sweep_stale_convergence(manifest_path=mp, now=now)
    assert first["aged"] == 1 and first["changed"] is True
    after_first = mp.read_text(encoding="utf-8")

    # Second pass: the state is now "error", not "running" -> nothing to age -> NO change.
    second = wd.sweep_stale_convergence(manifest_path=mp, now=now)
    assert second["aged"] == 0
    assert second["changed"] is False
    assert mp.read_text(encoding="utf-8") == after_first  # byte-identical -> no double-write


def test_running_with_missing_started_at_is_failsafe_not_aged(data_dir):
    now = datetime(2026, 6, 12, 12, 0, 0)
    mp = _write_manifest(data_dir, [{
        "job_id": "JOB-001",
        "convergence": {"state": "running", "rounds": 1},  # no started_at
    }])
    summary = wd.sweep_stale_convergence(manifest_path=mp, now=now)
    assert summary["aged"] == 0  # fail-safe: never age a running loop we can't prove is dead
    assert _read_record(mp)["convergence"]["state"] == "running"


def test_aged_record_does_not_overwrite_existing_open_blocker(data_dir):
    """If the dead loop already wrote a real (open) human_blocker before dying, the watchdog ages
    the state but PRESERVES the richer existing blocker (don't clobber its context)."""
    now = datetime(2026, 6, 12, 12, 0, 0)
    old = now - timedelta(seconds=wd.STALE_CONVERGE_TTL + 600)
    existing = {"id": "blk_real", "tier": "escalate", "category": "captcha",
                "question": "a real captcha halt", "answer_target": {"kind": "none", "qkey": ""},
                "answered_at": None}
    mp = _write_manifest(data_dir, [{
        "job_id": "JOB-001",
        "convergence": {"state": "running", "started_at": _iso(old)},
        "human_blocker": existing,
    }])
    wd.sweep_stale_convergence(manifest_path=mp, now=now)
    rec = _read_record(mp)
    assert rec["convergence"]["state"] == "error"
    assert rec["human_blocker"]["id"] == "blk_real"  # preserved, not overwritten


def test_missing_manifest_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(_cfg, "ARIA_DATA", tmp_path)
    # default path (doesn't exist) -> silent no-op, no raise
    summary = wd.sweep_stale_convergence(now=datetime(2026, 6, 12, 12, 0, 0))
    assert summary == {"aged": 0, "blockers_dropped": 0, "scanned": 0, "changed": False}


def test_corrupt_manifest_is_noop(data_dir):
    mp = data_dir / "staged_applications.json"
    mp.write_text("{not json", encoding="utf-8")
    summary = wd.sweep_stale_convergence(manifest_path=mp, now=datetime(2026, 6, 12, 12, 0, 0))
    assert summary["changed"] is False  # no crash, no write


# ======================================================================================
# STALE-BLOCKER CLEANUP
# ======================================================================================

def test_blocker_dropped_when_question_now_answered(data_dir):
    """A blocker pointing at a custom_q that is now status=answered -> dropped (need resolved)."""
    now = datetime(2026, 6, 12, 12, 0, 0)
    mp = _write_manifest(data_dir, [{
        "job_id": "JOB-001",
        "custom_qs": [{"q": "Why us?", "status": "answered", "value": "because"}],
        "human_blocker": {
            "id": "blk_1", "tier": "answerable", "category": "screening_yesno",
            "question": "Why us?", "answered_at": None,
            "answer_target": {"kind": "custom_q", "qkey": wd._qkey("Why us?")},
        },
    }])
    summary = wd.sweep_stale_convergence(manifest_path=mp, now=now)
    assert summary["blockers_dropped"] == 1
    assert "human_blocker" not in _read_record(mp)


def test_blocker_dropped_when_question_absent(data_dir):
    """A blocker whose answer_target question is no longer present at all -> dropped (pruned)."""
    now = datetime(2026, 6, 12, 12, 0, 0)
    mp = _write_manifest(data_dir, [{
        "job_id": "JOB-001",
        "custom_qs": [{"q": "Some other question", "status": "unanswered"}],
        "needs_sam": [],
        "human_blocker": {
            "id": "blk_1", "tier": "answerable", "category": "missing_value",
            "question": "Vanished question?", "answered_at": None,
            "answer_target": {"kind": "needs_sam", "qkey": wd._qkey("Vanished question?")},
        },
    }])
    summary = wd.sweep_stale_convergence(manifest_path=mp, now=now)
    assert summary["blockers_dropped"] == 1
    assert "human_blocker" not in _read_record(mp)


def test_open_blocker_with_unanswered_need_is_kept(data_dir):
    """An OPEN blocker whose need is STILL unanswered (its qkey is in the open set) -> KEPT."""
    now = datetime(2026, 6, 12, 12, 0, 0)
    q = "Are you authorized to work?"
    mp = _write_manifest(data_dir, [{
        "job_id": "JOB-001",
        "needs_sam": [{"q": q, "status": "unanswered"}],
        "human_blocker": {
            "id": "blk_1", "tier": "answerable", "category": "work_auth",
            "question": q, "answered_at": None,
            "answer_target": {"kind": "needs_sam", "qkey": wd._qkey(q)},
        },
    }])
    summary = wd.sweep_stale_convergence(manifest_path=mp, now=now)
    assert summary["blockers_dropped"] == 0
    assert "human_blocker" in _read_record(mp)  # kept — need still open


def test_blocker_with_answered_at_is_dropped(data_dir):
    """A blocker that already carries answered_at -> dropped regardless of need state."""
    now = datetime(2026, 6, 12, 12, 0, 0)
    mp = _write_manifest(data_dir, [{
        "job_id": "JOB-001",
        "needs_sam": [{"q": "still open", "status": "unanswered"}],
        "human_blocker": {
            "id": "blk_1", "tier": "answerable", "category": "missing_value",
            "question": "still open", "answered_at": _iso(now),
            "answer_target": {"kind": "needs_sam", "qkey": wd._qkey("still open")},
        },
    }])
    summary = wd.sweep_stale_convergence(manifest_path=mp, now=now)
    assert summary["blockers_dropped"] == 1
    assert "human_blocker" not in _read_record(mp)


def test_escalate_blocker_without_answered_at_is_kept(data_dir):
    """An escalate-tier blocker (kind=none) with no answered_at has no answerable question to
    resolve -> KEPT (only an explicit answered_at clears it)."""
    now = datetime(2026, 6, 12, 12, 0, 0)
    mp = _write_manifest(data_dir, [{
        "job_id": "JOB-001",
        "human_blocker": {
            "id": "blk_1", "tier": "escalate", "category": "captcha",
            "question": "captcha", "answered_at": None,
            "answer_target": {"kind": "none", "qkey": ""},
        },
    }])
    summary = wd.sweep_stale_convergence(manifest_path=mp, now=now)
    assert summary["blockers_dropped"] == 0
    assert "human_blocker" in _read_record(mp)


def test_stale_blocker_cleanup_is_idempotent(data_dir):
    now = datetime(2026, 6, 12, 12, 0, 0)
    mp = _write_manifest(data_dir, [{
        "job_id": "JOB-001",
        "custom_qs": [{"q": "Why us?", "status": "answered", "value": "x"}],
        "human_blocker": {
            "id": "blk_1", "tier": "answerable", "category": "screening_yesno",
            "question": "Why us?", "answered_at": None,
            "answer_target": {"kind": "custom_q", "qkey": wd._qkey("Why us?")},
        },
    }])
    wd.sweep_stale_convergence(manifest_path=mp, now=now)
    after = mp.read_text(encoding="utf-8")
    second = wd.sweep_stale_convergence(manifest_path=mp, now=now)
    assert second["blockers_dropped"] == 0 and second["changed"] is False
    assert mp.read_text(encoding="utf-8") == after


# ======================================================================================
# m5 KILL SWITCH
# ======================================================================================

def _stage_ready_record(data_dir: Path) -> Path:
    """A minimal staged record at the stage-success brink so converge_quality would normally run."""
    mp = data_dir / "staged_applications.json"
    mp.write_text(json.dumps([{
        "job_id": "JOB-001", "company": "Acme", "role": "AI Eng",
        "status": "ready_to_submit",
    }], indent=2), encoding="utf-8")
    return mp


def _write_registry(data_dir: Path, entries):
    rp = data_dir / "paused_registry.json"
    rp.write_text(json.dumps({"entries": entries}, indent=2), encoding="utf-8")
    return rp


def test_kill_switch_paused_returns_paused_and_runs_nothing(data_dir):
    mp = _stage_ready_record(data_dir)
    _write_registry(data_dir, [{
        "id": "APPLY_CONVERGE_LOOP", "type": "feature", "name": "Apply Convergence Loop",
        "status": "paused", "reason": "test", "do_not_alert": True,
    }])

    audit_calls, fix_calls = [], []

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        audit_calls.append(job_id)

    def fix_fn(job_id, finding):
        fix_calls.append(finding)
        return "x"

    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn, fix_fn=fix_fn, manifest_path=mp)
    assert tag == "paused"
    assert audit_calls == []   # ZERO audit calls
    assert fix_calls == []     # ZERO fix calls
    # convergence state stamped "paused" so the dashboard shows a deliberate pause.
    assert _read_record(mp)["convergence"]["state"] == "paused"


def test_kill_switch_absent_registry_runs_normally(data_dir):
    """No registry file at all -> fail-safe: loop runs (NOT paused)."""
    mp = _stage_ready_record(data_dir)
    # ensure NO registry exists
    rp = data_dir / "paused_registry.json"
    assert not rp.exists()
    assert pr.is_loop_paused() is False

    audit_calls = []

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        audit_calls.append(job_id)
        # mark clean so the loop converges immediately (keeps the test offline + bounded)
        data = json.loads(mp.read_text(encoding="utf-8"))
        data[0]["audit"] = {"verdict": "PASS", "judge_ran": True, "findings": [],
                            "gate_blocks": 0, "block_findings": 0}
        data[0]["quality_audit"] = {"verdict": "PASS", "judge_ran": True, "calibration": []}
        mp.write_text(json.dumps(data, indent=2), encoding="utf-8")

    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn,
                                    fix_fn=lambda *a: "x", manifest_path=mp)
    # NOT "paused": the loop actually ran (audit was called at least once).
    assert tag != "paused"
    assert len(audit_calls) >= 1


def test_kill_switch_malformed_registry_runs_normally(data_dir):
    """A malformed registry -> fail-safe: NOT paused (never block apply on a bad config file)."""
    mp = _stage_ready_record(data_dir)
    rp = data_dir / "paused_registry.json"
    rp.write_text("{ this is : not valid json", encoding="utf-8")
    assert pr.is_loop_paused() is False

    audit_calls = []

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        audit_calls.append(job_id)
        data = json.loads(mp.read_text(encoding="utf-8"))
        data[0]["audit"] = {"verdict": "PASS", "judge_ran": True, "findings": [],
                            "gate_blocks": 0, "block_findings": 0}
        data[0]["quality_audit"] = {"verdict": "PASS", "judge_ran": True, "calibration": []}
        mp.write_text(json.dumps(data, indent=2), encoding="utf-8")

    tag = converge.converge_quality("JOB-001", audit_fn=audit_fn,
                                    fix_fn=lambda *a: "x", manifest_path=mp)
    assert tag != "paused"
    assert len(audit_calls) >= 1


def test_kill_switch_wrong_status_does_not_pause(data_dir):
    """An APPLY_CONVERGE_LOOP entry with a NON-active status (e.g. 'active') does NOT pause."""
    _write_registry(data_dir, [{"id": "APPLY_CONVERGE_LOOP", "status": "active"}])
    assert pr.is_loop_paused() is False


def test_kill_switch_other_entry_does_not_pause(data_dir):
    """A paused entry for a DIFFERENT id never pauses the converge loop."""
    _write_registry(data_dir, [{"id": "WF-08", "status": "paused"}])
    assert pr.is_loop_paused() is False
