"""Draft grounded answers to custom application questions, gated against fabrication.

Two safety layers, both injectable for testing:
  llm_fn(prompt) -> str     : the drafter (real = Claude). Must be told to use ONLY the
                              provided FACTS and to output DECLINE if a question needs a
                              fact that isn't supported.
  audit_fn(text) -> [str]   : the deterministic fabrication/overstatement gate
                              (real = career/audit_gate.py). Any block kills the answer.

Status per answer:
  answered  : short factual, supported, passed the gate (safe to fill)
  drafted   : essay draft in the applicant's voice, passed the gate — FILL but flag for review
  declined  : the drafter returned DECLINE (fact not supported) — left for the user
  blocked   : the gate caught a forbidden/overstated phrase — left for the user
"""
import json
import re
from dataclasses import dataclass

DECLINE = "DECLINE"

# Personal-commitment / logistics questions the engine must NEVER auto-answer: it cannot
# ground the applicant's real constraints (where they live, what they'll commit to, their start date,
# pay expectations), and a wrong commitment is worse than a blank. These are always left
# for the user. Matched case-insensitively against the question text. Kept tight so genuine
# essays ("describe a project") are not swept up.
_COMMITMENT_PATTERNS = [
    r"\brelocat",                              # relocate / relocation
    r"\bcommut",                               # commute / commuting
    r"\bin[- ]?office\b", r"\bin the office\b", r"\bon[- ]?site\b", r"\bin[- ]?person\b",
    r"\bdays (?:per|a) week\b", r"\bcommit to being\b", r"\bable to commit\b",
    r"\bwilling to (?:relocate|travel|commute)\b", r"\b(?:able|willing) to travel\b",
    r"\btravel (?:requirement|up to|\d)", r"%\s*travel\b",
    r"\bstart date\b", r"\bwhen can you start\b", r"\bnotice period\b",
    r"\bavailable to start\b", r"\bearliest (?:start|availability)\b",
    r"\bsalary\b", r"\bcompensation expectation", r"\bdesired (?:salary|compensation|pay)\b",
    r"\bexpected (?:salary|pay|compensation)\b", r"\bpay expectation", r"\brate expectation",
]
_COMMITMENT_RE = re.compile("|".join(_COMMITMENT_PATTERNS), re.IGNORECASE)


def is_personal_commitment(question: str) -> bool:
    """True for logistics/commitment/availability/pay questions only the user can answer.

    EXCEPTION (JOB-281 Together AI): in-office / RTO / relocation are NO LONGER personal
    commitments to decline — policy reversed them to AUTO-YES (feedback_office_commitment_answer).
    Without this carve-out the office/relocation pattern here would DECLINE the office question
    before the auto-Yes guard could answer it, causing a false HALT. Genuine commitments
    (pay / start-date / travel) still return True."""
    if not _COMMITMENT_RE.search(question or ""):
        return False
    try:
        from .office_commitment import classify_office_commitment, OfficeCommitmentDecision
        if classify_office_commitment(question) == OfficeCommitmentDecision.AUTO_YES:
            return False
    except Exception:  # noqa: BLE001 — never let the guard import break the decline path
        pass
    return True


def is_office_auto_yes(question: str) -> bool:
    """True for in-office / RTO / relocation questions the policy answers YES (not the user's to
    decide here). Some forms render these as a free-text / short_text field rather than a Yes/No
    widget (JOB-281 Together AI), so they reach the essay drafter — answer 'Yes' before drafting
    instead of sending a screen-out gate to the model (which mis-declined / parse-failed it)."""
    try:
        from .office_commitment import classify_office_commitment, OfficeCommitmentDecision
        return classify_office_commitment(question or "") == OfficeCommitmentDecision.AUTO_YES
    except Exception:  # noqa: BLE001
        return False


# Editor-preamble stripping lives in the shared text_sanitize module so the fresh-draft path
# (here) and the minimal-edit/convergence path (regen_answer) can never drift — the leak that
# shipped on JOB-237 came through the regen path precisely because only this module had the guard.
# Re-exported under the old private name so existing call sites/tests keep working.
from .text_sanitize import strip_editor_preamble as _strip_editor_preamble  # noqa: E402
from .text_sanitize import reduce_emdashes as _reduce_emdashes  # noqa: E402


@dataclass
class Answer:
    label: str
    selector: str
    kind: str
    value: str = ""
    status: str = ""
    reason: str = ""


def _diversity_block(used_examples: str) -> str:
    """Instruction injected into 2nd+ answers for the SAME application so each answer draws on
    DIFFERENT evidence instead of recycling the same hero examples (ARIA, the same Meridian
    agents) across every question. Empty for the first answer."""
    if not (used_examples or "").strip():
        return ""
    return (
        "ANSWER DIVERSITY (important — this is one of several answers in the SAME application):\n"
        "Your OTHER answers to this application have ALREADY drawn on these examples:\n"
        f"{used_examples}\n"
        "For THIS answer, lead with and build on DIFFERENT experiences from the FACTS. Do NOT "
        "re-tell the same projects, metrics, or hero examples used above unless this specific "
        "question genuinely cannot be answered any other way. You have a broad record — spread it "
        "across the application (e.g. a different project, your MASc thesis/research, a different "
        "role, tool, or domain) so the whole set shows range, not one story repeated.\n\n"
    )


def build_prompt(question, kind: str, facts: str, used_examples: str = "") -> str:
    """A real writing brief — not 'write N sentences'.

    The FACTS blob carries a VOICE & CRAFT section (who the applicant is + how they write)
    ahead of the hard factual grounding (resume + vetted claims). The prompt tells
    the model to write with that voice and to that craft rubric, while every
    asserted fact must still trace to the grounding. Honesty guardrails stay hard.
    """
    base = (
        "You are the applicant, writing your own answer to a job-application question. "
        "It must read like the work of someone who writes exceptional cover letters: "
        "specific, grounded, and with a clear point — never a flat, generic paragraph.\n\n"
        "Follow the VOICE & CRAFT guidance in the FACTS for who you are and how you sound. "
        "Apply its craft rubric in full:\n"
        "- Open with substance (a specific example or observation), not throat-clearing or a "
        "restatement of the question.\n"
        "- Ground every point in a concrete example drawn ONLY from the FACTS. Show, don't assert.\n"
        "- Make a point: leave the reader understanding something true about how you think or work.\n"
        "- Connect to the role only where the JOB DESCRIPTION genuinely supports it; never stretch.\n"
        "- Write in complete, correctly punctuated sentences — proper commas and periods; "
        "no run-ons, comma splices, fragments, or missing terminal punctuation. Vary "
        "sentence length. Use active voice. No clichés, tool-dumps, or filler.\n"
        "- STYLE HARD RULE: at most one em-dash (—) in the entire answer; prefer periods and "
        "commas.\n\n"
        "HONESTY GUARDRAILS (hard — never violate):\n"
        "- Assert ONLY what the FACTS support. Do NOT name a specific tool, software, company, "
        "employer, metric, number, degree, or technology unless it appears VERBATIM in the FACTS, "
        "and do NOT add 'related' tools you assume he'd know.\n"
        "- Do NOT invent experiences, projects, outcomes, or numbers.\n"
        "- Do NOT make a personal commitment about location, relocation, office attendance, "
        "start date, travel, or pay. If the question asks for one, output exactly: DECLINE\n"
        "- NEVER volunteer or mention visa status, citizenship, nationality, work authorization, "
        "sponsorship needs, or green-card/marriage status. Work authorization is handled only in "
        "the structured screening questions — it must never appear in this answer, even if true.\n"
        "ANSWERING A QUESTION ABOUT EXPERIENCE YOU DON'T HAVE EXACTLY (e.g. external/customer-facing "
        "work, a specific language like TypeScript, a particular industry):\n"
        "- Do NOT blank-decline. Give an HONEST, grounded answer that leads with the closest real "
        "experience in the FACTS and is candid about the gap — e.g. 'My customer-facing work has "
        "been with internal R&D stakeholders rather than external clients, where I...', or 'I drive "
        "Python through LLM coding harnesses rather than hand-writing it, and TypeScript isn't part "
        "of my background.' Candor about a gap is stronger than silence.\n"
        "- Output DECLINE ONLY when answering would require inventing a fact, or when the question "
        "asks for a personal commitment (location/relocation/office/start date/travel/pay) — for "
        "those, output exactly: DECLINE. Otherwise find the honest, grounded answer.\n\n"
        f"FACTS:\n{facts}\n\n"
        f"{_diversity_block(used_examples)}"
        f"QUESTION: {question}\n\n"
    )
    if kind == "essay":
        return base + (
            "Write a substantial, well-structured paragraph (roughly 5–8 sentences of real "
            "content — depth, not padding; every sentence earns its place). Output only the "
            "answer text (or DECLINE).")
    return base + (
        "Answer in 1–3 crisp, complete, correctly punctuated sentences grounded only in the "
        "FACTS. Output only the answer text (or DECLINE).")


def build_refine_prompt(question, draft: str, facts: str, used_examples: str = "") -> str:
    """Second pass: elevate a clean draft to exceptional quality and fix mechanics.

    Tightens craft and punctuation without adding any claim not already grounded.
    A no-op-safe upgrade: if the draft is already excellent it returns it largely
    unchanged; it never introduces new facts.
    """
    return (
        "You are the applicant's writing editor. Below is a DRAFT answer to a job-application "
        "question, plus the FACTS it must stay within. Revise the DRAFT to the standard of an "
        "exceptional cover letter, following the VOICE & CRAFT rubric in the FACTS:\n"
        "- Deepen it: replace any vague or generic sentence with a specific example or a sharper "
        "point drawn ONLY from the FACTS. Cut filler and clichés.\n"
        "- Fix every mechanical issue: punctuation, grammar, run-ons, comma splices, fragments, "
        "and missing terminal punctuation. Ensure complete sentences and varied rhythm.\n"
        "- STYLE HARD RULE: at most one em-dash (—) in the entire answer; prefer periods and "
        "commas. Replace extra em-dashes with periods or commas.\n"
        "- Keep first person and the applicant's warm, confident, plain voice. Active voice throughout.\n"
        "- Add NO new claim, tool, number, employer, or outcome that is not already in the FACTS. "
        "Do not stretch a role connection beyond what the JOB DESCRIPTION supports.\n"
        "- NEVER volunteer or mention visa status, citizenship, nationality, work authorization, "
        "sponsorship needs, or green-card/marriage status. If the draft contains any such mention, "
        "REMOVE it — work authorization is handled only in the structured screening questions.\n"
        "- If the draft cannot be made truthful within the FACTS, output exactly: DECLINE\n\n"
        f"FACTS:\n{facts}\n\n"
        f"{_diversity_block(used_examples)}"
        f"QUESTION: {question}\n\n"
        f"DRAFT:\n{draft}\n\n"
        "Keep the draft anchored on its current (distinct) example; if it overlaps an example "
        "already used in another answer (listed above), swap to a different one from the FACTS. "
        "Output only the final revised answer text (or DECLINE). Do not explain your changes.")


def build_critique_prompt(question, draft: str, facts: str, used_examples: str = "") -> str:
    """The self-critique pass — the judgment a live session gets from a human in the loop.

    A strict reviewer reads the draft the way a sharp hiring manager (and the applicant) would and
    returns either the single token PASS or a short list of CONCRETE weaknesses to fix. This is
    what turns a competent-but-generic first draft into a strong one: it names the weakness
    ('the opening restates the question', 'this reuses the Codex story already used elsewhere',
    'it never says anything specific to THIS company') so the revise pass has something to act on.
    Bounded to one cycle by the caller — a critic, not a treadmill."""
    return (
        "You are a demanding reviewer of the applicant's job-application answer — both a sharp "
        "hiring manager and the applicant themselves, who has zero tolerance for generic filler. Read the "
        "DRAFT against the QUESTION and FACTS and judge it honestly.\n\n"
        "Fail the draft if ANY of these is true:\n"
        "- The opening is throat-clearing or restates the question instead of leading with a "
        "specific example or observation.\n"
        "- It is generic — it could have been written for any candidate or any company. For a "
        "'why this company/role' question, it must say something true and specific to THIS posting "
        "(drawn from the JOB DESCRIPTION), not a generic AI/engineering thesis.\n"
        "- It reuses a hero example/project/metric already used in another answer (see ALREADY "
        "USED below) when the FACTS offer a different one that fits.\n"
        "- It asserts anything not grounded in the FACTS, or makes a personal/visa/logistics "
        "commitment it shouldn't.\n"
        "- It is flat, padded, cliché, or makes no real point about how the applicant thinks or works.\n"
        "- DIRECTNESS: it does not answer the LITERAL question first and plainly. The reader must "
        "get the actual answer up front, not after a wind-up. A clever metaphor, framing device, or "
        "abstract thesis that BURIES the direct answer is a failure — say the real thing, then "
        "support it. (e.g. 'Why this company' must actually name why THIS company, not drift into a "
        "general philosophy; 'what do you optimize for' must state it, not bury it in an analogy.)\n"
        "- LENGTH/FOCUS: it over-writes — multiple paragraphs where a tight one would hit harder, or "
        "defensive over-explaining of a gap that one clean honest sentence would settle. Cut to the "
        "strongest version; concision is strength, not omission.\n\n"
        f"FACTS:\n{facts}\n\n"
        f"{_diversity_block(used_examples) if used_examples else ''}"
        f"QUESTION: {question}\n\n"
        f"DRAFT:\n{draft}\n\n"
        "If the draft is genuinely strong on every point above, output exactly: PASS\n"
        "Otherwise output a short bulleted list of the SPECIFIC weaknesses to fix (each concrete "
        "and actionable). Output only PASS or the bullets — nothing else.")


def build_revise_with_critique_prompt(question, draft: str, critique: str, facts: str,
                                      used_examples: str = "") -> str:
    """Revise a draft to address SPECIFIC critique points — the targeted second pass a live
    session does after a human says 'this part is weak, fix it'. Stays inside the FACTS and voice;
    adds no new claim. Honesty/commitment/visa guardrails remain hard."""
    return (
        "You are the applicant, revising your own job-application answer after a sharp reviewer "
        "flagged specific weaknesses. Rewrite the DRAFT so it fixes EVERY point in the CRITIQUE, "
        "following the VOICE & CRAFT guidance in the FACTS.\n"
        "- Address each critique point concretely — a stronger specific lead, a company-specific "
        "reason drawn from the JOB DESCRIPTION, a different grounded example, a sharper point.\n"
        "- Add NO claim, tool, number, employer, or outcome not already in the FACTS. Do not "
        "stretch a role connection beyond what the JOB DESCRIPTION supports.\n"
        "- Keep first person and the applicant's warm, confident, plain voice; active voice; at most one "
        "em-dash.\n"
        "- NEVER mention visa status, citizenship, work authorization, sponsorship, or relocation/"
        "office/start-date/pay commitments. Remove any such mention.\n"
        "- If the answer cannot be made strong AND truthful within the FACTS, output exactly: DECLINE\n\n"
        f"FACTS:\n{facts}\n\n"
        f"{_diversity_block(used_examples) if used_examples else ''}"
        f"QUESTION: {question}\n\n"
        f"DRAFT:\n{draft}\n\n"
        f"CRITIQUE (fix every point):\n{critique}\n\n"
        "Output only the final revised answer text (or DECLINE). Do not explain your changes.")


# P2: package-level critic fleet. Each lens is a focused reviewer that sees the WHOLE set of
# drafted answers at once — so it can catch what a per-question critic structurally cannot, above
# all CROSS-ANSWER duplication (the same hero story in three answers). The lenses run in PARALLEL
# (independent claude calls), their findings are unioned per answer, and only flagged answers get
# ONE revise pass — bounded, no treadmill (feedback_apply_quality_once_and_calibration).
PACKAGE_CRITIC_LENSES = [
    ("range/duplication",
     "Across the WHOLE set, flag any answer that leans on the same hero example, project, story, "
     "metric, or employer that another answer already uses, when the FACTS offer a different one "
     "that would fit. The set should show range, not one story retold. Name the weaker/redundant "
     "occurrence to change."),
    ("company-specificity",
     "Flag any answer that is generic — it could be sent to any company or for any role. For a "
     "'why this company/role' question especially, it must say something true and SPECIFIC to THIS "
     "posting (from the JOB DESCRIPTION in the FACTS), not a generic AI/engineering thesis."),
    ("voice/authenticity",
     "Flag any answer that reads as robotic, cliché, padded, or off the applicant's plain, warm, confident "
     "voice — anything that sounds AI-generated rather than like the applicant actually wrote it."),
]

_PKG_FIX_RE = re.compile(r"^\s*FIX\s+(\d+)\s*:\s*(.+)$", re.IGNORECASE)


def build_package_critique_prompt(lens_name: str, lens_instruction: str, items, facts: str) -> str:
    """One lens of the package critic: shows the reviewer the entire numbered set of drafted
    answers and asks for per-answer fixes UNDER THIS LENS ONLY. `items` is a list of
    (display_n, question, answer_text)."""
    block = "\n\n".join(f"[ANSWER {n}] QUESTION: {q}\nANSWER: {a}" for n, q, a in items)
    return (
        f"You are reviewing the applicant's drafted job-application answers through ONE lens: "
        f"{lens_name}.\n{lens_instruction}\n\n"
        "Judge ONLY through that lens — ignore issues another reviewer would catch. Be specific and "
        "honest; do not invent problems where the set is genuinely strong.\n\n"
        f"FACTS (grounding + JOB DESCRIPTION):\n{facts}\n\n"
        f"THE ANSWER SET:\n{block}\n\n"
        "For EACH answer that has a real problem under this lens, output one line exactly:\n"
        "FIX <number>: <one concrete, actionable instruction to fix it>\n"
        "If no answer has a problem under this lens, output exactly: PASS\n"
        "Output only FIX lines or PASS — nothing else.")


def _parse_package_findings(text: str) -> dict:
    """Parse a lens reply into {display_n: [fix, ...]}. Tolerates PASS and stray prose."""
    out: dict = {}
    for line in (text or "").splitlines():
        m = _PKG_FIX_RE.match(line)
        if m:
            out.setdefault(int(m.group(1)), []).append(m.group(2).strip())
    return out


def critique_and_revise_package(answers, facts, llm_fn, audit_fn,
                                lenses=PACKAGE_CRITIC_LENSES) -> list:
    """Run the parallel critic fleet over the whole drafted set, then revise only flagged essays.

    Best-effort and bounded: lenses run concurrently (independent claude calls); findings are
    unioned per answer; each flagged essay gets at most ONE revise pass that must still clear the
    fabrication gate or the original is kept. Any failure leaves the original answers untouched —
    this can only raise quality, never block or corrupt a good set."""
    import concurrent.futures

    # Only essays that actually shipped a value are worth critiquing as a set.
    essays = [(i, a) for i, a in enumerate(answers)
              if getattr(a, "kind", None) == "essay"
              and getattr(a, "status", None) in ("drafted", "answered")
              and getattr(a, "value", None)]
    if not essays:
        return answers
    # display_n (1-based) -> answers index, plus the items shown to each lens.
    n_to_idx = {n: i for n, (i, _a) in enumerate(essays, start=1)}
    items = [(n, answers[i].label, answers[i].value) for n, (i, _a) in enumerate(essays, start=1)]

    def _run_lens(lens):
        name, instruction = lens
        try:
            reply = llm_fn(build_package_critique_prompt(name, instruction, items, facts)) or ""
            return _parse_package_findings(reply)
        except Exception:  # noqa: BLE001 — a dead lens just contributes no findings
            return {}

    merged: dict = {}  # display_n -> list[fix]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(lenses))) as ex:
        for findings in ex.map(_run_lens, lenses):
            for n, fixes in findings.items():
                merged.setdefault(n, []).extend(fixes)

    for n, fixes in merged.items():
        idx = n_to_idx.get(n)
        if idx is None:
            continue
        ans = answers[idx]
        # 'Other answers' context so a duplication fix can swap to a genuinely different example.
        others = _used_summary([answers[j].value for (j, _a) in essays if j != idx])
        critique = "\n".join(f"- {f}" for f in fixes)
        try:
            revised = _strip_editor_preamble(
                (llm_fn(build_revise_with_critique_prompt(
                    ans.label, ans.value, critique, facts, others)) or "").strip())
        except Exception:  # noqa: BLE001 — keep the original on any revise failure
            continue
        if not revised or revised.upper().startswith(DECLINE):
            continue
        revised = _reduce_emdashes(revised)  # content-neutral em-dash fix before the gate
        try:
            if audit_fn(revised):  # revised text must still clear the fabrication gate
                continue
        except Exception:  # noqa: BLE001 — if the gate errors, do not risk an unvetted swap
            continue
        answers[idx] = Answer(ans.label, ans.selector, ans.kind, value=revised, status=ans.status)
    return answers


def _used_summary(used: list) -> str:
    """Compact 'already used' context fed to later answers: the lead of each prior answer
    (enough to convey WHICH example it leaned on) as a bulleted list. Bounded so the prompt
    stays lean even with many questions."""
    lines = []
    for txt in used:
        snippet = " ".join((txt or "").split())[:240]
        if snippet:
            lines.append(f"- {snippet}")
    return "\n".join(lines)


def generate(questions, facts: str, llm_fn, audit_fn, *,
             refine: bool = False, package: bool = True, package_min_essays: int = 3) -> list:
    """Draft answers for a set of questions.

    Cost knobs. Defaults are the CONSERVATIVE pipeline, chosen from a measured A/B (2026-06-17,
    JOB-237): with the hardened directness critic, the standalone refine pass and the package critic
    on small jobs ADD ~70% more tokens AND scored LOWER (over-processing → over-elaboration). So:
      refine=False  — skip the standalone craft pass; draft -> critic -> revise already polishes.
                      (Each extra pass is a full ~50k-token claude call, mostly fixed per-call
                      overhead — fewer calls is the dominant cost lever.)
      package=True  — keep the parallel cross-answer critic fleet, BUT only where it earns its cost:
      package_min_essays=3 — skip it unless >=3 essays (its only real job is cross-answer dedup; a
                      1-2 essay set has little to dedup, so it's pure cost there).
    Pass refine=True / package_min_essays=1 to force the full, most-thorough pipeline.
    """
    answers = []
    used_answers: list = []  # texts of prior drafted/answered responses in THIS application
    for q in questions:
        # Logistics / personal-commitment / pay questions are the user's to answer — the
        # engine cannot ground his real constraints, so it declines rather than risk a
        # false commitment (e.g. "yes, I can be in the SF office 3x/week"). No LLM call.
        if is_personal_commitment(q.label):
            answers.append(Answer(q.label, q.selector, q.kind, status="declined",
                                  reason="personal commitment / logistics — left for the user"))
            continue
        if is_office_auto_yes(q.label):
            answers.append(Answer(q.label, q.selector, q.kind, status="answered", value="Yes",
                                  reason="in-office/RTO/relocation auto-Yes (policy)"))
            used_answers.append("Yes")
            continue
        used_examples = _used_summary(used_answers)
        try:
            raw = _strip_editor_preamble((llm_fn(build_prompt(q.label, q.kind, facts, used_examples)) or "").strip())
        except Exception as e:  # noqa: BLE001
            answers.append(Answer(q.label, q.selector, q.kind,
                                  status="declined", reason=f"llm error: {e!r}"))
            continue
        if not raw or raw.upper().startswith(DECLINE):
            answers.append(Answer(q.label, q.selector, q.kind,
                                  status="declined", reason="not supported by facts"))
            continue
        # Essays get a second craft+mechanics pass (draft → editor → audit), the same
        # way cover letters get a revision pass. Refine failure falls back to the draft,
        # so this only ever raises quality — never blocks a good draft.
        if q.kind == "essay":
            if refine:
                try:
                    polished = _strip_editor_preamble((llm_fn(build_refine_prompt(q.label, raw, facts, used_examples)) or "").strip())
                    if polished and not polished.upper().startswith(DECLINE):
                        raw = polished
                except Exception:  # noqa: BLE001 — keep the draft if the editor pass fails
                    pass
            # Self-critique loop — the human-in-the-loop judgment a live session gets. A strict
            # critic reads the polished draft and returns PASS or concrete weaknesses; if it finds
            # any, ONE targeted revise pass fixes them. Bounded to a single cycle (a critic, not a
            # per-edit treadmill). Any failure here falls back to the polished draft, so this only
            # ever raises quality — it can never block or degrade a good answer.
            try:
                verdict = (llm_fn(build_critique_prompt(q.label, raw, facts, used_examples)) or "").strip()
                if verdict and verdict.upper() != "PASS" and not verdict.upper().startswith(DECLINE):
                    revised = _strip_editor_preamble(
                        (llm_fn(build_revise_with_critique_prompt(
                            q.label, raw, verdict, facts, used_examples)) or "").strip())
                    if revised and not revised.upper().startswith(DECLINE):
                        raw = revised
            except Exception:  # noqa: BLE001 — keep the polished draft if critique/revise fails
                pass
        if q.kind == "essay":
            raw = _reduce_emdashes(raw)  # content-neutral: don't let the gate BLOCK a good answer over em-dashes
        try:
            blocks = audit_fn(raw) or []
        except Exception as e:  # noqa: BLE001 — if the gate errors, fail safe (block)
            blocks = [f"audit error: {e!r}"]
        if blocks:
            answers.append(Answer(q.label, q.selector, q.kind, value=raw,
                                  status="blocked", reason="; ".join(blocks)[:200]))
            continue
        answers.append(Answer(q.label, q.selector, q.kind, value=raw,
                              status="drafted" if q.kind == "essay" else "answered"))
        # Feed this accepted answer forward so later answers in the SAME application draw on
        # different examples (only essays carry hero examples worth de-duping; short factual
        # answers don't drive the repetition the user flagged).
        if q.kind == "essay":
            used_answers.append(raw)
    # P2: once the whole set is drafted, run the parallel critic fleet over it — catches
    # cross-answer duplication and lens-specific weaknesses a per-question pass can't see, and
    # revises only the flagged essays (bounded, gate-checked). Best-effort: never blocks the set.
    # Skipped when disabled or when there are too few essays for cross-answer dedup to matter.
    n_essays = sum(1 for a in answers if a.kind == "essay" and a.status in ("drafted", "answered") and a.value)
    if package and n_essays >= package_min_essays:
        answers = critique_and_revise_package(answers, facts, llm_fn, audit_fn)
    return answers


_PKG_FENCE_RE = re.compile(r"^```(?:json)?|```$", re.M)


def _parse_single_call(raw: str) -> dict:
    """Parse the single-call JSON answer set into {1-based index: text}. Tolerant of the two shapes
    the model uses in practice — {"answers":[...]} or a bare [...] array — and of key variants
    (n/index/id for the number, text/answer for the body). Returns {} if nothing parses."""
    raw = _PKG_FENCE_RE.sub("", (raw or "").strip()).strip()
    by_n: dict = {}
    try:
        m_arr = re.search(r"\[\s*\{.*\}\s*\]", raw, re.S)
        if m_arr:
            items = json.loads(m_arr.group(0))
        else:
            items = json.loads(re.search(r"\{.*\}", raw, re.S).group(0)).get("answers", [])
        for idx, a in enumerate(items, start=1):
            if not isinstance(a, dict):
                continue
            n = a.get("n") or a.get("index") or a.get("id") or idx
            by_n[int(n)] = a.get("text") or a.get("answer") or ""
    except Exception:  # noqa: BLE001 — any malformed reply yields no answers (caller repairs/escalates)
        return {}
    return by_n


def draft_single_call(questions, facts: str, agent_fn, audit_fn, *, repair: bool = True) -> list:
    """The single-call drafter (2026-06-17). ONE agentic call drafts + self-critiques + finalizes
    ALL answers; the deterministic gates run after, in Python. ~6x cheaper/faster than the multi-pass
    generate() at equal quality (measured JOB-237/233).

    agent_fn(prompt)->str is the single-call agent (llm.make_single_call_agent in production;
    injectable for tests). Robustness — a single call is a single point of failure, so:
      * tolerant parse (two JSON shapes / key variants),
      * ONE repair retry with a firmer 'return only the JSON' instruction on a parse miss,
      * escalate the whole set to the user (status='declined') if even the retry won't parse —
        never fall back to the costlier multi-pass pipeline.
    Personal-commitment / logistics questions are declined WITHOUT a model call, same as generate().
    """
    askable = [q for q in questions
               if not is_personal_commitment(q.label) and not is_office_auto_yes(q.label)]
    commitments = [Answer(q.label, q.selector, q.kind, status="declined",
                          reason="personal commitment / logistics — left for the user")
                   for q in questions if is_personal_commitment(q.label)]
    out_by_q = {q.label: a for q, a in zip(
        [q for q in questions if is_personal_commitment(q.label)], commitments)}
    # in-office/RTO/relocation rendered as free-text -> answer 'Yes', never send to the model
    for q in questions:
        if is_office_auto_yes(q.label):
            out_by_q[q.label] = Answer(q.label, q.selector, q.kind, status="answered",
                                       value="Yes", reason="in-office/RTO/relocation auto-Yes (policy)")

    if askable:
        body = "\n\n".join(f"[{i+1}] {q.label}" for i, q in enumerate(askable))
        prompt = f"FACTS:\n{facts}\n\nQUESTIONS:\n{body}\n\nReturn the JSON now."
        try:
            by_n = _parse_single_call(agent_fn(prompt) or "")
        except Exception:  # noqa: BLE001 — agent error → treat as empty, repair/escalate below
            by_n = {}
        if not by_n and repair:
            repair_prompt = (prompt + "\n\nYour previous reply was NOT valid JSON in the required "
                             'shape. Return ONLY {"answers":[{"n":1,"text":"..."}, ...]} — one entry '
                             "per question, in order, no prose, no code fences.")
            try:
                by_n = _parse_single_call(agent_fn(repair_prompt) or "")
            except Exception:  # noqa: BLE001
                by_n = {}
        for i, q in enumerate(askable, start=1):
            if not by_n:
                # Total parse failure even after repair → escalate the whole set to the user.
                out_by_q[q.label] = Answer(q.label, q.selector, q.kind, status="declined",
                                           reason="single-call could not parse answers — needs the user")
                continue
            txt = _strip_editor_preamble((by_n.get(i) or "").strip())
            if not txt or txt.upper().startswith(DECLINE):
                out_by_q[q.label] = Answer(q.label, q.selector, q.kind, status="declined",
                                           reason="not supported by facts / declined")
                continue
            txt = _reduce_emdashes(txt)
            try:
                blocks = audit_fn(txt) or []
            except Exception as e:  # noqa: BLE001 — fail safe (block) if the gate errors
                blocks = [f"audit error: {e!r}"]
            out_by_q[q.label] = Answer(
                q.label, q.selector, q.kind, value=txt,
                status="blocked" if blocks else ("drafted" if q.kind == "essay" else "answered"),
                reason="; ".join(blocks)[:200] if blocks else "")
    # Preserve the caller's original question order.
    return [out_by_q[q.label] for q in questions]
