"""Conservative grounded Yes/No screening-qualifier classifier.

The choice-picker (choice_gen.resolve_choice) DECLINEs self-assessment / judgment-call
questions, so it escalated EVERY binary Yes/No screening qualifier ("3+ years experience?",
"deployed AI in production?") to the user — even the ones that are a clear, truthful Yes. This
module answers those, and ONLY those, while keeping the work_auth.py safety discipline:

  * a decision is VERIFIED — YES/NO is mapped to a real offered option, or it ESCALATES;
  * excluded classes (work-auth, EEO, relocation, criminal, clearance, …) short-circuit to
    ESCALATE WITHOUT calling the model — never auto-answered;
  * garbled / ambiguous / empty model output fails CLOSED to ESCALATE;
  * a YES is produced only when truthfully grounded in capabilities.md;
  * the chosen value still runs through the fabrication gate (defense in depth).

This is the one path that could auto-submit a FALSE qualification — hence the belt and braces.
"""
import re
from dataclasses import dataclass
from enum import Enum

from . import config
from .work_auth import classify_work_auth, WorkAuthDecision
from .choice_gen import Choice, resolve_choice

# Prefer the user-supplied real file (git-ignored); fall back to the committed example (the
# fictional demo applicant) — mirrors VOICE/voice_profile.example.md.
CAPABILITIES_FILE = config.PKG_DIR / "capabilities.md"
CAPABILITIES_EXAMPLE = config.PKG_DIR / "capabilities.example.md"


class ScreeningDecision(str, Enum):
    YES = "yes"
    NO = "no"
    ESCALATE = "escalate"      # ambiguous / unsupported / excluded -> leave for the user
    UNRELATED = "unrelated"    # not a binary Yes/No -> caller uses the generic picker


@dataclass
class ScreeningResult:
    decision: ScreeningDecision
    value: str = ""            # the offered option AS THE FORM PRESENTS IT, or ""
    reason: str = ""


def _norm(s: str) -> str:
    return " ".join(str(s or "").lower().split())


def is_yesno_screening(options) -> bool:
    """True iff `options` is exactly a clean binary {Yes, No} set (case/space-insensitive).

    A decorated option ("Yes, I do") or a third option means it is NOT a plain screening
    qualifier and must go through the generic facts-grounded picker instead."""
    if not options:
        return False
    normed = {_norm(o) for o in options}
    return normed == {"yes", "no"}


# --- excluded classes: never auto-answered, ESCALATE deterministically (no model call) -----
# Each has its own handler upstream (work_auth/office), is a protected/EEO field, is a factual
# human item, or is DISQUALIFYING-if-Yes. A truthful answer here is not the engine's to give.
# NOTE on boundaries: a LEADING `\b(?:...)` only — NO trailing `\b`. A trailing boundary breaks
# every stem-prefix entry (`relocat`+`\b` cannot match "relocate", `disab` cannot match
# "disability", etc.), silently leaking those classes to the model. The leading boundary still
# prevents substring false-matches like "race" inside "embrace". Over-exclusion here is the SAFE
# failure mode (it escalates to the user), so stems are preferred over exhaustive word lists.
_EXCLUDED_PATTERNS = re.compile(
    r"\b(?:"
    # EEO / demographic
    r"hispanic|latino|latinx|race\b|racial|ethnic|gender|underrepresented|pronoun|"
    r"veteran|disab|lgbtq|sexual orientation|"
    # relocation / travel
    r"relocat|willing to travel|able to travel|travel up to|% travel|"
    # criminal / background / drug
    r"convict|felony|misdemeanor|crime|criminal|arrest|background check|drug (?:test|screen)|"
    # clearance
    r"security clearance|clearance|polygraph|"
    # restrictive covenants / prior employment / references
    r"non-?compete|restrictive covenant|nda\b|previously (?:been )?(?:employed|worked)|"
    r"former employee|rehire|provide references|"
    # other factual/judgment human items
    r"terminated|fired for cause|salary|compensation|notice period|start date|"
    r"when can you start|can you start working|how did you hear|18 years (?:of age|or older)"
    r")",
    re.IGNORECASE,
)

# A NEGATED qualifier inverts polarity: "Do you LACK 3+ years?" is truthfully No, but a model
# grounded on "experience: YES" could emit the disqualifying Yes. The fabrication gate only ever
# sees the bare option text ("Yes"), never the question, so it cannot catch wrong polarity. The
# only safe handling is to ESCALATE any negated question deterministically, before the model runs.
_NEGATION = re.compile(
    r"\b(?:not|no|none|zero|no longer|never|cannot|can't|can not|unable|unwilling|incapable|"
    r"ineligible|disqualified|unqualified|barred|prohibited|insufficient|missing|"
    r"lack|lacks|lacking|don't|do not|doesn't|does not|haven't|hasn't|isn't|aren't|"
    r"wasn't|weren't|without|fail to)\b",
    re.IGNORECASE,
)


# NOTE on coding questions (2026-06-09): these are NOT excluded. The applicant codes Python via LLM
# harnesses (Claude Code/Codex) and ships/operates production software that way, so coding /
# experience / proficiency questions are answered TRUTHFULLY (capabilities.md grounds them YES, and
# the explicitly-unaided "from scratch / without AI" variant NO). The job is best honest wording,
# not escalation. See capabilities.md "Coding / software development".


def _is_excluded(question: str) -> bool:
    q = question or ""
    if classify_work_auth(q) != WorkAuthDecision.UNRELATED:
        return True
    if _NEGATION.search(q):
        return True
    return bool(_EXCLUDED_PATTERNS.search(q))


def build_screening_prompt(question: str, capabilities: str) -> str:
    return (
        "You are answering a binary YES/NO screening qualifier on the applicant's job\n"
        "application. Use ONLY the CAPABILITY FACTS below — they are the sole source of truth\n"
        "about the applicant.\n\n"
        "Output EXACTLY one token, nothing else:\n"
        "  YES       — only if the CAPABILITY FACTS clearly and truthfully support a Yes.\n"
        "  NO        — only if the CAPABILITY FACTS clearly and truthfully support a No.\n"
        "  ESCALATE  — anything not clearly covered, any judgment call, or any 'it depends' /\n"
        "              partial. When in doubt, ESCALATE. NEVER guess or fabricate a Yes.\n\n"
        f"CAPABILITY FACTS:\n{capabilities}\n\n"
        f"SCREENING QUESTION: {question}\n\n"
        "Answer (YES, NO, or ESCALATE):"
    )


_DECISION_RE = re.compile(r"^(yes|no|escalate)\b", re.IGNORECASE)


def _option_for(decision: ScreeningDecision, options) -> str:
    want = "yes" if decision == ScreeningDecision.YES else "no"
    for o in options:
        if _norm(o) == want:
            return o
    return ""


def classify_screening(question, options, capabilities, llm_fn, audit_fn=None) -> ScreeningResult:
    """Classify a binary Yes/No screening qualifier. PURE except for the injected llm_fn/audit_fn.

    Returns UNRELATED when `options` is not a clean Yes/No (caller falls through to the generic
    picker), ESCALATE for excluded classes / ambiguity / any failure, and YES/NO (with the
    matched offered option in `.value`) only for a truthfully-grounded, gate-clean answer."""
    if not is_yesno_screening(options):
        return ScreeningResult(ScreeningDecision.UNRELATED, "", "not a binary yes/no")

    if _is_excluded(question):
        return ScreeningResult(ScreeningDecision.ESCALATE, "",
                               "excluded class (work-auth/EEO/sensitive) — left for the user")

    try:
        raw = (llm_fn(build_screening_prompt(question, capabilities)) or "").strip()
    except Exception as e:  # noqa: BLE001 — never auto-answer on a model error
        return ScreeningResult(ScreeningDecision.ESCALATE, "", f"llm error: {e!r}")

    m = _DECISION_RE.match(raw)
    if not m:
        # empty or anything that is not a bare YES/NO/ESCALATE token -> fail closed
        return ScreeningResult(ScreeningDecision.ESCALATE, "",
                               f"unparseable model output ({raw[:40]!r})")
    token = m.group(1).lower()
    if token == "escalate":
        return ScreeningResult(ScreeningDecision.ESCALATE, "", "model escalated")

    decision = ScreeningDecision.YES if token == "yes" else ScreeningDecision.NO
    value = _option_for(decision, options)
    if not value:  # should not happen given is_yesno_screening, but never select a phantom
        return ScreeningResult(ScreeningDecision.ESCALATE, "",
                               "decision did not map to an offered option")

    # defense in depth: the chosen answer still passes the fabrication gate.
    if audit_fn is not None:
        try:
            blocks = audit_fn(value) or []
        except Exception as e:  # noqa: BLE001 — gate down -> fail safe (escalate)
            blocks = [f"audit error: {e!r}"]
        if blocks:
            return ScreeningResult(ScreeningDecision.ESCALATE, "",
                                   "gate flagged chosen answer: " + "; ".join(blocks)[:120])

    return ScreeningResult(decision, value, "grounded in capabilities")


def load_capabilities() -> str:
    for p in (CAPABILITIES_FILE, CAPABILITIES_EXAMPLE):
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            continue
    return ""


def resolve_with_screening(question, options, facts, capabilities, llm_fn, audit_fn) -> Choice:
    """Drop-in replacement for resolve_choice at the orchestrator's custom-select callsites.

    For a clean binary Yes/No it routes to the conservative screening classifier (grounded in
    `capabilities`); a YES/NO becomes an `answered` Choice, an ESCALATE becomes `declined`. For
    every other (non-binary) constrained question it delegates to the generic facts-grounded
    resolve_choice unchanged."""
    if is_yesno_screening(options):
        r = classify_screening(question, options, capabilities, llm_fn, audit_fn)
        if r.decision in (ScreeningDecision.YES, ScreeningDecision.NO) and r.value:
            return Choice(question, options, value=r.value, status="answered",
                          reason="screening: " + r.reason)
        return Choice(question, options, status="declined",
                      reason="screening: " + (r.reason or "left for the user"))
    return resolve_choice(question, options, facts, llm_fn, audit_fn)
