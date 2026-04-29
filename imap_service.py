"""IMAP fetcher for unread emails.

Connects with the user-supplied credentials, lists UNSEEN messages, and
returns plain-text bodies + headers. Credentials live only inside the
request — never stored, never logged.
"""
import email
import imaplib
import ssl
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Optional

from bs4 import BeautifulSoup


# Default IMAP host map by email domain. Users can override with custom_host.
DEFAULT_HOSTS: dict[str, tuple[str, int]] = {
    "gmail.com":     ("imap.gmail.com", 993),
    "googlemail.com":("imap.gmail.com", 993),
    "outlook.com":   ("outlook.office365.com", 993),
    "hotmail.com":   ("outlook.office365.com", 993),
    "live.com":      ("outlook.office365.com", 993),
    "office365.com": ("outlook.office365.com", 993),
    "icloud.com":    ("imap.mail.me.com", 993),
    "me.com":        ("imap.mail.me.com", 993),
    "yahoo.com":     ("imap.mail.yahoo.com", 993),
    "qq.com":        ("imap.qq.com", 993),
    "163.com":       ("imap.163.com", 993),
    "126.com":       ("imap.126.com", 993),
    "sina.com":      ("imap.sina.com", 993),
    "foxmail.com":   ("imap.qq.com", 993),
}


def guess_host(email_addr: str) -> Optional[tuple[str, int]]:
    domain = email_addr.split("@")[-1].strip().lower()
    return DEFAULT_HOSTS.get(domain)


def _decode(s) -> str:
    if not s:
        return ""
    if isinstance(s, bytes):
        try:
            return s.decode("utf-8", errors="replace")
        except Exception:
            return s.decode("latin-1", errors="replace")
    parts = decode_header(s)
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(charset or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _extract_body(msg) -> str:
    """Walk the MIME tree, prefer text/plain, fall back to html-stripped text."""
    text_parts: list[str] = []
    html_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            if ctype == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    text_parts.append(_decode(payload))
            elif ctype == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    html_parts.append(_decode(payload))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            ctype = msg.get_content_type()
            if ctype == "text/html":
                html_parts.append(_decode(payload))
            else:
                text_parts.append(_decode(payload))

    text = "\n\n".join(t for t in text_parts if t.strip())
    if text:
        return text.strip()
    if html_parts:
        soup = BeautifulSoup("\n".join(html_parts), "html.parser")
        return soup.get_text("\n").strip()
    return ""


def fetch_unread(
    email_addr: str,
    password: str,
    host: Optional[str] = None,
    port: int = 993,
    limit: int = 5,
) -> list[dict]:
    """Return up to `limit` unread emails. Marks them read=False (PEEK) so we
    don't change the server's read state.
    """
    if not host:
        guess = guess_host(email_addr)
        if not guess:
            raise ValueError("Unknown email provider. Please supply IMAP host explicitly.")
        host, port = guess

    ctx = ssl.create_default_context()
    try:
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=15)
    except Exception as e:
        raise RuntimeError(f"Cannot reach IMAP server {host}: {e}")

    try:
        try:
            conn.login(email_addr, password)
        except imaplib.IMAP4.error as e:
            raise RuntimeError(f"IMAP login failed (use an app-specific password, not your account password): {e}")

        conn.select("INBOX")
        typ, data = conn.search(None, "UNSEEN")
        if typ != "OK":
            raise RuntimeError("IMAP search failed")
        ids = (data[0] or b"").split()
        # Newest first
        ids = list(reversed(ids))[: max(1, min(limit, 20))]

        results: list[dict] = []
        for mid in ids:
            typ, msg_data = conn.fetch(mid, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            from_name, from_addr = parseaddr(_decode(msg.get("From")))
            try:
                dt = parsedate_to_datetime(msg.get("Date")).strftime("%Y-%m-%d %H:%M") if msg.get("Date") else ""
            except Exception:
                dt = ""
            results.append({
                "id": mid.decode() if isinstance(mid, bytes) else str(mid),
                "subject": _decode(msg.get("Subject")),
                "from_name": from_name,
                "from_addr": from_addr,
                "date": dt,
                "body": _extract_body(msg)[:20000],  # cap to keep prompts sane
            })
        return results
    finally:
        try:
            conn.logout()
        except Exception:
            pass
