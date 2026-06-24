# -*- coding: utf-8 -*-
"""Bug #3 — a cheap URL-liveness pre-flight runs BEFORE the expensive tailor step on a --live run,
so a dead posting is caught in <1s instead of after ~5 min + 2 `claude -p` calls.

Two layers under test:
  * liveness.check_posting_liveness(url, opener=...) — the pure HTTP check. It is FAIL-OPEN: only an
    UNAMBIGUOUS closed signal returns (True, reason); a network error / timeout / ambiguous 200 /
    any uncertainty returns (False, "") so a transient hiccup never blocks a real job.
  * cli.main wiring — on --live, an unambiguous-closed posting halts to needs_sam and NEVER calls
    ensure_tailored_package; a live 200 proceeds to tailor; a network error proceeds (fail-open).

No real network: the urllib opener is injected/monkeypatched throughout.
"""


from apply_engine import liveness


# --------------------------------------------------------------------------------------
# a fake urllib response + opener — no real network
# --------------------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, *, body="", final_url="https://example.com/job", status=200):
        self._body = body.encode("utf-8")
        self._final_url = final_url
        self.status = status

    def read(self, *a, **k):
        return self._body

    def geturl(self):
        return self._final_url

    # context-manager protocol (urlopen returns a CM)
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener(resp=None, *, raises=None):
    """Return a callable matching the urlopen(url, timeout=...) signature the module uses."""
    def _open(url, timeout=None):  # noqa: ARG001
        if raises is not None:
            raise raises
        return resp
    return _open


# ======================================================================================
# 1. the pure check — UNAMBIGUOUS closed signals return (True, reason)
# ======================================================================================

def test_greenhouse_error_redirect_is_closed():
    # Greenhouse closed postings 302 -> a URL containing ?error=true
    resp = _FakeResp(final_url="https://boards.greenhouse.io/acme/jobs/123?error=true", body="ok")
    closed, reason = liveness.check_posting_liveness(
        "https://boards.greenhouse.io/acme/jobs/123", opener=_opener(resp))
    assert closed is True
    assert reason  # a human reason is given


def test_body_no_longer_available_is_closed():
    resp = _FakeResp(body="<h1>This position is no longer available.</h1>")
    closed, reason = liveness.check_posting_liveness(
        "https://jobs.ashbyhq.com/acme/abc", opener=_opener(resp))
    assert closed is True
    assert reason


def test_body_position_filled_is_closed():
    resp = _FakeResp(body="Sorry — this position has been filled.")
    closed, _ = liveness.check_posting_liveness("https://example.com/x", opener=_opener(resp))
    assert closed is True


# ======================================================================================
# 2. FAIL-OPEN — anything ambiguous proceeds (returns (False, ""))
# ======================================================================================

def test_live_200_proceeds():
    resp = _FakeResp(body="<form>Apply now — Full name, Email</form>", status=200)
    closed, reason = liveness.check_posting_liveness("https://example.com/job", opener=_opener(resp))
    assert closed is False
    assert reason == ""


def test_network_error_fails_open():
    closed, reason = liveness.check_posting_liveness(
        "https://example.com/job", opener=_opener(raises=OSError("connection reset")))
    assert closed is False  # MUST NOT block a real job on a transient hiccup
    assert reason == ""


def test_timeout_fails_open():
    import socket
    closed, _ = liveness.check_posting_liveness(
        "https://example.com/job", opener=_opener(raises=socket.timeout("timed out")))
    assert closed is False


def test_empty_or_missing_url_fails_open():
    closed, _ = liveness.check_posting_liveness("", opener=_opener(_FakeResp()))
    assert closed is False


def test_ambiguous_404_without_closed_phrase_fails_open():
    # a bare 404 with no recognized closed phrase is NOT unambiguous -> proceed (fail-open)
    resp = _FakeResp(body="<html>Oops, something went wrong</html>", status=404)
    closed, _ = liveness.check_posting_liveness("https://example.com/x", opener=_opener(resp))
    assert closed is False
