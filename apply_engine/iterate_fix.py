# -*- coding: utf-8 -*-
"""Iterate-to-clean retry helpers shared by regen_answer / regen_content.

The convergence loop's ENGINE-OWN fix path (converge.apply_own_fix) needs a regen that, when
its own rewrite STILL fails the deterministic accuracy gate, RE-PROMPTS with the gate's specific
complaint about the previous attempt PLUS the supported ledger facts and tries AGAIN — up to K
attempts — instead of writing a still-blocked draft and letting the outer loop see "no shrink ->
exhausted". This module holds the two pure pieces of that mechanism:

  * `feedback_clause(instruction, attempt_text, blocks, findings, ledger_facts)` — builds the
    APPENDED clause that names the PREVIOUS attempt's offending text + the gate/ledger complaint
    and re-grounds in the ledger. Threaded onto the next attempt's instruction. Converge by
    REMOVAL: the clause tells the model to REMOVE/REWORD the unsupported claim, never to invent
    support for it.

  * `classify_residual(...)` — after K exhausted attempts, classify the surviving finding as
    `human_only` (the fix needs a fact only Sam has — the ledger can neither confirm nor deny
    the claimed experience) vs `unsupportable` (the premise can't be grounded at all — the content
    should be reworded or dropped). This lets the convergence loop surface the RIGHT blocker
    instead of a blunt "convergence stalled".

Both are deterministic-shaped (a tiny LLM call only for classification, on the SAME `claude -p`
subscription path the regen functions already use — never the metered API). Pure/injectable so
the iterate loop is testable without a live claude -p.
"""
import json

# Default inner-attempt cap for the engine-own iterate path. K≈3 (brief). Sam's own
# single-pass edits use max_attempts=1 (this module is never engaged for those).
DEFAULT_MAX_ATTEMPTS = 3


def _norm_ws(s):
    return " ".join((s or "").split())


def summarize_blocks(blocks, findings):
    """A compact, human-readable description of why the previous attempt failed, drawn from BOTH
    sources the regen has:
      * `blocks` — the deterministic gate's notes (list of strings) on the attempt text.
      * `findings` — the ledger self-audit's structured findings (dicts with offending_text/issue/fix).
    Returns a single string naming the specific complaints. Empty when neither source has anything."""
    parts = []
    for b in (blocks or []):
        b = _norm_ws(str(b))
        if b:
            parts.append(b)
    for f in (findings or []):
        if not isinstance(f, dict):
            continue
        off = _norm_ws(f.get("offending_text", ""))
        issue = _norm_ws(f.get("issue", ""))
        fix = _norm_ws(f.get("fix", ""))
        bits = []
        if off:
            bits.append(f'"{off}"')
        if issue:
            bits.append(issue)
        if fix:
            bits.append(f"(suggested fix: {fix})")
        if bits:
            parts.append(" — ".join(bits))
    return "; ".join(parts)


def feedback_clause(prev_attempt_text, blocks, findings, ledger_facts=""):
    """The clause APPENDED to the instruction on attempt N>1. It names the PREVIOUS attempt's
    specific failure (the gate notes + the ledger findings' offending_text/issue) and tells the
    model to REMOVE or REWORD the unsupported text — re-grounding in the ledger facts — NEVER to
    invent support. Returns "" when there is nothing specific to thread (the caller then just
    retries with the base instruction).

    `ledger_facts` is the SUPPORTED-claims text relevant to this question/element so the rewrite
    has the grounding in front of it (per feedback_ledger_truncation_false_blocks — give the model
    the oracle, don't make it guess)."""
    summary = summarize_blocks(blocks, findings)
    if not summary and not (prev_attempt_text or "").strip():
        return ""
    clause = (
        "\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED BY THE ACCURACY GATE. Do not repeat it.\n"
        "PREVIOUS ATTEMPT:\n" + (prev_attempt_text or "").strip() + "\n"
    )
    if summary:
        clause += (
            "WHY IT WAS REJECTED (fix exactly this — REMOVE or REWORD the offending claim; do NOT "
            "invent evidence to support it):\n" + summary + "\n"
        )
    if (ledger_facts or "").strip():
        clause += (
            "ONLY these supported facts may back any claim — re-ground the rewrite in them and drop "
            "anything they do not support:\n" + ledger_facts.strip()[:6000] + "\n"
        )
    clause += (
        "Rewrite so the gate passes: keep every supported sentence, and replace the rejected claim "
        "with a supported one or remove it. Stay strictly within the FACTS."
    )
    return clause


def length_feedback_clause(prev_attempt_text, current_words, min_words, max_words,
                           ledger_facts=""):
    """The clause appended on a retry when the previous attempt PASSED the fabrication/disclosure
    gate but was still out of the stated word RANGE (too short / too long). Names the measured count
    + the target band and tells the model to reach it by ADDING SUPPORTED detail (under) or CUTTING
    redundancy (over) — never by padding/inventing. Converge-by-grounding, same contract as
    feedback_clause. Returns '' when there is nothing to thread."""
    if not (prev_attempt_text or "").strip():
        return ""
    if min_words is not None and current_words < min_words:
        target = (f"at least {min_words}"
                  + (f" and at most {max_words}" if max_words is not None else "") + " words")
        ask = ("LENGTHEN it to the required range by adding SPECIFIC, SUPPORTED detail grounded in "
               "the facts below — concrete examples, outcomes, and context that are already true. Do "
               "NOT pad with filler, restate the question, or invent any experience.")
    elif max_words is not None and current_words > max_words:
        target = (f"at most {max_words}"
                  + (f" and at least {min_words}" if min_words is not None else "") + " words")
        ask = ("TIGHTEN it to the required range: cut redundancy and weaker points, keep the "
               "strongest grounded ones. Do NOT drop a supported claim just to fit — condense.")
    else:
        return ""
    clause = (
        "\n\nYOUR PREVIOUS ATTEMPT WAS THE WRONG LENGTH. It was "
        + str(int(current_words)) + " words; the form requires " + target + ".\n"
        "PREVIOUS ATTEMPT:\n" + (prev_attempt_text or "").strip() + "\n"
        + ask + "\n"
    )
    if (ledger_facts or "").strip():
        clause += (
            "ONLY these supported facts may back any claim — draw the added/kept detail from them and "
            "never assert anything they do not support:\n" + ledger_facts.strip()[:6000] + "\n"
        )
    clause += "Stay strictly within the FACTS and the honesty rules above."
    return clause


# ---- residual classification (after K attempts still blocked) ------------------------------

# A finding the loop could not clear by removal. Two terminal classes:
#   human_only    — the fix needs a fact only Sam has (the ledger can neither confirm nor deny
#                   the claimed experience). Surface as an ANSWERABLE blocker — ask Sam.
#   unsupportable — the premise can't be grounded in the ledger AT ALL; the content should be
#                   reworded or dropped (a "rewrite or drop this content" blocker).
HUMAN_ONLY = "human_only"
UNSUPPORTABLE = "unsupportable"
# A G2 length fix that could not reach the stated word range with SUPPORTED facts after K attempts.
# This is NOT human_only (a length problem is never "needs Sam to confirm a fact") — it means the
# answer can't be grown/shrunk into range without inventing or padding, so it's a rewrite-or-drop /
# review case. Surfaced as its own class so the blocker says "couldn't reach the length", not
# "needs your call".
LENGTH_UNMET = "length_unmet"


def _heuristic_class(finding):
    """Deterministic fallback classification when no LLM is available (or it errors). A finding
    whose issue/text talks about an UNVERIFIABLE/UNCONFIRMED experience or a fact only Sam can
    confirm -> human_only; otherwise default to unsupportable (the safe 'reword or drop' class —
    we never silently pass it)."""
    if not isinstance(finding, dict):
        return UNSUPPORTABLE
    blob = " ".join(str(finding.get(k, "")) for k in ("issue", "fix", "offending_text")).lower()
    human_markers = (
        "only sam", "sam can confirm", "sam to confirm", "needs sam",
        "cannot confirm", "can't confirm", "unverifiable", "unconfirmed", "ask sam",
        "his actual experience", "whether he", "did he", "does he actually",
    )
    if any(m in blob for m in human_markers):
        return HUMAN_ONLY
    return UNSUPPORTABLE


def classify_residual(finding, ledger_facts="", llm=None):
    """Classify a SURVIVING (still-blocked after K attempts) finding as human_only vs unsupportable.

    Uses a tiny `claude -p` call when `llm` is provided (the SAME subscription path the regen
    already built — never the metered API); falls back to `_heuristic_class` on no-LLM/parse-fail.
    Returns one of HUMAN_ONLY / UNSUPPORTABLE. Never raises."""
    if not isinstance(finding, dict):
        return UNSUPPORTABLE
    if llm is None:
        return _heuristic_class(finding)
    try:
        off = _norm_ws(finding.get("offending_text", ""))
        issue = _norm_ws(finding.get("issue", ""))
        prompt = (
            "You are triaging ONE accuracy-gate finding on Sam Rivera's job-application content "
            "that an automated rewrite loop could not fix by removing/rewording the claim. Decide which "
            "of TWO categories it belongs to, using the VETTED CLAIMS LEDGER as the only ground truth.\n\n"
            "human_only  : the claim might be TRUE but the ledger can neither confirm nor deny it — it "
            "names an experience/fact only Sam can verify. The right action is to ASK SAM.\n"
            "unsupportable: the claim's premise is not grounded in the ledger at all and asking Sam "
            "would not help — the content should be reworded or dropped.\n\n"
            f"VETTED CLAIMS LEDGER:\n{(ledger_facts or '')[:14000]}\n\n"
            f"FINDING offending_text: {off}\nFINDING issue: {issue}\n\n"
            'Return ONLY a JSON object, no prose: {"class":"human_only"|"unsupportable","why":"one short reason"}.'
        )
        raw = (llm(prompt) or "").strip()
        s, e = raw.find("{"), raw.rfind("}")
        if s != -1 and e != -1:
            obj = json.loads(raw[s:e + 1])
            cls = (obj.get("class") or "").strip().lower()
            if cls in (HUMAN_ONLY, UNSUPPORTABLE):
                return cls
    except Exception:  # noqa: BLE001 — classification is best-effort; fall back deterministically
        pass
    return _heuristic_class(finding)
