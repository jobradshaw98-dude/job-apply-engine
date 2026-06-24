# -*- coding: utf-8 -*-
"""Phase 4a — tests for the cross-process converge lock + the interlock read helpers.

Proven here:
  * converge_lock(job_id): acquired twice for the SAME job is mutually exclusive (the second blocks
    then fails fast with LockTimeout); two DIFFERENT job_ids don't block each other; the lock
    releases on normal context exit AND on an exception in the body.
  * is_edit_in_flight: reads the answer `edit_request` (within TTL) and content/deck pending markers
    — True/False cases, and the stale-marker-aged-out case.
  * is_converging: reads convergence.state == "running" — True/False, and stale-running aged out.

All unit-testable WITHOUT a live browser or server (the lock is a file lock; threads exercise the
same lockfile path two processes would).
"""
import threading
import time
from datetime import datetime, timedelta

import pytest

from apply_engine import converge_lock as cl
from apply_engine.filemutex import LockTimeout, _lock_path


# --------------------------------------------------------------------------------------
# converge_lock — cross-process mutual exclusion, keyed by job_id
# --------------------------------------------------------------------------------------

def _point_lock_dir(monkeypatch, tmp_path):
    """Redirect the converge lockfiles into tmp_path (they normally live in config.ARIA_DATA)."""
    monkeypatch.setattr(cl.config, "ARIA_DATA", tmp_path)


def test_same_job_is_mutually_exclusive(monkeypatch, tmp_path):
    """A second converge_lock for the SAME job blocks while the first holds, then fails fast."""
    _point_lock_dir(monkeypatch, tmp_path)
    with cl.converge_lock("JOB-210", timeout=5):
        # the lockfile exists while held
        assert _lock_path(cl._converge_lock_path("JOB-210")).exists()
        # a second acquirer of the SAME job can't get in -> times out fast (short timeout)
        with pytest.raises(LockTimeout):
            with cl.converge_lock("JOB-210", timeout=0.3):
                pytest.fail("second converge_lock for the same job must not acquire")
    # released on exit
    assert not _lock_path(cl._converge_lock_path("JOB-210")).exists()


def test_different_jobs_do_not_block(monkeypatch, tmp_path):
    """Two different job_ids use different lockfiles, so one never blocks the other."""
    _point_lock_dir(monkeypatch, tmp_path)
    with cl.converge_lock("JOB-210", timeout=1):
        # a DIFFERENT job acquires immediately even while JOB-210 is held
        with cl.converge_lock("JOB-999", timeout=1):
            assert _lock_path(cl._converge_lock_path("JOB-210")).exists()
            assert _lock_path(cl._converge_lock_path("JOB-999")).exists()


def test_releases_on_normal_exit(monkeypatch, tmp_path):
    """After the context exits normally the lock is free and re-acquirable."""
    _point_lock_dir(monkeypatch, tmp_path)
    with cl.converge_lock("JOB-1", timeout=1):
        pass
    assert not _lock_path(cl._converge_lock_path("JOB-1")).exists()
    # re-acquire proves it's free
    with cl.converge_lock("JOB-1", timeout=1):
        assert _lock_path(cl._converge_lock_path("JOB-1")).exists()


def test_releases_on_exception(monkeypatch, tmp_path):
    """An exception inside the body still releases the lock (filemutex finally)."""
    _point_lock_dir(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        with cl.converge_lock("JOB-2", timeout=1):
            raise ValueError("boom")
    assert not _lock_path(cl._converge_lock_path("JOB-2")).exists()
    # free again
    with cl.converge_lock("JOB-2", timeout=1):
        pass


def test_serialized_across_threads(monkeypatch, tmp_path):
    """Two threads contending on the same job are serialized — never both inside at once."""
    _point_lock_dir(monkeypatch, tmp_path)
    inside = []
    peak = []
    counter_lock = threading.Lock()

    def worker():
        try:
            with cl.converge_lock("JOB-X", timeout=10):
                with counter_lock:
                    inside.append(1)
                    peak.append(len(inside))
                time.sleep(0.02)
                with counter_lock:
                    inside.pop()
        except LockTimeout:
            pass

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max(peak) == 1


# --------------------------------------------------------------------------------------
# is_edit_in_flight — reads the manifest's own cross-process edit markers
# --------------------------------------------------------------------------------------

def _now_iso(offset_s=0):
    return (datetime.now() - timedelta(seconds=offset_s)).astimezone().isoformat(timespec="seconds")


def test_edit_in_flight_answer_true():
    """A custom_q with a non-empty edit_request stamped NOW is in flight."""
    rec = {"custom_qs": [{"q": "Why us?", "edit_request": "tighten it", "edit_request_at": _now_iso()}]}
    assert cl.is_edit_in_flight(rec) is True


def test_edit_in_flight_answer_false_when_no_request():
    """No edit_request anywhere -> not in flight."""
    rec = {"custom_qs": [{"q": "Why us?", "value": "answered", "status": "answered"}]}
    assert cl.is_edit_in_flight(rec) is False


def test_edit_in_flight_answer_stale_aged_out():
    """A non-empty edit_request older than the TTL is a dead process -> NOT in flight."""
    rec = {"custom_qs": [{"q": "Why us?", "edit_request": "x",
                          "edit_request_at": _now_iso(offset_s=cl.EDIT_PENDING_TTL + 60)}]}
    assert cl.is_edit_in_flight(rec) is False


def test_edit_in_flight_answer_missing_stamp_fails_safe():
    """edit_request set but no timestamp -> fail SAFE to 'in flight' (never unlock silently)."""
    rec = {"custom_qs": [{"q": "Why us?", "edit_request": "x"}]}
    assert cl.is_edit_in_flight(rec) is True


def test_edit_in_flight_content_pending_true():
    """A content_edits row whose latest status is 'pending' (within TTL) is in flight."""
    rec = {"content_edits": [{"doc": "cover", "element": "cover.doc",
                              "status": "pending", "ts": _now_iso()}]}
    assert cl.is_edit_in_flight(rec) is True


def test_edit_in_flight_content_settled_false():
    """A content_edits row whose latest status is 'edited' (settled) is NOT in flight."""
    rec = {"content_edits": [{"doc": "cover", "element": "cover.doc",
                              "status": "pending", "ts": _now_iso(offset_s=10)},
                             {"doc": "cover", "element": "cover.doc",
                              "status": "edited", "ts": _now_iso()}]}
    assert cl.is_edit_in_flight(rec) is False


def test_edit_in_flight_content_stale_aged_out():
    """A 'pending' content row older than the TTL is a dead process -> NOT in flight."""
    rec = {"content_edits": [{"doc": "resume", "element": "resume.bullet.1",
                              "status": "pending",
                              "ts": _now_iso(offset_s=cl.EDIT_PENDING_TTL + 60)}]}
    assert cl.is_edit_in_flight(rec) is False


def test_edit_in_flight_empty_record_false():
    assert cl.is_edit_in_flight({}) is False
    assert cl.is_edit_in_flight(None) is False


# --------------------------------------------------------------------------------------
# is_converging — reads convergence.state == "running"
# --------------------------------------------------------------------------------------

def test_is_converging_true():
    rec = {"convergence": {"state": "running", "started_at": _now_iso()}}
    assert cl.is_converging(rec) is True


def test_is_converging_false_when_converged():
    rec = {"convergence": {"state": "converged", "started_at": _now_iso()}}
    assert cl.is_converging(rec) is False


def test_is_converging_false_when_absent():
    assert cl.is_converging({}) is False
    assert cl.is_converging({"convergence": None}) is False
    assert cl.is_converging(None) is False


def test_is_converging_stale_running_aged_out():
    """A 'running' state older than the converge stale window is a dead loop -> NOT converging."""
    rec = {"convergence": {"state": "running", "started_at": _now_iso(offset_s=2000)}}
    assert cl.is_converging(rec) is False


def test_is_converging_running_no_stamp_fails_safe():
    """state running but no started_at -> fail SAFE to 'converging' (never barge a live loop)."""
    rec = {"convergence": {"state": "running"}}
    assert cl.is_converging(rec) is True
