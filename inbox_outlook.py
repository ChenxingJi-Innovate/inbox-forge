"""Outlook unread fetch via Microsoft Graph.

Uses an OAuth access token (Mail.Read scope). Returns the same shape as
inbox_gmail.fetch_unread for downstream use.
"""
import re

import httpx
from bs4 import BeautifulSoup


GRAPH_MESSAGES = "https://graph.microsoft.com/v1.0/me/messages"


def fetch_unread(access_token: str, limit: int = 5) -> list[dict]:
    headers = {"Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=30) as client:
        r = client.get(
            GRAPH_MESSAGES,
            headers=headers,
            params={
                "$filter": "isRead eq false",
                "$top": max(1, min(20, limit)),
                "$orderby": "receivedDateTime desc",
                "$select": "subject,from,sender,receivedDateTime,bodyPreview,body",
            },
        )
        r.raise_for_status()
        items = r.json().get("value", []) or []
        results: list[dict] = []
        for m in items:
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
