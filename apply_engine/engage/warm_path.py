"""warm_path — a STUB / documented integration hook (NOT a working outreach engine).

In the private system this module sources the best warm-path advocate for a target
company (a hiring/eng manager, a peer on the team, or a founder), verifies a
deliverable email through a third-party provider, drafts a tailored note in the
user's voice, and stages it onto a contact at the SEND BRINK — it never sends.

That real implementation needs things this public repo deliberately does NOT bundle:
an email-verification API key, a model/CLI to do the sourcing and drafting, and your
own identity/voice material. So this is only the SHAPE of the seam.

To enable warm-path sourcing in your own deployment, replace `find_path` with an
implementation that:
  1. sources a candidate advocate at `company` for `title` (your model / data source),
  2. verifies a deliverable email for them (your email-verification provider),
  3. drafts an outreach note in your own voice, and
  4. stages it onto a contact with outreach.verify.ok reflecting the verification,
     via engage.crm_util.add_contact (which keeps it SEND-BLOCKED until verified).

As shipped, `find_path` is inert: it sources nothing, calls no network, and returns
a clear "outreach not configured" result so the orchestrator routes the target to
bucket C (needs-work) and continues.
"""
from __future__ import annotations

from typing import Optional


def is_configured(config: Optional[dict] = None) -> bool:
    """Whether a real warm-path provider has been wired in. Always False in this
    public build — there is no provider to configure. Override this module to enable."""
    return False


def find_path(company: str, title: Optional[str] = None, *, config: Optional[dict] = None) -> dict:
    """Stub. Returns a clear "not configured" result instead of sourcing a contact.

    Real implementations should return a dict that at minimum carries:
      {"ok": bool, "status": str, "company": str, "contact_id": str | None}
    where ok=True means a contact was sourced AND its email was verified (staged at
    the send brink), and ok=False routes the target to bucket C.
    """
    return {
        "ok": False,
        "status": "outreach_not_configured",
        "company": company,
        "title": title,
        "contact_id": None,
        "detail": ("warm-path outreach is not configured in this build — provide your "
                   "own contact-sourcing + email-verification provider (see module docstring)"),
    }
