"""Pure unit tests for wd_widgets.score_option_match — no browser required.

The dangerous false positive these guard against: naive substring matching made "No"
match the word "now" inside the *Yes* option ("...sponsorship now or in the future..."),
which staged the WRONG immigration answer on a live Workday application. Word-start /
exact matching must beat substring.
"""
from apply_engine.wd_widgets import score_option_match, esc_id


def test_no_does_not_match_now_in_yes_option():
    # the real, dangerous false positive — must score 0
    opt = "Yes, I will need sponsorship now or in the future"
    assert score_option_match(opt, "No") == 0


def test_no_word_start_matches_real_no_option():
    opt = "No, I do not need sponsorship now or in the future"
    assert score_option_match(opt, "No") == 2


def test_exact_match():
    assert score_option_match("California", "California") == 2
    assert score_option_match("No", "No") == 2


def test_substring_fallback():
    assert score_option_match("Extremely familiar", "Familiar") == 1


def test_no_match():
    assert score_option_match("Mobile", "Landline") == 0


def test_word_start_at_boundary_not_midword():
    # "United States" word-starts the option -> 2
    assert score_option_match("United States of America (+1)", "United States") == 2
    # a target that only appears MID-word (not at a word boundary) must NOT match —
    # this is the same structural guard that rejects "No" inside "now".
    assert score_option_match("Reunited", "united") == 0


def test_empty_target_is_no_match():
    assert score_option_match("anything", "") == 0


def test_esc_id_escapes_double_dash():
    assert esc_id("name--legalName--firstName") == "#name\\-\\-legalName\\-\\-firstName"
