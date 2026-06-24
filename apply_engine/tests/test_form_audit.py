from apply_engine.form_audit import audit_fields, Correction, index_rows


def test_index_rows_marks_duplicate_labels_ambiguous():
    # Two fields share the label "email" -> that label's selector becomes "" so
    # apply_corrections refuses to overwrite either (the wrong-field-overwrite guard).
    rows = [
        ("current company", "BUILDS", '[name="org"]'),
        ("email", "a@x.com", '[name="email1"]'),
        ("email", "b@x.com", '[name="email2"]'),
    ]
    observed, selmap = index_rows(rows)
    assert observed["current company"] == "BUILDS"
    assert selmap["current company"] == '[name="org"]'  # unique -> keeps its selector
    assert selmap["email"] == ""                         # ambiguous -> refuse to overwrite


def test_index_rows_keeps_unique_selectors():
    rows = [("current company", "BUILDS", '[name="org"]')]
    _, selmap = index_rows(rows)
    assert selmap["current company"] == '[name="org"]'


def test_audit_does_not_touch_custom_questions_with_generic_keywords():
    # The always-on audit must NOT overwrite a custom short-text question whose label merely
    # contains a generic location/employer token (regression for reviewer WARN, 2026-06-01).
    observed = {
        "which city would you relocate from?": "New York",
        "describe your ideal employer": "a mission-driven AI lab",
    }
    known = {"current_location": "Austin, TX", "current_company": "Meridian Devices"}
    assert audit_fields(observed, known) == []


def _known():
    return {
        "current_company": "Meridian Devices",
        "current_location": "Carlsbad, CA",
        "full_name": "Sam Rivera",
        "email": "sam.rivera@example.com",
        "phone": "+1 555-555-0100",
        "linkedin": "https://www.linkedin.com/in/sam-rivera",
    }


def test_overwrites_wrong_current_company_the_builds_case():
    # The real bug observed today: Lever auto-filled "BUILDS" from a resume heading.
    observed = {"current company": "BUILDS"}
    corrections = audit_fields(observed, _known())
    assert len(corrections) == 1
    c = corrections[0]
    assert isinstance(c, Correction)
    assert c.label == "current company"
    assert c.current == "BUILDS"
    assert c.correct == "Meridian Devices"
    assert c.action == "overwrite"


def test_employer_synonym_label_maps_to_current_company():
    observed = {"current employer": "BUILDS"}
    corrections = audit_fields(observed, _known())
    assert len(corrections) == 1
    assert corrections[0].action == "overwrite"
    assert corrections[0].correct == "Meridian Devices"


def test_current_location_overwrite():
    observed = {"current location": "Austin, TX"}
    corrections = audit_fields(observed, _known())
    assert len(corrections) == 1
    assert corrections[0].action == "overwrite"
    assert corrections[0].correct == "Carlsbad, CA"


def test_no_correction_when_values_already_match():
    observed = {"current company": "Meridian Devices"}
    assert audit_fields(observed, _known()) == []


def test_match_is_case_and_space_insensitive():
    observed = {"current company": "  Meridian Devices "}
    assert audit_fields(observed, _known()) == []


def test_flag_when_identity_field_present_but_no_known_value():
    # Label clearly maps to an identity/employment key, but known value is empty.
    known = _known()
    known["current_company"] = ""
    observed = {"current company": "BUILDS"}
    corrections = audit_fields(observed, known)
    assert len(corrections) == 1
    assert corrections[0].action == "flag"
    assert corrections[0].correct == ""
    assert corrections[0].current == "BUILDS"


def test_flag_when_known_key_entirely_missing():
    known = _known()
    del known["current_company"]
    observed = {"current company": "BUILDS"}
    corrections = audit_fields(observed, known)
    assert len(corrections) == 1
    assert corrections[0].action == "flag"


def test_never_corrects_non_identity_custom_label():
    observed = {
        "why do you want to work here": "BUILDS",
        "describe a challenge you faced": "Meridian Devices",
        "salary expectations": "120000",
    }
    assert audit_fields(observed, _known()) == []


def test_empty_observed_value_is_ignored_not_overwritten():
    # An empty form field is the filler's job, not the auditor's; don't propose anything.
    observed = {"current company": ""}
    assert audit_fields(observed, _known()) == []


def test_url_field_tolerates_benign_normalization():
    observed = {"linkedin profile": "http://linkedin.com/in/sam-rivera"}
    # Same profile, just scheme/www stripped by the site — no correction.
    assert audit_fields(observed, _known()) == []


def test_url_field_wrong_path_is_overwritten():
    observed = {"linkedin profile": "https://linkedin.com/in/someone-else"}
    corrections = audit_fields(observed, _known())
    assert len(corrections) == 1
    assert corrections[0].action == "overwrite"


def test_email_mismatch_overwritten_case_insensitive_match_ok():
    assert audit_fields({"email address": "sam.rivera@example.com"}, _known()) == []
    bad = audit_fields({"email address": "wrong@gmail.com"}, _known())
    assert len(bad) == 1 and bad[0].action == "overwrite"


def test_name_field_maps():
    bad = audit_fields({"full name": "BUILDS Rivera"}, _known())
    assert len(bad) == 1
    assert bad[0].correct == "Sam Rivera"
    assert bad[0].action == "overwrite"


def test_phone_field_maps():
    bad = audit_fields({"phone number": "000-000-0000"}, _known())
    assert len(bad) == 1
    assert bad[0].action == "overwrite"


def test_multiple_corrections_returned_together():
    observed = {
        "current company": "BUILDS",
        "current location": "Austin, TX",
        "why us": "long essay text here",
    }
    corrections = audit_fields(observed, _known())
    labels = {c.label for c in corrections}
    assert labels == {"current company", "current location"}


def test_label_matching_is_case_insensitive():
    observed = {"Current Company": "BUILDS"}
    corrections = audit_fields(observed, _known())
    assert len(corrections) == 1
    assert corrections[0].action == "overwrite"
