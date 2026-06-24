# -*- coding: utf-8 -*-
"""Unit tests for apply_engine.filemutex — the no-dep cross-process file mutex.

Proven here:
  1. Mutual exclusion: many threads contending on the same target are SERIALIZED — at no point
     are two holders inside the critical section at once (no double-entry).
  2. A stale lockfile (mtime older than `stale`) is BROKEN so a crashed holder can't wedge edits.
  3. A fresh (non-stale) lock is NOT stolen — a waiter respects a live holder and times out.
  4. A raising body still releases the lock (finally), so the next waiter proceeds.
  5. Distinct targets don't block each other.

Threads are sufficient to exercise the lock's logic (it's a cross-process file lock, so threads
share the same lockfile path exactly as separate processes would). The concurrency CORRECTNESS
of the actual manifest merge is proven separately in test_regen_concurrency.py with subprocesses.
"""
import os
import threading
import time

import pytest

from apply_engine.filemutex import LockTimeout, _lock_path, locked


def test_mutual_exclusion_no_double_entry(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("{}", encoding="utf-8")

    inside = []          # current number of threads inside the critical section
    max_inside = []      # peak observed
    lock_for_counter = threading.Lock()

    def worker():
        with locked(target, timeout=10, stale=120):
            with lock_for_counter:
                inside.append(1)
                max_inside.append(len(inside))
            time.sleep(0.02)  # hold long enough that a racing thread would overlap if it could
            with lock_for_counter:
                inside.pop()

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # If the mutex works, at no instant were two threads inside at once.
    assert max(max_inside) == 1
    # Lock released cleanly at the end.
    assert not _lock_path(target).exists()


def test_serialized_writes_all_land(tmp_path):
    """A read-modify-write of a shared counter under the lock must not lose increments — the
    classic lost-update the mutex prevents. Without the lock this races and undercounts."""
    target = tmp_path / "counter.json"
    target.write_text("0", encoding="utf-8")

    # This asserts the no-lost-update GUARANTEE (the mutex never lets two readers interleave a
    # read-modify-write), NOT lock latency. `locked` RAISES LockTimeout when a WAITER can't acquire
    # in time — that means "didn't get the lock," not "the lock failed." The old test let that
    # exception escape the thread, dropping an increment and flaking the count whenever the host was
    # loaded (leftover browser procs mid-suite starved a holder past the deadline). We retry on
    # LockTimeout so a transient host stall can never undercount: the count is now host-independent
    # and can only be wrong if the mutex genuinely allows a lost update (a real race). stale is well
    # above any hold time so a slow thread's live lock is never mistaken for crashed.
    n_threads, n_iters = 4, 20

    def bump():
        for _ in range(n_iters):
            while True:
                try:
                    with locked(target, timeout=30, stale=600):
                        n = int(target.read_text(encoding="utf-8"))
                        time.sleep(0.0005)  # widen the race window
                        target.write_text(str(n + 1), encoding="utf-8")
                    break
                except LockTimeout:
                    continue  # couldn't acquire (host stall) — retry; never drop this increment

    threads = [threading.Thread(target=bump) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert int(target.read_text(encoding="utf-8")) == n_threads * n_iters


def test_stale_lock_is_broken(tmp_path):
    """A lockfile older than `stale` is presumed crashed and stolen, so edits aren't wedged."""
    target = tmp_path / "state.json"
    lockpath = _lock_path(target)
    lockpath.write_text("99999 0\n", encoding="utf-8")  # simulate a leftover lock from a dead pid
    # Backdate its mtime well past the stale window.
    old = time.time() - 600
    os.utime(str(lockpath), (old, old))

    acquired = {"ok": False}
    with locked(target, timeout=5, stale=120):
        acquired["ok"] = True
    assert acquired["ok"] is True
    assert not lockpath.exists()


def test_fresh_lock_is_not_stolen(tmp_path):
    """A live (fresh-mtime) lock must NOT be stolen — a waiter respects it and times out."""
    target = tmp_path / "state.json"
    lockpath = _lock_path(target)
    lockpath.write_text(f"{os.getpid()} {time.time():.3f}\n", encoding="utf-8")  # fresh holder

    with pytest.raises(LockTimeout):
        with locked(target, timeout=0.3, stale=120):
            pass  # should never get here — the fresh lock blocks us
    # The real holder's lockfile is left intact (we didn't steal it).
    assert lockpath.exists()
    lockpath.unlink()


def test_raising_body_releases_lock(tmp_path):
    """An exception inside the locked block still unlinks the lockfile (finally), so the next
    acquirer isn't wedged forever."""
    target = tmp_path / "state.json"
    with pytest.raises(ValueError):
        with locked(target, timeout=5, stale=120):
            raise ValueError("boom")
    assert not _lock_path(target).exists()
    # Re-acquire proves the lock is free.
    with locked(target, timeout=5, stale=120):
        pass


def test_distinct_targets_do_not_block(tmp_path):
    """Two different target paths use different lockfiles, so holding one never blocks the other."""
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    with locked(a, timeout=1, stale=120):
        # b must be acquirable immediately even while a is held.
        with locked(b, timeout=1, stale=120):
            assert _lock_path(a).exists()
            assert _lock_path(b).exists()
