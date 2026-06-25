"""Contact-hygiene self-healing for the engage agent.

Reusable, mostly-deterministic checks that keep the networking CRM clean WITHOUT
a human in the loop. Consumed by the engage hygiene lane so the gaps a human used
to hand-patch (contacts with no LinkedIn, stale/overdue outreach notes,
phantom-"active" rows) self-heal on every run.

Two kinds of repair:
  - DETERMINISTIC (no LLM, always safe): outreach-freshness — set a missing
    next_follow_up from cadence, clamp a future-dated last_contact, surface
    overdue/phantom rows for review.
  - LLM-GATED (web search, capped, verify-gated): source a missing LinkedIn URL
    by name+company. `source_linkedin` takes an INJECTABLE `runner(prompt)->text`
    so it is fully testable with a stub; it has NO default model shell-out in this
    public build — you must supply your own `runner` to enable it. Only a
    high-confidence match whose evidence names the right company is accepted.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

# A contact is "engaged" once it's been reached out to — only these expect a
# follow-up cadence. prospects/new are pre-outreach and are NOT flagged stale.
ENGAGED_STATUSES = {"contacted", "active", "replied"}
# never act on these (superset of the dead-status sets used elsewhere)
DEAD_STATUSES = {"dead", "closed", "rejected", "declined", "bounced", "not_interested"}
DEFAULT_CADENCE_DAYS = 7


def _parse_date(s):
    """Lenient date parse -> datetime or None. Handles ISO, tz-suffixed, date-only."""
    if not s:
        return None
    s = str(s)[:19]
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _cadence(c, default=DEFAULT_CADENCE_DAYS):
    try:
        return int(c.get("follow_up_cadence_days") or default)
    except (TypeError, ValueError):
        return default


# -- Deterministic: outreach freshness --------------------------------------- #
def find_outreach_issues(contacts, today):
    """Return [{id, issue, detail}] for engaged contacts whose outreach tracking is
    broken/stale. Pure — no mutation. Issues:
      future_last_contact  — last_contact dated after today (data error)
      phantom_engaged      — status engaged but no last_contact AND no outreach body
      missing_followup     — has last_contact but no next_follow_up (never resurfaces)
      overdue_followup     — next_follow_up older than today by > cadence
    """
    issues = []
    for c in contacts:
        if not isinstance(c, dict):
            continue
        st = (c.get("status") or "").lower()
        if st in DEAD_STATUSES:
            continue
        cid = c.get("id")
        lc = _parse_date(c.get("last_contact"))
        nf = _parse_date(c.get("next_follow_up"))
        if lc and lc.date() > today.date():
            issues.append({"id": cid, "issue": "future_last_contact",
                           "detail": f"last_contact={c.get('last_contact')} is in the future"})
        if st in ENGAGED_STATUSES:
            body = (c.get("outreach") or {}).get("body") if isinstance(c.get("outreach"), dict) else None
            if not lc and not body:
                issues.append({"id": cid, "issue": "phantom_engaged",
                               "detail": f"status={st} but no last_contact and no outreach draft"})
            elif lc and not nf:
                issues.append({"id": cid, "issue": "missing_followup",
                               "detail": f"contacted {c.get('last_contact')} but no next_follow_up set"})
            elif nf and (today - nf).days > _cadence(c):
                issues.append({"id": cid, "issue": "overdue_followup",
                               "detail": f"follow-up due {c.get('next_follow_up')} — {(today - nf).days}d overdue"})
    return issues


def repair_followups(contacts, today, default_cadence=DEFAULT_CADENCE_DAYS):
    """Deterministic IN-PLACE repair. Returns [{id, field, before, after}].
      - clamp a future-dated last_contact back to today (data error)
      - for engaged contacts with a last_contact but no next_follow_up, set
        next_follow_up = last_contact + cadence so the follow-up window resurfaces them
    Does NOT touch overdue/phantom rows — those need judgment, so they are only
    flagged (via find_outreach_issues), never auto-mutated.
    """
    changes = []
    for c in contacts:
        if not isinstance(c, dict):
            continue
        st = (c.get("status") or "").lower()
        if st in DEAD_STATUSES:
            continue
        cid = c.get("id")
        lc = _parse_date(c.get("last_contact"))
        if lc and lc.date() > today.date():
            before = c.get("last_contact")
            c["last_contact"] = today.strftime("%Y-%m-%d")
            changes.append({"id": cid, "field": "last_contact", "before": before, "after": c["last_contact"]})
            lc = today
        if st in ENGAGED_STATUSES and lc and not _parse_date(c.get("next_follow_up")):
            nf = (lc + timedelta(days=_cadence(c, default_cadence))).strftime("%Y-%m-%d")
            before = c.get("next_follow_up")
            c["next_follow_up"] = nf
            changes.append({"id": cid, "field": "next_follow_up", "before": before, "after": nf})
    return changes


# -- LLM-gated: LinkedIn sourcing -------------------------------------------- #
def contacts_missing_linkedin(contacts):
    """Actionable (non-dead) contacts with no linkedin_url. Pure."""
    out = []
    for c in contacts:
        if not isinstance(c, dict):
            continue
        if (c.get("status") or "").lower() in DEAD_STATUSES:
            continue
        if not c.get("linkedin_url"):
            out.append(c)
    return out


def _extract_json(text):
    """First JSON object/array out of an LLM response (handles ``` fences). Self-
    contained so tests don't need any model wiring."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    for opener, closer in (("[", "]"), ("{", "}")):
        i, j = t.find(opener), t.rfind(closer)
        if i != -1 and j != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except Exception:
                continue
    return json.loads(t)


_LINKEDIN_PROMPT = """Find the canonical LinkedIn profile URL for this person. Match on BOTH name AND company.

Name: {name}
Company: {company}
Role/context: {role}

Use web search. Return ONLY a strict JSON object:
{{"linkedin_url": "https://www.linkedin.com/in/<slug>", "confidence": "high|medium|low", "evidence": "what confirmed the name+company match (mention the company)"}}

Rules:
- linkedin_url must be a canonical https://www.linkedin.com/in/<slug> profile (NOT a search/company page).
- confidence "high" ONLY if the profile clearly shows the right name AND the right company.
- If you cannot confidently match, set "linkedin_url": null and confidence "low".
- Never guess or fabricate a URL. A null is better than a wrong profile.
- Return ONLY the JSON object."""


def source_linkedin(contact, runner):
    """Source one contact's LinkedIn URL via web search.

    `runner(prompt) -> text` is REQUIRED — this public build ships no default model
    shell-out, so you must supply your own (tests inject a stub). Returns
    {linkedin_url, confidence, evidence} only on a VERIFIED high-confidence match,
    else None — the verify gate requires the company's first token to appear in the
    evidence (a cheap wrong-person guard).
    """
    name = (contact.get("name") or "").strip()
    company = (contact.get("company") or "").strip()
    if not name or runner is None:
        return None
    prompt = _LINKEDIN_PROMPT.format(name=name, company=company, role=contact.get("role") or "")
    try:
        data = _extract_json(runner(prompt))
    except Exception:
        return None
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return None
    url = data.get("linkedin_url")
    conf = (data.get("confidence") or "").lower()
    ev = (data.get("evidence") or "")
    if not url or conf != "high":
        return None
    if "linkedin.com/in/" not in url:
        return None
    # verify gate: the company must be named in the evidence (guards wrong-person matches)
    if company:
        token = re.split(r"[\s(/,]", company.strip())[0].lower()
        if token and token not in ev.lower():
            return None
    return {"linkedin_url": url, "confidence": conf, "evidence": ev}
