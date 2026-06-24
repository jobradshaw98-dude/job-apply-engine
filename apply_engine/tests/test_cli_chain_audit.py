# -*- coding: utf-8 -*-
"""The CLI must AUTO-RUN the application-level accuracy review at the end of a SUCCESSFUL
--answer stage that drafted custom answers, so a freshly-staged card arrives review-ready
instead of locked behind a manual "Re-run accuracy review" click.

These tests cover the CHAINING DECISION (cli.chain_accuracy_review) — when the review fires,
when it must NOT, and that an audit failure never fails the stage. The audit's own scoring is
covered by test_refresh_audit.py; here refresh is the injected seam so no real Claude call runs.
"""

import apply_engine.cli as cli
from apply_engine import config
from apply_engine.staged_manifest import build_record, write_record


# ---- fakes ----

class _Outcome:
    """Minimal stand-in for a JobOutcome: only the attributes the chain + build_record read."""
    def __init__(self, job_id="JOB-131", status="ready_to_submit", generated=None):
        self.job_id = job_id
        self.status = status
        self.generated = generated or []
        # build_record reads these too (all default-empty here)
        self.unfilled_required = []
        self.halt_reason = ""
        self.error = ""
        self.submitted = False
        self.verify_ok = True
        self.run_dir = ""
        self.filled_fields = []
        self.work_auth_answers = []
        self.corrections = []
        self.uploaded_docs = []


def _drafted_qs():
    return [{"q": "Why us?", "kind": "essay", "status": "drafted", "value": "a clean answer"}]


def _stage(tmp_path, monkeypatch, outcome):
    """Point config.ARIA_DATA at tmp_path and write the staged manifest record for `outcome`,
    exactly as apply_to_job would have before main() chains the audit. Returns the manifest path."""
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    mp = tmp_path / "staged_applications.json"
    rec = build_record(outcome, {"id": outcome.job_id, "company": "Acme"}, "2026-06-08T10:00:00")
    write_record(rec, mp)
    return mp


# ---- the review FIRES on a successful --answer stage with drafted answers ----

def test_chain_runs_audit_after_successful_answer_stage(tmp_path, monkeypatch, capsys):
    out = _Outcome(status="ready_to_submit", generated=_drafted_qs())
    mp = _stage(tmp_path, monkeypatch, out)

    called = {}

    def fake_refresh(job_id, manifest_path=None, **kw):
        called["job_id"] = job_id
        called["manifest_path"] = manifest_path
        called["deterministic_only"] = kw.get("deterministic_only")
        called["include_quality"] = kw.get("include_quality")
        return {"verdict": "PASS", "judge_ran": True, "gate_blocks": 0}

    # chain_accuracy_review does `from .refresh_audit import refresh` at call time, so the only
    # binding that matters is apply_engine.refresh_audit.refresh.
    import apply_engine.refresh_audit as ra
    monkeypatch.setattr(ra, "refresh", fake_refresh)

    tag = cli.chain_accuracy_review(out, answered=True)
    # DETERMINISTIC-ONLY at stage (2026-06-22): gate_blocks == 0 -> PASS.
    assert tag == "PASS"
    assert called["job_id"] == "JOB-131"
    assert called["manifest_path"] == mp
    # Stage-end is the deterministic gate ONLY now: deterministic_only=True (no claude -p, the flag
    # also forces the holistic quality pass off inside refresh).
    assert called["deterministic_only"] is True
    assert "accuracy review (deterministic): PASS" in capsys.readouterr().out


def test_chain_reports_blocked_on_deterministic_gate(tmp_path, monkeypatch, capsys):
    # A deterministic gate block (gate_blocks > 0) -> BLOCKED tag naming the count. The (advisory)
    # LLM verdict is irrelevant to the tag now.
    out = _Outcome(generated=_drafted_qs())
    _stage(tmp_path, monkeypatch, out)
    import apply_engine.refresh_audit as ra
    monkeypatch.setattr(ra, "refresh",
                        lambda j, manifest_path=None, **k: {"verdict": "PASS", "gate_blocks": 2})
    tag = cli.chain_accuracy_review(out, answered=True)
    assert tag == "BLOCKED (2 deterministic gate finding(s))"
    assert "accuracy review (deterministic): BLOCKED (2 deterministic gate finding(s))" \
        in capsys.readouterr().out


def test_chain_passes_on_clean_gate_regardless_of_llm_verdict(tmp_path, monkeypatch, capsys):
    # FLIPPED from test_chain_reports_blocked: an LLM-BLOCKED verdict with a CLEAN deterministic
    # gate (no gate_blocks) is advisory now -> the stage tag is PASS.
    out = _Outcome(generated=_drafted_qs())
    _stage(tmp_path, monkeypatch, out)
    import apply_engine.refresh_audit as ra
    monkeypatch.setattr(ra, "refresh",
                        lambda j, manifest_path=None, **k: {"verdict": "BLOCKED", "judge_ran": True,
                                                            "gate_blocks": 0})
    assert cli.chain_accuracy_review(out, answered=True) == "PASS"


def test_chain_no_gate_only_or_degraded_distinction(tmp_path, monkeypatch, capsys):
    # FLIPPED from test_chain_reports_gate_only_when_judge_did_not_run +
    # test_chain_reports_degraded_blocked_distinctly: there is no longer an LLM judge_ran /
    # degraded distinction at stage. judge_ran False with a clean deterministic gate -> PASS.
    out = _Outcome(generated=_drafted_qs())
    _stage(tmp_path, monkeypatch, out)
    import apply_engine.refresh_audit as ra
    monkeypatch.setattr(ra, "refresh",
                        lambda j, manifest_path=None, **k: {"verdict": "BLOCKED", "judge_ran": False,
                                                            "gate_blocks": 0})
    assert cli.chain_accuracy_review(out, answered=True) == "PASS"


# ---- the review MUST NOT fire ----

def test_chain_skips_when_not_answer_run(tmp_path, monkeypatch):
    # No --answer -> the run never drafted answers -> nothing to audit. refresh must NOT be called.
    out = _Outcome(generated=_drafted_qs())
    _stage(tmp_path, monkeypatch, out)
    import apply_engine.refresh_audit as ra
    monkeypatch.setattr(ra, "refresh",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    assert cli.chain_accuracy_review(out, answered=False) is None


def test_chain_skips_on_needs_sam_stage(tmp_path, monkeypatch):
    # A needs_sam stage never produced a review-ready record -> no audit fabricated.
    out = _Outcome(status="needs_sam", generated=_drafted_qs())
    _stage(tmp_path, monkeypatch, out)
    import apply_engine.refresh_audit as ra
    monkeypatch.setattr(ra, "refresh",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    assert cli.chain_accuracy_review(out, answered=True) is None


def test_chain_skips_on_error_stage(tmp_path, monkeypatch):
    out = _Outcome(status="error", generated=_drafted_qs())
    _stage(tmp_path, monkeypatch, out)
    import apply_engine.refresh_audit as ra
    monkeypatch.setattr(ra, "refresh",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    assert cli.chain_accuracy_review(out, answered=True) is None


def test_chain_stamps_clean_gate_when_no_custom_answers(tmp_path, monkeypatch):
    # FLIPPED (2026-06-22 fail-closed fix). A successful standard-fields-only stage (ZERO custom
    # questions) NO LONGER skips — it must still stamp a clean DETERMINISTIC audit so the record
    # carries gate_blocks:0. Otherwise finish.can_submit fail-closes on the missing stamp and an
    # un-auditable standard app could never reach Submit. refresh RUNS (deterministic_only=True),
    # stamping a clean gate -> tag PASS.
    out = _Outcome(status="ready_to_submit", generated=[])
    _stage(tmp_path, monkeypatch, out)
    called = {}

    def fake_refresh(job_id, manifest_path=None, **kw):
        called["deterministic_only"] = kw.get("deterministic_only")
        return {"gate_blocks": 0}

    import apply_engine.refresh_audit as ra
    monkeypatch.setattr(ra, "refresh", fake_refresh)
    assert cli.chain_accuracy_review(out, answered=True) == "PASS"
    assert called["deterministic_only"] is True


def test_chain_stamps_clean_gate_when_only_declined_answers(tmp_path, monkeypatch):
    # FLIPPED (2026-06-22 fail-closed fix). Declined/empty answers filled no custom content, but the
    # stage still must carry a deterministic stamp (gate never ran => can_submit refuses). refresh
    # RUNS deterministic-only and stamps a clean gate -> tag PASS.
    out = _Outcome(status="ready_to_submit",
                   generated=[{"q": "Why?", "kind": "essay", "status": "declined", "value": ""}])
    _stage(tmp_path, monkeypatch, out)
    called = {}

    def fake_refresh(job_id, manifest_path=None, **kw):
        called["deterministic_only"] = kw.get("deterministic_only")
        return {"gate_blocks": 0}

    import apply_engine.refresh_audit as ra
    monkeypatch.setattr(ra, "refresh", fake_refresh)
    assert cli.chain_accuracy_review(out, answered=True) == "PASS"
    assert called["deterministic_only"] is True


def test_chain_does_not_double_audit(tmp_path, monkeypatch):
    # If a verdict is already stamped on the record, the chain leaves it (idempotent).
    out = _Outcome(status="ready_to_submit", generated=_drafted_qs())
    mp = _stage(tmp_path, monkeypatch, out)
    # stamp a prior audit onto the record
    from apply_engine.staged_manifest import attach_audit
    attach_audit(mp, "JOB-131", {"verdict": "PASS", "judge_ran": True})
    import apply_engine.refresh_audit as ra
    monkeypatch.setattr(ra, "refresh",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not re-audit")))
    assert cli.chain_accuracy_review(out, answered=True) == "skipped"


# ---- an audit failure must NEVER fail the stage ----

def test_chain_audit_exception_does_not_crash(tmp_path, monkeypatch, capsys):
    out = _Outcome(status="ready_to_submit", generated=_drafted_qs())
    _stage(tmp_path, monkeypatch, out)

    def boom(*a, **k):
        raise RuntimeError("claude -p exploded")

    import apply_engine.refresh_audit as ra
    monkeypatch.setattr(ra, "refresh", boom)
    # must not raise
    tag = cli.chain_accuracy_review(out, answered=True)
    assert tag.startswith("error:")
    assert "left un-audited" in capsys.readouterr().out


def test_chain_refresh_error_dict_does_not_crash(tmp_path, monkeypatch, capsys):
    # refresh returning an {"error": ...} dict (e.g. record vanished) is handled, not crashed.
    out = _Outcome(status="ready_to_submit", generated=_drafted_qs())
    _stage(tmp_path, monkeypatch, out)
    import apply_engine.refresh_audit as ra
    monkeypatch.setattr(ra, "refresh",
                        lambda *a, **k: {"error": "no staged record"})
    tag = cli.chain_accuracy_review(out, answered=True)
    assert tag.startswith("error:")
    assert "un-audited" in capsys.readouterr().out


# ---- end-to-end: main() chains the audit after an --answer stage ----

def _patch_main_pipeline(monkeypatch, tmp_path, outcome):
    """Stub out main()'s heavy collaborators so it runs deterministically with no browser/LLM,
    while leaving record_status + the chain call REAL. apply_to_job writes the manifest record
    (as in production) so the chain has something to audit."""
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")
    monkeypatch.setattr(config, "APPLICATIONS_JSON", tmp_path / "applications.json")
    (tmp_path / "applications.json").write_text("[]", encoding="utf-8")

    monkeypatch.setattr(cli, "find_job", lambda p, jid: {"id": jid, "company": "Acme"})
    # The live path now guarantees a tailored package before staging. These tests exercise the
    # AUDIT CHAIN, not tailoring, so stub the trigger to a no-op (tailoring is covered by
    # test_cli_tailoring.py). Without this the thin fake-job JD would HALT the run to needs_build.
    monkeypatch.setattr(cli, "ensure_tailored_package", lambda job: "existing")
    monkeypatch.setattr(cli, "ensure_pdfs",
                        lambda job, allow_master=False: (tmp_path / "r.pdf", None))
    monkeypatch.setattr(cli, "build_answers", lambda **k: {})
    monkeypatch.setattr(cli, "build_hooks",
                        lambda answer, job, recon=False: ((lambda p: "x"), (lambda t: []), "FACTS")
                        if answer else (None, None, ""))

    def fake_apply(job, answers, **kw):
        # mimic apply_to_job: write the staged manifest record for this run.
        mp = tmp_path / "staged_applications.json"
        rec = build_record(outcome, job, "2026-06-08T10:00:00")
        write_record(rec, mp)
        return outcome

    monkeypatch.setattr(cli, "apply_to_job", fake_apply)


def test_main_chains_audit_after_answer_stage(tmp_path, monkeypatch, capsys):
    # 2026-06-22: the LLM quality-convergence loop is RETIRED from the stage path. A real --answer
    # stage now runs only the cheap DETERMINISTIC accuracy stamp via chain_accuracy_review (no
    # claude -p, no quality pass), then reconciles status. We assert the deterministic stamp ran
    # the audit with llm=None + include_quality=False, and the stage exit code is still 0.
    out = _Outcome(status="ready_to_submit", generated=_drafted_qs())
    _patch_main_pipeline(monkeypatch, tmp_path, out)

    seen = {}
    import apply_engine.refresh_audit as ra

    def fake_refresh(job_id, manifest_path=None, deterministic_only=False, **k):
        seen["ran"] = job_id
        seen.setdefault("deterministic_only", deterministic_only)
        return {"verdict": "PASS", "judge_ran": True, "gate_blocks": 0}

    monkeypatch.setattr(ra, "refresh", fake_refresh)

    rc = cli.main(["--job", "JOB-131", "--live", "--answer"])
    assert rc == 0                                  # stage exit code reflects the STAGE outcome
    assert seen.get("ran") == "JOB-131"             # the deterministic stamp chained the audit
    assert seen.get("deterministic_only") is True   # deterministic gate only — no claude -p, no quality
    assert "accuracy (deterministic):" in capsys.readouterr().out  # the deterministic stamp ran


def test_main_does_not_chain_audit_without_answer_flag(tmp_path, monkeypatch):
    out = _Outcome(status="ready_to_submit", generated=[])
    _patch_main_pipeline(monkeypatch, tmp_path, out)
    import apply_engine.refresh_audit as ra
    monkeypatch.setattr(ra, "refresh",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    rc = cli.main(["--job", "JOB-131", "--live"])   # no --answer
    assert rc == 0


def test_main_audit_failure_does_not_fail_stage(tmp_path, monkeypatch):
    # The audit blowing up must leave the STAGE exit code intact (0) — Submit just stays locked.
    out = _Outcome(status="ready_to_submit", generated=_drafted_qs())
    _patch_main_pipeline(monkeypatch, tmp_path, out)
    import apply_engine.refresh_audit as ra
    monkeypatch.setattr(ra, "refresh",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit down")))
    rc = cli.main(["--job", "JOB-131", "--live", "--answer"])
    assert rc == 0
