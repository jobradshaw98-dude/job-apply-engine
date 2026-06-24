"""Answer-generation logic with injected (mock) drafter + gate. No network/LLM."""
from apply_engine.answer_gen import (build_prompt, build_refine_prompt, generate,
                                      is_personal_commitment)
from apply_engine.questions import Question


def _q(label, kind="essay"):
    return Question(label=label, kind=kind, selector="#x")


def test_essay_drafted_when_supported_and_clean():
    qs = [_q("Why do you want to work here?", "essay")]
    llm = lambda p: "I build physics simulations and AI automation at Meridian."
    audit = lambda t: []  # clean
    a = generate(qs, "FACTS...", llm, audit)[0]
    assert a.status == "drafted"
    assert "Meridian" in a.value


def test_short_factual_answered():
    qs = [_q("Have you used ANSYS?", "short_text")]
    a = generate(qs, "FACTS: FEA (ANSYS)", lambda p: "Yes — extensively for FEA.", lambda t: [])[0]
    assert a.status == "answered"


def test_decline_is_left_for_sam():
    qs = [_q("How many years did you work at Google?", "short_text")]
    a = generate(qs, "FACTS...", lambda p: "DECLINE", lambda t: [])[0]
    assert a.status == "declined"
    assert a.value == ""


def test_blocked_when_gate_flags_fabrication():
    qs = [_q("Describe your AI platform", "essay")]
    llm = lambda p: "I run a production multi-agent platform at platform scale."
    audit = lambda t: ["Overstatement — ARIA serves one user"]   # gate catches it
    a = generate(qs, "FACTS...", llm, audit)[0]
    assert a.status == "blocked"
    assert "Overstatement" in a.reason


def test_audit_error_fails_safe_to_blocked():
    def bad_audit(t):
        raise RuntimeError("gate down")
    a = generate([_q("Q", "essay")], "F", lambda p: "text", bad_audit)[0]
    assert a.status == "blocked"


def test_prompt_instructs_decline_and_facts():
    p = build_prompt("Why us?", "essay", "FACT-A")
    assert "DECLINE" in p
    assert "FACT-A" in p
    assert "ONLY" in p.upper()


def test_prompt_carries_craft_brief():
    """The drafting prompt must be a real writing brief, not 'write N sentences'."""
    p = build_prompt("Why us?", "essay", "F").lower()
    assert "punctuat" in p            # mechanics are demanded
    assert "concrete example" in p    # depth/specificity is demanded
    assert "throat-clearing" in p     # no generic openers


def test_refine_prompt_includes_draft_and_decline():
    rp = build_refine_prompt("Why us?", "my draft text", "FACT-A")
    assert "my draft text" in rp
    assert "Why us?" in rp
    assert "FACT-A" in rp
    assert "DECLINE" in rp


def _classify_pass(prompt: str) -> str:
    """Identify which generation pass a prompt belongs to, by its distinctive opening."""
    if "through ONE lens" in prompt:
        return "package"          # P2 package-critic lens (runs over the whole set)
    if "demanding reviewer" in prompt:
        return "critique"
    if "after a sharp reviewer flagged" in prompt:
        return "revise"
    if "writing editor" in prompt:
        return "refine"
    return "draft"


def _per_question(seen):
    """Per-question passes only — drop the parallel P2 package-lens calls (nondeterministic order)."""
    return [s for s in seen if s != "package"]


def test_essay_runs_full_quality_loop_when_critique_finds_issues():
    """Essays run draft → refine → critique → revise. When the critic flags weaknesses, the
    revised text is what ships (the self-critique loop's whole point)."""
    seen = []

    def llm(p):
        kind = _classify_pass(p)
        seen.append(kind)
        return {
            "draft": "flat first draft",
            "refine": "Polished, deeper answer.",
            "critique": "- The opening restates the question.",  # non-PASS → triggers revise
            "revise": "Sharp, revised answer with a specific lead.",
            "package": "PASS",                                   # P2 lenses find nothing
        }[kind]

    # Force the full pipeline (defaults are conservative: refine off, package only on >=3 essays).
    a = generate([_q("Why us?", "essay")], "FACTS", llm, lambda t: [],
                 refine=True, package_min_essays=1)[0]
    assert _per_question(seen) == ["draft", "refine", "critique", "revise"]
    assert seen.count("package") == 3                            # the 3 lenses ran in parallel
    assert a.status == "drafted"
    assert a.value == "Sharp, revised answer with a specific lead."  # revised text ships


def test_default_is_conservative_no_refine_no_package_on_small_job():
    """Defaults (measured A/B): skip the refine pass and skip the package critic on a <3-essay job
    — fewer ~50k-token calls, equal-or-better quality. A 2-essay run should make NO refine and NO
    package calls by default."""
    seen = []

    def llm(p):
        kind = _classify_pass(p)
        seen.append(kind)
        return {"draft": "a draft", "refine": "REFINED", "critique": "PASS", "revise": "REV",
                "package": "FIX 1: x"}.get(kind, "PASS")

    out = generate([_q("Q1", "essay"), _q("Q2", "essay")], "FACTS", llm, lambda t: [])
    assert "refine" not in seen          # refine pass skipped by default
    assert "package" not in seen         # package skipped (<3 essays) by default
    assert all(a.status == "drafted" for a in out)


def test_essay_critique_pass_short_circuits_revision():
    """When the critic returns PASS, no revise pass runs and the refined draft ships as-is."""
    seen = []

    def llm(p):
        kind = _classify_pass(p)
        seen.append(kind)
        return {
            "draft": "flat first draft",
            "refine": "Polished, deeper answer.",
            "critique": "PASS",
            "package": "PASS",
        }[kind]

    a = generate([_q("Why us?", "essay")], "FACTS", llm, lambda t: [],
                 refine=True, package_min_essays=1)[0]
    assert _per_question(seen) == ["draft", "refine", "critique"]   # no revise pass
    assert a.status == "drafted"
    assert a.value == "Polished, deeper answer."     # refined text ships


def test_essay_emdashes_autofixed_not_blocked():
    """A strong essay with too many em-dashes must be content-neutrally de-dashed before the gate,
    not blocked. Regression for JOB-233 Q1 (3 em-dashes blocked an excellent reframe answer)."""
    dashy = "Lead — a strong point — drawn from the work. Another clause — here too."

    def llm(p):
        kind = _classify_pass(p)
        if kind == "package":
            return "PASS"
        if kind == "critique":
            return "PASS"
        return dashy   # draft + refine both return em-dash-laden text

    # A real em-dash gate: block > 2 em-dashes (mirrors make_audit_fn's backstop).
    def audit(t):
        return ["too many em-dashes"] if t.count("—") > 2 else []

    a = generate([_q("Tell us about your work.", "essay")], "FACTS", llm, audit)[0]
    assert a.status == "drafted"          # NOT blocked
    assert a.value.count("—") <= 1        # de-dashed before the gate


def test_short_answer_is_single_pass():
    """Short factual answers stay crisp — no refine pass, no bloat."""
    calls = []

    def llm(p):
        calls.append(p)
        return "Yes — extensively for FEA."

    a = generate([_q("Used ANSYS?", "short_text")], "FACTS", llm, lambda t: [])[0]
    assert len(calls) == 1
    assert a.status == "answered"


def test_refine_failure_falls_back_to_draft():
    """If the editor pass errors, the clean draft still ships (quality floor, not a block)."""
    def llm(p):
        if "DRAFT:" in p:
            raise RuntimeError("refine model down")
        return "solid grounded draft"

    a = generate([_q("Why us?", "essay")], "FACTS", llm, lambda t: [])[0]
    assert a.status == "drafted"
    assert a.value == "solid grounded draft"


def test_commitment_questions_detected():
    # GENUINE personal commitments only Sam can answer -> still declined
    assert is_personal_commitment("What is your expected salary?")
    assert is_personal_commitment("What is your earliest start date?")
    assert is_personal_commitment("Are you able to travel up to 25%?")
    # genuine essays must NOT be swept up
    assert not is_personal_commitment("Describe the most interesting project you've built.")
    assert not is_personal_commitment("Why do you want to work here?")
    assert not is_personal_commitment("What FEA tools are you most proficient with?")


def test_office_freetext_pre_answered_yes_no_model():
    # JOB-281: a free-text-rendered office question is answered 'Yes' before the drafter,
    # never sent to the model (which previously mis-declined / parse-failed it -> false halt).
    from apply_engine.answer_gen import draft_single_call
    calls = []

    def agent(p):
        calls.append(p)
        return '{"answers":[]}'

    out = draft_single_call(
        [_q("Are you willing to work four days per week in our San Francisco office?", "short_text")],
        "FACTS", agent, lambda t: [])
    assert out[0].status == "answered" and out[0].value == "Yes"
    assert calls == []          # never reached the model


def test_office_and_relocation_are_not_declined_commitments():
    # POLICY REVERSAL (feedback_office_commitment_answer / JOB-281): in-office / RTO / relocation
    # are AUTO-YES now, NOT personal commitments to decline. is_personal_commitment must return
    # False for them so the office-Yes guard answers instead of the engine false-halting.
    assert not is_personal_commitment(
        "This is a hybrid role. Are you able to commit to being in the office 3x per week?")
    assert not is_personal_commitment("Are you willing to relocate to New York?")
    assert not is_personal_commitment(
        "Are you willing to work four days per week in our San Francisco office?")


def test_commitment_question_declined_without_llm():
    """A logistics/commitment question is declined and never reaches the model."""
    calls = []

    def llm(p):
        calls.append(p)
        return "Yes, I can commit to being in the SF office three days a week."

    # use a GENUINE personal commitment (pay) — office/relocation are AUTO-YES now, not declined
    a = generate([_q("What is your expected salary for this role?", "essay")],
                 "FACTS", llm, lambda t: [])[0]
    assert a.status == "declined"
    assert "commitment" in a.reason.lower()
    assert calls == []          # the model was never asked
    assert a.value == ""        # nothing filled — left blank for Sam


def test_later_answers_see_prior_examples_for_diversity():
    """The repetition fix: each essay after the first is told which examples the prior answers
    used, so the model can pick different evidence. We capture the prompts and assert the 2nd
    answer's prompt carries the diversity instruction + the 1st answer's content; the 1st does
    not. (refine passes are skipped here by returning the draft unchanged.)"""
    prompts = []
    drafts = iter([
        "I built ARIA, a multi-agent platform I run daily to manage my career and finances.",
        "For my MASc thesis I built an automated design-optimization framework in ANSYS.",
    ])

    def llm(p):
        prompts.append(p)
        if "DRAFT:" in p:           # refine pass: return the draft body unchanged
            return p.split("DRAFT:\n", 1)[1].strip()
        return next(drafts)

    qs = [_q("Most complex project?", "essay"), _q("What do you optimize for?", "essay")]
    out = generate(qs, "FACTS", llm, lambda t: [])
    assert [a.status for a in out] == ["drafted", "drafted"]

    # First DRAFT prompt: no diversity block (nothing used yet).
    first = prompts[0]
    assert "ANSWER DIVERSITY" not in first

    # The draft prompt for the SECOND question must carry the diversity block naming the 1st answer.
    second_draft = next(p for p in prompts if "What do you optimize for?" in p and "DRAFT:" not in p)
    assert "ANSWER DIVERSITY" in second_draft
    assert "ARIA" in second_draft   # the prior answer's example is surfaced as already-used


def test_build_prompt_diversity_block_only_when_used():
    assert "ANSWER DIVERSITY" not in build_prompt("Q?", "essay", "FACTS", used_examples="")
    p = build_prompt("Q?", "essay", "FACTS", used_examples="- I built ARIA ...")
    assert "ANSWER DIVERSITY" in p and "ARIA" in p


# ---- P2: package-level parallel critic fleet ----
from apply_engine.answer_gen import Answer, critique_and_revise_package, _parse_package_findings


def _essay(label, value):
    return Answer(label=label, selector="#x", kind="essay", value=value, status="drafted")


def test_parse_package_findings_extracts_fixes_and_ignores_pass():
    assert _parse_package_findings("PASS") == {}
    parsed = _parse_package_findings("FIX 2: swap the reused Codex story\nFIX 2: tighten the lead\nnoise")
    assert parsed == {2: ["swap the reused Codex story", "tighten the lead"]}


def test_package_critic_revises_only_flagged_answer():
    """A lens flags answer #2 for duplication; only that answer is revised, the other untouched."""
    a1 = _essay("Why us?", "Answer one, leans on the ARIA story.")
    a2 = _essay("Tell us about a project.", "Answer two, also leans on the ARIA story.")

    def llm(p):
        if "through ONE lens" in p:
            # range/duplication lens flags #2; others find nothing
            return "FIX 2: swap to a different example than answer 1." if "range/duplication" in p else "PASS"
        if "after a sharp reviewer flagged" in p:
            return "Answer two, now grounded in the MASc thesis instead."
        return "PASS"

    out = critique_and_revise_package([a1, a2], "FACTS", llm, lambda t: [])
    assert out[0].value == "Answer one, leans on the ARIA story."          # untouched
    assert out[1].value == "Answer two, now grounded in the MASc thesis instead."  # revised


def test_package_critic_all_pass_leaves_set_unchanged():
    a1, a2 = _essay("Q1", "alpha"), _essay("Q2", "beta")
    out = critique_and_revise_package([a1, a2], "FACTS", lambda p: "PASS", lambda t: [])
    assert [a.value for a in out] == ["alpha", "beta"]


def test_package_revise_that_fails_audit_keeps_original():
    """If the revised text trips the fabrication gate, the original answer is kept (no unvetted swap)."""
    a1 = _essay("Q1", "original grounded answer")

    def llm(p):
        if "through ONE lens" in p:
            return "FIX 1: make it punchier" if "voice/authenticity" in p else "PASS"
        return "fabricated punchier answer"

    out = critique_and_revise_package([a1], "FACTS", llm, lambda t: ["BLOCK: fabrication"])
    assert out[0].value == "original grounded answer"  # blocked revise → original kept


# ---- single-call drafter (2026-06-17) ----
import json as _json
from apply_engine.answer_gen import draft_single_call, _parse_single_call


def test_parse_single_call_object_and_array_shapes():
    obj = '{"answers":[{"n":1,"text":"A1"},{"n":2,"text":"A2"}]}'
    arr = '[{"id":1,"answer":"A1"},{"id":2,"answer":"A2"}]'
    assert _parse_single_call(obj) == {1: "A1", 2: "A2"}
    assert _parse_single_call(arr) == {1: "A1", 2: "A2"}
    assert _parse_single_call("```json\n" + obj + "\n```") == {1: "A1", 2: "A2"}
    assert _parse_single_call("not json at all") == {}


def test_single_call_drafts_all_in_one_call():
    calls = []

    def agent(p):
        calls.append(p)
        return '{"answers":[{"n":1,"text":"First answer."},{"n":2,"text":"Second answer."}]}'

    out = draft_single_call([_q("Q1", "essay"), _q("Q2", "essay")], "FACTS", agent, lambda t: [])
    assert len(calls) == 1                       # ONE call for the whole set
    assert [a.status for a in out] == ["drafted", "drafted"]
    assert [a.value for a in out] == ["First answer.", "Second answer."]


def test_single_call_repair_retry_on_bad_json():
    calls = []

    def agent(p):
        calls.append(p)
        return "garbage, not json" if len(calls) == 1 else '{"answers":[{"n":1,"text":"Recovered."}]}'

    out = draft_single_call([_q("Q1", "essay")], "FACTS", agent, lambda t: [])
    assert len(calls) == 2                       # draft + ONE repair retry
    assert out[0].status == "drafted" and out[0].value == "Recovered."


def test_single_call_escalates_after_double_parse_failure():
    out = draft_single_call([_q("Q1", "essay"), _q("Q2", "essay")], "FACTS",
                            lambda p: "still not json", lambda t: [])
    assert all(a.status == "declined" and "needs Sam" in a.reason for a in out)


def test_single_call_skips_personal_commitment_without_model():
    calls = []

    def agent(p):
        calls.append(p)
        return '{"answers":[{"n":1,"text":"Real answer."}]}'

    # genuine commitment (pay) — relocation/office are AUTO-YES now, no longer declined
    qs = [_q("What is your expected salary?", "essay"), _q("Tell us about a project.", "essay")]
    out = draft_single_call(qs, "FACTS", agent, lambda t: [])
    assert out[0].status == "declined"           # commitment, no model
    assert out[1].status == "drafted" and out[1].value == "Real answer."
    # the commitment question must NOT appear in the prompt sent to the model
    assert "salary" not in calls[0]


def test_single_call_people_count_passes_audit():
    """With the relaxed answer gate (mock returns no block), a people-count answer ships drafted."""
    txt = "My agent replaced a recurring ten-person, two-hour cross-team review."
    out = draft_single_call([_q("Q1", "essay")], "FACTS",
                            lambda p: _json.dumps({"answers": [{"n": 1, "text": txt}]}),
                            lambda t: [])           # gate returns no blocks
    assert out[0].status == "drafted" and "ten-person" in out[0].value


def test_single_call_blocked_when_gate_blocks():
    out = draft_single_call([_q("Q1", "essay")], "FACTS",
                            lambda p: '{"answers":[{"n":1,"text":"fabricated claim"}]}',
                            lambda t: ["BLOCK: fabrication"])
    assert out[0].status == "blocked"


def test_single_call_decline_token():
    out = draft_single_call([_q("Q1", "essay")], "FACTS",
                            lambda p: '{"answers":[{"n":1,"text":"DECLINE"}]}', lambda t: [])
    assert out[0].status == "declined"
