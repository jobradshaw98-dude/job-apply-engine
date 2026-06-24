"""Read a Workday account-verification link from Sam's inbox so the engine can clear
tenants (e.g. ResMed) that require email verification before the apply wizard proceeds.

Pure `extract_verify_link` (unit-tested) + a thin IMAP fetcher (live, brief_config creds)."""
import re

_URL_RE = re.compile(r'https?://[^\s"\'<>)\]]+', re.IGNORECASE)
_VERIFY_KW = ("verifyemail", "emailverification", "verify", "register", "activate", "confirm")


def extract_verify_link(body: str, tenant_host: str = "") -> str:
    """Find the Workday verification URL in an email body. Prefers a URL that matches the
    tenant host (or any *workday* domain) AND looks like a verify link. PURE. Returns the URL
    or None. Trailing punctuation is stripped."""
    if not body:
        return None
    host = (tenant_host or "").lower()
    urls = [u.rstrip(').,>"\'') for u in _URL_RE.findall(body)]
    cands = [u for u in urls
             if "workday" in u.lower() or (host and host in u.lower())]
    if not cands:
        return None
    for kw in _VERIFY_KW:
        for u in cands:
            if kw in u.lower():
                return u
    return cands[0]


def _msg_body(msg) -> str:
    """Flatten an email.message into searchable text (prefers text/html for href links)."""
    parts = []
    if msg.is_multipart():
        for p in msg.walk():
            ct = p.get_content_type()
            if ct in ("text/plain", "text/html"):
                try:
                    parts.append(p.get_payload(decode=True).decode(
                        p.get_content_charset() or "utf-8", "ignore"))
                except Exception:
                    pass
    else:
        try:
            parts.append(msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", "ignore"))
        except Exception:
            pass
    return "\n".join(parts)


def _imap_creds():
    """(email_addr, app_password) from brief_config.json, or (None, None)."""
    import json
    from . import config
    try:
        c = json.loads((config.ARIA_DATA / "brief_config.json").read_text(encoding="utf-8"))
        return c.get("sender") or c.get("recipient"), c.get("smtp_password")
    except Exception:
        return None, None


def fetch_verify_link(tenant_host: str, lookback: int = 15,
                      imap_host: str = "imap.gmail.com") -> str:
    """LIVE: scan the most recent inbox mail from a Workday sender for this tenant's verify
    link. Returns the URL or None. Best-effort — swallows all errors (caller escalates)."""
    import imaplib
    import email as emaillib
    addr, pw = _imap_creds()
    if not (addr and pw):
        return None
    M = None
    try:
        M = imaplib.IMAP4_SSL(imap_host)
        M.login(addr, pw)
        M.select("INBOX")
        typ, data = M.search(None, '(OR FROM "workday" FROM "myworkday")')
        ids = data[0].split() if (data and data[0]) else []
        for eid in reversed(ids[-lookback:]):
            typ, md = M.fetch(eid, "(RFC822)")
            if not md or not md[0]:
                continue
            msg = emaillib.message_from_bytes(md[0][1])
            link = extract_verify_link(_msg_body(msg), tenant_host)
            if link:
                return link
        return None
    except Exception:
        return None
    finally:
        try:
            if M is not None:
                M.logout()
        except Exception:
            pass
