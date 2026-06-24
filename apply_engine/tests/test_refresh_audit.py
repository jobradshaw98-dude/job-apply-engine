# -*- coding: utf-8 -*-
"""TDD for refresh_audit: re-running the accuracy audit over a staged application's CURRENT
answers and overwriting the stored verdict.

The whole point is the verdict TRANSITION: a BLOCKED verdict frozen at staging must flip to
PASS once the offending answers are fixed, and stay BLOCKED while they are not. No browser, no
real LLM — the deterministic gate and the judgment LLM are injected as plain callables.
"""
import json

from apply_engine.refresh_audit import audit_answers, refresh, _original_docs


# ---- audit_answers: the PURE scoring core ----

def _drafts(*answers):
    return [{"question": f"Q{i}", "answer": a, "kind": "essay"} for i, a in enumerate(answers)]


def test_clean_answers_pass_no_findings():
    # gate finds nothing, judge finds nothing -> PASS
    out = audit_answers(_drafts("clean supported answer"),
                        gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER")
    assert out["verdict"] == "PASS"
    assert out["gate_blocks"] == 0
    assert out["findings"] == []


def test_gate_block_makes_verdict_blocked():
    out = audit_answers(_drafts("answer with a banned phrase"),
                        gate_fn=lambda t: ["forbidden: clinical"], llm=lambda p: "[]",
                        ledger="LEDGER")
    assert out["verdict"] == "BLOCKED"
    assert out["gate_blocks"] == 1
    assert out["findings"][0]["lens"] == "gate"
    assert out["findings"][0]["severity"] == "BLOCK"


def test_block_severity_judgment_finding_makes_verdict_blocked():
    # A BLOCK-severity judgment finding (fabrication class) -> BLOCKED.
    judged = json.dumps([{"offending_text": "10x faster", "issue": "no ledger support",
                          "fix": "remove the number", "severity": "BLOCK"}])
    out = audit_answers(_drafts("we made it 10x faster"),
                        gate_fn=lambda t: [], llm=lambda p: judged, ledger="LEDGER")
    assert out["verdict"] == "BLOCKED"
    assert out["gate_blocks"] == 0
    assert len(out["findings"]) == 1
    assert out["findings"][0]["lens"] == "fabrication"
    assert out["findings"][0]["severity"] == "BLOCK"
    assert out["findings"][0]["offending_text"] == "10x faster"
    assert out["block_findings"] == 1
    assert out["flag_findings"] == 0


# ---- two-severity policy (2026-06-05): FLAG findings ride along on a PASS ----

def test_all_flag_findings_pass_with_findings_present():
    # An all-FLAG judgment result -> PASS, but the findings list is NON-EMPTY (they ride along).
    judged = json.dumps([
        {"offending_text": "perfectly aligned", "issue": "telling them their value prop",
         "fix": "soften", "severity": "FLAG"},
        {"offending_text": "deeply passionate", "issue": "tone", "fix": "tighten",
         "severity": "FLAG"},
    ])
    out = audit_answers(_drafts("perfectly aligned, deeply passionate"),
                        gate_fn=lambda t: [], llm=lambda p: judged, ledger="LEDGER")
    assert out["verdict"] == "PASS"
    assert len(out["findings"]) == 2
    assert out["block_findings"] == 0
    assert out["flag_findings"] == 2


def test_one_block_among_flags_makes_verdict_blocked():
    judged = json.dumps([
        {"offending_text": "tone thing", "issue": "voice", "fix": "x", "severity": "FLAG"},
        {"offending_text": "used TensorFlow", "issue": "not in ledger", "fix": "remove",
         "severity": "BLOCK"},
    ])
    out = audit_answers(_drafts("tone thing; used TensorFlow"),
                        gate_fn=lambda t: [], llm=lambda p: judged, ledger="LEDGER")
    assert out["verdict"] == "BLOCKED"
    assert out["block_findings"] == 1
    assert out["flag_findings"] == 1


def test_gate_block_still_blocks_even_with_only_flags_from_judge():
    judged = json.dumps([{"offending_text": "x", "issue": "tone", "fix": "y",
                          "severity": "FLAG"}])
    out = audit_answers(_drafts("answer"),
                        gate_fn=lambda t: ["forbidden: clinical"], llm=lambda p: judged,
                        ledger="LEDGER")
    assert out["verdict"] == "BLOCKED"
    assert out["gate_blocks"] == 1


def test_missing_severity_defaults_to_flag():
    # A judged finding with NO severity field -> defaults to FLAG (uncertainty rule) -> PASS.
    judged = json.dumps([{"offending_text": "something", "issue": "unclear", "fix": "z"}])
    out = audit_answers(_drafts("something"),
                        gate_fn=lambda t: [], llm=lambda p: judged, ledger="LEDGER")
    assert out["verdict"] == "PASS"
    assert len(out["findings"]) == 1
    assert out["findings"][0]["severity"] == "FLAG"


def test_unknown_severity_defaults_to_flag():
    judged = json.dumps([{"offending_text": "x", "issue": "y", "fix": "z",
                          "severity": "CRITICAL"}])
    out = audit_answers(_drafts("x"),
                        gate_fn=lambda t: [], llm=lambda p: judged, ledger="LEDGER")
    assert out["verdict"] == "PASS"
    assert out["findings"][0]["severity"] == "FLAG"


def test_transition_blocked_to_pass_when_findings_resolved():
    # Same answers, but now NOTHING is flagged (the user fixed them) -> PASS.
    out = audit_answers(_drafts("now a clean answer"),
                        gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER")
    assert out["verdict"] == "PASS"


def test_empty_answers_are_skipped():
    out = audit_answers([{"question": "Q", "answer": "", "kind": "essay"}],
                        gate_fn=lambda t: ["should never run"], llm=lambda p: "[]",
                        ledger="LEDGER")
    assert out["verdict"] == "PASS"
    assert out["findings"] == []


def test_llm_none_degrades_to_gate_only():
    # No LLM available -> judgment lens skipped, gate still runs.
    out = audit_answers(_drafts("answer"), gate_fn=lambda t: ["block"], llm=None, ledger="")
    assert out["verdict"] == "BLOCKED"
    assert out["gate_blocks"] == 1


# ---- judge_ran semantic: "judgment lens was AVAILABLE and nothing degraded it" ----
# (NOT "ran on >=1 answer" — see FINDING 2). Independent of how many answers existed.

def test_judge_ran_true_when_available_and_answers_judged():
    # llm available + answers judged (even returning []) -> judge_ran True.
    out = audit_answers(_drafts("a clean answer"),
                        gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER")
    assert out["judge_ran"] is True


def test_judge_ran_true_with_zero_answers_when_available():
    # No answers to judge, but the judge WAS available -> vacuously complete -> judge_ran True.
    # This is the standard-fields-only Greenhouse/Lever case: no custom questions exist, so the
    # judge has nothing to flag. Previously judge_ran=False here made the app un-submittable.
    out = audit_answers([{"question": "Q", "answer": "", "kind": "essay"}],
                        gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER")
    assert out["verdict"] == "PASS"
    assert out["findings"] == []
    assert out["judge_ran"] is True


def test_judge_ran_true_with_no_drafts_at_all():
    # An app with NO custom questions at all (empty drafts list) -> judge_ran True (vacuous).
    out = audit_answers([], gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER")
    assert out["verdict"] == "PASS"
    assert out["judge_ran"] is True


def test_judge_ran_false_when_llm_unavailable():
    # llm None (claude -p couldn't be constructed) -> lens unavailable -> judge_ran False.
    out = audit_answers(_drafts("answer"), gate_fn=lambda t: [], llm=None, ledger="LEDGER")
    assert out["judge_ran"] is False


def test_degraded_judge_fails_closed_on_verdict_too():
    # Defense in depth (2026-06-11): when the LLM lens never ran, the stamp must be BLOCKED on
    # the verdict VALUE itself, not a PASS that only judge_ran marks as degraded. A consumer
    # that reads the verdict without judge_ran must also see an un-submittable stamp.
    out = audit_answers(_drafts("answer"), gate_fn=lambda t: [], llm=None, ledger="LEDGER")
    assert out["judge_ran"] is False
    assert out["verdict"] == "BLOCKED"


def test_judge_ran_false_when_ledger_missing():
    # No oracle (empty ledger) -> the judge can't trace claims -> lens unavailable -> False.
    out = audit_answers(_drafts("answer"), gate_fn=lambda t: [], llm=lambda p: "[]", ledger="")
    assert out["judge_ran"] is False


def test_judge_ran_false_when_judge_call_raises():
    # llm available but a per-answer judge call RAISES (claude -p died mid-review) -> a real
    # degradation -> judge_ran False. The gate is still the floor; the lens just doesn't count.
    def boom(_):
        raise RuntimeError("claude -p died")
    out = audit_answers(_drafts("answer"), gate_fn=lambda t: [], llm=boom, ledger="LEDGER")
    assert out["judge_ran"] is False


def test_judge_ran_true_when_response_garbled_but_call_succeeded():
    # A garbled/unparseable RESPONSE is NOT a degradation — the call succeeded, the judge ran,
    # it simply produced nothing parseable. judge_ran stays True.
    out = audit_answers(_drafts("answer"), gate_fn=lambda t: [],
                        llm=lambda p: "not json at all", ledger="LEDGER")
    assert out["judge_ran"] is True


def test_gate_exception_fails_safe_to_block():
    def boom(_):
        raise RuntimeError("gate crashed")
    out = audit_answers(_drafts("answer"), gate_fn=boom, llm=None, ledger="")
    assert out["verdict"] == "BLOCKED"
    assert out["gate_blocks"] == 1


def test_bad_llm_json_is_ignored():
    out = audit_answers(_drafts("answer"), gate_fn=lambda t: [],
                        llm=lambda p: "not json at all", ledger="LEDGER")
    assert out["verdict"] == "PASS"


# ---- _original_docs: scope is never widened past the original verdict ----

def test_original_docs_defaults_to_essay_answer():
    assert _original_docs({}) == {"essay_answer"}
    assert _original_docs({"audit": {"findings": []}}) == {"essay_answer"}


def test_original_docs_reads_prior_finding_docs():
    rec = {"audit": {"findings": [{"doc": "essay_answer"}, {"doc": "resume"}]}}
    assert _original_docs(rec) == {"essay_answer", "resume"}


# ---- refresh: end-to-end against a real manifest file (atomic write via attach_audit) ----

def _write_manifest(tmp_path, custom_qs, prior_audit):
    mp = tmp_path / "staged_applications.json"
    rec = {"job_id": "JOB-131", "company": "Oura", "custom_qs": custom_qs,
           "audit": prior_audit}
    mp.write_text(json.dumps([rec], indent=2), encoding="utf-8")
    return mp


def _read_audit(mp):
    return json.loads(mp.read_text(encoding="utf-8"))[0]["audit"]


def test_refresh_flips_blocked_to_pass_after_fix(tmp_path):
    # Stored verdict is BLOCKED; the CURRENT answer is now clean -> refresh flips to PASS.
    prior = {"app_id": "JOB-131", "verdict": "BLOCKED", "gate_blocks": 0,
             "findings": [{"doc": "essay_answer", "lens": "jd_honesty", "severity": "BLOCK",
                           "offending_text": "old bad line", "issue": "x", "fix": "y"}]}
    mp = _write_manifest(tmp_path, [{"q": "Why Oura?", "status": "drafted",
                                     "value": "a now-clean answer"}], prior)
    out = refresh("JOB-131", manifest_path=mp,
                  gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER")
    assert out["verdict"] == "PASS"
    assert out["app_id"] == "JOB-131"
    assert "refreshed_at" in out
    # persisted atomically
    assert _read_audit(mp)["verdict"] == "PASS"


def test_refresh_stays_blocked_when_unresolved(tmp_path):
    prior = {"app_id": "JOB-131", "verdict": "BLOCKED", "gate_blocks": 0, "findings": []}
    mp = _write_manifest(tmp_path, [{"q": "Why Oura?", "status": "drafted",
                                     "value": "still a 10x faster overstatement"}], prior)
    judged = json.dumps([{"offending_text": "10x faster", "issue": "unsupported", "fix": "cut it",
                          "severity": "BLOCK"}])
    out = refresh("JOB-131", manifest_path=mp,
                  gate_fn=lambda t: [], llm=lambda p: judged, ledger="LEDGER")
    assert out["verdict"] == "BLOCKED"
    assert _read_audit(mp)["verdict"] == "BLOCKED"


def test_refresh_passes_with_style_flags_riding_along(tmp_path):
    # The current answer has only a STYLE (FLAG) note -> PASS, summary names the style flag,
    # and the finding persists on the record (visible but non-blocking).
    prior = {"app_id": "JOB-131", "verdict": "BLOCKED", "gate_blocks": 0, "findings": []}
    mp = _write_manifest(tmp_path, [{"q": "Why Oura?", "status": "drafted",
                                     "value": "an answer that is a touch over-enthusiastic"}],
                         prior)
    judged = json.dumps([{"offending_text": "over-enthusiastic", "issue": "tone",
                          "fix": "tighten", "severity": "FLAG"}])
    out = refresh("JOB-131", manifest_path=mp,
                  gate_fn=lambda t: [], llm=lambda p: judged, ledger="LEDGER")
    assert out["verdict"] == "PASS"
    assert len(out["findings"]) == 1
    assert "style flag" in out["summary"].lower()
    assert _read_audit(mp)["verdict"] == "PASS"


def test_refresh_missing_record_returns_error(tmp_path):
    mp = tmp_path / "staged_applications.json"
    mp.write_text("[]", encoding="utf-8")
    out = refresh("JOB-999", manifest_path=mp, gate_fn=lambda t: [], llm=lambda p: "[]",
                  ledger="L")
    assert "error" in out


def test_refresh_zero_custom_answers_is_submittable(tmp_path):
    # FINDING 2 end-to-end: a standard-fields-only app (no custom questions) re-audited with the
    # judge AVAILABLE -> PASS with judge_ran True -> finish.can_submit's judge gate does NOT block.
    from apply_engine.finish import can_submit
    prior = {"app_id": "JOB-200", "verdict": "BLOCKED", "gate_blocks": 0, "findings": []}
    mp = tmp_path / "staged_applications.json"
    rec = {"job_id": "JOB-200", "company": "Acme", "custom_qs": [], "audit": prior,
           "status": "ready_to_submit", "submitted": False, "needs_sam": [],
           "unfilled_required": [], "work_auth_ok": True}
    mp.write_text(json.dumps([rec], indent=2), encoding="utf-8")
    # Inject a tailored package + a fake quality judge so the SECOND (quality) gate also clears —
    # both gates now stamp on every refresh, and can_submit requires a non-FAIL quality_audit too.
    quality_json = json.dumps({
        "jd_coverage": {"score": 5, "note": "ok"}, "fit": {"score": 5, "note": "ok"},
        "specificity": {"score": 5, "note": "ok"}, "voice": {"score": 5, "note": "ok"},
        "summary": "strong"})
    # include_quality=True: this models the STAGING path (the one place the quality judge runs),
    # so both gates stamp and can_submit can clear the second (quality) gate too.
    out = refresh("JOB-200", manifest_path=mp,
                  gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER",
                  quality_llm=lambda p: quality_json, include_quality=True,
                  application={"job_id": "JOB-200", "resume": {"summary": "s"},
                               "cover": {"paragraphs": ["p"]}},
                  job={"id": "JOB-200", "jd_text": "build things"})
    assert out["verdict"] == "PASS"
    assert out["judge_ran"] is True
    # the freshly-written audit must clear can_submit's accuracy-review gate (not blocked on judge)
    written = json.loads(mp.read_text(encoding="utf-8"))[0]
    assert written["quality_audit"]["verdict"] == "PASS"
    ok, reason = can_submit(written)
    assert ok is True, reason


def test_refresh_judge_unavailable_is_advisory_not_a_submit_block(tmp_path):
    # FLIPPED (2026-06-22 demotion). refresh STILL stamps a degraded audit (BLOCKED, judge_ran False)
    # when the LLM judge is unavailable — that's the refresh module's own behavior, unchanged and
    # asserted below. But the LLM verdict / judge_ran are ADVISORY now: with a clean deterministic
    # gate (gate_blocks 0), can_submit no longer refuses on a degraded judge. We force unavailability
    # via a sentinel llm whose every call raises (refresh uses an injected callable as-is). With zero
    # answers the call never fires, so we give it one answer to exercise the judge path.
    from apply_engine.finish import can_submit

    def dead_judge(_):
        raise RuntimeError("claude -p unavailable")

    prior = {"app_id": "JOB-201", "verdict": "BLOCKED", "gate_blocks": 0, "findings": []}
    mp = tmp_path / "staged_applications.json"
    rec = {"job_id": "JOB-201", "company": "Acme",
           "custom_qs": [{"q": "Why us?", "status": "answered", "value": "a clean answer"}],
           "audit": prior, "status": "ready_to_submit", "submitted": False,
           "needs_sam": [], "unfilled_required": [], "work_auth_ok": True}
    mp.write_text(json.dumps([rec], indent=2), encoding="utf-8")
    out = refresh("JOB-201", manifest_path=mp,
                  gate_fn=lambda t: [], llm=dead_judge, ledger="LEDGER")
    # refresh's own degraded stamp is unchanged: BLOCKED verdict + judge_ran False (advisory).
    assert out["verdict"] == "BLOCKED"
    assert out["judge_ran"] is False
    assert "unavailable" in out["summary"].lower()
    # but it no longer gates submit: clean deterministic gate (gate_blocks 0) -> can_submit PASSES.
    written = json.loads(mp.read_text(encoding="utf-8"))[0]
    ok, reason = can_submit(written)
    assert ok is True, reason


def test_refresh_only_audits_filled_answers(tmp_path):
    # A declined/empty answer must not be audited (drafts_for_audit excludes it).
    prior = {"app_id": "JOB-131", "verdict": "BLOCKED", "gate_blocks": 1,
             "findings": [{"doc": "essay_answer", "severity": "BLOCK"}]}
    mp = _write_manifest(tmp_path, [
        {"q": "answered", "status": "answered", "value": "clean"},
        {"q": "declined", "status": "declined", "value": ""},
    ], prior)
    # gate would block any answer it sees; declined has no value so it is skipped, answered is clean
    out = refresh("JOB-131", manifest_path=mp, gate_fn=lambda t: [], llm=lambda p: "[]",
                  ledger="LEDGER")
    assert out["verdict"] == "PASS"


# ======================================================================================
# include_quality: the QUALITY-JUDGE-ONCE switch (2026-06-10)
# A fabrication-only refresh (default) must NOT regenerate the quality_audit; only an explicit
# include_quality=True (staging / re-judge) recomputes it. This is the treadmill fix.
# ======================================================================================

def _write_manifest_with_quality(tmp_path, custom_qs, prior_audit, quality_audit):
    mp = tmp_path / "staged_applications.json"
    rec = {"job_id": "JOB-300", "company": "Acme", "custom_qs": custom_qs,
           "audit": prior_audit, "quality_audit": quality_audit,
           "status": "ready_to_submit", "submitted": False,
           "needs_sam": [], "unfilled_required": []}
    mp.write_text(json.dumps([rec], indent=2), encoding="utf-8")
    return mp


def test_refresh_default_leaves_quality_audit_byte_identical(tmp_path):
    # include_quality defaults to False: the fabrication audit re-runs and updates, but the stored
    # quality_audit must be left BYTE-IDENTICAL (never recomputed, never wiped). Pin it by
    # serializing the quality_audit before and after and comparing the exact bytes.
    prior = {"app_id": "JOB-300", "verdict": "BLOCKED", "gate_blocks": 0, "findings": []}
    stored_quality = {"verdict": "FLAG", "judge_ran": True,
                      "dimensions": {"jd_coverage": {"score": 4, "note": "n", "fix": ""},
                                     "fit": {"score": 3, "note": "weak", "fix": "tighten"},
                                     "specificity": {"score": 5, "note": "n", "fix": ""},
                                     "voice": {"score": 5, "note": "n", "fix": ""}},
                      "calibration": [], "summary": "one quality pass from staging",
                      "refreshed_at": "2026-06-10T09:00:00-07:00"}
    mp = _write_manifest_with_quality(
        tmp_path, [{"q": "Why us?", "status": "drafted", "value": "now a clean answer"}],
        prior, stored_quality)

    before = json.dumps(json.loads(mp.read_text(encoding="utf-8"))[0]["quality_audit"],
                        sort_keys=True)
    # a quality_llm that, if ever called, would CHANGE the stored audit (so a regression that
    # recomputes shows up as a diff, not a coincidental match).
    def loud_quality_llm(_):
        raise AssertionError("quality judge must NOT run on a fabrication-only refresh")

    out = refresh("JOB-300", manifest_path=mp,
                  gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER",
                  quality_llm=loud_quality_llm)
    # fabrication audit was updated (BLOCKED -> PASS, with a fresh refreshed_at)
    assert out["verdict"] == "PASS"
    written = json.loads(mp.read_text(encoding="utf-8"))[0]
    assert written["audit"]["verdict"] == "PASS"
    # quality_audit untouched — byte-identical
    after = json.dumps(written["quality_audit"], sort_keys=True)
    assert after == before
    assert written["quality_audit"] == stored_quality


def test_refresh_with_quality_true_recomputes_quality_audit(tmp_path):
    # include_quality=True recomputes + stamps a fresh quality_audit (the staging / re-judge path).
    prior = {"app_id": "JOB-300", "verdict": "PASS", "gate_blocks": 0, "findings": []}
    stale_quality = {"verdict": "FLAG", "judge_ran": True, "dimensions": {},
                     "calibration": [], "summary": "stale", "refreshed_at": "old"}
    mp = _write_manifest_with_quality(
        tmp_path, [{"q": "Why us?", "status": "answered", "value": "clean"}],
        prior, stale_quality)
    fresh_quality_json = json.dumps({
        "jd_coverage": {"score": 5, "note": "ok"}, "fit": {"score": 5, "note": "ok"},
        "specificity": {"score": 5, "note": "ok"}, "voice": {"score": 5, "note": "ok"},
        "calibration": [], "summary": "freshly judged"})
    refresh("JOB-300", manifest_path=mp,
            gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER",
            quality_llm=lambda p: fresh_quality_json, include_quality=True,
            application={"job_id": "JOB-300", "resume": {"summary": "s"},
                         "cover": {"paragraphs": ["p"]}},
            job={"id": "JOB-300", "jd_text": "build things"})
    written = json.loads(mp.read_text(encoding="utf-8"))[0]
    assert written["quality_audit"]["verdict"] == "PASS"
    assert written["quality_audit"]["summary"] == "freshly judged"
    assert written["quality_audit"]["summary"] != "stale"


def test_refresh_recheck_calibration_advances_both_stamps_and_freezes_dims(tmp_path):
    """THE STALENESS-WEDGE HEAL (2026-06-11): the dashboard's Re-run accuracy review button must be
    able to clear _content_edit_outdates_audit, whose reference time is min(audit.refreshed_at,
    quality_audit.refreshed_at). A fabrication-only refresh left quality_audit.refreshed_at
    pre-edit, so min() never advanced and the gate refused forever (the infinite-click loop).
    recheck_calibration=True must re-stamp BOTH gates: fabrication fully re-run, quality via the
    SAME calibration-only path refresh_after_content_edit uses (polish dims byte-frozen, NO full
    re-judge: the quality-once rule)."""
    prior = {"app_id": "JOB-310", "verdict": "PASS", "gate_blocks": 0, "findings": [],
             "judge_ran": True, "refreshed_at": "2026-06-08T09:00:00-07:00"}
    stored_quality = {"verdict": "PASS", "judge_ran": True,
                      "dimensions": {"jd_coverage": {"score": 5, "note": "n", "fix": ""},
                                     "fit": {"score": 4, "note": "n", "fix": ""},
                                     "specificity": {"score": 5, "note": "n", "fix": ""},
                                     "voice": {"score": 5, "note": "n", "fix": ""}},
                      "calibration": [], "summary": "one quality pass from staging",
                      "refreshed_at": "2026-06-08T09:00:00-07:00"}
    mp = tmp_path / "staged_applications.json"
    rec = {"job_id": "JOB-310", "company": "Acme",
           "custom_qs": [{"q": "Why us?", "status": "answered", "value": "clean"}],
           "audit": prior, "quality_audit": stored_quality,
           "status": "ready_to_submit", "submitted": False,
           "needs_sam": [], "unfilled_required": []}
    mp.write_text(json.dumps([rec], indent=2), encoding="utf-8")

    calls = []

    def cal_llm(prompt):  # call-tracking fake, never raises (the raising-stub trap)
        calls.append(prompt)
        return json.dumps({"calibration": []})

    out = refresh("JOB-310", manifest_path=mp,
                  gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER",
                  quality_llm=cal_llm, recheck_calibration=True,
                  application={"job_id": "JOB-310", "resume": {"summary": "s"},
                               "cover": {"paragraphs": ["p"]}},
                  job={"id": "JOB-310", "jd_text": "build things"})
    assert out["verdict"] == "PASS"
    written = json.loads(mp.read_text(encoding="utf-8"))[0]
    # BOTH stamps advanced past the pre-edit values
    assert written["audit"]["refreshed_at"] != "2026-06-08T09:00:00-07:00"
    assert written["quality_audit"]["refreshed_at"] != "2026-06-08T09:00:00-07:00"
    # the calibration recheck actually RAN (recomputed, not carried)
    assert calls, "the calibration-only recheck must call the llm"
    # polish dims BYTE-FROZEN from staging (quality-once: no re-judge treadmill)
    assert written["quality_audit"]["dimensions"] == stored_quality["dimensions"]
    assert written["quality_audit"]["verdict"] == "PASS"


def test_refresh_recheck_calibration_introduced_violation_fails(tmp_path):
    """A mis-targeting violation introduced by the edit must surface through the recheck path:
    quality verdict FAIL with the violation recorded, polish dims still frozen."""
    prior = {"app_id": "JOB-311", "verdict": "PASS", "gate_blocks": 0, "findings": [],
             "judge_ran": True, "refreshed_at": "2026-06-08T09:00:00-07:00"}
    stored_quality = {"verdict": "PASS", "judge_ran": True,
                      "dimensions": {"jd_coverage": {"score": 5, "note": "", "fix": ""},
                                     "fit": {"score": 5, "note": "", "fix": ""},
                                     "specificity": {"score": 5, "note": "", "fix": ""},
                                     "voice": {"score": 5, "note": "", "fix": ""}},
                      "calibration": [], "summary": "clean staging pass",
                      "refreshed_at": "2026-06-08T09:00:00-07:00"}
    mp = tmp_path / "staged_applications.json"
    rec = {"job_id": "JOB-311", "company": "Acme", "custom_qs": [],
           "audit": prior, "quality_audit": stored_quality,
           "status": "ready_to_submit", "submitted": False,
           "needs_sam": [], "unfilled_required": []}
    mp.write_text(json.dumps([rec], indent=2), encoding="utf-8")
    cal_fail = json.dumps({"calibration": [{"type": "coding_fluency", "where": "resume",
                                            "evidence": "proficient in Python",
                                            "fix": "frame as AI-native orchestration"}]})
    refresh("JOB-311", manifest_path=mp,
            gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER",
            quality_llm=lambda p: cal_fail, recheck_calibration=True,
            application={"job_id": "JOB-311", "resume": {"summary": "s"},
                         "cover": {"paragraphs": ["p"]}},
            job={"id": "JOB-311", "jd_text": "build things"})
    written = json.loads(mp.read_text(encoding="utf-8"))[0]
    assert written["quality_audit"]["verdict"] == "FAIL"
    assert written["quality_audit"]["calibration"][0]["type"] == "coding_fluency"
    assert written["quality_audit"]["dimensions"] == stored_quality["dimensions"]


def test_refresh_recheck_calibration_skipped_without_prior_quality(tmp_path):
    """No stored quality_audit -> nothing to re-stamp; the recheck must NOT invent one (the
    missing-quality case belongs to --with-quality / FINDING #3 recovery, not this path)."""
    prior = {"app_id": "JOB-312", "verdict": "PASS", "gate_blocks": 0, "findings": []}
    mp = tmp_path / "staged_applications.json"
    rec = {"job_id": "JOB-312", "company": "Acme", "custom_qs": [], "audit": prior,
           "status": "ready_to_submit", "submitted": False,
           "needs_sam": [], "unfilled_required": []}
    mp.write_text(json.dumps([rec], indent=2), encoding="utf-8")

    def loud_llm(_):
        raise AssertionError("calibration recheck must not run with no prior quality_audit")

    refresh("JOB-312", manifest_path=mp,
            gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER",
            quality_llm=loud_llm, recheck_calibration=True)
    written = json.loads(mp.read_text(encoding="utf-8"))[0]
    assert "quality_audit" not in written or not isinstance(written.get("quality_audit"), dict)


def test_main_wires_recheck_calibration_flag(monkeypatch):
    """The server launches `python -m apply_engine.refresh_audit <job> --recheck-calibration`;
    the CLI must parse the flag and pass it through to refresh()."""
    import apply_engine.refresh_audit as ra
    captured = {}

    def fake_refresh(job_id, include_quality=False, recheck_calibration=False):
        captured.update(job_id=job_id, iq=include_quality, rc=recheck_calibration)
        return {"verdict": "PASS", "gate_blocks": 0, "findings": [],
                "refreshed_at": "2026-06-11T10:00:00-07:00"}

    monkeypatch.setattr(ra, "refresh", fake_refresh)
    assert ra.main(["JOB-313", "--recheck-calibration"]) == 0
    assert captured["job_id"] == "JOB-313"
    assert captured["rc"] is True
    assert captured["iq"] is False


def test_refresh_with_quality_true_calibration_fail_is_advisory_not_a_submit_block(tmp_path):
    # FLIPPED (2026-06-22 demotion). A re-judge that returns a calibration violation STILL stamps a
    # FAIL quality_audit (the quality judge's own scoring is unchanged — asserted below), but the
    # quality verdict is ADVISORY now: with a clean deterministic gate, finish.can_submit no longer
    # refuses on it. Only audit.gate_blocks > 0 blocks.
    from apply_engine.finish import can_submit
    prior = {"app_id": "JOB-300", "verdict": "PASS", "gate_blocks": 0, "findings": []}
    mp = _write_manifest_with_quality(
        tmp_path, [{"q": "Why us?", "status": "answered", "value": "clean"}],
        prior, {"verdict": "PASS", "judge_ran": True, "dimensions": {}, "calibration": []})
    # add the fields can_submit needs to otherwise clear
    data = json.loads(mp.read_text(encoding="utf-8"))
    data[0]["url"] = "https://boards.greenhouse.io/acme/jobs/1"
    data[0]["work_auth"] = [{"field": "sponsor", "answer": "No"}]
    mp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    cal_fail_json = json.dumps({
        "jd_coverage": {"score": 5, "note": "ok"}, "fit": {"score": 5, "note": "ok"},
        "specificity": {"score": 5, "note": "ok"}, "voice": {"score": 5, "note": "ok"},
        "calibration": [{"type": "wrong_domain_pitch", "where": "resume",
                         "evidence": "life-sciences fit", "fix": "drop the domain pitch"}],
        "summary": "mis-targeted"})
    refresh("JOB-300", manifest_path=mp,
            gate_fn=lambda t: [], llm=lambda p: "[]", ledger="LEDGER",
            quality_llm=lambda p: cal_fail_json, include_quality=True,
            application={"job_id": "JOB-300", "resume": {"summary": "s"},
                         "cover": {"paragraphs": ["p"]}},
            job={"id": "JOB-300", "jd_text": "enterprise applied AI"})
    written = json.loads(mp.read_text(encoding="utf-8"))[0]
    # quality judge scoring is UNCHANGED: it still stamps FAIL + the calibration finding (advisory).
    assert written["quality_audit"]["verdict"] == "FAIL"
    assert written["quality_audit"]["calibration"][0]["type"] == "wrong_domain_pitch"
    # but it no longer gates submit: clean deterministic gate -> can_submit PASSES.
    ok, reason = can_submit(written)
    assert ok is True, reason
