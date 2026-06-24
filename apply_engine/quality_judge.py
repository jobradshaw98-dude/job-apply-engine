# -*- coding: utf-8 -*-
"""HOLISTIC QUALITY JUDGE — the SECOND mandatory gate on a staged application, run alongside
the fabrication auditor (refresh_audit.audit_answers).

WHY THIS EXISTS (Stage-3 of the apply-pipeline quality contract)
The fabrication auditor answers ONE question: "is every claim in the answers supported by the
ledger?" It is a HONESTY gate — it cannot tell a truthful-but-generic master-resume dump from a
truthful, sharply tailored package. the quality contract says every job the applicant selects gets a
full tailored+audited package: no master fallback, no junk. So a package can pass fabrication
(nothing false) and still be junk (covers none of the JD's asks, makes a generic case, has no
specifics, reads like boilerplate AI). This judge is the QUALITY lens that catches that class.

WHAT IT JUDGES (the whole package, not just answers)
It scores the TAILORED package as a unit — the resume bullets/summary + the cover paragraphs +
the custom answers — against the JD, on four dimensions (1-5 each):
  * jd_coverage  — does the package address the JD's KEY requirements?
  * fit          — does it make a SPECIFIC case for THIS role/company (not a generic case any
                   employer would receive)?
  * specificity  — concrete projects / numbers / evidence vs vague filler?
  * voice        — the applicant's authentic voice, not generic-AI and not master-resume boilerplate?

VERDICT (mirrors the fabrication two-severity idea, but THREE-valued: PASS / FLAG / FAIL)
  * FAIL = a HARD FLOOR breach: jd_coverage <= 2 OR specificity <= 2. A package that doesn't
           cover the JD, or has no specifics, is not submittable. FAIL blocks submit.
  * FLAG = any dimension <= 3 (and no FAIL): the package is submittable but weak on something;
           surfaced to the user, NOT a hard block (he can submit or improve via the edit loop).
  * PASS = every dimension >= 4.
This is the NON-WEDGING reading of the contract's "must PASS both gates": fabrication PASS +
quality NOT-FAIL. FLAG is advisory so an over-critical LLM can't permanently lock Submit the way
an all-BLOCK fabrication policy once did (see refresh_audit's 2026-06-05 note).

judge_ran (mirrors audit_answers exactly)
True iff the LLM lens ran cleanly. An LLM-call failure or an unparseable response (after one
retry) RAISES out of judge_quality; the ORCHESTRATION layer (refresh_audit.refresh) catches it
and stamps judge_ran False WITH verdict FAIL (fail closed on both fields, 2026-06-11), so a
degraded judge can never present as a completed quality review even to a consumer that reads
the verdict alone. finish.can_submit refuses a judge_ran=False quality_audit the same
as a missing one. claude -p ONLY — never the metered API (the user's hard rule).
"""
import json
from datetime import datetime
from typing import Callable, List, Optional

# ---- verdict-mapping constants (tunable; see module docstring for rationale) ----
# A score AT OR BELOW this on a HARD-FLOOR dimension is a FAIL (un-submittable).
_FAIL_FLOOR = 2
# The dimensions whose floor breach is a hard FAIL: a package must cover the JD and carry
# specifics. fit/voice weakness is FLAG-worthy but not a hard block on its own.
_HARD_FLOOR_DIMS = ("jd_coverage", "specificity")
# A score AT OR BELOW this on ANY dimension (when no FAIL) is a FLAG (advisory, non-blocking).
_FLAG_CEILING = 3
# The four dimensions, in display order.
_DIMENSIONS = ("jd_coverage", "fit", "specificity", "voice")

# The recognized calibration violation TYPES (the six grounded positioning rules). An unknown
# type from the model is still KEPT as a violation (a flagged miss we don't recognize is more
# safely surfaced than silently dropped) — this set is only for documentation + the prompt.
_CALIBRATION_TYPES = (
    "wrong_domain_pitch",      # rule 1: pitching life-sciences DOMAIN fit on a non-life-sciences JD
    "leads_with_cad",          # rule 2: resume/cover leads with hands-on CAD, not simulation
    "coding_fluency",          # rule 3: claims Python/MATLAB/programming-language fluency
    "wrong_tool_attribution",  # rule 4: a tool attributed to the wrong employer (ANSYS at Meridian)
    "excluded_project",        # rule 5: MobilityCo or Signal Intel in a career artifact
    "seniority_mismatch",      # rule 6: pitched as senior-leadership, or under-claiming below ~5yr
)

# Verdicts finish.can_submit treats as NOT-blocking (the "not-FAIL" set).
PASS, FLAG, FAIL = "PASS", "FLAG", "FAIL"


def _local_iso() -> str:
    """Local ISO timestamp WITH offset — matches refresh_audit/regen_answer history rows."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------------------
# package -> prompt text (PURE)
# --------------------------------------------------------------------------------------

def _str(x) -> str:
    return "" if x is None else str(x)


def _jd_text(job: dict) -> str:
    """The JD to judge coverage against. Prefer the full jd_text; fall back to title+notes so the
    judge always has SOMETHING to score against rather than judging in a vacuum."""
    job = job or {}
    jd = _str(job.get("jd_text")).strip()
    if jd:
        return jd[:12000]
    bits = [_str(job.get("title")), _str(job.get("company")), _str(job.get("notes"))]
    return "\n".join(b for b in bits if b).strip()


def _resume_text(resume: dict) -> str:
    """Flatten the tailored resume dict (headline/summary/*_bullets/skills) into readable text.
    Tolerant of missing keys and of bullets stored as list[str] or list[dict]."""
    resume = resume or {}
    out: List[str] = []
    if resume.get("headline"):
        out.append(f"HEADLINE: {_str(resume['headline'])}")
    if resume.get("summary"):
        out.append(f"SUMMARY: {_str(resume['summary'])}")
    for key in ("current_bullets", "masc_bullets", "stateuni_bullets", "mobilityco_bullets"):
        bullets = resume.get(key) or []
        if not isinstance(bullets, list):
            continue
        lines = []
        for b in bullets:
            if isinstance(b, dict):
                b = b.get("text") or b.get("bullet") or ""
            b = _str(b).strip()
            if b:
                lines.append(f"  - {b}")
        if lines:
            out.append(f"{key.upper()}:\n" + "\n".join(lines))
    skills = resume.get("skills")
    if isinstance(skills, list) and skills:
        out.append("SKILLS: " + ", ".join(_str(s) for s in skills))
    elif skills:
        out.append("SKILLS: " + _str(skills))
    return "\n".join(out).strip() or "(no tailored resume content found)"


def _cover_text(cover: dict) -> str:
    """Flatten the tailored cover dict (salutation + paragraphs) into readable text."""
    cover = cover or {}
    out: List[str] = []
    sal = cover.get("salutation") or cover.get("addressee")
    if sal:
        out.append(_str(sal))
    paras = cover.get("paragraphs") or []
    if isinstance(paras, list):
        for p in paras:
            p = _str(p).strip()
            if p:
                out.append(p)
    elif paras:
        out.append(_str(paras))
    return "\n\n".join(out).strip() or "(no tailored cover letter found)"


def _answers_text(answers: list) -> str:
    """Flatten the custom answers list (each {q/question, value/answer}) into Q/A text. Only
    answered, non-empty entries are shown — an empty/declined answer is not 'weak quality',
    it simply isn't part of the package to judge."""
    out: List[str] = []
    for a in (answers or []):
        if not isinstance(a, dict):
            continue
        q = _str(a.get("q") or a.get("question")).strip()
        v = a.get("value")
        if v is None:
            v = a.get("answer")
        if isinstance(v, (list, tuple)):
            v = ", ".join(_str(x) for x in v)
        v = _str(v).strip()
        if not v:
            continue
        out.append(f"Q: {q}\nA: {v}")
    return "\n\n".join(out).strip() or "(no custom answers — standard-fields-only application)"


def _candidate_stack() -> str:
    """the applicant's real, assertable AI/engineering stack — the ONLY tools a quality `fix` may tell them
    to name. Pulled from capabilities.md so it stays in sync with the screening grounding; falls
    back to a concise inline summary if that file can't be read. Prevents the judge from suggesting
    he claim a JD tool he hasn't used (the LangGraph mis-fix, 2026-06-10)."""
    try:
        from .screening import load_capabilities
        caps = (load_capabilities() or "").strip()
        if caps:
            return caps[:4000]
    except Exception:
        pass
    return ("LLM coding harnesses: Claude Code, Codex. Multi-agent orchestration + agent-to-agent "
            "handoff (ARIA). MCP tool integration, tool-calling, structured outputs, RAG. Python "
            "(AI-orchestrated), Flask, Playwright, FEA/optimization. NOT used (never claim): "
            "LangChain, LangGraph, Langfuse, Pydantic-AI, Kubernetes-at-scale.")


def _positioning_rules() -> str:
    """the applicant's SIX grounded positioning rules — the discrete, GROUNDED targeting checks the
    calibration gate runs. These are NOT style opinions: each is a true-content-but-wrong-audience
    miss (or a hard never-claim) drawn from his career-memory rules. The prompt asks the model to
    judge the package against EACH and report a violation TYPE only when a rule is actually broken,
    JD-aware where rule 1 requires it. PURE (a static grounding block)."""
    return (
        "1. WRONG-DOMAIN PITCH (type: wrong_domain_pitch). FIRST decide: is THIS JOB itself a "
        "life-sciences / medical-device / healthcare / mechanical role? Read the JD's domain. If "
        "it IS, there is NO violation here. If it is NOT, then pitching healthcare / life-sciences "
        "/ mechanical as a DOMAIN-FIT asset is a violation — e.g. framing \"fluency in healthcare "
        "and life-sciences applications\" or claiming domain alignment to a field the JD is not in. "
        "His mechanical background as GENERAL engineering depth is fine; it is a violation ONLY "
        "when sold as domain fit on a non-life-sciences role. (This is the exact JOB-210 miss.)\n"
        "2. LEADS WITH CAD (type: leads_with_cad). The resume/cover must lead with simulation, "
        "optimization, or test-to-sim correlation, NOT hands-on CAD modeling. Leading with "
        "SolidWorks / Creo / NX / CAD-modeling as the headline strength is a violation.\n"
        "3. CODING-FLUENCY CLAIM (type: coding_fluency). ANY claim of Python / MATLAB / "
        "programming-language fluency, proficiency, or hand-coding skill is a violation. The applicant "
        "ORCHESTRATES AI coding agents (Claude Code, Codex); he does not hand-code. \"Proficient in "
        "Python\", \"expert MATLAB\", \"strong programmer\" are all violations. Framing as an "
        "AI-native engineer who ships software via AI agents is correct and NOT a violation.\n"
        "4. WRONG TOOL ATTRIBUTION (type: wrong_tool_attribution). Meridian work used LS-DYNA + "
        "HyperWorks ONLY (never ANSYS at Meridian). ANSYS belongs to State University / graduate work. "
        "Attributing a tool to the wrong employer is a violation.\n"
        "5. EXCLUDED PROJECTS (type: excluded_project). MobilityCo and Signal Intel are EXCLUDED from "
        "all career artifacts. Either appearing in the resume, cover, or answers is a violation.\n"
        "6. SENIORITY MISMATCH (type: seniority_mismatch). Pitching as senior-leadership / "
        "principal / director / head-of, OR under-claiming below ~5 years of effective experience "
        "(his MASc counts toward that), is a violation."
    )


def _build_prompt(job: dict, resume: dict, cover: dict, answers: list) -> str:
    """Build the single claude -p prompt. PURE except for reading the capabilities grounding."""
    return (
        "You are a hiring-quality reviewer for the user Rivera's job applications. Your job is "
        "NOT to fact-check (a separate honesty auditor does that) — it is to judge whether this "
        "TAILORED application package is GOOD ENOUGH to send for THIS specific role, or whether "
        "it reads like a generic, untailored, or low-substance package.\n\n"
        "Score these FOUR dimensions, each on an integer 1-5 scale (5 = excellent, 1 = poor):\n"
        "  * jd_coverage — does the package actually ADDRESS the key requirements and themes in "
        "the JOB DESCRIPTION? (5 = hits the JD's main asks; 1 = ignores them.)\n"
        "  * fit — does it make a SPECIFIC case for THIS role at THIS company, vs. a generic case "
        "any employer could receive? (5 = clearly THIS job; 1 = boilerplate.)\n"
        "  * specificity — concrete projects, numbers, named tools, and real evidence vs. vague "
        "filler and adjectives? (5 = concrete and evidenced; 1 = hand-wavy.)\n"
        "  * voice — the applicant's authentic, direct, human voice vs. generic-AI phrasing or recycled "
        "master-resume boilerplate? (5 = sounds like a real, specific person; 1 = AI/boilerplate.)\n\n"
        "Be a tough but fair reviewer. Reserve 5s for genuinely strong work; do not inflate.\n\n"
        "For EACH dimension, also give a `fix`: a concrete, actionable instruction that would RAISE "
        "that dimension's score. HARD RULE on fixes: ground every suggestion in the CANDIDATE STACK "
        "below. NEVER tell the applicant to name, add, or claim a tool/framework/skill they have not used. If "
        "the JD names a specific tool he lacks (e.g. LangGraph, Langfuse, Pydantic-AI, Kubernetes), "
        "do NOT suggest adding it: instead suggest naming his REAL equivalent from the stack and "
        "mapping it to the JD's intent. Say WHAT to change and WHERE (resume / cover letter / a "
        "custom answer). Good fix: \"In cover para 2, name his real agent stack (Claude Code, Codex, "
        "MCP orchestration, multi-agent ARIA) as the equivalent of the JD's agent-framework ask, "
        "rather than leaving coverage implicit.\" Bad fix (NEVER do this): \"name LangGraph.\" For a "
        "4 or 5 that needs nothing, use an empty string \"\".\n\n"
        "SEPARATELY, run a CALIBRATION check. This is NOT a score — it is a list of DISCRETE, "
        "GROUNDED targeting violations. It catches MIS-TARGETING: content that may be TRUE but is "
        "aimed at the wrong audience (the honesty auditor cannot see this, and a confidently "
        "wrong-domain pitch can still score well on the four dimensions). Check the package "
        "(resume + cover + answers) against EACH of the SIX positioning rules below. Report a "
        "violation ONLY when a rule is actually broken — do NOT invent violations on style, tone, "
        "or subjective grounds, and respect rule 1's JD-awareness. Return an EMPTY list when the "
        "package is correctly targeted.\n\n"
        "=== POSITIONING RULES (the calibration gate — break one = a violation) ===\n"
        + _positioning_rules() + "\n\n"
        "For EACH violation, the `fix` is grounded the SAME way as the dimension fixes: it may ONLY "
        "reference the applicant's real stack / the rules above, and may NEVER invent a tool or claim.\n\n"
        "=== CANDIDATE STACK (what the applicant has ACTUALLY used — fixes may only reference THIS) ===\n"
        + _candidate_stack() + "\n\n"
        "=== JOB DESCRIPTION ===\n" + _jd_text(job) + "\n\n"
        "=== TAILORED RESUME ===\n" + _resume_text(resume) + "\n\n"
        "=== COVER LETTER ===\n" + _cover_text(cover) + "\n\n"
        "=== CUSTOM ANSWERS ===\n" + _answers_text(answers) + "\n\n"
        "Return ONLY strict JSON, no prose and no code fence, in EXACTLY this shape:\n"
        '{"jd_coverage": {"score": 1-5, "note": "one line", "fix": "one line or \\"\\""}, '
        '"fit": {"score": 1-5, "note": "one line", "fix": "one line or \\"\\""}, '
        '"specificity": {"score": 1-5, "note": "one line", "fix": "one line or \\"\\""}, '
        '"voice": {"score": 1-5, "note": "one line", "fix": "one line or \\"\\""}, '
        '"calibration": [{"type": "one of wrong_domain_pitch|leads_with_cad|coding_fluency|'
        'wrong_tool_attribution|excluded_project|seniority_mismatch", "where": "resume|cover|answer", '
        '"evidence": "the offending phrase", "fix": "grounded correction"}], '
        '"summary": "one-line overall verdict"}'
    )


# --------------------------------------------------------------------------------------
# strict-JSON parse (PURE)
# --------------------------------------------------------------------------------------

def _coerce_score(raw) -> int:
    """Clamp a parsed score to the 1-5 integer range. A garbled/missing score is treated as the
    WORST (1) so a malformed dimension fails closed (a quality gate must never silently pass on
    junk it couldn't read)."""
    try:
        v = int(round(float(raw)))
    except (TypeError, ValueError):
        return 1
    return max(1, min(5, v))


def _parse_calibration(raw_list) -> List[dict]:
    """Parse the judge's `calibration` array into a clean list of violation dicts. Each kept
    violation carries {type, where, evidence, fix}. Garbled entries fail SAFE for the GATE but
    SILENT in the list: a non-dict entry, or a dict with no `type`, is DROPPED (not counted as a
    violation) — the model returns junk there only when it has nothing real to flag, so dropping
    it avoids a false FAIL. A dict WITH a type is always kept (even an unrecognized type — a
    flagged miss we don't recognize is surfaced, not swallowed). A missing/non-list field -> []."""
    if not isinstance(raw_list, list):
        return []
    out: List[dict] = []
    for v in raw_list:
        if not isinstance(v, dict):
            continue
        vtype = _str(v.get("type")).strip()
        if not vtype:
            continue
        out.append({
            "type": vtype,
            "where": _str(v.get("where")).strip(),
            "evidence": _str(v.get("evidence")).strip(),
            "fix": _str(v.get("fix")).strip(),
        })
    return out


def _parse_dimensions(raw: str) -> dict:
    """Parse the judge's strict-JSON object into {dimensions, calibration, summary}. Raises
    ValueError if no JSON object is present or it doesn't carry all four dimensions — the caller
    treats that as a parse failure (one retry, then raise). The `calibration` array is OPTIONAL
    (a missing/garbled field parses to an empty list — backward-compatible) so its absence is
    never itself a parse failure, only a 'no calibration violations' signal."""
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e == -1 or e < s:
        raise ValueError("no JSON object in judge response")
    obj = json.loads(raw[s:e + 1])  # JSONDecodeError -> caller retries/raises
    if not isinstance(obj, dict):
        raise ValueError("judge response was not a JSON object")

    dims = {}
    for name in _DIMENSIONS:
        d = obj.get(name)
        if not isinstance(d, dict) or "score" not in d:
            raise ValueError(f"judge response missing dimension {name!r}")
        dims[name] = {"score": _coerce_score(d.get("score")),
                      "note": _str(d.get("note")).strip(),
                      "fix": _str(d.get("fix")).strip()}
    calibration = _parse_calibration(obj.get("calibration"))
    summary = _str(obj.get("summary")).strip()
    return {"dimensions": dims, "calibration": calibration, "summary": summary}


# --------------------------------------------------------------------------------------
# verdict mapping (PURE)
# --------------------------------------------------------------------------------------

def _verdict_for(dimensions: dict, calibration: Optional[List[dict]] = None) -> str:
    """Map the four scores (+ the calibration violations) to PASS/FLAG/FAIL.
      * FAIL if ANY calibration violation exists (a grounded mis-targeting breach — independent of
        the four scores; a confidently wrong-domain pitch can score well yet is un-submittable),
      * else FAIL if any HARD-FLOOR dim (jd_coverage, specificity) <= _FAIL_FLOOR (2),
      * else FLAG if ANY dim <= _FLAG_CEILING (3),
      * else PASS.
    The calibration check is FIRST and dominant: it is a discrete grounded gate, not a score, so
    it overrides even all-5 dimensions (the JOB-210 class — true content, wrong audience).
    """
    if calibration:
        return FAIL
    for name in _HARD_FLOOR_DIMS:
        if dimensions.get(name, {}).get("score", 1) <= _FAIL_FLOOR:
            return FAIL
    for name in _DIMENSIONS:
        if dimensions.get(name, {}).get("score", 1) <= _FLAG_CEILING:
            return FLAG
    return PASS


# --------------------------------------------------------------------------------------
# the judge (one claude -p call; strict JSON; one retry then raise)
# --------------------------------------------------------------------------------------

def judge_quality(job: dict, resume: dict, cover: dict, answers: list,
                  *, llm: Optional[Callable[[str], str]] = None) -> dict:
    """Score a tailored application package on the four quality dimensions via ONE claude -p call.

    Returns:
        {"verdict": "PASS"|"FLAG"|"FAIL",
         "dimensions": {jd_coverage|fit|specificity|voice: {"score": 1-5, "note": "..."}},
         "calibration": [{"type", "where", "evidence", "fix"}, ...],  # mis-targeting violations
         "judge_ran": True,
         "summary": "...",
         "refreshed_at": "<local ISO>"}

    A non-empty `calibration` list forces verdict FAIL (un-submittable) independent of the four
    scores — it is the grounded gate that catches true-content-but-wrong-audience mis-targeting.

    judge_ran is always True on a returned dict — a clean run is the only way this function
    returns. An LLM-call failure, or a response that won't parse after ONE retry, RAISES; the
    orchestration caller (refresh_audit.refresh) catches it and stamps a judge_ran=False,
    non-PASS quality_audit instead. claude -p only; NEVER the metered API.

    `llm` is injectable for tests; when omitted it is constructed for real via
    make_claude_llm('sonnet'). If the Claude CLI is unavailable that construction raises
    LLMUnavailable, which propagates (caught by refresh).
    """
    if llm is None:
        from .llm import make_claude_llm
        llm = make_claude_llm("sonnet")  # raises LLMUnavailable if the CLI is missing

    prompt = _build_prompt(job, resume, cover, answers)

    last_err: Optional[Exception] = None
    parsed = None
    for _ in range(2):  # one initial attempt + one retry on a parse failure
        raw = (llm(prompt) or "").strip()  # an LLM-call failure raises out of here -> propagates
        try:
            parsed = _parse_dimensions(raw)
            break
        except (ValueError, json.JSONDecodeError) as ex:
            last_err = ex
            parsed = None
    if parsed is None:
        raise ValueError(f"quality judge returned unparseable JSON after retry: {last_err}")

    dimensions = parsed["dimensions"]
    calibration = parsed["calibration"]
    verdict = _verdict_for(dimensions, calibration)
    summary = parsed["summary"] or f"quality review: {verdict}"
    if calibration:
        # Name the mis-targeting class on the summary so the dashboard badge hover + the
        # can_submit refusal reason both say WHY it failed (mirrors the fabrication BLOCK naming).
        types = ", ".join(sorted({_str(v.get("type")).strip() for v in calibration if v.get("type")}))
        summary = (f"calibration FAIL ({types}): the package is mis-targeted "
                   "(true content aimed at the wrong audience). Fix the flagged item(s) before "
                   "submitting. " + summary).strip()
    return {
        "verdict": verdict,
        "dimensions": dimensions,
        "calibration": calibration,
        "judge_ran": True,
        "summary": summary,
        "refreshed_at": _local_iso(),
    }


# --------------------------------------------------------------------------------------
# degraded-judge stamp (used by the orchestration layer when judge_quality RAISES)
# --------------------------------------------------------------------------------------

def degraded_quality_audit(reason: str = "") -> dict:
    """The safe quality_audit to stamp when the LLM judge could not run (CLI missing, call died,
    or unparseable after retry). FAILS CLOSED ON BOTH FIELDS (2026-06-11): judge_ran is False
    (finish.can_submit refuses it the same as a missing audit) AND the verdict is FAIL, so a
    consumer that reads the verdict without checking judge_ran also treats a degraded judge as
    un-submittable. The FAIL is a refusal stamp, not a real judgment; the summary says plainly
    that the review did not run. Mirrors refresh_audit's degraded fail-closed stamp."""
    note = "the quality reviewer (LLM) was unavailable" + (f": {reason}" if reason else "")
    return {
        "verdict": FAIL,
        "dimensions": {name: {"score": 0, "note": "not scored — judge unavailable", "fix": ""}
                       for name in _DIMENSIONS},
        "calibration": [],
        "judge_ran": False,
        "summary": ("quality review did NOT run — " + note
                    + ". Re-run it (with the LLM judge available) before submitting."),
        "refreshed_at": _local_iso(),
    }


# --------------------------------------------------------------------------------------
# CALIBRATION-ONLY recheck (the EDIT-time gate — runs WITHOUT re-judging the 4 polish dims)
# --------------------------------------------------------------------------------------
#
# WHY THIS IS SEPARATE FROM judge_quality. The four polish dimensions (jd_coverage / fit /
# specificity / voice) are a GRADIENT: an LLM critic asked "how could this be better" always
# finds something, so re-judging them on every edit spawned an advisory treadmill that never
# converged. That is why the quality judge runs ONCE at staging and then goes quiet
# (feedback_apply_quality_once_and_calibration). The CALIBRATION violations are different: they
# are six DISCRETE, GROUNDED pass/fail rules (true content aimed at the wrong audience), exactly
# the class a resume/cover edit can INTRODUCE. So a content edit re-runs ONLY this calibration
# sub-check, recomputing the `calibration` array + its FAIL contribution while the four polish
# dimension scores stay BYTE-FROZEN from the staging pass. No treadmill, but a mis-targeting edit
# still cannot ride along unflagged.


def _build_calibration_prompt(job: dict, resume: dict, cover: dict, answers: list) -> str:
    """Calibration-ONLY prompt: the SAME six grounded positioning rules + the same candidate-stack
    grounding judge_quality uses, but it asks for ONLY the calibration array (no dimension scores).
    PURE except for reading the capabilities grounding (via _positioning_rules/_candidate_stack)."""
    return (
        "You are a targeting reviewer for the user Rivera's job applications. Your ONLY job here "
        "is the CALIBRATION check: a list of DISCRETE, GROUNDED targeting violations. You are NOT "
        "scoring quality and NOT fact-checking (other gates do that). You catch MIS-TARGETING: "
        "content that may be TRUE but is aimed at the wrong audience. Check the package (resume + "
        "cover + answers) against EACH of the SIX positioning rules below. Report a violation ONLY "
        "when a rule is actually broken; do NOT invent violations on style, tone, or subjective "
        "grounds, and respect rule 1's JD-awareness. Return an EMPTY list when correctly targeted.\n\n"
        "=== POSITIONING RULES (break one = a violation) ===\n"
        + _positioning_rules() + "\n\n"
        "For EACH violation, the `fix` may ONLY reference the applicant's real stack / the rules above, "
        "and may NEVER invent a tool or claim.\n\n"
        "=== CANDIDATE STACK (what the applicant has ACTUALLY used — fixes may only reference THIS) ===\n"
        + _candidate_stack() + "\n\n"
        "=== JOB DESCRIPTION ===\n" + _jd_text(job) + "\n\n"
        "=== TAILORED RESUME ===\n" + _resume_text(resume) + "\n\n"
        "=== COVER LETTER ===\n" + _cover_text(cover) + "\n\n"
        "=== CUSTOM ANSWERS ===\n" + _answers_text(answers) + "\n\n"
        "Return ONLY strict JSON, no prose and no code fence, in EXACTLY this shape:\n"
        '{"calibration": [{"type": "one of wrong_domain_pitch|leads_with_cad|coding_fluency|'
        'wrong_tool_attribution|excluded_project|seniority_mismatch", "where": "resume|cover|answer", '
        '"evidence": "the offending phrase", "fix": "grounded correction"}]}'
    )


def _parse_calibration_only(raw: str) -> List[dict]:
    """Parse the calibration-only response into a clean violation list. Slices the outer {...}
    like _parse_dimensions, then reuses _parse_calibration's per-entry hygiene. Raises ValueError
    if no JSON object is present (the caller retries once, then degrades — never wedges)."""
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e == -1 or e < s:
        raise ValueError("no JSON object in calibration response")
    obj = json.loads(raw[s:e + 1])  # JSONDecodeError -> caller retries/degrades
    if not isinstance(obj, dict):
        raise ValueError("calibration response was not a JSON object")
    return _parse_calibration(obj.get("calibration"))


def _frozen_dims(prior_quality: dict) -> dict:
    """The four polish dimension scores carried forward from the staging pass, untouched. Falls
    back to the all-fives shape only if the prior audit has no usable dimensions (so a recheck of
    a malformed prior never crashes — the calibration result still governs the verdict)."""
    dims = (prior_quality or {}).get("dimensions")
    if isinstance(dims, dict) and all(isinstance(dims.get(n), dict) for n in _DIMENSIONS):
        return dims
    return {n: {"score": 5, "note": "", "fix": ""} for n in _DIMENSIONS}


def recheck_calibration(job: dict, resume: dict, cover: dict, answers: list,
                        prior_quality: dict, *,
                        llm: Optional[Callable[[str], str]] = None) -> dict:
    """Re-run ONLY the calibration sub-check after a resume/cover edit, returning the quality_audit
    to re-stamp. The four POLISH dimension scores are carried forward BYTE-FROZEN from
    `prior_quality` (no re-judging — that is the treadmill we killed); only the `calibration` array
    + its FAIL contribution are recomputed against the edited package.

    Returns a full quality_audit dict (same shape judge_quality returns): {verdict, dimensions,
    calibration, judge_ran, summary, refreshed_at}. The verdict follows _verdict_for(frozen_dims,
    new_calibration): a non-empty calibration forces FAIL; otherwise it falls back to whatever the
    frozen dims imply (so a CLEAN edit returns to the staging verdict — no wedge).

    NON-WEDGING on degradation: if the recheck llm can't run / won't parse after one retry, we do
    NOT manufacture a FAIL and do NOT clear the staging verdict — we return `prior_quality`
    essentially intact (its dims, its calibration, its verdict, its judge_ran). Only the
    calibration recheck failed; the one staging pass still stands. claude -p only; never the API.

    `llm` is injectable for tests; omitted it is constructed via make_claude_llm('sonnet')."""
    prior_quality = prior_quality if isinstance(prior_quality, dict) else {}
    frozen = _frozen_dims(prior_quality)

    if llm is None:
        from .llm import make_claude_llm
        try:
            llm = make_claude_llm("sonnet")  # raises LLMUnavailable if the CLI is missing
        except Exception:
            llm = None

    calibration = None
    if llm is not None:
        prompt = _build_calibration_prompt(job, resume, cover, answers)
        for _ in range(2):  # one attempt + one retry on a parse failure
            try:
                raw = (llm(prompt) or "").strip()
                calibration = _parse_calibration_only(raw)
                break
            except Exception:  # noqa: BLE001 — llm death OR parse failure: try once more, then degrade
                calibration = None

    if calibration is None:
        # Degraded recheck: keep the staging verdict intact. Do NOT invent a FAIL (would wedge a
        # good submit) and do NOT wipe the prior calibration. Stamp a fresh refreshed_at so the
        # staleness check sees the recheck happened, but the substance is the prior pass.
        out = dict(prior_quality)
        out.setdefault("dimensions", frozen)
        out.setdefault("calibration", [])
        out.setdefault("verdict", _verdict_for(frozen, out.get("calibration")))
        out.setdefault("judge_ran", True)
        base = (prior_quality.get("summary") or "").strip()
        note = "calibration recheck could not run; the staging quality verdict stands."
        out["summary"] = (base + " " + note).strip() if base else note
        out["refreshed_at"] = _local_iso()
        return out

    verdict = _verdict_for(frozen, calibration)
    if calibration:
        types = ", ".join(sorted({_str(v.get("type")).strip() for v in calibration if v.get("type")}))
        summary = (f"calibration FAIL ({types}): an edit left the package mis-targeted "
                   "(true content aimed at the wrong audience). Fix the flagged item(s) before "
                   "submitting.")
    else:
        summary = "calibration recheck clean after the edit; quality verdict follows the staged scores."
    return {
        "verdict": verdict,
        "dimensions": frozen,
        "calibration": calibration,
        "judge_ran": bool(prior_quality.get("judge_ran", True)),
        "summary": summary,
        "refreshed_at": _local_iso(),
    }
