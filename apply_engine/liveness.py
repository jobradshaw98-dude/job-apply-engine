# -*- coding: utf-8 -*-
"""Cheap URL-liveness pre-flight (Bug #3).

A `--live` apply run used to spend ~5 min + 2 `claude -p` calls TAILORING a package and only THEN
open the page and HALT "posting is closed". This module does a <1s HTTP GET BEFORE tailoring so an
obviously-dead posting is caught up front and never burns the tailor quota.

CONTRACT — FAIL-OPEN. This must NEVER block a real job on a transient problem. Only an UNAMBIGUOUS
closed signal returns (True, reason); a network error, timeout, ambiguous 200/404, redirect we
don't recognize, or ANY uncertainty returns (False, "") so the run proceeds to tailor as normal. A
flaky network must read as "live".

Unambiguous closed signals:
  * Greenhouse closed postings 302-redirect to a URL containing `?error=true` (the final URL after
    redirects carries it).
  * The rendered body contains one of orchestrator.CLOSED_BODY_SIGNALS ("no longer available",
    "position has been filled", "job not found", "this posting is closed", ...).

The closed body phrases are SHARED with orchestrator._classify_no_form (CLOSED_BODY_SIGNALS) so the
up-front pre-flight and the mid-run detection agree on what "closed" means.
"""
import urllib.request
from typing import Callable, Optional, Tuple

from .orchestrator import CLOSED_BODY_SIGNALS

# A browser-ish UA — some ATSes return a bare/blocked page to the default urllib UA, which would
# look like a (fail-open) ambiguous page rather than the real posting. We only ACT on closed
# signals, so a block still fails open; the UA just makes the live/closed read more accurate.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Cap the body read so a huge page can't stall the pre-flight (we only scan for short phrases).
_MAX_BYTES = 200_000


def _default_opener(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    return urllib.request.urlopen(req, timeout=timeout)


def check_posting_liveness(
    url: str,
    *,
    opener: Optional[Callable] = None,
    timeout: float = 8.0,
) -> Tuple[bool, str]:
    """Return (is_closed, human_reason). FAIL-OPEN: (False, "") on any error/timeout/ambiguity.

    `opener(url, timeout=...)` is injectable for tests (defaults to a real urllib GET). It must
    return a response object exposing .read() and .geturl() (and may be a context manager).
    """
    url = (url or "").strip()
    if not url:
        return (False, "")  # nothing to check -> proceed (fail-open)

    open_fn = opener or _default_opener
    try:
        resp = open_fn(url, timeout=timeout)
    except Exception:  # noqa: BLE001 — ANY network/timeout/DNS error fails open (transient hiccup)
        return (False, "")

    # Support both a plain response and a context-manager response (urlopen returns a CM).
    cm = hasattr(resp, "__enter__") and hasattr(resp, "__exit__")
    try:
        r = resp.__enter__() if cm else resp
        try:
            # Signal 1: Greenhouse closed postings land on a final URL carrying ?error=true.
            try:
                final_url = (r.geturl() or "")
            except Exception:  # noqa: BLE001
                final_url = ""
            if "error=true" in final_url.lower():
                return (True, "posting closed — remove/re-source "
                              "(Greenhouse redirected to an error page)")

            # Signal 2: an unambiguous closed phrase in the rendered body.
            try:
                raw = r.read(_MAX_BYTES)
            except TypeError:
                # a fake/opener whose read() takes no size arg
                raw = r.read()
            except Exception:  # noqa: BLE001 — a read failure is ambiguous -> fail open
                return (False, "")
            if isinstance(raw, bytes):
                body = raw.decode("utf-8", errors="ignore")
            else:
                body = str(raw or "")
            body = body.lower()
            if any(sig in body for sig in CLOSED_BODY_SIGNALS):
                return (True, "posting closed — remove/re-source "
                              "(the page says the posting is no longer available)")
        finally:
            if cm:
                resp.__exit__(None, None, None)
    except Exception:  # noqa: BLE001 — any unexpected shape -> fail open
        return (False, "")

    # 200 with no recognized closed signal, an ambiguous 404, a redirect we don't recognize, etc.
    # -> treat as LIVE and proceed to tailor (fail-open).
    return (False, "")
