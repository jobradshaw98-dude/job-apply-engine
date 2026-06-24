import json
import pytest
from apply_engine.source_data import build_answers
from apply_engine.source_data import Answers
from apply_engine.source_data import canonical_upload_path
from apply_engine.source_data import CANONICAL_RESUME_NAME
from apply_engine.source_data import CANONICAL_COVER_NAME


@pytest.fixture
def profile(tmp_path):
    p = tmp_path / "profile.json"
    p.write_text(json.dumps({
        "first_name": "Sam", "last_name": "Rivera",
        "full_name": "Sam Rivera", "email": "sam.rivera@example.com",
        "phone": "555-555-0100", "city": "Austin", "state": "CA",
        "country": "United States", "linkedin": "https://linkedin.com/in/x",
        "portfolio_url": "", "how_did_you_hear": "Company website",
    }), encoding="utf-8")
    return p


def test_build_answers_maps_profile_and_resume(profile, tmp_path):
    resume = tmp_path / "resume.pdf"; resume.write_bytes(b"%PDF-1.4")
    cover = tmp_path / "cover.pdf"; cover.write_bytes(b"%PDF-1.4")
    job = {"id": "JOB-131", "company": "Oura", "title": "Staff FEA Engineer"}

    ans = build_answers(profile_path=profile, job=job, resume_pdf=resume, cover_pdf=cover)
    assert isinstance(ans, Answers)
    assert ans.get("first_name") == "Sam"
    assert ans.get("email") == "sam.rivera@example.com"
    # G6: the docs are normalized to the canonical recruiter-visible upload names (the source
    # names here were resume.pdf / cover.pdf), and point at real, existing PDF bytes.
    assert ans.resume_pdf.name == CANONICAL_RESUME_NAME
    assert ans.cover_pdf.name == CANONICAL_COVER_NAME
    assert ans.resume_pdf.exists() and ans.cover_pdf.exists()
    assert ans.resume_pdf.read_bytes() == b"%PDF-1.4"


def test_build_answers_rejects_unfilled_phone(profile, tmp_path):
    bad = json.loads(profile.read_text(encoding="utf-8")); bad["phone"] = "FILL_ME"
    profile.write_text(json.dumps(bad), encoding="utf-8")
    resume = tmp_path / "r.pdf"; resume.write_bytes(b"%PDF")
    with pytest.raises(ValueError, match="FILL_ME"):
        build_answers(profile_path=profile, job={"id": "X"}, resume_pdf=resume, cover_pdf=None)


def test_build_answers_requires_existing_resume(profile, tmp_path):
    missing = tmp_path / "nope.pdf"
    with pytest.raises(FileNotFoundError):
        build_answers(profile_path=profile, job={"id": "X"}, resume_pdf=missing, cover_pdf=None)


# ---------------------------------------------------------------------------
# G6 — canonical upload filename
# ---------------------------------------------------------------------------
def test_canonical_upload_path_renames_noncanonical_source(tmp_path):
    """A non-canonical source name (Scale_Resume.pdf) is copied to a file whose basename is the
    canonical recruiter-visible name, with the SAME bytes."""
    src = tmp_path / "Scale_Resume.pdf"
    src.write_bytes(b"%PDF-resume-bytes")
    out = canonical_upload_path(src, CANONICAL_RESUME_NAME)
    assert out.name == CANONICAL_RESUME_NAME
    assert out != src
    assert out.read_bytes() == b"%PDF-resume-bytes"


def test_canonical_upload_path_passes_through_already_canonical(tmp_path):
    """An already-canonical source is returned UNCHANGED (no needless copy)."""
    src = tmp_path / CANONICAL_RESUME_NAME
    src.write_bytes(b"%PDF")
    out = canonical_upload_path(src, CANONICAL_RESUME_NAME)
    assert out == src


def test_build_answers_uploads_canonical_resume_from_company_named_source(profile, tmp_path):
    """The on-disk source name (Scale_Resume.pdf / Company_Cover.pdf) is NOT what gets uploaded —
    the canonical SAM_RIVERA_* names are. This is the recruiter-visible filename contract."""
    resume = tmp_path / "Scale_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4 resume")
    cover = tmp_path / "Company_Cover.pdf"
    cover.write_bytes(b"%PDF-1.4 cover")
    job = {"id": "JOB-300", "company": "Scale AI", "title": "Applied AI Engineer"}

    ans = build_answers(profile_path=profile, job=job, resume_pdf=resume, cover_pdf=cover)
    # The uploaded basename (set_input_files / file-chooser use Path(path).name) is canonical.
    assert ans.resume_pdf.name == CANONICAL_RESUME_NAME
    assert ans.cover_pdf.name == CANONICAL_COVER_NAME
    # never the company/temp source name
    assert "Scale" not in ans.resume_pdf.name
    assert "Company" not in ans.cover_pdf.name
    # the canonical files carry the original bytes
    assert ans.resume_pdf.read_bytes() == b"%PDF-1.4 resume"
    assert ans.cover_pdf.read_bytes() == b"%PDF-1.4 cover"


def test_build_answers_no_cover_leaves_cover_none(profile, tmp_path):
    """No cover provided -> cover_pdf stays None (canonicalization is skipped, no crash)."""
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF")
    ans = build_answers(profile_path=profile, job={"id": "X"}, resume_pdf=resume, cover_pdf=None)
    assert ans.cover_pdf is None
    assert ans.resume_pdf.name == CANONICAL_RESUME_NAME
