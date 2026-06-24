# -*- coding: utf-8 -*-
"""Phase 5 — the TTL watchdog + stale-blocker cleanup sweep for the convergence loop.

WHY THIS EXISTS
---------------
The convergence loop (converge.py) runs in a DETACHED engine process. If that process is
SIGKILLed mid-loop (power loss, OOM, the user closing the terminal, an uncaught hard crash that
the non-raising guard never sees), it leaves `convergence.state == "running"` on the record with
no terminal state ever written. The dashboard renders its progress bar from `convergence.state`,
so a dead loop shows a PHANTOM SPINNER forever — the "orphan bar" bug (design §5c #4, §6 #4).

This module ages such an orphaned `running` state to `error` + a generic human_blocker so the bar
can never hang on a dead process. It MIRRORS the existing `_AUDIT_REFRESH_TTL` aging pattern
(aria_server `_audit_refreshing`, `feedback_apply_submit_integrity_gate` BLOCK #4): a `started_at`
older than a generous TTL is a dead process, not a slow one.

It ALSO cleans up STALE human_blockers (design §6 #6): a blocker whose underlying need was already
resolved — its `answer_target` question is no longer unanswered in the record (it got answered or
pruned), or the blocker carries an `answered_at` — must STOP surfacing on the dashboard.

HARD CONSTRAINTS (the brief)
----------------------------
  * ADDITIVE + IDEMPOTENT — a healthy record is untouched; running the sweep twice changes nothing
    after the first age-out (the second pass sees `state == "error"`, not `running`, and a dropped
    blocker is already gone). No double-notify: the sweep DOES NOT notify; it only ages state and
    drops dead blockers (a watchdog-aged error is a dashboard surface, not a Telegram nudge — the
    loop already notified when it had a live blocker; an orphan never got one and a generic
    "interrupted" line would be noise).
  * MERGE-SAFE — every write goes through the cross-process filemutex + re-read-splice-rewrite
    pattern (feedback_apply_queue_concurrency). It NEVER does a naked partial write; it re-reads the
    WHOLE manifest fresh inside the lock, mutates only the matched records, atomic-temp-replaces.
  * FAIL-SAFE — a missing/corrupt manifest is a silent no-op (never raises). A FRESH `running`
    (recent `started_at`) is NEVER aged out (respect started_at + TTL): only a genuinely dead loop
    is touched.
  * TIME IS INJECTABLE — `now` / `ttl_s` are passed so tests are deterministic + offline (construct
    an old `started_at`; never rely on real wall-clock drift).

The watchdog is a CHEAP, on-demand sweep wired where the queue is READ (aria_server's
`apply_queue` list render) — NOT a daemon/thread. One pass per dashboard load is enough: the bar
only needs to be correct when Sam looks at it.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config
from .filemutex import locked

# A `convergence.state == "running"` whose `started_at` is older than this (seconds) is a DEAD
# loop — a hard-killed engine process that never wrote a terminal state. Mirrors the converge
# lock's own 30-min stale window (converge_lock.converge_lock stale=1800) and is_converging's
# 1800s aging: a converge run legitimately takes several minutes (audit + per-fix claude -p ×
# up to max_rounds), so the window is generous so a live multi-round loop is NEVER aged out.
STALE_CONVERGE_TTL = 1800  # 30 min


def _manifest_path() -> Path:
    return config.ARIA_DATA / "staged_applications.json"


def _parse_iso(s) -> Optional[datetime]:
    """Parse a local-ISO timestamp (with or without offset) to a datetime, or None on any failure.
    Mirrors aria_server `_parse_local_iso`: an aware datetime is normalized to naive-local so it
    subtracts cleanly against a naive `now`."""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        dt = datetime.fromisoformat(s.strip())
    except Exception:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _now_naive(now: Optional[datetime]) -> datetime:
    """Resolve the comparison `now` to a naive-local datetime (default: wall clock). Tests pass an
    explicit `now` so aging is deterministic and offline."""
    if now is None:
        return datetime.now()
    if now.tzinfo is not None:
        return now.astimezone().replace(tzinfo=None)
    return now


# --------------------------------------------------------------------------------------
# Stale-running detection (TTL watchdog)
# --------------------------------------------------------------------------------------

def _is_stale_running(conv: dict, now_naive: datetime, ttl_s: int) -> bool:
    """True iff this `convergence` block is a DEAD `running` loop: state=="running" AND its
    `started_at` is parseable AND older than `ttl_s`.

    FAIL-SAFE toward "still running" (NOT stale) when started_at is missing/garbled — a running
    state with no usable timestamp is never aged out, so a live loop whose writer hasn't yet
    stamped started_at (or a legacy record) is never barged. Only a CLEARLY-old timestamp ages."""
    if not isinstance(conv, dict):
        return False
    if (conv.get("state") or "").strip().lower() != "running":
        return False
    started = _parse_iso(conv.get("started_at"))
    if started is None:
        return False  # no usable timestamp -> fail safe to "still running", never age out
    return (now_naive - started).total_seconds() >= ttl_s


def _generic_interrupted_blocker(job_id: str, now_naive: datetime) -> dict:
    """A page-less, generic human_blocker for an orphaned/aged-out converge loop. Mirrors the §1b
    schema (and converge._build_human_blocker's error shape): escalate tier (no answer maps back —
    the loop died, it isn't a question only Sam can answer), category render_fail, a generic
    'convergence interrupted — re-run staging' sentence. answered_at None so it surfaces; notified
    flags False (the sweep does NOT notify — see module docstring)."""
    ts = now_naive.astimezone().isoformat(timespec="seconds")
    ts_compact = "".join(c for c in ts.split("+")[0] if c.isdigit())
    return {
        "id": f"blk_{job_id}_{ts_compact}",
        "tier": "escalate",
        "category": "render_fail",
        "blocking_reason": "convergence loop process died without writing a terminal state",
        "question": "convergence interrupted — re-run staging",
        "options": [],
        "free_text_ok": False,
        "answer_target": {"kind": "none", "qkey": ""},
        "screenshot": "",
        "page_state": {"url": "", "ats": "", "reached": "converge", "fields_filled": 0},
        "finding": None,
        "code_context": {"source": "converge_watchdog.sweep_stale_convergence", "snippet": ""},
        "created_at": ts,
        "answered_at": None,
        "notified": {"telegram": False, "dashboard_badge": False},
    }


# --------------------------------------------------------------------------------------
# Stale-blocker cleanup detection
# --------------------------------------------------------------------------------------

def _qkey(s: str) -> str:
    """Normalize a question string to the SAME qkey the /provide-answer route + the halt classifier
    use ("".join(alnum)[:70], lowercased) so a blocker's answer_target.qkey compares apples-to-apples
    with the record's live custom_qs / needs_sam keys."""
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())[:70]


def _open_need_qkeys(record: dict) -> set:
    """The set of qkeys for needs that are STILL OPEN on this record — i.e. a future answer would
    still map to them. A blocker pointing at a qkey NOT in this set has been resolved (answered or
    pruned) and must stop surfacing.

    Open needs come from two places (matching the answer route's validation surface):
      * needs_sam[] — each entry is an unanswered field the orchestrator left for Sam. An
        entry is open unless it carries an answer/answered_at.
      * custom_qs[] (a.k.a. `generated`) — an UNANSWERED essay/question. An entry whose status is
        'answered' (or that carries a non-empty value/answer + answered_at) is resolved, so its key
        drops out of the open set.
    """
    open_keys = set()

    for n in (record.get("needs_sam") or []):
        if isinstance(n, dict):
            # answered? -> resolved, not open.
            if n.get("answered_at") or (n.get("answer") or n.get("value")):
                continue
            label = n.get("qkey") or n.get("q") or n.get("question") or n.get("label") or n.get("field") or ""
            k = _qkey(n.get("qkey")) if n.get("qkey") else _qkey(label)
            if k:
                open_keys.add(k)
        elif isinstance(n, str):
            k = _qkey(n)
            if k:
                open_keys.add(k)

    for q in (record.get("custom_qs") or record.get("generated") or []):
        if not isinstance(q, dict):
            continue
        status = (q.get("status") or "").strip().lower()
        answered = (status == "answered") or (
            (q.get("value") or q.get("answer")) and q.get("answered_at"))
        if answered:
            continue
        label = q.get("qkey") or q.get("q") or q.get("question") or q.get("label") or ""
        k = _qkey(q.get("qkey")) if q.get("qkey") else _qkey(label)
        if k:
            open_keys.add(k)

    return open_keys


def _is_stale_blocker(blocker: dict, record: dict) -> bool:
    """True iff this human_blocker should STOP surfacing because its underlying need is resolved:

      (a) it carries an `answered_at` (Sam answered it — the answer route stamps this), OR
      (b) its answer_target points at an answerable question whose qkey is no longer in the record's
          OPEN-need set (it got answered or pruned).

    An ESCALATE-tier blocker (answer_target.kind in {none, ""}) has NO answerable question to
    resolve, so (b) never applies — it only clears via (a) `answered_at`. A blocker whose
    answer_target.qkey is empty is likewise only cleared by (a). This is conservative on purpose:
    we never drop a blocker we can't positively prove is resolved (so an open blocker with its need
    still unanswered is always KEPT)."""
    if not isinstance(blocker, dict):
        return False
    # (a) explicitly answered.
    if blocker.get("answered_at") is not None:
        return True
    # (b) its answerable question is no longer open.
    at = blocker.get("answer_target")
    if not isinstance(at, dict):
        return False
    kind = (at.get("kind") or "").strip().lower()
    if kind in ("", "none"):
        return False  # escalate / no mapped question -> only (a) can clear it
    qkey = _qkey(at.get("qkey"))
    if not qkey:
        return False  # no key to test -> can't prove resolved -> keep
    return qkey not in _open_need_qkeys(record)


# --------------------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------------------

def sweep_stale_convergence(manifest_path: Optional[Path] = None,
                            ttl_s: int = STALE_CONVERGE_TTL,
                            now: Optional[datetime] = None) -> dict:
    """ONE merge-safe, idempotent pass over the staged manifest that:

      1. TTL WATCHDOG — ages any `convergence.state == "running"` whose `started_at` is older than
         `ttl_s` to `state="error"` (+ `finished_at`, + an `error` note) AND writes a generic
         `human_blocker` ("convergence interrupted — re-run staging") so the dashboard never shows
         a phantom spinner after the engine process died (the orphan-bar fix, §5c #4 / §6 #4).
      2. STALE-BLOCKER CLEANUP — drops a `human_blocker` whose underlying need was already resolved
         (its answer_target question is no longer unanswered, or it carries answered_at). A resolved
         blocker stops surfacing.

    IDEMPOTENT: a healthy record is untouched; a second pass sees the aged-out state ("error", not
    "running") and the already-dropped blocker, so nothing changes after the first pass. The whole
    sweep is a SINGLE re-read-splice-rewrite under the cross-process filemutex (merge-safe — a
    concurrent answer/content edit on another field is never clobbered).

    FAIL-SAFE: a missing/corrupt manifest, or no changes, is a silent no-op — NEVER raises. Returns
    a small summary dict {aged, blockers_dropped, scanned, changed} for the caller/tests; on a no-op
    it returns zeros.

    `now` / `ttl_s` are injectable so tests are deterministic + offline (pass an old `started_at`
    and a fixed `now`). Defaults: real wall clock + the 30-min STALE_CONVERGE_TTL.
    """
    mp = Path(manifest_path) if manifest_path else _manifest_path()
    summary = {"aged": 0, "blockers_dropped": 0, "scanned": 0, "changed": False}
    if not mp.exists():
        return summary
    now_naive = _now_naive(now)
    try:
        with locked(mp):
            try:
                loaded = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                return summary  # corrupt manifest -> no-op rather than clobber
            if not isinstance(loaded, list):
                return summary

            changed = False
            for entry in loaded:
                if not isinstance(entry, dict):
                    continue
                summary["scanned"] += 1

                # ---- (1) TTL watchdog: age a dead `running` loop ----
                conv = entry.get("convergence")
                if isinstance(conv, dict) and _is_stale_running(conv, now_naive, ttl_s):
                    conv["state"] = "error"
                    conv["finished_at"] = now_naive.astimezone().isoformat(timespec="seconds")
                    conv["error"] = ("convergence interrupted — process died without "
                                     "writing a terminal state")
                    entry["convergence"] = conv
                    # Write a generic blocker so the bar surfaces a real terminal state, UNLESS the
                    # record already carries an OPEN blocker (don't overwrite a real human_blocker
                    # the loop wrote before dying — that one carries the actual context).
                    existing = entry.get("human_blocker")
                    has_open = isinstance(existing, dict) and existing.get("answered_at") is None
                    if not has_open:
                        job_id = entry.get("job_id") or "unknown"
                        entry["human_blocker"] = _generic_interrupted_blocker(job_id, now_naive)
                    summary["aged"] += 1
                    changed = True

                # ---- (2) stale-blocker cleanup: drop a resolved blocker ----
                blk = entry.get("human_blocker")
                if isinstance(blk, dict) and _is_stale_blocker(blk, entry):
                    # Don't drop the generic blocker we JUST wrote in step (1) this same pass — that
                    # one is open/escalate and not stale. _is_stale_blocker already returns False for
                    # it (escalate kind + no answered_at), so this is naturally safe, but the guard
                    # makes the intent explicit.
                    entry.pop("human_blocker", None)
                    summary["blockers_dropped"] += 1
                    changed = True

            if not changed:
                return summary  # idempotent no-op: nothing to write

            tmp = mp.with_suffix(mp.suffix + ".tmp")
            tmp.write_text(json.dumps(loaded, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, mp)
            summary["changed"] = True
            return summary
    except Exception:
        # absolute fail-closed: anything unexpected -> no-op, never raise (a watchdog must never
        # break the dashboard render it's wired into).
        return summary
