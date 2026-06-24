# -*- coding: utf-8 -*-
"""Tests for form_driver_stage — the canonical writer that keeps a form-driver result from
500'ing the apply-queue dashboard (JOB-163 Edwards regression, 2026-06-20)."""
import json

import pytest

from apply_engine import form_driver_stage as F


def _valid_kwargs(**over):
    base = dict(
        job_id="JOB-TST", company="Acme", role="Eng", url="http://x", ats="workday",
        staged_at="2026-06-20T12:00:00",
        filled_fields=["step1: country=US"],
        custom_qs=[{"q": "Desired salary", "kind": "text", "status": "answered",
                    "reason": "", "value": "125000", "answered_by": "sam"}],
        work_auth=[{"q": "Authorized?", "field": "authorized_no_sponsorship", "answer": "Yes"}],
        uploaded_docs=[{"doc": "resume", "path": "C:/x/APPLICANT_Resume.pdf",
                        "name": "APPLICANT_Resume.pdf"}],
    )
    base.update(over)
    return base


def test_build_record_valid_shapes():
    rec = F.build_record(**_valid_kwargs())
    assert rec["submitted"] is False
    assert rec["status"] == "ready_to_submit"
    assert rec["reached"] == "review-brink"
    # every dict-list field is a list of dicts (the invariant the dashboard depends on)
    for key in ("custom_qs", "work_auth", "uploaded_docs"):
        assert all(isinstance(x, dict) for x in rec[key])
    # extra caller keys are preserved, not dropped
    assert rec["custom_qs"][0]["answered_by"] == "sam"


def test_filled_fields_stay_strings():
    # filled_fields is the ONE field that renders as plain strings — must not be coerced to dicts
    rec = F.build_record(**_valid_kwargs(filled_fields=["a=1", "b=2"]))
    assert rec["filled_fields"] == ["a=1", "b=2"]


@pytest.mark.parametrize("field", ["custom_qs", "work_auth", "uploaded_docs"])
def test_string_in_dict_list_field_raises_loud(field):
    # The exact JOB-163 failure: a freeform string where a dict is required must fail at WRITE
    # time (loud ValueError) instead of silently 500'ing the dashboard at render time.
    with pytest.raises(ValueError) as ei:
        F.build_record(**_valid_kwargs(**{field: ["step3 Q7 desired salary -> 125000"]}))
    assert field in str(ei.value)


def test_missing_required_key_filled_blank():
    rec = F.build_record(**_valid_kwargs(custom_qs=[{"q": "Q only"}]))
    q = rec["custom_qs"][0]
    for k in ("q", "kind", "status", "reason", "value"):
        assert k in q
    assert q["value"] == ""


def test_stage_round_trip(tmp_path):
    mp = tmp_path / "staged.json"
    F.stage_form_driver_result(manifest_path=mp, **_valid_kwargs())
    back = json.loads(mp.read_text(encoding="utf-8"))
    assert back[0]["job_id"] == "JOB-TST"
    assert isinstance(back[0]["custom_qs"][0], dict)


def test_stage_replaces_same_job_id(tmp_path):
    mp = tmp_path / "staged.json"
    F.stage_form_driver_result(manifest_path=mp, **_valid_kwargs(role="Eng v1"))
    F.stage_form_driver_result(manifest_path=mp, **_valid_kwargs(role="Eng v2"))
    back = json.loads(mp.read_text(encoding="utf-8"))
    assert len(back) == 1 and back[0]["role"] == "Eng v2"


def test_run_accuracy_review_calls_engine_hook(monkeypatch):
    # The form-driver path must run the SAME review the engine runs: chain_accuracy_review with a
    # successful-stage outcome and answered=True. (Closes the JOB-163/JOB-160 no-audit gap.)
    import apply_engine.cli as cli
    seen = {}

    def fake(outcome, *, answered):
        seen["status"] = outcome.status
        seen["job_id"] = outcome.job_id
        seen["answered"] = answered
        return "PASS"

    monkeypatch.setattr(cli, "chain_accuracy_review", fake)
    tag = F.run_accuracy_review("JOB-ZZ", "ready_to_submit")
    assert tag == "PASS"
    assert seen == {"status": "ready_to_submit", "job_id": "JOB-ZZ", "answered": True}


def test_run_accuracy_review_is_non_raising(monkeypatch):
    # A review failure must never crash the stage — it returns an error tag and leaves Submit locked.
    import apply_engine.cli as cli

    def boom(outcome, *, answered):
        raise RuntimeError("judge down")

    monkeypatch.setattr(cli, "chain_accuracy_review", boom)
    assert F.run_accuracy_review("JOB-ZZ").startswith("error:")
