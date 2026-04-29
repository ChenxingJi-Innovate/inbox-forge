"""Gmail unread fetch via Gmail API.

Uses an OAuth access token (from oauth_connections). Returns parsed plain-text
bodies + headers, ready for analyze_email().
"""
import base64
import re
from typing import Iterable

import httpx
from bs4 import BeautifulSoup


GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


def _b64url_decode(data: str) -> bytes:
    pad = 4 - (len(data) % 4)
    if pad and pad != 4:
        data += "=" * pad
    return base64.urlsafe_b64decode(data.encode())


def _walk_parts(payload: dict) -> Iterable[dict]:
    if not payload:
        return
    yield payload
    for p in payload.get("parts", []) or []:
        yield from _walk_parts(p)


def _extract_body(payload: dict) -> str:
    text_parts: list[str] = []
    html_parts: list[str] = []
    for p in _walk_parts(payload):
        mime = p.get("mimeType", "")
        body = p.get("body", {}) or {}
        data = body.get("data")
        if not data:
            continue
        try:
            raw = _b64url_decode(data).decode("utf-8", errors="replace")
        except Exception:
            continue
        if mime == "text/plain":
            text_parts.append(raw)
        elif mime == "text/html":
            html_parts.append(raw)
    text = "\n\n".join(t for t in text_parts if t.strip()).strip()
    if text:
        return text
    if html_parts:
        soup = BeautifulSoup("\n".join(html_parts), "html.parser")
        return soup.get_text("\n").strip()
    return ""


def _header(headers: list[dict], name: str) -> str:
    for h in headers or []:
        if (h.get("name") or "").lower() == name.lower():
            return h.get("value", "")
    return ""


def fetch_unread(access_token: str, limit: int = 5) -> list[dict]:
    """Pull up to `limit` unread messages from INBOX."""
    headers = {"Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=30) as client:
        # Gmail uses search syntax. is:unread + INBOX label.
        r = client.get(
            f"{GMAIL_BASE}/messages",
            headers=headers,
            params={"q": "is:unread in:inbox", "maxResults": max(1, min(20, limit))},
        )
        r.raise_for_status()
        ids = [m["id"] for m in (r.json().get("messages") or [])]

        results: list[dict] = []
        for mid in ids:
            mr = client.get(
                f"{GMAIL_BASE}/messages/{mid}",
                headers=headers,
                params={"format": "full"},
            )
            mr.raise_for_status()
            msg = mr.json()
            payload = msg.get("payload") or {}
            hs = payload.get("headers") or []
            from_raw = _header(hs, "From")
            # crude name + addr split
            m = re.match(r'^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$', from_raw)
            from_name, from_addr = (m.group(1).strip(), m.group(2).strip()) if m else ("", from_raw)
            results.append({
                "id": mid,
                "subject": _header(hs, "Subject"),
                "from_name": from_name,
                "from_addr": from_addr,
                "date": _header(hs, "Date"),
                "body": _extract_body(payload)[:20000],
            })
        return results
