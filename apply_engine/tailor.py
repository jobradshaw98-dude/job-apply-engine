# -*- coding: utf-8 -*-
"""JD-driven LLM resume + cover-letter tailoring generator.

This is the core of Sam's apply-pipeline QUALITY CONTRACT: every job he selects gets a
FULL TAILORED resume+cover package specific to that job description. This module's entire
reason to exist is to REFUSE generic output — it never falls back to the master resume, and
it HALTs (raises) rather than degrade when the JD is too thin or the LLM output fails the
hard-rule guards.

Output schema (must match what `build.py::build_resume_content` / `build_cover_letter_content`
consume, confirmed against the gold example APP-023 in applications.json):

    {
      "resume": {
        "current_bullets": list[str],     # 4-5, mapped to THIS JD
        "stateuni_bullets":   list[str],     # Assistant Research Engineer (2021) block
        "masc_bullets":     list[str],     # SEPARATE Graduate Researcher block — ANSYS lives HERE
        "skills":           list[{"label": str, "content": str}],
        "include_mobilityco":   False,         # forced False, always
        "headline":         str,           # optional per-JD headline
        "summary":          str,           # per-JD summary
      },
      "cover": {
        "addressee":  str (html, <br> separated),
        "salutation": str,
        "paragraphs": list[str],           # ~4, JD-requirement -> evidence
      }
    }

LLM infra: reuses `apply_engine/llm.py::make_claude_llm()`, which runs on Sam's Claude
SUBSCRIPTION via `claude -p` headless (zero metered-API spend — hard rule). It raises
`LLMUnavailable` if claude isn't on PATH; we propagate that and never fall back to anything.

HARD RULES enforced deterministically in `_validate()` (independent of the prompt — the safety
net that catches an LLM that ignores the DO-NOT list):
  - ANSYS outside masc/stateuni (or an entry naming State University/MASc/Helix Robotics) -> reject
        (Meridian = LS-DYNA/HyperWorks ONLY; unqualified ANSYS reads as Meridian use)
  - "MobilityCo" anywhere                            -> reject
  - "Signal Intel" / "ariasignals"               -> reject
  - "$198M" (or 198M/198 million portfolio fig)  -> reject
  - "adopted" describing the agentic frameworks   -> reject (must be "rolling out")
  - include_mobilityco must be False                 -> forced
  - empty meridian/masc/stateuni bullets, empty cover paras -> reject
"""
import argparse
import json
import re
import sys
from pathlib import Path

from . import config
from .llm import RESUME, VOICE, RESUME_RULES_FILE, make_claude_llm

# filemutex guards every applications.json write in the concurrent apply pipeline
# (feedback_apply_queue_concurrency: file writes MUST be mutex-guarded). We import it lazily at
# import time but record any failure — at WRITE time we FAIL LOUD rather than write unlocked,
# because an unlocked write can clobber a sibling apply-queue process's edit.
try:
    from .filemutex import locked as _filemutex_locked
    _FILEMUTEX_IMPORT_ERROR = None
except Exception as _e:  # pragma: no cover - filemutex is expected to be present
    _filemutex_locked = None
    _FILEMUTEX_IMPORT_ERROR = _e


def _require_filemutex():
    """Return the filemutex `locked` context manager, or raise if it is unavailable.

    Writing applications.json without the mutex is unsafe in the concurrent apply pipeline, so a
    missing filemutex is a HARD failure (we never silently write unlocked).
    """
    if _filemutex_locked is None:
        raise TailorError(
            "filemutex unavailable — refusing to write applications.json without the lock "
            f"(concurrent apply pipeline requires it): {_FILEMUTEX_IMPORT_ERROR!r}")
    return _filemutex_locked

MIN_JD_CHARS = 400  # below this we refuse to tailor — a thin JD can't drive a real package


class TailorError(RuntimeError):
    """Raised when a tailored package cannot be produced and we must HALT (never degrade)."""


# ── Hard-rule guard primitives ──────────────────────────────────────────────
# These run on the LLM output regardless of what the prompt said. They are the deterministic
# safety net for the rules in memory/feedback_*.md that are interview-exposure-critical.

# Meridian-context tokens. If "ANSYS" appears in the same text entry as any of these, that
# entry implies current Meridian ANSYS use, which is FALSE and gets Sam caught in interview.
_MERIDIAN_CONTEXT = re.compile(r"meridian|woods|quantum|max\s*d|apex\s*uw|mini\s*driver|fairway",
                               re.IGNORECASE)
# State University / MASc / Helix Robotics context. ANSYS is ONLY legitimate where one of these appears
# (or in a masc/stateuni-kind entry, which carries that context implicitly).
_STATEUNI_CONTEXT = re.compile(r"stateuni|masc|m\.a\.sc|helix robotics|thesis|graduate\s+research",
                             re.IGNORECASE)
_ANSYS = re.compile(r"\bansys\b", re.IGNORECASE)
_MOBILITYCO = re.compile(r"\bmobility\s*co\b", re.IGNORECASE)
_SIGNAL_INTEL = re.compile(r"signal\s*intel|ariasignals", re.IGNORECASE)
# Require the M/million UNIT (not optional) so a bare "198 m" in unrelated prose doesn't trip.
# Matches: "$198M", "$198 million", "198 million", "198M". The "M" abbreviation must be attached
# (no space) — "198 m" with a space is a stray unit (e.g. metres), not the portfolio figure;
# only the spelled-out " million" is allowed to follow a space.
_PORTFOLIO_FIG = re.compile(r"\$?\s*198(?:m\b|\s*million\b)", re.IGNORECASE)
# "adopted" used to describe the agentic-frameworks rollout (must be "rolling out").
_ADOPTED_FRAMEWORKS = re.compile(
    r"adopt\w*\b[^.]{0,80}\b(framework|agentic|workflow)|"
    r"\b(framework|agentic|workflow)[^.]{0,80}\badopt\w*",
    re.IGNORECASE,
)
# Coding-fluency proficiency guard (skills rows ONLY). Sam does NOT claim unaided hand-coding
# fluency (claims_ledger.md ~87, feedback_no_coding_language_fluency). On a skills line:
#   - "MATLAB" is dropped entirely — any MATLAB token in a skills row is a reject.
#   - "Python" is allowed ONLY in the AI-orchestrated framing the master resume uses
#     ("Python-based tooling, AI-orchestrated rather than hand-coded"); a bare "Python" token
#     reads as hand-coding fluency and caused the JOB-237 calibration FAIL.
# Scoped to skills entries: "Built a Python test-analysis agent" in a bullet is an AI-built-tool
# claim, not a proficiency, and must NOT trip this.
_MATLAB = re.compile(r"\bmatlab\b", re.IGNORECASE)
_PYTHON = re.compile(r"\bpython\b", re.IGNORECASE)
# An AI-orchestration qualifier anywhere within a short window of "Python" makes the mention OK.
_AI_QUALIFIER = r"(?:ai[\s\-]?orchestrat\w*|ai[\s\-]?native|ai[\s\-]?built|agent(?:ic)?[\s\-]?built)"
_PYTHON_AI_FRAMED = re.compile(
    rf"(?:{_AI_QUALIFIER}[^·|]{{0,40}}\bpython\b)|(?:\bpython\b[^·|]{{0,40}}{_AI_QUALIFIER})",
    re.IGNORECASE,
)


def _entry_texts(pkg: dict):
    """Yield (location_label, text) for every free-text entry we must guard.

    We guard per-ENTRY (not the whole blob) so the ANSYS+Meridian co-occurrence check is
    accurate: ANSYS is legitimate inside a MASc bullet, illegitimate inside a Meridian bullet.
    """
    resume = pkg.get("resume") or {}
    cover = pkg.get("cover") or {}
    for i, b in enumerate(resume.get("current_bullets") or []):
        yield (f"resume.current_bullets[{i}]", str(b), "meridian")
    for i, b in enumerate(resume.get("stateuni_bullets") or []):
        yield (f"resume.stateuni_bullets[{i}]", str(b), "other")
    for i, b in enumerate(resume.get("masc_bullets") or []):
        yield (f"resume.masc_bullets[{i}]", str(b), "masc")
    for i, s in enumerate(resume.get("skills") or []):
        if isinstance(s, dict):
            yield (f"resume.skills[{i}]", f"{s.get('label', '')} {s.get('content', '')}", "skills")
    for key in ("headline", "summary"):
        if resume.get(key):
            yield (f"resume.{key}", str(resume[key]), "other")
    yield ("cover.addressee", str(cover.get("addressee") or ""), "other")
    yield ("cover.salutation", str(cover.get("salutation") or ""), "other")
    for i, p in enumerate(cover.get("paragraphs") or []):
        yield (f"cover.paragraphs[{i}]", str(p), "cover")


# Resume-only impact-as-count patterns, imported from the canonical audit_gate so the repair loop
# uses the SAME definition as the deterministic submit gate (no rule drift). Degrades to the
# external gate if career-root isn't importable in some run context.
try:
    from audit_gate import IMPACT_AS_COUNT as _IMPACT_AS_COUNT
except Exception:  # noqa: BLE001
    _IMPACT_AS_COUNT = []


def _collect_violations(pkg: dict, *, scope: str = "both") -> list:
    """Return the list of hard-rule violation strings for `pkg` WITHOUT raising.

    `scope` controls which structural-minimum checks run, so the repair loop can validate ONE
    call's output in isolation (a resume violation re-runs only the resume call, not the cover):
      - "resume": only the resume structural minimums (meridian/masc/stateuni bullets)
      - "cover":  only the cover structural minimum (paragraphs)
      - "both":   all of them (the final combined gate)

    The per-ENTRY content guards always run on whatever entries are present in `pkg` (via
    `_entry_texts`, which simply yields nothing for an absent section), so a resume-only or
    cover-only pkg is validated correctly without special-casing each guard. The strings returned
    are the SAME strings `_validate` raises with — no rule is weakened, only the raising is
    deferred so the repair loop can read them.
    """
    violations = []
    resume = pkg.get("resume") or {}
    cover = pkg.get("cover") or {}

    # Structural minimums — empty primary content is a hard reject. The QUALITY CONTRACT requires
    # a FULL package: Meridian + MASc + State University blocks must all be present. build.py silently
    # skips an empty block, so an empty masc/stateuni here would render a thinned package without
    # any error — we must fail loud in the validator instead.
    if scope in ("resume", "both"):
        if not (resume.get("current_bullets") or []):
            violations.append(
                "empty current_bullets (a tailored resume must have Meridian bullets)")
        if not (resume.get("masc_bullets") or []):
            violations.append("empty masc_bullets (a full package requires the MASc Graduate "
                              "Researcher block — build.py silently skips an empty block)")
        if not (resume.get("stateuni_bullets") or []):
            violations.append("empty stateuni_bullets (a full package requires the State University Assistant "
                              "Research Engineer block — build.py silently skips an empty block)")
    if scope in ("cover", "both"):
        if not (cover.get("paragraphs") or []):
            violations.append(
                "empty cover paragraphs (a tailored cover letter must have paragraphs)")

    # Per-entry content guards.
    for label, text, kind in _entry_texts(pkg):
        # ANSYS is legitimate in the State University/MASc work AND in the skills tool-inventory list.
        # Allow it in a masc/stateuni-kind entry (those blocks carry that context implicitly), in a
        # skills-kind entry (a tool inventory is a capability list, NOT an employer-attributed
        # claim — the gold APP-023 resume lists "ANSYS Mechanical" right alongside LS-DYNA), or in
        # ANY entry that itself names an explicit State University/MASc/Helix Robotics context token. Reject
        # everywhere else — meridian bullets, summary, headline, and every cover paragraph —
        # because there the employer is implicitly Meridian, so unqualified ANSYS reads as Meridian
        # ANSYS use, the exact false attribution feedback_fea_tools_by_employer forbids.
        if _ANSYS.search(text):
            allowed = kind in ("masc", "stateuni", "skills") or bool(_STATEUNI_CONTEXT.search(text))
            if not allowed:
                violations.append(
                    f"{label}: names ANSYS outside the State University/MASc/Helix Robotics narrative — "
                    f"Meridian is LS-DYNA/HyperWorks ONLY; ANSYS is only legitimate in "
                    f"masc_bullets/stateuni_bullets, the skills tool-inventory list, or an entry "
                    f"that explicitly names its State University/MASc/Helix Robotics context")
        if _MOBILITYCO.search(text):
            violations.append(f"{label}: mentions MobilityCo — MobilityCo never appears on any artifact")
        if _SIGNAL_INTEL.search(text):
            violations.append(
                f"{label}: mentions Signal Intel / ariasignals — never appears on any artifact")
        if _PORTFOLIO_FIG.search(text):
            violations.append(
                f"{label}: contains the $198M portfolio figure — confidential, never rendered")
        if _ADOPTED_FRAMEWORKS.search(text):
            violations.append(
                f"{label}: says the agentic frameworks were 'adopted' — they are 'rolling out', "
                f"not yet adopted")
        # Coding-fluency proficiency guard — SKILLS rows only. A skills line is a proficiency
        # inventory, so a bare Python/MATLAB token there reads as hand-coding fluency Sam does
        # not claim (the exact mis-framing that FAILed JOB-237 calibration).
        if kind == "skills":
            if _MATLAB.search(text):
                violations.append(
                    f"{label}: lists MATLAB as a skill — MATLAB must be dropped entirely; Sam "
                    f"does not claim it as a coding proficiency (claims_ledger.md)")
            # Remove every AI-orchestrated-framed Python mention; any Python token left is bare.
            residual = _PYTHON_AI_FRAMED.sub("", text)
            if _PYTHON.search(residual):
                violations.append(
                    f"{label}: lists a bare 'Python' as a skill — Python may appear ONLY in the "
                    f"AI-orchestrated framing (e.g. 'Python-based tooling, AI-orchestrated rather "
                    f"than hand-coded'); a bare token reads as hand-coding fluency (FAILed JOB-237)")

        # Resume-ONLY renderings (covers/essays allow these): the resume requires a PERCENTAGE for
        # agent impact (never a people/time count) and NO parentheses in the body. Cover paragraphs
        # (label "cover.*") are exempt — they may use a concrete count and prose parentheses.
        if label.startswith("resume."):
            if "(" in text or ")" in text:
                violations.append(
                    f"{label}: parenthesis in the resume body — rephrase as prose and list tools "
                    f"inline with ' / ' (e.g. 'FEA in LS-DYNA / ANSYS / HyperWorks'), never '( ... )'")
            for pat in _IMPACT_AS_COUNT:
                if re.search(pat, text, re.I):
                    violations.append(
                        f"{label}: renders agent impact as a people/time/meeting count — the resume "
                        f"requires a percentage (e.g. '~90% reduction in cross-team review effort')")
                    break

    return violations


def _validate(pkg: dict) -> dict:
    """Run the deterministic hard-rule guards on the assembled package. The ultimate gate.

    Raises TailorError listing EVERY violation found (so a regen prompt can fix them all at
    once rather than one round-trip per rule). On success, normalizes the package (forces
    include_mobilityco=False) and returns it. This is a thin raising wrapper over the non-raising
    `_collect_violations` — the repair loop reads violations directly; this is the final
    defense-in-depth gate that converts any remaining violation into a true HALT.
    """
    if not isinstance(pkg, dict):
        raise TailorError("tailored package is not a dict")
    resume = pkg.get("resume")
    cover = pkg.get("cover")
    if not isinstance(resume, dict) or not isinstance(cover, dict):
        raise TailorError("package missing 'resume' or 'cover' dict")

    violations = _collect_violations(pkg, scope="both")
    if violations:
        raise TailorError("tailored package failed hard-rule validation:\n  - "
                          + "\n  - ".join(violations))

    # Normalize: include_mobilityco is ALWAYS forced False (never trust the model on this).
    resume["include_mobilityco"] = False
    return pkg


# ── Output shape validation ─────────────────────────────────────────────────

def _check_shape(pkg) -> dict:
    """Validate the LLM returned the exact keys build.py consumes. Raise TailorError if not.

    This is the schema gate (separate from the hard-rule content gate). It coerces nothing it
    can't safely coerce — a wrong shape means the LLM didn't follow the contract and we HALT.
    """
    if not isinstance(pkg, dict):
        raise TailorError("LLM output is not a JSON object")
    resume = pkg.get("resume")
    cover = pkg.get("cover")
    if not isinstance(resume, dict):
        raise TailorError("LLM output missing 'resume' object")
    if not isinstance(cover, dict):
        raise TailorError("LLM output missing 'cover' object")

    # resume required list/str keys
    for key in ("current_bullets", "stateuni_bullets", "masc_bullets"):
        if not isinstance(resume.get(key), list):
            raise TailorError(f"resume.{key} must be a list of strings")
        if not all(isinstance(x, str) for x in resume[key]):
            raise TailorError(f"resume.{key} must contain only strings")
    skills = resume.get("skills")
    if not isinstance(skills, list) or not skills:
        raise TailorError("resume.skills must be a non-empty list of {label, content}")
    for s in skills:
        if not isinstance(s, dict) or "label" not in s or "content" not in s:
            raise TailorError("each resume.skills entry must be {label, content}")

    # cover required keys
    if not isinstance(cover.get("paragraphs"), list) or not all(
            isinstance(x, str) for x in cover.get("paragraphs") or []):
        raise TailorError("cover.paragraphs must be a list of strings")
    cover.setdefault("salutation", "Dear Hiring Manager,")
    cover.setdefault("addressee", "")
    if not isinstance(cover.get("salutation"), str) or not isinstance(cover.get("addressee"), str):
        raise TailorError("cover.salutation and cover.addressee must be strings")

    return pkg


# ── JSON extraction ─────────────────────────────────────────────────────────

def _extract_json(raw: str) -> dict:
    """Parse JSON from an LLM response, tolerating ```json fences and surrounding prose.

    Raises json.JSONDecodeError (let the caller decide whether to retry) on failure.
    """
    text = (raw or "").strip()
    # Strip a leading/trailing code fence if present.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fall back to the first balanced {...} object in the text.
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


# ── Prompt construction ─────────────────────────────────────────────────────

def _read_text(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception:
        return ""


_HARD_RULES_BLOCK = """\
HARD RULES — violating ANY of these is a hard FAIL (the output will be rejected by a
deterministic guard, so do not even get close):
1. Meridian Devices work uses LS-DYNA and HyperWorks (Altair: HyperMesh, OptiStruct, HyperStudy)
   ONLY. NEVER write "ANSYS" in any Meridian bullet or Meridian-context sentence. ANSYS belongs
   ONLY to the State University / MASc / Helix Robotics narrative — put it in masc_bullets, never meridian.
2. NEVER mention "MobilityCo" anywhere. Treat it as if it never happened.
3. NEVER mention "Signal Intel" or "ariasignals" anywhere.
4. NEVER write the "$198M" portfolio figure (or "198 million") anywhere — it is confidential.
5. The agentic frameworks Sam built at Meridian are "rolling out" across R&D — they are NOT
   yet "adopted". Never say "adopted"; say "rolling out" / "being rolled out".
6. Do NOT lead with CAD. Sam's hands-on CAD seat-time is limited. Lead with simulation,
   optimization, and test-to-simulation correlation. Never write "expert in SolidWorks/NX",
   never imply he "designed"/"modeled" parts from scratch in CAD.
7. Assert only what the resume FACTS support. Do not invent tools, numbers, employers, metrics,
   or outcomes. Do not name a technology unless it appears in the FACTS.
8. ARIA / any personal project is personal, single-user, daily-use — NEVER "production",
   "at scale", "deployed", or "multi-user".
9. Codex = the Meridian R&D automation tools. Claude Code = the personal ARIA platform. Never
   swap them.
10. The skills list must NEVER contain a bare "Python" or "MATLAB" as a coding proficiency.
    Sam does NOT hand-code — he builds automation by orchestrating AI agents (Codex, Claude
    Code). DROP "MATLAB" entirely. Python may appear ONLY framed as AI-orchestrated tooling
    (e.g. "Python-based tooling, AI-orchestrated rather than hand-coded", "AI-orchestrated
    Python-based analysis automation"), never as a standalone skill token like "... · Python".
"""

# Schema for the RESUME-only call (Call A). The cover keys are intentionally absent.
_RESUME_SCHEMA_BLOCK = """\
Return STRICT JSON (and NOTHING else — no prose, no markdown fence) with exactly this shape:

{
  "current_bullets": ["...", "..."],        // 4-5 bullets, strongest Meridian work mapped to THIS JD's requirements, impact-led, measurable where provable
  "stateuni_bullets":   ["...", "..."],        // 1-2 bullets for the 2021 Assistant Research Engineer block (prosthetic/implant design, clinician collaboration)
  "masc_bullets":     ["...", "..."],        // 1-2 bullets for the SEPARATE MASc Graduate Researcher block — the automated design-optimization framework; ANSYS + OptiSLang live HERE
  "skills":           [{"label": "...", "content": "..."}],   // 3-5 grouped skill rows tuned to the JD (mirror the JD's vocabulary where genuine; ANSYS is fine in this tool-inventory list; NEVER a bare "Python"/"MATLAB" token — drop MATLAB, frame Python only as AI-orchestrated tooling)
  "headline":         "...",                 // a concise per-JD headline
  "summary":          "..."                  // a 2-3 sentence per-JD summary; simulation-led, ~5 years, obsessive learner, AI-native systems building
}
"""

# Schema for the COVER-only call (Call B). The resume keys are intentionally absent.
_COVER_SCHEMA_BLOCK = """\
Return STRICT JSON (and NOTHING else — no prose, no markdown fence) with exactly this shape:

{
  "addressee":  "Hiring Manager<br>{Company} &mdash; {Team/Function}<br>{City, ST}",
  "salutation": "Dear Hiring Manager,",
  "paragraphs": ["P1", "P2", "P3", "P4"]     // exactly 4: (P1) role+company hook + why-him, (P2) Meridian+MASc evidence mapped to the JD with a concrete named project, (P3) the cross-domain differentiator + one company-specific paragraph nobody else could write, (P4) short warm close
}
"""


def _job_header(job: dict, *, master_text: str) -> str:
    """The shared context every call needs: the target job, the JD, and Sam's FACTS."""
    company = job.get("company", "")
    role = job.get("role") or job.get("title") or ""
    track = job.get("track")
    jd = str(job.get("jd_text") or "")
    return f"""# TARGET JOB
Company: {company}
Role: {role}
Track: {track}

## JOB DESCRIPTION (the requirements to map against — not a source of facts about Sam)
{jd}

# SAM'S FACTS (the ONLY source of truth for what he has done — master resume)
{master_text}"""


def _build_resume_prompt(job: dict, *, master_text: str, rules_text: str) -> str:
    """Call A — produce ONLY the resume dict. Gets the resume-format learned rules; no cover/voice
    block (the cover call owns voice/craft), keeping this prompt small so it clears the 240s cap."""
    rules_section = ("\n\n# LEARNED RESUME/STYLE RULES\n" + rules_text) if rules_text.strip() else ""
    return f"""You are tailoring Sam Rivera's RESUME bullets to ONE specific job. \
Map the job description's real requirements to Sam's genuine experience. Do NOT stretch, infer, \
or fabricate a connection that the FACTS don't support.

{_job_header(job, master_text=master_text)}{rules_section}

{_HARD_RULES_BLOCK}

# YOUR TASK (RESUME ONLY — do NOT write a cover letter)
1. Select and REWRITE the strongest 4-5 Meridian bullets, each mapped to a real requirement in
   THIS JD. Mirror the JD's language where it's genuine. Lead with action + outcome.
2. Pick 1-2 stateuni_bullets (2021 Assistant Research Engineer) and 1-2 masc_bullets (MASc
   Graduate Researcher — the automated design-optimization framework; ANSYS + OptiSLang go HERE,
   never in Meridian bullets).
3. Compose a skills list (3-5 grouped rows) tuned to this JD.
4. Write a per-JD headline and a 2-3 sentence summary.
5. RESUME IMPACT = PERCENTAGE. Render any Meridian agent's impact as a percentage (e.g. "~90%
   reduction in cross-team review effort"), NEVER as a count of people/engineers or an
   hour/meeting duration ("a 10-person, two-hour review"). A people/time count on a resume bullet
   is rejected by a deterministic guard. (Such counts are fine in a cover letter — never here.)
6. NO PARENTHESES anywhere in the resume body — not in the summary, skills rows, or bullets.
   Rephrase any parenthetical aside as prose; write tool lists inline (e.g. "FEA in LS-DYNA /
   ANSYS Mechanical / HyperWorks", not "FEA (LS-DYNA, ANSYS Mechanical, HyperWorks)"). A
   parenthesis in the resume body is rejected by a deterministic guard.

{_RESUME_SCHEMA_BLOCK}"""


def _build_cover_prompt(job: dict, *, master_text: str, voice_text: str) -> str:
    """Call B — produce ONLY the cover dict. Gets the voice/craft block (cover-letter writing);
    no resume-format rules, keeping this prompt small so it clears the 240s cap."""
    voice_section = ("\n\n# VOICE & CRAFT (how Sam writes — style, NOT new facts)\n" + voice_text
                     ) if voice_text.strip() else ""
    return f"""You are writing Sam Rivera's COVER LETTER for ONE specific job. \
Map the job description's real requirements to Sam's genuine experience. Do NOT stretch, infer, \
or fabricate a connection that the FACTS don't support.

{_job_header(job, master_text=master_text)}{voice_section}

{_HARD_RULES_BLOCK}

# YOUR TASK (COVER LETTER ONLY — do NOT write resume bullets)
Draft a 4-paragraph cover letter that maps JD requirements -> Sam's evidence, leading with
Meridian + MASc, in his voice. At most ONE em-dash in the whole letter. Earn at least one
paragraph that no other candidate could have written about THIS company.

{_COVER_SCHEMA_BLOCK}"""


_REPROMPT = ("Your previous response was not valid JSON. Return ONLY a single valid JSON object "
             "matching the schema — no prose, no markdown code fence, nothing else.\n\n")

# Max self-correction reprompts per call AFTER the original attempt. 2 repairs => 3 total tries
# (original + 2). Sam: quality over speed for this generator — a few extra calls is acceptable.
_MAX_REPAIRS = 2


def _build_repair_prompt(base_prompt: str, prior_json: dict, violations: list) -> str:
    """Reprompt the SAME call with its own just-produced JSON + the exact violation messages.

    We feed the model back its prior output verbatim and the deterministic guard's exact strings,
    and instruct it to fix ONLY those issues — not to introduce new content. This keeps the
    self-correction tightly scoped (the guards remain the final authority on whether it worked).
    """
    violation_block = "\n".join(f"  - {v}" for v in violations)
    return (
        f"{base_prompt}\n\n"
        "# YOUR PREVIOUS OUTPUT (the JSON you just produced)\n"
        f"{json.dumps(prior_json, ensure_ascii=False, indent=2)}\n\n"
        "# IT VIOLATED THESE HARD RULES\n"
        f"{violation_block}\n\n"
        "Fix ONLY those issues and re-emit the corrected JSON in the SAME schema. Do not "
        "introduce new content, do not change anything that was not flagged, and return ONLY the "
        "JSON object — no prose, no markdown code fence."
    )


def _generate_valid_section(llm, base_prompt: str, *, what: str, scope: str) -> dict:
    """Run one section call (resume or cover) and self-correct via the validation-repair loop.

    Flow: call -> parse -> collect violations for THIS section's scope. If clean, return it. If it
    violates a hard rule, reprompt the SAME call with its output + the exact violations (up to
    `_MAX_REPAIRS` times). If still violating after the repairs, raise TailorError — a true HALT
    with the final violations (no partial, no master fallback).

    Localizing the loop per-section means a resume violation re-runs only the resume call, never
    the (already-good) cover call.
    """
    section = _call_json(llm, base_prompt, what=what)
    violations = _collect_violations({what: section}, scope=scope)
    attempt = 0
    while violations and attempt < _MAX_REPAIRS:
        attempt += 1
        repair_prompt = _build_repair_prompt(base_prompt, section, violations)
        section = _call_json(llm, repair_prompt, what=what)
        violations = _collect_violations({what: section}, scope=scope)
    if violations:
        raise TailorError(
            f"{what} call failed hard-rule validation after {_MAX_REPAIRS} repair attempts:\n  - "
            + "\n  - ".join(violations))
    return section


def _call_json(llm, prompt: str, *, what: str) -> dict:
    """Run one LLM call and parse its JSON, with the same strip-fences + ONE retry contract the
    one-shot path used. `what` ("resume"/"cover") only flavours the error message.

    Raises TailorError if the JSON still won't parse after the single reprompt retry.
    """
    raw = llm(prompt)
    try:
        return _extract_json(raw)
    except json.JSONDecodeError:
        raw = llm(_REPROMPT + prompt)
        try:
            return _extract_json(raw)
        except json.JSONDecodeError as e:
            raise TailorError(
                f"LLM did not return valid JSON for the {what} call after retry: {e}") from e


# ── Public API ──────────────────────────────────────────────────────────────

def generate_tailored_package(job: dict, *, llm=None, master_path=None) -> dict:
    """Generate a fully tailored {"resume": {...}, "cover": {...}} package for one job.

    Args:
      job: a jobs.json record. Needs at least `company`, `role`/`title`, and `jd_text`.
      llm: a callable prompt->str (e.g. from make_claude_llm). If None, one is built.
      master_path: override path to master_resume.md (defaults to the real one).

    Raises:
      ValueError: if the JD is missing or under ~400 chars (we refuse to tailor a thin JD).
      TailorError: if the LLM output fails JSON parse (after one retry), shape, or any
        hard-rule guard. We NEVER return a master-resume fallback — refusing generic output is
        this module's whole purpose.
      LLMUnavailable: propagated from make_claude_llm if claude isn't on PATH (never falls back
        to the metered API).
    """
    jd = str(job.get("jd_text") or "").strip()
    if len(jd) < MIN_JD_CHARS:
        raise ValueError(
            f"insufficient JD to tailor ({len(jd)} chars < {MIN_JD_CHARS}); "
            "refusing to tailor on a thin JD")

    if llm is None:
        llm = make_claude_llm(model="sonnet")

    master_text = _read_text(master_path or RESUME)
    if not master_text.strip():
        raise TailorError(f"master resume not found / empty at {master_path or RESUME}")
    voice_text = _read_text(VOICE)
    rules_text = _read_text(RESUME_RULES_FILE)

    # TWO sequential focused calls instead of one combined prompt. The one-shot prompt (resume +
    # cover in a single JSON) measured ~239s — right at make_claude_llm's 240s subprocess cap, so
    # it timed out in practice. Splitting the work means each call ships a smaller prompt and only
    # the slice it owns, comfortably finishing under the cap. Each call keeps the same strip-fences
    # + one-retry JSON contract. Halt semantics are UNCHANGED: if EITHER call fails/times out or
    # its JSON won't parse, the raised error propagates and we produce NO partial package and NEVER
    # fall back to the master resume.
    # Each section is generated through a VALIDATION-REPAIR LOOP: if the LLM makes a recoverable
    # phrasing slip (e.g. "adopted" instead of "rolling out"), we reprompt the SAME call with its
    # output + the exact violation strings and let it self-correct (up to 2 repairs each). The
    # deterministic guards stay the final authority — if a section still violates after the
    # repairs, _generate_valid_section raises and we HALT (no partial, no master fallback). We
    # validate per-section so a resume violation only re-runs the resume call, not the cover.
    resume_prompt = _build_resume_prompt(job, master_text=master_text, rules_text=rules_text)
    resume = _generate_valid_section(llm, resume_prompt, what="resume", scope="resume")

    cover_prompt = _build_cover_prompt(job, master_text=master_text, voice_text=voice_text)
    cover = _generate_valid_section(llm, cover_prompt, what="cover", scope="cover")

    # Assemble the combined object and run the SAME deterministic gates on it exactly as before:
    # schema first, then the hard-rule content guards (which also force include_mobilityco=False). The
    # combined _validate is defense in depth — per-section loops should have already cleared every
    # violation, but if anything slips through here it is a true HALT.
    pkg = {"resume": resume, "cover": cover}
    pkg = _check_shape(pkg)   # schema gate
    pkg = _validate(pkg)      # deterministic hard-rule gate (also forces include_mobilityco=False)

    # Return only the two content dicts in the exact shape build.py consumes.
    return {"resume": pkg["resume"], "cover": pkg["cover"]}


# ── CLI ─────────────────────────────────────────────────────────────────────

def _load_json_list(path: Path) -> list:
    return json.loads(path.read_text(encoding="utf-8"))


def _find_job(jobs: list, job_id: str) -> dict:
    j = next((x for x in jobs if x.get("id") == job_id), None)
    if not j:
        raise TailorError(f"job {job_id} not found in {config.JOBS_JSON}")
    return j


def _next_app_id(apps: list) -> str:
    nums = []
    for a in apps:
        m = re.match(r"APP-(\d+)$", str(a.get("id") or ""))
        if m:
            nums.append(int(m.group(1)))
    return f"APP-{(max(nums) + 1) if nums else 1:03d}"


def _write_app_record(job_id: str, pkg: dict) -> str:
    """Resolve (match by job_id) or CREATE the APP record, splice in resume+cover, atomic write.

    Uses the file mutex + a fresh re-read under the lock so a sibling apply-queue process can't
    clobber this edit (merge-safe). Returns the APP id written.
    """
    apps_path = config.APPLICATIONS_JSON

    def _do_write() -> str:
        apps = _load_json_list(apps_path)
        app = next((a for a in apps if a.get("job_id") == job_id), None)
        if app is None:
            # Create a new record from the job, mirroring get_app_content's expected keys.
            jobs = _load_json_list(config.JOBS_JSON)
            job = _find_job(jobs, job_id)
            app = {
                "id": _next_app_id(apps),
                "job_id": job_id,
                "company": job.get("company", ""),
                "role": job.get("role") or job.get("title") or "",
                "track": job.get("track"),
                "status": "drafting",
            }
            apps.append(app)
        elif not app.get("id"):
            # An engine-written stub record (job_id/status/apply_run_dir only, no APP id) matched.
            # Writing resume/cover into it without an id would make `return app["id"]` KeyError and
            # render later with `--job ?`. Backfill a real APP id so build.py can find it.
            app["id"] = _next_app_id(apps)
            app.setdefault("company", "")
            app.setdefault("role", "")
        app["resume"] = pkg["resume"]
        app["cover"] = pkg["cover"]
        tmp = apps_path.with_suffix(apps_path.suffix + ".tmp")
        tmp.write_text(json.dumps(apps, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(apps_path)
        return app["id"]

    with _require_filemutex()(apps_path):
        return _do_write()


def rebuild_tailored_package(job: dict) -> str:
    """Force a FRESH tailored package for `job`, replacing the stored one ONLY on success.

    This is the `--rebuild` escape hatch: packages built by an older/weaker drafting pipeline can
    be rebuilt with the current one. Crucially it GENERATES BEFORE it destroys — `generate_tailored_
    package` runs first and RAISES on any failure (thin JD, LLM down, validation-exhausted), so a
    transient failure leaves the existing package untouched (the caller halts to needs_build with the
    OLD package still intact). Only on a clean generate do we overwrite resume+cover (atomic + mutex
    via `_write_app_record`) and re-render the PDFs. Never leaves the record packageless — the failure
    mode the clear-then-generate ordering would have created.

    Custom answers are NOT touched: they live in the staged manifest and re-draft live on a `--answer`
    stage, so they refresh on their own. Returns the APP id written."""
    pkg = generate_tailored_package(job)                 # raises on failure — old package untouched
    app_id = _write_app_record(job.get("id", ""), pkg)   # overwrite resume+cover (atomic + mutex)
    _render(app_id)                                       # build.py render (reuse helper, no dup)
    return app_id


def ensure_app_id(job_id: str) -> str:
    """Return the APP id for job_id, backfilling a real APP id onto a matched id-less stub
    record (engine-written job_id/status stubs) and persisting it (atomic + mutex). Raises
    TailorError if no applications.json record matches. Used by the 'existing tailored content'
    path so it never renders build.py with `--job ?`."""
    apps_path = config.APPLICATIONS_JSON

    def _do() -> str:
        apps = _load_json_list(apps_path)
        app = next((a for a in apps if a.get("job_id") == job_id), None)
        if app is None:
            raise TailorError(f"no applications.json record for {job_id}")
        if not app.get("id"):
            app["id"] = _next_app_id(apps)
            tmp = apps_path.with_suffix(apps_path.suffix + ".tmp")
            tmp.write_text(json.dumps(apps, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(apps_path)
        return app["id"]

    with _require_filemutex()(apps_path):
        return _do()


def _parse_autofit_adjustments(app_id: str, build_stdout: str):
    """G3: recover the cover-render auto-fit adjustment count for `app_id`. Returns an int (>=0)
    or None when no count could be found (pass-when-absent — the caller stores nothing, so the G3
    gate stays absent-friendly).

    Two redundant channels, sidecar first (authoritative, written next to the PDFs), then the
    `AUTOFIT_ADJUSTMENTS=<n>` stdout line build.py emits. PURE-ish (one filesystem read). Never
    raises — a render that didn't surface a count must not break the stage run."""
    import re
    # (1) sidecar cover_render.json in the app's tailored dir (the build wrote it there).
    try:
        app_dir = _app_tailored_dir(app_id)
        if app_dir is not None:
            sidecar = app_dir / "cover_render.json"
            if sidecar.exists():
                data = json.loads(sidecar.read_text(encoding="utf-8"))
                n = data.get("autofit_adjustments")
                if isinstance(n, (int, float)):
                    return int(n)
    except Exception:
        pass
    # (2) the stdout marker line (redundant channel; survives a missing/garbled sidecar).
    try:
        m = re.search(r"^AUTOFIT_ADJUSTMENTS=(\d+)\s*$", build_stdout or "", re.MULTILINE)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def _app_tailored_dir(app_id: str):
    """The applications/<APP-ID>-<company-slug>/ dir build.py renders into, or None. Mirrors
    build.py's slug logic (company up to '(', spaces/slashes -> '-'). PURE (one JSON read)."""
    try:
        apps = _load_json_list(config.APPLICATIONS_JSON)
        app = next((a for a in apps if a.get("id") == app_id), None)
        if app is None:
            return None
        company = (app.get("company") or app_id).split("(")[0].strip()
        slug = company.replace(" ", "-").replace("/", "-")
        from pathlib import Path
        return Path(config.PKG_DIR.parent) / "applications" / f"{app_id}-{slug}"
    except Exception:
        return None


def _store_autofit_adjustments(app_id: str, adjustments: int) -> None:
    """G3: persist the cover auto-fit adjustment count onto the APP record's `cover` dict
    (applications.json) under `autofit_adjustments`, so it flows onto the staged record and the
    `finish._g3_cover_ok` gate (which reads `record['cover']['autofit_adjustments']`) sees it.
    Merge-safe (filemutex + atomic replace), additive, never raises."""
    apps_path = config.APPLICATIONS_JSON

    def _do() -> None:
        apps = _load_json_list(apps_path)
        app = next((a for a in apps if a.get("id") == app_id), None)
        if app is None:
            return
        cover = app.get("cover")
        if not isinstance(cover, dict):
            cover = {}
        cover["autofit_adjustments"] = int(adjustments)
        app["cover"] = cover
        tmp = apps_path.with_suffix(apps_path.suffix + ".tmp")
        tmp.write_text(json.dumps(apps, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(apps_path)

    try:
        with _require_filemutex()(apps_path):
            _do()
    except Exception:
        pass  # the count is best-effort metadata; never fail the stage run over storing it


def _render(app_id: str) -> None:
    """Invoke build.py to render PDFs for the freshly written APP record.

    The build.py call has a hard 180s timeout. build.py's Edge `--print-to-pdf` step can hang
    indefinitely (the known no-op-with-a-browser-open class — see feedback_career_render_edge_silent_noop),
    which would otherwise wedge the live/batch stage run forever. On a timeout OR a non-zero build
    exit we raise TailorError; main()/ensure_tailored_package turn that into a `needs_build` halt,
    which correctly attaches NOTHING on a render hang rather than proceeding with stale PDFs.

    G3: after a clean render, parse the cover auto-fit adjustment count from the build output
    (sidecar cover_render.json, then the AUTOFIT_ADJUSTMENTS= stdout line) and store it on the APP
    record's `cover` dict, so the finish._g3_cover_ok gate can fail-closed when the cover was
    font-shrunk (>0 adjustments) to fake one page. Best-effort: a missing count stores nothing
    (pass-when-absent)."""
    import subprocess
    career_dir = config.PKG_DIR.parent
    venv_py = career_dir / "apply_engine" / ".venv" / "Scripts" / "python.exe"
    py = str(venv_py) if venv_py.exists() else sys.executable
    cmd = [py, str(career_dir / "build.py"), "--job", app_id, "--type", "both"]
    try:
        r = subprocess.run(cmd, cwd=str(career_dir), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
    except subprocess.TimeoutExpired as e:
        raise TailorError(f"build.py render for {app_id} timed out after {e.timeout:.0f}s "
                          f"(likely an Edge --print-to-pdf hang with a browser open)") from e
    if r.returncode != 0:
        raise TailorError(f"build.py render failed for {app_id} (exit {r.returncode}):\n"
                          f"{(r.stderr or r.stdout or '').strip()[:1000]}")

    # G3: surface the cover auto-fit adjustment count onto the record (pass-when-absent if none).
    n = _parse_autofit_adjustments(app_id, r.stdout or "")
    if n is not None:
        _store_autofit_adjustments(app_id, n)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m apply_engine.tailor",
        description="Generate a JD-tailored resume+cover package and write it to an APP record.")
    ap.add_argument("--job", required=True, help="jobs.json id, e.g. JOB-079")
    ap.add_argument("--app", default=None,
                    help="explicit APP id to write into (default: match by job_id or create)")
    ap.add_argument("--no-render", action="store_true",
                    help="skip the build.py PDF render step")
    args = ap.parse_args(argv)

    try:
        jobs = _load_json_list(config.JOBS_JSON)
        job = _find_job(jobs, args.job)
        pkg = generate_tailored_package(job)
    except (ValueError, TailorError) as e:
        print(f"HALT: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 — includes LLMUnavailable; surface clearly, write nothing
        print(f"HALT: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    try:
        if args.app:
            # Force-write into an explicit APP id (still merge-safe).
            app_id = _write_app_record_explicit(args.job, args.app, pkg)
        else:
            app_id = _write_app_record(args.job, pkg)
    except Exception as e:  # noqa: BLE001 — never leave a partial write
        print(f"HALT: failed to write APP record: {e}", file=sys.stderr)
        return 2

    print(f"OK: wrote tailored package to {app_id}")

    if not args.no_render:
        try:
            _render(app_id)
            print(f"OK: rendered PDFs for {app_id}")
        except Exception as e:  # noqa: BLE001
            print(f"WARN: package written but render failed: {e}", file=sys.stderr)
            return 1
    return 0


def _write_app_record_explicit(job_id: str, app_id: str, pkg: dict) -> str:
    """Like _write_app_record but targets a caller-specified APP id (create if absent)."""
    apps_path = config.APPLICATIONS_JSON

    def _do_write() -> str:
        apps = _load_json_list(apps_path)
        app = next((a for a in apps if a.get("id") == app_id), None)
        if app is None:
            jobs = _load_json_list(config.JOBS_JSON)
            job = _find_job(jobs, job_id)
            app = {
                "id": app_id,
                "job_id": job_id,
                "company": job.get("company", ""),
                "role": job.get("role") or job.get("title") or "",
                "track": job.get("track"),
                "status": "drafting",
            }
            apps.append(app)
        app["resume"] = pkg["resume"]
        app["cover"] = pkg["cover"]
        tmp = apps_path.with_suffix(apps_path.suffix + ".tmp")
        tmp.write_text(json.dumps(apps, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(apps_path)
        return app["id"]

    with _require_filemutex()(apps_path):
        return _do_write()


if __name__ == "__main__":
    raise SystemExit(main())
