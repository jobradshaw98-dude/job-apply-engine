# -*- coding: utf-8 -*-
"""G2 length-fix path — a too-short / too-long essay is auto-rewritten into the form's stated word
range by the convergence loop, instead of dead-ending at "exhausted / needs your call" (the JOB-237
gap: a "Why Anthropic?" essay flagged 140 words vs the form's stated 200-400).

Three layers, all with INJECTED fakes (NO real claude -p / API / network):
  1. compliance.ComplianceResult.to_findings() — a length violation becomes a routable finding
     carrying {kind:length, question, range:[lo,hi], current_words, direction}.
  2. converge.apply_own_fix — a length finding routes to regen_answer with a lengthen/tighten
     instruction (the band, "too short"/"too long", "do not invent/pad") + --min-words/--max-words.
  3. converge.converge_quality — the loop treats a length violation as a FIXABLE block (not human-
     only): reaches the range -> converges; never reaches it -> residual length_unmet (NOT human_only).
"""
import json
from pathlib import Path

import pytest

from apply_engine import converge
from apply_engine import config as _cfg
from apply_engine import regen_answer
from apply_engine.compliance import check_form_constraints
from apply_engine.form_spec import FormSpec, FieldSpec


# ---------------------------------------------------------------------------
# Layer 1 — compliance violations surface as routable length findings
# ---------------------------------------------------------------------------

def _spec(*fields) -> FormSpec:
    s = FormSpec(ats="greenhouse")
    s.fields = list(fields)
    return s


def _essay(key, label, constraints):
    return FieldSpec(key=key, label=label, required=True, widget_kind="textarea",
                     constraints=constraints)


def test_under_length_to_finding_shape():
    spec = _spec(_essay("why", "Why Anthropic?", {"words": [200, 400]}))
    rec = {"custom_qs": [{"q": "Why Anthropic?", "kind": "essay", "value": "word " * 140}]}
    res = check_form_constraints(spec, rec)
    finds = res.to_findings()
    assert len(finds) == 1
    f = finds[0]
    assert f["kind"] == "length"
    assert f["doc"] == "essay_answer"
    assert f["question"] == "Why Anthropic?"
    assert f["range"] == [200, 400]
    assert f["current_words"] == 140
    assert f["direction"] == "under"


def test_over_length_to_finding_shape():
    spec = _spec(_essay("why", "Why Anthropic?", {"words": [200, 400]}))
    rec = {"custom_qs": [{"q": "Why Anthropic?", "kind": "essay", "value": "word " * 500}]}
    finds = check_form_constraints(spec, rec).to_findings()
    assert finds[0]["direction"] == "over"
    assert finds[0]["current_words"] == 500


def test_words_min_standalone_bounds_under():
    spec = _spec(_essay("why", "Why?", {"words_min": 150}))
    rec = {"custom_qs": [{"q": "Why?", "kind": "essay", "value": "word " * 50}]}
    f = check_form_constraints(spec, rec).to_findings()[0]
    assert f["direction"] == "under"
    assert f["range"] == [150, None]


# ---------------------------------------------------------------------------
# Layer 2 — apply_own_fix routes a length finding to regen_answer
# ---------------------------------------------------------------------------

def _seed_rec(tmp_path, value="word "):
    rec = {
        "job_id": "JOB-237", "company": "Anthropic",
        "custom_qs": [{"q": "Why Anthropic?", "kind": "essay", "status": "drafted",
                       "value": value}],
    }
    mp = tmp_path / "staged_applications.json"
    mp.write_text(json.dumps([rec], indent=2), encoding="utf-8")
    return mp


def test_apply_own_fix_routes_under_length_lengthen_instruction(tmp_path, monkeypatch):
    monkeypatch.setattr(_cfg, "ARIA_DATA", tmp_path)
    _seed_rec(tmp_path)
    seen = {}

    def _fake_main(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(regen_answer, "main", _fake_main)

    finding = {"kind": "length", "doc": "essay_answer", "question": "Why Anthropic?",
               "range": [200, 400], "current_words": 140, "direction": "under"}
    tag = converge.apply_own_fix("JOB-237", finding)

    argv = seen["argv"]
    assert "--max-attempts" in argv and argv[argv.index("--max-attempts") + 1] == "3"
    # the band passed through as min/max words
    assert argv[argv.index("--min-words") + 1] == "200"
    assert argv[argv.index("--max-words") + 1] == "400"
    instr = argv[argv.index("--instruction") + 1]
    assert "200-400" in instr
    assert "too short" in instr.lower()
    assert "do not invent" in instr.lower()
    assert "do not pad" in instr.lower()
    # disclosure / coding-fluency guard is named in the lengthen instruction
    assert "visa" in instr.lower() and "coding-fluency" in instr.lower()
    assert tag.startswith("length:ok")


def test_apply_own_fix_routes_over_length_tighten_instruction(tmp_path, monkeypatch):
    monkeypatch.setattr(_cfg, "ARIA_DATA", tmp_path)
    _seed_rec(tmp_path)
    seen = {}

    def _fake_main(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(regen_answer, "main", _fake_main)

    finding = {"kind": "length", "doc": "essay_answer", "question": "Why Anthropic?",
               "range": [200, 400], "current_words": 600, "direction": "over"}
    converge.apply_own_fix("JOB-237", finding)
    instr = seen["argv"][seen["argv"].index("--instruction") + 1]
    assert "200-400" in instr
    assert "too long" in instr.lower()
    assert "tighten" in instr.lower()
    assert "cutting redundancy" in instr.lower()


# ---------------------------------------------------------------------------
# Layer 3 — the convergence loop treats length as a fixable block
# ---------------------------------------------------------------------------

@pytest.fixture()
def career_tree(tmp_path, monkeypatch):
    """A throwaway career tree so verify_submittable's PDF-integrity invariant passes for real."""
    root = tmp_path / "career"
    pkg = root / "apply_engine"
    pkg.mkdir(parents=True)
    (root / "APPLICANT_Resume_Master.pdf").write_text("MASTER", encoding="utf-8")
    appdir = root / "applications" / "APP-001-Anthropic"
    appdir.mkdir(parents=True)
    (appdir / "APPLICANT_Resume.pdf").write_text("TAILORED", encoding="utf-8")
    (appdir / "APPLICANT_Cover_Letter.pdf").write_text("COVER", encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(_cfg, "ARIA_DATA", data_dir)
    monkeypatch.setattr(_cfg, "PKG_DIR", pkg)
    return {"appdir": appdir, "data_dir": data_dir}


def _form_spec_summary(lo=200, hi=400):
    """A captured form_spec carrying a single Why-Anthropic essay with a stated word range, in the
    compact summary shape the record stores (compliance recomputes G2 straight from this)."""
    return {
        "ats": "greenhouse", "has_resume_field": True, "has_cover_field": True, "n_fields": 1,
        "fields": [{"key": "why", "label": "Why Anthropic?", "required": True,
                    "widget_kind": "textarea", "doc_kind": "",
                    "constraints": {"words": [lo, hi]}}],
    }


def _record(appdir: Path, essay_value: str) -> dict:
    """A staged record that PASSES verify_submittable/can_submit when the essay is in range — the
    only outstanding gate is G2 length (computed live from form_spec)."""
    return {
        "job_id": "JOB-237", "company": "Anthropic", "role": "Applied AI Engineer",
        "status": "ready_to_submit", "submitted": False,
        "uploaded_docs": [
            {"doc": "resume", "path": str(appdir / "APPLICANT_Resume.pdf"),
             "name": "APPLICANT_Resume.pdf"},
            {"doc": "cover", "path": str(appdir / "APPLICANT_Cover_Letter.pdf"),
             "name": "APPLICANT_Cover_Letter.pdf"},
        ],
        "work_auth": [{"field": "sponsor", "answer": "No"}],
        "custom_qs": [{"q": "Why Anthropic?", "kind": "essay", "status": "answered",
                       "value": essay_value}],
        "unfilled_required": [], "needs_sam": [],
        "audit": {"verdict": "PASS", "judge_ran": True, "findings": [],
                  "gate_blocks": 0, "block_findings": 0},
        "quality_audit": {"verdict": "PASS", "judge_ran": True, "calibration": []},
        "form_spec": _form_spec_summary(),
    }


def _write(data_dir: Path, rec: dict) -> Path:
    mp = data_dir / "staged_applications.json"
    mp.write_text(json.dumps([rec], indent=2), encoding="utf-8")
    return mp


def _read(mp: Path) -> dict:
    return json.loads(mp.read_text(encoding="utf-8"))[0]


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, x):
        self.calls.append(x)
        return True


def test_length_under_converges_when_fix_reaches_range(career_tree):
    """A 140-word essay (under 200-400) is a fixable BLOCK; the fix grows it into range -> converged.
    NOT exhausted, NOT human-only."""
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    mp = _write(data_dir, _record(appdir, "word " * 140))  # 140 words, under 200

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        pass  # audit stays clean; the only block is the live G2 length violation

    def fix_fn(job_id, finding):
        # the length finding routes here; model that the regen reached the range by rewriting the
        # staged answer to 300 words (in range). Mirrors regen_answer landing an in-range value.
        assert finding["kind"] == "length" and finding["direction"] == "under"
        data = json.loads(mp.read_text(encoding="utf-8"))
        data[0]["custom_qs"][0]["value"] = "word " * 300
        mp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return "length:ok"

    notify = _Recorder()
    tag = converge.converge_quality("JOB-237", audit_fn=audit_fn, fix_fn=fix_fn,
                                    notify_fn=notify, manifest_path=mp)
    assert tag == "converged", tag
    rec = _read(mp)
    assert rec["convergence"]["state"] == "converged"
    assert notify.calls == []  # a clean converge never notifies


def test_length_unmet_residual_not_human_only(career_tree):
    """A length fix that NEVER reaches the range -> exhausted with residual length_unmet, surfaced as
    'couldn't reach the length' — NOT human_only / 'needs your call'."""
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    mp = _write(data_dir, _record(appdir, "word " * 140))  # stays under 200 forever

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        pass  # the essay never grows; G2 length stays violated every round

    def fix_fn(job_id, finding):
        # regen iterated K attempts and could not reach the band with supported facts.
        return "length:ok:length_unmet"

    notify = _Recorder()
    tag = converge.converge_quality("JOB-237", audit_fn=audit_fn, fix_fn=fix_fn,
                                    notify_fn=notify, manifest_path=mp, max_rounds=3)
    assert tag == "exhausted", tag
    rec = _read(mp)
    blk = rec["human_blocker"]
    assert blk["category"] == "length_unmet"
    assert blk["category"] != "calibration_unfixable"
    reason = (blk.get("blocking_reason") or "").lower()
    assert "required word range" in reason or "word range" in reason
    assert "needs your call" not in reason
    assert len(notify.calls) == 1


def test_length_block_is_not_partitioned_human_only(career_tree):
    """A length finding must be FIXABLE (routed to a fix), never partitioned into the human-only
    bucket — so the loop attempts the fix at least once rather than blocking immediately."""
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    mp = _write(data_dir, _record(appdir, "word " * 140))
    attempted = {"n": 0}

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        pass

    def fix_fn(job_id, finding):
        attempted["n"] += 1
        return "length:ok:length_unmet"  # never reaches range -> eventually exhausted

    tag = converge.converge_quality("JOB-237", audit_fn=audit_fn, fix_fn=fix_fn,
                                    notify_fn=_Recorder(), manifest_path=mp, max_rounds=3)
    assert attempted["n"] >= 1  # the loop tried to fix it (not immediately blocked human-only)
    assert tag == "exhausted"


def test_no_length_violation_no_length_fix(career_tree):
    """Backward-compat: an in-range essay yields no length block -> converges round 1, no fix."""
    appdir, data_dir = career_tree["appdir"], career_tree["data_dir"]
    mp = _write(data_dir, _record(appdir, "word " * 300))  # 300 words, in 200-400

    def audit_fn(job_id, include_quality=False, recheck_calibration=False):
        pass

    def fix_fn(job_id, finding):
        raise AssertionError("no length violation -> no fix should be attempted")

    tag = converge.converge_quality("JOB-237", audit_fn=audit_fn, fix_fn=fix_fn, manifest_path=mp)
    assert tag == "converged"
    assert _read(mp)["convergence"]["rounds"] == 1
