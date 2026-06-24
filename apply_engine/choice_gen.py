"""Pick a grounded answer to a CONSTRAINED (dropdown / single-select) question.

The dropdown analog of answer_gen. Two safety layers, both injectable for testing:
  llm_fn(prompt) -> str     : the picker (real = Claude). Told to return EXACTLY one of
                              the provided options verbatim, or DECLINE if no option is
                              supported by the FACTS.
  audit_fn(text) -> [str]   : the deterministic fabrication/overstatement gate
                              (real = career/audit_gate.py). Any block kills the pick.

Hard safety property: the returned value is ALWAYS one of the offered options (matched
case/whitespace-insensitively) or "". The model can never invent an option, and a pick
with no factual basis DECLINEs — so judgment-call dropdowns ("how familiar are you with
X") are left for the user, by construction, not by hope.

Status per choice:
  answered  : the model picked an offered option, supported, passed the gate (safe to select)
  declined  : DECLINE, an invented/unmatched option, an empty option list, or an llm error
              (left for the user)
  blocked   : the gate flagged the chosen option text (left for the user)
"""
from dataclasses import dataclass, field
from typing import List

DECLINE = "DECLINE"


@dataclass
class Choice:
    question: str
    options: List[str]
    value: str = ""        # canonical option text to select, or "" if not answered
    status: str = ""
    reason: str = ""


def build_choice_prompt(question: str, options: List[str], facts: str) -> str:
    opts = "\n".join(f"- {o}" for o in options)
    return (
        "You are choosing the applicant's answer to a job-application question that has a\n"
        "FIXED set of options. Use ONLY the FACTS below.\n"
        "HARD RULES:\n"
        "- Return EXACTLY ONE of the OPTIONS, copied verbatim. Nothing else.\n"
        "- Do NOT invent an option, combine options, or add words.\n"
        "- If no option is supported by the FACTS — or the question is a self-assessment /\n"
        "  judgment call with no factual basis — output exactly: DECLINE\n\n"
        f"FACTS:\n{facts}\n\n"
        f"QUESTION: {question}\n\n"
        f"OPTIONS:\n{opts}\n\n"
        "Output only the chosen option text (or DECLINE)."
    )


def _match_option(raw: str, options: List[str]) -> str:
    """Return the canonical option whose text equals `raw` (case/whitespace-insensitive),
    else "". Never a partial/substring match — the pick must be an exact offered option."""
    norm = " ".join(raw.lower().split())
    for o in options:
        if " ".join(o.lower().split()) == norm:
            return o
    return ""


def resolve_choice(question: str, options: List[str], facts: str,
                   llm_fn, audit_fn) -> Choice:
    if not options:
        return Choice(question, options, status="declined",
                      reason="no options offered")
    try:
        raw = (llm_fn(build_choice_prompt(question, options, facts)) or "").strip()
    except Exception as e:  # noqa: BLE001 — never auto-select on a drafter error
        return Choice(question, options, status="declined", reason=f"llm error: {e!r}")

    if not raw or raw.upper().startswith(DECLINE):
        return Choice(question, options, status="declined",
                      reason="not supported by facts")

    matched = _match_option(raw, options)
    if not matched:
        # the model returned text that is not one of the offered options — refuse it.
        return Choice(question, options, status="declined",
                      reason=f"model did not return an offered option (said {raw[:60]!r})")

    try:
        blocks = audit_fn(matched) or []
    except Exception as e:  # noqa: BLE001 — gate down -> fail safe (block, don't select)
        blocks = [f"audit error: {e!r}"]
    if blocks:
        return Choice(question, options, value=matched, status="blocked",
                      reason="; ".join(blocks)[:200])

    return Choice(question, options, value=matched, status="answered")


def make_resolver(facts: str, llm_fn, audit_fn):
    """Bind FACTS + the drafter + the gate into a `(question, options) -> Choice` callable
    for adapters to use on custom dropdown questions. Returns None when there is no drafter
    (`llm_fn` is None) — the caller then escalates every custom question, the safe default."""
    if llm_fn is None:
        return None

    def _resolve(question: str, options: List[str]) -> Choice:
        return resolve_choice(question, options, facts, llm_fn, audit_fn)

    return _resolve


# ---------------------------------------------------------------------------
# Multi-select analog: "check all that apply" checkbox-groups.
# ---------------------------------------------------------------------------

@dataclass
class MultiChoice:
    question: str
    options: List[str]
    values: List[str] = field(default_factory=list)  # canonical options to check
    status: str = ""
    reason: str = ""


def build_multi_choice_prompt(question: str, options: List[str], facts: str) -> str:
    opts = "\n".join(f"- {o}" for o in options)
    return (
        "You are choosing the applicant's answer to a job-application question that lets\n"
        "you select MULTIPLE options ('check all that apply'). Use ONLY the FACTS below.\n"
        "HARD RULES:\n"
        "- Return the SUBSET of the OPTIONS that the FACTS clearly support — each on its own\n"
        "  line, copied verbatim. Nothing else.\n"
        "- Do NOT invent an option, combine options, or add words.\n"
        "- Include an option ONLY when the FACTS support it. When in doubt, leave it out.\n"
        "- If NO option is supported by the FACTS, output exactly: DECLINE\n\n"
        f"FACTS:\n{facts}\n\n"
        f"QUESTION: {question}\n\n"
        f"OPTIONS:\n{opts}\n\n"
        "Output only the chosen option lines (or DECLINE)."
    )


def resolve_multi_choice(question: str, options: List[str], facts: str,
                         llm_fn, audit_fn) -> MultiChoice:
    """Pick the FACTS-supported SUBSET of a checkbox-group. Same safety contract as
    resolve_choice: every returned value is an offered option (exact, case/whitespace-
    insensitive match — never substring); each chosen option is run through the gate, and a
    block on ANY one fails the whole pick (check nothing); no basis / error -> declined."""
    if not options:
        return MultiChoice(question, options, status="declined",
                           reason="no options offered")
    try:
        raw = (llm_fn(build_multi_choice_prompt(question, options, facts)) or "").strip()
    except Exception as e:  # noqa: BLE001 — never auto-check on a drafter error
        return MultiChoice(question, options, status="declined",
                           reason=f"llm error: {e!r}")

    if not raw or raw.upper().startswith(DECLINE):
        return MultiChoice(question, options, status="declined",
                           reason="not supported by facts")

    # Match each returned line to an offered option; silently drop anything not offered.
    picked: List[str] = []
    for line in raw.splitlines():
        line = line.lstrip("-* \t").strip()
        if not line:
            continue
        m = _match_option(line, options)
        if m and m not in picked:
            picked.append(m)

    if not picked:
        # everything the model returned was invented / unmatched -> escalate, check nothing.
        return MultiChoice(question, options, status="declined",
                           reason=f"model returned no offered option (said {raw[:60]!r})")

    # Preserve the offered-option order for deterministic, stable output.
    picked = [o for o in options if o in picked]

    try:
        blocks = []
        for opt in picked:
            blocks.extend(audit_fn(opt) or [])
    except Exception as e:  # noqa: BLE001 — gate down -> fail safe (block, check nothing)
        blocks = [f"audit error: {e!r}"]
    if blocks:
        return MultiChoice(question, options, status="blocked",
                           reason="; ".join(blocks)[:200])

    return MultiChoice(question, options, values=picked, status="answered")


def make_multi_resolver(facts: str, llm_fn, audit_fn):
    """Bind FACTS + drafter + gate into a `(question, options) -> MultiChoice` callable for
    checkbox-groups. None when there is no drafter (escalate every group — safe default)."""
    if llm_fn is None:
        return None

    def _resolve(question: str, options: List[str]) -> MultiChoice:
        return resolve_multi_choice(question, options, facts, llm_fn, audit_fn)

    return _resolve
