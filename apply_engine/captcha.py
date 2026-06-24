"""CAPTCHA pre-check — detect a captcha that would block an automated submit.

PURPOSE: the engine NEVER solves a captcha. If a form is captcha-gated, the record
must be routed to Sam for a manual submit (he solves the captcha + clicks submit),
NOT failed hard. So this module's only job is to RECOGNIZE the gate, so two callers can
divert cleanly:

  (a) orchestrator staging — if a captcha is present, the staged record becomes
      needs_sam with a "captcha-gated, manual submit required" reason.
  (b) finish.py replay pre-submit — abort the auto-submit click with the same reason.

CRITICAL distinction — VISIBLE vs INVISIBLE:
  * hCaptcha and a *rendered* (visible) reCAPTCHA challenge ARE blockers — a human must
    solve them. We detect those.
  * INVISIBLE reCAPTCHA (the bare hidden `g-recaptcha-response` textarea that Greenhouse
    and Ashby drop on every form for background scoring) is NOT a blocker — it scores in
    the background and submits without any human interaction. It must return None, or we
    would falsely divert nearly every Greenhouse/Ashby application to manual.

The whole module is best-effort and defensive: any DOM error degrades to None (no false
positive that would needlessly block a clean auto-submit). PURE-ish: reads the live page,
never mutates it.
"""
from typing import Optional


def _is_visible(el) -> bool:
    """True only if Playwright reports the element as visibly rendered. Defensive: a
    detached/odd element that raises is treated as NOT visible (so it can't trip a
    visible-captcha false positive)."""
    try:
        return bool(el.is_visible())
    except Exception:
        return False


def _query(page, selector: str):
    try:
        return page.query_selector_all(selector) or []
    except Exception:
        return []


def _has_recaptcha_v3(page) -> bool:
    """True if the page loads reCAPTCHA **v3** — the score-based variant whose api.js is loaded
    with a `render=<sitekey>` query param (`recaptcha/api.js?render=...`). v3 runs no challenge;
    it SCORES the session in the background and the SITE's backend rejects low-scoring submits.
    A Playwright/automation-driven browser scores low, so a v3 form's submit gets flagged as a bot
    even though there's nothing to "solve" (root cause of the Baseten/Ashby JOB-297 flag,
    2026-06-09; sitekey 6LeFb_…). Distinct from v2-invisible (api.js with NO render=), which
    submits fine. Best-effort: any DOM error → False (never a false block)."""
    for sel in ("script[src*='recaptcha/api.js']", "script[src*='recaptcha.net/recaptcha/api.js']"):
        for el in _query(page, sel):
            try:
                src = (el.get_attribute("src") or "").lower()
            except Exception:
                src = ""
            if "render=" in src and "render=explicit" not in src and "render=onload" not in src:
                return True
    return False


def detect_captcha(page, submit_phase: bool = False) -> Optional[str]:
    """Return the kind of BLOCKING captcha present on the page, or None.

    Returns:
      "hcaptcha"          — an hCaptcha widget is present (always human-gated).
      "recaptcha_visible" — a rendered reCAPTCHA challenge/checkbox widget is visible.
      "recaptcha_v3"      — (submit_phase only) reCAPTCHA v3 score-based detection — automation
                            scores low and the submit gets bot-flagged. No challenge to solve; the
                            only fix is a real human browser. NOT raised during staging (filling to
                            brink is fine; only the auto-SUBMIT click is doomed), so it gates the
                            finish/submit path only.
      None                — no blocking captcha, OR only an INVISIBLE (v2) reCAPTCHA that submits
                            without a human.

    hCaptcha is checked first: its presence is unconditionally a blocker. For reCAPTCHA we
    distinguish a visibly-rendered widget (blocker) from the invisible background variant
    (not a blocker) by requiring the widget/iframe to actually be VISIBLE; and, on the submit
    phase only, treat reCAPTCHA v3 (render=sitekey) as a blocker because its bot-score rejects
    automated submits.
    """
    # ---- hCaptcha: any of the canonical markers is a hard blocker (always interactive) ----
    # .h-captcha is the rendered container; [data-sitekey] is the config div hCaptcha uses;
    # the iframe is the actual challenge. Presence alone gates — hCaptcha has no silent
    # background-scoring mode the engine could ride through.
    for sel in (".h-captcha", "iframe[src*='hcaptcha']", "iframe[src*='hcaptcha.com']"):
        if _query(page, sel):
            return "hcaptcha"
    # [data-sitekey] is hCaptcha's config attribute — but Google's invisible reCAPTCHA ALSO
    # uses data-sitekey on a .g-recaptcha div, so only treat it as hCaptcha when it is NOT a
    # reCAPTCHA node.
    for el in _query(page, "[data-sitekey]"):
        try:
            cls = (el.get_attribute("class") or "").lower()
        except Exception:
            cls = ""
        if "g-recaptcha" in cls or "grecaptcha" in cls:
            continue  # that's reCAPTCHA, judged below by visibility
        if "h-captcha" in cls or el.get_attribute("data-hcaptcha-widget-id") is not None:
            return "hcaptcha"
        # a bare data-sitekey with no class is ambiguous; hCaptcha widgets render .h-captcha,
        # already handled above. Don't guess hCaptcha from data-sitekey alone.

    # ---- reCAPTCHA: VISIBLE rendered challenge is a blocker; invisible textarea is NOT ----
    # A rendered reCAPTCHA shows either a visible .g-recaptcha widget div (the checkbox/v2
    # widget) or a visible challenge iframe. The invisible variant has neither rendered — it
    # only injects a hidden <textarea name="g-recaptcha-response"> for background scoring, plus
    # a 0-size/hidden iframe. So gate strictly on VISIBILITY.
    for el in _query(page, ".g-recaptcha"):
        # data-size="invisible" is the invisible-mode widget — it renders only the floating
        # badge, never a challenge the engine must wait on. Skip it (else every Greenhouse/
        # Ashby form, which all use invisible mode, would falsely divert to manual).
        try:
            if (el.get_attribute("data-size") or "").lower() == "invisible":
                continue
        except Exception:
            pass
        if _is_visible(el):
            return "recaptcha_visible"
    for el in _query(page, "iframe[src*='recaptcha']"):
        # CRITICAL: invisible reCAPTCHA renders a VISIBLE badge/anchor iframe (the "protected by
        # reCAPTCHA" logo) whose src carries `size=invisible`. is_visible() is True for that badge,
        # so visibility ALONE is not enough — gate it out by the src marker. Only a v2 anchor
        # (checkbox) WITHOUT size=invisible, or the bframe challenge popup, is a real human gate.
        try:
            src = (el.get_attribute("src") or "").lower()
        except Exception:
            src = ""
        if "size=invisible" in src:
            continue  # invisible-mode badge/anchor — background scoring, not a blocker
        if _is_visible(el):
            return "recaptcha_visible"

    # reCAPTCHA v3 (score-based) blocks the SUBMIT only — automation scores low and the backend
    # flags it. Staging (submit_phase=False) must NOT divert on this (we still fill to brink).
    if submit_phase and _has_recaptcha_v3(page):
        return "recaptcha_v3"

    # Only an invisible (v2) g-recaptcha-response textarea (or nothing) — NOT a blocker.
    return None
