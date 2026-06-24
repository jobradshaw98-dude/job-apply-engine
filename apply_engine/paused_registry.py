# -*- coding: utf-8 -*-
"""m5 — the kill-switch for the autonomous convergence loop.

Design rule: `paused_registry.json` (in your data hub, resolved via config.ARIA_DATA) is the single
source of truth for kill switches. Any unattended automation should have a pausable control there
and check it before running. The convergence loop (converge.converge_quality) burns `claude -p`
quota unattended, so it gets a kill switch here.

THE KEY
-------
    APPLY_CONVERGE_LOOP

Add an entry to `paused_registry.json`'s `entries[]` with `"id": "APPLY_CONVERGE_LOOP"` and an
ACTIVE status (`paused` or `mothballed`) to HALT the loop. While that entry is active,
`converge_quality` returns "paused" WITHOUT running any audit / fix / `claude -p` call. Remove the
entry (or set its status to anything not in the active set) to resume.

Example entry to paste into `paused_registry.json` -> `entries`:

    {
      "id": "APPLY_CONVERGE_LOOP",
      "type": "feature",
      "name": "Apply Autonomous Convergence Loop",
      "status": "paused",
      "reason": "Pausing the unattended audit->fix->re-audit loop (claude -p quota / debugging).",
      "paused_since": "YYYY-MM-DD",
      "unblock_condition": "the user re-enables the autonomous convergence loop.",
      "memory_ref": null,
      "owner": "the user",
      "do_not_alert": true
    }

FAIL-SAFE (hard constraint)
---------------------------
If `paused_registry.json` is MISSING or MALFORMED, treat the loop as NOT paused (don't block normal
operation). A kill switch that fails CLOSED on a corrupt registry would silently halt every apply —
the opposite of safe here. So every error path returns `is_loop_paused() == False`. NEVER raises.

The registry path is read from `config.ARIA_DATA` (your data hub), so it resolves the same file any
other component reads. Tests point `config.ARIA_DATA` at a throwaway dir (no real registry mutation).
"""
import json
from pathlib import Path
from typing import Optional

from . import config

# The kill-switch key for the convergence loop. Document this in any registry edit.
CONVERGE_LOOP_KEY = "APPLY_CONVERGE_LOOP"

# A registry entry counts as ACTIVE (the switch is THROWN, loop halted) when its status is one of
# these. An entry that is absent, or whose status is anything else, does NOT pause the loop.
_ACTIVE_STATUSES = {"paused", "mothballed", "deprecated", "disabled"}


def _registry_path() -> Path:
    """Path to paused_registry.json in the shared data hub (config resolves ARIA_CORE_DATA)."""
    return config.ARIA_DATA / "paused_registry.json"


def is_paused(key: str, manifest_path: Optional[Path] = None) -> bool:
    """True iff the paused_registry carries an ACTIVE entry whose `id` == `key`.

    FAIL-SAFE: a missing/corrupt/malformed registry, or any unexpected error, returns False (NOT
    paused) — a kill switch must never halt normal operation just because its own config file is
    unreadable. NEVER raises.
    """
    try:
        rp = Path(manifest_path) if manifest_path else _registry_path()
        if not rp.exists():
            return False
        data = json.loads(rp.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        entries = data.get("entries")
        if not isinstance(entries, list):
            return False
        for e in entries:
            if not isinstance(e, dict):
                continue
            if e.get("id") == key:
                status = (e.get("status") or "").strip().lower()
                return status in _ACTIVE_STATUSES
        return False
    except Exception:
        return False  # fail-safe: unreadable registry -> NOT paused, never crash the loop


def is_loop_paused(manifest_path: Optional[Path] = None) -> bool:
    """True iff the convergence loop is HALTED via the APPLY_CONVERGE_LOOP kill switch. Fail-safe
    (see is_paused). Call this at the TOP of converge_quality, before any audit/fix/claude -p."""
    return is_paused(CONVERGE_LOOP_KEY, manifest_path=manifest_path)
