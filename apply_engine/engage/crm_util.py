"""Canonical writers for contacts.json / applications.json — the single source of
truth for the CRM record schemas the engage agent mutates.

Why this exists: engage mutates contacts/applications autonomously. Hand-rolled
writes risk two failures:

  1. Id collisions — a writer that trusts an ad-hoc counter can hand out a
     colliding CON-NNN. `next_prefixed_id` ALWAYS derives from max(existing
     suffixes), so a stale counter can't win.
  2. Schema drift — a contact written without `outreach.body` / `warmth` / `id`
     silently fails to render on a dashboard. `normalize_*` coerces any shape
     onto the canonical schema.

Writes are atomic (temp + os.replace) so a crash can't truncate the CRM for any
concurrent reader.

SAFE-DEFAULT INVARIANT: a contact created here is send-BLOCKED by default
(outreach.verify.ok == False). A separate (here: stubbed) verification step must
flip it — the agent can never accidentally stage a sendable contact on an
unverified email.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from .. import config

ARIA_DATA = config.ARIA_DATA
CONTACTS_FILE = ARIA_DATA / "contacts.json"
APPLICATIONS_FILE = ARIA_DATA / "applications.json"

# contact statuses considered "open"/live (a dashboard renders these)
CONTACT_OPEN_DEFAULTS = {"prospect", "active", "contacted", "replied"}
# application default status when one is missing
APPLICATION_DEFAULT_STATUS = "drafted"

_ID_RE = re.compile(r"^([A-Z]+)-(\d+)$")


def _data_dir() -> Path:
    """Resolve the data dir live so tests that monkeypatch config.ARIA_DATA are honored."""
    return config.ARIA_DATA


def _contacts_file() -> Path:
    return _data_dir() / "contacts.json"


def _applications_file() -> Path:
    return _data_dir() / "applications.json"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# id allocation — collision-safe, derived from max() (never a stale counter)
# --------------------------------------------------------------------------- #

def next_prefixed_id(rows: list[dict], prefix: str) -> str:
    """Return the next `PREFIX-NNN` id, derived from the max existing numeric suffix.

    Foreign-prefix, missing, and malformed ids are ignored. Width is the wider of
    3 or the widest existing suffix, so we never shrink padding."""
    max_n = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        m = _ID_RE.match(str(r.get("id", "")))
        if m and m.group(1) == prefix:
            max_n = max(max_n, int(m.group(2)))
    return f"{prefix}-{max_n + 1:03d}"


# --------------------------------------------------------------------------- #
# normalize — coerce any historical shape onto the canonical schema
# --------------------------------------------------------------------------- #

def _coerce_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def normalize_contact(c: dict) -> dict:
    """Coerce a contact onto the canonical schema, in place. Never raises."""
    c.setdefault("name", "(unknown)")
    c.setdefault("company", "")
    c.setdefault("role", "")
    c["warmth"] = _coerce_int(c.get("warmth"), 1)
    c.setdefault("status", "prospect")
    c.setdefault("stream", "")
    c.setdefault("last_contact", None)
    c.setdefault("notes", "")

    o = c.get("outreach")
    if not isinstance(o, dict):
        o = {}
    # legacy: some writers used `.draft`; a dashboard reads `.body`. Migrate.
    if not o.get("body") and o.get("draft"):
        o["body"] = o.pop("draft")
    o.setdefault("body", "")
    o.setdefault("channel", "linkedin")
    o.setdefault("subject", "")
    o.setdefault("linkedin", o.get("body", ""))
    verify = o.get("verify")
    if not isinstance(verify, dict):
        verify = {}
    verify.setdefault("ok", False)          # send-blocked until verified
    verify.setdefault("status", "unverified")
    o["verify"] = verify
    c["outreach"] = o
    return c


def normalize_application(a: dict) -> dict:
    """Coerce an application onto the canonical schema, in place. Never raises."""
    a.setdefault("job_id", None)
    a.setdefault("company", "")
    a.setdefault("role", "")
    a.setdefault("track", None)
    if not a.get("status"):
        a["status"] = APPLICATION_DEFAULT_STATUS
    return a


# --------------------------------------------------------------------------- #
# load / save — atomic
# --------------------------------------------------------------------------- #

def _load_list(path: Path) -> list[dict]:
    # A not-yet-created CRM file is an empty CRM, not an error — so a fresh install
    # (no contacts.json / applications.json yet) runs cleanly instead of routing
    # every hygiene step to bucket C.
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    return raw.get("contacts", raw.get("applications", []))


def _save_list(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_contacts(path: Path | None = None, normalize: bool = True) -> list[dict]:
    rows = _load_list(path or _contacts_file())
    if normalize:
        for c in rows:
            if isinstance(c, dict):
                normalize_contact(c)
    return rows


def save_contacts(rows: list[dict], path: Path | None = None) -> None:
    _save_list(rows, path or _contacts_file())


def load_applications(path: Path | None = None, normalize: bool = True) -> list[dict]:
    rows = _load_list(path or _applications_file())
    if normalize:
        for a in rows:
            if isinstance(a, dict):
                normalize_application(a)
    return rows


def save_applications(rows: list[dict], path: Path | None = None) -> None:
    _save_list(rows, path or _applications_file())


# --------------------------------------------------------------------------- #
# add helpers — allocate id, normalize, append (caller still calls save_*)
# --------------------------------------------------------------------------- #

def add_contact(rows: list[dict], *, name: str, company: str = "", role: str = "",
                warmth: int = 1, stream: str = "", notes: str = "",
                linkedin_url: str | None = None, **extra) -> dict:
    c = {
        "id": next_prefixed_id(rows, "CON"),
        "name": name, "company": company, "role": role,
        "warmth": warmth, "stream": stream, "notes": notes,
    }
    if linkedin_url:
        c["linkedin_url"] = linkedin_url
    c.update(extra)
    normalize_contact(c)
    rows.append(c)
    return c


def add_application(rows: list[dict], *, job_id: str, company: str = "", role: str = "",
                    track: str | None = None, status: str | None = None, **extra) -> dict:
    a = {
        "id": next_prefixed_id(rows, "APP"),
        "job_id": job_id, "company": company, "role": role, "track": track,
    }
    if status:
        a["status"] = status
    a.update(extra)
    normalize_application(a)
    rows.append(a)
    return a
