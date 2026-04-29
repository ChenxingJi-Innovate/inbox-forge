"""
Inbox Forge - bilingual AI email summarizer with Google + Microsoft OAuth.

Architecture:
- Identity: "Sign in with Google" or "Sign in with Microsoft" creates a user
  and an oauth_connection in one step. The same OAuth grant carries the inbox
  read scope (gmail.readonly / Mail.Read), so connect-once = sign in + inbox.
- Storage: Postgres holds users, sessions, oauth_connections, records.
  Provider refresh tokens are encrypted with TOKEN_ENC_KEY.
- Stateless analyze + Excel build remain available without sign-in.
"""
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import (
    Cookie, Depends, FastAPI, File, Form, HTTPException, Query,
    Request, Response, UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import auth
import db
import inbox_gmail
import inbox_outlook
import oauth_providers as oauth
from crypto_util import decrypt, encrypt
from excel_service import build_workbook
from gemini_service import analyze_email

load_dotenv()

app = FastAPI(title="Inbox Forge")

BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# In-process state cache for OAuth CSRF tokens. Stateless instances will lose
# these on restart — for the demo this is acceptable since the lifetime is
# under a minute. Could move to a Redis/KV later if multi-instance is needed.
_oauth_states: dict[str, float] = {}
_OAUTH_STATE_TTL = 600  # 10 min


def _norm_lang(lang: Optional[str]) -> str:
    return "zh" if str(lang or "").lower() == "zh" else "en"


def _put_state(state: str) -> None:
    _oauth_states[state] = time.time() + _OAUTH_STATE_TTL
    # opportunistic GC
    now = time.time()
    expired = [k for k, t in _oauth_states.items() if t < now]
    for k in expired:
        _oauth_states.pop(k, None)


def _consume_state(state: str) -> bool:
    deadline = _oauth_states.pop(state, 0)
    return deadline > time.time()


# ---------- Pages ----------

@app.get("/")
async def root():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
async def health():
    return {
        "gemini_key_set": bool(os.getenv("GEMINI_API_KEY")),
        "google_oauth_set": bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET")),
        "microsoft_oauth_set": bool(os.getenv("MICROSOFT_CLIENT_ID") and os.getenv("MICROSOFT_CLIENT_SECRET")),
        "db_set": bool(os.getenv("DATABASE_URL")),
    }


# ---------- Auth: identity + me ----------

@app.get("/api/me")
async def me(request: Request):
    user = auth.current_user_optional(request)
    if not user:
        return {"user": None, "connections": []}
    conns = db.list_oauth_connections(user["id"])
    return {
        "user": {"id": user["id"], "email": user["email"], "name": user.get("name"), "avatar_url": user.get("avatar_url")},
        "connections": [
            {
                "id": c["id"],
                "provider": c["provider"],
                "provider_email": c["provider_email"],
            } for c in conns
        ],
    }


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get(auth.SESSION_COOKIE)
    auth.clear_session(response, token)
    return {"success": True}


# ---------- OAuth: Google ----------

@app.get("/api/auth/google/start")
async def google_start():
    state = oauth.make_state()
    _put_state(state)
    return RedirectResponse(oauth.google_auth_url(state), status_code=302)


@app.get("/api/auth/google/callback")
async def google_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    if error:
        return RedirectResponse(f"/?oauth_error={error}", status_code=302)
    if not code or not state or not _consume_state(state):
        return RedirectResponse("/?oauth_error=bad_state", status_code=302)
    try:
        tokens = oauth.google_exchange_code(code)
        info = oauth.google_userinfo(tokens["access_token"])
    except Exception as e:
        return RedirectResponse(f"/?oauth_error={type(e).__name__}", status_code=302)

    email = (info.get("email") or "").lower()
    name = info.get("name")
    avatar = info.get("picture")
    user = db.upsert_user(email=email, name=name, avatar_url=avatar)

    db.upsert_oauth_connection(
        user_id=user["id"],
        provider="google",
        provider_email=email,
        access_token_enc=encrypt(tokens["access_token"]),
        refresh_token_enc=encrypt(tokens["refresh_token"]) if tokens.get("refresh_token") else None,
        expires_at=oauth.expires_at_from_seconds(tokens.get("expires_in")),
        scope=tokens.get("scope"),
    )

    response = RedirectResponse("/?signed_in=google", status_code=302)
    auth.issue_session(response, user["id"])
    return response


# ---------- OAuth: Microsoft ----------

@app.get("/api/auth/microsoft/start")
async def microsoft_start():
    state = oauth.make_state()
    _put_state(state)
    return RedirectResponse(oauth.microsoft_auth_url(state), status_code=302)


@app.get("/api/auth/microsoft/callback")
async def microsoft_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    if error:
        return RedirectResponse(f"/?oauth_error={error}", status_code=302)
    if not code or not state or not _consume_state(state):
        return RedirectResponse("/?oauth_error=bad_state", status_code=302)
    try:
        tokens = oauth.microsoft_exchange_code(code)
        info = oauth.microsoft_userinfo(tokens["access_token"])
    except Exception as e:
        return RedirectResponse(f"/?oauth_error={type(e).__name__}", status_code=302)

    email = (info.get("mail") or info.get("userPrincipalName") or "").lower()
    name = info.get("displayName")
    if not email:
        return RedirectResponse("/?oauth_error=no_email", status_code=302)

    user = db.upsert_user(email=email, name=name, avatar_url=None)
    db.upsert_oauth_connection(
        user_id=user["id"],
        provider="microsoft",
        provider_email=email,
        access_token_enc=encrypt(tokens["access_token"]),
        refresh_token_enc=encrypt(tokens["refresh_token"]) if tokens.get("refresh_token") else None,
        expires_at=oauth.expires_at_from_seconds(tokens.get("expires_in")),
        scope=tokens.get("scope"),
    )

    response = RedirectResponse("/?signed_in=microsoft", status_code=302)
    auth.issue_session(response, user["id"])
    return response


# ---------- Inbox fetch (uses stored OAuth tokens) ----------

def _live_access_token(user_id: int, conn_id: int, provider: str) -> str:
    """Return a fresh access token, refreshing via the stored refresh token if needed."""
    conn = db.get_oauth_connection(user_id, conn_id)
    if not conn or conn["provider"] != provider:
        raise HTTPException(status_code=404, detail="Connection not found")

    access = decrypt(conn["access_token_enc"])
    expires_at = conn["expires_at"]
    needs_refresh = expires_at and expires_at <= datetime.now(timezone.utc)
    if not needs_refresh:
        return access

    refresh = decrypt(conn["refresh_token_enc"]) if conn["refresh_token_enc"] else None
    if not refresh:
        raise HTTPException(status_code=400, detail="Token expired and no refresh token; please re-connect")
    try:
        new_tokens = (oauth.google_refresh if provider == "google" else oauth.microsoft_refresh)(refresh)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Refresh failed: {e}; please re-connect")
    new_access = new_tokens["access_token"]
    db.update_oauth_tokens(
        conn_id=conn_id,
        access_token_enc=encrypt(new_access),
        expires_at=oauth.expires_at_from_seconds(new_tokens.get("expires_in")),
    )
    return new_access


@app.get("/api/inbox/{conn_id}/unread")
async def inbox_unread(conn_id: int, limit: int = 5, user: dict = Depends(auth.require_user)):
    conn = db.get_oauth_connection(user["id"], conn_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    access = _live_access_token(user["id"], conn_id, conn["provider"])
    try:
        if conn["provider"] == "google":
            emails = inbox_gmail.fetch_unread(access, limit=limit)
        elif conn["provider"] == "microsoft":
            emails = inbox_outlook.fetch_unread(access, limit=limit)
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported provider: {conn['provider']}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fetch failed: {e}")
    return {"success": True, "emails": emails}


@app.delete("/api/inbox/{conn_id}")
async def inbox_disconnect(conn_id: int, user: dict = Depends(auth.require_user)):
    db.delete_oauth_connection(user["id"], conn_id)
    return {"success": True}


# ---------- Analyze (no sign-in required) ----------

MAX_TOTAL_BYTES = 40 * 1024 * 1024
MAX_FILES = 8


@app.post("/api/analyze")
async def analyze(
    files: List[UploadFile] = File(default=[]),
    text: str = Form(""),
    lang: str = Form("en"),
):
    lang = _norm_lang(lang)
    text = (text or "").strip()
    images_bytes: list[bytes] = []
    total = 0

    if files and len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"Too many screenshots (max {MAX_FILES})")
    for f in files or []:
        if not f or not f.filename:
            continue
        if not f.content_type or not f.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="Only image files are supported")
        content = await f.read()
        if not content:
            continue
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise HTTPException(status_code=400, detail="Total image size too large (max 40MB)")
        images_bytes.append(content)

    if not images_bytes and not text:
        raise HTTPException(status_code=400, detail="Provide at least one screenshot or paste email text")

    try:
        result = analyze_email(images=images_bytes, text=text, lang=lang)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gemini error: {e}")

    return {
        "success": True,
        "summary": result.get("summary", ""),
        "action_items": result.get("action_items", ""),
        "sender": result.get("sender", ""),
        "company": result.get("company", ""),
        "subject": result.get("subject", ""),
    }


# ---------- Records ----------

class RecordIn(BaseModel):
    customer_name: str
    company: str = ""
    date: str = ""
    summary: str = ""
    action_items: str = ""
    status: str = ""


@app.get("/api/records")
async def get_records(request: Request):
    user = auth.current_user_optional(request)
    if not user:
        return {"success": True, "records": [], "anonymous": True}
    rows = db.list_records(user["id"])
    return {
        "success": True,
        "records": [
            {
                **r,
                "created_at": r["created_at"].isoformat() if r.get("created_at") else "",
            } for r in rows
        ],
    }


@app.post("/api/records")
async def post_record(record: RecordIn, user: dict = Depends(auth.require_user)):
    if not record.customer_name.strip():
        raise HTTPException(status_code=400, detail="Customer name is required")
    row = db.add_record(user["id"], record.model_dump())
    row["created_at"] = row["created_at"].isoformat() if row.get("created_at") else ""
    return {"success": True, "record": row}


@app.delete("/api/records/{record_id}")
async def delete_record(record_id: int, user: dict = Depends(auth.require_user)):
    db.delete_record(user["id"], record_id)
    return {"success": True}


@app.delete("/api/records")
async def clear_records(user: dict = Depends(auth.require_user)):
    db.clear_records(user["id"])
    return {"success": True}


# ---------- Excel ----------

class ExcelBody(BaseModel):
    records: list[dict]
    lang: str = "en"


@app.post("/api/excel")
async def excel_endpoint(body: ExcelBody):
    lang = _norm_lang(body.lang)
    try:
        data = build_workbook(body.records or [], lang=lang)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Excel build failed: {e}")
    filename = "email_records_zh.xlsx" if lang == "zh" else "email_records_en.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------- Errors ----------

@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# ---------- Local run ----------

if __name__ == "__main__":
    import socket
    import uvicorn

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"

    print()
    print("=" * 50)
    print(" Inbox Forge running!")
    print(f"   Local:    http://127.0.0.1:8000")
    print(f"   Network:  http://{local_ip}:8000")
    print("=" * 50)
    print()

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
