"""Assemble the {field key -> value} map for an application from the applicant
profile, the job record, and prebuilt tailored resume/cover PDFs."""
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# G6 — canonical recruiter-visible upload filenames (feedback_apply_doc_filenames). Every doc
# uploaded to an ATS must carry the applicant's name + the doc type, NEVER a temp/Company_*/resume.pdf
# name (the uploaded basename is recruiter-visible on the ATS). The uploaded filename is the
# on-disk basename everywhere (set_input_files / file-chooser use Path(path).name), so we enforce
# the canonical name HERE, once, and every adapter attach inherits it.
CANONICAL_RESUME_NAME = "APPLICANT_Resume.pdf"
CANONICAL_COVER_NAME = "APPLICANT_Cover_Letter.pdf"


def canonical_upload_path(src, canonical_name: str) -> Path:
    """Return a path whose BASENAME is `canonical_name`, pointing at the same PDF bytes as `src`.

    If `src` already has the canonical basename, it's returned unchanged. Otherwise the file is
    COPIED to a temp file named `canonical_name` (in a fresh temp dir so two docs can't collide)
    and that path is returned — so the upload step uploads the canonical recruiter-visible name
    regardless of the on-disk source name (Scale_Resume.pdf, resume.pdf, a build temp, ...).
    Best-effort: on any copy error we fall back to the original path (a correctly-named upload is
    preferred, but never break an application over a rename)."""
    src = Path(src)
    if src.name == canonical_name:
        return src
    try:
        tmpdir = Path(tempfile.mkdtemp(prefix="aria_upload_"))
        dst = tmpdir / canonical_name
        shutil.copyfile(src, dst)
        return dst
    except Exception:
        return src


@dataclass
class Answers:
    values: dict
    resume_pdf: Path
    cover_pdf: Optional[Path]

    def get(self, key: str, default=None):
        return self.values.get(key, default)


def build_answers(profile_path: Path, job: dict, resume_pdf: Path,
                  cover_pdf: Optional[Path]) -> Answers:
    profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))

    unfilled = [k for k, v in profile.items() if v == "FILL_ME"]
    if unfilled:
        raise ValueError(f"applicant_profile has unfilled FILL_ME fields: {unfilled}")

    resume_pdf = Path(resume_pdf)
    if not resume_pdf.exists():
        raise FileNotFoundError(f"resume PDF not found: {resume_pdf}")
    if cover_pdf is not None:
        cover_pdf = Path(cover_pdf)
        if not cover_pdf.exists():
            raise FileNotFoundError(f"cover PDF not found: {cover_pdf}")

    # G6: ensure the uploaded basenames are the canonical recruiter-visible names, regardless of
    # the on-disk source name. Existence was validated above on the ORIGINAL paths; do this after.
    resume_pdf = canonical_upload_path(resume_pdf, CANONICAL_RESUME_NAME)
    if cover_pdf is not None:
        cover_pdf = canonical_upload_path(cover_pdf, CANONICAL_COVER_NAME)

    values = dict(profile)
    values["company"] = job.get("company", "")
    values["role"] = job.get("title", "")
    return Answers(values=values, resume_pdf=resume_pdf, cover_pdf=cover_pdf)
