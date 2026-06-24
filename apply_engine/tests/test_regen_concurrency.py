# -*- coding: utf-8 -*-
"""Concurrency tests for the merge-safe answer write (Part 2 + the file mutex of Part 1).

The hazard being proven gone: regen_answer.main loads the WHOLE manifest at start, spends time
in the LLM, then writes back. Two parallel runs that each whole-file-wrote their stale snapshot
would LAST-WRITER-WINS — the first run's edit is clobbered (lost update). With merge-safe writes
(re-read fresh under the file mutex, splice in only this run's delta), two edits to DIFFERENT
questions of the same job must BOTH land, and BOTH their edit_history rows must survive.

How the race is forced: make_claude_llm is monkeypatched to a stub that SLEEPS before returning,
so both threads are guaranteed to have loaded the manifest before either writes — exactly the
window that produced the lost update before the fix. The audit gate is stubbed to pass, and the
self-audit ledger read is neutralized so no second LLM call is needed.

Threads (not subprocesses) are used so the LLM/audit factories can be monkeypatched in-process;
the file mutex serializes on the lockfile identically whether the contenders are threads or
processes (it's an os.O_EXCL file-create lock, not a thread lock).
"""
import json
import threading
import time

from apply_engine import config
from apply_engine import regen_answer


def _seed(tmp_path):
    """One staged record with TWO drafted custom_qs (Q1, Q2) and one unrelated stub record."""
    apps = [
        {
            "job_id": "JOB-700",
            "company": "TestCo",
            "role": "Engineer",
            "status": "ready_to_submit",
            "custom_qs": [
                {"q": "Why this company?", "kind": "essay", "status": "drafted",
                 "value": "Original answer one.", "reason": "", "review_findings": [],
                 "edit_request": "", "edit_history": []},
                {"q": "Describe a hard project.", "kind": "essay", "status": "drafted",
                 "value": "Original answer two.", "reason": "", "review_findings": [],
                 "edit_request": "", "edit_history": []},
            ],
        },
        {"job_id": "JOB-STUB", "company": "StubCo", "note": "must survive untouched"},
    ]
    mp = tmp_path / "staged_applications.json"
    mp.write_text(json.dumps(apps, indent=2), encoding="utf-8")
    (tmp_path / "jobs.json").write_text(json.dumps([{"id": "JOB-700"}]), encoding="utf-8")
    (tmp_path / "claims_ledger.md").write_text("Sam worked at TestCo as an Engineer.\n",
                                               encoding="utf-8")
    return mp


def _wire(tmp_path, monkeypatch, answer_for, sleep_s=0.4):
    """Point config at the tmp manifest and stub the LLM/audit so no real claude runs.

    `answer_for(prompt) -> str` decides the rewrite text from the prompt (lets each thread's
    instruction map to a distinct new value). The LLM SLEEPS first to force the race. The audit
    gate always passes ([] = no blocks). The self-audit second call (judge) returns "[]"."""
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS_JSON", tmp_path / "jobs.json")
    # PKG_DIR.parent / "claims_ledger.md" is read by the self-audit; point PKG_DIR so its parent
    # is tmp_path (where we seeded the ledger). config.PKG_DIR is a Path; give it a child of tmp.
    monkeypatch.setattr(config, "PKG_DIR", tmp_path / "pkg")

    def _make_llm(*a, **k):
        def _llm(prompt):
            # The judge self-audit prompt contains the ledger marker; return an empty findings
            # array for it (no sleep needed). The rewrite prompt sleeps to widen the race.
            if "VETTED CLAIMS LEDGER" in prompt:
                return "[]"
            time.sleep(sleep_s)
            return answer_for(prompt)
        return _llm

    monkeypatch.setattr(regen_answer, "make_claude_llm", _make_llm)
    monkeypatch.setattr(regen_answer, "make_audit_fn", lambda *a, **k: (lambda text: []))
    monkeypatch.setattr(regen_answer, "load_facts", lambda job=None, **k: "facts")


def _read(mp):
    return json.loads(mp.read_text(encoding="utf-8"))


def _app(mp):
    return next(a for a in _read(mp) if a.get("job_id") == "JOB-700")


def _q(mp, text):
    qk = regen_answer._qkey(text)
    return next(q for q in _app(mp)["custom_qs"] if regen_answer._qkey(q.get("q", "")) == qk)


def test_parallel_edits_to_different_questions_both_land(tmp_path, monkeypatch):
    """Two concurrent regen_answer runs on DIFFERENT questions of the same job — proves the
    merge-safe write: BOTH new values land and BOTH edit_history rows survive (neither clobbered).
    This is the exact lost-update the per-job lock used to prevent by SERIALIZING (slow); now the
    edits run in parallel AND are safe."""
    mp = _seed(tmp_path)

    # Each thread's instruction text is embedded in the prompt, so the stub returns a distinct
    # new value per question (keys off which question's CURRENT ANSWER the prompt carries).
    def _answer_for(prompt):
        if "Original answer one." in prompt:
            return "REWRITTEN one (parallel)."
        if "Original answer two." in prompt:
            return "REWRITTEN two (parallel)."
        return "fallback."

    _wire(tmp_path, monkeypatch, _answer_for, sleep_s=0.4)

    errors = []

    def _run(question, instruction):
        try:
            regen_answer.main(["JOB-700", "--question", question, "--instruction", instruction])
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=_run, args=("Why this company?", "tighten it"))
    t2 = threading.Thread(target=_run, args=("Describe a hard project.", "tighten it"))
    start = time.time()
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    elapsed = time.time() - start

    assert not errors, f"thread raised: {errors}"

    q1 = _q(mp, "Why this company?")
    q2 = _q(mp, "Describe a hard project.")

    # BOTH edits landed — neither clobbered the other (the core lost-update assertion).
    assert q1["value"] == "REWRITTEN one (parallel).", "Q1 edit was clobbered (lost update)"
    assert q2["value"] == "REWRITTEN two (parallel).", "Q2 edit was clobbered (lost update)"

    # BOTH edit_history rows survive (each appended to the FRESH list, not overwriting the other).
    assert len(q1["edit_history"]) == 1 and q1["edit_history"][-1]["status"] == "edited"
    assert len(q2["edit_history"]) == 1 and q2["edit_history"][-1]["status"] == "edited"
    assert q1["edit_history"][-1]["after"] == "REWRITTEN one (parallel)."
    assert q2["edit_history"][-1]["after"] == "REWRITTEN two (parallel)."

    # The unrelated stub record is untouched.
    stub = next(a for a in _read(mp) if a.get("job_id") == "JOB-STUB")
    assert stub.get("note") == "must survive untouched"

    # Sanity: they actually overlapped in time (both slept ~0.4s but ran together, so wall time is
    # well under the ~0.8s a fully-serialized pair would take). This guards the test itself from
    # silently degenerating into a serial run that wouldn't exercise the race.
    assert elapsed < 0.75, f"runs did not overlap (elapsed {elapsed:.2f}s) — race not exercised"


def test_same_question_edits_serialize_safely(tmp_path, monkeypatch):
    """Two concurrent edits to the SAME question must still produce a consistent record: the
    file mutex serializes the two writes (no interleaved/torn write), the final value is one of
    the two rewrites, and BOTH edit_history rows are present (one appended after the other).

    NOTE: the SERVER's per-question launch lock (Part 3) normally prevents two same-question
    edits from launching together; this test drives the CLI directly to prove the engine layer is
    safe even without that guard (defense in depth)."""
    mp = _seed(tmp_path)

    # Both threads target Q1; each returns a value tagged with its instruction so we can tell
    # which one won the final write.
    def _answer_for(prompt):
        if "INSTR-A" in prompt:
            return "RESULT-A."
        if "INSTR-B" in prompt:
            return "RESULT-B."
        return "fallback."

    _wire(tmp_path, monkeypatch, _answer_for, sleep_s=0.3)

    def _run(instruction):
        regen_answer.main(["JOB-700", "--question", "Why this company?",
                           "--instruction", instruction])

    t1 = threading.Thread(target=_run, args=("INSTR-A",))
    t2 = threading.Thread(target=_run, args=("INSTR-B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    q1 = _q(mp, "Why this company?")
    # Final value is one of the two rewrites (a coherent last-writer, not a torn/merged string).
    assert q1["value"] in ("RESULT-A.", "RESULT-B."), f"torn write: {q1['value']!r}"
    # BOTH edits are recorded in history — the second appended onto the FRESH list after the first
    # (the merge-safe append never overwrites a sibling/prior row).
    assert len(q1["edit_history"]) == 2, f"history rows lost: {q1['edit_history']}"
    afters = {h.get("after") for h in q1["edit_history"]}
    assert afters == {"RESULT-A.", "RESULT-B."}, f"a history row was clobbered: {afters}"

    # The OTHER question is completely untouched by the same-question contention.
    q2 = _q(mp, "Describe a hard project.")
    assert q2["value"] == "Original answer two."
    assert q2["edit_history"] == []


def test_manifest_never_torn_under_contention(tmp_path, monkeypatch):
    """After heavy concurrent contention the manifest is always valid JSON (the atomic tmp+replace
    under the mutex never leaves a half-written file). Fires several same- and cross-question edits
    at once and re-parses the result."""
    mp = _seed(tmp_path)

    def _answer_for(prompt):
        return "X" + str(threading.get_ident())[-4:] + "."

    _wire(tmp_path, monkeypatch, _answer_for, sleep_s=0.05)

    threads = []
    for i in range(6):
        q = "Why this company?" if i % 2 == 0 else "Describe a hard project."
        threads.append(threading.Thread(
            target=lambda qq=q, n=i: regen_answer.main(
                ["JOB-700", "--question", qq, "--instruction", f"edit {n}"])))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The file parses (never torn) and both questions still exist.
    data = _read(mp)
    assert isinstance(data, list)
    app = next(a for a in data if a.get("job_id") == "JOB-700")
    assert len(app["custom_qs"]) == 2
