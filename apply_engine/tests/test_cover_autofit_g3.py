# -*- coding: utf-8 -*-
"""G3 — cover auto-fit count plumbing: build.py emits the count, tailor parses + stores it.

The build.py render emits the cover auto-fit adjustment count two redundant ways:
  1. a sidecar `<app_dir>/cover_render.json` {"autofit_adjustments": n, "pages": p}
  2. an `AUTOFIT_ADJUSTMENTS=<n>` stdout line.

tailor._parse_autofit_adjustments recovers N from either channel (sidecar first), returns None when
neither is present (PASS-WHEN-ABSENT — the gate stays absent-friendly), and _store_autofit_adjustments
writes it onto the APP record's cover dict where finish._g3_cover_ok reads it. These tests pin the
COUNT-PARSING deterministically against sample build output — no real Edge render required.
"""
import json


from apply_engine import tailor


# ======================================================================================
# _parse_autofit_adjustments — sidecar + stdout + absent
# ======================================================================================

def test_parse_from_stdout_line():
    """A build stdout carrying the AUTOFIT_ADJUSTMENTS= marker -> N extracted (no sidecar present)."""
    stdout = ("=== COVER LETTER: APP-001 ===\n"
              "  PDF: x.pdf\n  Pages: 1\n"
              "AUTOFIT_ADJUSTMENTS=3\n  All checks passed.\n")
    # no app dir resolvable -> sidecar path returns nothing, falls through to the stdout marker
    assert tailor._parse_autofit_adjustments("APP-NONE", stdout) == 3


def test_parse_zero_from_stdout():
    stdout = "AUTOFIT_ADJUSTMENTS=0\n"
    assert tailor._parse_autofit_adjustments("APP-NONE", stdout) == 0


def test_parse_returns_none_when_absent():
    """No sidecar and no marker line -> None (pass-when-absent; the caller stores nothing)."""
    stdout = "=== COVER LETTER: APP-001 ===\n  PDF: x.pdf\n  Pages: 1\n  All checks passed.\n"
    assert tailor._parse_autofit_adjustments("APP-NONE", stdout) is None
    assert tailor._parse_autofit_adjustments("APP-NONE", "") is None


def test_sidecar_wins_over_stdout(tmp_path, monkeypatch):
    """When both channels are present, the sidecar (authoritative) is read."""
    appdir = tmp_path / "applications" / "APP-002-Acme"
    appdir.mkdir(parents=True)
    (appdir / "cover_render.json").write_text(
        json.dumps({"autofit_adjustments": 2, "pages": 1}), encoding="utf-8")
    monkeypatch.setattr(tailor, "_app_tailored_dir", lambda app_id: appdir)
    # stdout disagrees (says 5); the sidecar's 2 must win
    n = tailor._parse_autofit_adjustments("APP-002", "AUTOFIT_ADJUSTMENTS=5\n")
    assert n == 2


def test_sidecar_only(tmp_path, monkeypatch):
    appdir = tmp_path / "applications" / "APP-003-Acme"
    appdir.mkdir(parents=True)
    (appdir / "cover_render.json").write_text(
        json.dumps({"autofit_adjustments": 0, "pages": 1}), encoding="utf-8")
    monkeypatch.setattr(tailor, "_app_tailored_dir", lambda app_id: appdir)
    assert tailor._parse_autofit_adjustments("APP-003", "") == 0


# ======================================================================================
# _store_autofit_adjustments — writes the key finish._g3_cover_ok reads, then the gate fires
# ======================================================================================

def test_store_and_g3_gate_end_to_end(tmp_path, monkeypatch):
    """Store a >0 count onto an APP record's cover dict, then assert finish._g3_cover_ok reads that
    exact key and FAILS — proving the build->tailor->record->gate chain lines up."""
    from apply_engine.finish import _g3_cover_ok

    apps_path = tmp_path / "applications.json"
    apps_path.write_text(json.dumps([
        {"id": "APP-010", "job_id": "JOB-X", "company": "Acme",
         "cover": {"paragraphs": ["p1", "p2"]}},
    ]), encoding="utf-8")

    # point tailor's config + filemutex at the throwaway tree
    monkeypatch.setattr(tailor.config, "APPLICATIONS_JSON", apps_path)
    monkeypatch.setattr(tailor, "_require_filemutex", lambda: (lambda p: _NullLock()))

    tailor._store_autofit_adjustments("APP-010", 3)

    rec = json.loads(apps_path.read_text(encoding="utf-8"))[0]
    assert rec["cover"]["autofit_adjustments"] == 3
    # the gate finish._g3_cover_ok reads this exact key -> FAIL on >0
    ok, reason = _g3_cover_ok(rec)
    assert ok is False
    assert "g3" in reason.lower() or "cover" in reason.lower()


def test_store_zero_passes_gate(tmp_path, monkeypatch):
    from apply_engine.finish import _g3_cover_ok

    apps_path = tmp_path / "applications.json"
    apps_path.write_text(json.dumps([
        {"id": "APP-011", "job_id": "JOB-Y", "company": "Acme", "cover": {"paragraphs": ["p"]}},
    ]), encoding="utf-8")
    monkeypatch.setattr(tailor.config, "APPLICATIONS_JSON", apps_path)
    monkeypatch.setattr(tailor, "_require_filemutex", lambda: (lambda p: _NullLock()))

    tailor._store_autofit_adjustments("APP-011", 0)
    rec = json.loads(apps_path.read_text(encoding="utf-8"))[0]
    assert rec["cover"]["autofit_adjustments"] == 0
    assert _g3_cover_ok(rec)[0] is True


def test_store_creates_cover_dict_when_absent(tmp_path, monkeypatch):
    """A record with no cover dict yet still gets the count (cover dict created)."""
    apps_path = tmp_path / "applications.json"
    apps_path.write_text(json.dumps([{"id": "APP-012", "job_id": "JOB-Z", "company": "Acme"}]),
                         encoding="utf-8")
    monkeypatch.setattr(tailor.config, "APPLICATIONS_JSON", apps_path)
    monkeypatch.setattr(tailor, "_require_filemutex", lambda: (lambda p: _NullLock()))

    tailor._store_autofit_adjustments("APP-012", 1)
    rec = json.loads(apps_path.read_text(encoding="utf-8"))[0]
    assert rec["cover"]["autofit_adjustments"] == 1


class _NullLock:
    """A no-op context manager standing in for the filemutex in tests."""
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
