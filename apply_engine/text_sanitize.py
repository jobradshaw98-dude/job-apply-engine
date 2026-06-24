"""Shared text sanitizers for LLM-generated answer text.

Lives in its own module so EVERY generator (the fresh-draft path in answer_gen and the
minimal-edit / convergence rewrite path in regen_answer) calls the SAME implementation —
they previously drifted, and the editor-preamble leak shipped through the one path that
had no stripper (regen_answer; caught on JOB-237 Anthropic "Why Anthropic?").
"""
import re

# Editor models frequently disobey "output only the final text" and prepend meta-commentary
# describing their edit ("One word change, everything else verbatim:", "Here is the revised
# answer:", "Revised:") often followed by a horizontal rule. That preamble was shipping inside
# the stored answer. This strips it so only the answer body survives. Conservative: only removes
# a SHORT leading meta block, never body text.
_META_LEAD_RE = re.compile(
    r"^\s*(?:"
    r"(?:here(?:'s| is)\b|revised\b|final\b|updated\b|edited\b|rewrite\b|rewritten\b|"
    r"sure\b|certainly\b|note\b|changes?\b|edit\b|one\s+\w+\s+change\b|no\s+changes?\b|"
    r"i(?:'ve| have)?\s+\w+|below\s+is\b)"
    r"[^\n]{0,120}:\s*)$",
    re.IGNORECASE,
)
_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,}|={3,})\s*$")


def strip_editor_preamble(text: str) -> str:
    """Remove leading editor meta-commentary / horizontal-rule fences from an LLM edit reply.

    Drops a leading run of (a) horizontal-rule lines and (b) short meta lines ending in ':'
    that describe the edit, plus any blank lines between them. Stops at the first real content
    line so answer bodies are never touched. Also trims a trailing HR fence if present.
    """
    if not text:
        return text
    lines = text.split("\n")
    i = 0
    stripped_any = False
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():                       # blank — skip but don't count as content
            i += 1
            continue
        if _HR_RE.match(ln):                     # --- / *** fence
            i += 1
            stripped_any = True
            continue
        if _META_LEAD_RE.match(ln):              # "Revised answer:" style meta line
            i += 1
            stripped_any = True
            continue
        break                                    # first real content line
    if not stripped_any:
        return text.strip()
    body = "\n".join(lines[i:]).strip()
    # trailing fence cleanup
    body_lines = body.split("\n")
    while body_lines and _HR_RE.match(body_lines[-1]):
        body_lines.pop()
    return "\n".join(body_lines).strip() or text.strip()


_EMDASH_RE = re.compile(r"\s*—\s*")


def reduce_emdashes(text: str, limit: int = 1) -> str:
    """Deterministically bring an answer's em-dash count to <= limit by replacing em-dashes with
    commas (the spaces around them collapsed). Em-dashes read as an AI tell and the answer gate
    BLOCKS > 2 of them — but that's a mechanical, content-neutral defect, so a strong answer should
    be auto-fixed here rather than blocked. Pure punctuation swap; never changes wording. Applied to
    final answer text just before the fabrication gate."""
    if not text or text.count("—") <= limit:
        return text
    out = _EMDASH_RE.sub(", ", text)
    out = re.sub(r",\s*,", ", ", out)          # collapse any ",," the swap created
    out = re.sub(r"\s+([.,;:!?])", r"\1", out)  # no space before punctuation
    out = re.sub(r"[ \t]{2,}", " ", out)        # collapse double spaces
    return out.strip()


def has_editor_leak(text: str) -> bool:
    """True if `text` shows the signature of an LLM edit reply whose scaffolding leaked into the
    stored answer: a LEADING meta-commentary line ("Revised:", "One word change:", a self-critique
    that the strip anchors couldn't catch) OR a bare horizontal-rule fence ('---', '***') anywhere.
    A real job-application answer never contains a markdown HR, so a surviving fence is a reliable
    leak tell. Used as a deterministic BLOCK backstop in refresh_audit so a leak is caught at the
    submit gate even if strip_editor_preamble ever misses a variant (defense in depth)."""
    if not text:
        return False
    nonblank = [ln for ln in text.split("\n") if ln.strip()]
    if not nonblank:
        return False
    if _META_LEAD_RE.match(nonblank[0]):
        return True
    return any(_HR_RE.match(ln) for ln in nonblank)
