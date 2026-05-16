"""Outlook fetch via Microsoft Graph.

Uses an OAuth access token (Mail.Read scope). Returns the same shape as
inbox_gmail.fetch_messages so downstream callers don't care which provider
delivered the email.
"""
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup


GRAPH_MESSAGES = "https://graph.microsoft.com/v1.0/me/messages"


def _build_filter(
    *,
    read_state: str = "unread",
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> str:
    """Compose Microsoft Graph $filter expression. ISO-8601 UTC timestamps."""
    clauses: list[str] = []
    if read_state == "unread":
        clauses.append("isRead eq false")
    elif read_state == "read":
        clauses.append("isRead eq true")
    if since:
        clauses.append(f"receivedDateTime ge {since}T00:00:00Z")
    if until:
        clauses.append(f"receivedDateTime le {until}T23:59:59Z")
    return " and ".join(clauses)


def fetch_messages(
    access_token: str,
    *,
    read_state: str = "unread",
    since: Optional[str] = None,
    until: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Pull up to `limit` messages from Inbox matching the given filters.

    Note: Graph forbids combining $search with $filter, so when a keyword is
    supplied we drop the read-state / date filters and let the search query
    handle scoping. This matches Outlook's UI behavior (search bar widens
    results regardless of read state).
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    capped = max(1, min(500, limit))
    select = "subject,from,sender,receivedDateTime,bodyPreview,body,isRead"

    results: list[dict] = []
    with httpx.Client(timeout=30) as client:
        if keyword and keyword.strip():
            params = {
                "$search": f'"{keyword.strip()}"',
                "$top": min(25, capped),
                "$select": select,
            }
            headers_search = {**headers, "ConsistencyLevel": "eventual"}
            r = client.get(GRAPH_MESSAGES, headers=headers_search, params=params)
        else:
            f = _build_filter(read_state=read_state, since=since, until=until)
            params = {
                "$top": min(50, capped),
                "$orderby": "receivedDateTime desc",
                "$select": select,
            }
            if f:
                params["$filter"] = f
            r = client.get(GRAPH_MESSAGES, headers=headers, params=params)

        r.raise_for_status()
        items = r.json().get("value", []) or []
        for m in items:
            if len(results) >= capped:
                break
            from_obj = ((m.get("from") or {}).get("emailAddress") or {})
            body = m.get("body") or {}
            body_text = body.get("content", "") or ""
            if (body.get("contentType") or "").lower() == "html":
                soup = BeautifulSoup(body_text, "html.parser")
                body_text = soup.get_text("\n")
            results.append({
                "id": m.get("id"),
                "subject": m.get("subject", ""),
                "from_name": from_obj.get("name", ""),
                "from_addr": from_obj.get("address", ""),
                "date": m.get("receivedDateTime", ""),
                "body": (body_text or m.get("bodyPreview") or "").strip()[:20000],
            })
    return results


# Back-compat shim.
def fetch_unread(access_token: str, limit: int = 5) -> list[dict]:
    return fetch_messages(access_token, read_state="unread", limit=limit)
