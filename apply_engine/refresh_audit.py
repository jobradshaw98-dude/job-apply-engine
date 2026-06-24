# -*- coding: utf-8 -*-
"""Re-run the fabrication/accuracy audit over a staged application's CURRENT answers and
overwrite the stored verdict.

WHY THIS EXISTS
The career-draft-auditor stamps `record["audit"] = {verdict, gate_blocks, findings, summary}`
at staging time. `finish.can_submit` refuses to submit while that verdict is "BLOCKED". But the
verdict is FROZEN at staging — once the user edits/fixes the offending answers, the stored verdict
is stale and keeps Submit locked even though the findings are resolved. This module re-runs the
SAME audit over the answers AS THEY STAND NOW and writes a fresh verdict, so a resolved BLOCK
flips to PASS (and an unresolved one stays BLOCKED).

SCOPE MIRRORS THE ORIGINAL EXACTLY
The original Oura (JOB-131) audit was answer-only (every finding doc == "essay_answer",
gate_blocks == 0): the resume/cover were not in scope. So this re-audits the CURRENT custom_qs
answers and nothing else, unless the stored verdict carried resume/cover findings — in which case
those docs were in the original scope and we re-audit them too. We never widen scope beyond what
the original verdict covered.

HOW IT AUDITS (same two layers the engine + auditor use)
  1. Deterministic gate (career/audit_gate.py via apply_engine.llm.make_audit_fn) on each answer
     — every block is a confirmed BLOCK finding, counted into gate_blocks.
  2. LLM judgment lens (claude -p on the subscription, via apply_engine.llm.make_claude_llm)
     traces every claim against the vetted claims ledger and assigns a per-finding SEVERITY:
       * BLOCK = the factual-fabrication class ONLY (a tool/number/role not in the ledger, a
         wrong-employer attribution, an invented metric, a NEVER-CLAIM-list entry).
       * FLAG  = everything else (overclaim-adjacent phrasing, tone/voice, level-of-detail,
         alignment opinions). When uncertain, the judge chooses FLAG.
     This is the same ledger-tracing the career-draft-auditor and regen_answer's self-audit do.

TWO-SEVERITY VERDICT (2026-06-05 policy change)
verdict == "BLOCKED" iff gate_blocks > 0 OR any finding has severity BLOCK; else "PASS".
The findings list may be NON-EMPTY on a PASS — FLAG findings ride along visibly so the user still
sees the style notes, but they no longer lock Submit. Previously every finding was severity BLOCK
and any finding ⇒ BLOCKED, which made PASS nearly unreachable: an LLM critic always finds
something, so style opinions locked Submit with the same force as fabrications.

WRITES
Atomically via staged_manifest.attach_audit (the engine owns all manifest writes; the server
only launches this detached). Adds `refreshed_at` (local ISO) to the verdict.

    python -m apply_engine.refresh_audit JOB-131

Degrades safely: if the LLM can't run (no Claude CLI), the judgment lens is skipped and only the
deterministic gate runs — we never fall back to the metered API and never crash a review step.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from . import config
from .disclosure_guard import detect_immigration_disclosure
from .draft_audit import drafts_for_audit
from .staged_manifest import apply_recompute, attach_audit, attach_quality_audit
from .text_sanitize import has_editor_leak


# ---------------------------------------------------------------------------------------------
# THE SINGLE SOURCE OF TRUTH for the LLM judge's BLOCK criteria, shared VERBATIM by BOTH judge
# prompts (_judge_answer for answers, audit_content_text for resume/cover elements).
#
# WHY A SHARED CONSTANT (do not inline this into either prompt again):
# The recurring bug class (feedback_apply_judge_ledger_format_rules — bugs #2 and #5) was a
# ledger-grounded fact rendered in a ledger-FORBIDDEN FORM shipping as "ready" because the judge's
# BLOCK criteria didn't enforce the ledger's PROSE rules. Each instance was fixed by hand by
# editing BOTH prompt literals — and the ORIGINAL failure was a fix that landed in one prompt but
# not the other (they drifted). Consolidating the BLOCK definition into one constant referenced by
# both prompts makes that drift structurally impossible: there is exactly one place to add a rule.
#
# The deterministic forbidden-phrase gate (career/audit_gate.py) already catches the *mechanical*
# string violations on resume/cover HTML ($198M, "rare combination", "production multi-agent
# platform", "at platform scale", "clinical fluency", Codex/Claude-Code section conflation). Those
# are NOT duplicated here verbatim as regexes — but the LLM judge still needs the SEMANTIC versions
# because (a) free-text ANSWERS never pass through the HTML section gate, and (b) the same idea can
# be expressed in words the regex doesn't match. So this block restates the prose CONSTRAINTS the
# regex can't see (timeline framing, employer-tool attribution by meaning, impact-as-percentage,
# thesis framing, puffery, coding-fluency) as judge instructions.
#
# Any prose rule that CANNOT be cleanly expressed as a judge instruction belongs in audit_gate.py
# as a deterministic check instead — note it, don't weaken this block to cover it.
# ---------------------------------------------------------------------------------------------
_LEDGER_PROSE_PREFIX = (
    "  BLOCK = the factual-fabrication class AND ledger-forbidden RENDERINGS of true facts. "
    "Mark a finding BLOCK when the text does ANY of the following:\n"
    "  (a) FABRICATION/MISATTRIBUTION: names a tool/technology not in the ledger or attributes one "
    "to the wrong employer; states an invented or unsupported number/metric/outcome; attributes "
    "work or a role incorrectly; or asserts anything on the ledger's NEVER CLAIM list.\n")
# Impact-form (people/time-count rendering) is a BLOCK for RESUME/COVER bullets (the ledger wants a
# clean scannable percentage there) but is ALLOWED in free-text ESSAY answers (a concrete '10-person
# review' is vivid and credible). So it lives in its own clause, included only for resume/cover.
_LEDGER_PROSE_IMPACT_FORM = (
    "  (b) IMPACT FORM: expresses a Meridian agent's impact as a count of people/engineers, hours, "
    "or meeting durations when the ledger requires it be stated as a PERCENTAGE (e.g. '10+ "
    "engineers, two-hour review' must read '~90% reduction'). A true fact in this forbidden "
    "rendering is still a BLOCK, not a FLAG.\n")
_LEDGER_PROSE_REST = (
    "  (c) THESIS FRAMING: frames the MASc/graduate thesis as materials-science, material-modeling, "
    "or biomechanics work. The ledger LOCKS it as an automated design-optimization framework (an "
    "ANSYS + OptiSLang metamodel/Pareto pipeline searching surface-texture geometry against "
    "contact-mechanics FEA); the UHMWPE material model was a FIXED input, never the subject. That "
    "mischaracterization is a fabrication-class error he would have to contradict in an interview.\n"
    "  (d) ARIA TIMELINE: states or implies ARIA has been running longer than it has. ARIA has "
    "been running daily only SINCE EARLY 2026 (a few months). Phrasings like 'over a year', 'for "
    "the past year', 'two years', or any multi-year duration for ARIA are a BLOCK — use 'since "
    "early 2026' / 'running daily since early 2026'.\n"
    "  (e) EMPLOYER-TOOL ATTRIBUTION: conflates the two toolchains. CODEX is the Meridian "
    "toolchain; CLAUDE CODE is the ARIA toolchain. Attributing Claude Code to Meridian work, or "
    "Codex to ARIA, is a wrong-attribution BLOCK (a skills line may list both tools career-wide, "
    "but a specific Meridian accomplishment must credit Codex and an ARIA one Claude Code).\n"
    "  (f) NEVER-CLAIM PUFFERY: 'production multi-agent platform' / 'production agentic systems' / "
    "'at platform scale' / 'deployed into real workflows' (ARIA serves ONE user), or "
    "self-aggrandizing 'rare / unusual / uncommon combination', 'first-class', 'world-class' "
    "modifiers asserted without evidence.\n"
    "  (g) CODING-LANGUAGE FLUENCY: claims hand-coding PROFICIENCY in Python or MATLAB ('fluent in "
    "Python', 'proficient in MATLAB', 'comfortable writing Python day to day'). The applicant no longer "
    "hand-codes either — he BUILDS automation by ORCHESTRATING AI agents (Claude Code, Codex). "
    "Python may be named only as the language his AI-built tools are implemented in; MATLAB must "
    "not appear as a personal coding skill. A coding-fluency claim is a BLOCK.\n"
)

# RESUME/COVER judge: includes impact-form (percentage-only) — keeps bullets clean and scannable.
LEDGER_PROSE_BLOCK_RULES = _LEDGER_PROSE_PREFIX + _LEDGER_PROSE_IMPACT_FORM + _LEDGER_PROSE_REST
# ESSAY-ANSWER judge: drops impact-form — a concrete people-count is allowed in free-text answers.
LEDGER_PROSE_BLOCK_RULES_ANSWERS = _LEDGER_PROSE_PREFIX + _LEDGER_PROSE_REST


def _local_iso() -> str:
    """Local ISO timestamp WITH offset — matches regen_answer's history rows."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _stamp_at_or_after(floor_ts: Optional[str]) -> str:
    """Return a refreshed_at stamp that is max(now, floor_ts) (BUG B, JOB-242).

    The self-heal stamps the audit MID content-edit run, but the content_edit row it heals is
    finalized with a LATER ts (the LLM window + the terminal "<doc>.doc" row both push the row's
    ts past the heal's now). The dashboard then reads the edit as 'newer than the audit' and
    locks Submit on a permanent FALSE staleness. Lifting the stamp to >= the controlling edit ts
    makes a SUCCESSFUL self-heal read as not-stale. The floor only ever RAISES the stamp
    (max), never lowers it — a heal still genuinely refreshes the verdict, and an edit that NEVER
    re-audited keeps its old (pre-edit) stamp and still reads stale, so wedge recovery stays live.

    Parse-robust: if floor_ts is missing/unparseable, fall back to plain now."""
    now = _local_iso()
    if not floor_ts:
        return now
    try:
        if datetime.fromisoformat(floor_ts) > datetime.fromisoformat(now):
            return floor_ts
    except Exception:
        return now
    return now


def _load_record(manifest_path: Path, job_id: str) -> Optional[dict]:
    """Read the staged record for job_id, or None. Best-effort: missing/corrupt -> None."""
    try:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    for entry in data:
        if isinstance(entry, dict) and entry.get("job_id") == job_id:
            return entry
    return None


def _original_docs(record: dict) -> set:
    """The set of `doc` types the ORIGINAL verdict covered. The refresh must not widen scope
    past what was originally audited (the Oura verdict was essay_answer-only). If there is no
    prior audit (or no findings), default to essay_answer — answers are always in scope."""
    docs = set()
    for f in ((record.get("audit") or {}).get("findings") or []):
        if isinstance(f, dict) and f.get("doc"):
            docs.add(f["doc"])
    return docs or {"essay_answer"}


def _ledger_text() -> str:
    """The vetted claims ledger — the fabrication oracle. Empty string if unreadable."""
    try:
        return (config.PKG_DIR.parent / "claims_ledger.md").read_text(encoding="utf-8")[:20000]
    except Exception:
        return ""


def _normalize_severity(raw_sev: str) -> str:
    """Defensive severity parse. The PROMPT carries the burden of assigning severity; this is
    only a safety net for a missing/garbled value. Per the uncertainty rule, anything that is
    not an explicit, recognized BLOCK defaults to FLAG (the human reviews everything before
    submit anyway). No clever issue-text heuristics — keep it simple."""
    return "BLOCK" if (raw_sev or "").strip().upper() == "BLOCK" else "FLAG"


def _judge_answer(llm: Callable[[str], str], question: str, answer: str, ledger: str) -> List[dict]:
    """Trace every claim in `answer` against the ledger; return findings, each with a per-finding
    SEVERITY (BLOCK = factual-fabrication class only; FLAG = everything else). Mirrors
    regen_answer's self-audit prompt + the auditor's fabrication lens. PURE-ish: only calls the
    injected llm.

    A real LLM-call failure RAISES (the caller flips judge_degraded so judge_ran becomes False —
    a degraded judge must not present as a completed accuracy review). An empty/garbled RESPONSE
    is NOT a degradation: the judge ran and simply found nothing parseable, so we return [] —
    the deterministic gate remains the floor and the judgment lens contributed no findings."""
    raw = (llm(
            "You are an honesty auditor for the user Rivera's job-application answers. Below is his "
            "VETTED CLAIMS LEDGER (the complete set of claims he is allowed to make), the QUESTION "
            "he was asked, and his ANSWER. Identify every claim in the ANSWER — any number, metric, "
            "percentage, scope, tool, employer, outcome, or stated interest/affinity — that is NOT "
            "supported by the ledger or is overstated beyond its stated bounds, OR that claims a "
            "match to something the question implies he does not actually have.\n\n"
            "For EACH finding, assign a SEVERITY of exactly \"BLOCK\" or \"FLAG\" using these "
            "criteria:\n"
            + LEDGER_PROSE_BLOCK_RULES_ANSWERS +   # essays: people-count framing allowed
            "  FLAG  = everything else: overclaim-adjacent phrasing, tone, voice, "
            "\"telling the company its own value prop\"-class patterns, level-of-detail judgments, "
            "and alignment opinions.\n"
            "When you are uncertain between the two, choose FLAG (the human reviews everything "
            "before submit anyway).\n\n"
            f"VETTED CLAIMS LEDGER:\n{ledger}\n\nQUESTION:\n{question}\n\nANSWER:\n{answer}\n\n"
            'Return ONLY a JSON array, no prose or code fence: '
            '[{"offending_text":"exact quote","issue":"why unsupported","fix":"how to correct",'
            '"severity":"BLOCK or FLAG"}]. '
            "Return [] if every claim is supported."
        ) or "").strip()
    s, e = raw.find("["), raw.rfind("]")
    if s == -1 or e == -1 or e < s:
        return []
    try:
        parsed = json.loads(raw[s:e + 1])
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    out = []
    for x in parsed:
        if not isinstance(x, dict):
            continue
        out.append({
            "doc": "essay_answer",
            "lens": "fabrication",
            "question": question,
            "severity": _normalize_severity(x.get("severity", "")),
            "offending_text": str(x.get("offending_text", "")),
            "issue": str(x.get("issue", "")),
            "fix": str(x.get("fix", "")),
        })
    return out


def audit_answers(drafts: List[dict], gate_fn: Optional[Callable[[str], list]],
                  llm: Optional[Callable[[str], str]], ledger: str) -> dict:
    """PURE (given injected gate_fn/llm). Re-audit a list of {question, answer, kind} drafts.

    Returns {verdict, gate_blocks, findings, judge_ran} where:
      * gate_blocks = total deterministic-gate blocks across all answers (all severity BLOCK),
      * findings    = one BLOCK finding per gate block (lens="gate") + LLM judgment findings,
                      each carrying its own severity ("BLOCK" or "FLAG"),
      * verdict     = "BLOCKED" iff gate_blocks > 0 OR any finding has severity BLOCK OR the
                      LLM judgment lens did not run (judge_ran False, the fail-closed degraded
                      stamp, 2026-06-11); else "PASS" (FLAG-only findings ride along visibly
                      but do not lock Submit). verdict "PASS" therefore IMPLIES judge_ran True.
      * judge_ran   = whether the LLM judgment LENS WAS AVAILABLE AND NOTHING DEGRADED IT. It is
                      True when the judge could be constructed (llm + ledger present) and no
                      per-answer judge call raised — INDEPENDENT of how many answers existed.
                      So: llm/ledger unavailable -> False; available + 0 answers -> True (the
                      review ran, there was simply nothing to flag — vacuously complete);
                      available + N answers all judged -> True (even if a call returned []);
                      available but a judge call RAISED -> False (a real degradation).
                      A PASS with judge_ran=False is "deterministic gate only" — NOT fully
                      verified, and finish.can_submit refuses it the same as a missing audit.
    gate_fn/llm may be None (degrade): a None gate_fn contributes no gate blocks; a None llm
    skips the judgment lens. Never raises on a single bad answer."""
    findings: List[dict] = []
    gate_blocks = 0
    # The judge lens is AVAILABLE iff we can construct it (llm + oracle present). Availability is
    # the baseline for judge_ran; a per-answer call that RAISES flips judge_degraded and tears it
    # back down. Zero answers with an available judge is vacuously complete -> judge_ran stays True.
    judge_available = llm is not None and bool(ledger)
    judge_degraded = False
    for d in drafts:
        question = d.get("question", "") or ""
        answer = d.get("answer", "") or ""
        if not answer:
            continue
        # 1. deterministic gate
        if gate_fn is not None:
            try:
                blocks = gate_fn(answer) or []
            except Exception as ex:  # noqa: BLE001 — fail safe to a block, never crash
                blocks = [f"audit error: {ex!r}"]
            for note in blocks:
                gate_blocks += 1
                findings.append({
                    "doc": "essay_answer",
                    "lens": "gate",
                    "question": question,
                    "severity": "BLOCK",
                    "offending_text": answer[:200],
                    "issue": str(note),
                    "fix": "Remove or rephrase the flagged claim.",
                })
        # 2. LLM judgment lens (ledger tracing). A real call failure (claude -p died mid-review)
        # is a DEGRADATION: catch it, flip judge_degraded so judge_ran goes False, and keep going
        # (the deterministic gate is still the floor). An empty/garbled RESPONSE is NOT a
        # degradation — _judge_answer returns [] for that and the lens still counts as having run.
        if judge_available:
            try:
                findings.extend(_judge_answer(llm, question, answer, ledger))
            except Exception:  # noqa: BLE001 — a raised judge call degrades the lens, never crashes
                judge_degraded = True
        # 3. DISCLOSURE lens (deterministic, no LLM). A ledger-grounded answer can be TRUTHFUL but
        # still volunteer the applicant's visa/citizenship/sponsorship/GC status — the fabrication judge
        # passes it (it's true), but the work-auth policy forbids it in free-text content. Each hit
        # is a BLOCK finding so the verdict goes BLOCKED, the converge loop drives on it, and
        # verify_ready refuses the card until the disclosure is removed.
        for dh in detect_immigration_disclosure(answer):
            findings.append({
                "doc": "essay_answer",
                "lens": "disclosure",
                "question": question,
                "severity": "BLOCK",
                "category": dh.get("category", ""),
                "offending_text": dh.get("offending_text", ""),
                "issue": dh.get("issue", ""),
                "fix": dh.get("fix", ""),
            })
        # 4. LEAK lens (deterministic, no LLM). An LLM edit reply's scaffolding — a leading
        # meta-commentary line ("One word change, everything else verbatim:") or a bare '---' fence
        # — must never ship inside the answer value. The fabrication judge passes it (it invents no
        # fact), so without this backstop a leaked preamble reads as PASS and submits (JOB-237). The
        # generators strip it at write time; this catches any variant that slips the stripper.
        if has_editor_leak(answer):
            findings.append({
                "doc": "essay_answer",
                "lens": "leak",
                "question": question,
                "severity": "BLOCK",
                "offending_text": answer[:200],
                "issue": ("editor meta-commentary or a horizontal-rule fence leaked into the answer "
                          "text — this is the LLM's edit scaffolding, not the answer."),
                "fix": "Remove the leading preamble / '---' fence; keep only the answer body.",
            })

    block_findings = sum(1 for f in findings if (f.get("severity", "") or "").upper() == "BLOCK")
    flag_findings = len(findings) - block_findings
    judge_ran = judge_available and not judge_degraded
    # FAIL CLOSED on the verdict itself (2026-06-11): a degraded/unavailable judge must never
    # stamp a PASS-shaped verdict. judge_ran=False already blocks finish.can_submit, but a
    # consumer that reads the verdict without judge_ran would treat the stamp as submittable,
    # so the verdict goes BLOCKED too whenever the LLM lens did not run.
    verdict = "BLOCKED" if (gate_blocks > 0 or block_findings > 0 or not judge_ran) else "PASS"
    return {"verdict": verdict, "gate_blocks": gate_blocks, "findings": findings,
            "block_findings": block_findings, "flag_findings": flag_findings,
            "judge_ran": judge_ran}


def audit_content_text(text: str, element: str, gate_fn: Optional[Callable[[str], list]],
                       llm: Optional[Callable[[str], str]], ledger: str) -> List[dict]:
    """PURE (given injected gate_fn/llm). Trace the claims in ONE just-edited resume/cover element
    against the ledger and return findings, each with its own SEVERITY (BLOCK = factual-fabrication
    class; FLAG = everything else). Mirrors _judge_answer's prompt + the deterministic gate, but for
    a resume/cover element rather than an answer. Used by refresh_after_content_edit to make a
    content edit BLOCK-capable (the FLAG-only self_audit in regen_content was advisory).

    `element` is the selector (e.g. "current_bullets.0", "para.2") — its prefix decides the `doc`
    label ("cover" for para.N, else "resume") so the merged verdict names where the issue lives.
    A None gate_fn contributes no gate blocks; a None llm skips the judgment lens. Never raises on
    a single bad element (the caller must keep a content edit from crashing the audit)."""
    text = (text or "").strip()
    if not text:
        return []
    doc = "cover" if str(element).split(".")[0] == "para" else "resume"
    findings: List[dict] = []
    # DISCLOSURE lens (deterministic): a resume/cover edit must never volunteer immigration/work-
    # auth status either. Same BLOCK-finding shape as the answer path so the merged verdict goes
    # BLOCKED and verify_ready refuses the card until the disclosure sentence is removed.
    for dh in detect_immigration_disclosure(text):
        findings.append({
            "doc": doc, "lens": "disclosure", "element": element, "severity": "BLOCK",
            "category": dh.get("category", ""),
            "offending_text": dh.get("offending_text", ""),
            "issue": dh.get("issue", ""), "fix": dh.get("fix", ""),
        })
    if gate_fn is not None:
        try:
            blocks = gate_fn(text) or []
        except Exception as ex:  # noqa: BLE001 — fail safe to a block, never crash
            blocks = [f"audit error: {ex!r}"]
        for note in blocks:
            findings.append({
                "doc": doc, "lens": "gate", "element": element, "severity": "BLOCK",
                "offending_text": text[:200], "issue": str(note),
                "fix": "Remove or rephrase the flagged claim.",
            })
    if llm is not None and ledger:
        # Count-as-impact ("10-person, two-hour review") is BLOCK on a RESUME bullet (percentage-only)
        # but ALLOWED in cover prose — same as essay answers (2026-06-21). So the cover judge
        # uses the ANSWERS rule string, matching make_audit_fn's deterministic gate; resume stays strict.
        prose_rules = LEDGER_PROSE_BLOCK_RULES_ANSWERS if doc == "cover" else LEDGER_PROSE_BLOCK_RULES
        try:
            raw = (llm(
                "You are an honesty auditor for the user Rivera's job-application documents. Below "
                "is his VETTED CLAIMS LEDGER (the complete set of claims he is allowed to make) and "
                "a just-edited resume/cover ELEMENT. Identify every claim in the ELEMENT — any "
                "number, metric, percentage, scope, tool, employer, outcome, or stated interest — "
                "that is NOT supported by the ledger or is overstated beyond its stated bounds.\n\n"
                "For EACH finding, assign a SEVERITY of exactly \"BLOCK\" or \"FLAG\":\n"
                + prose_rules +
                "  FLAG  = everything else: overclaim-adjacent phrasing, tone, voice, level-of-"
                "detail judgments.\n"
                "When uncertain between the two, choose FLAG.\n\n"
                f"VETTED CLAIMS LEDGER:\n{ledger}\n\nELEMENT:\n{text}\n\n"
                'Return ONLY a JSON array, no prose or code fence: '
                '[{"offending_text":"exact quote","issue":"why unsupported","fix":"how to correct",'
                '"severity":"BLOCK or FLAG"}]. '
                "Return [] if every claim is supported."
            ) or "").strip()
            s, e = raw.find("["), raw.rfind("]")
            if s != -1 and e != -1 and e >= s:
                parsed = json.loads(raw[s:e + 1])
                if isinstance(parsed, list):
                    for x in parsed:
                        if not isinstance(x, dict):
                            continue
                        findings.append({
                            "doc": doc, "lens": "fabrication", "element": element,
                            "severity": _normalize_severity(x.get("severity", "")),
                            "offending_text": str(x.get("offending_text", "")),
                            "issue": str(x.get("issue", "")),
                            "fix": str(x.get("fix", "")),
                        })
        except Exception:  # noqa: BLE001 — a flaky judge call must never crash a content edit
            pass
    return findings


def _current_drafts(record: dict, docs_in_scope: set) -> List[dict]:
    """The CURRENT answers to re-audit, mirroring the original scope. essay_answer scope =
    the live custom_qs answers (via drafts_for_audit — only FILLED, non-empty ones). Resume/
    cover scope is intentionally NOT re-audited here: those documents live in applications.json
    and are rebuilt+re-gated by their own regen path; the original Oura verdict was answer-only.
    If the original verdict carried resume/cover findings we surface that limitation rather than
    silently passing a doc we can't re-audit in this process."""
    drafts = drafts_for_audit(record.get("custom_qs") or [])
    return drafts


def _load_application(job_id: str) -> Optional[dict]:
    """Find the APP record in applications.json whose job_id matches `job_id`, or None. This is
    where the TAILORED resume/cover dicts live (the staged manifest carries answers, not the
    package documents). Best-effort: a missing/corrupt file -> None."""
    try:
        data = json.loads(config.APPLICATIONS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return None
    items = data if isinstance(data, list) else (data.get("applications") or [])
    for a in items:
        if isinstance(a, dict) and a.get("job_id") == job_id:
            return a
    return None


def _load_job(job_id: str) -> dict:
    """The job dict (for jd_text/title/company) from jobs.json, or a minimal stub. The quality
    judge scores JD coverage, so the JD is the load-bearing input here. Best-effort."""
    try:
        data = json.loads(config.JOBS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {"id": job_id}
    items = data if isinstance(data, list) else (data.get("jobs") or [])
    for j in items:
        if isinstance(j, dict) and j.get("id") == job_id:
            return j
    return {"id": job_id}


def _answer_drafts_for_quality(record: dict) -> list:
    """The custom answers to hand the quality judge: the stored custom_qs (the judge's own
    flattener keeps only answered/non-empty ones). Distinct from the fabrication drafts because
    the quality lens wants the answers AS-IS alongside the resume/cover, not the gate's filtered
    view."""
    return list(record.get("custom_qs") or record.get("generated") or [])


def _compute_quality_audit(job_id: str, record: dict,
                           llm: Optional[Callable[[str], str]] = None,
                           application: Optional[dict] = None,
                           job: Optional[dict] = None) -> dict:
    """Run the holistic quality judge for `job_id` and return the quality_audit dict to stamp.

    NON-RAISING at THIS layer (mirrors the fabrication path's resilience): a quality-judge
    failure — CLI missing, call died, unparseable after retry, or no application record to judge —
    returns a degraded (judge_ran=False, non-PASS) audit rather than crashing the refresh. Both
    audits therefore always refresh together on every stage-end and dashboard re-run, and a
    degraded quality judge locks Submit via finish.can_submit's judge_ran gate instead of throwing.

    `llm`/`application`/`job` are injectable for tests; omitted they are loaded for real
    (applications.json / jobs.json) and judge_quality constructs claude -p('sonnet')."""
    from .quality_judge import judge_quality, degraded_quality_audit
    app = application if application is not None else _load_application(job_id)
    if app is None:
        # No tailored package on record to judge — that is itself a quality problem (the contract
        # forbids a master fallback). Degrade to a non-PASS, judge_ran=False audit so Submit stays
        # locked until a real tailored package exists to review.
        return degraded_quality_audit(f"no application record with the tailored package for {job_id}")
    job = job if job is not None else _load_job(job_id)
    resume = app.get("resume") if isinstance(app.get("resume"), dict) else {}
    cover = app.get("cover") if isinstance(app.get("cover"), dict) else {}
    answers = _answer_drafts_for_quality(record)
    try:
        return judge_quality(job, resume, cover, answers, llm=llm)
    except Exception as ex:  # noqa: BLE001 — degrade, never crash the refresh (fabrication parity)
        return degraded_quality_audit(repr(ex)[:200])


def refresh(job_id: str, manifest_path: Optional[Path] = None,
            gate_fn: Optional[Callable[[str], list]] = None,
            llm: Optional[Callable[[str], str]] = None,
            ledger: Optional[str] = None,
            quality_llm: Optional[Callable[[str], str]] = None,
            application: Optional[dict] = None,
            job: Optional[dict] = None,
            include_quality: bool = False,
            recheck_calibration: bool = False,
            deterministic_only: bool = False) -> dict:
    """Re-audit job_id's current answers and write a fresh verdict to the manifest.

    Dependencies are injectable for tests; when omitted they are constructed for real
    (deterministic gate + claude -p judge + the on-disk ledger), degrading safely if the
    Claude CLI is unavailable. Returns the new audit dict (with refreshed_at) or a dict with
    an `error` key if the record is missing. Writes via attach_audit (atomic).

    include_quality — THE QUALITY-JUDGE-ONCE SWITCH (2026-06-10). The fabrication `audit`
    ALWAYS re-runs (it is the truth gate and must reflect the current answers on every edit).
    The holistic quality judge is different: an LLM critic asked "how could this be better"
    always finds something, so re-judging on every post-edit refresh spawned an endless
    treadmill of advisory fixes that never converged. So the quality judge now runs ONCE, at
    initial STAGING, and then goes quiet:
      * include_quality=False (DEFAULT — the dashboard "accuracy review" button and every
        post-edit re-check): re-run ONLY the fabrication audit + apply_recompute. The stored
        quality_audit is left BYTE-IDENTICAL (never recomputed, never wiped). A staged app that
        already passed its one quality pass stays submittable (finish.can_submit reads that
        preserved quality_audit); an edit re-checks fabrication but never regenerates quality
        advisories.
      * include_quality=True (initial staging via chain_accuracy_review, or an explicit
        "re-judge quality" action): compute + stamp the quality_audit as before, so a freshly
        staged app gets its single quality pass.

    BOTH gates refresh together ONLY when include_quality=True. When False, the fabrication
    audit refreshes alone and the quality_audit rides along untouched.
    The quality step is non-raising at this layer — a degraded judge stamps judge_ran=False
    rather than crashing — so a stage-end re-judge always leaves a coherent quality_audit.
    `quality_llm` is injectable for tests (the quality judge's claude -p call).

    recheck_calibration — THE STALENESS-WEDGE HEAL (2026-06-11). The dashboard's staleness gate
    (_content_edit_outdates_audit) compares the latest landed content edit against
    min(audit.refreshed_at, quality_audit.refreshed_at). A fabrication-only refresh advances only
    the first stamp, so the min() stayed pre-edit and the gate refused forever no matter how many
    times the user clicked Re-run accuracy review. With recheck_calibration=True, after the
    fabrication re-stamp we ALSO run the CALIBRATION-ONLY recheck (quality_judge.recheck_calibration
    — the SAME polish-dims-frozen path refresh_after_content_edit uses) and re-stamp
    quality_audit.refreshed_at. This respects the quality-once rule: the four polish dimension
    scores stay byte-frozen from staging; only the grounded calibration array is recomputed
    against the current package. Skipped when no prior quality_audit exists (that case belongs to
    include_quality — FINDING #3 recovery) and superseded by include_quality=True (a full quality
    stamp already refreshes the timestamp)."""
    manifest_path = manifest_path or (config.ARIA_DATA / "staged_applications.json")
    record = _load_record(manifest_path, job_id)
    if record is None:
        return {"error": f"no staged record for {job_id}"}

    # deterministic_only (2026-06-22): the LLM accuracy + quality judges were demoted to advisory.
    # The stage path + the regen tool stamp ONLY the deterministic gate (gate_blocks) — finish.
    # can_submit gates on that alone. So we skip constructing the claude -p judge (no quota burn)
    # and force the holistic quality pass off. gate_fn (the deterministic gate) STILL runs, so
    # gate_blocks is stamped truthfully; llm stays None so audit_answers skips the judgment lens.
    if deterministic_only:
        include_quality = False

    docs_in_scope = _original_docs(record)
    drafts = _current_drafts(record, docs_in_scope)

    # Construct the real audit deps unless injected. Each is independent: a missing Claude CLI
    # disables only the judgment lens; the deterministic gate still runs. We never fall back to
    # the metered API (the user's hard rule) and never crash a review step.
    if gate_fn is None:
        try:
            from .llm import make_audit_fn
            gate_fn = make_audit_fn()
        except Exception:
            gate_fn = None
    if llm is None and not deterministic_only:
        try:
            from .llm import make_claude_llm
            llm = make_claude_llm()
        except Exception:
            llm = None
    if ledger is None:
        ledger = _ledger_text()

    audit = audit_answers(drafts, gate_fn, llm, ledger)

    # Carry the original app_id + a human summary; stamp the refresh time. The summary states
    # the BLOCK vs FLAG counts so a reader knows whether the lock is fabrication-class or just
    # style notes riding along on a PASS.
    prior = record.get("audit") or {}
    audit["app_id"] = prior.get("app_id") or job_id
    n_block = int(audit.get("block_findings", 0) or 0)
    n_flag = int(audit.get("flag_findings", 0) or 0)
    n_gate = int(audit.get("gate_blocks", 0) or 0)
    flag_phrase = f"{n_flag} style flag" + ("s" if n_flag != 1 else "")
    judge_ran = bool(audit.get("judge_ran"))
    if not judge_ran and (n_gate + n_block) == 0:
        # The fail-closed degraded stamp (2026-06-11): BLOCKED purely because the LLM
        # ledger-tracing lens never ran (e.g. claude -p unavailable), with no fabrication
        # findings to list. Say that plainly instead of implying findings exist; finish.can_submit
        # refuses it with the matching "incomplete" reason, so Submit stays locked.
        audit["summary"] = ("BLOCKED: the LLM accuracy review was unavailable, so the answers "
                            "are NOT verified (deterministic gate only). Re-run the accuracy "
                            "review with the LLM judge available before submitting."
                            + (f" Also {flag_phrase}." if n_flag else ""))
    elif audit["verdict"] == "PASS":
        if n_flag:
            audit["summary"] = (f"PASS with {flag_phrase}: no fabrication-class findings — "
                                f"send-ready. The style note(s) are advisory.")
        else:
            audit["summary"] = ("PASS: re-audit of the current answers found no unsupported or "
                                "overstated claims — send-ready.")
    else:
        # BLOCKED: gate blocks and/or BLOCK-severity findings are the fabrication class.
        fab = n_gate + n_block
        fab_phrase = f"{fab} fabrication-class finding" + ("s" if fab != 1 else "")
        audit["summary"] = (f"BLOCKED: {fab_phrase}"
                            + (f", {flag_phrase}" if n_flag else "")
                            + " — fix the fabrication-class item(s) before submitting.")
    # Note the scope so a reader knows resume/cover were not re-checked here.
    audit["refreshed_at"] = _local_iso()
    audit["refresh_scope"] = sorted(docs_in_scope)

    attach_audit(manifest_path, job_id, audit)

    # SECOND GATE: the holistic quality judge. Computed + stamped under "quality_audit" ONLY when
    # include_quality is True (initial staging or an explicit re-judge). On the default path
    # (post-edit re-check / dashboard accuracy-review button) we DO NOT touch quality_audit — the
    # one stored at staging rides along unchanged, so editing answers never regenerates the
    # advisory fixes (the treadmill fix, 2026-06-10). Non-raising (degrades to judge_ran=False)
    # — never crashes the refresh.
    if include_quality:
        quality = _compute_quality_audit(job_id, record, llm=quality_llm,
                                         application=application, job=job)
        attach_quality_audit(manifest_path, job_id, quality)
    elif recheck_calibration:
        # THE STALENESS-WEDGE HEAL: re-stamp quality_audit.refreshed_at via the calibration-only
        # recheck (polish dims byte-frozen — the quality-once rule), so the dashboard's
        # min(fab, quality) staleness reference finally advances past the landed edit. Only when
        # a prior quality_audit exists; inventing one here would bypass the one real staging pass.
        prior_quality = record.get("quality_audit")
        if isinstance(prior_quality, dict):
            from .quality_judge import recheck_calibration as _recheck_calibration
            app = application if application is not None else _load_application(job_id)
            job = job if job is not None else _load_job(job_id)
            resume = (app or {}).get("resume") if isinstance((app or {}).get("resume"), dict) else {}
            cover = (app or {}).get("cover") if isinstance((app or {}).get("cover"), dict) else {}
            answers = _answer_drafts_for_quality(record)
            quality = _recheck_calibration(job, resume, cover, answers, prior_quality,
                                           llm=quality_llm)
            attach_quality_audit(manifest_path, job_id, quality)

    # A refresh that flipped BLOCKED->PASS removed the audit blocker; recompute the record's status
    # so a record with no remaining blockers becomes ready_to_submit (and a still-BLOCKED one stays
    # needs_input). Reads the freshly-written audit back via the manifest. One-way valve; atomic.
    apply_recompute(manifest_path, job_id)
    return audit


def refresh_after_content_edit(job_id: str, doc: str, element: str, new_text: str,
                               manifest_path: Optional[Path] = None,
                               gate_fn: Optional[Callable[[str], list]] = None,
                               llm: Optional[Callable[[str], str]] = None,
                               ledger: Optional[str] = None,
                               quality_llm: Optional[Callable[[str], str]] = None,
                               extra_texts: Optional[List[tuple]] = None,
                               audit_floor_ts: Optional[str] = None) -> dict:
    """Re-stamp the STAGED verdict after a resume/cover content edit (BLOCK #2). Two gates re-run;
    the four POLISH dimensions stay FROZEN (no treadmill — see the module note + quality_judge):

      1. FABRICATION (BLOCK-capable): re-audit the current ANSWERS (the original answer-scope) AND
         the just-edited resume/cover ELEMENT text. A ledger-unsupported claim INTRODUCED by the
         edit lands a BLOCK finding so the merged verdict goes BLOCKED and can_submit refuses.
      2. CALIBRATION-ONLY: recheck the six grounded positioning rules over the edited package and
         re-stamp quality_audit with the calibration array + its FAIL contribution, leaving the
         four polish dimension scores byte-frozen from staging.

    A CLEAN edit returns both verdicts to their pre-edit (submittable) state with no advisory
    regeneration. Non-raising: a degraded judge stamps a safe verdict, never crashes. Returns the
    merged fabrication audit dict. Dependencies are injectable for tests; omitted they construct
    the real gate + claude -p judge + on-disk ledger, degrading safely if the CLI is unavailable.

    audit_floor_ts (BUG B, JOB-242): the ts of the content_edit row this heal is repairing. Both
    the fabrication and quality refreshed_at stamps are lifted to max(now, audit_floor_ts) so a
    SUCCESSFUL self-heal never reads as stale on the dashboard's min(audit, quality) staleness
    derivation. The caller (regen_content) passes the latest landed edit-row ts, including the
    terminal "<doc>.doc" row written after a doc-level batch."""
    manifest_path = manifest_path or (config.ARIA_DATA / "staged_applications.json")
    record = _load_record(manifest_path, job_id)
    if record is None:
        return {"error": f"no staged record for {job_id}"}

    if gate_fn is None:
        try:
            from .llm import make_audit_fn
            gate_fn = make_audit_fn()
        except Exception:
            gate_fn = None
    if llm is None:
        try:
            from .llm import make_claude_llm
            llm = make_claude_llm()
        except Exception:
            llm = None
    if ledger is None:
        ledger = _ledger_text()

    # 1. FABRICATION over answers (original scope) + the edited element text, merged into one verdict.
    docs_in_scope = _original_docs(record)
    drafts = _current_drafts(record, docs_in_scope)
    audit = audit_answers(drafts, gate_fn, llm, ledger)
    # The primary edited element + any sibling elements changed in the SAME doc-level batch are all
    # fabrication-audited so a violation introduced by ANY of them blocks submit.
    edited = [(element, new_text)] + list(extra_texts or [])
    content_findings: List[dict] = []
    for el, txt in edited:
        content_findings.extend(audit_content_text(txt, el, gate_fn, llm, ledger))
    if content_findings:
        audit["findings"] = list(audit.get("findings") or []) + content_findings
        n_block = sum(1 for f in audit["findings"]
                      if (f.get("severity", "") or "").upper() == "BLOCK")
        audit["block_findings"] = n_block
        audit["flag_findings"] = len(audit["findings"]) - n_block
        if n_block > 0 or int(audit.get("gate_blocks", 0) or 0) > 0:
            audit["verdict"] = "BLOCKED"

    prior = record.get("audit") or {}
    audit["app_id"] = prior.get("app_id") or job_id
    n_block = int(audit.get("block_findings", 0) or 0)
    n_flag = int(audit.get("flag_findings", 0) or 0)
    n_gate = int(audit.get("gate_blocks", 0) or 0)
    fab = n_gate + n_block
    if not bool(audit.get("judge_ran")) and fab == 0:
        # The fail-closed degraded stamp (2026-06-11): BLOCKED because the LLM lens never ran on
        # the edit, not because findings exist. Name the real reason instead of "0 findings".
        audit["summary"] = (f"BLOCKED after the {doc} edit: the LLM accuracy review was "
                            "unavailable, so the edit is NOT verified. Re-run it with the LLM "
                            "judge available before submitting.")
    elif audit["verdict"] == "BLOCKED":
        fab_phrase = f"{fab} fabrication-class finding" + ("s" if fab != 1 else "")
        audit["summary"] = (f"BLOCKED after a {doc} edit: {fab_phrase}"
                            + (f", {n_flag} style flag" + ("s" if n_flag != 1 else "") if n_flag else "")
                            + ". Fix the fabrication-class item(s) before submitting.")
    else:
        audit["summary"] = (f"PASS: re-audit after the {doc} edit found no unsupported or "
                            "overstated claims; send-ready."
                            + (f" {n_flag} style flag" + ("s" if n_flag != 1 else "") + " ride along."
                               if n_flag else ""))
    # BUG B (JOB-242): lift the stamp to >= the content_edit row this heal repairs, so a successful
    # self-heal never reads as false-stale on the dashboard's min(audit, quality) staleness gate.
    audit["refreshed_at"] = _stamp_at_or_after(audit_floor_ts)
    audit["refresh_scope"] = sorted(set(docs_in_scope) | {doc})
    attach_audit(manifest_path, job_id, audit)

    # 2. CALIBRATION-ONLY recheck over the edited package, frozen polish dims, re-stamp quality_audit.
    from .quality_judge import recheck_calibration
    application = _load_application(job_id)
    job = _load_job(job_id)
    resume = (application or {}).get("resume") if isinstance((application or {}).get("resume"), dict) else {}
    cover = (application or {}).get("cover") if isinstance((application or {}).get("cover"), dict) else {}
    answers = _answer_drafts_for_quality(record)
    prior_quality = record.get("quality_audit") if isinstance(record.get("quality_audit"), dict) else {}
    quality = recheck_calibration(job, resume, cover, answers, prior_quality, llm=quality_llm)
    # The quality stamp must clear the SAME floor as the fabrication stamp — the staleness gate keys
    # off min(audit, quality), so leaving quality at its own (earlier) now would re-introduce BUG B.
    if isinstance(quality, dict):
        quality["refreshed_at"] = _stamp_at_or_after(audit_floor_ts)
    attach_quality_audit(manifest_path, job_id, quality)

    apply_recompute(manifest_path, job_id)
    return audit


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="apply_engine.refresh_audit",
                                 description="Re-run the accuracy audit over a staged "
                                             "application's CURRENT answers and update the verdict.")
    ap.add_argument("job_id", help="Job id, e.g. JOB-131")
    ap.add_argument("--with-quality", action="store_true",
                    help="Also RE-JUDGE the holistic quality gate (recompute quality_audit). "
                         "OFF by default: the dashboard accuracy-review button and every post-edit "
                         "re-check run fabrication-only and leave the staged quality_audit "
                         "untouched (the quality judge runs once at staging, then stays quiet).")
    ap.add_argument("--recheck-calibration", action="store_true",
                    help="After the fabrication re-stamp, ALSO run the calibration-only quality "
                         "recheck and re-stamp quality_audit.refreshed_at (polish dimension scores "
                         "stay frozen from staging; this is NOT a full re-judge). The dashboard "
                         "passes this when a landed resume/cover edit post-dates the stored "
                         "verdicts, so one Re-run accuracy review click can clear the staleness "
                         "lock instead of looping forever. Superseded by --with-quality.")
    args = ap.parse_args(argv)

    result = refresh(args.job_id, include_quality=args.with_quality,
                     recheck_calibration=args.recheck_calibration)
    if result.get("error"):
        print(result["error"])
        return 2
    print(f"re-audited {args.job_id}: verdict={result['verdict']} "
          f"gate_blocks={result['gate_blocks']} findings={len(result['findings'])} "
          f"refreshed_at={result['refreshed_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
