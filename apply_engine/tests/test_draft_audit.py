"""drafts_for_audit pulls the LLM-GENERATED answers out of a run's `generated`/`custom_qs`
records — exactly the ones that need the career-draft-auditor (judgment) gate before they
reach Sam. Deterministic answers Sam would never question aren't the concern; invented
essay/short-text content is. declined/blocked/fill_error and empty values are excluded (nothing
was filled, so nothing to audit)."""
from apply_engine.draft_audit import drafts_for_audit, load_job_drafts
import json


def test_returns_only_llm_filled_answers_with_text():
    generated = [
        {"q": "Why do you want to work here?", "kind": "essay",
         "status": "drafted", "value": "Because ARIA taught me to..."},
        {"q": "Languages?", "kind": "checkbox_group", "status": "answered",
         "values": ["English"]},  # multi-select: has values, audited too
        {"q": "Favorite LLM project?", "kind": "essay", "status": "drafted",
         "value": "I built ARIA, a multi-agent system."},
        {"q": "University", "kind": "select", "status": "declined",
         "reason": "too many options"},                    # not filled -> skip
        {"q": "Why Palantir?", "kind": "essay", "status": "blocked",
         "reason": "gate flagged"},                         # gate blocked -> skip
        {"q": "Portfolio URL", "kind": "short_text", "status": "answered",
         "value": ""},                                      # empty -> skip
    ]
    out = drafts_for_audit(generated)
    questions = [d["question"] for d in out]
    assert "Why do you want to work here?" in questions
    assert "Favorite LLM project?" in questions
    assert "Languages?" in questions
    assert "University" not in questions          # declined
    assert "Why Palantir?" not in questions       # blocked
    assert "Portfolio URL" not in questions       # empty value
    # carries the answer text so the auditor can trace it to the claims ledger
    why = next(d for d in out if d["question"] == "Why do you want to work here?")
    assert why["answer"] == "Because ARIA taught me to..."
    assert why["kind"] == "essay"
    # checkbox group answer is rendered from its values
    langs = next(d for d in out if d["question"] == "Languages?")
    assert "English" in langs["answer"]


def test_empty_and_missing_generated_is_safe():
    assert drafts_for_audit([]) == []
    assert drafts_for_audit(None) == []


def test_load_job_drafts_reads_manifest(tmp_path):
    manifest = tmp_path / "staged_applications.json"
    manifest.write_text(json.dumps([
        {"job_id": "JOB-1", "custom_qs": [
            {"q": "Why us?", "kind": "essay", "status": "drafted", "value": "Because X."}]},
        {"job_id": "JOB-2", "custom_qs": [
            {"q": "Other", "kind": "essay", "status": "drafted", "value": "Y."}]},
    ]), encoding="utf-8")
    out = load_job_drafts(manifest, "JOB-1")
    assert len(out) == 1
    assert out[0]["question"] == "Why us?"
    # unknown job -> empty, never raises
    assert load_job_drafts(manifest, "JOB-NOPE") == []
    assert load_job_drafts(tmp_path / "missing.json", "JOB-1") == []
