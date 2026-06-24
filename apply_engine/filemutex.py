# -*- coding: utf-8 -*-
"""No-dependency, cross-process file mutex for the apply-queue manifests.

Why this exists
---------------
The apply dashboard launches several short-lived DETACHED processes (regen_answer,
regen_content, refresh_audit, finish) that each read-modify-write a shared JSON manifest
(staged_applications.json / applications.json). Two of those processes running at once both
load the WHOLE file at start, spend 60-90s in an LLM call, then whole-file-write at the end —
so the LAST writer clobbers the first writer's edit (a classic lost update).

The fix has two halves and BOTH are required:
  1. this mutex — serialize the actual disk write across processes, and
  2. merge-safe writes (re-read fresh under the mutex, splice in only this run's delta) so
     even serialized writers don't carry a 60-90s-stale snapshot over a sibling's edit.

This module is half 1. It is intentionally tiny and dependency-free: it uses an exclusive
file-creation primitive (os.O_CREAT | os.O_EXCL) that is atomic on Windows and POSIX alike,
so no filelock/portalocker package is needed. It is a cooperative lock — only code that calls
`locked()` on the same target path is serialized; it does not lock the OS file handle itself.

Usage
-----
    from apply_engine.filemutex import locked
    with locked(manifest_path):
        data = json.loads(manifest_path.read_text(...))   # re-read FRESH inside the lock
        ... splice in only this run's delta ...
        tmp = manifest_path.with_suffix(".json.tmp")
        tmp.write_text(...); tmp.replace(manifest_path)    # atomic write

Staleness / crash safety
------------------------
A holder that crashes (power loss, killed process) would otherwise leave its lockfile behind
and wedge every future edit. So if the existing lockfile's mtime is older than `stale` seconds,
a waiter STEALS it (removes it and tries to recreate it). `stale` must be comfortably longer
than the longest legitimate hold (an LLM regen is ~60-90s) so we never steal a lock that is
still doing real work — the default is 120s.
"""
import os
import time
from contextlib import contextmanager
from pathlib import Path


class LockTimeout(RuntimeError):
    """Raised when the lock could not be acquired within `timeout` seconds."""


def _lock_path(target_path):
    """The sidecar lockfile for a target: `<target>.lock` (e.g. staged_applications.json.lock)."""
    return Path(str(target_path) + ".lock")


def _try_create(lockpath):
    """Atomically create the lockfile, returning True if WE created it (we now hold the lock),
    False if it already existed (someone else holds it). os.O_EXCL makes the create-or-fail
    atomic on every platform, which is the whole point — no read-then-create TOCTOU window."""
    try:
        fd = os.open(str(lockpath), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    # Stamp the holder's pid + acquire time so a debugging human can see who holds it. Best
    # effort — the lock's correctness rests on the file's existence, not its contents.
    try:
        os.write(fd, f"{os.getpid()} {time.time():.3f}\n".encode("utf-8"))
    finally:
        os.close(fd)
    return True


def _is_stale(lockpath, stale):
    """True if the lockfile exists and its mtime is older than `stale` seconds — i.e. the holder
    very likely died. A missing file is NOT stale (it's just gone)."""
    try:
        age = time.time() - os.path.getmtime(str(lockpath))
    except OSError:
        return False
    return age > stale


@contextmanager
def locked(target_path, timeout=30, stale=120, poll=0.05):
    """Serialize access to `target_path` across processes via a `<target>.lock` sidecar.

    Acquire by atomically creating the lockfile; if it already exists, retry every `poll`
    seconds until `timeout` is reached (then raise LockTimeout). If the existing lockfile is
    older than `stale` seconds the holder is presumed crashed and the lock is STOLEN (unlinked,
    then re-contended for). Release unlinks the lockfile in a finally so a raising body never
    wedges the lock.

    `stale` MUST exceed the longest legitimate hold (LLM regens run ~60-90s) so a live holder
    is never stolen from. `timeout` is how long a *waiter* will block before giving up.
    """
    lockpath = _lock_path(target_path)
    # Make sure the parent dir exists so os.open can create the sidecar (mirrors the manifest
    # writers, which mkdir the parent). Harmless if it already exists.
    try:
        lockpath.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    deadline = time.time() + timeout
    acquired = False
    while True:
        if _try_create(lockpath):
            acquired = True
            break
        # Held by someone else. If their lockfile is stale, steal it: unlink and immediately
        # re-contend (another waiter may steal-and-create first, so we LOOP rather than assume
        # the steal hands us the lock).
        if _is_stale(lockpath, stale):
            try:
                os.unlink(str(lockpath))
            except OSError:
                pass  # someone else unlinked/stole it first — fine, just re-contend
            continue
        if time.time() >= deadline:
            raise LockTimeout(
                f"could not acquire lock on {target_path} within {timeout}s "
                f"(held by another process; lockfile {lockpath})"
            )
        time.sleep(poll)

    try:
        yield
    finally:
        if acquired:
            try:
                os.unlink(str(lockpath))
            except OSError:
                pass  # already stolen/removed — nothing to do
