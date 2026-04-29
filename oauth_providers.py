"""OAuth provider configs and token-exchange helpers (Google + Microsoft Graph).

Both flows are implemented directly with httpx — no Authlib dependency. Refresh
tokens are returned to the caller, which encrypts them before storing.
"""
import os
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx


# ---------- shared ----------

def _base_url() -> str:
    return os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def make_state() -> str:
    return secrets.token_urlsafe(32)


# ---------- Google ----------

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

GOOGLE_SCOPES = " ".join([
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.readonly",
])


def google_auth_url(state: str) -> str:
    cid = os.getenv("GOOGLE_CLIENT_ID")
    if not cid:
        raise RuntimeError("GOOGLE_CLIENT_ID not set")
    params = {
        "client_id": cid,
        "redirect_uri": f"{_base_url()}/api/auth/google/callback",
        "response_type": "code",
        "scope": GOOGLE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",          # force refresh_token issuance
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def google_exchange_code(code: str) -> dict:
    cid = os.getenv("GOOGLE_CLIENT_ID")
    sec = os.getenv("GOOGLE_CLIENT_SECRET")
    if not (cid and sec):
        raise RuntimeError("GOOGLE_CLIENT_ID/SECRET not set")
    with httpx.Client(timeout=20) as client:
        r = client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": cid,
                "client_secret": sec,
                "redirect_uri": f"{_base_url()}/api/auth/google/callback",
                "grant_type": "authorization_code",
            },
        )
        r.raise_for_status()
        return r.json()


def google_refresh(refresh_token: str) -> dict:
    cid = os.getenv("GOOGLE_CLIENT_ID")
    sec = os.getenv("GOOGLE_CLIENT_SECRET")
    with httpx.Client(timeout=20) as client:
        r = client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": cid,
                "client_secret": sec,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        r.raise_for_status()
        return r.json()


def google_userinfo(access_token: str) -> dict:
    with httpx.Client(timeout=20) as client:
        r = client.get(GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        r.raise_for_status()
        return r.json()


# ---------- Microsoft Graph ----------

# `common` tenant accepts personal + work/school
MS_AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
MS_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MS_GRAPH_ME = "https://graph.microsoft.com/v1.0/me"

MS_SCOPES = " ".join([
    "openid",
    "email",
    "profile",
    "offline_access",
    "User.Read",
    "Mail.Read",
])


def microsoft_auth_url(state: str) -> str:
    cid = os.getenv("MICROSOFT_CLIENT_ID")
    if not cid:
        raise RuntimeError("MICROSOFT_CLIENT_ID not set")
    params = {
        "client_id": cid,
        "redirect_uri": f"{_base_url()}/api/auth/microsoft/callback",
        "response_type": "code",
        "scope": MS_SCOPES,
        "response_mode": "query",
        "state": state,
        "prompt": "select_account",
    }
    return f"{MS_AUTH_URL}?{urllib.parse.urlencode(params)}"


def microsoft_exchange_code(code: str) -> dict:
    cid = os.getenv("MICROSOFT_CLIENT_ID")
    sec = os.getenv("MICROSOFT_CLIENT_SECRET")
    if not (cid and sec):
        raise RuntimeError("MICROSOFT_CLIENT_ID/SECRET not set")
    with httpx.Client(timeout=20) as client:
        r = client.post(
            MS_TOKEN_URL,
            data={
                "code": code,
                "client_id": cid,
                "client_secret": sec,
                "redirect_uri": f"{_base_url()}/api/auth/microsoft/callback",
                "grant_type": "authorization_code",
                "scope": MS_SCOPES,
            },
        )
        r.raise_for_status()
        return r.json()


def microsoft_refresh(refresh_token: str) -> dict:
    cid = os.getenv("MICROSOFT_CLIENT_ID")
    sec = os.getenv("MICROSOFT_CLIENT_SECRET")
    with httpx.Client(timeout=20) as client:
        r = client.post(
            MS_TOKEN_URL,
            data={
                "client_id": cid,
                "client_secret": sec,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": MS_SCOPES,
            },
        )
        r.raise_for_status()
        return r.json()


def microsoft_userinfo(access_token: str) -> dict:
    with httpx.Client(timeout=20) as client:
        r = client.get(MS_GRAPH_ME, headers={"Authorization": f"Bearer {access_token}"})
        r.raise_for_status()
        return r.json()


# ---------- helpers ----------

def expires_at_from_seconds(expires_in: Optional[int]) -> Optional[datetime]:
    if not expires_in:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in) - 30)
