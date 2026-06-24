# -*- coding: utf-8 -*-
"""Regenerate ONE staged custom-question answer per a Sam instruction, re-run the
deterministic accuracy gate, and write the result back to staged_applications.json.

Launched DETACHED by the ARIA dashboard's /apply-queue/<job>/request-edit endpoint, so
the server itself never runs an LLM call or writes the manifest. Honesty guardrails are
inherited from build_prompt/build_refine_prompt; the user instruction is applied on top,
explicitly told to stay within the FACTS.

    python -m apply_engine.regen_answer JOB-131 --question "..." --instruction "make it tighter"
    python -m apply_engine.regen_answer JOB-131 --question "..." --revert
    python -m apply_engine.regen_answer JOB-216 --question "..." --provide "Yes"

--provide writes Sam's OWN words onto the question (status=answered, answered_by=sam).
It runs NO LLM and NO accuracy gate — Sam's answer is final. It also prunes the matching
item out of the record's top-level needs_sam list so the card's "Still needs you" count
drops. A --provide answer deliberately leaves edit_request empty so it never trips the
"edited — needs a fresh accuracy review" submit block (that block is only for AI rewrites).
When the question exists ONLY in needs_sam (no staged custom_q widget), --provide CREATES
a custom_q so finish.replay deterministically re-types the answer into the live form.

edit_request now means "edit IN FLIGHT" (set by the dashboard when it launches an LLM regen,
cleared when that regen completes). An --instruction regen clears it on EVERY terminal outcome
(drafted/blocked/needs_input/failed); the instruction lives on in the edit_history row, which
is what the dashboard renders. --revert and --provide also leave it empty.

BLOCKED EDITS DO NOT CLOBBER THE ANSWER. When the deterministic accuracy gate rejects a NEW
draft, the original answer STANDS: target["value"]/["status"]/["reason"] are left exactly as
they were. The refusal lives ONLY in the edit_history row — {status:"blocked", before:<old
value>, after:<the REFUSED draft>, reason:<gate blocks>} — which is what the dashboard renders
as a "your edit was refused, original kept" note. This mirrors regen_content.py's blocked path
(it never overwrites the element value on a block). A refused edit is non-revertible (history
status is "blocked", not "edited") and edit_request is still cleared (terminal outcome). Older
records may carry a legacy shape where a block set status=="blocked" with the refused text as
the value; the dashboard keeps rendering those for backward compatibility.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import config
from . import iterate_fix
from .answer_gen import DECLINE, build_prompt, build_refine_prompt
from .filemutex import locked
from .llm import load_facts, make_audit_fn, make_claude_llm
from .staged_manifest import recompute_status
from .text_sanitize import strip_editor_preamble


def _qkey(s):
    return "".join(c for c in (s or "").lower() if c.isalnum())[:70]


def _local_iso():
    """Local ISO timestamp WITH offset — matches regen_content for consistent history rows."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _merge_write(manifest, job_id, mutate_fn, qkey=None):
    """The core merge-safe write for staged_applications.json (Part 2).

    A regen run loads the manifest at start, then spends 60-90s in the LLM. Writing back the
    WHOLE stale `data` it loaded would clobber any sibling edit that landed in the meantime
    (lost update). Instead, AT WRITE TIME, under the cross-process file mutex, we:
      1. RE-READ the manifest fresh from disk,
      2. locate THIS run's record (by job_id) and — if `qkey` is given — THIS run's custom_q
         (by normalized question key) on the FRESH objects,
      3. hand them to `mutate_fn(fresh_app, fresh_target)` which applies ONLY this run's delta
         (set fields, APPEND its one history row to the fresh list — never replace the list, a
         concurrent edit to a SIBLING question may have appended too), prunes needs_sam on
         the fresh record, and may recompute status,
      4. atomic-write (tmp + replace) and release the lock.

    Concurrent edits to DIFFERENT questions of the same job both land. Edits to the SAME
    question are prevented from launching together by the per-element server lock (Part 3); the
    mutex here is the cross-process safety net that also serializes the unrelated whole-file
    writers (finish._mark_submitted, refresh_audit.attach_audit) against answer edits.

    mutate_fn returns a value that `_merge_write` returns through to the caller (e.g. a status
    string for the print line). If the record (or the requested custom_q) can't be found in the
    FRESH file, mutate_fn is called with whatever was found (target may be None) — callers that
    require a target must guard. Falls back to the stale in-memory write only if the fresh read
    fails, so a transiently-corrupt manifest can't silently drop an edit."""
    with locked(manifest):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("manifest is not a list")
        except Exception:
            # Fresh read failed (missing/corrupt). We have nothing safe to merge onto, so we
            # cannot proceed without risking a clobber — re-raise so the caller's outer handling
            # surfaces it rather than silently losing the edit.
            raise
        fresh_app = next(
            (a for a in data if isinstance(a, dict) and a.get("job_id") == job_id), None)
        fresh_target = None
        if fresh_app is not None and qkey is not None:
            fresh_target = next(
                (q for q in (fresh_app.get("custom_qs") or [])
                 if isinstance(q, dict) and _qkey(q.get("q", "")) == qkey), None)
        result = mutate_fn(fresh_app, fresh_target)
        tmp = manifest.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(manifest)
        return result


# corrections_log lives at the career ROOT (one level above this package). These scripts run
# with cwd = ~/projects/career so a bare import resolves; guard it anyway so a missing/broken
# ledger module can never stop an answer edit from being saved.
try:  # pragma: no cover - import wiring
    import corrections_log as _corrections_log
except Exception:  # noqa: BLE001
    _corrections_log = None


def _ledger_answer(app, question, entry):
    """Mirror one answer edit_history row into the consolidated corrections ledger.

    Called right after every `edit_history.append(...)` site. The element is the question text
    (the answer's identity), truncated for the ledger. Best-effort: a None module or any error
    is swallowed (record_correction itself never raises, but the import guard might leave it
    None)."""
    if _corrections_log is None:
        return
    _corrections_log.record_correction(
        app.get("id", "") if isinstance(app, dict) else "",
        "answer",
        (question or "")[:80],
        entry.get("instruction", ""),
        entry.get("before", ""),
        entry.get("after", ""),
        entry.get("status", ""),
        "regen_answer",
        company=app.get("company", "") if isinstance(app, dict) else "",
        role=(app.get("role", "") or app.get("title", "")) if isinstance(app, dict) else "",
        extra={"reason": entry.get("reason", ""), "question": question or ""},
    )


def _norm(s):
    """Loose normalization for matching a question against a needs_sam free-text item:
    lowercase, strip a trailing required asterisk, collapse whitespace. Used only for the
    needs_sam substring prune — the custom_q match still uses the strict _qkey."""
    t = (s or "").replace("*", " ").lower()
    return " ".join(t.split())


def _match_needs_sam(app, question):
    """Return the FULL text of the first needs_sam item that refers to `question`
    (normalized exact OR substring either direction), or None. Read-only — does not mutate.
    Used to recover the exact item text so a created custom_q stores the live-widget label."""
    items = app.get("needs_sam")
    if not isinstance(items, list) or not items:
        return None
    qn = _norm(question)
    if not qn:
        return None
    for it in items:
        itn = _norm(str(it))
        if itn and (itn == qn or itn in qn or qn in itn):
            return str(it)
    return None


def _prune_needs_sam(app, question):
    """Drop any needs_sam entry that refers to `question` (normalized exact OR substring
    either direction). Returns the count removed. Mutates app['needs_sam'] in place."""
    items = app.get("needs_sam")
    if not isinstance(items, list) or not items:
        return 0
    qn = _norm(question)
    if not qn:
        return 0
    kept = []
    removed = 0
    for it in items:
        itn = _norm(str(it))
        if itn and (itn == qn or itn in qn or qn in itn):
            removed += 1
            continue
        kept.append(it)
    if removed:
        app["needs_sam"] = kept
    return removed


def _is_multi_kind(kind):
    """Only checkbox-group ('check all that apply') answers are multi-valued; everything else
    (essay/short_text/select/yesno) stores a single `value`. Mirrors finish._replay_custom."""
    return (kind or "").strip().lower() in ("checkbox_group", "checkbox", "multi_select", "multiselect")


def _do_provide(manifest, job_id, target, question, text):
    """Write Sam's OWN answer: value/values + status=answered, clear the AI-review machinery
    (reason/review_findings), stamp answered_by=sam, append a 'provided' edit_history row,
    prune the matching needs_sam item, MERGE-WRITE. NO LLM, NO gate.

    MERGE-SAFE (Part 2): all mutation happens inside `_merge_write` on the FRESHLY re-read record
    and custom_q, so a concurrent edit to a sibling question of the same job is never clobbered.
    `target` from the caller's initial load is used ONLY to decide the multi-kind shape and the
    needs_sam-only branch; the actual writes go onto the fresh objects.

    edit_request is deliberately NOT set: the server submit gate blocks any custom_q carrying
    an edit_request (an AI rewrite awaiting re-review). Sam's own words need no re-review,
    so leaving edit_request empty keeps this answer from blocking submit."""
    kind = (target.get("kind", "") or "") if target else ""
    qkey = _qkey(question)

    def _mutate(fresh_app, fresh_target):
        if fresh_app is None:
            return ("missing", 0)
        before = fresh_target.get("value", "") if fresh_target else ""
        if isinstance(before, list):
            before = ", ".join(str(x) for x in before)
        before = before or ""

        if fresh_target is not None:
            if _is_multi_kind(kind):
                values = [p.strip() for p in text.split(",") if p.strip()] or [text.strip()]
                fresh_target["values"] = values
                fresh_target["value"] = ", ".join(values)
                after = fresh_target["value"]
            else:
                fresh_target["value"] = text
                after = text
            fresh_target["status"] = "answered"
            fresh_target["reason"] = ""
            fresh_target["review_findings"] = []
            fresh_target["answered_by"] = "sam"
            # Never carry an edit_request on a Sam-provided answer (see docstring).
            fresh_target["edit_request"] = ""
            # APPEND to the fresh list — never replace it; a concurrent sibling edit may have
            # appended its own history row that we must not drop.
            fresh_target.setdefault("edit_history", []).append({
                "ts": _local_iso(),
                "instruction": "(provided by Sam)",
                "before": before,
                "after": after,
                "status": "provided",
            })
            _ledger_answer(fresh_app, question, fresh_target["edit_history"][-1])

        # needs_sam-ONLY item (no staged custom_q widget): CREATE a real custom_q so the
        # deterministic finish.replay path will re-type Sam's answer into the live form.
        # The created entry stores the FULL needs_sam item text as `q` so
        # finish.match_custom_entry matches it to the live widget by normalized label.
        created = None
        if fresh_target is None:
            item_text = _match_needs_sam(fresh_app, question)
            if item_text is not None:
                value = text
                ck = "yesno" if text.strip().lower() in ("yes", "no") else "short_text"
                created = {
                    "q": item_text,
                    "kind": ck,
                    "value": value,
                    "status": "answered",
                    "answered_by": "sam",
                    "reason": "",
                    "review_findings": [],
                    "edit_request": "",
                    "edit_history": [{
                        "ts": _local_iso(),
                        "instruction": "(provided by Sam)",
                        "before": "",
                        "after": value,
                        "status": "provided",
                    }],
                }
                fresh_app.setdefault("custom_qs", []).append(created)
                _ledger_answer(fresh_app, question, created["edit_history"][-1])

        # Prune the matching needs_sam item whether or not a custom_q matched.
        pruned = _prune_needs_sam(fresh_app, question)

        if fresh_target is None and created is None and pruned == 0:
            return ("notfound", 0)

        # One-way valve: a needs_input record whose last blocker Sam just answered flips to
        # ready_to_submit. Recompute on the FRESH record so a sibling edit's resolution counts too.
        new_status = recompute_status(fresh_app)
        if new_status and new_status != (fresh_app.get("status") or ""):
            fresh_app["status"] = new_status

        where = "custom_q" if fresh_target is not None else (
            "needs_sam -> created custom_q" if created is not None else "needs_sam only")
        return (where, pruned)

    where, pruned = _merge_write(manifest, job_id, _mutate, qkey=qkey)
    if where == "missing":
        print(f"no staged record for {job_id}")
        return 2
    if where == "notfound":
        print("question not found on this application")
        return 2
    print(f"provided answer for {question[:50]!r} ({where}); needs_sam pruned={pruned}")
    return 0


def _do_revert(manifest, job_id, question):
    """Undo the latest answer edit: restore its `before`, drop back to 'drafted', clear the
    edit_request + review_findings, append a 'reverted' history row, MERGE-WRITE. NO LLM calls.

    MERGE-SAFE (Part 2): re-reads the FRESH custom_q under the mutex and reverts THAT, so a
    sibling-question edit landing between launch and write is preserved. The revert guard runs
    against the fresh value too: the answer's CURRENT value must equal the latest history entry's
    `after`; otherwise a later edit moved it and reverting would clobber that — refuse (exit 1)."""
    qkey = _qkey(question)

    def _mutate(fresh_app, fresh_target):
        if fresh_app is None or fresh_target is None:
            return "notfound"
        hist = fresh_target.get("edit_history") or []
        last = hist[-1] if hist else None
        if not isinstance(last, dict) or (last.get("status", "") or "").lower() != "edited":
            return "no-edit"
        current = fresh_target.get("value", "") or ""
        if current != (last.get("after", "") or ""):
            return "moved"
        restored = last.get("before", "") or ""
        fresh_target["value"] = restored
        fresh_target["status"] = "drafted"
        fresh_target["edit_request"] = ""
        fresh_target["review_findings"] = []
        # APPEND to the fresh list (never replace) so a concurrent sibling history row survives.
        fresh_target.setdefault("edit_history", []).append({
            "ts": _local_iso(),
            "instruction": "(revert)",
            "before": current,
            "after": restored,
            "status": "reverted",
        })
        # The question text lives on the custom_q record as `q`. app={} → ledger records empty
        # app_id/company/role for reverts (mirrors prior behavior).
        _ledger_answer({}, fresh_target.get("q", ""), fresh_target["edit_history"][-1])
        return "ok"

    out = _merge_write(manifest, job_id, _mutate, qkey=qkey)
    if out == "notfound":
        print("question not found on this application")
        return 1
    if out == "no-edit":
        print("no edit to revert for this answer")
        return 1
    if out == "moved":
        print("current text no longer matches that edit — manual review needed")
        return 1
    print("reverted answer: status=drafted")
    return 0


def _norm_ws(s):
    """Whitespace-normalized lowercase form for substring containment checks: collapse all
    runs of whitespace to single spaces and lowercase. Used to decide whether a finding's
    offending_text still appears in the (possibly reflowed) new answer."""
    return " ".join((s or "").split()).lower()


# BUG #6 (live JOB-246, 2026-06-13): when an --instruction edit is effectively a NO-OP (the thing
# to change isn't present), the model sometimes returns COMMENTARY ABOUT the answer — e.g.
# "'Over the past year' doesn't appear in the current answer. The ARIA sentence reads: > '...'.
# No edit needed." — instead of the answer itself. The deterministic fabrication gate passes it
# (it fabricates nothing), so it landed as the new value and CORRUPTED the answer downstream.
#
# This detector recognizes commentary-about-the-answer rather than an answer. It is intentionally
# TIGHT — scoped to (1) explicit no-op / "no edit needed" phrases the model uses when it decides
# nothing should change, (2) opening/structural signals that the text is talking ABOUT the answer
# rather than being one, and (3) a body that is largely a markdown blockquote (a quoted prior
# draft). A legitimate answer that merely contains the word "change" or a short embedded quote
# must NOT trip it (tested both directions), so we anchor on phrases + the OPENING of the text and
# the share of quoted lines, never on stray interior words.

# Explicit "I didn't / shouldn't change anything" phrases. These are near-unambiguous: a real
# job-application answer addressed to an employer does not say "no edit needed".
_META_NOOP_PHRASES = (
    "doesn't appear", "does not appear", "doesn't exist in", "does not exist in",
    "no edit needed", "no edits needed", "no edit is needed", "no edits are needed",
    "no change needed", "no changes needed", "no change is needed", "no changes are needed",
    "no change was needed", "nothing to change", "nothing needs to change",
    "nothing needs changing", "already clean", "already complies", "already compliant",
    "i have not changed", "i haven't changed", "i did not change", "i didn't change",
    "i have left the answer", "i left the answer", "the answer is unchanged",
    "the answer remains unchanged", "no rewrite needed", "no rewrite is needed",
)

# Meta phrases that reference "the answer/prompt/question/text/draft/sentence" as an OBJECT being
# discussed. On their own these are weaker (an answer could mention "the question"), so they only
# count when they appear near the START of the text (the opening signals commentary, not content).
_META_OBJECT_PHRASES = (
    "the answer reads", "the current answer", "the existing answer", "the answer currently",
    "the answer already", "the original answer", "the prior answer", "the draft reads",
    "the current draft", "the sentence reads", "the existing text", "the current text",
    "the answer says", "the answer does not", "the answer doesn't", "as written, the",
)


def _is_meta_commentary(text):
    """True when `text` is commentary ABOUT an answer rather than an answer itself (BUG #6).

    Tight, deterministic heuristics — designed to fire on the JOB-246 corruption while leaving real
    answers (even ones containing 'change' or a short quote) untouched:

      1. Any explicit no-op phrase anywhere ("no edit needed", "doesn't appear", "already clean",
         "I didn't change", ...). These don't occur in a genuine answer addressed to an employer.
      2. An OPENING (first ~200 chars / first line) that references "the answer/draft/sentence" as
         an object being discussed ("the current answer", "the ARIA sentence reads", ...). Scoped to
         the opening so an answer that merely mentions "the question" deep in its body is safe.
      3. A body that is LARGELY a markdown blockquote: more than half its non-empty lines start with
         ">" (the model quoting a prior draft back at us), or it is mostly quoted with a no-op-ish
         framing line. A real answer is prose, not a quote block.

    Returns False on empty/short text (nothing to land anyway; the decline path handles that)."""
    t = (text or "").strip()
    if not t:
        return False
    low = t.lower()

    # (1) explicit no-op phrases — strongest signal, anywhere in the text.
    if any(p in low for p in _META_NOOP_PHRASES):
        return True

    # (3) blockquote-dominant body: the model handed back a quoted prior draft.
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if lines:
        quoted = sum(1 for ln in lines if ln.startswith(">"))
        if quoted and quoted * 2 >= len(lines):
            # Majority of the content is a quote block — not an answer.
            return True

    # (2) opening references the answer/draft/sentence as an object under discussion. Look only at
    # the first line and the first ~200 chars so an interior "the question" can't false-positive.
    opening = (lines[0] if lines else "")[:200].lower() + " " + low[:200]
    if any(p in opening for p in _META_OBJECT_PHRASES):
        return True

    return False


# Appended to the instruction on the ONE meta-commentary re-prompt: tell the model in no uncertain
# terms to emit the answer text only.
_META_REPROMPT = (
    "\n\nIMPORTANT: Output ONLY the complete answer text itself — the exact words that should go in "
    "the application field. Do NOT write commentary about the answer. Never say 'no edit needed', "
    "'doesn't appear', or 'no change needed'. Never quote or describe a prior draft. If the edit "
    "instruction does not apply, return the answer UNCHANGED as plain prose, with no explanation.")


def _prune_stale_findings(app, question, new_value):
    """After a successful dashboard fix, drop from app['audit']['findings'] any finding that
    (a) refers to THIS question (same _qkey) AND (b) whose offending_text no longer appears in
    the new answer text (whitespace-normalized substring). This makes the dashboard reflect
    reality after each fix without waiting for a full re-audit — the CLI owns the manifest, so
    this is the right place to do it.

    If, after pruning, NO BLOCK-severity findings remain and the stored verdict was 'BLOCKED'
    with gate_blocks == 0, flip the verdict to 'PASS' (all fabrication-class findings were
    addressed via dashboard fixes). This mirrors refresh_audit's two-severity rule: a PASS may
    still carry FLAG (style) findings — those ride along visibly and never lock Submit. gate_blocks
    > 0 means a deterministic gate block is still outstanding, so we leave BLOCKED.

    Returns the number of findings removed. Mutates app['audit'] in place (no-op if absent)."""
    audit = app.get("audit")
    if not isinstance(audit, dict):
        return 0
    findings = audit.get("findings")
    if not isinstance(findings, list) or not findings:
        return 0
    qk = _qkey(question)
    new_norm = _norm_ws(new_value)
    kept = []
    removed = 0
    for f in findings:
        if isinstance(f, dict) and _qkey(f.get("question", "")) == qk:
            off = _norm_ws(f.get("offending_text", ""))
            # Drop the finding once its flagged text is gone from the answer. An empty
            # offending_text can't be matched, so treat it as resolved too (the fix ran).
            if not off or off not in new_norm:
                removed += 1
                continue
        kept.append(f)
    if not removed:
        return 0
    audit["findings"] = kept
    # Two-severity recompute (mirrors refresh_audit.audit_answers): PASS when no BLOCK-severity
    # finding remains AND no deterministic gate block is outstanding. FLAG findings may remain in
    # `kept` on a PASS — they're advisory and never lock Submit.
    blocks_left = sum(1 for f in kept
                      if isinstance(f, dict) and (f.get("severity", "") or "").upper() == "BLOCK")
    if (blocks_left == 0 and audit.get("verdict") == "BLOCKED"
            and int(audit.get("gate_blocks", 0) or 0) == 0):
        audit["verdict"] = "PASS"
        n_flag = len(kept)
        audit["summary"] = ("fabrication-class findings addressed via dashboard fixes; verdict "
                            + ("PASS with " + str(n_flag) + " style flag"
                               + ("s" if n_flag != 1 else "") if n_flag else "PASS")
                            + " — updated by regen_answer " + _local_iso())
    return removed


def _find_job(jobs_path, job_id):
    try:
        for j in json.loads(Path(jobs_path).read_text(encoding="utf-8")):
            if j.get("id") == job_id:
                return j
    except Exception:
        pass
    return {}


def _crash_clear_edit_request(args, ex):
    """TERMINAL backstop for BLOCK #4 invariant #5: when _run raises ANYWHERE (including the
    pre-guard file I/O — load_facts has caused a live incident — or the manifest read) on an
    --instruction edit, CLEAR the in-flight edit_request and append a `failed` edit_history row
    under the file mutex. Without this, a detached crash (stderr->DEVNULL) leaves edit_request set
    and Submit locked forever with no completed review to release it.

    Only meaningful for an --instruction edit (the only mode the server marks edit_request for;
    --revert/--provide never set it). Best-effort: never re-raises; the server TTL is the final net."""
    if not getattr(args, "instruction", None):
        return
    try:
        manifest = Path(config.ARIA_DATA) / "staged_applications.json"
        qk = _qkey(args.question)
        reason = f"regeneration crashed: {type(ex).__name__}: {ex}"[:200]

        def _mutate_failed(fresh_app, fresh_target):
            if fresh_app is None or fresh_target is None:
                return
            fresh_target["edit_request"] = ""
            fresh_before = fresh_target.get("value", "") or ""
            fresh_target.setdefault("edit_history", []).append({
                "ts": _local_iso(),
                "instruction": args.instruction,
                "before": fresh_before,
                "after": fresh_before,
                "status": "failed",
                "reason": reason,
            })
            _ledger_answer(fresh_app, fresh_target.get("q", args.question), fresh_target["edit_history"][-1])

        _merge_write(manifest, args.job_id, _mutate_failed, qkey=qk)
        print(f"regen_answer CRASHED for {args.job_id}: {reason}")
    except Exception:  # noqa: BLE001 — the backstop itself must never raise
        pass


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="apply_engine.regen_answer")
    ap.add_argument("job_id")
    ap.add_argument("--question", required=True)
    # --instruction (LLM edit), --revert (undo), --provide (Sam's own words) are
    # mutually exclusive: exactly one mode per invocation.
    ap.add_argument("--instruction")
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--provide")
    # ITERATE-TO-CLEAN (engine-own fix path only): when N>1 and the gate rejects an --instruction
    # rewrite, re-prompt with the gate's specific complaint + ledger facts and regen AGAIN, up to N.
    # DEFAULT 1 == today's single-pass behaviour EXACTLY (Sam's dashboard edits never pass N>1).
    ap.add_argument("--max-attempts", type=int, default=1, dest="max_attempts")
    # G2 LENGTH target (engine-own length-fix path only). When set, an attempt that PASSES the
    # fabrication/disclosure gate is ALSO checked against the stated word range; an answer still
    # under --min-words / over --max-words is treated as not-yet-clean and re-prompted (within the
    # --max-attempts budget). DEFAULT unset == today's behaviour EXACTLY (no length re-check).
    ap.add_argument("--min-words", type=int, default=None, dest="min_words")
    ap.add_argument("--max-words", type=int, default=None, dest="max_words")
    args = ap.parse_args(argv)

    # TOP-LEVEL CRASH GUARD (BLOCK #4 #5): _run does ALL the work, including pre-guard file I/O
    # (load_facts, the manifest read) not covered by the inner LLM try/except. Any unhandled raise
    # clears the in-flight edit_request + records a failed history row, then exits non-zero — so a
    # detached crash can never wedge Submit on a permanently-set edit_request.
    try:
        return _run(args)
    except SystemExit:
        raise
    except Exception as ex:  # noqa: BLE001 — terminal backstop, then fail non-zero
        _crash_clear_edit_request(args, ex)
        return 1


def _run(args):
    modes = [bool(args.instruction), bool(args.revert), args.provide is not None]
    if sum(modes) > 1:
        print("--instruction, --revert, and --provide are mutually exclusive")
        return 2
    if sum(modes) == 0:
        print("need --instruction (to edit), --revert (to undo), or --provide (your own answer)")
        return 2
    if args.provide is not None and not args.provide.strip():
        print("--provide needs a non-empty answer")
        return 2

    manifest = Path(config.ARIA_DATA) / "staged_applications.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    app = next((a for a in data if isinstance(a, dict) and a.get("job_id") == args.job_id), None)
    if not app:
        print(f"no staged record for {args.job_id}")
        return 2
    cqs = app.get("custom_qs") or []
    qk = _qkey(args.question)
    target = next((q for q in cqs if _qkey(q.get("q", "")) == qk), None)

    # --provide tolerates a needs_sam-only question (no staged custom_q widget): it still
    # prunes the callout item. Every other mode requires a real custom_q to act on.
    if args.provide is not None:
        return _do_provide(manifest, args.job_id, target, args.question, args.provide.strip())

    if not target:
        print("question not found on this application")
        return 2

    if args.revert:
        return _do_revert(manifest, args.job_id, args.question)

    job = _find_job(config.JOBS_JSON, args.job_id)
    facts = load_facts(job)
    kind = (target.get("kind", "essay") or "essay")
    question = target.get("q", "")
    old_value = target.get("value", "") or ""   # snapshot BEFORE the edit, for edit_history

    instr_line = ("\n\nADDITIONAL INSTRUCTION FROM SAM — apply this to the rewrite while "
                  "staying strictly within the FACTS and the honesty rules above:\n" + args.instruction)
    # MINIMAL-EDIT contract: when an answer already exists, the model MUST see it and edit it
    # in place — otherwise every "apply this correction and change nothing else" produces a
    # from-scratch draft that randomly re-introduces previously-fixed violations (observed
    # 2026-06-04: ANSYS-at-Meridian and a forbidden tool name resurfacing across fix rounds).
    if old_value.strip():
        instr_line = (
            "\n\nCURRENT ANSWER (edit THIS text — do not draft a new answer from scratch):\n"
            + old_value
            + "\n\nEDIT INSTRUCTION FROM SAM — change ONLY what this requires, preserve "
              "every other sentence verbatim, and stay strictly within the FACTS and the "
              "honesty rules above:\n" + args.instruction)

    # The claude-CLI factory/generation can raise (apply_engine.llm.LLMUnavailable or anything
    # else) before anything is written. This process runs DETACHED with stdout/stderr at DEVNULL,
    # so an unhandled raise would die leaving NO trace and the dashboard would show the old answer
    # forever. Catch here: leave the target's existing value untouched, mark it needs_input with a
    # reason, record the edit_request, persist the manifest atomically, and exit 1.
    def _write_failed(reason):
        """Durable failure outcome (factory raise OR attempt-1 generation raise): status=needs_input
        + reason, value/review_findings UNTOUCHED, edit_request cleared, a 'failed' edit_history row.
        MERGE-SAFE on the FRESH custom_q. Returns 1 (the failure exit code). Backward-compatible —
        this is the EXACT outcome the single-pass code wrote on a pre-write generation failure."""
        def _mutate_failed(fresh_app, fresh_target):
            if fresh_app is None or fresh_target is None:
                return
            fresh_target["status"] = "needs_input"
            fresh_target["reason"] = reason
            # CONTRACT: edit_request means "edit IN FLIGHT". This regen has terminated (failed),
            # so clear it — the instruction is preserved in the edit_history row below. Leaving it
            # set would lock Submit forever with no completed review to unlock it.
            fresh_target["edit_request"] = ""
            fresh_before = fresh_target.get("value", "") or ""
            fresh_target.setdefault("edit_history", []).append({
                "ts": _local_iso(),
                "instruction": args.instruction,
                "before": fresh_before,
                "after": fresh_before,
                "status": "failed",
            })
            _ledger_answer(fresh_app, question, fresh_target["edit_history"][-1])

        _merge_write(manifest, args.job_id, _mutate_failed, qkey=qk)
        print(f"regenerated answer for {args.job_id}: status=needs_input reason={reason}")
        return 1

    try:
        llm = make_claude_llm()
        audit = make_audit_fn()
    except Exception as ex:  # noqa: BLE001 — fail loud but leave a durable record
        return _write_failed(f"regeneration failed: {type(ex).__name__}: {ex}"[:200])

    # ── ITERATE-TO-CLEAN attempt loop (N=1 == today's single pass, exactly) ──
    # The ledger facts re-grounded into a retry's feedback clause come from the SAME ledger the
    # self-audit traces against (read once; full file per feedback_ledger_truncation_false_blocks).
    try:
        ledger = (config.PKG_DIR.parent / "claims_ledger.md").read_text(encoding="utf-8")[:20000]
    except Exception:
        ledger = ""

    max_attempts = max(1, int(getattr(args, "max_attempts", 1) or 1))

    def _ledger_self_audit(answer_text):
        """Trace every claim in `answer_text` against the ledger → FLAG findings (offending_text/
        issue/fix). Same prompt as before; reused per attempt so a retry's feedback clause can name
        the previous attempt's specific ledger findings. Best-effort: [] on any failure."""
        try:
            jr = (llm(
                "You are an honesty auditor for Sam Rivera's job-application answers. Below is his "
                "VETTED CLAIMS LEDGER (the complete set of claims he is allowed to make) and a drafted "
                "ANSWER. Identify every claim in the ANSWER — any number, metric, percentage, scope, tool, "
                "employer, outcome, or stated interest — that is NOT supported by the ledger or is overstated "
                "beyond its stated bounds.\n\n"
                f"VETTED CLAIMS LEDGER:\n{ledger}\n\nANSWER:\n{answer_text}\n\n"
                'Return ONLY a JSON array, no prose or code fence: '
                '[{"offending_text":"exact quote","issue":"why unsupported","fix":"how to correct"}]. '
                "Return [] if every claim is supported."
            ) or "").strip()
            s, e = jr.find("["), jr.rfind("]")
            if s != -1 and e != -1:
                parsed = json.loads(jr[s:e + 1])
                if isinstance(parsed, list):
                    return [
                        {"severity": "FLAG",
                         "offending_text": str(x.get("offending_text", "")),
                         "issue": str(x.get("issue", "")),
                         "fix": str(x.get("fix", ""))}
                        for x in parsed if isinstance(x, dict)][:5]
        except Exception:
            pass
        return []

    # Outcome accumulators (written onto the FRESH record under the mutex below). On a clean
    # landing: new_value/new_status_field/new_reason + new_findings. On a (final) block: blocked_draft
    # + block_reason, original kept. On needs_input: new_status_field="needs_input".
    blocked_draft = ""
    block_reason = ""
    new_value = ""
    new_status_field = ""
    new_reason = ""
    new_findings = []
    leave_findings = False
    preserve_prior = False   # BUG #6: keep the existing value (don't write new_value) — set when the
                             # rewrite returned commentary about the answer instead of an answer.
    residual = None          # set ONLY when N>1 exhausts still-blocked: classified residual dict
    attempts_used = 0

    feedback = ""            # threaded onto the instruction on attempts >1
    prev_text = ""           # the previous attempt's text (named in the feedback clause)
    for attempt in range(1, max_attempts + 1):
        attempts_used = attempt
        try:
            raw = strip_editor_preamble((llm(build_prompt(question, kind, facts) + instr_line + feedback) or "").strip())
        except Exception as ex:  # noqa: BLE001 — a mid-loop generation failure ends the loop
            # A generation failure on attempt 1 reproduces the legacy pre-write failure outcome
            # EXACTLY (status=needs_input, 'failed' history row, value/review_findings untouched,
            # exit 1) via the shared _write_failed. A LATER-attempt generation failure keeps whatever
            # the PRIOR attempt already staged (its blocked_draft / needs_input outcome stands) and
            # ends the loop — we do not discard a usable earlier attempt for a transient retry crash.
            if attempt == 1:
                return _write_failed(f"regeneration failed: {type(ex).__name__}: {ex}"[:200])
            break

        # Refine ONLY on from-scratch drafts, and ONLY on the first attempt. A minimal edit of an
        # existing answer must not pass through the polish prompt — it rewrites sentences the edit
        # was told to preserve (the CURRENT-ANSWER contract). Retries already carry the feedback.
        if (attempt == 1 and raw and not raw.upper().startswith(DECLINE)
                and kind == "essay" and not old_value.strip()):
            try:
                polished = strip_editor_preamble((llm(build_refine_prompt(question, raw, facts) +
                                "\n\nAlso honor this instruction from Sam, within the FACTS: "
                                + args.instruction) or "").strip())
                if polished and not polished.upper().startswith(DECLINE):
                    raw = polished
            except Exception:
                pass

        # ── META-COMMENTARY GUARD (BUG #6) ──
        # The model sometimes returns commentary ABOUT the answer ("'X' doesn't appear in the
        # current answer ... No edit needed.") instead of the answer itself — typically when the
        # edit is a no-op. That commentary is gate-clean (it fabricates nothing) and would land as
        # the new value, corrupting it. Detect it BEFORE the fabrication gate, re-prompt ONCE with a
        # stronger "output ONLY the answer text" instruction, and if the re-prompt STILL trips the
        # guard, KEEP THE PRIOR answer (never overwrite with commentary) and flag it visibly.
        if raw and not raw.upper().startswith(DECLINE) and _is_meta_commentary(raw):
            try:
                reprompted = strip_editor_preamble((llm(build_prompt(question, kind, facts) + instr_line + feedback
                                  + _META_REPROMPT) or "").strip())
            except Exception:  # noqa: BLE001 — treat a re-prompt failure as "still meta"
                reprompted = ""
            if reprompted and not reprompted.upper().startswith(DECLINE) \
                    and not _is_meta_commentary(reprompted):
                # The re-prompt produced a real answer — proceed with it through the normal path.
                raw = reprompted
            else:
                # Still commentary (or empty/declined): do NOT land it. Preserve the prior answer
                # and flag the record so Sam sees it — never silently store commentary.
                new_value = ""
                new_status_field = "needs_input"
                new_reason = ("regen_produced_commentary: the rewrite returned commentary about the "
                              "answer instead of the answer itself; prior answer kept")
                preserve_prior = True   # keep the existing value — do NOT wipe to "" like a decline
                blocked_draft = ""
                prev_text = ""
                if max_attempts > 1:
                    residual = {
                        "class": iterate_fix.UNSUPPORTABLE,
                        "issue": new_reason,
                        "offending_text": "",
                        "attempts": attempts_used,
                        "block_reason": new_reason,
                    }
                break

        if not raw or raw.upper().startswith(DECLINE):
            # The model declined — it could not satisfy the edit within the facts. Terminal; no
            # value lands. A decline is the model telling us the premise is unsupportable, so on
            # N>1 it classifies as an `unsupportable` residual (asking Sam won't ground it).
            new_value = ""
            new_status_field = "needs_input"
            new_reason = "requested edit could not be satisfied within the supported facts"
            blocked_draft = ""
            prev_text = ""
            if max_attempts > 1:
                residual = {
                    "class": iterate_fix.UNSUPPORTABLE,
                    "issue": new_reason,
                    "offending_text": "",
                    "attempts": attempts_used,
                    "block_reason": new_reason,
                }
            break

        try:
            blocks = audit(raw) or []
        except Exception as e:  # noqa: BLE001 — fail safe to blocked
            blocks = [f"audit error: {e!r}"]

        if not blocks:
            # FABRICATION/DISCLOSURE GATE PASSED. If a G2 length target was passed (engine-own
            # length-fix path), the answer must ALSO be within the stated word range before it counts
            # as clean — the hard floor (fabrication + disclosure) stays first, length second, so a
            # lengthened answer that introduced a violation never gets here. An in-range answer lands;
            # an out-of-range one is re-prompted to grow/shrink (within budget), and on the final
            # attempt stamps a `length_unmet` residual (NOT human_only — a length miss is engine-
            # reachable, just not with these facts).
            from .compliance import count_words
            wc = count_words(raw)
            under = args.min_words is not None and wc < args.min_words
            over = args.max_words is not None and wc > args.max_words
            if under or over:
                prev_text = raw
                if attempt < max_attempts:
                    feedback = iterate_fix.length_feedback_clause(
                        raw, wc, args.min_words, args.max_words, ledger_facts=ledger)
                    continue
                # Final attempt still out of range: LAND the best (gate-clean) draft so the answer
                # improves, but stamp a length_unmet residual so the convergence loop surfaces the
                # right blocker ("couldn't reach the length") instead of "needs your call".
                new_value = raw
                new_status_field = "drafted"
                new_reason = ""
                new_findings = _ledger_self_audit(raw)
                blocked_draft = ""
                leave_findings = False
                band = (f"{args.min_words}-{args.max_words}" if args.min_words is not None
                        and args.max_words is not None else
                        (f"min {args.min_words}" if under else f"max {args.max_words}"))
                residual = {
                    "class": iterate_fix.LENGTH_UNMET,
                    "issue": (f"answer is {wc} words; could not reach the required {band} "
                              "with supported facts"),
                    "offending_text": "",
                    "attempts": attempts_used,
                    "block_reason": f"length {wc} words outside {band}",
                }
                break
            # CLEAN: the deterministic gate passed (and length is in range / unconstrained). Run the
            # ledger self-audit for review_findings and land the value. (review_findings are FLAG/
            # advisory; they never block — unchanged from the single-pass behaviour.)
            new_value = raw
            new_status_field = "drafted"
            new_reason = ""
            new_findings = _ledger_self_audit(raw)
            blocked_draft = ""
            leave_findings = False
            break

        # BLOCKED this attempt. Capture the specifics for the feedback clause + (possible) residual.
        last_blocks = list(blocks)
        last_findings = _ledger_self_audit(raw) if max_attempts > 1 else []
        blocked_draft = raw
        block_reason = "; ".join(blocks)[:300]
        leave_findings = True
        prev_text = raw

        if attempt < max_attempts:
            # Re-prompt with the gate's specific complaint about THIS attempt + the supported ledger
            # facts, and regen again. Converge by REMOVAL — the clause says reword/remove, never invent.
            feedback = iterate_fix.feedback_clause(prev_text, last_blocks, last_findings,
                                                   ledger_facts=ledger)
            continue
        # Final attempt still blocked → classify the residual (N>1 only; N=1 keeps legacy block path
        # with residual=None). Pick the richest finding (a structured ledger finding if any, else a
        # synthetic one from the gate notes) to classify.
        if max_attempts > 1:
            residual_finding = (last_findings[0] if last_findings else
                                {"offending_text": "", "issue": block_reason, "fix": ""})
            cls = iterate_fix.classify_residual(residual_finding, ledger_facts=ledger, llm=llm)
            residual = {
                "class": cls,
                "issue": residual_finding.get("issue", "") or block_reason,
                "offending_text": residual_finding.get("offending_text", ""),
                "attempts": attempts_used,
                "block_reason": block_reason,
            }

    # ── Apply the computed outcome onto the FRESH record under the mutex (Part 2) ──
    def _mutate(fresh_app, fresh_target):
        if fresh_app is None or fresh_target is None:
            return "missing"
        # before = the value as it stands FRESH right now (a sibling edit can't move THIS
        # question — the per-element launch lock prevents two edits to the same qkey — so this
        # equals old_value in practice, but reading fresh keeps us honest if state changed).
        fresh_before = fresh_target.get("value", "") or ""

        if not blocked_draft:
            # clean OR needs_input: set status/reason from the computed outcome. The VALUE is set
            # from new_value UNLESS preserve_prior is set (BUG #6 meta-commentary guard), in which
            # case the existing value stands — we must never overwrite a real answer with the empty
            # placeholder the way the decline path intentionally does.
            if not preserve_prior:
                fresh_target["value"] = new_value
            fresh_target["status"] = new_status_field
            fresh_target["reason"] = new_reason
        # else (blocked): leave value/status/reason exactly as they are on disk — original stands.

        if not leave_findings:
            fresh_target["review_findings"] = new_findings

        # CONTRACT: edit_request means "edit IN FLIGHT". This regen COMPLETED — clear it so the
        # submit gate releases. The instruction lives on in the edit_history row below.
        fresh_target["edit_request"] = ""

        # Reflect the fix in the app-level audit so the dashboard updates WITHOUT a full re-audit.
        # Only meaningful when a new value actually landed.
        if not blocked_draft and new_value:
            _prune_stale_findings(fresh_app, question, new_value)

        # The classified residual (N>1 exhausted still-blocked / unsupportable decline): stamp it on
        # the answer so the convergence loop + dashboard surface the RIGHT blocker (human_only =>
        # answerable; unsupportable => rewrite-or-drop) instead of a blunt "stalled". On a clean
        # landing there is no residual; clear any stale one.
        if residual is not None:
            fresh_target["residual"] = residual
        elif not blocked_draft and new_value:
            fresh_target.pop("residual", None)

        # Record the edit in edit_history (APPEND to the fresh list — never replace; a concurrent
        # sibling-question edit may have appended its own row). Three terminal shapes; the
        # HISTORY-row status literal ("edited" on a clean edit) is what the revert gates key on.
        if blocked_draft:
            row = {
                "ts": _local_iso(),
                "instruction": args.instruction,
                "before": fresh_before,
                "after": blocked_draft,
                "status": "blocked",
                "reason": block_reason,
            }
            if residual is not None:
                row["residual"] = residual
                row["attempts"] = attempts_used
            fresh_target.setdefault("edit_history", []).append(row)
        elif new_value:
            fresh_target.setdefault("edit_history", []).append({
                "ts": _local_iso(),
                "instruction": args.instruction,
                "before": fresh_before,
                "after": new_value,
                "status": "edited",
            })
        else:
            row = {
                "ts": _local_iso(),
                "instruction": args.instruction,
                "before": fresh_before,
                "after": "",
                "status": "needs_input",
            }
            if residual is not None:
                row["residual"] = residual
                row["attempts"] = attempts_used
            fresh_target.setdefault("edit_history", []).append(row)
        _ledger_answer(fresh_app, question, fresh_target["edit_history"][-1])

        # Recompute the record's status from its updated FRESH state. One-way valve.
        ns = recompute_status(fresh_app)
        if ns and ns != (fresh_app.get("status") or ""):
            fresh_app["status"] = ns

        return "blocked (original kept)" if blocked_draft else fresh_target["status"]

    out_status = _merge_write(manifest, args.job_id, _mutate, qkey=qk)
    if out_status == "missing":
        print(f"question disappeared from {args.job_id} during edit — nothing written")
        return 1
    suffix = ""
    if residual is not None:
        suffix = f" residual={residual['class']} attempts={attempts_used}"
    print(f"regenerated answer for {args.job_id}: status={out_status}{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
