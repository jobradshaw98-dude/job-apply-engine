"""Orchestrator single-page custom-question block: with answer_fn passed, custom <select>
dropdowns and "check all that apply" checkbox-groups are resolved (grounded + gated) and
filled, alongside the existing free-text path. Default-off (no answer_fn) is unchanged.

Browser-backed on the fixture server. The LLM/gate are injected stubs — no network."""
import pytest
from apply_engine.orchestrator import apply_to_job
from apply_engine.source_data import Answers


@pytest.fixture
def answers(tmp_path):
    r = tmp_path / "r.pdf"; r.write_bytes(b"%PDF-1.4")
    return Answers(values={"first_name": "Sam", "last_name": "Rivera",
                           "full_name": "Sam Rivera",
                           "email": "sam.rivera@example.com", "phone": "555-555-0100"},
                   resume_pdf=r, cover_pdf=None)


# An injected LLM that answers the two custom questions from FACTS, declines anything else.
def _stub_llm(prompt: str) -> str:
    p = prompt.lower()
    if "simulation experience" in p:
        return "5+ years"
    if "language skill" in p:
        return "English (ENG)"  # facts support English only
    return "DECLINE"


def test_custom_select_and_checkbox_group_are_resolved_and_filled(
        fixture_server, answers, tmp_path):
    job = {"id": "JOB-CUSTOM", "company": "Acme", "title": "Sim Engineer",
           "url": f"{fixture_server}/greenhouse_custom_form.html"}
    out = apply_to_job(
        job=job, answers=answers, runs_root=tmp_path / "runs",
        profile_dir=tmp_path / "p", headless=True, dry_run=True,
        ats_override="greenhouse",
        answer_fn=_stub_llm, audit_fn=lambda t: [],
        facts="FACTS: ~5 years simulation-led engineering. Native English speaker.",
    )
    # The select question was answered and recorded
    gens = {g["q"]: g for g in out.generated}
    sel = next(g for q, g in gens.items() if "simulation experience" in q.lower())
    assert sel["status"] == "answered"
    assert sel["value"] == "5+ years"
    # The checkbox-group was answered with the grounded subset
    grp = next(g for q, g in gens.items() if "language skill" in q.lower())
    assert grp["status"] == "answered"
    assert grp["values"] == ["English (ENG)"]
    # required custom select no longer blocks -> staged to brink
    assert out.status == "ready_to_submit"
    assert out.submitted is False


def test_default_off_leaves_custom_qs_untouched(fixture_server, answers, tmp_path):
    # No answer_fn -> nothing drafted; the required custom select stays empty -> needs_input.
    job = {"id": "JOB-OFF", "company": "Acme", "title": "Sim Engineer",
           "url": f"{fixture_server}/greenhouse_custom_form.html"}
    out = apply_to_job(
        job=job, answers=answers, runs_root=tmp_path / "runs",
        profile_dir=tmp_path / "p", headless=True, dry_run=True,
        ats_override="greenhouse",
    )
    assert out.generated == []
    assert out.status == "needs_input"
    assert any("simulation experience" in m.lower() for m in out.unfilled_required)


def test_declined_custom_select_is_left_blank_and_escalates(
        fixture_server, answers, tmp_path):
    # LLM declines everything -> the required select stays blank -> needs_input (never guessed).
    job = {"id": "JOB-DEC", "company": "Acme", "title": "Sim Engineer",
           "url": f"{fixture_server}/greenhouse_custom_form.html"}
    out = apply_to_job(
        job=job, answers=answers, runs_root=tmp_path / "runs",
        profile_dir=tmp_path / "p", headless=True, dry_run=True,
        ats_override="greenhouse",
        answer_fn=lambda p: "DECLINE", audit_fn=lambda t: [],
        facts="FACTS: nothing relevant.",
    )
    gens = {g["q"]: g for g in out.generated}
    sel = next(g for q, g in gens.items() if "simulation experience" in q.lower())
    assert sel["status"] == "declined"
    assert "value" not in sel or not sel.get("value")
    assert out.status == "needs_input"
