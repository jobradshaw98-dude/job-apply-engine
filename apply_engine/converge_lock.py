# -*- coding: utf-8 -*-
"""Phase 4a foundation: a CROSS-PROCESS convergence lock + the read helpers that let the
engine's (future) convergence loop and the server's edit routes interlock on the SAME on-disk
state.

WHY THIS IS FILE-BASED (the load-bearing correctness point)
-----------------------------------------------------------
The convergence loop will run inside the ENGINE process (a detached `claude -p` driver), while
the dashboard edit routes (`/content-edit`, `/provide-answer`, ...) run inside the FLASK server
process. The server's in-memory launch registry (`aria_server.py::_SUBMIT_LOCKS` /
`_acquire_if_none_active`) is Flask-process-only — the engine CANNOT see it, so reusing it for
the converge/edit interlock would be a silent no-op across the process boundary. That is exactly
the lost-update class `feedback_apply_queue_concurrency` exists to prevent.

So the interlock here is built entirely on state BOTH processes can read on disk:

  1. `converge_lock(job_id)` — a `filemutex` lockfile keyed by job_id. Both processes can call it
     and contend on the same sidecar file, so a converge run is serialized against any other
     converge run (and a future server-side caller) cross-process.
  2. `is_edit_in_flight(record)` — reads the manifest record's OWN `custom_qs[].edit_request`
     (answer edits) and `content_edits[]/deck_edits[]` pending markers (content/deck edits). These
     fields are written by the server BEFORE it launches a detached regen and cleared by the
     engine on every terminal outcome — already cross-process via the filemutex'd manifest writes
     (`feedback_apply_queue_concurrency`). The converge loop refuses to START while this is True.
  3. `is_converging(record)` — reads `convergence.state == "running"` off the same record. The
     server edit routes refuse / warn while this is True.

The interlock HOLDS ONLY BECAUSE both processes read the same on-disk manifest + lockfile. There
is no shared in-memory state. If a future change moves any of these signals into process memory,
the interlock silently breaks across the process boundary — do not do that (see §6 #14 of
`docs/superpowers/AUTONOMOUS_CONVERGENCE_AND_COMM_CHANNEL.md`).

NOTE: this module is the Phase 4a FOUNDATION. The convergence loop itself (which acquires the
lock, checks `is_edit_in_flight`, writes `convergence`, and drives the fix rounds) lands in a
later phase. Here we only build the lock primitive + the two read helpers, both unit-testable
without a live browser or server.
"""
from contextlib import contextmanager
from datetime import datetime

from . import config
from .filemutex import locked

# An edit_request / content-edit pending marker older than this (seconds) is a DEAD process — a
# hard-killed regen that left its in-flight marker set. It no longer counts as "in flight" so it
# can't wedge the converge loop forever. Mirrors the server's `_EDIT_PENDING_TTL` intent
# (aria_server `_answer_edit_request_live` / `_pending_row_live`): a stamp older than the TTL is
# a dead process, not a running one. Generous (300s) so a legitimately slow LLM regen is never
# mistaken for dead.
EDIT_PENDING_TTL = 300


def _converge_lock_path(job_id):
    """The lockfile target for a job_id's converge lock. Lives in the SAME dir as the manifests
    (`config.ARIA_DATA`, where filemutex already writes its `<manifest>.lock` sidecars), so the
    engine and the server resolve the identical path. `filemutex.locked` appends the `.lock`
    suffix, so the on-disk sidecar is `converge_<job_id>.lock`. The job_id is sanitized to a safe
    filename stem (job ids are like `JOB-210`; defensive against odd chars)."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(job_id or "")) or "unknown"
    return config.ARIA_DATA / f"converge_{safe}"


@contextmanager
def converge_lock(job_id, timeout=5, stale=None, poll=0.05):
    """CROSS-PROCESS mutex for a single job's convergence run.

    A context manager built on `filemutex.locked`: acquiring it twice for the SAME job_id is
    mutually exclusive (the second caller blocks until `timeout`, then raises `LockTimeout`), but
    two DIFFERENT job_ids use different lockfiles and never block each other. The lock releases on
    normal context exit AND on an exception inside the body (filemutex's `finally`).

    It is keyed by job_id and lives in `config.ARIA_DATA`, so a converge run launched by the
    ENGINE and an edit/converge caller in the SERVER contend on the identical sidecar file — the
    only way the two processes can actually serialize (the in-memory `_SUBMIT_LOCKS` can't, per the
    module docstring).

    `timeout` is short by default (5s): a convergence run can hold the lock for minutes (multiple
    `claude -p` rounds), so a SECOND launcher should fail FAST rather than block for the whole run —
    the caller turns a `LockTimeout` into "a converge run is already active for this job, skipping".

    `stale` defaults to a window comfortably longer than the longest legitimate hold so a live
    multi-round run is never stolen from. A converge run can legitimately run for several minutes
    (audit + per-fix `claude -p` calls × up to max_rounds), so the default stale window is wide
    (30 min). A crashed converge process older than that is presumed dead and its lock is stolen so
    the job isn't wedged forever.
    """
    if stale is None:
        stale = 1800  # 30 min — must exceed the longest legitimate multi-round converge hold
    target = _converge_lock_path(job_id)
    with locked(target, timeout=timeout, stale=stale, poll=poll):
        yield


# --------------------------------------------------------------------------------------
# Read helpers — the cross-process interlock (read the SAME on-disk manifest fields both
# processes write). PURE: dict in, bool out. No I/O, no lock taken (callers already hold the
# manifest fresh, or read it under their own lock).
# --------------------------------------------------------------------------------------

def _marker_live(stamp_iso, ttl=EDIT_PENDING_TTL):
    """True if an in-flight marker stamped `stamp_iso` is still plausibly running (within `ttl`
    seconds). Fail-SAFE toward 'live' when the marker is set but the stamp is missing/garbled
    (legacy data) — a missing stamp must never silently declare an edit settled and let the
    converge loop barge in on top of it. Mirrors aria_server `_answer_edit_request_live`."""
    if not stamp_iso:
        return True  # marker present but no usable timestamp -> fail safe to "still running"
    try:
        marker = datetime.fromisoformat(str(stamp_iso))
    except (ValueError, TypeError):
        return True  # unparseable -> fail safe to "still running"
    now = datetime.now(marker.tzinfo) if marker.tzinfo else datetime.now()
    return (now - marker).total_seconds() < ttl


def is_edit_in_flight(record):
    """True if an answer or content/deck edit is MID-FLIGHT on this staged record.

    The convergence loop consults this and REFUSES TO START while True — it must not race a
    user-launched dashboard edit (an answer rewrite or a resume/cover/deck content edit), which
    would re-open the very fabrication/calibration gates the loop is trying to clear.

    Reads only the record's OWN cross-process fields (written by the server before it launches a
    detached regen, cleared by the engine on every terminal outcome — already cross-process via the
    filemutex'd manifest writes, `feedback_apply_queue_concurrency`):

      * ANSWER edits  — any `custom_qs[].edit_request` non-empty AND within the TTL (a hard-killed
        regen's stale marker is aged out so it can't wedge the loop forever).
      * CONTENT/DECK edits — any `content_edits[]` / `deck_edits[]` entry whose latest status is
        "pending" AND within the TTL. (Content edits live on the applications.json record; when a
        merged record is passed they're present here, else this clause is simply absent — the
        answer-edit clause is the primary staged-manifest signal and is always present.)

    PURE — no I/O. The caller passes a record it already read fresh under the manifest lock.
    """
    if not isinstance(record, dict):
        return False

    # ANSWER edits (staged_applications.json record): a non-empty edit_request within the TTL.
    for q in (record.get("custom_qs") or record.get("generated") or []):
        if not isinstance(q, dict):
            continue
        if (q.get("edit_request") or "").strip() and _marker_live(q.get("edit_request_at")):
            return True

    # CONTENT / DECK edits (applications.json record fields; present only on a merged record).
    # The append-only list's LAST matching (doc, element) row is the current state of that element;
    # a "pending" latest row within the TTL means that element's edit is in flight.
    for list_key in ("content_edits", "deck_edits"):
        rows = record.get(list_key) or []
        if not isinstance(rows, list):
            continue
        latest_by_key = {}
        for e in rows:
            if not isinstance(e, dict):
                continue
            key = (e.get("doc"), e.get("element"))
            latest_by_key[key] = e  # append-only -> last wins
        for e in latest_by_key.values():
            if (e.get("status", "") or "").lower() == "pending" and _marker_live(e.get("ts") or e.get("at")):
                return True

    return False


def is_converging(record):
    """True if a convergence loop is currently RUNNING on this record (`convergence.state ==
    "running"`).

    The SERVER edit routes consult this and refuse / warn while True — launching a dashboard edit
    on top of a live converge run would race the loop's own regen writes. Reads the record's own
    `convergence` block (written by the loop via the merge-safe manifest pattern), so it's
    cross-process: the server sees the engine's running state on disk.

    A "running" state older than the converge stale window is treated as a DEAD loop (a crashed
    converge process that never wrote a terminal state), so it can't wedge the edit routes forever
    — mirrors the lock's own stale-steal. Uses `convergence.started_at` for the age check; a
    missing/garbled timestamp fails SAFE toward "still converging" so a live loop is never barged.

    PURE — no I/O.
    """
    if not isinstance(record, dict):
        return False
    conv = record.get("convergence")
    if not isinstance(conv, dict):
        return False
    if (conv.get("state") or "").strip().lower() != "running":
        return False
    # A "running" loop older than the converge stale window (30 min) is presumed dead — mirror the
    # lock's stale-steal so a crashed loop can't wedge the edit routes. Fail safe to "running" when
    # the timestamp is missing/garbled.
    return _marker_live(conv.get("started_at"), ttl=1800)
