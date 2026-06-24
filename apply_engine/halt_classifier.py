# -*- coding: utf-8 -*-
"""Structured halt classifier (Feature B, Phase 1 — data model only).

When the orchestrator HALTs a run it sets `out.status` + `out.halt_reason` (a human
sentence). Those stay untouched for backward-compat. ON TOP of them, this module builds a
structured `human_blocker` record (the §1b schema of
`career/docs/superpowers/AUTONOMOUS_CONVERGENCE_AND_COMM_CHANNEL.md`) that tells the dashboard
*what kind of answer would unblock this* — so a future comm channel can render a real control
(answerable tier) or route to a watched run (escalate tier).

PHASE 1 IS ADDITIVE: nothing here changes run behavior. The orchestrator merely attaches the
returned dict to `out.human_blocker`; `build_record` carries it onto the flat manifest record.
No notification, no convergence loop, no dashboard render — those are later phases.

Classifier discipline (HARD, from feedback_apply_engine_live_dom_and_empty_guard):
  * A FAILED WIDGET SET is ALWAYS `escalate` (category `unknown_widget`) — a value the user types
    can't fix a DOM the engine couldn't drive. NEVER `answerable`.
  * `escalate` tier ALWAYS sets `answer_target.kind="none"` (no provide-answer box) and carries
    `code_context` (file:line) + screenshot + page_state so the watched-MCP fallback lands with
    full context.
  * `answerable` tier carries `options` / `free_text_ok` / a real `answer_target` (kind+qkey) so
    the answer maps back through the existing /provide-answer route's _qkey normalization.

The qkey normalization MUST match the /provide-answer route + regen_answer._qkey EXACTLY:
  "".join(c for c in s.lower() if c.isalnum())[:70]
"""
import re
from pathlib import Path

# A needs_sam / unfilled item that is an internal field key, not a human question, has no
# answer the user can supply from the dashboard -> escalate, not answerable. Mirrors the dashboard's
# aria_server._is_raw_field_key intent: a bare token (UUID / snake_case id / cards[...] name) with
# no space and no '?'. Kept here so the engine doesn't depend on the server.
_RAW_FIELD_RE = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f-]+|question_\d+|cards\[.*\]|[a-z][a-z0-9_]*(?:--[a-z0-9_]+)*)$", re.I)


def is_raw_field_key(text) -> bool:
    """True when `text` is an internal field key (no human question) rather than a real labeled
    question. Such an unfilled field is a perception/engine miss -> escalate, never answerable."""
    t = (text or "").strip()
    if not t or " " in t or "?" in t:
        return False
    return bool(_RAW_FIELD_RE.match(t))


# Tier per category (§4 table). escalate = needs perception/improvisation (route to watched MCP);
# answerable = a fact/value the user can supply via the dashboard.
_ESCALATE = {"unknown_widget", "captcha", "file_upload", "zero_fields", "render_fail"}
_ANSWERABLE = {"screening_yesno", "missing_value", "city", "work_auth",
               "calibration_unfixable", "unverifiable_claim"}

# answer_target.kind per answerable category (§4). escalate categories force kind="none".
_TARGET_KIND = {
    "screening_yesno": "custom_q",
    "missing_value": "needs_sam",
    "city": "needs_sam",
    "work_auth": "needs_sam",
    "calibration_unfixable": "custom_q",
    "unverifiable_claim": "custom_q",
}


def _qkey(s):
    """EXACT match to aria_server.py /provide-answer + regen_answer._qkey (alnum, lower, [:70])."""
    return "".join(c for c in (s or "").lower() if c.isalnum())[:70]


def _latest_halt_shot(ctx) -> str:
    """Basename of the most recent screenshot in the run dir (the halt sites all capture
    `_shot(page, ctx, "halt")` right before classifying, so the newest step_*.png IS the halt
    shot). Returns '' if none/absent — never raises (best-effort)."""
    try:
        rd = Path(getattr(ctx, "run_dir", "") or "")
        if not rd.is_dir():
            return ""
        shots = sorted(p.name for p in rd.glob("step_*_*.png"))
        return shots[-1] if shots else ""
    except Exception:
        return ""


def _page_state(page, ats: str, reached: str, fields_filled) -> dict:
    """Best-effort snapshot of where the run was. Reading page.url can raise on a closed page,
    so it's guarded — page_state is context, never load-bearing."""
    url = ""
    try:
        url = page.url if page is not None else ""
    except Exception:
        url = ""
    return {
        "url": url or "",
        "ats": (ats or "") or "",
        "reached": reached or "",
        "fields_filled": int(fields_filled or 0),
    }


def classify_halt(out, page, ctx, *, category: str, halt_ts: str,
                  ats: str = "", reached: str = "", fields_filled: int = 0,
                  question: str = "", options=None, free_text_ok: bool = False,
                  answer_qkey_source: str = "", finding: dict = None,
                  code_source: str = "", code_snippet: str = "") -> dict:
    """Build the §1b `human_blocker` for one halt. Returns the dict (never None for a real halt
    site; callers pass an explicit category so each site produces a distinct, correct blocker).

    Args:
      out: the JobOutcome being halted (its status/halt_reason are already set; read-only here).
      page, ctx: live Playwright page + RunContext (for page_state + the halt screenshot basename).
      category: one of the §4 categories (drives tier + answer_target.kind).
      halt_ts: the halt timestamp string — id is DETERMINISTIC from job_id + this (no fresh random).
      ats/reached/fields_filled: page_state context.
      question: the human-readable question (answerable) or "" (escalate).
      options: list of allowed answers ([] / None when free-text only).
      free_text_ok: whether a free-text answer is accepted.
      answer_qkey_source: the string the answer_target.qkey is normalized from (the live question
        text), so it matches the /provide-answer route's record-side _qkey. Defaults to `question`.
      finding: the audit finding dict for gate-block halts (else None).
      code_source/code_snippet: file:line + a one-line snippet so the watched path isn't blind.

    Tier discipline: a failed widget set (category unknown_widget) is escalate — never answerable.
    """
    job_id = getattr(out, "job_id", "") or ""
    tier = "escalate" if category in _ESCALATE else "answerable"

    # answer_target: escalate -> kind="none", qkey="" (no provide box). answerable -> kind per
    # category + a qkey normalized EXACTLY like the /provide-answer route so the answer maps back.
    if tier == "escalate":
        target_kind = "none"
        qkey = ""
    else:
        target_kind = _TARGET_KIND.get(category, "needs_sam")
        qkey = _qkey(answer_qkey_source or question)

    # Deterministic id: job_id + the (caller-supplied, real) halt timestamp, digits of the
    # DATETIME only (the trailing ±HH:MM tz offset is stripped first so the id is the wall-clock
    # stamp, not date+offset). Stable across re-derivation, never a fresh random — so tests can
    # assert it and a re-staged card's new blocker is keyed by its OWN halt time.
    ts = halt_ts or ""
    ts = re.sub(r"[+-]\d{2}:?\d{2}$", "", ts)  # drop a trailing tz offset (+HH:MM / -HH:MM)
    ts_compact = "".join(ch for ch in ts if ch.isdigit())
    blk_id = f"blk_{job_id}_{ts_compact}" if ts_compact else f"blk_{job_id}"

    return {
        "id": blk_id,
        "tier": tier,
        "category": category,
        "blocking_reason": getattr(out, "halt_reason", "") or "",
        "question": question or "",
        "options": list(options or []),
        "free_text_ok": bool(free_text_ok),
        "answer_target": {"kind": target_kind, "qkey": qkey},
        "screenshot": _latest_halt_shot(ctx),
        "page_state": _page_state(page, ats, reached, fields_filled),
        "finding": finding if isinstance(finding, dict) else None,
        "code_context": {"source": code_source or "", "snippet": code_snippet or ""},
        "created_at": halt_ts or "",
        "answered_at": None,
        "notified": {"telegram": False, "dashboard_badge": False},
    }
