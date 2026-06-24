"""Re-stage CONSUMES a Sam-provided answer instead of re-declining the question.

The bug: when a card halts at needs_input because the engine declined a custom question,
Sam supplies the answer via `regen_answer --provide`, which writes it onto the staged
record's custom_q (answered_by="sam"). On the NEXT stage the orchestrator re-extracted
every question from the live form and re-ran the classifier, re-declining the same one ->
the card could never reach the brink. The fix loads the prior record's Sam-provided
answers at stage start and, in each custom-question handler, DRIVES the provided value into
the live widget (verified-set discipline) and skips the classifier.

Browser-backed on the fixture server. The LLM/gate are injected stubs — no network."""
import json

import pytest

from apply_engine import config
from apply_engine.orchestrator import apply_to_job
from apply_engine.source_data import Answers


@pytest.fixture
def answers(tmp_path):
    r = tmp_path / "r.pdf"; r.write_bytes(b"%PDF-1.4")
    return Answers(values={"first_name": "Sam", "last_name": "Rivera",
                           "full_name": "Sam Rivera",
                           "email": "sam.rivera@example.com", "phone": "555-555-0100"},
                   resume_pdf=r, cover_pdf=None)


def _seed(monkeypatch, tmp_path, job_id, custom_qs):
    """Point config.ARIA_DATA at our own dir (overriding the autouse sandbox) and write a
    prior staged record carrying the given Sam-provided custom_qs."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config, "ARIA_DATA", data_dir)
    rec = {"job_id": job_id, "company": "Acme", "role": "Sim Engineer",
           "status": "needs_input", "needs_sam": [], "custom_qs": custom_qs}
    (data_dir / "staged_applications.json").write_text(
        json.dumps([rec], indent=2), encoding="utf-8")
    return data_dir


# An LLM that would DECLINE the screening question — proving the consume path skips it.
def _decline_llm(prompt: str) -> str:
    return "DECLINE"


# ── 1. react-select screening question with a Sam-provided answer is FILLED, not re-declined.
def test_provided_react_select_is_filled_not_redeclined(
        fixture_server, answers, tmp_path, monkeypatch):
    job_id = "JOB-RS-PROVIDED"
    _seed(monkeypatch, tmp_path, job_id, [
        {"q": "Have you helped develop and validate FEA models?",
         "kind": "react_select", "status": "answered", "answered_by": "sam",
         "value": "Yes", "reason": "", "review_findings": []},
    ])
    job = {"id": job_id, "company": "Acme", "title": "Sim Engineer",
           "url": f"{fixture_server}/greenhouse_modern_form.html"}
    out = apply_to_job(
        job=job, answers=answers, runs_root=tmp_path / "runs",
        profile_dir=tmp_path / "p", headless=True, dry_run=True,
        ats_override="greenhouse",
        # The LLM would DECLINE — the only way this question gets answered is the consume path.
        answer_fn=_decline_llm, audit_fn=lambda t: [],
        facts="FACTS: nothing relevant.",
    )
    gens = {g["q"]: g for g in out.generated}
    fea = next(g for q, g in gens.items() if "fea models" in q.lower())
    assert fea["status"] == "answered"
    assert fea["value"] == "Yes"
    assert fea.get("answered_by") == "sam"
    # THE FIX: the engine never halted ON the FEA question (no needs_sam escalation for it,
    # no fill_error) — it consumed Sam's answer and drove the live widget. (The modern form
    # also has required Country/State react-selects the bare test profile doesn't fill, so the
    # overall card stays needs_input on THOSE — that's unrelated to the question we provided.)
    assert out.submitted is False
    assert not any("fea models" in str(m).lower() for m in (out.unfilled_required or []))
    hb = out.human_blocker or {}
    assert "fea models" not in str(hb.get("question", "")).lower()


# ── 1b. native <select> with a Sam-provided answer is selected, not re-declined.
def test_provided_native_select_is_filled_not_redeclined(
        fixture_server, answers, tmp_path, monkeypatch):
    job_id = "JOB-SEL-PROVIDED"
    _seed(monkeypatch, tmp_path, job_id, [
        {"q": "Years of simulation experience?",
         "kind": "select", "status": "answered", "answered_by": "sam",
         "value": "5+ years", "reason": "", "review_findings": []},
        # The language checkbox-group also provided so the form fully clears to the brink.
        {"q": "Language Skill(s) (check all that apply)",
         "kind": "checkbox_group", "status": "answered", "answered_by": "sam",
         "value": "English (ENG)", "reason": "", "review_findings": []},
    ])
    job = {"id": job_id, "company": "Acme", "title": "Sim Engineer",
           "url": f"{fixture_server}/greenhouse_custom_form.html"}
    out = apply_to_job(
        job=job, answers=answers, runs_root=tmp_path / "runs",
        profile_dir=tmp_path / "p", headless=True, dry_run=True,
        ats_override="greenhouse",
        answer_fn=_decline_llm, audit_fn=lambda t: [],
        facts="FACTS: nothing relevant.",
    )
    gens = {g["q"]: g for g in out.generated}
    sel = next(g for q, g in gens.items() if "simulation experience" in q.lower())
    assert sel["status"] == "answered"
    assert sel["value"] == "5+ years"
    assert sel.get("answered_by") == "sam"
    grp = next(g for q, g in gens.items() if "language skill" in q.lower())
    assert grp["status"] == "answered"
    assert grp["values"] == ["English (ENG)"]
    assert grp.get("answered_by") == "sam"
    # THE FIX: neither provided custom question re-declined or re-escalated — both consumed
    # Sam's answer and drove the live widgets. (This fixture has no file input, so the card
    # still carries the pre-existing "Resume (did not attach)" blocker — unrelated to the fix.)
    assert out.submitted is False
    blockers = " ".join(str(m).lower() for m in (out.unfilled_required or []))
    assert "simulation experience" not in blockers
    assert "language skill" not in blockers


# ── 2. a provided value that the widget will NOT accept -> fill_error, NOT a phantom answered.
def test_provided_value_that_fails_to_drive_is_fill_error(
        fixture_server, answers, tmp_path, monkeypatch):
    job_id = "JOB-SEL-BADVALUE"
    _seed(monkeypatch, tmp_path, job_id, [
        # "17 years" is not one of the <select> options (0-2 / 3-5 / 5+). select_option(label=...)
        # raises -> the path must record fill_error, never answered (live-dom rule).
        {"q": "Years of simulation experience?",
         "kind": "select", "status": "answered", "answered_by": "sam",
         "value": "17 years", "reason": "", "review_findings": []},
    ])
    job = {"id": job_id, "company": "Acme", "title": "Sim Engineer",
           "url": f"{fixture_server}/greenhouse_custom_form.html"}
    out = apply_to_job(
        job=job, answers=answers, runs_root=tmp_path / "runs",
        profile_dir=tmp_path / "p", headless=True, dry_run=True,
        ats_override="greenhouse",
        answer_fn=_decline_llm, audit_fn=lambda t: [],
        facts="FACTS: nothing relevant.",
    )
    gens = {g["q"]: g for g in out.generated}
    sel = next(g for q, g in gens.items() if "simulation experience" in q.lower())
    assert sel["status"] == "fill_error"
    assert "value" not in sel or not sel.get("value")
    # the unfilled required select means the card cannot be ready_to_submit
    assert out.status != "ready_to_submit"


# ── 3. backward-compat: NO provided answer -> the question still runs through the classifier
#       exactly as before (declines -> needs_input, never auto-filled).
def test_no_provided_answer_declines_as_before(
        fixture_server, answers, tmp_path, monkeypatch):
    job_id = "JOB-NO-PRIOR"
    # No prior record at all for this job (empty manifest) — must behave identically to today.
    data_dir = tmp_path / "data"; data_dir.mkdir()
    monkeypatch.setattr(config, "ARIA_DATA", data_dir)
    (data_dir / "staged_applications.json").write_text("[]", encoding="utf-8")
    job = {"id": job_id, "company": "Acme", "title": "Sim Engineer",
           "url": f"{fixture_server}/greenhouse_custom_form.html"}
    out = apply_to_job(
        job=job, answers=answers, runs_root=tmp_path / "runs",
        profile_dir=tmp_path / "p", headless=True, dry_run=True,
        ats_override="greenhouse",
        answer_fn=_decline_llm, audit_fn=lambda t: [],
        facts="FACTS: nothing relevant.",
    )
    gens = {g["q"]: g for g in out.generated}
    sel = next(g for q, g in gens.items() if "simulation experience" in q.lower())
    assert sel["status"] == "declined"
    assert "value" not in sel or not sel.get("value")
    assert sel.get("answered_by") != "sam"
    assert out.status == "needs_input"
