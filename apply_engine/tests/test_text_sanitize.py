"""Regression tests for the editor-preamble leak fix (shared text_sanitize module).

The leak that shipped on JOB-237 ("Why Anthropic?") and JOB-234 came from an LLM edit reply whose
scaffolding ("One word change, everything else verbatim:\n\n---", a mid-text '---' fence, a
self-critique) landed inside the stored answer value. strip_editor_preamble removes the leading
form at write time; has_editor_leak is the deterministic submit-gate backstop for any variant.
"""
from apply_engine.text_sanitize import has_editor_leak, strip_editor_preamble


# ---- strip_editor_preamble ----

def test_strip_removes_job237_leak():
    leaked = "One word change, everything else verbatim:\n\n---\n\nClaude Code has been my harness."
    assert strip_editor_preamble(leaked) == "Claude Code has been my harness."


def test_strip_removes_here_is_preamble():
    assert strip_editor_preamble("Here is the revised answer:\n\nEvery agent I built taught me.") \
        == "Every agent I built taught me."


def test_strip_removes_bare_leading_fence():
    assert strip_editor_preamble("---\nThe work speaks for itself.") == "The work speaks for itself."


def test_strip_leaves_clean_answer_untouched():
    clean = "Claude Code has been my harness. I build agents daily."
    assert strip_editor_preamble(clean) == clean


def test_strip_does_not_eat_mid_body_dash_fence():
    # a '---' that is part of the body (not a leading editor fence) must survive the strip
    body = "I optimize for understanding.\n\n---\n\nThat is the honest answer."
    assert strip_editor_preamble(body) == body


def test_strip_passes_through_decline_and_empty():
    assert strip_editor_preamble("DECLINE") == "DECLINE"
    assert strip_editor_preamble("") == ""


# ---- has_editor_leak (deterministic gate backstop) ----

def test_leak_detected_on_leading_meta_line():
    assert has_editor_leak("One word change, everything else verbatim:\n\n---\n\nReal answer.")


def test_leak_detected_on_mid_text_fence_job234():
    # JOB-234 shape: a self-critique the strip anchors miss, but it carries a bare '---' fence
    txt = "The only violation is in paragraph 2: the people-count phrasing.\n---\nReal answer body."
    assert has_editor_leak(txt)


def test_leak_not_flagged_on_clean_answer():
    assert not has_editor_leak("Claude Code has been my harness. I build agents daily.")
    assert not has_editor_leak("")


def test_strip_output_passes_the_leak_gate():
    # the contract: after stripping, the surviving body must not trip the deterministic backstop
    leaked = "Revised:\n\n---\n\nEvery agent I built taught me something real."
    assert not has_editor_leak(strip_editor_preamble(leaked))
