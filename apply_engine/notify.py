# -*- coding: utf-8 -*-
"""Engine-side Telegram notifier for an open human_blocker (Feature B, Phase 3).

When the apply engine writes a record carrying an OPEN `human_blocker` (a halt Sam must
action), it fires ONE Telegram line so he knows a card is waiting on the dashboard. This is the
ENGINE's notify (design doc §5a, M2): it is fired atomically with the blocker write under the
SAME filemutex the engine already owns for the manifest, NOT a server helper. The server NEVER
sends — it only renders the dashboard badge. One owner removes the cross-process idempotency race.

HARD Telegram rules (MEMORY.md + scheduled-task convention), all enforced here:
  * EXACTLY ONE message per blocker event. Idempotent on `notified.telegram` (and only when the
    blocker is OPEN, `answered_at is None`) — a second stage of the same blocker re-sends nothing.
  * Build the URL with `urllib.parse.urlencode`.
  * Check the response `ok:true`. Do NOT retry on failure.
  * NEVER raise out of the notifier — every failure path returns False (fail-closed). A missing
    creds file, a missing token key, or a network error must never crash the stage.

The real network send is injectable via `send_fn` so tests NEVER hit the Telegram API.
"""
import json
import urllib.parse
import urllib.request

from . import config

# Sam's Telegram chat_id (MEMORY.md / global CLAUDE.md). The bot token is read from
# brief_config.json (key `telegram_bot_token`); the chat_id is a stable constant.
_CHAT_ID = "8698619324"
# The brief_config.json key holding the bot token (verified present 2026-06-11). If this key is
# absent we fail closed (no send, return False) rather than guess an endpoint.
_TOKEN_KEY = "telegram_bot_token"


def _brief_config_path():
    """Path to brief_config.json in the shared data hub (config resolves ARIA_CORE_DATA)."""
    return config.ARIA_DATA / "brief_config.json"


def _load_token():
    """Return the Telegram bot token from brief_config.json, or '' if the file/key is absent or
    unreadable. NEVER raises — a missing creds file or key is a fail-closed no-send, not a crash."""
    try:
        data = json.loads(_brief_config_path().read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    tok = data.get(_TOKEN_KEY)
    return tok.strip() if isinstance(tok, str) else ""


def _real_send(token: str, params: dict) -> bool:
    """Actually POST sendMessage via urllib. Returns True only on HTTP 200 + JSON `ok:true`.
    Any exception (network, timeout, bad JSON) -> False. Never retries, never raises."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(params).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return bool(isinstance(payload, dict) and payload.get("ok") is True)
    except Exception:
        return False  # fire-and-forget: a failed send never crashes the stage, never retries


def _blocker_of(record_or_blocker):
    """Accept either a full manifest record (with a `human_blocker` key) or a bare blocker dict.
    Returns the blocker dict, or None if there isn't one."""
    if not isinstance(record_or_blocker, dict):
        return None
    # A full record carries the blocker under "human_blocker"; a bare blocker has its own keys.
    if isinstance(record_or_blocker.get("human_blocker"), dict):
        return record_or_blocker["human_blocker"]
    if "tier" in record_or_blocker or "category" in record_or_blocker or "id" in record_or_blocker:
        return record_or_blocker
    return None


def _message(record_or_blocker, blocker: dict) -> str:
    """The ONE message line (design §5a):
        ⏸ Apply HALT — {company} {role}: {question}  → dashboard /apply-queue/{job_id}
    company/role are read from the record when present (a bare blocker carries page_state only),
    job_id from the blocker id-suffix fallback / record. Best-effort, never raises."""
    rec = record_or_blocker if isinstance(record_or_blocker, dict) else {}
    company = (rec.get("company") or "").strip()
    role = (rec.get("role") or "").strip()
    job_id = (rec.get("job_id") or "").strip()
    question = (blocker.get("question") or blocker.get("blocking_reason") or "").strip()
    head = " ".join(p for p in (company, role) if p) or "(unknown role)"
    tail = f"  → dashboard /apply-queue/{job_id}" if job_id else "  → dashboard /apply-queue"
    return f"⏸ Apply HALT — {head}: {question}{tail}"


def notify_blocker(record_or_blocker, *, send_fn=None) -> bool:
    """Fire ONE Telegram message for an OPEN human_blocker, idempotently.

    Sends ONLY when the blocker is open (`answered_at is None`) AND not already notified
    (`notified.telegram` is not True). On a successful send returns True; the CALLER is responsible
    for stamping `notified.telegram = True` on the record (atomically, under the manifest filemutex)
    so the flag write can't race a second sender — see mark_notified() / cli.py wiring.

    Returns:
      True  — a message was sent and acknowledged (ok:true). Caller should stamp notified.telegram.
      False — nothing was sent (no open blocker / already notified / missing creds / send failed).

    NEVER raises. `send_fn(token, params) -> bool` is injectable so tests never hit the network;
    defaults to the real urllib sender.
    """
    try:
        blocker = _blocker_of(record_or_blocker)
        if not isinstance(blocker, dict):
            return False  # no structured blocker -> nothing to notify

        # idempotency + open-only gate: skip an answered blocker and one already notified.
        if blocker.get("answered_at") is not None:
            return False
        notified = blocker.get("notified")
        if isinstance(notified, dict) and notified.get("telegram") is True:
            return False

        token = _load_token()
        if not token:
            return False  # missing creds file or token key -> fail closed, no send

        params = {"chat_id": _CHAT_ID, "text": _message(record_or_blocker, blocker)}
        sender = send_fn if send_fn is not None else _real_send
        return bool(sender(token, params))
    except Exception:
        # absolute fail-closed: anything unexpected -> no send, no raise, stage unaffected.
        return False


def mark_notified(manifest_path, job_id: str) -> None:
    """Stamp `human_blocker.notified.telegram = True` on the manifest record for `job_id`,
    atomically under the SAME filemutex every other manifest writer uses (merge-safe: re-read
    FRESH inside the lock, splice ONLY this key, atomic temp-replace). Mirrors
    staged_manifest.attach_audit exactly so it can't clobber a concurrent answer edit.

    Fully forgiving: a missing/corrupt manifest, an unknown job_id, or a record with no blocker is
    a silent no-op (never raises). Call this ONLY after notify_blocker returned True."""
    import os
    from pathlib import Path

    from .filemutex import locked

    mp = Path(manifest_path)
    if not mp.exists():
        return
    try:
        with locked(mp):
            try:
                loaded = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                return  # corrupt manifest -> no-op rather than clobber
            if not isinstance(loaded, list):
                return
            matched = False
            for entry in loaded:
                if isinstance(entry, dict) and entry.get("job_id") == job_id:
                    blk = entry.get("human_blocker")
                    if not isinstance(blk, dict):
                        return  # no blocker on this record -> nothing to stamp
                    nf = blk.get("notified")
                    if not isinstance(nf, dict):
                        nf = {}
                    nf["telegram"] = True
                    blk["notified"] = nf
                    matched = True
                    break
            if not matched:
                return  # unknown job_id -> no-op
            tmp = mp.with_suffix(mp.suffix + ".tmp")
            tmp.write_text(json.dumps(loaded, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, mp)
    except Exception:
        return  # never raise out of the notify path
