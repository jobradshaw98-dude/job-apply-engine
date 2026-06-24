import pytest
from apply_engine.orchestrator import apply_to_job
from apply_engine.source_data import Answers


@pytest.fixture
def answers(tmp_path):
    r = tmp_path / "r.pdf"; r.write_bytes(b"%PDF")
    return Answers(values={"first_name": "Sam", "last_name": "Rivera",
                           "full_name": "Sam Rivera",
                           "email": "sam.rivera@example.com", "phone": "555-555-0100"},
                   resume_pdf=r, cover_pdf=None)


def test_unknown_ats_uses_generic_filler(fixture_server, answers, tmp_path):
    job = {"id": "JOB-GEN", "company": "Acme", "title": "Eng",
           "url": f"{fixture_server}/generic_form.html"}
    out = apply_to_job(job=job, answers=answers, runs_root=tmp_path / "runs",
                       profile_dir=tmp_path / "p", headless=True, dry_run=True)
    # no adapter matched the fixture URL -> generic filler ran and staged
    assert out.status == "ready_to_submit"
    assert out.verify_ok is True


def test_lever_routed_by_override(fixture_server, answers, tmp_path):
    job = {"id": "JOB-LV", "company": "Acme", "title": "Eng",
           "url": f"{fixture_server}/lever_form.html"}
    out = apply_to_job(job=job, answers=answers, runs_root=tmp_path / "runs",
                       profile_dir=tmp_path / "p", headless=True, dry_run=True,
                       ats_override="lever")
    assert out.status == "ready_to_submit"
    assert any(w["answer"] == "Yes" for w in out.work_auth_answers)
