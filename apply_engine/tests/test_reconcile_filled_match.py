"""Reconcile must credit the engine's STANDARD filled fields + uploaded docs against the live
required fields, even when the live names differ from the engine's semantic keys.

REGRESSION (JOB-296 Cognition / Ashby, 2026-06-18): Ashby labels its built-in fields
`_systemfield_name` / `_systemfield_resume` / a UUID for LinkedIn. The engine fills `full_name`,
`email`, `linkedin` + uploads the resume. An EXACT-match coverage check missed all three, so
reconcile reported 3 'unfilled required' fields -> a human_blocker -> a spurious Telegram halt,
while status still read ready_to_submit. The fix tokenizes filled keys + matches file fields by
uploaded doc kind."""
from apply_engine.form_spec import FormSpec, FieldSpec
from apply_engine.reconcile import reconcile_form


def _ashby_form(extra=None):
    fields = [
        FieldSpec(key="_systemfield_name", label="Name", required=True, widget_kind="text"),
        FieldSpec(key="_systemfield_email", label="Email", required=True, widget_kind="text"),
        FieldSpec(key="_systemfield_resume", label="Resume", required=True,
                  widget_kind="file", doc_kind="resume"),
        FieldSpec(key="b4c4a03b-8d23-4e7c", label="Linkedin profile", required=True,
                  widget_kind="text"),
    ]
    if extra:
        fields = fields + extra
    return FormSpec(ats="ashby", has_resume_field=True, fields=fields)


_FILLED_RECORD = {
    "filled_fields": ["full_name", "email", "linkedin"],
    "uploaded_docs": [{"doc": "resume", "name": "SAM_RIVERA_Resume.pdf"}],
    "custom_qs": [],
    "work_auth": [],
}


def test_ashby_systemfields_credited_no_false_unfilled():
    r = reconcile_form(_ashby_form(), _FILLED_RECORD)
    # full_name -> Name, linkedin -> Linkedin profile, uploaded resume -> _systemfield_resume
    assert r.unfilled_required_live == []
    assert not r.mismatched


def test_genuinely_missing_required_still_flagged():
    extra = [FieldSpec(key="github_url", label="GitHub URL", required=True, widget_kind="text")]
    r = reconcile_form(_ashby_form(extra), _FILLED_RECORD)
    labels = [o.live_label for o in r.unfilled_required_live]
    assert labels == ["GitHub URL"]      # the real gap flags; the filled three do not


def test_missing_resume_upload_still_flagged():
    # no uploaded_docs -> the required Resume file field IS genuinely unfilled
    rec = dict(_FILLED_RECORD, uploaded_docs=[])
    r = reconcile_form(_ashby_form(), rec)
    assert "Resume" in [o.live_label for o in r.unfilled_required_live]
