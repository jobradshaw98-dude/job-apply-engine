"""Tests for the qualify CORE (qualify_one / run_qualify).

Every heavy external is injected, so these run fully offline: no network, no
browser, no `claude` shell-out, no real data hub. We assert the four contract
behaviours the pipeline depends on:
  * PROMOTE on pass (enriched stub -> jobs.json record with a fresh JOB-NNN),
  * HOLD on fail (thin/unresolved stub stays in holding for next run),
  * PRUNE after MAX_ATTEMPTS dead tries (no JD AND no live URL) — and ONLY then,
  * NEVER drop for low fit (a low score still promotes; pruning is dead-link-only),
and that the default LLM scorer is never invoked under injection (no shell-out).
"""
import json

import pytest

from apply_engine import config
from apply_engine.qualify import qualify as Q


# ── injectable stubs (deterministic, offline) ────────────────────────────────

def _resolve_none(job):
    """A resolver that never finds a direct URL (fail closed)."""
    return None


def _resolve_to(url):
    def _r(job):
        return url
    return _r


def _fetch_none(url):
    return None


def _fetch_full(url):
    """Return a JD comfortably over the enrichment floor."""
    return "Full job description. " * 120  # ~2640 chars > MIN_JD_CHARS


def _score_stub(value=8, reason="stub band"):
    calls = []

    def _s(job):
        calls.append(job)
        return {"fit_score": value, "reason": reason}
    _s.calls = calls
    return _s


def _enriched_stub(**over):
    h = {
        "id": "HOLD-1",
        "title": "Forward Deployed Engineer",
        "company": "Acme",
        "url": "https://job-boards.greenhouse.io/acme/jobs/123",
        "jd_text": "Full JD. " * 200,  # well over the floor
        "location": "Remote",
        "source": "discover",
        "date_added": "2026-01-01",
    }
    h.update(over)
    return h


# ── PROMOTE on pass ───────────────────────────────────────────────────────────

def test_qualify_one_promotes_enriched_stub():
    h = _enriched_stub()
    jobs = []
    score = _score_stub(value=9, reason="bullseye")
    outcome, rec = Q.qualify_one(
        h, jobs, resolve_url=_resolve_none, fetch_jd=_fetch_none, score=score)
    assert outcome == "promoted"
    assert rec["id"] == "JOB-001"           # fresh canonical id
    assert rec["company"] == "Acme"
    assert rec["fit_score"] == 9 and rec["fit_reason"] == "bullseye"
    assert rec["status"] == "Spotted"
    assert rec["url"].endswith("/jobs/123") and rec["apply_url"] == rec["url"]
    assert len(score.calls) == 1            # scored exactly once


def test_promote_allocates_next_id_after_existing():
    jobs = [{"id": "JOB-007"}, {"id": "JOB-003"}]
    outcome, rec = Q.qualify_one(
        _enriched_stub(), jobs, resolve_url=_resolve_none, fetch_jd=_fetch_none,
        score=_score_stub())
    assert outcome == "promoted" and rec["id"] == "JOB-008"  # max+1, zero-padded


def test_qualify_one_resolves_then_fetches_when_thin():
    # Stub starts with NO url and NO jd; resolve supplies a URL, fetch supplies a JD,
    # then it should pass the gate and promote.
    h = {"title": "Applied AI Engineer", "company": "Globex", "url": "", "jd_text": ""}
    outcome, rec = Q.qualify_one(
        h, [], resolve_url=_resolve_to("https://jobs.lever.co/globex/abc-uuid-1234-5678-9012"),
        fetch_jd=_fetch_full, score=_score_stub(value=7))
    assert outcome == "promoted"
    assert "lever.co/globex" in rec["url"]
    assert len(rec["jd_text"]) >= Q.MIN_JD_CHARS


# ── NEVER drop for low fit ────────────────────────────────────────────────────

def test_low_fit_still_promotes_never_dropped():
    # A rock-bottom fit score must STILL promote — pruning is for dead links only.
    h = _enriched_stub()
    outcome, rec = Q.qualify_one(
        h, [], resolve_url=_resolve_none, fetch_jd=_fetch_none,
        score=_score_stub(value=1, reason="off-target"))
    assert outcome == "promoted"
    assert rec["fit_score"] == 1            # kept, not pruned


# ── HOLD on fail ──────────────────────────────────────────────────────────────

def test_hold_when_unenrichable_but_has_url():
    # Thin JD, can't fetch a better one, but a live URL remains -> HOLD (retry), not prune.
    h = {"title": "X", "company": "Y",
         "url": "https://job-boards.greenhouse.io/y/jobs/9", "jd_text": "thin"}
    outcome, rec = Q.qualify_one(
        h, [], resolve_url=_resolve_none, fetch_jd=_fetch_none, score=_score_stub())
    assert outcome == "hold" and rec is None
    assert h["attempts"] == 1               # attempt counter bumped


def test_hold_increments_attempts_each_run():
    h = {"title": "X", "company": "Y", "url": "https://x/jobs/9", "jd_text": "thin"}
    for expected in (1, 2):
        outcome, _ = Q.qualify_one(
            h, [], resolve_url=_resolve_none, fetch_jd=_fetch_none, score=_score_stub())
        assert outcome == "hold" and h["attempts"] == expected


# ── PRUNE only after MAX_ATTEMPTS AND no live URL ────────────────────────────

def test_prune_after_max_attempts_with_no_url():
    # No URL, no recoverable JD, already at the attempt ceiling -> dead -> prune.
    h = {"title": "X", "company": "Y", "url": "", "jd_text": "",
         "attempts": Q.MAX_ATTEMPTS - 1}
    outcome, rec = Q.qualify_one(
        h, [], resolve_url=_resolve_none, fetch_jd=_fetch_none, score=_score_stub())
    assert outcome == "prune" and rec is None
    assert h["attempts"] == Q.MAX_ATTEMPTS


def test_no_prune_at_max_attempts_if_url_present():
    # At the ceiling but a live URL remains -> still HOLD (a resolvable link isn't dead).
    h = {"title": "X", "company": "Y", "url": "https://x/jobs/9", "jd_text": "",
         "attempts": Q.MAX_ATTEMPTS - 1}
    outcome, _ = Q.qualify_one(
        h, [], resolve_url=_resolve_none, fetch_jd=_fetch_none, score=_score_stub())
    assert outcome == "hold"                # url present => never pruned


# ── the default LLM scorer is never reached under injection (no shell-out) ────

def test_injected_score_means_no_claude_shellout(monkeypatch):
    # If the default scorer (which shells out to `claude -p`) were ever reached, it
    # would call subprocess.run; assert that injecting a stub bypasses it entirely.
    import subprocess
    def _boom(*a, **k):
        raise AssertionError("qualify must not shell out when a score stub is injected")
    monkeypatch.setattr(subprocess, "run", _boom)
    outcome, rec = Q.qualify_one(
        _enriched_stub(), [], resolve_url=_resolve_none, fetch_jd=_fetch_none,
        score=_score_stub())
    assert outcome == "promoted"            # scored via the stub, never via claude


def test_default_score_raises_without_claude(monkeypatch):
    # Belt-and-suspenders: the default scorer FAILS LOUD (never silently bills an API)
    # when the Claude CLI is absent — and never falls through to a network call.
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="claude"):
        Q._default_score({"title": "X", "company": "Y", "jd_text": "z"})


# ── run_qualify: drains holding, promotes to jobs.json, persists atomically ───

def _seed(tmp_path, monkeypatch, holding, jobs=None):
    """Point config at a throwaway data dir and seed holding/jobs files."""
    monkeypatch.setattr(config, "ARIA_DATA", tmp_path)
    hp = tmp_path / "holding.json"
    jp = tmp_path / "jobs.json"
    monkeypatch.setattr(config, "HOLDING_JSON", hp)
    monkeypatch.setattr(config, "JOBS_JSON", jp)
    hp.write_text(json.dumps(holding), encoding="utf-8")
    if jobs is not None:
        jp.write_text(json.dumps(jobs), encoding="utf-8")
    return hp, jp


def test_run_qualify_promotes_and_rewrites_holding(tmp_path, monkeypatch):
    holding = [
        _enriched_stub(id="HOLD-1"),                                  # -> promote
        {"id": "HOLD-2", "title": "X", "company": "Y",               # -> hold
         "url": "https://x/jobs/9", "jd_text": "thin"},
    ]
    hp, jp = _seed(tmp_path, monkeypatch, holding)
    summary = Q.run_qualify(
        resolve_url=_resolve_none, fetch_jd=_fetch_none, score=_score_stub(value=6))
    assert summary["promoted"] == 1 and summary["held"] == 1 and summary["pruned"] == 0
    assert summary["promoted_ids"] == ["JOB-001"]
    # jobs.json got exactly the one promoted record...
    jobs_out = json.loads(jp.read_text(encoding="utf-8"))
    assert [j["id"] for j in jobs_out] == ["JOB-001"]
    # ...and holding now holds ONLY the held stub (the promoted one left).
    held_out = json.loads(hp.read_text(encoding="utf-8"))
    assert [h["id"] for h in held_out] == ["HOLD-2"]


def test_run_qualify_prunes_dead_stub(tmp_path, monkeypatch):
    holding = [{"id": "HOLD-D", "title": "X", "company": "Y", "url": "", "jd_text": "",
                "attempts": Q.MAX_ATTEMPTS - 1}]
    hp, jp = _seed(tmp_path, monkeypatch, holding)
    summary = Q.run_qualify(
        resolve_url=_resolve_none, fetch_jd=_fetch_none, score=_score_stub())
    assert summary["pruned"] == 1 and summary["promoted"] == 0
    assert json.loads(hp.read_text(encoding="utf-8")) == []   # dead stub dropped


def test_run_qualify_dry_run_writes_nothing(tmp_path, monkeypatch):
    holding = [_enriched_stub(id="HOLD-1")]
    hp, jp = _seed(tmp_path, monkeypatch, holding)
    before = hp.read_text(encoding="utf-8")
    summary = Q.run_qualify(
        dry_run=True, resolve_url=_resolve_none, fetch_jd=_fetch_none, score=_score_stub())
    assert summary["promoted"] == 1            # computed...
    assert hp.read_text(encoding="utf-8") == before   # ...but holding untouched
    assert not jp.exists()                     # and no jobs.json written


def test_run_qualify_respects_cap(tmp_path, monkeypatch):
    holding = [_enriched_stub(id=f"HOLD-{i}") for i in range(3)]
    hp, jp = _seed(tmp_path, monkeypatch, holding)
    summary = Q.run_qualify(
        cap=1, resolve_url=_resolve_none, fetch_jd=_fetch_none, score=_score_stub())
    assert summary["promoted"] == 1
    # the two un-processed stubs are preserved in holding (cap doesn't drop them).
    held_out = json.loads(hp.read_text(encoding="utf-8"))
    assert {h["id"] for h in held_out} == {"HOLD-1", "HOLD-2"}


def test_run_qualify_one_bad_stub_does_not_abort(tmp_path, monkeypatch):
    holding = ["not-a-dict", _enriched_stub(id="HOLD-OK")]
    hp, jp = _seed(tmp_path, monkeypatch, holding)
    summary = Q.run_qualify(
        resolve_url=_resolve_none, fetch_jd=_fetch_none, score=_score_stub())
    assert summary["promoted"] == 1 and summary["errors"] == 1
    assert [j["id"] for j in json.loads(jp.read_text(encoding="utf-8"))] == ["JOB-001"]
