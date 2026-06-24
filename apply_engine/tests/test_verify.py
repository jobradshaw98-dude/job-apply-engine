from apply_engine.verify import verify_fields, VerifyResult


def test_all_match():
    intended = {"first_name": "Sam", "email": "j@x.com"}
    observed = {"first_name": "Sam", "email": "j@x.com"}
    r = verify_fields(intended, observed)
    assert isinstance(r, VerifyResult)
    assert r.ok is True
    assert r.mismatches == []


def test_normalizes_whitespace_and_case_for_email():
    intended = {"email": "sam.rivera@example.com"}
    observed = {"email": " sam.rivera@example.com "}
    assert verify_fields(intended, observed).ok is True


def test_detects_mismatch():
    intended = {"first_name": "Sam", "phone": "555-555-0100"}
    observed = {"first_name": "Jordab", "phone": "555-555-0100"}
    r = verify_fields(intended, observed)
    assert r.ok is False
    assert r.mismatches == [("first_name", "Sam", "Jordab")]


def test_missing_observed_field_is_mismatch():
    intended = {"first_name": "Sam"}
    observed = {}
    r = verify_fields(intended, observed)
    assert r.ok is False
    assert r.mismatches[0][0] == "first_name"


def test_url_field_tolerates_scheme_and_www_normalization():
    # Lever (live) normalizes https://www.linkedin.com/... -> http://linkedin.com/...
    intended = {"linkedin": "https://www.linkedin.com/in/sam-rivera"}
    observed = {"linkedin": "http://linkedin.com/in/sam-rivera"}
    assert verify_fields(intended, observed).ok is True


def test_url_field_tolerates_trailing_slash():
    intended = {"portfolio_url": "https://sam.dev"}
    observed = {"portfolio_url": "https://sam.dev/"}
    assert verify_fields(intended, observed).ok is True


def test_url_field_still_catches_a_wrong_path():
    # tolerance must NOT extend to a genuinely different profile
    intended = {"linkedin": "https://www.linkedin.com/in/sam-rivera"}
    observed = {"linkedin": "https://www.linkedin.com/in/someone-else"}
    assert verify_fields(intended, observed).ok is False


def test_non_url_value_is_not_loosened():
    # a plain (non-URL) field must still compare strictly (case-sensitive)
    intended = {"first_name": "Sam"}
    observed = {"first_name": "sam"}
    assert verify_fields(intended, observed).ok is False


def test_phone_field_tolerates_separator_reformat():
    # Reducto (live) re-rendered the same number with spaces instead of dashes — same number,
    # must NOT flag a verification mismatch and abort the finish.
    intended = {"phone": "+1 555-555-0100"}
    observed = {"phone": "+1 555-555-0100"}
    assert verify_fields(intended, observed).ok is True


def test_phone_field_tolerates_dropped_country_code():
    intended = {"phone": "+1 555-555-0100"}
    observed = {"phone": "555-555-0100"}
    assert verify_fields(intended, observed).ok is True


def test_phone_field_still_catches_a_different_number():
    intended = {"phone": "+1 555-555-0100"}
    observed = {"phone": "+1 555-555-0199"}
    assert verify_fields(intended, observed).ok is False
