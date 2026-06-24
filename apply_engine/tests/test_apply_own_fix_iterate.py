# -*- coding: utf-8 -*-
"""apply_own_fix passes --max-attempts 3 to the regen AND surfaces the classified residual.

Proves the convergence loop's engine-own fix path iterates (N=3) and, when the regen exhausts
still-blocked, reads the classified residual the regen stamped and returns it in the enriched tag
so converge_quality can build the right blocker. regen_answer.main is monkeypatched (no claude -p):
it asserts the flag and writes a residual onto the manifest record the way the real regen does.
"""
import json

from apply_engine import converge
from apply_engine import config as _cfg


def _seed(tmp_path):
    rec = {
        "job_id": "JOB-001", "company": "Acme",
        "custom_qs": [{"q": "Why us?", "kind": "essay", "status": "drafted", "value": "x"}],
    }
    mp = tmp_path / "staged_applications.json"
    mp.write_text(json.dumps([rec], indent=2), encoding="utf-8")
    return mp


def test_apply_own_fix_passes_max_attempts_and_reads_residual(tmp_path, monkeypatch):
    monkeypatch.setattr(_cfg, "ARIA_DATA", tmp_path)
    mp = _seed(tmp_path)

    seen = {}

    def _fake_regen_main(argv):
        seen["argv"] = argv
        # The real regen stamps residual on the matching custom_q when it exhausts blocked.
        data = json.loads(mp.read_text(encoding="utf-8"))
        data[0]["custom_qs"][0]["residual"] = {"class": "human_only", "attempts": 3}
        mp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return 0

    from apply_engine import regen_answer
    monkeypatch.setattr(regen_answer, "main", _fake_regen_main)

    finding = {"kind": "fabrication", "doc": "essay_answer", "question": "Why us?",
               "issue": "claim not in ledger", "fix": "remove it"}
    tag = converge.apply_own_fix("JOB-001", finding)

    # --max-attempts 3 was threaded into the regen call.
    argv = seen["argv"]
    assert "--max-attempts" in argv
    assert argv[argv.index("--max-attempts") + 1] == "3"
    # The enriched tag carries the classified residual class.
    assert tag == "answer:ok:human_only"


def test_apply_own_fix_no_residual_plain_tag(tmp_path, monkeypatch):
    """A clean fix (no residual stamped) returns the plain tag, unchanged from before."""
    monkeypatch.setattr(_cfg, "ARIA_DATA", tmp_path)
    mp = _seed(tmp_path)

    from apply_engine import regen_answer
    monkeypatch.setattr(regen_answer, "main", lambda argv: 0)

    finding = {"kind": "fabrication", "doc": "essay_answer", "question": "Why us?",
               "issue": "x", "fix": "y"}
    tag = converge.apply_own_fix("JOB-001", finding)
    assert tag == "answer:ok"


def test_residual_blocker_text_mapping():
    """The dominant-residual -> blocker mapping: human_only is answerable, unsupportable is
    rewrite-or-drop, none keeps the generic stalled message."""
    r_human, cat_human = converge._residual_blocker_text("human_only")
    assert "only you can confirm" in r_human and cat_human == "missing_value"
    r_uns, cat_uns = converge._residual_blocker_text("unsupportable")
    assert "rewrite or drop" in r_uns and cat_uns == "calibration_unfixable"
    r_none, _ = converge._residual_blocker_text(None)
    assert "convergence stalled" in r_none


def test_residual_from_tag_and_dominant():
    assert converge._residual_from_tag("answer:fail:human_only") == "human_only"
    assert converge._residual_from_tag("content:fail:unsupportable") == "unsupportable"
    assert converge._residual_from_tag("answer:ok") is None
    # human_only wins when both present in a round.
    assert converge._dominant_residual(["unsupportable", "human_only"]) == "human_only"
    assert converge._dominant_residual([]) is None
