"""Gmail fetch via Gmail API.

Uses an OAuth access token (from oauth_connections). Returns parsed plain-text
bodies + headers, ready for analyze_email() / analyze_with_context().

`fetch_unread` is kept for back-compat (old single-button sync). New callers
should use `fetch_messages` which takes proper filters: date range, read state,
arbitrary keyword query.
"""
import base64
import re
from typing import Iterable, Optional

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


def _build_query(
    *,
    read_state: str = "unread",  # "unread" | "read" | "all"
    since: Optional[str] = None,  # ISO date "YYYY-MM-DD"
    until: Optional[str] = None,
    keyword: Optional[str] = None,
) -> str:
    """Compose the Gmail `q=` search string. Gmail uses YYYY/MM/DD slashes."""
    parts: list[str] = ["in:inbox"]
    if read_state == "unread":
        parts.append("is:unread")
    elif read_state == "read":
        parts.append("is:read")
    # "all" adds neither
    if since:
        parts.append(f"after:{since.replace('-', '/')}")
    if until:
        parts.append(f"before:{until.replace('-', '/')}")
    if keyword and keyword.strip():
        parts.append(keyword.strip())
    return " ".join(parts)


def fetch_messages(
    access_token: str,
    *,
    read_state: str = "unread",
    since: Optional[str] = None,
    until: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Pull up to `limit` messages from INBOX matching the given filters.

    Returns a list of normalized dicts:
        {id, subject, from_name, from_addr, date, body}
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    q = _build_query(read_state=read_state, since=since, until=until, keyword=keyword)
    capped = max(1, min(500, limit))

    results: list[dict] = []
    with httpx.Client(timeout=30) as client:
        page_token: Optional[str] = None
        remaining = capped
        while remaining > 0:
            params = {"q": q, "maxResults": min(100, remaining)}
            if page_token:
                params["pageToken"] = page_token
            r = client.get(f"{GMAIL_BASE}/messages", headers=headers, params=params)
            r.raise_for_status()
            data = r.json()
            ids = [m["id"] for m in (data.get("messages") or [])]
            if not ids:
                break
            for mid in ids:
                if len(results) >= capped:
                    break
                mr = client.get(f"{GMAIL_BASE}/messages/{mid}", headers=headers, params={"format": "full"})
                mr.raise_for_status()
                msg = mr.json()
                payload = msg.get("payload") or {}
                hs = payload.get("headers") or []
                from_raw = _header(hs, "From")
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
            remaining = capped - len(results)
            page_token = data.get("nextPageToken")
            if not page_token or remaining <= 0:
                break
    return results


# Back-compat shim for older callers.
def fetch_unread(access_token: str, limit: int = 5) -> list[dict]:
    return fetch_messages(access_token, read_state="unread", limit=limit)
