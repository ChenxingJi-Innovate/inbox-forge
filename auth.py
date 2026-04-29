"""Cookie-based session auth.

The session token is a 32-byte random hex string stored in an httpOnly cookie.
Server-side mapping lives in the `sessions` table.
"""
import os
import secrets

from fastapi import Cookie, HTTPException, Request, Response

import db


SESSION_COOKIE = "ifs"   # inbox-forge session
SESSION_DAYS = 30


def _cookie_secure() -> bool:
    return os.getenv("COOKIE_SECURE", "false").lower() == "true"


def issue_session(response: Response, user_id: int) -> str:
    token = secrets.token_hex(32)
    db.create_session(user_id, token)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
    )
    return token


def clear_session(response: Response, token: str | None = None) -> None:
    if token:
        try:
            db.delete_session(token)
        except Exception:
            pass
    response.delete_cookie(SESSION_COOKIE, path="/")


def current_user_optional(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return db.get_user_by_session(token)


def require_user(request: Request) -> dict:
    user = current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user
