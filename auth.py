"""Auth: password hashing + signed-cookie sessions."""
import os
import re

import bcrypt
from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeSerializer

import db

SESSION_COOKIE = "es_session"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# bcrypt limits the password to 72 bytes; pre-hashing keeps long passwords safe.
_BCRYPT_MAX = 72


def _serializer() -> URLSafeSerializer:
    secret = os.getenv("SESSION_SECRET")
    if not secret:
        raise RuntimeError("SESSION_SECRET not set in .env")
    return URLSafeSerializer(secret, salt="email-summarizer-session")


def hash_password(plain: str) -> str:
    pw = plain.encode("utf-8")[:_BCRYPT_MAX]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        pw = plain.encode("utf-8")[:_BCRYPT_MAX]
        return bcrypt.checkpw(pw, hashed.encode("utf-8"))
    except Exception:
        return False


def validate_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    return email


def validate_password(pw: str) -> str:
    if not pw or len(pw) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    if len(pw) > 200:
        raise HTTPException(status_code=400, detail="密码过长")
    return pw


def issue_session(response: Response, user_id: int):
    token = _serializer().dumps({"uid": user_id})
    # Cookie attrs: httpOnly, lax, 30 days. Secure flag controlled by env (prod=true).
    secure = os.getenv("COOKIE_SECURE", "false").lower() == "true"
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")


def current_user(request: Request) -> dict:
    """Resolve the logged-in user, or raise 401."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        data = _serializer().loads(token)
    except BadSignature:
        raise HTTPException(status_code=401, detail="会话已失效")
    user = db.get_user_by_id(data.get("uid"))
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user
