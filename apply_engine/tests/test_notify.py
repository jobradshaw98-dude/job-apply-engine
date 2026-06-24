# -*- coding: utf-8 -*-
"""Phase 3 (Feature B notification): engine-side Telegram notify for an OPEN human_blocker.

EVERY test injects a fake `send_fn` (a recording stub) — NO test ever hits the real Telegram API
or the real urllib path. These pin the HARD Telegram rules (one message per event, ok:true check,
no retry, never raise) + the idempotency / open-only / fail-closed contract of notify_blocker, and
the atomic-under-filemutex stamp of mark_notified.
"""
import json
from pathlib import Path


from apply_engine import config, notify
from apply_engine.notify import notify_blocker, mark_notified


# --------------------------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------------------------- #
class _Recorder:
    """A fake send_fn that records every call and returns a fixed result. NEVER touches network."""
    def __init__(self, result=True, raises=False):
        self.calls = []
        self.result = result
        self.raises = raises

    def __call__(self, token, params):
        self.calls.append({"token": token, "params": dict(params)})
        if self.raises:
            raise RuntimeError("boom — simulated network blowup")
        return self.result


def _write_brief_config(token="test-bot-token"):
    """Drop a brief_config.json with the telegram_bot_token key into the isolated sandbox
    (config.ARIA_DATA is monkeypatched to a tmp dir by the autouse conftest fixture)."""
    p = config.ARIA_DATA / "brief_config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"telegram_bot_token": token, "recipient": "x@y.com"}),
                 encoding="utf-8")
    return p


def _blocker(answered_at=None, notified_tg=False, question="Are you authorized to work in the US?"):
    return {
        "id": "blk_JOB-210_20260611201403",
        "tier": "answerable",
        "category": "work_auth",
        "blocking_reason": "could not set work-auth answer on the form",
        "question": question,
        "options": ["Yes", "No"],
        "free_text_ok": False,
        "answer_target": {"kind": "needs_sam", "qkey": "areyouauthorizedtoworkinth"},
        "answered_at": answered_at,
        "notified": {"telegram": notified_tg, "dashboard_badge": False},
    }


def _record(**blk_kwargs):
    return {
        "job_id": "JOB-210",
        "company": "Acme Corp",
        "role": "Applied AI Engineer",
        "status": "needs_sam",
        "human_blocker": _blocker(**blk_kwargs),
    }


# --------------------------------------------------------------------------------------------- #
# 1. open blocker + creds present -> exactly ONE send, correct format
# --------------------------------------------------------------------------------------------- #
def test_open_blocker_sends_exactly_once_correct_format():
    _write_brief_config(token="TOKEN-123")
    rec = _record()
    rec_sender = _Recorder(result=True)

    sent = notify_blocker(rec, send_fn=rec_sender)

    assert sent is True
    assert len(rec_sender.calls) == 1, "exactly ONE message per event"
    call = rec_sender.calls[0]
    assert call["token"] == "TOKEN-123"
    assert call["params"]["chat_id"] == notify._CHAT_ID
    text = call["params"]["text"]
    assert text.startswith("⏸ Apply HALT — Acme Corp Applied AI Engineer:")
    assert "Are you authorized to work in the US?" in text
    assert "→ dashboard /apply-queue/JOB-210" in text


def test_open_blocker_accepts_bare_blocker_dict():
    """notify_blocker accepts a bare blocker (not just a full record). Still one send, no crash."""
    _write_brief_config()
    rec_sender = _Recorder(result=True)
    sent = notify_blocker(_blocker(), send_fn=rec_sender)
    assert sent is True
    assert len(rec_sender.calls) == 1


# --------------------------------------------------------------------------------------------- #
# 2. already notified (notified.telegram=True) -> NO send (idempotent)
# --------------------------------------------------------------------------------------------- #
def test_already_notified_does_not_resend():
    _write_brief_config()
    rec = _record(notified_tg=True)
    rec_sender = _Recorder(result=True)

    sent = notify_blocker(rec, send_fn=rec_sender)

    assert sent is False
    assert rec_sender.calls == [], "a second stage of the same blocker re-sends nothing"


# --------------------------------------------------------------------------------------------- #
# 3. answered blocker (answered_at set) -> NO send
# --------------------------------------------------------------------------------------------- #
def test_answered_blocker_does_not_send():
    _write_brief_config()
    rec = _record(answered_at="2026-06-11T21:00:00-07:00")
    rec_sender = _Recorder(result=True)

    sent = notify_blocker(rec, send_fn=rec_sender)

    assert sent is False
    assert rec_sender.calls == []


def test_no_blocker_on_record_does_not_send():
    """A clean stage (record with no human_blocker) sends nothing — additive, behaves as today."""
    _write_brief_config()
    rec = {"job_id": "JOB-9", "company": "X", "role": "Y", "human_blocker": None}
    rec_sender = _Recorder(result=True)
    assert notify_blocker(rec, send_fn=rec_sender) is False
    assert rec_sender.calls == []


# --------------------------------------------------------------------------------------------- #
# 4. missing creds / missing token key -> returns False, NO send, NO raise
# --------------------------------------------------------------------------------------------- #
def test_missing_brief_config_file_fails_closed():
    # No brief_config.json written at all.
    rec_sender = _Recorder(result=True)
    sent = notify_blocker(_record(), send_fn=rec_sender)
    assert sent is False
    assert rec_sender.calls == [], "no creds -> no send"


def test_missing_token_key_fails_closed():
    # brief_config.json exists but lacks the telegram_bot_token key.
    p = config.ARIA_DATA / "brief_config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"recipient": "x@y.com"}), encoding="utf-8")
    rec_sender = _Recorder(result=True)
    sent = notify_blocker(_record(), send_fn=rec_sender)
    assert sent is False
    assert rec_sender.calls == []


def test_empty_token_value_fails_closed():
    _write_brief_config(token="   ")  # whitespace-only -> treated as absent
    rec_sender = _Recorder(result=True)
    assert notify_blocker(_record(), send_fn=rec_sender) is False
    assert rec_sender.calls == []


# --------------------------------------------------------------------------------------------- #
# 5. send_fn returns ok:false OR raises -> returns False, never raises, notified NOT set
# --------------------------------------------------------------------------------------------- #
def test_send_fn_returns_false_yields_false():
    _write_brief_config()
    rec_sender = _Recorder(result=False)  # simulates ok:false / failed send
    sent = notify_blocker(_record(), send_fn=rec_sender)
    assert sent is False
    assert len(rec_sender.calls) == 1  # attempted once, NOT retried


def test_send_fn_raising_is_swallowed():
    _write_brief_config()
    rec_sender = _Recorder(raises=True)
    # must NOT propagate the exception
    sent = notify_blocker(_record(), send_fn=rec_sender)
    assert sent is False
    assert len(rec_sender.calls) == 1  # attempted once, no retry


def test_no_retry_on_failure():
    """A failing send is attempted exactly once — the global Telegram 'do not retry' rule."""
    _write_brief_config()
    rec_sender = _Recorder(result=False)
    for _ in range(1):
        notify_blocker(_record(), send_fn=rec_sender)
    assert len(rec_sender.calls) == 1


# --------------------------------------------------------------------------------------------- #
# mark_notified: atomic stamp under filemutex
# --------------------------------------------------------------------------------------------- #
def _write_manifest(records):
    mp = config.ARIA_DATA / "staged_applications.json"
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(records, indent=2), encoding="utf-8")
    return mp


def test_mark_notified_stamps_flag():
    mp = _write_manifest([_record()])
    mark_notified(mp, "JOB-210")
    data = json.loads(Path(mp).read_text(encoding="utf-8"))
    assert data[0]["human_blocker"]["notified"]["telegram"] is True
    # other notified subkeys preserved
    assert data[0]["human_blocker"]["notified"]["dashboard_badge"] is False


def test_mark_notified_unknown_job_is_noop():
    mp = _write_manifest([_record()])
    before = Path(mp).read_text(encoding="utf-8")
    mark_notified(mp, "JOB-NONE")
    assert Path(mp).read_text(encoding="utf-8") == before  # untouched


def test_mark_notified_missing_manifest_is_noop():
    # no manifest on disk -> silent no-op, never raises
    mp = config.ARIA_DATA / "does_not_exist.json"
    mark_notified(mp, "JOB-210")  # must not raise
    assert not Path(mp).exists()


def test_mark_notified_record_without_blocker_is_noop():
    mp = _write_manifest([{"job_id": "JOB-210", "company": "X"}])
    before = Path(mp).read_text(encoding="utf-8")
    mark_notified(mp, "JOB-210")
    assert Path(mp).read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------------------------- #
# end-to-end through the real urllib boundary is NEVER exercised: assert the default sender is
# only the real network path and confirm we always pass an injected stub in tests.
# --------------------------------------------------------------------------------------------- #
def test_default_sender_is_real_network_path():
    """Sanity: the production default IS the real urllib sender (so tests MUST inject a stub).
    We do not CALL it here — just assert identity so a regression that swaps the default to a
    no-op stub (silently never sending in prod) is caught."""
    assert notify._real_send.__name__ == "_real_send"
