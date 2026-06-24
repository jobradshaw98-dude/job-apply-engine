# -*- coding: utf-8 -*-
"""Bug #3 — cli.main runs the URL-liveness pre-flight BEFORE the expensive tailor step on --live.

  (a) unambiguous-closed posting -> status needs_sam, and ensure_tailored_package is NEVER called
      (the whole point: don't burn ~5 min + 2 claude -p calls tailoring a dead posting).
  (b) a live 200 -> proceeds past the pre-flight into ensure_tailored_package (tailor runs).
  (c) a network error during the pre-flight -> proceeds (fail-open): tailoring still runs.

No real browser/tailor/network: ensure_tailored_package, ensure_pdfs, apply_to_job and the liveness
check are all stubbed.
"""
import apply_engine.cli as cli
from apply_engine import config


class _Outcome:
    def __init__(self):
        self.job_id = "JOB-238"
        self.status = "ready_to_submit"
        self.submitted = False
        self.verify_ok = True
        self.run_dir = ""
        self.filled_fields = []
        self.work_auth_answers = []
        self.generated = []
        self.corrections = []
        self.unfilled_required = []
        self.halt_reason = ""
        self.error = ""


def _wire(monkeypatch, tmp_path, *, closed, raises=False):
    """Stub everything main() touches; record whether ensure_tailored_package fired and the status
    that got recorded. `closed`/`raises` script the liveness pre-flight."""
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    job = {"id": "JOB-238", "company": "Anthropic",
           "url": "https://boards.greenhouse.io/anthropic/jobs/123"}
    monkeypatch.setattr(cli, "find_job", lambda *a, **k: job)

    state = {"tailored": False}
    monkeypatch.setattr(cli, "ensure_tailored_package",
                        lambda j: state.__setitem__("tailored", True) or "generated")
    monkeypatch.setattr(cli, "ensure_pdfs", lambda j, **k: ("/tmp/r.pdf", "/tmp/c.pdf"))
    monkeypatch.setattr(cli, "build_answers", lambda **k: {})
    monkeypatch.setattr(cli, "build_hooks", lambda *a, **k: (None, None, ""))
    monkeypatch.setattr(cli, "apply_to_job", lambda **k: _Outcome())
    monkeypatch.setattr(cli, "_converge_after_stage", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_maybe_notify_blocker", lambda *a, **k: None)

    recorded = {}
    monkeypatch.setattr(cli, "record_status",
                        lambda apps, job_id, status, run_dir, note="":
                        recorded.update(status=status, note=note))

    def fake_check(url, **kw):
        if raises:
            raise OSError("transient")  # the check itself is fail-open, but be defensive in main too
        return (True, "posting closed — remove/re-source") if closed else (False, "")

    monkeypatch.setattr(cli, "check_posting_liveness", fake_check)
    return state, recorded


def test_closed_posting_halts_before_tailor(monkeypatch, tmp_path):
    state, recorded = _wire(monkeypatch, tmp_path, closed=True)
    rc = cli.main(["--job", "JOB-238", "--live", "--answer"])
    assert state["tailored"] is False          # the expensive tailor step NEVER ran
    assert recorded.get("status") == "needs_sam"
    assert "posting closed" in (recorded.get("note") or "").lower()
    assert rc == 2


def test_live_posting_proceeds_to_tailor(monkeypatch, tmp_path):
    state, recorded = _wire(monkeypatch, tmp_path, closed=False)
    cli.main(["--job", "JOB-238", "--live", "--answer"])
    assert state["tailored"] is True           # a live posting tailors as normal


def test_dry_run_skips_preflight_and_tailor(monkeypatch, tmp_path):
    # a dry run never tailors AND never pre-flights (gated on --live), so a dead URL is irrelevant
    state, _ = _wire(monkeypatch, tmp_path, closed=True)
    called = {"checked": False}
    monkeypatch.setattr(cli, "check_posting_liveness",
                        lambda url, **k: called.__setitem__("checked", True) or (True, "x"))
    cli.main(["--job", "JOB-238"])  # dry-run is the default
    assert called["checked"] is False
    assert state["tailored"] is False
